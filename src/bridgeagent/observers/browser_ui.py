"""Dialogs Chrome draws inside its own window, outside the page DOM.

Print preview, permission bubbles, "Open <app>?" prompts, passkey / Windows
Hello sheets and similar UI live in the browser window but are invisible to
the page's DOM, and `document.hasFocus()` keeps returning true while they are
open. In the UI Automation tree they show up as a bubble/dialog frame under
the browser root, so we look for those frames and expose their controls with
d-refs next to the page's b-refs.

The walk skips the page's own DocumentControl (the DOM observer already covers
it) and only descends into dialog subtrees, so it stays cheap on heavy pages.
"""

from __future__ import annotations

from ..focus import CHROMIUM_WINDOW_CLASS
from ..tree import UINode

try:
    import uiautomation as auto
except ImportError:  # pragma: no cover
    auto = None

DIALOG_FRAME_CLASSES = {
    "BubbleFrameView",
    "BubbleDialogDelegateView",
    "ConstrainedWindowFrameView",
    "DialogClientView",
    "DialogDelegateView",
}
MAX_SEARCH_DEPTH = 9


def _dialog_roots(root) -> list[tuple[str, object]]:
    """(name, control) for each dialog frame found under the browser root."""
    found: list[tuple[str, object]] = []
    skip_until: int | None = None
    last_named: dict[int, str] = {}
    for ctrl, depth in auto.WalkControl(root, includeTop=False, maxDepth=MAX_SEARCH_DEPTH):
        if skip_until is not None:
            if depth > skip_until:
                continue
            skip_until = None
        try:
            type_name, cls, name = ctrl.ControlTypeName, ctrl.ClassName, ctrl.Name or ""
        except Exception:
            continue
        if name:
            last_named[depth] = name
        if type_name == "DocumentControl":  # the page itself; the DOM observer has it
            skip_until = depth
            continue
        if cls in DIALOG_FRAME_CLASSES:
            title = next((last_named[d] for d in range(depth - 1, -1, -1) if d in last_named), "") or "dialog"
            found.append((title, ctrl))
            skip_until = depth  # do not look for nested frames
    return found


def observe_browser_dialogs(max_nodes: int = 80) -> tuple[list[UINode], str]:
    """Return (nodes, title) for the first dialog open inside the foreground Chrome window, or ([], '')."""
    if auto is None:
        return [], ""
    from .desktop import _walk

    root = auto.GetForegroundControl()
    try:
        if root.ClassName != CHROMIUM_WINDOW_CLASS:
            return [], ""
    except Exception:
        return [], ""
    roots = _dialog_roots(root)
    if not roots:
        return [], ""
    title, frame = roots[0]
    obs = _walk(max_depth=14, max_nodes=max_nodes, max_text=20, root=frame)
    nodes = [n for n in obs.nodes if n.role != "window"]
    return nodes, title
