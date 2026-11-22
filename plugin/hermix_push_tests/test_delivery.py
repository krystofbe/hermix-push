"""``deliver: hermix`` / send_message: targets, the cron wrapper, the relay answers, the agent's note."""
import asyncio
import json
import sys
import types

import pytest

from hermix_push import adapter
from hermix_push.hooks import HermexPush, parse_delivery
from hermix_push.privacy import unseal
from hermix_push.relay import RelaySender

WRAPPED = ("Cronjob Response: Flachwitz\n(job_id: 0123456789ab)\n-------------\n\n**Warum** der Witz?\n\n"
           'To stop or manage this job, send me a new message (e.g. "stop reminder Flachwitz").')


class FakeRelay:
    def __init__(self, *answers):
        self.answers, self.posts = list(answers), []

    def post(self, url, body):
        self.posts.append((url, json.loads(body)))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def make_push(keys, relay, **kw):
    sender = RelaySender(post=relay.post, sleep=lambda _: None)
    return HermexPush(sender=sender, keys_loader=lambda: keys, relay_url=lambda: "https://relay.test",
                      bot_name=lambda: "Hermes", **kw)


def sealed(event, keys):
    return unseal(event["sealed"], preview_key=keys.preview_key, install_key=keys.install_key)


@pytest.mark.parametrize("ref,ok", [("home", True), ("20260101_120000_abc123", True), ("a.b-c_d", True),
                                    ("", False), ("a b", False), ("a/b", False), ("x" * 201, False),
                                    ("home:other", False), ("a:urgent:x", False), (":urgent", False)])
def test_parse_target_accepts_home_and_session_ids_only(ref, ok):
    assert adapter.parse_target(ref) == ((ref, None) if ok else None)


def test_the_urgent_marker_rides_in_the_thread_id():
    assert adapter.parse_target("urgent") == ("home", "urgent")
    assert adapter.parse_target("home:urgent") == ("home", "urgent")
    assert adapter.parse_target("sess-1:urgent") == ("sess-1", "urgent")


def test_only_a_marked_delivery_asks_for_a_time_sensitive_banner(keys, monkeypatch):
    relay = FakeRelay((200, "accepted"))
    monkeypatch.setattr(adapter, "shared_push", lambda: make_push(keys, relay))
    asyncio.run(adapter.standalone_send(None, "home", "plain"))
    asyncio.run(adapter.standalone_send(None, "home", "now", thread_id="urgent"))
    plain, marked = (event for _, event in relay.posts)
    assert "urgent" not in plain and marked["urgent"] is True and marked["kind"] == "delivery"


def test_cron_wrapper_gives_job_name_and_body_without_the_footer():
    assert parse_delivery(WRAPPED) == ("Flachwitz", "**Warum** der Witz?")
    assert parse_delivery("Test") == ("", "Test")


def test_delivery_is_sealed_time_sensitive_ready_and_never_collapses(keys):
    relay = FakeRelay((200, "accepted"))
    push = make_push(keys, relay)
    assert push.deliver_message("home", WRAPPED)[0] and push.deliver_message("home", WRAPPED)[0]
    (url, first), (_, second) = relay.posts
    assert url == "https://relay.test/installs/" + keys.install_key + "/notify"
    assert first["kind"] == "delivery" and first["session_id"] == "home" and first["source"] == "other"
    assert first["collapse_id"] == first["event_id"] != second["event_id"]
    assert sealed(first, keys) == {"title": "Hermes · Scheduled task", "subtitle": "Flachwitz", "body": "Warum der Witz?",
                                   "profile": "", "request_id": "", "bot_name": "Hermes"}
    assert "Witz" not in json.dumps(first)


def test_a_cron_retry_keeps_its_event_id_and_a_plain_send_does_not(keys, monkeypatch):
    execution = types.SimpleNamespace(job_name="Daily", execution_id="exec-1")
    module = types.ModuleType("cron.execution_identity")
    module.current_cron_execution = lambda: execution
    monkeypatch.setitem(sys.modules, "cron", types.ModuleType("cron"))
    monkeypatch.setitem(sys.modules, "cron.execution_identity", module)
    relay = FakeRelay((200, "accepted"))
    push = make_push(keys, relay)
    push.deliver_message("sess-1", "plain body")
    push.deliver_message("sess-1", "plain body")
    (_, a), (_, b) = relay.posts
    assert a["event_id"] == b["event_id"]
    assert sealed(a, keys)["subtitle"] == "Daily"  # unwrapped jobs fall back to the executing job's name
    monkeypatch.setitem(sys.modules, "cron.execution_identity", types.ModuleType("empty"))  # no execution bound
    push.deliver_message("sess-1", "plain body")
    assert relay.posts[-1][1]["event_id"] != a["event_id"]


