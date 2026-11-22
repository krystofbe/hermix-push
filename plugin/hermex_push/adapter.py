"""The ``hermix`` platform entry. Hermix is the target of cron ``deliver: hermix`` /
``hermix:<session>`` and of ``send_message``: ``send`` (live gateway) and ``standalone_send``
(cron without a gateway) both seal the message and post it to the relay, then report the relay's
real answer so a job never shows delivered when no phone got it. Nothing arrives from the phone
through this adapter."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, Optional

from . import HOME_CHAT_ID, PLATFORM_NAME, RELAY_URL_ENV
from .hooks import relay_url_from_env, shared_push

logger = logging.getLogger("hermex_push")

_SESSION_REF = re.compile(r"[A-Za-z0-9_.-]{1,200}")


def parse_target(ref: str) -> Optional[tuple[str, None]]:
    """``parse_target_ref_fn``: ``hermix`` (the cron home, ``home``) and ``hermix:<session>`` are
    the only valid targets; anything else is refused instead of guessed."""
    ref = (ref or "").strip()
    return (ref, None) if _SESSION_REF.fullmatch(ref) else None


async def deliver(chat_id: str, content: str) -> tuple[bool, str]:
    return await asyncio.to_thread(shared_push().deliver_message, chat_id, content)


async def standalone_send(pconfig: Any, chat_id: str, message: str, **_: Any) -> dict:
    """``standalone_sender_fn``: delivery from a process without the live adapter."""
    ok, detail = await deliver(chat_id or HOME_CHAT_ID, message)
    return {"success": True, "message_id": detail} if ok else {"error": detail}


def check_requirements() -> bool:
    """Passive probe: stdlib plus cryptography, which hermes-agent already pins."""
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def is_connected(config: Any = None) -> bool:
    return bool(relay_url_from_env())


def env_enablement() -> Optional[dict]:
    """Auto-enable the platform when the relay URL is set, so ``gateway status`` shows it."""
    url = relay_url_from_env()
    return {"relay_url": url} if url else None


def make_adapter(config: Any):
    """``adapter_factory``; imports gateway modules lazily so the plugin loads without them."""
    from gateway.config import Platform
    from gateway.platforms.base import BasePlatformAdapter, SendResult

    class HermixAdapter(BasePlatformAdapter):
        def __init__(self, cfg: Any) -> None:
            super().__init__(config=cfg, platform=Platform(PLATFORM_NAME))

        async def connect(self, *, is_reconnect: bool = False) -> bool:
            if not relay_url_from_env():
                logger.warning("[%s] %s not configured", self.name, RELAY_URL_ENV)
                return False
            self._mark_connected()
            return True

        async def disconnect(self) -> None:
            self._mark_disconnected()

        async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                       metadata: Optional[Dict[str, Any]] = None) -> SendResult:
            ok, detail = await deliver(chat_id or HOME_CHAT_ID, content)
            return SendResult(success=True, message_id=detail) if ok else SendResult(success=False, error=detail)

        async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
            return {"name": "Hermix", "type": "dm"}

    return HermixAdapter(config)


PLATFORM_HINT = (
    "Your final reply may be shown as a phone notification preview. Lead with the outcome in one "
    "or two plain sentences before any detail."
)
