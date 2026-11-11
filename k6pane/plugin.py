"""Plugin registration: wires the k6pane tools and the bundled skill into Hermes."""

from __future__ import annotations

from pathlib import Path

from . import hermes_host, schemas, tools

TOOLSET = "k6pane"
SKILL_NAME = "load-testing"
SKILL_PATH = Path(__file__).parent / "skills" / SKILL_NAME / "SKILL.md"


def register(ctx) -> None:
    hermes_host.set_context(ctx)
    ctx.register_tool(name="k6pane_doctor", toolset=TOOLSET, schema=schemas.DOCTOR,
                      handler=tools.doctor, emoji="🩺")
    ctx.register_tool(name="k6pane_run", toolset=TOOLSET, schema=schemas.RUN,
                      handler=tools.run, emoji="📈")
    ctx.register_tool(name="k6pane_report", toolset=TOOLSET, schema=schemas.REPORT,
                      handler=tools.report, emoji="📊")
    # Read-only skill, resolvable as "k6pane:load-testing"; never copied into ~/.hermes/skills.
    hermes_host.register_skill(ctx, SKILL_NAME, SKILL_PATH)
