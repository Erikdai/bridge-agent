"""BridgeAgent as an MCP server: the execution layer for any agent that speaks MCP.

Claude Code, Cursor, browser-use or a custom client gets the same tools the
built-in agent uses: observe the unified tree, click/type/press by ref, launch
apps, fill credentials from the OS keyring, replay learned skills. The
permission manifest, the confirmation gate, the focus guard and the audit log
all apply exactly as they do for the built-in agent, because the tools go
through the same executors.

Run:  bridgeagent mcp [--start-url URL]        (stdio transport)

Every action tool returns the result line followed by a fresh observation, so
a client needs one round trip per step.
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .actions import Action, BrowserExecutor, DesktopExecutor, ExecResult, dispatch
from .agent import DOWNLOADS_PLACEHOLDER, Agent, BrowserSession
from .config import Settings, load_settings
from .guard import confirm, needs_confirmation
from .skills.replay import replay, step_desc
from .tree import Observation

mcp = MCPServer(
    "bridgeagent",
    instructions=(
        "Tools to operate a Windows PC across the browser boundary. Call observe first; it returns one "
        "accessibility tree for whatever has keyboard focus (web page with b-refs, or native window with "
        "d-refs). Act by ref. Values read on one surface are not visible on the other: keep your own notes. "
        "Risky actions may require the human's confirmation; policy may block apps, folders or windows."
    ),
)


class _State:
    settings: Settings | None = None
    session: BrowserSession | None = None
    agent: Agent | None = None
    bx: BrowserExecutor | None = None
    dx: DesktopExecutor | None = None
    last_obs: Observation | None = None
    run_dir: Path | None = None


_S = _State()


class _Worker(threading.Thread):
    """One thread owns COM (UI Automation) and the Playwright session.

    MCP runs synchronous tool functions on a thread pool; UIA needs COM
    initialised per thread and Playwright's sync objects must stay on the
    thread that created them, so every tool body is executed here.
    """

    def __init__(self) -> None:
        super().__init__(daemon=True, name="bridgeagent-worker")
        self.q: queue.Queue = queue.Queue()

    def run(self) -> None:
        import uiautomation as auto

        with auto.UIAutomationInitializerInThread():
            while True:
                fn, args, kwargs, done, box = self.q.get()
                try:
                    box["result"] = fn(*args, **kwargs)
                except BaseException as e:  # noqa: BLE001 - re-raised on the caller's thread
                    box["error"] = e
                done.set()

    def call(self, fn, *args, **kwargs):
        if not self.is_alive():
            self.start()
        done, box = threading.Event(), {}
        self.q.put((fn, args, kwargs, done, box))
        done.wait()
        if "error" in box:
            raise box["error"]
        return box["result"]


_W = _Worker()


class _NoPlanner:  # the MCP client is the planner; only skill verification would need one
    model = "mcp-client"

    def decide(self, *a, **k):
        raise RuntimeError("no LLM configured inside the MCP server; the client decides")

    def ask(self, *a, **k):
        raise RuntimeError("no LLM configured inside the MCP server")


def _ensure(start_url: str | None = None) -> None:
    if _S.session is not None:
        return
    _S.settings = load_settings()
    _S.run_dir = Path(_S.settings.runs_dir) / ("mcp-" + time.strftime("%Y%m%d-%H%M%S"))
    _S.run_dir.mkdir(parents=True, exist_ok=True)
    _S.agent = Agent(_S.settings, _NoPlanner())
    _S.session = BrowserSession(_S.settings, _S.run_dir, start_url or None).__enter__()
    _S.bx = BrowserExecutor(_S.session.page, activate=_S.session.activate)
    _S.dx = DesktopExecutor(activate=_S.session.activate)


def _observe() -> str:
    assert _S.agent and _S.session
    _, obs = _S.agent.observe(_S.session)
    _S.last_obs = obs
    extra = ""
    if _S.session.downloads:
        extra = "\nDOWNLOADED FILES:\n" + "\n".join(f"- {p}" for p in _S.session.downloads[-5:])
    return obs.to_prompt() + extra


def _run(action: Action) -> str:
    return _W.call(_run_now, action)


def _run_now(action: Action) -> str:
    """Permission check, confirmation gate, dispatch, audit; then a fresh observation."""
    _ensure()
    assert _S.agent and _S.session and _S.bx and _S.dx
    obs = _S.last_obs
    if obs is None:
        _observe()
        obs = _S.last_obs
    assert obs is not None
    node = obs.by_ref(action.ref) if action.ref else None
    forbidden = _S.agent.permissions.check(action, node, obs, extra_dirs=[str(_S.run_dir / "downloads")])
    if forbidden:
        result, decision = ExecResult(ok=False, message=f"blocked by policy: {forbidden}"), "blocked-policy"
    elif obs.note.startswith("this is a different browser") and action.type in (
        "click",
        "type",
        "press",
        "scroll",
        "fill_credential",
    ):
        result, decision = (
            ExecResult(ok=False, message="refusing to act on another application's browser window"),
            "blocked-foreign-window",
        )
    else:
        why = needs_confirmation(action, node, obs, _S.agent.policy)
        if why is not None and not confirm(why, _S.agent.policy):
            result, decision = ExecResult(ok=False, message=f"blocked by user: {why}"), "blocked-user"
        else:
            _S.bx.page = _S.session.page
            result = dispatch(action, obs, _S.bx, _S.dx).execute(action, obs)
            decision = "confirmed" if why else "allowed"
    _S.agent.permissions.audit(
        {
            "client": "mcp",
            "surface": obs.surface,
            "window": obs.title[:80],
            "action": action.model_dump(exclude_none=True),
            "target": node.descriptor() if node else None,
            "decision": decision,
            "ok": result.ok,
            "message": result.message[:200],
        }
    )
    status = "ok" if result.ok else "ERROR"
    line = f"RESULT: {status} {action.describe()}" + (f" :: {result.message}" if result.message else "")
    return line + "\n\n" + _observe()


@mcp.tool()
def observe(start_url: str = "") -> str:
    """Return the accessibility tree of whatever has keyboard focus. On the first call, optionally open start_url in the browser."""

    def body() -> str:
        _ensure(start_url or None)
        if start_url and _S.session and _S.session.page.url != start_url:
            _S.session.page.goto(start_url, wait_until="domcontentloaded")
            _S.session.activate()
        return _observe()

    return _W.call(body)


@mcp.tool()
def click(ref: str) -> str:
    """Click the element with this ref (b12 = page element, d7 = native/dialog element)."""
    return _run(Action(type="click", ref=ref))


@mcp.tool()
def type_text(text: str, ref: str = "", submit: bool = False) -> str:
    """Type text into the element with ref, or, with no ref, send the keystrokes to the focused window (whole expressions, file names)."""
    return _run(Action(type="type", ref=ref or None, text=text, submit=submit))


@mcp.tool()
def press(keys: str) -> str:
    """Press one key or chord in Playwright syntax: Enter, Escape, Control+S, Alt+F4, F2."""
    return _run(Action(type="press", keys=keys))


@mcp.tool()
def scroll(direction: str = "down", ref: str = "") -> str:
    """Scroll up or down, optionally over the element with ref."""
    return _run(Action(type="scroll", direction=direction, ref=ref or None))  # type: ignore[arg-type]


@mcp.tool()
def navigate(url: str) -> str:
    """Open a URL in the agent's browser tab."""
    return _run(Action(type="navigate", url=url))


