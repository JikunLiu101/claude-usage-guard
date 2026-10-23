from __future__ import annotations

import logging
import pathlib
import tempfile
import time
import traceback

from langbot_plugin.api.definition.plugin import BasePlugin

logger = logging.getLogger('claude_usage_guard.main')


class ClaudeUsageGuard(BasePlugin):
    """插件入口。用量记录与闸门在 meter/ 里（包装脚本也用它），配置同步和日志在 components/event_listener/。"""

    async def initialize(self) -> None:
        # 事件监听组件要等第一条消息才初始化；插件一启动就同步一次，COMMAND.txt 和日志里的命令立刻可用。
        # 这里出任何错都不能让插件启动失败：闸门本身在包装脚本里，插件起不来只会少了配置同步和日志。
        try:
            from components.event_listener.guard import Guard

            Guard.shared().sync(self.get_config() or {})
        except Exception:
            logger.exception('插件启动时同步配置失败（插件照常运行，下一条消息时会再试）')
            try:
                path = pathlib.Path(tempfile.gettempdir()) / 'claude-usage-guard-startup-error.txt'
                path.write_text(time.strftime('%Y-%m-%d %H:%M:%S\n') + traceback.format_exc(), encoding='utf-8')
            except Exception:
                pass
