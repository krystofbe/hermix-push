"""Shared code for the hermix-push plugin.

Imported by both the plugin entry point (``plugin/__init__.py``) and the dashboard route
(``dashboard/plugin_api.py``). The dashboard loads its API file standalone, outside the plugin
package, so both callers put the plugin root on ``sys.path`` and import this package by name.
"""

PLUGIN_NAME = "hermix-push"
PLATFORM_NAME = "hermix"
RELAY_URL_ENV = "HERMIX_PUSH_RELAY_URL"
# Names from before the Hermix fork: the old env var is still read as a fallback, and keys paired
# under the old plugin name are copied to the new one on first use (``keys``).
LEGACY_PLUGIN_NAME = "hermex-push"
LEGACY_RELAY_URL_ENV = "HERMEX_PUSH_RELAY_URL"
# Cron ``deliver: hermix`` resolves its chat id from this env var; ``home`` means every device.
HOME_CHANNEL_ENV = "HERMIX_HOME_CHANNEL"
HOME_CHAT_ID = "home"

# Bump on every merged change under plugin/ (patch for fixes, minor for features), together with
# plugin.yaml, pyproject.toml and dashboard/manifest.json; a test keeps the four in step. The
# pairing route reports this loaded value, so Hermex can tell an outdated or not-yet-restarted host.
PLUGIN_VERSION = "0.8.0"

# The identifier Hermex sends to ``POST /api/dashboard/agent-plugins/install``. hermes-agent
# splits it at ``.git/`` into the clone URL and the ``plugin`` subdirectory.
INSTALL_IDENTIFIER = "https://github.com/krystofbe/hermix-push.git/plugin"

__all__ = [
    "PLUGIN_NAME", "PLATFORM_NAME", "RELAY_URL_ENV", "LEGACY_PLUGIN_NAME", "LEGACY_RELAY_URL_ENV",
    "HOME_CHANNEL_ENV", "HOME_CHAT_ID", "PLUGIN_VERSION", "INSTALL_IDENTIFIER",
]
