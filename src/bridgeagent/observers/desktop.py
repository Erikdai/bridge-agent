"""Windows UI Automation -> UINode list.

Walks the foreground window's UIA tree, keeps interactive controls and short
text labels, and assigns stable refs ("d1", "d2", ...) for this observation.
The UIA control object is kept in `handle` so the executor can act on it
without re-walking the tree.
"""

from __future__ import annotations

import os
import tempfile

import psutil

from ..tree import INTERACTIVE_ROLES, Observation, UINode

try:  # imported lazily on non-Windows so the rest of the package still imports
    import uiautomation as auto
except ImportError:  # pragma: no cover
    auto = None

ROLE_MAP = {
    "ButtonControl": "button",
    "SplitButtonControl": "button",
    "HyperlinkControl": "link",
    "EditControl": "textbox",
    "DocumentControl": "document",
    "ComboBoxControl": "combobox",
    "CheckBoxControl": "checkbox",
    "RadioButtonControl": "radio",
    "MenuItemControl": "menuitem",
    "ListItemControl": "listitem",
    "TreeItemControl": "treeitem",
    "TabItemControl": "tab",
    "SliderControl": "slider",
    "SpinnerControl": "spinbutton",
    "DataItemControl": "dataitem",
    "HeaderItemControl": "columnheader",
    "TextControl": "text",
    "ImageControl": "image",
    "WindowControl": "window",
    "PaneControl": "pane",
    "GroupControl": "group",
    "MenuControl": "menu",
    "MenuBarControl": "menubar",
    "ToolBarControl": "toolbar",
    "TitleBarControl": "titlebar",
    "ListControl": "list",
    "TreeControl": "tree",
    "TabControl": "tablist",
    "TableControl": "table",
    "DataGridControl": "grid",
    "StatusBarControl": "status",
    "ProgressBarControl": "progressbar",
    "ScrollBarControl": "scrollbar",
    "CustomControl": "custom",
}
CONTEXT_ROLES = {"text", "window", "tab", "columnheader"}  # kept even though not interactive
SKIP_SUBTREES = {"ScrollBarControl", "TitleBarControl"}


def _pattern(ctrl, name: str):
    try:
        return ctrl.GetPattern(getattr(auto.PatternId, name))
    except Exception:
        return None


def _states(ctrl) -> list[str]:
    states: list[str] = []
    try:
        if not ctrl.IsEnabled:
            states.append("disabled")
    except Exception:
        pass
    try:
        if ctrl.HasKeyboardFocus:
            states.append("focused")
    except Exception:
        pass
    tp = _pattern(ctrl, "TogglePattern")
    if tp is not None and getattr(tp, "ToggleState", 0) == 1:
        states.append("checked")
    sp = _pattern(ctrl, "SelectionItemPattern")
    if sp is not None and getattr(sp, "IsSelected", False):
        states.append("selected")
    ep = _pattern(ctrl, "ExpandCollapsePattern")
    if ep is not None and getattr(ep, "ExpandCollapseState", 0) == 1:
        states.append("expanded")
    return states


def _value(ctrl, role: str) -> str | None:
    if role not in {"textbox", "combobox", "spinbutton", "slider", "document"}:
        return None
    vp = _pattern(ctrl, "ValuePattern")
    if vp is not None:
        v = getattr(vp, "Value", None)
        if isinstance(v, str) and v:
            return v[:200]
    return None


def _screenshot(root) -> bytes | None:
    try:
        bmp = root.ToBitmap()
        if bmp is None:
            return None
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        bmp.ToFile(path)
        with open(path, "rb") as f:
            data = f.read()
        os.remove(path)
        return data
    except Exception:
        return None


def observe_desktop(
    max_depth: int = 16, max_nodes: int = 250, max_text: int = 60, with_screenshot: bool = False, _retry: bool = True
) -> Observation:
    if auto is None:
        raise RuntimeError("uiautomation is only available on Windows")
    obs = _walk(max_depth, max_nodes, max_text)
    if _retry and len(obs.nodes) <= 1:
        # Chromium and some Electron apps build their accessibility tree lazily,
        # on the first UIA client query; a second walk a moment later is usually full.
        import time

        time.sleep(0.6)
        obs = _walk(max_depth, max_nodes, max_text)
    if with_screenshot:
        obs.screenshot_png = _screenshot(auto.GetForegroundControl())
        if obs.screenshot_png:
            obs.note = "screenshot attached because the accessibility tree is sparse"
    return obs


def _walk(max_depth: int, max_nodes: int, max_text: int, root=None, ref_prefix: str = "d") -> Observation:
    root = root if root is not None else auto.GetForegroundControl()
    try:
        process = psutil.Process(root.ProcessId).name()
    except Exception:
        process = "?"
    title = root.Name or ""
    nodes: list[UINode] = []
    text_count = 0
    skip_until_depth: int | None = None
    role_at_depth: dict[int, str] = {}  # pre-order walk: the entry at depth-1 is the parent's role

    walker = auto.WalkControl(root, includeTop=True, maxDepth=max_depth)
    while True:
        try:
            ctrl, depth = next(walker)
        except StopIteration:
            break
        except Exception:  # COMError: an element disappeared mid-walk (a closing bubble); keep what we have
            break
        if skip_until_depth is not None:
            if depth > skip_until_depth:
                continue
            skip_until_depth = None
        try:
            type_name = ctrl.ControlTypeName
        except Exception:
            continue
        if type_name in SKIP_SUBTREES:
            skip_until_depth = depth
            continue
        role = ROLE_MAP.get(type_name, "custom")
        role_at_depth[depth] = role
        if role not in INTERACTIVE_ROLES and role not in CONTEXT_ROLES:
            continue
        if role == "text" and role_at_depth.get(depth - 1) in INTERACTIVE_ROLES:
            continue  # a button's caption: the button itself is the thing to click
        try:
            if ctrl.IsOffscreen:
                continue
            rect = ctrl.BoundingRectangle
            bbox = (rect.left, rect.top, rect.right, rect.bottom)
            if rect.right - rect.left <= 0 or rect.bottom - rect.top <= 0:
                continue
            name = (ctrl.Name or "").strip()
        except Exception:
            continue
        if role == "text":
            if not name or text_count >= max_text:
                continue
            text_count += 1
        if role == "window" and depth > 0:
            role = "pane"
            continue
        nodes.append(
            UINode(
                ref=f"{ref_prefix}{len(nodes) + 1}",
                role=role,
                name=name,
                surface="desktop",
                bbox=bbox,
                value=_value(ctrl, role),
                states=_states(ctrl),
                depth=depth,
                handle=ctrl,
            )
        )
        if len(nodes) >= max_nodes:
            break

    try:
        cls = root.ClassName
    except Exception:
        cls = ""
    try:
        hwnd, pid = int(root.NativeWindowHandle or 0), int(root.ProcessId or 0)
    except Exception:
        hwnd, pid = 0, 0
    return Observation(surface="desktop", title=title, detail=f"{process} / {cls}", nodes=nodes, hwnd=hwnd, pid=pid)
