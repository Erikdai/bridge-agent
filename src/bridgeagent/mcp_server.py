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
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

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


Ref = Annotated[
    str,
    Field(
        description="Element ref from the latest observation, e.g. 'b12' for a page element or 'd7' for a native window "
        "or dialog control. Refs are renumbered on every observation, so always use the most recent tree."
    ),
]


@mcp.tool()
def observe(
    start_url: Annotated[
        str,
        Field(
            description="Optional URL to open in the agent's browser before observing, e.g. 'https://example.com' or a "
            "file:// page. Leave empty to observe the current state."
        ),
    ] = "",
) -> str:
    """Read the current screen as one accessibility tree and return it as text.

    Call this first, and again whenever you need fresh refs. The result starts with a SURFACE line:
    'browser' means the tree is the web page (refs b1, b2, ...); 'desktop' means it is the native window that has
    keyboard focus such as Explorer, Calculator, a Save dialog or a Windows prompt (refs d1, d2, ...). A Chrome dialog
    drawn on top of the page (print preview, permission bubble) is included with d refs. Each line shows the ref, role,
    accessible name, value and state of an element, followed by a visible-text excerpt and the files downloaded so far.
    Nothing is clicked or changed.
    """

    def body() -> str:
        _ensure(start_url or None)
        if start_url and _S.session and _S.session.page.url != start_url:
            _S.session.page.goto(start_url, wait_until="domcontentloaded")
            _S.session.activate()
        return _observe()

    return _W.call(body)


@mcp.tool()
def click(ref: Ref) -> str:
    """Click one element by ref: a page element through the DOM, or a native control through UI Automation.

    Use for buttons, links, checkboxes, menu items, list items and tabs. Clicks on controls whose name suggests an
    irreversible effect (Send, Pay, Delete, Submit, ...) first show the human a native confirmation box and are refused
    if it is declined or times out. Returns a RESULT line (ok or ERROR with the reason) followed by a fresh observation,
    so you do not need a separate observe call.
    """
    return _run(Action(type="click", ref=ref))


@mcp.tool()
def type_text(
    text: Annotated[
        str,
        Field(
            description="The exact text to enter, e.g. 'report-2026.csv' or a whole calculator expression such as "
            "'1234+5678='."
        ),
    ],
    ref: Annotated[
        str,
        Field(
            description="Ref of the text field to fill (it is cleared first). Leave empty to send the keystrokes to "
            "whatever currently has keyboard focus, which is right for calculators, address bars and dialogs that "
            "already have focus."
        ),
    ] = "",
    submit: Annotated[
        bool, Field(description="Press Enter after typing, e.g. to submit a search box or confirm a file name.")
    ] = False,
) -> str:
    """Enter text into a field by ref, or send keystrokes to the focused window when no ref is given.

    Keystrokes are only sent if the window observed last is still in front; if focus was stolen in between, the call
    fails with an explanation instead of typing into the wrong window. Never use this for passwords: use
    fill_credential, which keeps the secret out of the conversation. Returns a RESULT line and a fresh observation.
    """
    return _run(Action(type="type", ref=ref or None, text=text, submit=submit))


@mcp.tool()
def press(
    keys: Annotated[
        str,
        Field(
            description="One key or chord in Playwright syntax: 'Enter', 'Escape', 'Tab', 'F2', 'Control+S', 'Alt+F4', "
            "'Control+Shift+S'. Not a string of characters; use type_text for that."
        ),
    ],
) -> str:
    """Press a single key or key chord in the focused window.

    Chords with Control, Alt or Windows modifiers and function keys are sent at operating-system level so they reach
    browser shortcuts and native apps; plain keys go to the page when the browser is in front. Pressing Enter while a
    risky control is focused asks the human first. Returns a RESULT line and a fresh observation.
    """
    return _run(Action(type="press", keys=keys))


@mcp.tool()
def scroll(
    direction: Annotated[str, Field(description="'down' or 'up'.")] = "down",
    ref: Annotated[
        str,
        Field(description="Optional ref of a scrollable element to scroll over; defaults to the whole page or window."),
    ] = "",
) -> str:
    """Scroll the page or the focused window to reveal more content, then return a fresh observation.

    Use when the element you need is not in the tree yet; the browser observer only lists elements inside the viewport.
    """
    return _run(Action(type="scroll", direction=direction, ref=ref or None))  # type: ignore[arg-type]


