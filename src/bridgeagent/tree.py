"""Unified UI tree: the one representation the LLM sees for both surfaces.

Browser nodes come from a DOM scan (observers/browser.py); desktop nodes come
from the Windows UI Automation tree (observers/desktop.py). Both are reduced to
the same `UINode` shape, so the planner prompt, the action schema and the
skill format do not care which side of the browser boundary they are on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Surface = Literal["browser", "desktop"]

# Roles the agent can act on. Everything else is context only.
INTERACTIVE_ROLES = {
    "button",
    "link",
    "textbox",
    "searchbox",
    "combobox",
    "checkbox",
    "radio",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "listitem",
    "option",
    "treeitem",
    "tab",
    "slider",
    "spinbutton",
    "switch",
    "dataitem",
    "cell",
    "document",  # editable text areas (Notepad, Word, rich editors) expose this instead of textbox
    "fileinput",  # <input type=file>; use the upload action with a local path
}

MAX_NAME_LEN = 120


@dataclass
class UINode:
    ref: str  # "b12" (browser) or "d7" (desktop)
    role: str  # normalized role, see INTERACTIVE_ROLES
    name: str  # accessible name, trimmed
    surface: Surface
    bbox: tuple[int, int, int, int] | None = None  # left, top, right, bottom
    value: str | None = None
    states: list[str] = field(default_factory=list)  # focused, checked, disabled, expanded, selected
    depth: int = 0
    handle: Any = None  # backend object (CSS selector / UIA control); never serialized

    @property
    def interactive(self) -> bool:
        return self.role in INTERACTIVE_ROLES

    def to_line(self) -> str:
        parts = [f"[{self.ref}]", self.role]
        if self.name:
            parts.append(f'"{self.name[:MAX_NAME_LEN]}"')
        if self.value:
            parts.append(f"value={self.value[:60]!r}")
        if self.states:
            parts.append("(" + ", ".join(self.states) + ")")
        indent = " " * min(self.depth, 6)
        return indent + " ".join(parts)

    def descriptor(self) -> dict[str, str]:
        """Backend-independent identity used by skills to re-find this node later."""
        return {"role": self.role, "name": self.name[:MAX_NAME_LEN]}


@dataclass
class Observation:
    surface: Surface
    title: str
    detail: str  # URL for browser, "process.exe / window class" for desktop
    nodes: list[UINode]
    screenshot_png: bytes | None = None
    note: str = ""
    page_text: str = ""  # visible text excerpt (browser only), for reading tasks
    hwnd: int = 0  # foreground window handle at observation time (executors verify it before sending keys)
    pid: int = 0

    def by_ref(self, ref: str) -> UINode | None:
        for n in self.nodes:
            if n.ref == ref:
                return n
        return None

    def interactive_count(self) -> int:
        return sum(1 for n in self.nodes if n.interactive)

    def compact(self, max_nodes: int = 12, max_text: int = 600) -> str:
        """A few lines: title, the first interactive element names, a text excerpt."""
        lines = [f"{self.surface}: {self.title} ({self.detail})"]
        names = [
            f'{n.role} "{n.name}"' + (f" = {n.value[:60]!r}" if n.value else "")
            for n in self.nodes
            if n.interactive and n.name
        ][:max_nodes]
        if names:
            lines.append("elements: " + "; ".join(names))
        values = [f"{n.name[:40]} = {n.value[:60]!r}" for n in self.nodes if n.value and n.name][:6]
        if values and len(names) >= max_nodes:  # make sure field values survive the cut
            lines.append("values: " + "; ".join(values))
        texts = [n.name for n in self.nodes if n.role == "text" and n.name][:8]
        if texts:
            lines.append("labels: " + " | ".join(t[:60] for t in texts))
        if self.page_text:
            lines.append("text: " + self.page_text[:max_text].replace("\n", " "))
        return "\n".join(lines)

    def to_prompt(self, max_nodes: int = 160) -> str:
        head = [f"SURFACE: {self.surface}", f"TITLE: {self.title}", f"DETAIL: {self.detail}"]
        if self.note:
            head.append(f"NOTE: {self.note}")
        shown = self.nodes[:max_nodes]
        lines = [n.to_line() for n in shown]
        if len(self.nodes) > max_nodes:
            lines.append(f"... {len(self.nodes) - max_nodes} more nodes omitted")
        if not lines:
            lines.append("(no accessible nodes found)")
        out = "\n".join(head) + "\nELEMENTS:\n" + "\n".join(lines)
        if self.page_text:
            out += "\nVISIBLE TEXT (truncated):\n" + self.page_text
        return out
