"""The observe -> route -> decide -> act loop, plus skill replay and self-healing.

Each step: look at which window owns the keyboard (focus.py), observe that
surface into the unified tree, ask the planner for exactly one action, run it
through the executor the ref points at, and log everything to runs/<timestamp>/.

Skills: a task that matches a saved skill is replayed without the LLM first.
One verification call then confirms the outcome. If replay breaks (an element
disappeared, an app changed), the LLM takes over from that exact point and the
repaired step sequence is written back, so the next run is cheap again.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .actions import Action, BrowserExecutor, DesktopExecutor, ExecResult, dispatch
from .config import Settings
from .focus import CHROMIUM_WINDOW_CLASS, Focus, bring_to_front, get_focus, top_windows_for_pid
from .guard import ConfirmPolicy, Permissions, confirm, needs_confirmation
from .llm import Decision, Planner, PlannerRefused
from .llm.base import SkillCompaction
from .observers import observe_browser, observe_desktop
from .observers.browser_ui import observe_browser_dialogs
from .skills.params import parameterize, variables_to_json
from .skills.replay import ReplayResult, replay, step_desc
from .skills.store import Skill, SkillStep, SkillStore
from .tree import Observation

SYSTEM_PROMPT = """You are BridgeAgent, an operator completing tasks on a Windows PC.

Each step you receive ONE accessibility tree. The SURFACE line says where keyboard focus is:
- browser: the tree is the current web page; refs look like b12. navigate, scroll, click and type act on the page.
  If Chrome has a dialog open on top of the page (print preview, a permission bubble, an "Open app?" prompt, a passkey sheet), its controls are listed too with d refs. They are not part of the page.
- desktop: the tree is the foreground native window (a file dialog, a Windows Hello / passkey prompt, a certificate picker, Excel, Explorer, a password manager...); refs look like d7.
The task may start on a web page and continue in a native app, or the other way round. That handoff is normal: just act on whatever surface is in front of you.

Rules:
1. Exactly one action per step. Never invent refs that are not listed. Every action in HISTORY marked ok DID take effect; build on it instead of undoing it.
2. Prefer clicking/typing by ref. To enter a whole string (an expression, a file name) into the focused window, use type with no ref: it sends the keystrokes. press is for ONE key or chord (Enter, Escape, Control+S), never a string.
   Make the most progress per step: type "1234+5678=" in one action instead of clicking nine buttons; never re-enter something that HISTORY shows was entered correctly.
3. You only see ONE surface per step. Anything you read on the page is NOT visible once focus moves to a desktop app, and vice versa. Whenever you read a value you will need later (numbers, names, file paths), put it in `note` in the same step. Your notes are shown back to you every step.
4. upload attaches a local file (full path) to a fileinput element on the page; no native file dialog is involved.
   launch opens an app or file and brings its window to the front. Launching the same app again only re-focuses the existing window; do not launch repeatedly.
5. If the tree is empty or belongs to the wrong window, use focus_browser, launch, or press, or wait once. Do not loop on the same action.
6. Never type a password. For logins use fill_credential with a site key that the task lists; if none is listed, stop with done and explain.
7. Risky actions (send, pay, delete, submit, ...) are intercepted by a confirmation dialog shown to the human; if it reports "blocked by user", stop with fail and say what was blocked. Do not try to get around it.
   The user's policy may also block an app, folder, URL or window ("blocked by policy: ..."): pick another allowed way, or stop with fail and say what the policy blocked.
