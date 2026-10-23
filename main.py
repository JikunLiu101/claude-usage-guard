from __future__ import annotations

import logging

from langbot_plugin.api.definition.plugin import BasePlugin

logger = logging.getLogger('claude-usage-guard')


class ClaudeUsageGuard(BasePlugin):
    """插件入口。用量记录与闸门在 meter/ 里（包装脚本也用它），配置同步在 components/event_listener/。"""

    async def initialize(self) -> None:
        # 事件监听组件要等第一条消息才初始化；插件一启动就同步一次，COMMAND.txt 立刻可用
        from components.event_listener.guard import sync

        try:
            sync(self.get_config() or {})
        except Exception:
            logger.warning('启动时同步配置失败', exc_info=True)
