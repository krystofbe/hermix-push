"""Hook callbacks. One :class:`HermexPush` instance owns the per-session state that turns
hermes-agent lifecycle hooks into relay events.

Per turn: ``pre_llm_call`` records the platform and starts progress, ``post_llm_call`` captures
the final reply text, ``on_session_end`` is the single boundary that sends exactly one ``reply``
or ``turn_error`` (or nothing, for an interrupted turn). Approvals come from the
``pre_approval_request`` observer, questions from ``pre_tool_call`` on the ``clarify`` tool.
Every callback swallows its own errors: a push must never break a turn. A session whose
platform was never seen gets nothing (fail closed), so an evicted chat-platform session can
never be pushed to the phone by mistake.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Callable, Optional

from . import LEGACY_RELAY_URL_ENV, RELAY_URL_ENV
from .keys import Keys, hermes_root, load_or_create_keys
from .payload import _as_text, notify_event, preview, progress_event
from .privacy import keyed_id
from .progress import ProgressCoalescer, ProgressSnapshot
from .relay import RelaySender, ResultFn, allowed_relay_url, notify_url
from .sources import coarse_source, should_notify

logger = logging.getLogger("hermix_push")

CLARIFY_TOOL = "clarify"
# The hermes-agent hooks this plugin observes; each is a same-named ``HermexPush`` method.
HOOK_NAMES = (
    "pre_llm_call", "post_llm_call", "on_session_end", "pre_tool_call", "post_tool_call",
    "pre_approval_request", "post_approval_response", "subagent_start",
)
MAX_TRACKED_SESSIONS = 4096
RELAY_URL_CACHE_SECONDS = 5.0
# The English label after the bot's name in each sealed title; a reply is titled by the name
# alone. Hermex builds the same title in the phone's language from ``kind`` and ``bot_name``, so
# this English copy only shows on app builds that predate ``bot_name``.
TITLE_LABELS = {"approval": "Approval needed", "clarify": "Question", "turn_error": "Turn failed"}


def _root_dotenv_value(key: str) -> str:
    """``KEY=VALUE`` from ``<hermes root>/.env`` so profiles inherit the host's relay URL. This
    bypasses profile secret scoping on purpose and must only ever read a non-secret setting."""
    try:
        for line in (hermes_root() / ".env").read_text(encoding="utf-8").splitlines():
            name, sep, value = line.strip().partition("=")
            if sep and name.strip() == key:
                return value.strip().strip("'\"")
    except Exception:
        pass
    return ""


def relay_url_from_env() -> str:
    """``HERMIX_PUSH_RELAY_URL`` (or, as a fallback, the pre-fork ``HERMEX_PUSH_RELAY_URL``) resolved the way hermes-agent resolves any managed credential:
    the active profile scope, then the process environment, then the active home's ``.env``
    (the step hosts such as hermes-webui need, since they never export the Hermes ``.env``).
    A profile without its own value falls back to the root ``.env``. Anything but https (or
    plain http to loopback, for local capture) is refused so the install key never travels in
    the clear."""
    for name in (RELAY_URL_ENV, LEGACY_RELAY_URL_ENV):
        try:
            from hermes_cli.config import get_env_value
            value = get_env_value(name)
        except Exception:
            value = os.environ.get(name, "")
        value = (value or "").strip() or _root_dotenv_value(name)
        if value and not allowed_relay_url(value):
            logger.warning("hermix-push: refusing %s=%r (https required, http only to loopback)", name, value)
            return ""
        if value:
            return value
    return ""


def bot_name_for(profile: str) -> str:
    """The name Hermex's bot roster shows for a Profile: its Desktop title, then its
    ``display_name``, then the slug, with the default Profile as "Hermes". Read from the host's
    ``profile.yaml`` on every banner (hermes-agent caches the read by file signature), so a rename
    shows on the next one. ``hermes_cli.profiles`` is a hermes-agent internal: a host without it,
    or a read that fails, gets the slug."""
    fallback = profile if profile and profile != "default" else "Hermes"
    try:
        from hermes_cli.profiles import get_profile_dir, read_profile_meta
        meta = read_profile_meta(get_profile_dir(profile or "default"))
        names = (str(meta.get(key) or "").strip() for key in ("bot_title", "display_name"))
        return next((name for name in names if name), fallback)
    except Exception:
        return fallback


# What the cron scheduler wraps around a job's reply (``cron.wrap_response``), see
# cron/scheduler_delivery.py; send_message and unwrapped jobs arrive as the plain body.
_CRON_HEADER = re.compile(r"^Cronjob Response:\s*(?P<name>.+?)\s*\n\(job_id:[^)]*\)\s*\n-+\s*\n+(?P<body>.*)$", re.S)
_CRON_FOOTER = re.compile(r"\n+To stop or manage this job, send me a new message.*\Z", re.S)
# A user message that asks for a push or a timer; the agent then gets the delivery note (``pre_llm_call``).
PUSH_INTENT = re.compile(r"(?<!git )(?<!force )\bpush|notif|benachricht|erinner|remind|wecker|alarm", re.I)


def parse_delivery(message: str) -> tuple[str, str]:
    """``(job name, body)`` of a cron delivery; the name is empty for a plain message."""
    match = _CRON_HEADER.match(message or "")
    name, body = (match["name"].strip(), match["body"]) if match else ("", message or "")
    return name, _CRON_FOOTER.sub("", body).strip()


def _current_cron() -> tuple[str, str]:
    """``(job name, execution id)`` while a cron job fires, else empty strings."""
    try:
        from cron.execution_identity import current_cron_execution
        execution = current_cron_execution()
        return (execution.job_name, execution.execution_id) if execution else ("", "")
    except Exception:
        return "", ""


def delivery_note(session_id: str) -> str:
    return (
        f"Hermix push: to notify the user on their iPhone now or later, use cronjob_manage deliver='hermix:{session_id}' "
        "(tap opens this chat) or 'hermix'; send_message target 'hermix'. deliver='origin' from this chat does not push."
    )


class _Recent(OrderedDict):
    """Bounded insertion-ordered map so long-lived gateways never grow without limit."""

    def remember(self, key: str, value: Any) -> None:
        self[key] = value
        self.move_to_end(key)
        while len(self) > MAX_TRACKED_SESSIONS:
            self.popitem(last=False)


def clarify_question(args: Optional[dict[str, Any]]) -> str:
    """The first question of a ``clarify`` call, canonical ``questions: [{question}]`` shape
    first, legacy top-level ``question`` second; a batch notes how many more there are."""
    args = args or {}
    questions = args.get("questions")
    if isinstance(questions, list) and questions:
        first = questions[0]
        text = str((first.get("question") if isinstance(first, dict) else first) or "")
        if text and len(questions) > 1:
            text += f" (+{len(questions) - 1} more)"
        if text:
            return text
    return str(args.get("question") or "") or "The agent has a question for you."


class HermexPush:
    # Marks this plugin's hook callbacks across module copies (see ``scopes``).
    is_hermix_push = True

    def __init__(
        self, *, profile: str = "", sender: Optional[RelaySender] = None,
        keys_loader: Callable[[], Keys] = load_or_create_keys,
        relay_url: Callable[[], str] = relay_url_from_env, now: Callable[[], float] = time.time,
        schedule: Optional[Callable[[float, Callable[[], None]], Any]] = None,
        bot_name: Optional[Callable[[], str]] = None,
    ) -> None:
        self._profile = "" if profile == "default" else profile
        self._bot_name = bot_name or (lambda: bot_name_for(self._profile))
        self._sender = sender or RelaySender()
        self._keys_loader = keys_loader
        self._relay_url = relay_url
        self._relay_url_cache: tuple[float, str] = (float("-inf"), "")
        self._now = now
        self._schedule = schedule or self._timer
        self._lock = threading.RLock()
        self._keys: Optional[Keys] = None
        self._platform_by_session = _Recent()
        self._reply_by_session = _Recent()
        self._subagent_sessions = _Recent()
        self._progress = ProgressCoalescer()
        self._flush_at: Optional[float] = None  # when the armed flush fires; None when unarmed
        self._flush_timer: Optional[threading.Timer] = None
        self._closed = False

    # -- wiring ---------------------------------------------------------------------------

    def hook_callbacks(self) -> dict[str, Callable[..., None]]:
        return {name: getattr(self, name) for name in HOOK_NAMES}

    def close(self) -> None:
        """Plugin unload: stop the flush timer and the relay thread so a reload leaks nothing."""
        with self._lock:
            self._closed = True
            timer, self._flush_timer = self._flush_timer, None
        if timer is not None:
            timer.cancel()
        self._sender.close()

    def _timer(self, delay: float, fn: Callable[[], None]) -> threading.Timer:
        timer = threading.Timer(delay, fn)
        timer.daemon = True
        timer.start()
        return timer

    def _keys_or_none(self) -> Optional[Keys]:
        if self._keys is None:
            try:
                self._keys = self._keys_loader()
            except Exception:
                logger.warning("hermix-push: keys unavailable; nothing will be sent", exc_info=True)
        return self._keys

    def _relay(self) -> str:
        """Relay URL, re-resolved at most every few seconds so the turn path never re-reads a file."""
        with self._lock:
            resolved_at, url = self._relay_url_cache
            if self._now() - resolved_at < RELAY_URL_CACHE_SECONDS:
                return url
        url = self._relay_url()
        with self._lock:
            self._relay_url_cache = (self._now(), url)
        return url

    def _send(self, event: dict[str, Any], on_result: Optional[ResultFn] = None) -> bool:
        url = self._relay()
        keys = self._keys_or_none()
        if not url or keys is None or self._closed:
            logger.debug("hermix-push: relay not configured; dropping a %s event", event.get("kind"))
            return False
        return self._sender.enqueue(notify_url(url, keys.install_key), event, on_result)

    def _session(self, session_id: str) -> tuple[str, str, bool]:
        platform = self._platform_by_session.get(session_id, "")
        return platform, coarse_source(platform), session_id in self._subagent_sessions

    def _notify(self, kind: str, session_id: str, event_ref: str, **content: Any) -> None:
        """Seal one banner. ``content`` is the ``preview`` fields besides the title, which is
        the bot's name and the kind's label; the name is only read when a banner goes out."""
        platform, source, is_subagent = self._session(session_id)
        if not should_notify(session_id, platform):
            return
        keys = self._keys_or_none()
        if keys is None:
            return
        name = self._bot_name() or self._profile or "Hermes"
        label = TITLE_LABELS.get(kind)
        sealed = preview(title=f"{name} · {label}" if label else name, bot_name=name,
                         profile=self._profile, **content)
        self._send(notify_event(
            kind=kind, session_id=session_id, event_ref=event_ref, source=source, is_subagent=is_subagent,
            keys=keys, preview=sealed, now=self._now(),
        ))

    # -- delivery (cron ``deliver: hermix``, send_message) ---------------------------------

    def deliver_message(self, chat_id: str, content: str) -> tuple[bool, str]:
        """Send one scheduled or requested message to every paired phone and wait for the relay's
        answer: ``(True, event_id)`` or ``(False, reason)``. ``chat_id`` is ``home`` (a tap opens
        the app) or a session id (a tap opens that chat). Never logs the content."""
        url, keys = self._relay(), self._keys_or_none()
        if not url or keys is None:
            return False, f"hermix: {RELAY_URL_ENV} not set or keys unavailable"
        job_name, body = parse_delivery(content)
        cron_name, execution_id = _current_cron()
        name = self._bot_name() or self._profile or "Hermes"
        sealed = preview(title=f"{name} \u00b7 Scheduled task", subtitle=job_name or cron_name, body=body,
                         profile=self._profile, bot_name=name)
        # A cron retry repeats the execution id and content, so the relay deduplicates it; a plain
        # send_message is always a new event.
        ref = hashlib.sha256(f"{execution_id}\n{chat_id}\n{content}".encode()).hexdigest()[:32] if execution_id \
            else uuid.uuid4().hex
        event = notify_event(kind="delivery", session_id=chat_id, event_ref=ref, source="other",
                             is_subagent=False, keys=keys, preview=sealed, now=self._now())
        answers: list[str] = []
        if not self._sender.deliver(notify_url(url, keys.install_key), event, answers.append):
            return False, "hermix: relay rejected or unreachable"
        result = answers[0] if answers else ""
        if result in ("accepted", "deduplicated"):
            return True, event["event_id"]
        reasons = {"ignored": "hermix: relay does not know kind 'delivery', update the relay",
                   "no_devices": "hermix: no paired iPhone"}
        return False, reasons.get(result, "hermix: relay answered unexpectedly")

    # -- progress -------------------------------------------------------------------------

    def _emit_progress(self, snapshot: Optional[ProgressSnapshot]) -> None:
        if snapshot is None:
            self._arm_flush()
            return
        platform, source, is_subagent = self._session(snapshot.session_id)
        if not should_notify(snapshot.session_id, platform):
            return
        keys = self._keys_or_none()
        if keys is None:
            return
        session_id = snapshot.session_id
        self._send(progress_event(
            session_id=session_id, source=source, is_subagent=is_subagent, keys=keys,
            status=snapshot.status, tool=snapshot.tool, tool_calls=snapshot.tool_calls,
            started_at=snapshot.started_at, now=self._now(),
        ), on_result=lambda result: self._progress_answered(session_id, result))

    def _progress_answered(self, session_id: str, result: str) -> None:
        """The relay's answer to a delivered progress event (drain thread). ``no_activity`` means
        no phone shows a Live Activity for the session, so its routine updates quiet down; any
        other answer, including an older relay's ``accepted``, keeps the normal cadence."""
        with self._lock:
            if result == "no_activity":
                self._progress.unwatched(session_id, self._now())
            else:
                self._progress.watched(session_id)
        self._arm_flush()

    def _arm_flush(self) -> None:
        """Schedule a flush for when the earliest held update's hold ends, replacing an armed
        flush that would fire later (another session's hold can end first). A stray flush is
        harmless: it sends only what is due and re-arms."""
        with self._lock:
            if self._closed:
                return
            now = self._now()
            delay = self._progress.flush_delay(now)
            if delay is None or (self._flush_at is not None and self._flush_at <= now + delay):
                return
            self._flush_at = now + delay
            replaced, self._flush_timer = self._flush_timer, None
        if replaced is not None:
            replaced.cancel()
        try:
            timer = self._schedule(delay, self.flush_progress)
        except Exception:
            with self._lock:
                self._flush_at = None
            logger.debug("hermix-push: could not schedule a progress flush", exc_info=True)
            return
        with self._lock:
            self._flush_timer = timer if isinstance(timer, threading.Timer) else None

    def flush_progress(self) -> None:
        with self._lock:
            self._flush_at = None
            self._flush_timer = None
            snapshots = self._progress.due(self._now())
        for snapshot in snapshots:
            self._emit_progress(snapshot)
        self._arm_flush()

    # -- hook callbacks (kwargs only; unknown kwargs ignored) ------------------------------

    def _remember_platform(self, session_id: str, platform: str) -> bool:
        """Record a session's platform; True the first time the session is seen."""
        first_sight = session_id not in self._platform_by_session
        if platform:
            self._platform_by_session.remember(session_id, platform)
        return first_sight and bool(platform)

    def pre_llm_call(self, *, session_id: str = "", platform: str = "", user_message: Any = "",
                     is_first_turn: bool = False, **_: Any) -> Optional[dict[str, str]]:
        """Records the turn; returns the delivery note (hermes appends it to the user message) on a
        phone-facing session's first turn or when the user mentions pushes or timers."""
        try:
            with self._lock:
                first_sight = self._remember_platform(session_id, platform)
                self._reply_by_session.pop(session_id, None)
                snapshot = self._progress.turn_started(session_id, self._now())
            if first_sight:  # ids and platform only, never content
                logger.info("hermix-push: session %s platform=%r source=%s", session_id, platform,
                            coarse_source(platform))
            self._emit_progress(snapshot)
            if should_notify(session_id, platform) and (
                is_first_turn or PUSH_INTENT.search(_as_text(user_message))
            ):
                return {"context": delivery_note(session_id)}
        except Exception:
            logger.warning("hermix-push: pre_llm_call failed", exc_info=True)
        return None

    def post_llm_call(self, *, session_id: str = "", user_message: Any = "", assistant_response: Any = "",
                      platform: str = "", **_: Any) -> None:
        try:
            with self._lock:
                self._remember_platform(session_id, platform)
                self._reply_by_session.remember(session_id, (user_message, assistant_response))
        except Exception:
            logger.warning("hermix-push: post_llm_call failed", exc_info=True)

    def on_session_end(self, *, session_id: str = "", completed: bool = False, interrupted: bool = False,
                       failed: bool = False, turn_id: str = "", turn_exit_reason: str = "",
                       platform: str = "", **_: Any) -> None:
        try:
            with self._lock:
                self._remember_platform(session_id, platform)
                captured = self._reply_by_session.pop(session_id, None)
                snapshot = self._progress.turn_ended(session_id, self._now(), failed=bool(failed))
            self._emit_progress(snapshot)
            if interrupted:
                return
            ref = turn_id or str(int(self._now()))
            if failed:
                self._notify("turn_error", session_id, ref,
                             body=turn_exit_reason or "The agent stopped without finishing.")
            elif completed and captured is not None:
                user_message, reply = captured
                self._notify("reply", session_id, ref, subtitle=user_message, body=reply)
        except Exception:
            logger.warning("hermix-push: on_session_end failed; a push was dropped", exc_info=True)

    def pre_tool_call(self, *, tool_name: str = "", args: Optional[dict[str, Any]] = None, session_id: str = "",
                      tool_call_id: str = "", **_: Any) -> None:
        try:
            now = self._now()
            if tool_name == CLARIFY_TOOL:
                with self._lock:
                    snapshot = self._progress.waiting(session_id, now)
                self._emit_progress(snapshot)
                self._notify("clarify", session_id, tool_call_id or f"{tool_name}:{int(now)}",
                             body=clarify_question(args), request_id=tool_call_id)
                return
            with self._lock:
                snapshot = self._progress.tool_started(session_id, tool_name, now)
            self._emit_progress(snapshot)
        except Exception:
            logger.warning("hermix-push: pre_tool_call failed", exc_info=True)

    def post_tool_call(self, *, tool_name: str = "", session_id: str = "", **_: Any) -> None:
        try:
            with self._lock:
                if tool_name == CLARIFY_TOOL:
                    snapshot = self._progress.resumed(session_id, self._now())
                else:
                    snapshot = self._progress.tool_finished(session_id, self._now())
            self._emit_progress(snapshot)
        except Exception:
            logger.warning("hermix-push: post_tool_call failed", exc_info=True)

    def _approval_session(self, session_id: str, session_key: str) -> str:
        """The agent session id when the host bound one, else an opaque id derived from the
        gateway session key, which can embed chat ids or phone numbers and must never go out raw."""
        if session_id:
            return session_id
        keys = self._keys_or_none()
        if not session_key or keys is None:
            return ""
        return keyed_id("session", session_key, install_key=keys.install_key)

    def pre_approval_request(self, *, command: str = "", description: str = "", session_key: str = "",
                             session_id: str = "", surface: str = "", request_id: str = "",
                             tool_call_id: str = "", **_: Any) -> None:
        if surface == "smart":  # auxiliary-model decision, nobody is waiting
            return
        try:
            sid = self._approval_session(session_id, session_key)
            with self._lock:
                snapshot = self._progress.waiting(sid, self._now())
            self._emit_progress(snapshot)
            ref = request_id or tool_call_id or f"approval:{int(self._now())}"
            # The command is shell, not markdown: flattening would turn `*.md` or `__pycache__`
            # into a different command than the one awaiting approval.
            self._notify("approval", sid, ref, subtitle=description, body=command, request_id=request_id,
                         markdown=False)
        except Exception:
            logger.warning("hermix-push: pre_approval_request failed", exc_info=True)

    def post_approval_response(self, *, session_key: str = "", session_id: str = "", **_: Any) -> None:
        try:
            with self._lock:
                snapshot = self._progress.resumed(self._approval_session(session_id, session_key), self._now())
            self._emit_progress(snapshot)
        except Exception:
            logger.warning("hermix-push: post_approval_response failed", exc_info=True)

    def subagent_start(self, *, child_session_id: Optional[str] = None, **_: Any) -> None:
        if child_session_id:
            with self._lock:
                self._subagent_sessions.remember(child_session_id, True)


_shared: dict[str, HermexPush] = {}
_shared_lock = threading.Lock()


def shared_push() -> HermexPush:
    """The instance that sends deliveries for the active profile (adapter and standalone sender).
    It never receives hooks, so it holds no session state."""
    try:
        from hermes_cli.profiles import get_active_profile_name
        profile = get_active_profile_name() or ""
    except Exception:
        profile = ""
    with _shared_lock:
        if profile not in _shared:
            _shared[profile] = HermexPush(profile=profile)
        return _shared[profile]
