import pytest

from hermix_push.sources import coarse_source, should_notify


@pytest.mark.parametrize("platform,expected", [
    ("ios", "bot"), ("desktop", "bot"), ("tui", "bot"), ("bot_room", "other"), ("webui", "webui"), ("cli", "other"), ("", "other"), (None, "other"),
])
def test_coarse_source(platform, expected):
    assert coarse_source(platform) == expected


def test_native_notifying_platforms_and_automation_sessions_are_skipped():
    assert should_notify("s1", "desktop")
    assert should_notify("s1", "webui")
    assert not should_notify("s1", "")  # unknown platform fails closed
    assert not should_notify("s1", "telegram")
    assert not should_notify("cron_job_1", "desktop")
    assert not should_notify("kanban_task", "desktop")
    assert not should_notify("", "desktop")


def test_cron_runs_named_like_chats_are_skipped_by_their_platform():
    # Newer hosts name a cron run `local-<sha>`; only the agent platform marks it.
    assert not should_notify("local-c24dbee0146d", "cron")
    assert should_notify("local-c24dbee0146d", "desktop")
