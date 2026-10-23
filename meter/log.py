"""插件日志：所有 logger 挂在 claude_usage_guard 下，级别由配置项 log-level 控制。

LangBot 在插件详情页显示插件进程的日志。级别约定：
- DEBUG：每次调用的用量、配置未变化等细节
- INFO：配置更新、包装脚本安装、锁定期间拦下的调用、接近上限、解锁
- WARNING：开始锁定、Claude 出错（额度、拒答、超时……）
- ERROR：需要人处理的问题（数据目录写不进去、找不到 claude 命令、意外异常）
"""

from __future__ import annotations

import logging

ROOT = 'claude_usage_guard'
LEVELS = ('DEBUG', 'INFO', 'WARNING', 'ERROR')


def get_logger(module: str) -> logging.Logger:
    return logging.getLogger(f'{ROOT}.{module.rsplit(".", 1)[-1]}')


def apply_level(level: object) -> str:
    name = str(level or 'INFO').upper()
    if name not in LEVELS:
        name = 'INFO'
    logging.getLogger(ROOT).setLevel(name)
    return name
