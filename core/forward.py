from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


FORWARD_BATCH_SIZE = 100


def _api_message_id(message_id: str) -> int | str:
    try:
        return int(message_id)
    except (TypeError, ValueError):
        return message_id


def action_failed(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    status = result.get("status")
    if status not in {None, "ok", "async"}:
        return True
    retcode = result.get("retcode")
    return retcode is not None and str(retcode) != "0"


def action_message_id(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    containers: list[dict[str, Any]] = [result]
    data = result.get("data")
    if isinstance(data, dict):
        containers.append(data)
    for container in containers:
        for key in ("message_id", "real_id", "id"):
            value = container.get(key)
            if value is not None and str(value):
                return str(value)
    return None


@dataclass(frozen=True)
class FoldResult:
    requested: int
    forwarded: int
    forward_failed: int
    recalled: int
    recall_failed: int


class FoldExecutor:
    def __init__(self, recall_original: bool):
        self.recall_original = recall_original
        self._group_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def fold_message_ids(
        self,
        event: AstrMessageEvent,
        message_ids: list[str],
        *,
        recall_original: bool | None = None,
        operation: str = "manual",
    ) -> FoldResult:
        unique_ids = list(dict.fromkeys(str(message_id) for message_id in message_ids))
        if not unique_ids:
            return FoldResult(0, 0, 0, 0, 0)

        should_recall = (
            self.recall_original if recall_original is None else recall_original
        )
        group_id = str(event.get_group_id())
        forwarded = 0
        forward_failed = 0
        recalled = 0
        recall_failed = 0

        async with self._group_locks[group_id]:
            for batch_start in range(0, len(unique_ids), FORWARD_BATCH_SIZE):
                batch = unique_ids[batch_start : batch_start + FORWARD_BATCH_SIZE]
                if not await self.send_forward(event, batch):
                    forward_failed += len(batch)
                    continue

                forwarded += len(batch)
                if not should_recall:
                    continue

                for message_id in batch:
                    try:
                        result = await event.bot.delete_msg(
                            message_id=_api_message_id(message_id),
                            self_id=str(event.get_self_id()),
                        )
                        if action_failed(result):
                            raise RuntimeError(f"delete_msg returned {result!r}")
                        recalled += 1
                    except Exception as exc:
                        recall_failed += 1
                        logger.debug(
                            "原消息撤回失败 消息编号=%s 原因：%s",
                            message_id,
                            exc,
                        )

        if forward_failed or recall_failed:
            logger.warning(
                "折叠完成但有消息处理失败 请求%s条 合并转发成功%s条 合并转发失败%s条 "
                "撤回成功%s条 撤回失败%s条",
                len(unique_ids),
                forwarded,
                forward_failed,
                recalled,
                recall_failed,
            )
        elif should_recall:
            logger.info(
                "折叠完成 已合并转发并撤回%s条消息",
                len(unique_ids),
            )
        else:
            logger.info(
                "折叠完成 已合并转发%s条消息 未撤回原消息",
                len(unique_ids),
            )
        return FoldResult(
            requested=len(unique_ids),
            forwarded=forwarded,
            forward_failed=forward_failed,
            recalled=recalled,
            recall_failed=recall_failed,
        )

    async def send_forward(
        self, event: AstrMessageEvent, message_ids: list[str]
    ) -> bool:
        node_payload = [
            {"type": "node", "data": {"id": str(message_id)}}
            for message_id in message_ids
        ]
        try:
            result = await event.bot.call_action(
                "send_group_forward_msg",
                group_id=str(event.get_group_id()),
                messages=node_payload,
                self_id=str(event.get_self_id()),
            )
            if action_failed(result):
                logger.error(
                    "合并转发失败 本批%s条消息未处理 返回信息：%s",
                    len(message_ids),
                    result,
                )
                return False
            return True
        except Exception as exc:
            logger.error(
                "合并转发失败 本批%s条消息未处理 原因：%s",
                len(message_ids),
                exc,
            )
            return False
