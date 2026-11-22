"""hermix-push: hermes-agent platform plugin entry point.

Registers the ``hermix`` platform and the lifecycle hooks that turn turn boundaries, approvals
and questions into sealed relay events. The dashboard pairing route lives in
``dashboard/plugin_api.py`` and is mounted by the web server, not here.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The dashboard imports its API file standalone, so shared code is a package imported by name
# from the plugin root (see hermix_push/__init__.py). Appended, never prepended: nothing in
# here may shadow a host module. With the plugin installed in several profiles the first copy
# imported serves every scope in that process; restart after upgrading.
_ROOT = str(Path(__file__).resolve().parent)
if _ROOT not in sys.path:
    sys.path.append(_ROOT)

import os  # noqa: E402

from hermix_push import HOME_CHANNEL_ENV, HOME_CHAT_ID, PLATFORM_NAME, RELAY_URL_ENV  # noqa: E402
from hermix_push.adapter import (  # noqa: E402
    PLATFORM_HINT, check_requirements, env_enablement, is_connected, make_adapter, parse_target, standalone_send,
)
from hermix_push.hooks import HermexPush  # noqa: E402
from hermix_push.scopes import serve_every_profile_in_host  # noqa: E402


def register(ctx) -> None:
    """Plugin entry point called by the Hermes plugin system, once per profile scope."""
    # Bare ``deliver: hermix`` needs a home chat id from this env var; "home" reaches every phone.
    os.environ.setdefault(HOME_CHANNEL_ENV, HOME_CHAT_ID)
    ctx.register_platform(
        name=PLATFORM_NAME, label="Hermix", adapter_factory=make_adapter, check_fn=check_requirements,
        is_connected=is_connected, required_env=[RELAY_URL_ENV],
        install_hint="pip install cryptography   # already a Hermes dependency",
        env_enablement_fn=env_enablement, emoji="📱", pii_safe=True, platform_hint=PLATFORM_HINT,
        cron_deliver_env_var=HOME_CHANNEL_ENV, parse_target_ref_fn=parse_target,
        standalone_sender_fn=standalone_send,
    )
    profile = ""
    try:
        profile = str(getattr(ctx, "profile_name", "") or "")
    except Exception:
        pass
    push = HermexPush(profile=profile)
    for hook_name, callback in push.hook_callbacks().items():
        ctx.register_hook(hook_name, callback)
    # The first copy loaded in a process also serves every profile without its own copy.
    undo = serve_every_profile_in_host()
    if callable(getattr(ctx, "on_unload", None)):
        ctx.on_unload(push.close)
        if undo is not None:
            ctx.on_unload(undo)
