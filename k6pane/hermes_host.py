"""The only place k6pane touches Hermes. Everything goes through documented public surfaces.

- Settings: ``ctx.get_config`` (``plugins.entries.k6pane.settings.*``), with
  ``K6PANE_*`` environment variables as a fallback for non-secret settings.
- Secrets: ``agent.secret_scope.get_secret``, which honours the active profile
  (catalog rule 10). k6pane never reads ``.env`` or any credential file itself.
- Durable data: ``plugins.plugin_storage.plugin_data_dir("k6pane")``.
- Skill: ``ctx.register_skill`` (read-only, never copied into ``~/.hermes/skills``).

Each falls back cleanly on older Hermes releases that predate the surface, so
the plugin keeps working there; outside Hermes (tests, ``python -m``) only the
fallbacks apply.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

PLUGIN_ID = "k6pane"
_ctx: Any = None
_UNSET = object()


def set_context(ctx: Any) -> None:
    global _ctx
    _ctx = ctx


def setting(key: str, env_name: str, default: Any = None) -> Any:
    """A non-secret setting: Hermes plugin settings first, then the K6PANE_* env var, then default."""
    if _ctx is not None and hasattr(_ctx, "get_config"):
        try:
            value = _ctx.get_config(key, _UNSET)
        except Exception:
            value = _UNSET
        if value is not _UNSET and value is not None and value != "":
            return value
    raw = os.environ.get(env_name)
    if raw is not None and raw != "":
        return raw
    return default


class SecretUnavailable(Exception):
    """Hermes refused to resolve a secret (e.g. no profile scope under multiplexing)."""


def get_secret(name: str) -> Optional[str]:
    """Resolve a credential by env-var name through Hermes' profile-aware secret scope."""
    try:
        from agent.secret_scope import get_secret as hermes_get_secret
    except ImportError:
        # Hermes too old to have profile secret scopes (no multiplexing either), or not
        # running inside Hermes at all: the process environment is the only source.
        return os.environ.get(name)
    try:
        return hermes_get_secret(name)
    except Exception as exc:  # UnscopedSecretError: fail closed, never guess another profile's value
        raise SecretUnavailable(f"Hermes could not resolve {name} in the current profile: {exc}") from None


def data_dir() -> Path:
    """Where runs are stored. K6PANE_HOME overrides; otherwise the Hermes per-plugin data dir."""
    override = setting("home", "K6PANE_HOME")
    if override:
        path = Path(str(override)).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path
    try:
        from plugins.plugin_storage import plugin_data_dir
    except ImportError:
        path = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / PLUGIN_ID
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path(plugin_data_dir(PLUGIN_ID))


# Outcome of register_skill, reported by k6pane_doctor so a missing skill is never silent.
skill_status: dict = {"registered": False, "reason": "register() has not run"}


def register_skill(ctx: Any, name: str, path: Path) -> bool:
    """Register the bundled skill read-only. Returns False on Hermes releases without the API.

    Older Hermes (e.g. 0.7.x) has no plugin skill API and only discovers skills in
    ``~/.hermes/skills`` or ``skills.external_dirs``. k6pane never writes there itself;
    instead the status records the one config line the user can add.
    """
    skills_root = str(Path(path).resolve().parent.parent)
    if not hasattr(ctx, "register_skill"):
        skill_status.update(
            registered=False,
            name=name,
            reason="This Hermes release has no plugin skill API (ctx.register_skill).",
            fix=f"Add {skills_root} to skills.external_dirs in config.yaml, then use -s {name}.",
            skills_dir=skills_root,
        )
        return False
    try:
        ctx.register_skill(name, path)
    except Exception as exc:
        skill_status.update(registered=False, name=name, reason=f"register_skill failed: {exc}",
                            fix=f"Add {skills_root} to skills.external_dirs in config.yaml.",
                            skills_dir=skills_root)
        return False
    skill_status.clear()
    skill_status.update(registered=True, name=f"{PLUGIN_ID}:{name}")
    return True
