"""Minimal desktop window for BridgeAgent: type a task, watch it run, stop it.

Five things and nothing else: task input, progress log, risky-action approval
(the native confirmation box the executor already shows), stop, result. The
agent runs on its own thread, which owns COM and the Playwright session; the
window only sends it a task and receives log lines through a signal.

Run:  bridgeagent gui [--task "..."] [--start-url URL] [--autorun]
"""

from __future__ import annotations

import sys
import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from . import agent as agent_module
from .agent import Agent
from .config import load_settings
from .llm import make_planner

STYLE = """
QMainWindow, QWidget { background: #0f1115; color: #e6e6e6; font-family: 'Segoe UI'; font-size: 13px; }
QLabel#title { font-size: 20px; font-weight: 600; color: #ffffff; }
QLabel#subtitle { color: #8b93a3; }
QLabel#section { color: #8b93a3; font-size: 11px; letter-spacing: 1px; }
QLabel#status { color: #8b93a3; padding: 4px 10px; border: 1px solid #2a2f3a; border-radius: 10px; }
QPlainTextEdit, QLineEdit { background: #161a22; border: 1px solid #2a2f3a; border-radius: 8px; padding: 8px; selection-background-color: #3b5bdb; }
QPlainTextEdit:focus, QLineEdit:focus { border-color: #4c6ef5; }
QPlainTextEdit#log { font-family: 'Cascadia Mono', Consolas, monospace; font-size: 12px; color: #c9d1d9; }
QPushButton { background: #1c2230; border: 1px solid #2a2f3a; border-radius: 8px; padding: 7px 18px; color: #e6e6e6; }
QPushButton:hover { background: #242b3b; }
QPushButton:disabled { color: #5c6475; }
QPushButton#run { background: #4c6ef5; border-color: #4c6ef5; color: white; font-weight: 600; }
QPushButton#run:hover { background: #5c7cfa; }
QPushButton#run:disabled { background: #2b3550; border-color: #2b3550; color: #7f8aa5; }
QPushButton#stop:enabled { color: #ff8787; border-color: #5a1f1f; }
"""


class _Bus(QObject):
    line = Signal(str)
    finished = Signal(str, str)  # status, summary


class Window(QMainWindow):
    def __init__(self, task: str = "", start_url: str = "", autorun: bool = False, quit_when_done: bool = False):
        super().__init__()
        self.setWindowTitle("BridgeAgent")
        self.resize(760, 640)
        self.bus = _Bus()
        self.bus.line.connect(self._append)
        self.bus.finished.connect(self._done)
        self.agent: Agent | None = None
        self.quit_when_done = quit_when_done

        self.task = QPlainTextEdit(task)
        self.task.setPlaceholderText(
            "What should the agent do? e.g. Open the invoices folder in Explorer and upload the newest one to the form."
        )
        self.task.setFixedHeight(76)
        self.url = QLineEdit(start_url)
        self.url.setPlaceholderText("Start URL (optional)")
        self.run_btn = QPushButton("Run")
        self.run_btn.setObjectName("run")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("stop")
        self.stop_btn.setEnabled(False)
        self.status = QLabel("idle")
        self.status.setObjectName("status")
        self.log = QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.run_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(self.stop)

        title = QLabel("BridgeAgent")
        title.setObjectName("title")
        subtitle = QLabel("One task across the browser and Windows. Risky actions ask you first.")
        subtitle.setObjectName("subtitle")
        row = QHBoxLayout()
        row.addWidget(self.run_btn)
        row.addWidget(self.stop_btn)
        row.addStretch(1)
        row.addWidget(self.status)
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(8)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(8)
        layout.addWidget(self._section("TASK"))
        layout.addWidget(self.task)
        layout.addWidget(self.url)
        layout.addLayout(row)
        layout.addSpacing(6)
        layout.addWidget(self._section("PROGRESS"))
        layout.addWidget(self.log, 1)
        root = QWidget()
        root.setLayout(layout)
        self.setCentralWidget(root)
        self.setStyleSheet(STYLE)
        self._tray()
        if autorun and task:
            self.start()

    @staticmethod
    def _section(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("section")
        return label

    STATUS_COLORS = {
        "idle": ("#8b93a3", "#2a2f3a"),
        "running": ("#ffd166", "#5a4b1a"),
        "done": ("#6ee7a0", "#1f5a3a"),
        "failed": ("#ff6b6b", "#5a1f1f"),
    }

    def _set_status(self, text: str, state: str) -> None:
        color, border = self.STATUS_COLORS.get(state, self.STATUS_COLORS["idle"])
        self.status.setText(text)
        self.status.setStyleSheet(
            f"color: {color}; border: 1px solid {border}; border-radius: 10px; padding: 4px 10px;"
        )

    def _tray(self) -> None:
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.setWindowIcon(icon)
        tray = QSystemTrayIcon(QIcon(icon), self)
        menu = QMenu()
        menu.addAction("Show", self.showNormal)
        menu.addAction("Quit", QApplication.instance().quit)
        tray.setContextMenu(menu)
        tray.activated.connect(
            lambda reason: self.showNormal() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        tray.show()
        self.tray = tray

    # -- running ----------------------------------------------------------

    def start(self) -> None:
        task = self.task.toPlainText().strip()
        if not task or self.agent is not None:
            return
        self.log.clear()
        self._set_status("running", "running")
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        settings = load_settings()
        self.agent = Agent(settings, make_planner(settings))
        agent_module.LOG_SINK = self.bus.line.emit
        threading.Thread(target=self._work, args=(task, self.url.text().strip() or None), daemon=True).start()

    def _work(self, task: str, start_url: str | None) -> None:
        import uiautomation as auto

        with auto.UIAutomationInitializerInThread():
            try:
                result = self.agent.run(task, start_url=start_url)  # type: ignore[union-attr]
                status, summary = result.status, result.summary or ""
                summary += (
                    f"\n({result.mode}, {len(result.steps)} LLM steps, {result.llm_calls} calls, {result.seconds:.0f}s)"
                )
            except Exception as e:  # noqa: BLE001
                status, summary = "error", f"{type(e).__name__}: {e}"
        self.bus.finished.emit(status, summary)

    def stop(self) -> None:
        if self.agent is not None:
            self.agent.stop_requested = True
            self._set_status("stopping after this step", "running")

    def _append(self, line: str) -> None:
        self.log.appendPlainText(line)

    def _done(self, status: str, summary: str) -> None:
        self._set_status(status, "done" if status == "done" else "failed")
        self.log.appendPlainText(f"\n=== {status} ===\n{summary}")
        self.agent = None
        agent_module.LOG_SINK = None
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        if self.quit_when_done:
            QApplication.instance().quit()

    def closeEvent(self, event) -> None:  # closing the window keeps the tray icon; Quit is in its menu
        if self.tray.isVisible():
            self.hide()
            event.ignore()
        else:
            event.accept()


def main(task: str = "", start_url: str = "", autorun: bool = False, quit_when_done: bool = False) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    win = Window(task, start_url, autorun, quit_when_done)
    win.show()
    return app.exec()
