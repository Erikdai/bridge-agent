"""Replay a skill without an LLM and report exactly where it broke.

Resolution is by descriptor (role + name): exact match first, then
case-insensitive substring. The result carries the index of the first failed
step plus a trail of compact observations taken after every step, so a single
LLM call can afterwards verify the outcome and answer the task, or take over
from the failure point with full context.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..actions import Action, BrowserExecutor, DesktopExecutor, dispatch
from ..focus import Focus, activate_window_by_title
from ..tree import Observation, UINode
from .params import resolve as resolve_template
from .params import variables_from_json
from .store import Skill, SkillStep

Observe = Callable[[], tuple[Focus, Observation]]


@dataclass
class ReplayResult:
    failed_at: int | None  # 0-based index of the failing step, None if all ran
    error: str = ""
    trail: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.failed_at is None


def resolve(obs: Observation, target: dict[str, str]) -> UINode | None:
    role, name = target.get("role"), (target.get("name") or "")
    for n in obs.nodes:
        if n.role == role and n.name == name:
            return n
    low = name.lower()
    for n in obs.nodes:
        if n.role == role and low and low in n.name.lower():
            return n
    return None


def step_desc(step: SkillStep) -> str:
    bits = [step.action]
    if step.target:
        bits.append(f'{step.target.get("role")} "{step.target.get("name")}"')
    for k in ("text", "keys", "url", "command", "path", "site"):
        v = getattr(step, k)
        if k == "text" and step.template:
            v = step.template
        if v:
            bits.append(f"{k}={v[:40]!r}")
    if step.submit:
        bits.append("submit")
    return " ".join(bits)


def _subst(value: str | None, substitutions: dict[str, str]) -> str | None:
    if not value:
        return value
    for k, v in substitutions.items():
        value = value.replace(k, v)
    return value


def _to_action(step: SkillStep, node: UINode | None, substitutions: dict[str, str] | None = None) -> Action:
    s = substitutions or {}
    return Action(
        type=step.action,
        ref=node.ref if node else None,
        text=_subst(step.text, s),
        keys=step.keys,  # type: ignore[arg-type]
        url=_subst(step.url, s),
        command=_subst(step.command, s),
        path=_subst(step.path, s),
        submit=step.submit,
        site=step.site,
    )


def replay(
    skill: Skill,
    observe: Observe,
    browser_exec: BrowserExecutor,
    desktop_exec: DesktopExecutor,
    on_step: Callable[[int, str, str], None] | None = None,
    substitutions: dict[str, str] | None = None,
) -> ReplayResult:
    """`substitutions` maps placeholders stored in the skill (e.g. {DOWNLOADS}) to this run's values."""
    t0 = time.time()
    trail: list[str] = []
    n = len(skill.steps)
    page_text = ""  # latest page text seen; variables are resolved against it
    for i, step in enumerate(skill.steps):
        focus, obs = observe()
        if obs.surface == "browser" and obs.page_text:
            page_text = obs.page_text
        if step.template and step.variables:
            if not page_text and browser_exec.activate is not None:  # need the page before a desktop step
                browser_exec.activate()
                _, b = observe()
                page_text = b.page_text if b.surface == "browser" else ""
                if step.surface == "desktop" and step.window:
                    activate_window_by_title(step.window)
                focus, obs = observe()
            value, err = resolve_template(step.template, variables_from_json(step.variables), page_text)
            if value is None:
                return ReplayResult(i, err, trail, time.time() - t0)
            step = SkillStep(**{**step.__dict__, "text": value})
        # Focus can be stolen between steps (the user clicks elsewhere, a toast pops up).
        # Put the window the step was recorded in back in front before looking for the target.
        if step.surface == "browser" and obs.surface != "browser" and browser_exec.activate is not None:
            browser_exec.activate()
            focus, obs = observe()
        elif step.surface == "desktop" and step.window and obs.title != step.window and step.action != "launch":
            if activate_window_by_title(step.window):
                focus, obs = observe()
        node = resolve(obs, step.target) if step.target else None
        if step.target and node is None:
            err = f'{step.target.get("role")} "{step.target.get("name")}" not found on {obs.surface} window "{obs.title[:50]}"'
            return ReplayResult(i, err, trail, time.time() - t0)
        action = _to_action(step, node, substitutions)
        result = dispatch(action, obs, browser_exec, desktop_exec).execute(action, obs)
        if not result.ok:
            return ReplayResult(i, result.message, trail, time.time() - t0)
        _, after = observe()
        trail.append(f"after step {i + 1} ({step_desc(step)}): {after.compact()}")
        if on_step:
            on_step(i + 1, step_desc(step), result.message)
        if n > i + 1:
            time.sleep(0.3)
    return ReplayResult(None, "", trail, time.time() - t0)
