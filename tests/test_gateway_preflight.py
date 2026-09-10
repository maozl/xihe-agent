"""Gateway startup preflight: platform credential gate before adapter start.

Exit code 2 (vs 1 for the model api_key gate) so deploy scripts can tell
"model not configured" from "platform credentials missing".
"""
from gateway.bot import PLATFORM_REQUIRED_FIELDS, platform_config_missing_message


def test_required_fields_mirror_adapter_checks():
    assert set(PLATFORM_REQUIRED_FIELDS["wecom"]) == {"bot_id", "secret"}
    assert set(PLATFORM_REQUIRED_FIELDS["feishu"]) == {"app_id", "app_secret"}


def test_missing_fields_message_names_fields_and_snippet():
    msg = platform_config_missing_message("wecom", ["bot_id", "secret"])
    for needle in ("platforms.wecom", "bot_id", "secret", "config.example.yaml"):
        assert needle in msg


def test_message_single_field():
    msg = platform_config_missing_message("feishu", ["app_secret"])
    assert "app_secret" in msg and "platforms.feishu" in msg