@pytest.mark.parametrize("status,result,ok,fragment", [
    (200, "accepted", True, ""), (200, "deduplicated", True, ""),
    (200, "ignored", False, "update the relay"), (200, "no_devices", False, "no paired iPhone"),
    (200, "", False, "unexpectedly"), (400, "", False, "rejected or unreachable"),
])
def test_the_result_reports_what_the_relay_answered(keys, status, result, ok, fragment):
    done, detail = make_push(keys, FakeRelay((status, result))).deliver_message("home", "x")
    assert done is ok and (ok or fragment in detail)


def test_a_5xx_is_retried_once_then_reported(keys):
    relay = FakeRelay((503, ""), (200, "accepted"))
    assert make_push(keys, relay).deliver_message("home", "x")[0] and len(relay.posts) == 2
    relay = FakeRelay((503, ""))
    assert not make_push(keys, relay).deliver_message("home", "x")[0] and len(relay.posts) == 2


def test_no_relay_or_no_keys_is_an_honest_error(keys):
    push = HermexPush(sender=RelaySender(post=FakeRelay((200, "accepted")).post), keys_loader=lambda: keys, relay_url=lambda: "")
    assert push.deliver_message("home", "x") == (False, "hermix: HERMIX_PUSH_RELAY_URL not set or keys unavailable")


def test_standalone_send_returns_the_sender_contract(keys, monkeypatch):
    relay = FakeRelay((200, "accepted"))
    monkeypatch.setattr(adapter, "shared_push", lambda: make_push(keys, relay))
    result = asyncio.run(adapter.standalone_send(None, "home", "hi"))
    assert result["success"] is True and len(result["message_id"]) == 32
    monkeypatch.setattr(adapter, "shared_push", lambda: make_push(keys, FakeRelay((200, "no_devices"))))
    assert asyncio.run(adapter.standalone_send(None, "home", "hi")) == {"error": "hermix: no paired iPhone"}


def test_the_agent_learns_the_target_on_the_first_turn_or_when_asked_for_a_push(keys):
    push = make_push(keys, FakeRelay((200, "accepted")), schedule=lambda d, f: None)
    first = push.pre_llm_call(session_id="s1", platform="ios", user_message="hallo", is_first_turn=True)
    assert "deliver='hermix:s1'" in first["context"]
    assert push.pre_llm_call(session_id="s1", platform="ios", user_message="hallo", is_first_turn=False) is None
    assert push.pre_llm_call(session_id="s1", platform="ios", user_message="Plane einen Wecker", is_first_turn=False)
    assert push.pre_llm_call(session_id="s1", platform="ios", user_message=[{"type": "text", "text": "send a PUSH"}])
    for session, platform in [("s2", "telegram"), ("cron_9", "cron"), ("s3", "")]:
        assert push.pre_llm_call(session_id=session, platform=platform, user_message="push", is_first_turn=True) is None


def test_the_platform_sets_no_home_channel_so_system_notices_never_push(monkeypatch):
    monkeypatch.setenv("HERMIX_PUSH_RELAY_URL", "https://relay.test")
    assert adapter.env_enablement() == {"relay_url": "https://relay.test"}
    monkeypatch.delenv("HERMIX_PUSH_RELAY_URL")
    assert adapter.env_enablement() is None


def test_send_message_target_hermix_home_resolves_and_delivers(keys, monkeypatch):
    chat_id, thread = adapter.parse_target("home")
    assert (chat_id, thread) == ("home", None)
    relay = FakeRelay((200, "accepted"))
    monkeypatch.setattr(adapter, "shared_push", lambda: make_push(keys, relay))
    assert asyncio.run(adapter.standalone_send(None, chat_id, "Test"))["success"] is True
    assert relay.posts[0][1]["session_id"] == "home"


def test_git_push_is_not_a_push_request(keys):
    push = make_push(keys, FakeRelay((200, "accepted")), schedule=lambda d, f: None)
    for text in ["please git push the branch", "git push --force"]:
        assert push.pre_llm_call(session_id="s1", platform="ios", user_message=text) is None, text
    for text in ["Erinnere mich in 5 Minuten", "schick mir eine Push-Nachricht", "send me a push"]:
        assert push.pre_llm_call(session_id="s1", platform="ios", user_message=text), text
