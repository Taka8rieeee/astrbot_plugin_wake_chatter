import random
import time

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import MessageChain, AstrMessageEvent, filter
from astrbot.api.star import Context, Star

# LLM 关闭时使用的内置随机回复库（可在配置中追加自定义回复语）
DEFAULT_REPLIES = [
    "有道理，我也这么觉得～",
    "哈哈哈哈，说得好！",
    "这个话题我熟，让我插一嘴：你们继续，我听着呢（狗头）",
    "确实。",
    "啊这，突然被点名的感觉（不是）",
    "前排围观，顺便水一句：大家聊得真热闹！",
    "楼上说得对，不过我觉得还有另一种可能（假装沉思）",
    "刚睡醒，你们聊到哪了？",
    "666",
    "mark 一下，这话题有意思。",
]

# 唤醒时（LLM 关闭或调用失败）的兜底欢迎语，{name} 会被替换为唤醒者昵称
DEFAULT_GREETINGS = [
    "我来啦～{name} 叫我什么事？",
    "在在在，{name} 有什么指示？",
    "谁在叫我？哦是 {name}，来咯来咯～",
    "叮——已上线！{name} 请讲～",
]


class WakeChatter(Star):
    """群聊唤醒陪聊插件。

    功能：
    1. 监听群聊消息，检测到唤醒词后进入"活跃"状态，并回复唤醒者；
    2. 活跃期间以一定概率随机插话，"偶尔加入大家的讨论"；
    3. 活跃时长可配置（默认 5 分钟），活跃期内再次唤醒会刷新倒计时；
    4. 单次活跃期内的最大回复条数可配置（超过后保持沉默直到休眠）；
    5. 活跃期内检测到休眠词会立即休眠；
    6. 所有参数均可在 WebUI 插件配置页修改。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 每个群的活跃状态: {group_id: {"active_until": float, "replies": int, "last_reply": float}}
        self.group_states: dict[str, dict] = {}
        # 每个群最近聊天记录（供 LLM 生成回复时参考）: {group_id: [(昵称, 文本), ...]}
        self.chat_history: dict[str, list] = {}

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #

    def _state(self, group_id: str) -> dict:
        """获取（或初始化）某个群的活跃状态。"""
        return self.group_states.setdefault(
            group_id, {"active_until": 0.0, "replies": 0, "last_reply": 0.0}
        )

    def _enabled_groups(self) -> list[str]:
        """配置中指定的生效群号列表（全部转为字符串）。"""
        return [str(g).strip() for g in (self.config.get("enabled_groups") or []) if str(g).strip()]

    def _record_history(self, group_id: str, name: str, text: str) -> None:
        """记录群聊最近消息，用于给 LLM 提供上下文。"""
        if not text:
            return
        hist = self.chat_history.setdefault(group_id, [])
        hist.append((name, text))
        # 只保留最近 20 条，防止内存无限增长
        if len(hist) > 20:
            del hist[: len(hist) - 20]

    def _cfg_replies(self, key: str, fallback: list[str]) -> list[str]:
        """读取配置中的自定义语料，为空时回退到内置语料。"""
        custom = [r for r in (self.config.get(key) or []) if str(r).strip()]
        return custom if custom else fallback

    async def _llm_reply(self, group_id: str, name: str, text: str, scene: str) -> str | None:
        """调用当前启用的 LLM 生成一条符合群聊语境的回复。失败返回 None。"""
        try:
            provider = self.context.get_using_provider()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[唤醒陪聊] 获取 LLM Provider 失败: {e}")
            return None
        if provider is None:
            return None

        hist = self.chat_history.get(group_id, [])
        history_lines = "\n".join(f"{n}: {t}" for n, t in hist[-12:])
        system_prompt = str(
            self.config.get("llm_system_prompt")
            or "你是 QQ 群聊里的一名普通活跃群友，说话口语化、简短自然，偶尔用表情和梗，不要像客服，不要自我介绍。"
        )
        user_prompt = (
            f"以下是群聊最近的聊天记录：\n{history_lines}\n\n"
            f"刚刚，群友「{name}」说：{text}\n"
            f"{'他喊了唤醒词，你在跟他打招呼。' if scene == 'wake' else '你想以普通群友身份接一句话。'}\n"
            f"请直接输出你要发送到群里的一句话，不要任何解释、引号或前缀。"
        )
        try:
            resp = await provider.text_chat(prompt=user_prompt, session_id=None, contexts=[])
            content = str(getattr(resp, "completion_text", "") or getattr(resp, "result_chain", "") or "").strip()
            return content or None
        except Exception as e:  # noqa: BLE001
            logger.error(f"[唤醒陪聊] LLM 生成回复失败: {e}")
            return None

    async def _send_reply(self, event: AstrMessageEvent, text: str, at_sender: bool) -> None:
        """通过 unified_msg_origin 主动向当前会话发送一条消息。"""
        max_len = int(self.config.get("max_reply_length") or 200)
        text = text[:max_len]
        chain = None
        if at_sender:
            # 尝试构造 @对方 + 文本 的消息链；非 QQ 平台或 ID 非数字时降级为纯文本
            try:
                chain = MessageChain(chain=[Comp.At(qq=int(event.get_sender_id())), Comp.Plain(" " + text)])
            except Exception:  # noqa: BLE001
                chain = None
        if chain is None:
            chain = MessageChain().message(text)
        try:
            await self.context.send_message(event.unified_msg_origin, chain)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[唤醒陪聊] 消息发送失败: {e}")

    # ------------------------------------------------------------------ #
    # 核心监听：群聊消息
    # ------------------------------------------------------------------ #

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=5)
    async def on_group_message(self, event: AstrMessageEvent):
        """监听所有群聊消息：负责唤醒检测、刷新唤醒时间与概率性自主回复。"""
        if not self.config.get("enabled", True):
            return

        msg = event.message_obj
        group_id = str(msg.group_id or "")
        if not group_id:
            return

        sender_id = str(event.get_sender_id() or "")
        # 忽略机器人自己的消息，避免自问自答
        if sender_id == str(msg.self_id or ""):
            return

        text = (event.message_str or "").strip()
        name = event.get_sender_name() or sender_id
        self._record_history(group_id, name, text)

        # 忽略指令消息（以 / 或 # 开头），避免和命令系统互相干扰
        if text.startswith("/") or text.startswith("#"):
            return

        # 群号白名单（为空表示所有群生效）
        enabled_groups = self._enabled_groups()
        if enabled_groups and group_id not in enabled_groups:
            return

        wake_words = [str(w).strip() for w in (self.config.get("wake_words") or []) if str(w).strip()]
        is_wake = bool(wake_words) and any(w in text for w in wake_words)
        sleep_words = [str(w).strip() for w in (self.config.get("sleep_words") or []) if str(w).strip()]
        is_sleep = bool(sleep_words) and any(w in text for w in sleep_words)

        state = self._state(group_id)
        now = time.time()
        duration = float(self.config.get("active_minutes") or 5) * 60

        # ---------- 休眠逻辑（优先于唤醒：活跃期内检测到休眠词立即休眠） ----------
        if is_sleep and state["active_until"] > now:
            self.group_states.pop(group_id, None)
            logger.info(f"[唤醒陪聊] 群 {group_id} 检测到休眠词，已休眠（发送者: {name}）")
            return

        # ---------- 唤醒逻辑 ----------
        if is_wake:
            first_wake = state["active_until"] <= now
            # 进入活跃（或刷新活跃截止时间），并重置本轮回复计数
            state["active_until"] = now + duration
            state["replies"] = 0
            logger.info(
                f"[唤醒陪聊] 群 {group_id} {'被唤醒' if first_wake else '唤醒时间已刷新'}，"
                f"活跃 {self.config.get('active_minutes')} 分钟（唤醒者: {name}）"
            )
            # 回复唤醒的群友
            if self.config.get("wake_reply_enabled", True):
                reply = None
                if self.config.get("use_llm", True):
                    reply = await self._llm_reply(group_id, name, text, scene="wake")
                if not reply:
                    greet_tpl = str(self.config.get("wake_greeting") or "").strip()
                    if greet_tpl:
                        reply = greet_tpl.replace("{name}", name)
                    else:
                        reply = random.choice(self._cfg_replies("wake_greetings", DEFAULT_GREETINGS)).replace(
                            "{name}", name
                        )
                if reply:
                    await self._send_reply(event, reply, at_sender=bool(self.config.get("at_sender", True)))
                    state["replies"] += 1
                    state["last_reply"] = time.time()
            return

        # ---------- 活跃期内的自主插话 ----------
        if state["active_until"] <= now:
            # 已过活跃时间，进入休眠，清理状态
            self.group_states.pop(group_id, None)
            return

        max_replies = int(self.config.get("max_replies") or 10)
        if state["replies"] >= max_replies:
            # 本轮回复条数已用完：保持休眠沉默直到倒计时结束
            return

        cooldown = float(self.config.get("reply_cooldown_seconds") or 15)
        if now - state.get("last_reply", 0.0) < cooldown:
            return

        chance = float(self.config.get("random_reply_chance") or 0.15)
        if random.random() >= chance:
            return

        # 生成回复：优先 LLM，失败或关闭时使用随机语料
        reply = None
        if self.config.get("use_llm", True):
            reply = await self._llm_reply(group_id, name, text, scene="chat")
        if not reply:
            reply = random.choice(self._cfg_replies("random_replies", DEFAULT_REPLIES))

        if reply:
            await self._send_reply(event, reply, at_sender=bool(self.config.get("at_sender", True)))
            state["replies"] += 1
            state["last_reply"] = time.time()
            logger.info(f"[唤醒陪聊] 群 {group_id} 自主插话（本轮第 {state['replies']}/{max_replies} 条）")

    # ------------------------------------------------------------------ #
    # 管理指令（仅管理员）
    # ------------------------------------------------------------------ #

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("陪聊状态", alias={"chatter_status"})
    async def cmd_status(self, event: AstrMessageEvent):
        """查看当前群的陪聊活跃状态"""
        group_id = str(event.message_obj.group_id or "")
        state = self.group_states.get(group_id)
        if not state or state["active_until"] <= time.time():
            yield event.plain_result("💤 当前处于休眠状态，发送唤醒词即可唤醒～")
        else:
            remain = int(state["active_until"] - time.time())
            yield event.plain_result(
                f"🔥 活跃中！剩余 {remain} 秒，本轮回复 {state['replies']}"
                f"/{self.config.get('max_replies')} 条"
            )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("唤醒陪聊", alias={"chatter_wake"})
    async def cmd_wake(self, event: AstrMessageEvent):
        """手动唤醒当前群的陪聊（不发送欢迎语）"""
        group_id = str(event.message_obj.group_id or "")
        if not group_id:
            yield event.plain_result("该指令仅在群聊中可用")
            return
        duration = float(self.config.get("active_minutes") or 5) * 60
        state = self._state(group_id)
        state["active_until"] = time.time() + duration
        state["replies"] = 0
        yield event.plain_result(f"✅ 已手动唤醒，活跃 {self.config.get('active_minutes')} 分钟")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("陪聊休眠", alias={"chatter_sleep"})
    async def cmd_sleep(self, event: AstrMessageEvent):
        """手动让当前群的陪聊立即休眠"""
        group_id = str(event.message_obj.group_id or "")
        self.group_states.pop(group_id, None)
        yield event.plain_result("💤 已休眠")

    async def terminate(self):
        """插件被卸载/停用时清理内存状态"""
        self.group_states.clear()
        self.chat_history.clear()
