"""Action schema (what the planner may emit) and the two executors.

The planner never sees a backend: it emits click [d7] or type [b3] "hello",
and the executor resolves the ref against the last observation. Credentials
are the one deliberate exception to "the LLM drives everything": a
fill_credential action names a site and two refs, and the executor pulls the
secret from the OS keyring itself, so plaintext never enters a prompt.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .tree import Observation, UINode

ActionType = Literal[
    "click",
    "type",
    "press",
    "scroll",
    "navigate",
    "launch",
    "upload",
    "focus_browser",
    "fill_credential",
    "wait",
    "done",
    "fail",
]


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ActionType
    ref: str | None = Field(
        default=None,
        description="Element ref like b12 or d7 (click/type/scroll; username field for fill_credential). type without a ref sends the keystrokes to whatever currently has keyboard focus.",
    )
    text: str | None = Field(default=None, description="Text to type, e.g. a whole expression like 1234+5678=")
    submit: bool = Field(default=False, description="Press Enter after typing")
    keys: str | None = Field(
        default=None, description="Key chord in Playwright syntax, e.g. Enter, Escape, Control+S, Alt+F4"
    )
    direction: Literal["up", "down"] | None = None
    url: str | None = None
    command: str | None = Field(default=None, description="File path or program to open with the OS (launch)")
    path: str | None = Field(
        default=None, description="Local file to attach to a file input (upload); needs ref of the file input"
    )
    site: str | None = Field(
        default=None,
        description="Credential site key (fill_credential). One fill_credential action fills BOTH the username and the password field; the password itself is never shown to you.",
    )
    password_ref: str | None = Field(
        default=None,
        description="Password field ref (fill_credential); if omitted, the field labelled password is used",
    )
    summary: str | None = Field(default=None, description="Final answer / result (done) or reason (fail)")

    def describe(self) -> str:
        bits = [self.type]
        for k in ("ref", "text", "keys", "direction", "url", "command", "path", "site", "summary"):
            v = getattr(self, k)
            if v:
                bits.append(f"{k}={str(v)[:60]!r}")
        if self.submit:
            bits.append("submit")
        return " ".join(bits)


class ExecResult(BaseModel):
    ok: bool
    message: str = ""


# --- key chord translation -------------------------------------------------

_MODIFIERS = {
    "control": "{Ctrl}",
    "ctrl": "{Ctrl}",
    "alt": "{Alt}",
    "shift": "{Shift}",
    "meta": "{Win}",
    "win": "{Win}",
}
_SPECIAL = {
    "enter": "{Enter}",
    "escape": "{Esc}",
    "esc": "{Esc}",
    "tab": "{Tab}",
    "backspace": "{Back}",
    "delete": "{Del}",
    "space": "{Space}",
    "arrowdown": "{Down}",
    "arrowup": "{Up}",
    "arrowleft": "{Left}",
    "arrowright": "{Right}",
    "home": "{Home}",
    "end": "{End}",
    "pageup": "{PageUp}",
    "pagedown": "{PageDown}",
    "insert": "{Insert}",
}


def is_browser_level_chord(chord: str) -> bool:
    """Chords Chrome handles in its own UI (Ctrl+S, Ctrl+P, F12, Alt+F4 ...).

    Playwright's keyboard goes to the renderer over CDP and never reaches the
    browser frame, so these must be sent at OS level to the foreground window.
    """
    parts = [p.strip().lower() for p in chord.split("+") if p.strip()]
    if any(p in ("control", "ctrl", "alt", "meta", "win") for p in parts):
        return True
    return any(p.startswith("f") and p[1:].isdigit() for p in parts)


def playwright_chord_to_sendkeys(chord: str) -> str:
    """Translate a Playwright chord (Control+Shift+S, Enter, F5) into uiautomation SendKeys syntax."""
    parts = [p.strip() for p in chord.split("+") if p.strip()]
    out = []
    for p in parts:
        low = p.lower()
        if low in _MODIFIERS:
            out.append(_MODIFIERS[low])
        elif low in _SPECIAL:
            out.append(_SPECIAL[low])
        elif low.startswith("f") and low[1:].isdigit():
            out.append("{" + p.upper() + "}")
        elif len(p) == 1:
            out.append(p.lower() if p.isalpha() else p)
        else:
            out.append("{" + p + "}")
    return "".join(out)


# --- shared helpers --------------------------------------------------------

_launched: dict[str, int] = {}  # command -> hwnd of the window it produced, for this process
owned_hwnds: set[int] = set()  # windows this agent opened or worked in; the focus guard defends them


def _launch(command: str) -> ExecResult:
    from .focus import activate_new_window, all_top_windows, bring_to_front

    key = command.strip().lower()
    hwnd = _launched.get(key)
    if hwnd and hwnd in all_top_windows():
        bring_to_front(hwnd)
        return ExecResult(
            ok=True, message=f"{command} is already open; brought its window to the front instead of launching again"
        )
    before = all_top_windows()
    try:
        if os.path.exists(command.strip('"')):
            os.startfile(command.strip('"'))  # a file or folder: open with its default app  # type: ignore[attr-defined]
        else:
            # `start` resolves App Paths (excel, winword, chrome ...) and URIs (ms-settings:about), unlike a bare Popen
            subprocess.Popen(f'start "" {command}', shell=True)
    except Exception as e:
        return ExecResult(ok=False, message=f"launch failed: {e}")
    hwnd = activate_new_window(before)
    if hwnd is None:
        # Single-instance apps (Settings, Calculator sometimes) only re-focus their existing window:
        # adopt whatever came to the front so the focus guard does not push it away again.
        from .focus import get_focus

        f = get_focus()
        if f.hwnd and f.hwnd not in owned_hwnds and f.kind != "browser":
            _launched[key] = f.hwnd
            owned_hwnds.add(f.hwnd)
            return ExecResult(
                ok=True, message=f"launched {command}; an existing window '{f.title[:40]}' came to the front"
            )
        return ExecResult(ok=True, message=f"launched {command}, but no new window appeared within the timeout")
    _launched[key] = hwnd
    owned_hwnds.add(hwnd)
    return ExecResult(ok=True, message=f"launched {command} and brought its window to the front")


def _need(obs: Observation, ref: str | None) -> UINode:
    if not ref:
        raise ValueError("this action needs a ref")
    node = obs.by_ref(ref)
    if node is None:
        raise ValueError(f"ref {ref} not in the current observation")
    return node


def _credential_secret(site: str) -> tuple[str, str]:
    from .credentials.broker import CredentialBroker

    cred = CredentialBroker().get(site)
    if cred is None:
        raise ValueError(f"no credential stored for site {site!r}; run: bridgeagent cred set {site}")
    return cred


_PASSWORD_WORDS = ("password", "passwd", "pass", "密码", "口令")
_USERNAME_WORDS = ("user", "email", "e-mail", "login", "account", "账号", "用户名", "邮箱", "学号", "id")


def _credential_fields(obs: Observation, action: Action) -> tuple[UINode, UINode]:
    """Resolve the username and password fields, falling back to name heuristics.

    Models often pass only `ref` or neither; a login form almost always labels
    its fields, so guessing from role+name is safer than failing the step.
    """
    boxes = [n for n in obs.nodes if n.role in ("textbox", "searchbox")]
    pwd = obs.by_ref(action.password_ref) if action.password_ref else None
    if pwd is None:
        pwd = next((n for n in boxes if any(w in n.name.lower() for w in _PASSWORD_WORDS)), None)
    user = obs.by_ref(action.ref) if action.ref else None
    if user is pwd:
        user = None
    if user is None:
        user = next((n for n in boxes if n is not pwd and any(w in n.name.lower() for w in _USERNAME_WORDS)), None)
    if user is None and pwd is not None:
        others = [n for n in boxes if n is not pwd]
        user = others[0] if len(others) == 1 else None
    if user is None or pwd is None:
        raise ValueError("could not identify both username and password fields; pass ref (username) and password_ref")
    return user, pwd


def dispatch(action: Action, obs: Observation, browser_exec: BrowserExecutor, desktop_exec: DesktopExecutor):
    """Pick the executor by the ref's prefix (b = page DOM, d = UIA), else by the observed surface.

    An observation taken on the browser surface may also carry d-refs for a
    dialog Chrome draws outside the page DOM, so the ref decides, not the surface.
    """
    ref = action.ref or (action.password_ref if action.type == "fill_credential" else None)
    if ref:
        return desktop_exec if ref.startswith("d") else browser_exec
    if action.type == "navigate":
        return browser_exec
    return browser_exec if obs.surface == "browser" else desktop_exec


# --- browser executor ------------------------------------------------------


def _activate_or_tab(page, activate: Callable[[], bool] | None) -> ExecResult:
    if activate is not None and activate():
        return ExecResult(ok=True)
    if page is None:
        why = "could not bring the browser window to the front" if activate else "no browser attached"
        return ExecResult(ok=False, message=why)
    page.bring_to_front()
    return ExecResult(ok=True, message="switched tab; window may still be behind another app")


class BrowserExecutor:
    def __init__(self, page, activate: Callable[[], bool] | None = None):
        self.page = page
        self.activate = activate

    def _loc(self, obs: Observation, ref: str | None):
        node = _need(obs, ref)
        return self.page.locator(node.handle).first

    def execute(self, action: Action, obs: Observation) -> ExecResult:
        p = self.page
        t = action.type
        try:
            if t == "click":
                self._loc(obs, action.ref).click(timeout=8000)
            elif t == "type":
                if action.ref:
                    loc = self._loc(obs, action.ref)
                    loc.click(timeout=8000)
                    loc.fill(action.text or "", timeout=8000)
                    if action.submit:
                        loc.press("Enter")
                else:
                    p.keyboard.type(action.text or "")
                    if action.submit:
                        p.keyboard.press("Enter")
            elif t == "press":
                keys = action.keys or "Enter"
                if is_browser_level_chord(keys):
                    import uiautomation as auto

                    if self.activate is not None and not self.activate():
                        return ExecResult(
                            ok=False, message="could not bring the browser window to the front; keys not sent"
                        )
                    auto.SendKeys(playwright_chord_to_sendkeys(keys))
                else:
                    p.keyboard.press(keys)
            elif t == "scroll":
                if action.ref:
                    self._loc(obs, action.ref).hover(timeout=5000)
                p.mouse.wheel(0, -600 if action.direction == "up" else 600)
            elif t == "navigate":
                p.goto(action.url or "about:blank", wait_until="domcontentloaded", timeout=30000)
            elif t == "launch":
                return _launch(action.command or "")
            elif t == "upload":
                if not action.path or not os.path.isfile(action.path):
                    return ExecResult(ok=False, message=f"upload needs an existing local file; got {action.path!r}")
                self._loc(obs, action.ref).set_input_files(action.path, timeout=8000)
            elif t == "focus_browser":
                return _activate_or_tab(p, self.activate)
            elif t == "fill_credential":
                user, pwd = _credential_secret(action.site or "")
                user_node, pwd_node = _credential_fields(obs, action)
                p.locator(user_node.handle).first.fill(user, timeout=8000)
                p.locator(pwd_node.handle).first.fill(pwd, timeout=8000)
                if action.submit:
                    p.locator(pwd_node.handle).first.press("Enter")
                time.sleep(0.6)
                return ExecResult(
                    ok=True,
                    message=f'filled username into [{user_node.ref}] "{user_node.name}" and password into [{pwd_node.ref}] "{pwd_node.name}"',
                )
            elif t == "wait":
                time.sleep(1.5)
            else:
                return ExecResult(ok=False, message=f"unsupported on browser: {t}")
            try:
                p.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            time.sleep(0.6)
            return ExecResult(ok=True)
        except Exception as e:
            return ExecResult(ok=False, message=f"{type(e).__name__}: {str(e)[:200]}")


# --- desktop executor ------------------------------------------------------


def sendkeys_literal(text: str) -> str:
    """Escape text so uiautomation.SendKeys types it verbatim (braces and parens are special there)."""
    return text.replace("{", "\x00").replace("}", "{}}").replace("\x00", "{{}").replace("(", "{(}").replace(")", "{)}")


def _pattern(ctrl, name: str):
    """Pattern lookup that works on every control class (TextControl lacks the typed getters)."""
    import uiautomation as auto

    try:
        return ctrl.GetPattern(getattr(auto.PatternId, name))
    except Exception:
        return None


class DesktopExecutor:
    def __init__(self, activate: Callable[[], bool] | None = None):
        self.activate = activate  # so focus_browser works from the desktop side
        self._obs: Observation | None = None

    def _click(self, node: UINode) -> None:
        ctrl = node.handle
        if node.role == "checkbox":
            tp = _pattern(ctrl, "TogglePattern")
            if tp is not None:
                tp.Toggle()
                return
        ip = _pattern(ctrl, "InvokePattern")
        if ip is not None:
            ip.Invoke()
            return
        sp = _pattern(ctrl, "SelectionItemPattern")
        if sp is not None and node.role in {"listitem", "tab", "treeitem", "radio", "option"}:
            sp.Select()
            return
        # No pattern: fall back to a real mouse click at the element's rectangle. That only hits the
        # right thing if the observed window is still in front, so re-verify the window first.
        if self._obs is not None:
            blocked = self._ensure_front(self._obs)
            if blocked:
                raise RuntimeError(blocked.message)
        ctrl.Click(simulateMove=False, waitTime=0.2)

    def _type(self, node: UINode | None, text: str, submit: bool) -> None:
        import uiautomation as auto

        if node is None:  # no ref: send keystrokes to whatever has keyboard focus
            auto.SendKeys(sendkeys_literal(text), interval=0.02)
        else:
            ctrl = node.handle
            try:
                ctrl.SetFocus()
            except Exception:
                ctrl.Click(simulateMove=False, waitTime=0.2)
            vp = _pattern(ctrl, "ValuePattern")
            if vp is not None:
                try:
                    vp.SetValue(text)
                except Exception:
                    ctrl.SendKeys(sendkeys_literal(text), interval=0.02)
            else:
                ctrl.SendKeys(sendkeys_literal(text), interval=0.02)
        if submit:
            auto.SendKeys("{Enter}")

    @staticmethod
    def _ensure_front(obs: Observation) -> ExecResult | None:
        """Keystrokes go to whatever is in front: make sure that is still the observed window."""
        import ctypes

        from .focus import bring_to_front, get_focus

        if not obs.hwnd:
            return None
        if get_focus().hwnd == obs.hwnd:
            return None
        if not ctypes.windll.user32.IsWindow(ctypes.c_void_p(obs.hwnd)):
            return ExecResult(
                ok=False,
                message=f"the observed window '{obs.title[:40]}' no longer exists; keys not sent. Observe again.",
            )
        bring_to_front(obs.hwnd)
        f = get_focus()
        if f.hwnd == obs.hwnd:
            return None
        return ExecResult(
            ok=False,
            message=f"focus was taken by {f.process} '{f.title[:40]}' since the observation; keys not sent. Observe again.",
        )

    def execute(self, action: Action, obs: Observation) -> ExecResult:
        import uiautomation as auto

        t = action.type
        self._obs = obs
        try:
            if t == "click":
                self._click(_need(obs, action.ref))
            elif t == "type":
                node = _need(obs, action.ref) if action.ref else None
                if node is None:
                    blocked = self._ensure_front(obs)
                    if blocked:
                        return blocked
                self._type(node, action.text or "", action.submit)
            elif t == "press":
                keys = action.keys or "Enter"
                if "+" not in keys and len(keys) > 1 and not keys[0].isalpha():
                    return ExecResult(
                        ok=False,
                        message="press takes ONE key or chord (e.g. Enter, Control+S); to enter a string use type without a ref",
                    )
                blocked = self._ensure_front(obs)
                if blocked:
                    return blocked
                auto.SendKeys(playwright_chord_to_sendkeys(keys))
            elif t == "scroll":
                times = 3
                if action.ref:
                    node = _need(obs, action.ref)
                    (node.handle.WheelUp if action.direction == "up" else node.handle.WheelDown)(wheelTimes=times)
                else:
                    (auto.WheelUp if action.direction == "up" else auto.WheelDown)(wheelTimes=times)
            elif t == "launch":
                return _launch(action.command or "")
            elif t == "focus_browser":
                return _activate_or_tab(None, self.activate)
            elif t == "fill_credential":
                user, pwd = _credential_secret(action.site or "")
                user_node, pwd_node = _credential_fields(obs, action)
                self._type(user_node, user, False)
                self._type(pwd_node, pwd, action.submit)
                return ExecResult(
                    ok=True,
                    message=f'filled username into [{user_node.ref}] "{user_node.name}" and password into [{pwd_node.ref}] "{pwd_node.name}"',
                )
            elif t == "wait":
                time.sleep(1.5)
            elif t in ("navigate", "upload"):
                return ExecResult(ok=False, message=f"{t} is browser-only; use focus_browser first")
            else:
                return ExecResult(ok=False, message=f"unsupported on desktop: {t}")
            time.sleep(0.8)
            return ExecResult(ok=True)
        except Exception as e:
            return ExecResult(ok=False, message=f"{type(e).__name__}: {str(e)[:200]}")
