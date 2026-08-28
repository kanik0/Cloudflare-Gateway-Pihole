import logging
from datetime import datetime
import os


class IconLevelFormatter(logging.Formatter):
    """
    Log formatter that prefixes each line with a level icon instead of
    relying on ANSI color codes. ANSI colors only look right on terminals
    with a dark background (and don't render at all in places like the
    GitHub Actions log viewer set to light mode, some CI dashboards, or
    when output is piped to a file). Icons are plain Unicode characters,
    so they show up consistently everywhere text does.
    """

    LEVEL_ICON = {
        'DEBUG':    '🐞',
        'INFO':     'ℹ️ ',
        'WARNING':  '⚠️ ',
        'ERROR':    '❌',
        'CRITICAL': '🔥',
    }

    def format(self, record):
        levelname = record.levelname
        icon = self.LEVEL_ICON.get(levelname, '•')

        current_time = datetime.fromtimestamp(record.created).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        original_message = record.getMessage()

        formatted_message = (
            f"{icon} {current_time} | "
            f"{original_message}"
        )

        record.msg = formatted_message
        formatted_record = super().format(record)

        return formatted_record


logging.getLogger().setLevel(logging.INFO)
formatter = IconLevelFormatter()
console = logging.StreamHandler()
console.setFormatter(formatter)
logger = logging.getLogger()
logger.addHandler(console)
