from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core import ManualFoldService


class AntiFlood(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self._service = ManualFoldService(config)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("折叠")
    async def fold_command(self, event: AstrMessageEvent):
        """按数量折叠群聊消息"""
        await self._service.handle_count_command(event)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("折叠端点")
    async def fold_endpoint_command(self, event: AstrMessageEvent):
        """按两条端点折叠群聊消息"""
        await self._service.handle_endpoint_command(event)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_message(self, event: AstrMessageEvent):
        await self._service.handle_long_message(event)
