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


class _Bus(QObject):
    line = Signal(str)
    finished = Signal(str, str)  # status, summary


class Window(QMainWindow):
    def __init__(self, task: str = "", start_url: str = "", autorun: bool = False, quit_when_done: bool = False):
        super().__init__()
        self.setWindowTitle("BridgeAgent")
        self.resize(720, 520)
        self.bus = _Bus()
        self.bus.line.connect(self._append)
        self.bus.finished.connect(self._done)
        self.agent: Agent | None = None
        self.quit_when_done = quit_when_done

        self.task = QPlainTextEdit(task)
        self.task.setPlaceholderText(
            "What should the agent do? e.g. Open the invoices folder in Explorer, upload the newest one to the form."
        )
        self.task.setFixedHeight(70)
        self.url = QLineEdit(start_url)
        self.url.setPlaceholderText("Start URL (optional)")
        self.run_btn = QPushButton("Run")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.status = QLabel("idle")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.run_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(self.stop)

        row = QHBoxLayout()
        row.addWidget(self.run_btn)
        row.addWidget(self.stop_btn)
        row.addWidget(self.status, 1)
        layout = QVBoxLayout()
        for w in (QLabel("Task"), self.task, self.url):
            layout.addWidget(w)
        layout.addLayout(row)
        layout.addWidget(QLabel("Progress (risky actions pop up a confirmation box)"))
        layout.addWidget(self.log, 1)
        root = QWidget()
        root.setLayout(layout)
        self.setCentralWidget(root)
        self._tray()
        if autorun and task:
            self.start()

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
        self.status.setText("running")
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
            self.status.setText("stopping after the current step...")

    def _append(self, line: str) -> None:
        self.log.appendPlainText(line)

    def _done(self, status: str, summary: str) -> None:
        self.status.setText(status)
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
