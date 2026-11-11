"""Plugin registration: wires tools to Hermes and installs the bundled skill."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from . import schemas, tools

logger = logging.getLogger(__name__)

TOOLSET = "k6pane"
_SKILL_SOURCE = Path(__file__).parent / "skill.md"


def _install_skill() -> None:
    """Copy the bundled skill to ~/.hermes/skills/k6pane/ once; never overwrite user edits."""
    try:
        try:
            from hermes_cli.config import get_hermes_home
            dest = Path(get_hermes_home()) / "skills" / "k6pane" / "SKILL.md"
        except Exception:
            dest = Path.home() / ".hermes" / "skills" / "k6pane" / "SKILL.md"
        if dest.exists() or not _SKILL_SOURCE.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_SKILL_SOURCE, dest)
    except Exception as exc:
        logger.debug("k6pane: skill install skipped: %s", exc)


def register(ctx) -> None:
    ctx.register_tool(name="k6pane_doctor", toolset=TOOLSET, schema=schemas.DOCTOR,
                      handler=tools.doctor, emoji="🩺")
    ctx.register_tool(name="k6pane_run", toolset=TOOLSET, schema=schemas.RUN,
                      handler=tools.run, emoji="📈")
    ctx.register_tool(name="k6pane_report", toolset=TOOLSET, schema=schemas.REPORT,
                      handler=tools.report, emoji="📊")
    _install_skill()
