from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Image, Reply

from .forward import FoldExecutor, action_message_id
from .history import (
    MAX_MESSAGE_AGE_SECONDS,
    HistoryMessage,
    HistoryResolver,
)


DEFAULT_FOLD_COUNT = 10
RANGE_STATE_TTL_SECONDS = 5 * 60
_ROLE_RANK = {"owner": 3, "admin": 2, "member": 1, "stranger": 0}


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return default


@dataclass(frozen=True)
class ManualFoldOptions:
    default_count: int = DEFAULT_FOLD_COUNT
    recall_original: bool = False
    fold_bot_messages: bool = False

    @classmethod
    def from_config(cls, config: AstrBotConfig) -> "ManualFoldOptions":
        raw = config.get("manual_fold", {})
        if not isinstance(raw, dict):
            raw = {}
        try:
            default_count = int(raw.get("default_count", DEFAULT_FOLD_COUNT))
        except (TypeError, ValueError):
            default_count = DEFAULT_FOLD_COUNT
        return cls(
            default_count=max(1, min(default_count, 1000)),
            recall_original=_as_bool(raw.get("recall_original", False), False),
            fold_bot_messages=_as_bool(
                raw.get("fold_bot_messages", False), False
            ),
        )


@dataclass
class PendingFoldRange:
    first_message_id: str
    first_command_id: str
    prompt_message_id: str | None
    target_ids: tuple[str, ...]
    created_at: float


def _role_rank(role: str | None) -> int:
    return _ROLE_RANK.get(role or "stranger", _ROLE_RANK["stranger"])


def _can_operate(bot_role: str | None, target_role: str | None) -> tuple[bool, str]:
    """现有自动长消息处理使用的 QQ 群权限判定。"""
    if _role_rank(bot_role) < _ROLE_RANK["admin"]:
        return False, "机器人不是群管理员，无法操作群员消息"
    if _role_rank(target_role) >= _ROLE_RANK["owner"]:
        return False, "目标用户是群主，无法操作"
    if (
        _role_rank(target_role) >= _ROLE_RANK["admin"]
        and _role_rank(bot_role) < _ROLE_RANK["owner"]
    ):
        return False, "机器人权限不足，无法操作管理员消息"
    return True, ""


def _extract_sender_role(event: AstrMessageEvent) -> str | None:
    raw = getattr(event.message_obj, "raw_message", None)
    if not isinstance(raw, dict):
        return None
    sender = raw.get("sender")
    return sender.get("role") if isinstance(sender, dict) else None


