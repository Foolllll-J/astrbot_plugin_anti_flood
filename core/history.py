from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


HISTORY_PAGE_SIZE = 100
MAX_HISTORY_SCAN = 1000
MAX_MESSAGE_AGE_SECONDS = 7 * 24 * 60 * 60
HISTORY_RETRY_COUNT = 3


@dataclass(frozen=True)
class HistoryMessage:
    message_id: str
    sender_id: str
    timestamp: int
    raw: dict[str, Any]
    position: int


def _action_data(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    data = result.get("data")
    return data if isinstance(data, (dict, list)) else result


def _message_id(raw: dict[str, Any]) -> str | None:
    for key in ("message_id", "msg_id", "real_id", "id"):
        value = raw.get(key)
        if value is not None and str(value):
            return str(value)
    return None


def _sender_id(raw: dict[str, Any]) -> str:
    sender = raw.get("sender")
    if isinstance(sender, dict):
        value = sender.get("user_id")
        if value is not None:
            return str(value)
    for key in ("user_id", "sender_id"):
        value = raw.get(key)
        if value is not None:
            return str(value)
    return ""


def _timestamp(raw: dict[str, Any]) -> int:
    for key in ("time", "timestamp", "created_at"):
        value = raw.get(key)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _cursor(raw: dict[str, Any]) -> str | None:
    for key in ("message_seq", "seq", "real_id", "message_id", "msg_id"):
        value = raw.get(key)
        if value is not None and str(value):
            return str(value)
    return None


def _history_items(result: Any) -> list[dict[str, Any]]:
    data = _action_data(result)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    messages = data.get("messages")
    if not isinstance(messages, list):
        return []
    return [item for item in messages if isinstance(item, dict)]


def _order_page(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(items) < 2:
        return items
    first = _timestamp(items[0])
    last = _timestamp(items[-1])
    if first and last and first > last:
        return list(reversed(items))
    return items


def message_timestamp(raw: dict[str, Any]) -> int:
    return _timestamp(raw)


def message_group_id(raw: dict[str, Any]) -> str:
    group_id = raw.get("group_id")
    if group_id is not None:
        return str(group_id)
    group = raw.get("group")
    if isinstance(group, dict) and group.get("group_id") is not None:
        return str(group["group_id"])
    return ""


class HistoryResolver:
    """读取近期群消息，并把不同 OneBot 返回顺序统一为时间正序。"""

    async def load_until(
        self,
        event: AstrMessageEvent,
        stop_ids: set[str],
        candidate_predicate: Callable[[HistoryMessage], bool] | None = None,
        required_candidates: int = 0,
    ) -> list[HistoryMessage]:
        """最多扫描固定范围，直到找到端点且满足候选数量，或触及安全边界。"""
        group_id = str(event.get_group_id())
        cutoff = int(time.time()) - MAX_MESSAGE_AGE_SECONDS
        collected: list[HistoryMessage] = []
        seen_ids: set[str] = set()
        cursor: str | None = None
        position = 0

        while len(collected) < MAX_HISTORY_SCAN:
            page = await self._load_page(event, group_id, cursor)
            if not page:
                break

            page = _order_page(page)
            page_added = 0
            for raw in page:
                message_id = _message_id(raw)
                if message_id is None or message_id in seen_ids:
                    continue
                if message_group_id(raw) not in {"", group_id}:
                    continue
                seen_ids.add(message_id)
                collected.append(
                    HistoryMessage(
                        message_id=message_id,
                        sender_id=_sender_id(raw),
                        timestamp=_timestamp(raw),
                        raw=raw,
                        position=position,
                    )
                )
                position += 1
                page_added += 1

            ordered_collected = sorted(
                collected,
                key=lambda item: (
                    item.timestamp if item.timestamp else float("inf"),
                    item.position,
                ),
            )
            found_stop = stop_ids.issubset(seen_ids)
            scoped_messages = ordered_collected
            if found_stop:
                stop_indexes = [
                    index
                    for index, item in enumerate(ordered_collected)
                    if item.message_id in stop_ids
                ]
                if stop_indexes:
                    # 数量模式只统计锚点之前的消息，不能把锚点之后的消息算进去。
                    scoped_messages = ordered_collected[: max(stop_indexes) + 1]
            candidate_count = sum(
                1
                for item in scoped_messages
                if candidate_predicate is None or candidate_predicate(item)
            )
            if found_stop and candidate_count >= required_candidates:
                break

            known_times = [item.timestamp for item in collected if item.timestamp]
            if known_times and min(known_times) < cutoff:
                break
            if page_added == 0:
                break

            oldest = min(
                (item for item in collected if item.timestamp),
                key=lambda item: item.timestamp,
                default=None,
            )
            next_cursor = _cursor(oldest.raw) if oldest is not None else None
            if next_cursor is None or next_cursor == cursor:
                break
            cursor = next_cursor

        collected.sort(
            key=lambda item: (
                item.timestamp if item.timestamp else float("inf"),
                item.position,
            )
        )
        return collected

    async def _load_page(
        self, event: AstrMessageEvent, group_id: str, cursor: str | None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "group_id": str(group_id),
            "count": HISTORY_PAGE_SIZE,
            "reverseOrder": True,
        }
        if cursor is not None:
            params["message_seq"] = cursor

        for attempt in range(1, HISTORY_RETRY_COUNT + 1):
            try:
                api = getattr(event.bot, "api", None)
                call_action = getattr(api, "call_action", None)
                if callable(call_action):
                    result = await call_action("get_group_msg_history", **params)
                else:
                    result = await event.bot.call_action(
                        "get_group_msg_history", **params
                    )
                return _history_items(result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt == HISTORY_RETRY_COUNT:
                    logger.warning(
                        "读取群消息失败 本次操作可能不完整 原因：%s",
                        exc,
                    )
                else:
                    await asyncio.sleep(attempt)
        return []