@mcp.tool()
def navigate(
    url: Annotated[
        str,
        Field(description="Absolute URL to open, e.g. 'https://example.com/login' or 'file:///C:/path/page.html'."),
    ],
) -> str:
    """Open a URL in the agent's own browser tab and return the page tree once it has loaded.

    Browser-only: if a native window is in front, the browser is brought back first. URLs matching the user's denied
    patterns are refused by policy. Returns a RESULT line and a fresh observation.
    """
    return _run(Action(type="navigate", url=url))


@mcp.tool()
def launch(
    command: Annotated[
        str,
        Field(
            description="What to open: a program name ('calc', 'notepad', 'excel'), a file or folder path "
            "('C:\\Users\\me\\Downloads\\report.csv'), or a URI ('ms-settings:about'). Program names are resolved "
            "like the Windows Run box."
        ),
    ],
) -> str:
    """Start a Windows app, open a file or folder with its default app, or open a URI, and bring that window to the front.

    This is how a task leaves the browser: after launch, observe shows the native window with d refs. Launching
    something that is already open only re-focuses its window. Apps, folders and files outside the user's permission
    manifest are refused. Returns a RESULT line and a fresh observation of the new window.
    """
    return _run(Action(type="launch", command=command))


@mcp.tool()
def focus_browser() -> str:
    """Bring the agent's browser window back to the front and return the page tree.

    Use after working in a native app when the next step is on the web page again, or when observe reports that
    another application's window is in front.
    """
    return _run(Action(type="focus_browser"))


@mcp.tool()
def fill_credential(
    site: Annotated[
        str,
        Field(
            description="Site key under which the user stored the credential with 'bridgeagent cred set <site>', e.g. "
            "'company-portal'. Only keys allowed by the task or the permission manifest are accepted."
        ),
    ],
    ref: Annotated[
        str,
        Field(
            description="Ref of the username or email field. Optional: when empty, the field labelled user, email, "
            "login or account is used."
        ),
    ] = "",
    password_ref: Annotated[
        str, Field(description="Ref of the password field. Optional: when empty, the field labelled password is used.")
    ] = "",
    submit: Annotated[
        bool, Field(description="Press Enter in the password field afterwards to submit the login form.")
    ] = False,
) -> str:
    """Fill a login form with a username and password that live in the operating-system keyring.

    The secret is read by the executor and typed directly; it is never returned to you and never appears in the
    observation. Use this instead of type_text for any password field. Works on web forms and on native login dialogs.
    Returns a RESULT line naming the two fields that were filled, plus a fresh observation.
    """
    return _run(
        Action(type="fill_credential", site=site, ref=ref or None, password_ref=password_ref or None, submit=submit)
    )


@mcp.tool()
def list_downloads() -> str:
    """List the full local paths of files the browser downloaded in this session, newest last.

    Use the returned path with launch to open a downloaded file in its desktop app, or to tell the user where it is.
    Returns '(no downloads yet)' when nothing has been downloaded.
    """

    def body() -> str:
        _ensure()
        assert _S.session
        return "\n".join(str(p) for p in _S.session.downloads) or "(no downloads yet)"

    return _W.call(body)


@mcp.tool()
def list_skills() -> str:
    """List the skills learned from earlier successful runs: name, task description, step count and success record.

    A skill is a recorded cross-environment procedure that replay_skill can execute without a model. Check this before
    planning a task from scratch; if a listed description matches the task, replay it.
    """

    def body() -> str:
        _ensure()
        assert _S.agent
        rows = [
            f"{sk.name}: {sk.description} ({len(sk.steps)} steps, {sk.successes}/{sk.runs} ok)"
            for sk in _S.agent.store.all()
        ]
        return "\n".join(rows) or "(no skills)"

    return _W.call(body)


@mcp.tool()
def replay_skill(
    name: Annotated[str, Field(description="Skill name exactly as printed by list_skills.")],
    start_url: Annotated[
        str, Field(description="Optional URL to open before replaying, when the skill starts on a web page.")
    ] = "",
) -> str:
    """Execute a learned skill step by step without a model and report what happened.

    Each step re-finds its target by role and name on the current screen; values the skill learned from a page are
    re-read from the current page. The first line says whether all steps ran or at which step replay stopped and why,
    followed by a short trail of what was seen after each step and a fresh observation. If replay stopped, continue the
    task from the current state with the other tools.
    """
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
