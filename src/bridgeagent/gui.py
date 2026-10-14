"""Minimal desktop window for BridgeAgent: type a task, watch it run, stop it.

Five things and nothing else: task input, progress log, risky-action approval
(the native confirmation box the executor already shows), stop, result. The
agent runs on its own thread, which owns COM and the Playwright session; the
window only sends it a task and receives log lines through a signal.

Run:  bridgeagent gui [--task "..."] [--start-url URL] [--autorun]
"""

from __future__ import annotations

import html
import sys
import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import agent as agent_module
from .agent import Agent
from .config import load_settings
from .llm import make_planner

STYLE = """
QMainWindow, QWidget { background: #0b0d12; color: #e7e9ee; font-family: 'Segoe UI Variable', 'Segoe UI'; font-size: 13px; }
QLabel { background: transparent; }
QLabel#title { font-size: 22px; font-weight: 600; color: #ffffff; }
QLabel#subtitle { color: #8a93a6; font-size: 13px; }
QLabel#meta { color: #6b7386; font-size: 11px; }
QLabel#section { color: #6b7386; font-size: 11px; font-weight: 600; letter-spacing: 1.5px; }
QFrame#card { background: #12151c; border: 1px solid #1f2430; border-radius: 14px; }
QPlainTextEdit, QLineEdit { background: #0f1218; border: 1px solid #262c3a; border-radius: 10px; padding: 9px 11px;
                            selection-background-color: #3b5bdb; }
QPlainTextEdit:focus, QLineEdit:focus { border-color: #5b7cfa; }
QTextEdit#log { background: #0f1218; border: 1px solid #262c3a; border-radius: 10px; padding: 10px;
                font-family: 'Cascadia Mono', Consolas, monospace; font-size: 12px; color: #c9d1d9; }
QPushButton { background: #1a1f2b; border: 1px solid #2a3040; border-radius: 10px; padding: 8px 20px; color: #e7e9ee; }
QPushButton:hover { background: #222838; }
QPushButton:disabled { color: #4f566a; border-color: #1f2430; }
QPushButton#run { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #5b7cfa, stop:1 #7c5cff);
                  border: none; color: white; font-weight: 600; padding: 9px 26px; }
QPushButton#run:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #6b8afc, stop:1 #8a6dff); }
QPushButton#run:disabled { background: #232a3d; color: #6b7386; }
QPushButton#stop:enabled { color: #ff8787; border-color: #4a2323; }
QScrollBar:vertical { background: transparent; width: 8px; }
QScrollBar::handle:vertical { background: #2a3040; border-radius: 4px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""
STATUS = {
    "idle": ("#8a93a6", "#262c3a"),
    "running": ("#ffd166", "#4d4120"),
    "done": ("#6ee7a0", "#1f4d35"),
    "failed": ("#ff6b6b", "#4d2323"),
}
LINE_COLORS = (
    ("-> ok", "#6ee7a0"),
    ("[verify]", "#8ab4ff"),
    ("[heal]", "#ffd166"),
    ("!!", "#ff6b6b"),
    ("FAILED", "#ff6b6b"),
)


class _Bus(QObject):
    line = Signal(str)
    finished = Signal(str, str)  # status, summary


class Window(QMainWindow):
    def __init__(self, task: str = "", start_url: str = "", autorun: bool = False, quit_when_done: bool = False):
        super().__init__()
        self.setWindowTitle("BridgeAgent")
        self.resize(780, 660)
        self.bus = _Bus()
        self.bus.line.connect(self._append)
        self.bus.finished.connect(self._done)
        self.agent: Agent | None = None
        self.quit_when_done = quit_when_done
        self._build(task, start_url)
        self._tray()
        if autorun and task:
            self.start()

    # -- layout -----------------------------------------------------------

    def _build(self, task: str, start_url: str) -> None:
        self.task = QPlainTextEdit(task)
        self.task.setPlaceholderText(
            "What should the agent do? e.g. Open the invoices folder in Explorer and upload the newest one to the form."
        )
        self.task.setFixedHeight(80)
        self.url = QLineEdit(start_url)
        self.url.setPlaceholderText("Start URL (optional)")
        self.run_btn = QPushButton("Run")
        self.run_btn.setObjectName("run")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("stop")
        self.stop_btn.setEnabled(False)
        self.status = QLabel()
        self.log = QTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.run_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(self.stop)
        self._set_status("idle", "idle")

        settings = load_settings()
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(self._label("BridgeAgent", "title"))
        titles.addWidget(
            self._label("One task across the browser and Windows. Risky actions ask you first.", "subtitle")
        )
        header.addLayout(titles, 1)
        header.addWidget(
            self._label(f"{settings.provider} / {settings.model}   confirm: {settings.confirm_mode}", "meta")
        )

        task_card = self._card()
        task_card.layout().addWidget(self._label("TASK", "section"))
        task_card.layout().addWidget(self.task)
        task_card.layout().addWidget(self.url)
        row = QHBoxLayout()
        row.addWidget(self.run_btn)
        row.addWidget(self.stop_btn)
        row.addStretch(1)
        row.addWidget(self.status)
        task_card.layout().addLayout(row)

        log_card = self._card()
        log_card.layout().addWidget(self._label("PROGRESS", "section"))
        log_card.layout().addWidget(self.log, 1)

        layout = QVBoxLayout()
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)
        layout.addLayout(header)
        layout.addWidget(task_card)
        layout.addWidget(log_card, 1)
        root = QWidget()
        root.setLayout(layout)
        self.setCentralWidget(root)
        self.setStyleSheet(STYLE)

    @staticmethod
    def _label(text: str, role: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName(role)
        return label

    @staticmethod
    def _card() -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        inner = QVBoxLayout(card)
        inner.setContentsMargins(16, 14, 16, 14)
        inner.setSpacing(10)
        return card

    def _set_status(self, text: str, state: str) -> None:
        color, border = STATUS[state]
        self.status.setText(f"●  {text}")
        self.status.setStyleSheet(
            f"color: {color}; border: 1px solid {border}; border-radius: 12px; padding: 5px 12px; font-weight: 600;"
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
                    f"\n{result.mode}, {len(result.steps)} LLM steps, {result.llm_calls} calls, {result.seconds:.0f}s"
                )
            except Exception as e:  # noqa: BLE001
                status, summary = "error", f"{type(e).__name__}: {e}"
        self.bus.finished.emit(status, summary)

    def stop(self) -> None:
        if self.agent is not None:
            self.agent.stop_requested = True
            self._set_status("stopping after this step", "running")

    def _append(self, line: str) -> None:
        color = next((c for marker, c in LINE_COLORS if marker in line), None)
        text = html.escape(line).replace("  ", "&nbsp;&nbsp;")
        self.log.append(f'<span style="color:{color or "#c9d1d9"}">{text}</span>')

    def _done(self, status: str, summary: str) -> None:
        self._set_status(status, "done" if status == "done" else "failed")
        color = STATUS["done" if status == "done" else "failed"][0]
        self.log.append(f'<br><b style="color:{color}">{status}</b>')
        for line in summary.splitlines():
            self.log.append(f"<span>{html.escape(line)}</span>")
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