@mcp.tool()
def launch(command: str) -> str:
    """Open an app, file, folder or URI (calc, notepad, excel, a path, ms-settings:about) and bring its window to the front."""
    return _run(Action(type="launch", command=command))


@mcp.tool()
def focus_browser() -> str:
    """Bring the agent's browser window back to the front."""
    return _run(Action(type="focus_browser"))


@mcp.tool()
def fill_credential(site: str, ref: str = "", password_ref: str = "", submit: bool = False) -> str:
    """Fill username and password for a site key stored in the OS keyring. The secret never passes through the model."""
    return _run(
        Action(type="fill_credential", site=site, ref=ref or None, password_ref=password_ref or None, submit=submit)
    )


@mcp.tool()
def list_downloads() -> str:
    """Full paths of files downloaded in this session."""
    _ensure()
    assert _S.session
    return "\n".join(str(p) for p in _S.session.downloads) or "(no downloads yet)"


@mcp.tool()
def list_skills() -> str:
    """Learned skills that can be replayed without an LLM."""
    _ensure()
    assert _S.agent
    rows = [
        f"{s.name}: {s.description} ({len(s.steps)} steps, {s.successes}/{s.runs} ok)" for s in _S.agent.store.all()
    ]
    return "\n".join(rows) or "(no skills)"


@mcp.tool()
def replay_skill(name: str, start_url: str = "") -> str:
    """Replay a learned skill step by step without an LLM; returns the trail and where it stopped, if it did."""
    return _W.call(_replay_now, name, start_url)


def _replay_now(name: str, start_url: str = "") -> str:
    _ensure(start_url or None)
    assert _S.agent and _S.session and _S.bx and _S.dx
    if start_url:
        _S.session.page.goto(start_url, wait_until="domcontentloaded")
        _S.session.activate()
    skill = _S.agent.store.load(name)
    rr = replay(
        skill,
        lambda: _S.agent.observe(_S.session),
        _S.bx,
        _S.dx,  # type: ignore[union-attr]
        substitutions={DOWNLOADS_PLACEHOLDER: str((_S.run_dir / "downloads").resolve())},
    )  # type: ignore[operator]
    head = (
        "REPLAY: all steps ran"
        if rr.ok
        else f"REPLAY: stopped at step {rr.failed_at + 1} ({step_desc(skill.steps[rr.failed_at])}): {rr.error}"
    )  # type: ignore[index,operator]
    return head + "\n" + "\n".join(rr.trail) + "\n\n" + _observe()


def main(start_url: str | None = None) -> None:
    import os

    os.environ["BRIDGE_LOG_STDERR"] = "1"  # stdout belongs to the MCP transport
    if start_url:
        _W.call(_ensure, start_url)
    mcp.run(transport="stdio")
