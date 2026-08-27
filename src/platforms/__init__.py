"""Platform adapter registry — discovers and instantiates platform adapters.

To add a new platform:
  1. Create platforms/<name>.py with a class inheriting BasePlatformAdapter
  2. Add it to the _PLATFORM_CLASSES dict below
  3. Add config to config.yaml platforms.<name> section
"""

import logging
from typing import Optional

from platforms.base import BasePlatformAdapter, MessageCallback

logger = logging.getLogger(__name__)

_PLATFORM_CLASSES: dict[str, type[BasePlatformAdapter]] = {}


def _register_defaults():
    """Register built-in platform adapters."""
    try:
        from platforms.wecom import WeComAdapter
        _PLATFORM_CLASSES["wecom"] = WeComAdapter
    except ImportError:
        pass

    # Future platforms — just uncomment + install deps:
    # try:
    #     from platforms.telegram import TelegramAdapter
    #     _PLATFORM_CLASSES["telegram"] = TelegramAdapter
    # except ImportError:
    #     pass

    try:
        from platforms.feishu import FeishuAdapter
        _PLATFORM_CLASSES["feishu"] = FeishuAdapter
    except ImportError:
        pass
    # try:
    #     from platforms.discord import DiscordAdapter
    #     _PLATFORM_CLASSES["discord"] = DiscordAdapter
    # except ImportError:
    #     pass


_register_defaults()


def list_platforms() -> list[str]:
    """Return names of available platform adapters."""
    return list(_PLATFORM_CLASSES.keys())


def create_adapter(
    platform_name: str,
    config: dict,
    on_message: MessageCallback = None,
) -> Optional[BasePlatformAdapter]:
    """Instantiate a platform adapter by name.

    Args:
        platform_name: e.g. "wecom", "telegram"
        config: Platform-specific config dict
        on_message: Callback for inbound messages

    Returns:
        Adapter instance, or None if platform not found
    """
    cls = _PLATFORM_CLASSES.get(platform_name)
    if not cls:
        logger.error("Unknown platform: %s (available: %s)",
                      platform_name, ", ".join(_PLATFORM_CLASSES.keys()))
        return None
    return cls(config=config, on_message=on_message)
