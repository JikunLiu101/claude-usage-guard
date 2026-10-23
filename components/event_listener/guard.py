from __future__ import annotations

import json
import logging
import pathlib
import shutil
import sys
import typing

from langbot_plugin.api.definition.components.common.event_listener import EventListener
from langbot_plugin.api.entities import context, events

from meter import __version__

logger = logging.getLogger('claude-usage-guard')

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG_KEYS = (
    'enabled', 'five-hour-limit-percent', 'seven-day-limit-percent', 'bot-window-hours', 'bot-window-tokens',
    'count-cache-read', 'lock-output', 'error-output',
)


def default_data_dir() -> pathlib.Path:
    return pathlib.Path.home() / '.claude-usage-guard'


def resolve_data_dir(config: dict[str, typing.Any]) -> pathlib.Path:
    raw = str(config.get('data-dir') or '').strip()
    return pathlib.Path(raw) if raw else default_data_dir()


def sync(config: dict[str, typing.Any], *, plugin_root: pathlib.Path = PLUGIN_ROOT, python: str = sys.executable) -> pathlib.Path:
    """把插件配置写成 <数据目录>/config.json，把包装脚本装到 <数据目录>/bin/（版本变了才重装）。返回数据目录。"""
    data_dir = resolve_data_dir(config)
    data_dir.mkdir(parents=True, exist_ok=True)

    wanted = {key: config.get(key) for key in CONFIG_KEYS if config.get(key) is not None}
    path = data_dir / 'config.json'
    text = json.dumps(wanted, ensure_ascii=False, indent=2)
    try:
        current = path.read_text(encoding='utf-8')
    except OSError:
        current = None
    if current != text:
        tmp = path.with_suffix('.tmp')
        tmp.write_text(text, encoding='utf-8')
        tmp.replace(path)  # 原子替换：包装脚本随时可能在读

    bin_dir = data_dir / 'bin'
    marker = bin_dir / 'VERSION'
    if not (marker.exists() and marker.read_text(encoding='utf-8').strip() == __version__):
        bin_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(plugin_root / 'tools' / 'claude_meter.py', bin_dir / 'claude_meter.py')
        target = bin_dir / 'meter'
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(plugin_root / 'meter', target, ignore=shutil.ignore_patterns('__pycache__'))
        marker.write_text(__version__, encoding='utf-8')
        logger.info('包装脚本已安装到 %s（%s）', bin_dir, __version__)

    command = f'{python} -I {bin_dir / "claude_meter.py"}'
    cmd_file = data_dir / 'COMMAND.txt'
    note = ('把 LangBot 流水线里 Claude Code Runner 的「command」设成下面这一行：\n'
            f'{command}\n\n查看当前用量：{command} --status\n')
    try:
        if cmd_file.read_text(encoding='utf-8') != note:
            cmd_file.write_text(note, encoding='utf-8')
    except OSError:
        cmd_file.write_text(note, encoding='utf-8')
    return data_dir


class GuardEventListener(EventListener):
    """不拦任何消息：闸门在包装脚本里。这里只负责在 Claude 被调用之前，把最新的配置同步给包装脚本。"""

    async def initialize(self):
        await super().initialize()
        self._sync()

        async def on_before_claude(ctx: context.EventContext):
            self._sync()

        self.handler(events.PersonNormalMessageReceived)(on_before_claude)
        self.handler(events.GroupNormalMessageReceived)(on_before_claude)

    def _sync(self) -> None:
        try:
            data_dir = sync(self.get_plugin_config() or {})
        except Exception:
            logger.warning('同步配置到包装脚本失败', exc_info=True)
            return
        logger.debug('配置已同步到 %s', data_dir)
