"""Confirmation gate for irreversible actions, enforced by the executor, not the model.

The planner is asked to be careful, but nothing stops a model from clicking
"Send". This module decides, from the action and the element it targets,
whether a human has to approve, and asks through a native Windows message box
that times out to "No". Policy comes from BRIDGE_CONFIRM:

    never  - trust the model (demo only)
    risky  - default: clicks on send/pay/delete-like controls, Enter-submits
    always - every state-changing action
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .actions import Action
from .tree import Observation, UINode

RISKY_WORDS_EN = {
    "send",
    "submit",
    "pay",
    "purchase",
    "buy",
    "checkout",
    "place order",
    "order",
    "delete",
    "remove",
    "confirm",
    "transfer",
    "post",
    "publish",
    "apply",
    "unsubscribe",
    "cancel subscription",
    "sign out",
    "log out",
    "format",
    "reset",
    "uninstall",
    "accept",
    "agree",
    "approve",
    "withdraw",
    "donate",
}
RISKY_WORDS_ZH = (
    "发送",
    "提交",
    "支付",
    "付款",
    "购买",
    "下单",
    "结算",
    "删除",
    "移除",
    "确认",
    "转账",
    "发布",
    "申请",
    "退订",
    "注销",
    "退出登录",
    "格式化",
    "重置",
    "卸载",
    "同意",
    "批准",
    "提现",
    "捐赠",
)
_EN_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in sorted(RISKY_WORDS_EN, key=len, reverse=True)) + r")\b", re.IGNORECASE
)
SAFE_ACTIONS = {"done", "fail", "wait", "focus_browser", "scroll", "navigate"}

IDYES, IDNO, MB_TIMEDOUT = 6, 7, 32000


@dataclass(frozen=True)
class ConfirmPolicy:
    mode: str = "risky"  # never | risky | always
    timeout_s: int = 60


def is_risky_name(name: str) -> bool:
    if not name:
        return False
    low = name.lower()
    return bool(_EN_PATTERN.search(low)) or any(w in name for w in RISKY_WORDS_ZH)


def needs_confirmation(action: Action, node: UINode | None, obs: Observation, policy: ConfirmPolicy) -> str | None:
    """Return a human-readable description if this action must be approved first, else None."""
    if policy.mode == "never" or action.type in SAFE_ACTIONS:
        return None
    where = f'{obs.surface} window "{obs.title[:60]}"'
    if policy.mode == "always":
        return f"{action.describe()} on {where}"
    if action.type == "click" and node is not None and is_risky_name(node.name):
        return f'click {node.role} "{node.name[:60]}" on {where}'
    if action.type in ("type", "fill_credential") and action.submit and node is not None and is_risky_name(node.name):
        return f'press Enter to submit {node.role} "{node.name[:60]}" on {where}'
    if action.type == "press" and (action.keys or "").lower() in ("enter", "control+enter"):
        focused = next((n for n in obs.nodes if "focused" in n.states), None)
        if focused is not None and is_risky_name(focused.name):
            return f'press {action.keys} while {focused.role} "{focused.name[:60]}" is focused on {where}'
    if action.type == "launch" and re.search(r"\b(format|rmdir|del|rm|shutdown|reg)\b", (action.command or "").lower()):
        return f"run command {action.command!r}"
    return None


@dataclass
class Permissions:
    """What the agent may touch, declared by the user, enforced by the executors.

    Loaded from a YAML file (see permissions.example.yaml). Empty lists mean
    "no restriction" for that axis, so a missing file is fully permissive
    apart from the confirmation gate.
    """

    allowed_apps: list[str] = field(default_factory=list)  # launch targets (basename, without .exe) or URI schemes
    denied_apps: list[str] = field(default_factory=list)
    allowed_dirs: list[str] = field(
        default_factory=list
    )  # paths the agent may open; the run's downloads dir is always allowed
    denied_windows: list[str] = field(default_factory=list)  # regexes on window titles / process names: never act there
    denied_url_patterns: list[str] = field(default_factory=list)
    sites: list[str] = field(default_factory=list)  # credential site keys usable without --site
    confirm: str | None = None  # never | risky | always, overrides env
    audit_log: str | None = "runs/audit.jsonl"
    source: str = "(defaults)"

    @classmethod
    def load(cls, path: str | Path | None) -> Permissions:
        if path is None:
            return cls()
        p = Path(path)
        if not p.exists():
            return cls()
        import yaml

        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

        def names(items) -> list[str]:
            # "ms-settings:" unquoted parses as a one-key mapping in YAML; accept both spellings
            out: list[str] = []
            for x in items or []:
                if isinstance(x, dict):
                    out.extend(f"{k}:".lower() for k in x)
                else:
                    out.append(str(x).lower())
            return out

        perms = cls(
            allowed_apps=names(data.get("allowed_apps")),
            denied_apps=names(data.get("denied_apps")),
            allowed_dirs=[str(x) for x in data.get("allowed_dirs") or []],
            denied_windows=[str(x) for x in data.get("denied_windows") or []],
            denied_url_patterns=[str(x) for x in data.get("denied_url_patterns") or []],
            sites=[str(x) for x in data.get("sites") or []],
            confirm=data.get("confirm"),
            audit_log=data.get("audit_log", "runs/audit.jsonl"),
            source=str(p),
        )
        return perms

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _app_key(command: str) -> str:
        cmd = command.strip().strip('"')
        m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]+):", cmd)  # URI scheme such as ms-settings: (not a drive letter)
        if m and not os.path.exists(cmd):
            return m.group(1).lower() + ":"
        first = cmd.split()[0] if cmd.split() else cmd
        base = os.path.basename(first.strip('"')).lower()
        return base.removesuffix(".exe")

    def _dir_allowed(self, path: str, extra_dirs: list[str]) -> bool:
        if not self.allowed_dirs:
            return True
        target = os.path.normcase(os.path.abspath(path.strip('"')))
        for d in list(self.allowed_dirs) + extra_dirs:
            root = os.path.normcase(os.path.abspath(d))
            if target == root or target.startswith(root.rstrip("\\/") + os.sep):
                return True
        return False

    def window_denied(self, obs: Observation) -> str | None:
        hay = f"{obs.title} {obs.detail}"
        for pat in self.denied_windows:
            if re.search(pat, hay, re.IGNORECASE):
                return pat
        return None

    # -- the check ----------------------------------------------------------

    def check(
        self,
        action: Action,
        node: UINode | None,
        obs: Observation,
        extra_dirs: list[str] | None = None,
        task_sites: list[str] | None = None,
    ) -> str | None:
        """Return a reason if the action is forbidden by this manifest, else None."""
        extra_dirs = extra_dirs or []
        t = action.type
        if t in ("click", "type", "press", "scroll", "fill_credential"):
            pat = self.window_denied(obs)
            if pat:
                return f'policy forbids acting in window "{obs.title[:50]}" (matches denied_windows {pat!r})'
        if t == "launch":
            cmd = action.command or ""
            key = self._app_key(cmd)
            if key in self.denied_apps:
                return f"policy forbids launching {key!r}"
            is_program = cmd.strip('"').lower().endswith((".exe", ".bat", ".cmd", ".ps1"))
            if os.path.exists(cmd.strip('"')) and not is_program:
                if not self._dir_allowed(cmd, extra_dirs):
                    return f"policy forbids opening {cmd!r}: outside allowed_dirs"
            elif self.allowed_apps and key not in self.allowed_apps:
                return f"policy forbids launching {key!r}: not in allowed_apps {self.allowed_apps}"
        if t == "upload" and action.path and not self._dir_allowed(action.path, extra_dirs):
            return f"policy forbids uploading {action.path!r}: outside allowed_dirs"
        if t == "navigate":
            url = action.url or ""
            for pat in self.denied_url_patterns:
                if re.search(pat, url, re.IGNORECASE):
                    return f"policy forbids navigating to {url!r} (matches {pat!r})"
            if url.lower().startswith("file:") and self.allowed_dirs:
                local = url[5:].lstrip("/").replace("/", os.sep)
                if not self._dir_allowed(local, extra_dirs):
                    return f"policy forbids opening local file {url!r}: outside allowed_dirs"
        if t == "fill_credential":
            allowed = set(self.sites) | set(task_sites or [])
            if allowed and (action.site or "") not in allowed:
                return f"policy forbids using credential site {action.site!r}; allowed: {sorted(allowed)}"
        return None

    def audit(self, event: dict) -> None:
        if not self.audit_log:
            return
        try:
            p = Path(self.audit_log)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **event}, ensure_ascii=False) + "\n")
        except Exception:
            pass


def confirm(description: str, policy: ConfirmPolicy) -> bool:
    """Native Yes/No box, default No, auto-denies after the timeout. Non-Windows: deny."""
    if sys.platform != "win32":
        return False
    MB_YESNO, MB_ICONWARNING, MB_DEFBUTTON2 = 0x4, 0x30, 0x100
    MB_TOPMOST, MB_SETFOREGROUND = 0x40000, 0x10000
    text = (
        f"BridgeAgent wants to:\n\n{description}\n\n"
        f"Allow this action? (No is chosen automatically after {policy.timeout_s}s)"
    )
    user32 = ctypes.windll.user32
    user32.MessageBoxTimeoutW.restype = ctypes.c_int
    user32.MessageBoxTimeoutW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.c_ushort,
        ctypes.c_ulong,
    ]
    rc = user32.MessageBoxTimeoutW(
        None,
        text,
        "BridgeAgent: confirm action",
        MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_TOPMOST | MB_SETFOREGROUND,
        0,
        int(policy.timeout_s * 1000),
    )
    return rc == IDYES