class ManualFoldService:
    def __init__(self, config: AstrBotConfig):
        self.config = config
        self.options = ManualFoldOptions.from_config(config)
        self.history = HistoryResolver()
        self.forward = FoldExecutor(self.options.recall_original)
        self._bot_role_cache: dict[str, str] = {}
        self._pending_ranges: dict[tuple[str, str], PendingFoldRange] = {}

    async def handle_count_command(self, event: AstrMessageEvent) -> None:
        event.stop_event()
        if not await self._require_bot_admin(event):
            return

        command_id = self._current_message_id(event)
        reply = self._extract_reply(event)
        reply_id = (
            str(reply.id).strip()
            if reply is not None and getattr(reply, "id", None) is not None
            else command_id
        )
        if reply_id is None:
            await self._send_error(event, "找不到折叠锚点")
            return
        if not await self._validate_anchor(event, reply_id, reply):
            return

        tokens = self._command_tokens(event, "折叠")
        count = self.options.default_count
        number_tokens = [token for token in tokens if token.isdigit()]
        if number_tokens:
            count = int(number_tokens[0])
        if count <= 0:
            await self._send_error(event, "折叠数量必须大于0")
            return

        at_ids = self._extract_at_ids(event)
        target_ids = set(at_ids) or None
        excluded_ids: set[str] = {command_id} if command_id is not None else set()
        history = await self.history.load_until(
            event,
            stop_ids={reply_id},
            candidate_predicate=lambda message: self._is_candidate(
                event, message, target_ids, excluded_ids
            ),
            required_candidates=count,
        )
        anchor_index = next(
            (
                index
                for index, message in enumerate(history)
                if message.message_id == reply_id
            ),
            None,
        )
        if anchor_index is None:
            await self._send_error(
                event,
                "找不到引用消息" if reply is not None else "找不到折叠锚点",
            )
            return

        candidates = [
            message
            for message in history[: anchor_index + 1]
            if self._is_candidate(event, message, target_ids, excluded_ids)
        ]
        selected = candidates[-count:]
        if not selected:
            await self._send_error(event, "没有找到可折叠消息")
            return
        await self.forward.fold_message_ids(
            event,
            [message.message_id for message in selected],
            operation="manual_count",
        )

    async def handle_endpoint_command(self, event: AstrMessageEvent) -> None:
        event.stop_event()
        if not await self._require_bot_admin(event):
            return
        if self._command_tokens(event, "折叠端点"):
            await self._send_error(event, "折叠端点不需要参数")
            return

        command_id = self._current_message_id(event)
        reply = self._extract_reply(event)
        reply_id = (
            str(reply.id).strip()
            if reply is not None and getattr(reply, "id", None) is not None
            else command_id
        )
        if reply_id is None:
            await self._send_error(event, "找不到折叠锚点")
            return
        if not await self._validate_anchor(event, reply_id, reply):
            return

        at_ids = self._extract_at_ids(event)
        target_ids = tuple(at_ids)
        key = self._range_key(event)
        state = self._get_pending_range(event)
        if state is None:
            prompt_id = await self._send_range_prompt(event)
            self._pending_ranges[key] = PendingFoldRange(
                first_message_id=reply_id,
                first_command_id=command_id or reply_id,
                prompt_message_id=prompt_id,
                target_ids=target_ids,
                created_at=time.monotonic(),
            )
            return

        target_ids = set(state.target_ids).union(at_ids) or None
        excluded_ids = {state.first_command_id}
        if command_id is not None:
            excluded_ids.add(command_id)
        if state.prompt_message_id is not None:
            excluded_ids.add(state.prompt_message_id)

        history = await self.history.load_until(
            event,
            stop_ids={state.first_message_id, reply_id},
        )
        indexes = {message.message_id: index for index, message in enumerate(history)}
        if state.first_message_id not in indexes or reply_id not in indexes:
            await self._send_error(event, "找不到折叠端点")
            return

        first_index = indexes[state.first_message_id]
        second_index = indexes[reply_id]
        low, high = sorted((first_index, second_index))
        # 两个引用端点都属于折叠范围；真正的操作控制消息单独排除。
        selected = [
            message
            for message in history[low : high + 1]
            if self._is_candidate(event, message, target_ids, excluded_ids)
        ]
        if not selected:
            self._pending_ranges.pop(self._range_key(event), None)
            await self._send_error(event, "端点之间没有可折叠消息")
            return

        self._pending_ranges.pop(self._range_key(event), None)
        await self.forward.fold_message_ids(
            event,
            [message.message_id for message in selected],
            operation="manual_endpoint",
        )

    def _get_pending_range(self, event: AstrMessageEvent) -> PendingFoldRange | None:
        key = self._range_key(event)
        state = self._pending_ranges.get(key)
        if state is None:
            return None
        if time.monotonic() - state.created_at > RANGE_STATE_TTL_SECONDS:
            self._pending_ranges.pop(key, None)
            return None
        return state

    def _range_key(self, event: AstrMessageEvent) -> tuple[str, str]:
        return str(event.get_group_id()), str(event.get_sender_id())

    def _current_message_id(self, event: AstrMessageEvent) -> str | None:
        value = getattr(event.message_obj, "message_id", None)
        if value is None or not str(value).strip():
            return None
        return str(value).strip()

    def _command_tokens(self, event: AstrMessageEvent, command: str) -> list[str]:
        text = (event.message_str or "").strip()
        tokens = text.split()
        if tokens and tokens[0].lstrip("/") == command:
            tokens = tokens[1:]
        return [
            token
            for token in tokens
            if re.fullmatch(r"@.*\(\d+\)", token) is None
        ]

    def _extract_at_ids(self, event: AstrMessageEvent) -> list[str]:
        leading_self_at_count = self._leading_self_at_count(event)
        ids: list[str] = []
        raw = getattr(event.message_obj, "raw_message", None)
        segments = raw.get("message") if isinstance(raw, dict) else None
        if isinstance(segments, list):
            for segment in segments:
                if not isinstance(segment, dict) or segment.get("type") != "at":
                    continue
                data = segment.get("data")
                if not isinstance(data, dict) or data.get("qq") is None:
                    continue
                user_id = str(data["qq"])
                if user_id == "all":
                    continue
                if user_id == str(event.get_self_id()) and leading_self_at_count:
                    leading_self_at_count -= 1
                    continue
                if user_id not in ids:
                    ids.append(user_id)
            return ids

        for component in event.get_messages():
            if isinstance(component, At) and str(component.qq) != "all":
                user_id = str(component.qq)
                if user_id == str(event.get_self_id()) and leading_self_at_count:
                    leading_self_at_count -= 1
                    continue
                if user_id not in ids:
                    ids.append(user_id)
        return ids

    def _leading_self_at_count(self, event: AstrMessageEvent) -> int:
        raw = getattr(event.message_obj, "raw_message", None)
        segments = raw.get("message") if isinstance(raw, dict) else None
        if not isinstance(segments, list):
            return 0
        first_text_index = next(
            (
                index
                for index, segment in enumerate(segments)
                if isinstance(segment, dict) and segment.get("type") == "text"
            ),
            None,
        )
        if first_text_index is None:
            return 0
        self_id = str(event.get_self_id())
        return sum(
            1
            for segment in segments[:first_text_index]
            if isinstance(segment, dict)
            and segment.get("type") == "at"
            and isinstance(segment.get("data"), dict)
            and str(segment["data"].get("qq")) == self_id
        )

    def _extract_reply(self, event: AstrMessageEvent) -> Reply | None:
        # 与其他 QQ 插件保持一致：优先读取事件已经解析好的消息链。
        for component in getattr(event.message_obj, "message", []) or []:
            if isinstance(component, Reply):
                return component
        raw = getattr(event.message_obj, "raw_message", None)
        segments = raw.get("message") if isinstance(raw, dict) else raw
        if isinstance(segments, list):
            for segment in segments:
                if not isinstance(segment, dict) or segment.get("type") != "reply":
                    continue
                data = segment.get("data")
                if not isinstance(data, dict):
                    continue
                reply_id = data.get("id") or data.get("message_id")
                if reply_id is not None:
                    return Reply(id=str(reply_id))
        return None

    async def _require_bot_admin(self, event: AstrMessageEvent) -> bool:
        if event.is_admin():
            return True
        logger.info("手动折叠权限不足")
        return False

    async def _send_error(self, event: AstrMessageEvent, message: str) -> None:
        await event.send(event.plain_result(message))

    async def _validate_anchor(
        self,
        event: AstrMessageEvent,
        message_id: str,
        reply: Reply | None = None,
    ) -> bool:
        # 引用组件中的 ID 就是 QQ 事件提供的引用目标。不要额外调用
        # get_msg：部分协议端可以正常提供引用组件，却无法按该 ID 返回详情。
        # 后续 load_until 会在当前群的历史消息中定位端点，并完成范围及时间限制。
        if reply is not None and reply.time:
            if time.time() - reply.time > MAX_MESSAGE_AGE_SECONDS:
                await self._send_error(event, "引用消息时间过久")
                return False
        return True

    async def _send_range_prompt(self, event: AstrMessageEvent) -> str | None:
        prompt = "请引用另一条消息或直接发送折叠端点"
        try:
            result = await event.bot.call_action(
                "send_group_msg",
                group_id=int(event.get_group_id()),
                message=[{"type": "text", "data": {"text": prompt}}],
                self_id=str(event.get_self_id()),
            )
            prompt_id = action_message_id(result)
            if prompt_id is None:
                logger.warning("端点提示已发送但未取得消息编号")
            return prompt_id
        except Exception as exc:
            logger.error("发送端点提示失败 原因：%s", exc, exc_info=True)
            return None

    def _is_candidate(
        self,
        event: AstrMessageEvent,
        message: HistoryMessage,
        target_ids: set[str] | None,
        excluded_ids: set[str],
    ) -> bool:
        if message.message_id in excluded_ids:
            return False
        if (
            not self.options.fold_bot_messages
            and message.sender_id == str(event.get_self_id())
        ):
            return False
        if target_ids is not None and message.sender_id not in target_ids:
            return False
        return not (
            message.timestamp
            and time.time() - message.timestamp > MAX_MESSAGE_AGE_SECONDS
        )

    async def handle_long_message(self, event: AstrMessageEvent) -> None:
        cfg = self.config["forward_longmsg"]
        group_id = str(event.get_group_id())
        group_whitelist = [str(x) for x in (cfg.get("group_whitelist") or [])]
        if group_whitelist and group_id not in group_whitelist:
            return
        if event.is_at_or_wake_command:
            return

        sender_id = str(event.get_sender_id())
        user_whitelist = [str(x) for x in (cfg.get("user_whitelist") or [])]
        if sender_id in user_whitelist:
            return

        message_str = event.message_str
        max_img = cfg.get("max_image_count") or 0
        max_length = cfg.get("max_length") or 0
        img_count = sum(
            1 for component in event.get_messages() if isinstance(component, Image)
        )
        if (max_length == 0 or len(message_str) < max_length) and (
            max_img == 0 or img_count < max_img
        ):
            return

        message_id = str(event.message_obj.message_id)
        bot_role = await self._get_bot_role(event)
        target_role = _extract_sender_role(event)
        if target_role is None:
            try:
                target_info = await event.bot.get_group_member_info(
                    group_id=int(group_id), user_id=int(sender_id)
                )
                target_role = target_info.get("role", "member")
            except Exception as exc:
                logger.warning("获取目标用户信息失败 自动折叠已跳过 原因：%s", exc)
                return

        can, _ = _can_operate(bot_role, target_role)
        if not can:
            return

        await self.forward.fold_message_ids(
            event,
            [message_id],
            recall_original=True,
            operation="automatic_long_message",
        )

    async def _get_bot_role(self, event: AstrMessageEvent) -> str | None:
        group_id = str(event.get_group_id())
        if group_id in self._bot_role_cache:
            return self._bot_role_cache[group_id]
        try:
            bot_info = await event.bot.get_group_member_info(
                group_id=int(group_id), user_id=int(event.get_self_id())
            )
            bot_role = bot_info.get("role", "member")
        except Exception as exc:
            logger.warning("获取机器人群权限失败 自动折叠已跳过 原因：%s", exc)
            return None
        self._bot_role_cache[group_id] = bot_role
        return bot_role
