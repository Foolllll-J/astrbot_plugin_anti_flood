from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Image
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


# 角色等级，数值越大权限越高。供撤回/禁言等操作的通用权限判定复用。
_ROLE_RANK = {"owner": 3, "admin": 2, "member": 1, "stranger": 0}


def _role_rank(role: str | None) -> int:
    """将群成员角色映射为等级数值，未知角色按最低等级处理。"""
    if role is None:
        return _ROLE_RANK["stranger"]
    return _ROLE_RANK.get(role, _ROLE_RANK["stranger"])


def _can_operate(bot_role: str | None, target_role: str | None) -> tuple[bool, str]:
    """判断 bot 能否对目标成员执行撤回/禁言等操作。"""
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
    """从原始消息直接提取发送者角色，避免调用 API。"""
    raw = getattr(event.message_obj, "raw_message", None)
    if not isinstance(raw, dict):
        return None
    sender = raw.get("sender")
    if not isinstance(sender, dict):
        return None
    return sender.get("role")


class AntiFlood(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 群号 -> bot 在该群的角色。命中即跳过 API，无缓存再拉取。
        self._bot_role_cache: dict[str, str] = {}

    async def _get_bot_role(self, event: AstrMessageEvent) -> str | None:
        """获取 bot 在群内的角色，优先使用缓存。"""
        group_id = str(event.get_group_id())
        if group_id in self._bot_role_cache:
            return self._bot_role_cache[group_id]

        try:
            bot_info = await event.bot.get_group_member_info(
                group_id=int(group_id), user_id=int(event.get_self_id())
            )
            bot_role = bot_info.get("role", "member")
        except Exception as e:
            logger.info("[anti_flood] 获取机器人群信息失败: %s", e)
            return None

        self._bot_role_cache[group_id] = bot_role
        return bot_role

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_message(self, event: AstrMessageEvent):
        """处理所有群消息，检测长度并合并转发后撤回"""
        cfg = self.config["forward_longmsg"]

        group_id = str(event.get_group_id())
        group_whitelist = [str(x) for x in (cfg.get("group_whitelist") or [])]
        if group_whitelist and group_id not in group_whitelist:
            return

        # 跳过唤醒/提及 Bot 的消息，不做转发与撤回
        if event.is_at_or_wake_command:
            return

        sender_id = str(event.get_sender_id())
        user_whitelist = [str(x) for x in (cfg.get("user_whitelist") or [])]
        if sender_id in user_whitelist:
            return

        message_str = event.message_str
        max_img = cfg.get("max_image_count") or 0
        max_length = cfg.get("max_length") or 0
        img_count = sum(1 for c in event.get_messages() if isinstance(c, Image))
        # max_length/max_image_count 为 0 时表示不检查该维度
        if (max_length == 0 or len(message_str) < max_length) and (
            max_img == 0 or img_count < max_img
        ):
            return

        assert isinstance(event, AiocqhttpMessageEvent)
        message_id = int(event.message_obj.message_id)

        bot_role = await self._get_bot_role(event)
        target_role = _extract_sender_role(event)
        if target_role is None:
            try:
                target_info = await event.bot.get_group_member_info(
                    group_id=int(group_id), user_id=int(sender_id)
                )
                target_role = target_info.get("role", "member")
            except Exception as e:
                logger.info("[anti_flood] 获取目标用户群信息失败: %s", e)
                return

        can, reason = _can_operate(bot_role, target_role)
        if not can:
            logger.info(
                "[anti_flood] 无权限操作消息，跳过处理。sender=%s group=%s reason=%s",
                sender_id,
                group_id,
                reason,
            )
            return

        sent = await self._send_forward_by_msg_id(event, message_id)
        if not sent:
            return

        logger.debug(
            "[anti_flood] 已成功转发群员长消息。sender=%s message_id=%s group=%s length=%s max=%s",
            sender_id,
            message_id,
            group_id,
            len(event.message_str),
            max_length,
        )

        try:
            await event.bot.delete_msg(message_id=message_id)
        except Exception as exc:
            logger.info(f"消息撤回失败: {exc}，可能已被手动撤回，取消转发。")

    async def _send_forward_by_msg_id(
        self, event: AstrMessageEvent, message_id: int
    ) -> bool:
        """基于 message_id 发送群合并转发"""
        node_payload = [{"type": "node", "data": {"id": str(message_id)}}]
        try:
            await event.bot.call_action(
                "send_group_forward_msg",
                group_id=str(event.get_group_id()),
                messages=node_payload,
            )
            return True
        except Exception as exc:
            logger.error(
                "[anti_flood] 转发失败，保留原消息。message_id=%s error=%s",
                message_id,
                exc,
                exc_info=True,
            )
            return False