8. When the observation already shows the outcome the task asked you to report, reply done at once; never repeat an action that HISTORY shows succeeded. When finished, reply done with a short summary that includes any answer the task asked for. If stuck after trying alternatives, reply fail with the reason.
"""


LOG_SINK = None  # optional callable(str); the GUI sets it to receive progress lines


def _say(*parts: object) -> None:
    """Progress output: the GUI sink if set, stderr under MCP (stdout is the transport), else stdout."""
    import os
    import sys

    line = " ".join(str(p) for p in parts)
    if LOG_SINK is not None:
        LOG_SINK(line)
        return
    print(line, file=sys.stderr if os.environ.get("BRIDGE_LOG_STDERR") else sys.stdout, flush=True)


SPARSE_TREE_THRESHOLD = 4  # fewer interactive desktop nodes than this -> attach a screenshot
MAX_CONSECUTIVE_FAILURES = 3
HISTORY_WINDOW = 12


@dataclass
class StepRecord:
    step: int
    surface: str
    focus: str
    decision: Decision
    result: ExecResult
    target: dict[str, str] | None
    usage: dict
    seconds: float
    window: str = ""
    context_text: str = ""  # page text the model had seen by then; typed values found in it become variables

    def line(self) -> str:
        status = "ok" if self.result.ok else f"error: {self.result.message}"
        if self.result.ok and self.result.message:
            status += f" ({self.result.message})"
        desc = self.decision.action.describe()
        if self.target:  # say what the ref pointed at; refs are renumbered every step
            desc += f' = {self.target.get("role")} "{self.target.get("name")}"'
        return f"{self.step}. [{self.surface}] {desc} -> {status}"


@dataclass
class RunResult:
    task: str
    status: str  # done | fail | max_steps | refused | blocked
    summary: str
    steps: list[StepRecord] = field(default_factory=list)
    run_dir: Path | None = None
    seconds: float = 0.0
    mode: str = "llm"  # llm | replay | replay+llm
    skill: str | None = None
    replay_steps: int = 0
    extra_usage: dict = field(default_factory=dict)  # verification call etc.

    @property
    def llm_calls(self) -> int:
        return len(self.steps) + (1 if self.extra_usage else 0)

    @property
    def tokens(self) -> dict:
        totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
        for s in self.steps:
            for k in totals:
                totals[k] += int(s.usage.get(k, 0) or 0)
        for k in totals:
            totals[k] += int(self.extra_usage.get(k, 0) or 0)
        return totals


class BrowserSession:
    """Owns the Playwright browser for one run. Downloads land in run_dir/downloads."""

    def __init__(self, settings: Settings, run_dir: Path, start_url: str | None = None):
        self.settings = settings
        self.run_dir = run_dir
        self.start_url = start_url
        self._pw = None
        self._pid: int | None = None
        self.main_hwnd: int | None = None
        self.browser = None
        self.context = None
        self.downloads: list[Path] = []  # files saved by the download handler, shown to the model

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        downloads = self.run_dir / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        if self.settings.cdp_url:
            self.browser = self._pw.chromium.connect_over_cdp(self.settings.cdp_url)
            self.context = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
        else:
            self.browser = self._pw.chromium.launch(
                headless=False, channel=self.settings.browser_channel, args=["--start-maximized"]
            )
            self.context = self.browser.new_context(no_viewport=True, accept_downloads=True)
        self.context.on("page", lambda p: p.on("download", self._save_download))
        page = self.context.pages[0] if self.context.pages else self.context.new_page()
        page.on("download", self._save_download)
        if self.start_url:
            page.goto(self.start_url, wait_until="domcontentloaded")
        if not self.activate():
            _say("warning: could not bring the browser window to the foreground")
        return self

    def browser_pid(self) -> int | None:
        """Browser process id via CDP; works for launched and CDP-attached browsers alike."""
        if self._pid is None:
            try:  # SystemInfo is a browser-level domain, so it needs a browser CDP session
                cdp = self.browser.new_browser_cdp_session()
                info = cdp.send("SystemInfo.getProcessInfo")
                cdp.detach()
                for proc in info.get("processInfo", []):
                    if proc.get("type") == "browser":
                        self._pid = int(proc["id"])
                        break
            except Exception:
                return None
        return self._pid

    def activate(self) -> bool:
        """Bring the browser *window* to the OS foreground, not just the tab."""
        try:
            self.page.bring_to_front()
        except Exception:
            pass
        pid = self.browser_pid()
        if not pid:
            return False
        for hwnd in top_windows_for_pid(pid, CHROMIUM_WINDOW_CLASS):
            if bring_to_front(hwnd):
                self.main_hwnd = hwnd
                return True
        return False

    def _save_download(self, download) -> None:
        try:
            target = (self.run_dir / "downloads" / download.suggested_filename).resolve()
            download.save_as(target)
            self.downloads.append(target)
        except Exception:
            pass

    @property
    def page(self):
        pages = self.context.pages
        return pages[-1] if pages else self.context.new_page()

    def __exit__(self, *exc):
        try:
            if not self.settings.cdp_url and self.browser is not None:
                self.browser.close()
        finally:
            if self._pw is not None:
                self._pw.stop()


class Agent:
    def __init__(self, settings: Settings, planner: Planner, store: SkillStore | None = None):
        self.settings = settings
        self.planner = planner
        self.store = store or SkillStore(settings.skills_dir)
        self.permissions = Permissions.load(settings.permissions_file)
        self.policy = ConfirmPolicy(self.permissions.confirm or settings.confirm_mode, settings.confirm_timeout)
        self._last_owned_hwnd: int | None = None
        self.stop_requested = False  # set from another thread (GUI stop button); checked before each step

    # -- observation ------------------------------------------------------

    def _owned_pids(self, session: BrowserSession) -> set[int]:
        """Processes the agent is working with: the driven browser plus anything it launched or acted in."""
        from .actions import owned_hwnds

        if session.main_hwnd:
            owned_hwnds.add(session.main_hwnd)
        pids = {session.browser_pid() or -1}
        for h in list(owned_hwnds):
            p = _pid_of(h)
            if p:
                pids.add(p)
        return pids

    def _focus_guard(self, session: BrowserSession, focus: Focus) -> Focus:
        """Push the agent's own window back in front when an unrelated app has stolen focus.

        A dialog of one of the agent's processes is legitimate and stays. Another
        browser, a chat app, a toast from the shell: pushed back behind the
        window the agent was last working in.
        """
        owned = self._owned_pids(session)
        if focus.pid in owned:
            self._last_owned_hwnd = focus.hwnd
            return focus
        last = self._last_owned_hwnd or session.main_hwnd
        if last and bring_to_front(last):
            restored = get_focus()
            _say(
                f"      (focus guard) {focus.process} '{focus.title[:30]}' was in front; restored '{restored.title[:30]}'"
            )
            return restored
        return focus

    def _wants_screenshot(self, obs: Observation) -> bool:
        mode = self.settings.screenshots
        return mode == "always" or (mode == "sparse" and obs.interactive_count() < SPARSE_TREE_THRESHOLD)

    def observe(self, session: BrowserSession) -> tuple[Focus, Observation]:
        focus = self._focus_guard(session, get_focus())
        if focus.kind == "browser" and focus.pid != session.browser_pid():
            # some other browser (the user's own Edge/Chrome), not the one this agent drives
            obs = observe_desktop()
            obs.note = (
                "this is a different browser window, not the agent's page; use focus_browser to return to the page"
            )
            return focus, obs
        if focus.kind == "browser" and session.main_hwnd and focus.hwnd != session.main_hwnd:
            # a popup window owned by our browser (extension popup, picker) rather than the page
            obs = observe_desktop()
            obs.note = "this is a browser popup window, not the page; use focus_browser to return to the page"
            return focus, obs
        if focus.kind == "browser":
            try:
                obs = observe_browser(session.page)
            except Exception as e:  # page navigating / closed mid-observation
                obs = Observation(
                    surface="browser",
                    title="",
                    detail=str(e)[:120],
                    nodes=[],
                    note="the page could not be read this step; wait once or focus_browser",
                )
            try:
                dialog_nodes, title = observe_browser_dialogs()
            except Exception:  # UIA elements vanish while being walked (a bubble closing); not fatal
                dialog_nodes, title = [], ""
            if dialog_nodes:
                obs.nodes = obs.nodes + dialog_nodes
                obs.note = (
                    f'Chrome dialog "{title}" is open on top of the page; its controls are the d refs below '
                    f"and are NOT part of the page DOM. Deal with it first."
                )
            if self.settings.screenshots == "always":
                with contextlib.suppress(Exception):
                    obs.screenshot_png = session.page.screenshot()
            return focus, obs
        try:
            obs = observe_desktop()
            if self._wants_screenshot(obs):
                obs = observe_desktop(with_screenshot=True)
        except Exception as e:
            obs = Observation(
                surface="desktop",
                title=focus.title,
                detail=f"{focus.process} / {focus.window_class}",
                nodes=[],
                note=f"the window could not be read this step ({type(e).__name__}); wait once",
                hwnd=focus.hwnd,
                pid=focus.pid,
            )
        return focus, obs

    # -- prompt -----------------------------------------------------------

    def _user_prompt(
        self,
        task: str,
        sites: list[str],
        history: list[StepRecord],
        obs: Observation,
        step: int,
        notes: list[str],
        other_surface: Observation | None,
        hint: str | None,
        downloads_dir: Path | None = None,
        downloads: list[Path] | None = None,
        changed: list[str] | None = None,
    ) -> str:
        parts = [f"TASK: {task}"]
        if hint:
            parts.append("CONTEXT: " + hint)
        if downloads_dir is not None:
            parts.append(f"DOWNLOADS FOLDER (files downloaded in the browser are saved here): {downloads_dir}")
        if downloads:
            parts.append(
                "DOWNLOADED FILES (full paths; use launch with a path to open one, or open the folder in Explorer):\n"
                + "\n".join(f"- {p}" for p in downloads[-5:])
            )
        if sites:
            parts.append("CREDENTIAL SITE KEYS AVAILABLE FOR fill_credential: " + ", ".join(sites))
        if notes:
            parts.append("YOUR NOTES (facts you saved earlier):\n" + "\n".join(f"- {n}" for n in notes[-20:]))
        if history:
            recent = history[-HISTORY_WINDOW:]
            parts.append("HISTORY (most recent last):\n" + "\n".join(r.line() for r in recent))
            last = [r.decision.action.describe() for r in history[-6:]]
            if len(last) >= 3 and len(set(last[-3:])) == 1:
                parts.append("WARNING: you repeated the same action three times. Choose a different approach.")
            elif len(last) == 6 and len(set(last)) <= 2:
                parts.append(
                    "WARNING: you are alternating between the same two actions without progress. "
                    "Save what you know in `note`, then do the next concrete step on the current surface."
                )
        if other_surface is not None:
            parts.append(
                f"LAST SEEN ON THE {other_surface.surface.upper()} SURFACE (may be stale):\n" + other_surface.compact()
            )
        parts.append(f"CURRENT OBSERVATION (step {step}):\n{obs.to_prompt()}")
        return "\n\n".join(parts)

    # -- entry point ------------------------------------------------------

    def run(
        self,
        task: str,
        sites: list[str] | None = None,
        start_url: str | None = None,
        use_skills: bool = True,
        skill_name: str | None = None,
    ) -> RunResult:
        sites = sites or []
        run_dir = Path(self.settings.runs_dir) / time.strftime("%Y%m%d-%H%M%S")
        run_dir.mkdir(parents=True, exist_ok=True)
        trace = (run_dir / "trace.jsonl").open("a", encoding="utf-8")
        t0 = time.time()
        result = RunResult(task=task, status="max_steps", summary="", run_dir=run_dir)

        with BrowserSession(self.settings, run_dir, start_url) as session:
            bx = BrowserExecutor(session.page, activate=session.activate)
            dx = DesktopExecutor(activate=session.activate)
            hint: str | None = None
            heal_prefix: list[SkillStep] | None = None  # steps that replayed fine before the break
            skill = self.store.match(task) if use_skills else None

            if skill is not None:
                _say(
                    f"skill: {skill.name} ({len(skill.steps)} steps, {skill.source}, {skill.successes}/{skill.runs} ok) -> replaying without LLM"
                )
                result.skill = skill.name
                rr = replay(
                    skill,
                    lambda: self.observe(session),
                    bx,
                    dx,
                    on_step=lambda i, d, m: _say(
                        f"[replay {i}/{len(skill.steps)}] {d} -> ok" + (f" ({m})" if m else "")
                    ),
                    substitutions={DOWNLOADS_PLACEHOLDER: str((session.run_dir / "downloads").resolve())},
                )
                result.replay_steps = len(skill.steps) if rr.ok else rr.failed_at
                trace.write(
                    json.dumps(
                        {
                            "replay": skill.name,
                            "failed_at": rr.failed_at,
                            "error": rr.error,
                            "trail": rr.trail,
                            "seconds": round(rr.seconds, 1),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                if rr.ok:
                    verdict, usage = self._verify_replay(task, skill, rr, session)
                    result.extra_usage = usage
                    _say(f"[verify] {verdict.reasoning[:120]}")
                    if verdict.action.type == "done":
                        result.status, result.summary, result.mode = "done", verdict.action.summary or "", "replay"
                        skill.runs += 1
                        skill.successes += 1
                        self.store.save(skill)
                    else:
                        hint = (
                            f'A saved skill "{skill.name}" was replayed in full, but the outcome does not look right: '
                            f"{verdict.action.summary or verdict.reasoning} Continue from the current state and finish the task properly."
                        )
                        skill.runs += 1
                        skill.last_status = "failed"
                        self.store.save(skill)
                        result.mode = "replay+llm"
                        _say("[verify] not done -> handing over to the LLM")
                else:
                    i = rr.failed_at
                    done_lines = "; ".join(step_desc(s) for s in skill.steps[:i]) or "(none)"
                    hint = (
                        f'A saved skill "{skill.name}" was replayed. Steps 1-{i} succeeded ({done_lines}). '
                        f"Step {i + 1} ({step_desc(skill.steps[i])}) FAILED: {rr.error}. "
                        f"The screen is in the state right after step {i}. Continue from here and finish the task; "
                        f"the UI may have changed, so look for the equivalent control."
                    )
                    heal_prefix = list(skill.steps[:i])
                    result.mode = "replay+llm"
                    _say(f"[replay {i + 1}/{len(skill.steps)}] {step_desc(skill.steps[i])} -> FAILED: {rr.error}")
                    _say("[heal] handing over to the LLM from this point")

            if result.status != "done":
                self._loop(session, bx, dx, task, sites, result, trace, hint)

            if use_skills and result.status == "done" and result.mode != "replay":
                new_steps = distill_steps(result.steps, downloads_dir=(session.run_dir / "downloads").resolve())
                if heal_prefix is not None and skill is not None:
                    merged = heal_prefix + new_steps
                    skill.steps = self._compact(task, merged, result)
                    skill.source, skill.healed, skill.last_status = "healed", skill.healed + 1, "ok"
                    skill.runs += 1
                    skill.successes += 1
                    path = self.store.save(skill)
                    _say(
                        f"[heal] skill rewritten: {len(heal_prefix)} replayed + {len(new_steps)} new -> {len(skill.steps)} steps after compaction -> {path}"
                    )
                elif result.mode == "llm" and new_steps:
                    steps = self._compact(task, new_steps, result)
                    fresh = Skill(
                        name=skill_name or slugify(task),
                        description=task,
                        steps=steps,
                        source="distilled",
                        runs=1,
                        successes=1,
                    )
                    path = self.store.save(fresh)
                    result.skill = fresh.name
                    _say(f"[skill] learned {len(new_steps)} steps, kept {len(steps)} after compaction -> {path}")

        trace.close()
        result.seconds = time.time() - t0
        (run_dir / "summary.json").write_text(
            json.dumps(
                {
                    "task": task,
                    "status": result.status,
                    "summary": result.summary,
                    "mode": result.mode,
                    "skill": result.skill,
                    "replay_steps": result.replay_steps,
                    "llm_steps": len(result.steps),
                    "llm_calls": result.llm_calls,
                    "seconds": round(result.seconds, 1),
                    "tokens": result.tokens,
                    "model": self.planner.model,
                    "provider": self.settings.provider,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return result

    # -- skill compaction (one LLM call) ----------------------------------

    def _compact(self, task: str, steps: list[SkillStep], result: RunResult) -> list[SkillStep]:
        """Ask the model which recorded steps a clean replay actually needs.

        Trajectories contain detours: re-launching an app that was already open,
        clearing a value that was typed by mistake, closing a stray dialog.
        Replaying those would re-create the mess, so the skill keeps the
        minimal ordered subset. Falls back to the full list if the call fails.
        """
        if len(steps) <= 2:
            return steps
        listing = "\n".join(
            f"{i + 1}. [{s.surface}{(' / ' + s.window) if s.window else ''}] {step_desc(s)}"
            for i, s in enumerate(steps)
        )
        prompt = (
            f"TASK: {task}\n\nThese actions were executed, in order, and the task succeeded:\n{listing}\n\n"
            "Select the minimal ordered subset that a replay on a fresh machine needs to complete the same task. "
            "Drop repeated launches of an app that is already open, actions that undo or redo earlier ones, and "
            "clicks on dialogs that belonged to other applications. Keep every launch/type/click that contributes "
            "to the final result, and keep the final cleanup (e.g. closing the app) if the task asked for it. "
            "Return the 1-based indices to keep."
        )
        try:
            out, usage = self.planner.ask(SYSTEM_PROMPT, prompt, SkillCompaction)
            for k in result.extra_usage.keys() | usage.keys():
                result.extra_usage[k] = int(result.extra_usage.get(k, 0) or 0) + int(usage.get(k, 0) or 0)
            keep = [i for i in out.keep if 1 <= i <= len(steps)]
            keep = sorted(dict.fromkeys(keep))
            if keep:
                return [steps[i - 1] for i in keep]
        except Exception as e:
            _say(f"[skill] compaction skipped ({type(e).__name__}: {str(e)[:80]})")
        return steps

    # -- replay verification (one LLM call) -------------------------------

    def _verify_replay(
        self, task: str, skill: Skill, rr: ReplayResult, session: BrowserSession
    ) -> tuple[Decision, dict]:
        _, obs = self.observe(session)
        trail = "\n".join(rr.trail) or "(no trail)"
        prompt = (
            f"TASK: {task}\n\n"
            f'A saved skill "{skill.name}" was just replayed step by step WITHOUT you. What was seen after each step:\n{trail}\n\n'
            f"CURRENT OBSERVATION:\n{obs.to_prompt()}\n\n"
            "Decide whether the task is fully complete. If yes, reply with action done and a summary that answers the task, "
            "using values seen in the trail (e.g. a calculator display). If something is wrong or missing, reply with action fail "
            "and say what. Use only done or fail."
        )
        return self.planner.decide(SYSTEM_PROMPT, prompt, None)

    # -- the LLM loop -----------------------------------------------------

    def _loop(
        self,
        session: BrowserSession,
        bx: BrowserExecutor,
        dx: DesktopExecutor,
        task: str,
        sites: list[str],
        result: RunResult,
        trace,
        hint: str | None,
    ) -> None:
        failures = 0
        notes: list[str] = []  # the model's own cross-surface memory
        last_seen: dict[str, Observation] = {}  # most recent observation per surface
        last_page_text = ""
        prev_page_text = ""
        for step in range(1, self.settings.max_steps + 1):
            if self.stop_requested:
                result.status, result.summary = "cancelled", "stopped by the user"
                return
            st = time.time()
            focus, obs = self.observe(session)
            bx.page = session.page
            if obs.surface == "browser" and obs.page_text:
                last_page_text = obs.page_text
            if step == 1 and focus.kind != "browser" and hint is None:
                _say(f"warning: step 1 focus is {focus.describe()}, not the browser")

            other = last_seen.get("desktop" if obs.surface == "browser" else "browser")
            changed = _new_lines(prev_page_text, obs.page_text) if obs.surface == "browser" and step > 1 else []
            prompt = self._user_prompt(
                task,
                sites,
                result.steps,
                obs,
                step,
                notes,
                other,
                hint if step == 1 else None,
                downloads_dir=(session.run_dir / "downloads").resolve(),
                downloads=session.downloads,
                changed=changed,
            )
            if obs.surface == "browser":
                prev_page_text = obs.page_text
            last_seen[obs.surface] = obs
            try:
                decision, usage = self.planner.decide(SYSTEM_PROMPT, prompt, obs.screenshot_png)
            except PlannerRefused as e:
                result.status, result.summary = "refused", str(e)
                return
            except Exception as e:  # malformed reply, transient API error: retry the step, give up after a few
                failures += 1
                _say(f"[{step}] planner error ({type(e).__name__}): {str(e)[:160]}")
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    result.status, result.summary = "fail", f"planner failed {failures} times: {str(e)[:200]}"
                    return
                notes.append(
                    f"(system) your previous reply could not be used: {type(e).__name__}: {str(e)[:120]}. Reply with the exact JSON schema."
                )
                continue
            action = decision.action
            if decision.note and decision.note.strip() and decision.note.strip() not in notes:
                notes.append(decision.note.strip())
            _say(f"[{step}] {focus.kind:7s} | {decision.reasoning[:110]}")
            if decision.note:
                _say(f"      note: {decision.note[:100]}")
            _say(f"      -> {action.describe()}")

            if action.type in ("done", "fail"):
                result.status = action.type
                result.summary = action.summary or ""
                rec = StepRecord(
                    step, obs.surface, focus.describe(), decision, ExecResult(ok=True), None, usage, time.time() - st
                )
                result.steps.append(rec)
                self._trace(trace, rec, obs)
                return

            node = obs.by_ref(action.ref) if action.ref else None
            target = node.descriptor() if node else None
            why = needs_confirmation(action, node, obs, self.policy)
            forbidden = self.permissions.check(
                action, node, obs, extra_dirs=[str(session.run_dir / "downloads")], task_sites=sites
            )
            decision_kind = "allowed"
            if forbidden:
                exec_result = ExecResult(ok=False, message=f"blocked by policy: {forbidden}")
                decision_kind = "blocked-policy"
            elif obs.note.startswith("this is a different browser") and action.type in (
                "click",
                "type",
                "press",
                "scroll",
                "fill_credential",
            ):
                exec_result = ExecResult(
                    ok=False,
                    message="refusing to act on another application's browser window; use focus_browser or launch to get to the right window",
                )
                decision_kind = "blocked-foreign-window"
            elif why is not None:
                _say(f"      ?? confirmation required: {why}")
                if confirm(why, self.policy):
                    exec_result = dispatch(action, obs, bx, dx).execute(action, obs)
                    exec_result.message = (exec_result.message + " [approved by user]").strip()
                    decision_kind = "confirmed"
                else:
                    exec_result = ExecResult(ok=False, message=f"blocked by user: {why}")
                    decision_kind = "blocked-user"
            else:
                exec_result = dispatch(action, obs, bx, dx).execute(action, obs)
            self.permissions.audit(
                {
                    "task": task[:80],
                    "step": step,
                    "surface": obs.surface,
                    "window": obs.title[:80],
                    "action": action.model_dump(exclude_none=True),
                    "target": target,
                    "decision": decision_kind,
                    "ok": exec_result.ok,
                    "message": exec_result.message[:200],
                }
            )
            rec = StepRecord(
                step,
                obs.surface,
                focus.describe(),
                decision,
                exec_result,
                target,
                usage,
                time.time() - st,
                window=obs.title,
                context_text=last_page_text,
            )
            result.steps.append(rec)
            self._trace(trace, rec, obs)
            if exec_result.ok:
                failures = 0
                if obs.surface == "desktop" and obs.hwnd:
                    from .actions import owned_hwnds

                    owned_hwnds.add(obs.hwnd)  # the agent worked here: defend this window from now on
                continue
            _say(f"      !! {exec_result.message}")
            if exec_result.message.startswith("blocked by user"):
                result.status, result.summary = "blocked", exec_result.message
                return
            failures += 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                result.status = "fail"
                result.summary = f"{MAX_CONSECUTIVE_FAILURES} consecutive execution failures: {exec_result.message}"
                return

    @staticmethod
    def _trace(fh, rec: StepRecord, obs: Observation) -> None:
        fh.write(
            json.dumps(
                {
                    "step": rec.step,
                    "surface": rec.surface,
                    "focus": rec.focus,
                    "reasoning": rec.decision.reasoning,
                    "note": rec.decision.note,
                    "action": rec.decision.action.model_dump(exclude_none=True),
                    "target": rec.target,
                    "ok": rec.result.ok,
                    "message": rec.result.message,
                    "usage": rec.usage,
                    "seconds": round(rec.seconds, 2),
                    "observation": obs.to_prompt()[:8000],
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        fh.flush()


def _new_lines(before: str, after: str) -> list[str]:
    """Lines present in `after` but not in `before`: what the last action changed on the page."""
    seen = set(before.splitlines())
    return [ln for ln in after.splitlines() if ln.strip() and ln not in seen]


def _pid_of(hwnd: int) -> int:
    import ctypes

    pid = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    return int(pid.value)


def slugify(text: str) -> str:
    slug = re.sub(r"[^\w一-鿿]+", "-", text.lower()).strip("-")
    return slug[:48] or "skill"


DOWNLOADS_PLACEHOLDER = "{DOWNLOADS}"


def _generalize(value: str | None, downloads_dir: Path | None) -> str | None:
    """Replace this run's downloads folder with a placeholder so the skill works in the next run."""
    if not value or downloads_dir is None:
        return value
    for form in (str(downloads_dir), str(downloads_dir).replace("\\", "/"), str(downloads_dir).replace("\\", "\\\\")):
        value = value.replace(form, DOWNLOADS_PLACEHOLDER)
    return value


def distill_steps(records: list[StepRecord], downloads_dir: Path | None = None) -> list[SkillStep]:
    """Successful, non-terminal actions of a trajectory as replayable steps (targets by role+name)."""
    steps: list[SkillStep] = []
    for rec in records:
        a: Action = rec.decision.action
        if a.type in ("done", "fail", "wait") or not rec.result.ok:
            continue
        if a.ref and rec.target is None:
            continue  # we could not describe the target; a replay could not find it either
        template, variables = None, None
        if a.type == "type" and a.text and rec.context_text:
            parsed = parameterize(a.text, rec.context_text)
            if parsed:
                template, variables = parsed[0], variables_to_json(parsed[1])
        steps.append(
            SkillStep(
                action=a.type,
                target=rec.target,
                surface=rec.surface,
                text=_generalize(a.text, downloads_dir),
                keys=a.keys,
                url=_generalize(a.url, downloads_dir),
                command=_generalize(a.command, downloads_dir),
                path=_generalize(a.path, downloads_dir),
                submit=a.submit,
                window=rec.window if rec.surface == "desktop" else None,
                site=a.site,
                template=template,
                variables=variables,
            )
        )
    return steps
