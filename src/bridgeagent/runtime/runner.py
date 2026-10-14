"""Windows toast notification, used by the CLI and by the eval harness to simulate a focus interrupt."""

from __future__ import annotations

import subprocess
import sys


def notify(title: str, body: str) -> None:
    if sys.platform != "win32":
        return
    title, body = title.replace("'", "''"), body.replace("'", "''")
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null\n"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)\n"
        "$n = $t.GetElementsByTagName('text')\n"
        f"$n.Item(0).AppendChild($t.CreateTextNode('{title}')) | Out-Null\n"
        f"$n.Item(1).AppendChild($t.CreateTextNode('{body}')) | Out-Null\n"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('BridgeAgent').Show([Windows.UI.Notifications.ToastNotification]::new($t))\n"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, timeout=10
        )
    except Exception:
        pass
