"""Native Qt inbox. The window never fetches recommendations or autoplays a queue."""
import datetime as dt
import shutil
import sys

from PySide6.QtCore import Qt, QProcess, QThread, QTime, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QIcon, QKeySequence, QShortcut
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox,
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QTimeEdit, QVBoxLayout, QWidget)

import core


class CheckWorker(QThread):
    completed = Signal(object)

    def __init__(self, paths, parent=None):
        super().__init__(parent)
        self.paths = paths

    def run(self):
        try:
            self.completed.emit(core.run_check(self.paths))
        except Exception as exc:
            self.completed.emit({"new": 0, "checked": 0, "errors": [str(exc)]})


class ChannelsDialog(QDialog):
    def __init__(self, paths, parent=None):
        super().__init__(parent)
        self.paths = paths
        self.setWindowTitle("Your channels")
        self.resize(650, 430)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("One channel link or @handle per line. Lines starting with # are comments."))
        self.editor = QPlainTextEdit(paths.channels.read_text(encoding="utf-8"))
        self.editor.setPlaceholderText("https://www.youtube.com/@TechnologyConnections\n@YourOtherChannel")
        layout.addWidget(self.editor)
        note = QLabel("Tracking begins with the first check after you add a channel. Older uploads stay out of the inbox.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.error = QLabel("")
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def save(self):
        text = self.editor.toPlainText()
        _, errors = core.parse_channels(text)
        if errors:
            self.error.setText("\n".join(errors))
            return
        try:
            core.atomic_write(self.paths.channels, text.rstrip() + "\n")
        except OSError as exc:
            self.error.setText(str(exc))
            return
        self.accept()


class SettingsDialog(QDialog):
    def __init__(self, paths, parent=None):
        super().__init__(parent)
        self.paths = paths
        self.setWindowTitle("Settings")
        layout = QFormLayout(self)
        current = core.settings(paths)
        self.daily = QTimeEdit(QTime.fromString(current["daily_time"], "HH:mm"))
        self.daily.setDisplayFormat("HH:mm")
        layout.addRow("Daily check (local time)", self.daily)
        self.player = QComboBox()
        self.player.addItem("mpv — play only this video", "mpv")
        self.player.addItem("Default web browser", "browser")
        self.player.setCurrentIndex(0 if current["player"] == "mpv" else 1)
        layout.addRow("Default player", self.player)
        note = QLabel("Watched status is always manual. Opening or closing a video does not remove it.")
        note.setWordWrap(True)
        layout.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def save(self):
        values = {"daily_time": self.daily.time().toString("HH:mm"), "player": self.player.currentData()}
        try:
            old = core.settings(self.paths)
            core.save_settings(self.paths, values)
            if old["daily_time"] != values["daily_time"]:
                from install import update_schedule
                notice = update_schedule(self.paths, values["daily_time"])
                if notice:
                    QMessageBox.information(self, "Schedule", notice)
        except (OSError, core.InboxError) as exc:
            QMessageBox.warning(self, "Schedule could not be activated", f"Settings may be saved, but the schedule could not be activated:\n{exc}\n\nRun install.sh again to activate it.")
        self.accept()


class Window(QMainWindow):
    def __init__(self, paths):
        super().__init__()
        self.paths = paths
        self.worker = None
        self.players = []
        self.rows = []
        self.signature = None
        body_font = QFont(self.font())
        body_font.setPointSizeF(max(11, body_font.pointSizeF()))
        self.setFont(body_font)
        self.setWindowTitle("YouTube Inbox")
        self.setWindowIcon(QIcon.fromTheme("video-x-generic"))
        self.resize(1060, 640)
        self.setMinimumSize(760, 460)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(22, 20, 22, 16)
        layout.setSpacing(12)
        header = QHBoxLayout()
        title = QLabel("YouTube Inbox")
        font = QFont(title.font())
        font.setPointSize(22)
        font.setBold(True)
        title.setFont(font)
        header.addWidget(title)
        header.addStretch()
        self.channels_button = QPushButton("Channels")
        self.channels_button.clicked.connect(self.edit_channels)
        header.addWidget(self.channels_button)
        options = QPushButton("Settings")
        options.clicked.connect(self.edit_settings)
        header.addWidget(options)
        self.refresh_button = QPushButton("Check now")
        self.refresh_button.clicked.connect(self.check)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)
        self.summary = QLabel("")
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.summary)
        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        self.warning.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.warning)
        filters = QHBoxLayout()
        self.view = QComboBox()
        for label, state in (("Unwatched", "pending"), ("Watched", "watched"), ("Skipped", "skipped")):
            self.view.addItem(label, state)
        self.view.currentIndexChanged.connect(self.reload)
        filters.addWidget(self.view)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by title or channel")
        self.search.textChanged.connect(self.reload)
        filters.addWidget(self.search)
        layout.addLayout(filters)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Published", "Channel", "Video"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(37)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(1, 205)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        self.table.itemDoubleClicked.connect(lambda _: self.play())
        layout.addWidget(self.table, 1)
        self.empty = QLabel("")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)
        self.link = QLineEdit()
        self.link.setReadOnly(True)
        self.link.setPlaceholderText("Select a video to see its direct link")
        layout.addWidget(self.link)
        actions = QHBoxLayout()
        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(self.play)
        actions.addWidget(self.play_button)
        self.browser_button = QPushButton("Open in browser")
        self.browser_button.clicked.connect(self.browser)
        actions.addWidget(self.browser_button)
        actions.addStretch()
        self.watched_button = QPushButton("Mark watched")
        self.watched_button.clicked.connect(lambda: self.change("watched"))
        actions.addWidget(self.watched_button)
        self.skip_button = QPushButton("Skip")
        self.skip_button.clicked.connect(lambda: self.change("skipped"))
        actions.addWidget(self.skip_button)
        self.restore_button = QPushButton("Restore")
        self.restore_button.clicked.connect(lambda: self.change("pending"))
        actions.addWidget(self.restore_button)
        undo_button = QPushButton("Undo")
        undo_button.clicked.connect(self.undo)
        actions.addWidget(undo_button)
        layout.addLayout(actions)
        for keys, function in (("Ctrl+R", self.check), ("Ctrl+Z", self.undo), ("Ctrl+W", lambda: self.change("watched")), ("Ctrl+K", lambda: self.change("skipped")), ("Return", self.play)):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.activated.connect(function)
        self.poll = QTimer(self)
        self.poll.setInterval(3000)
        self.poll.timeout.connect(self.reload_if_changed)
        self.poll.start()
        self.reload()

    def selected(self):
        return [self.rows[index.row()] for index in self.table.selectionModel().selectedRows() if index.row() < len(self.rows)]

    def reload_if_changed(self):
        signature = tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None for p in (self.paths.database, self.paths.database.with_name(self.paths.database.name + "-wal")))
        if signature != self.signature:
            self.signature = signature
            self.reload()

    def reload(self, *_):
        selected_ids = {r["id"] for r in self.selected()} if hasattr(self, "table") else set()
        state = self.view.currentData()
        query = self.search.text().casefold().strip()
        self.rows = [r for r in core.queue_rows(self.paths, state) if not query or query in (r["title"] + " " + r["channel"]).casefold()]
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setRowCount(len(self.rows))
        for i, row in enumerate(self.rows):
            values = (dt.datetime.fromtimestamp(row["published"]).strftime("%d %b %H:%M"), row["channel"], row["title"])
            for j, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(i, j, item)
            if row["id"] in selected_ids:
                for col in range(3):
                    self.table.item(i, col).setSelected(True)
        self.table.blockSignals(False)
        db = core.connect(self.paths)
        try:
            count = db.execute("SELECT count(*) FROM videos v JOIN channels c ON c.id=v.channel_id WHERE c.active=1 AND v.status='pending'").fetchone()[0]
            channels = db.execute("SELECT count(*) FROM channels WHERE active=1").fetchone()[0]
            last = core.meta_get(db, "last_success")
            errors = core.meta_get(db, "last_errors")
        finally:
            db.close()
        when = dt.datetime.fromtimestamp(float(last)).strftime("%d %b, %H:%M") if last else "not checked yet"
        self.summary.setText(f"{count} unwatched · {channels} channel{'s' if channels != 1 else ''} · Last complete check: {when}")
        self.warning.setText("Some channels could not be checked. Try Check now; scheduled checks retry automatically.\n" + errors[:800] if errors else "")
        self.warning.setVisible(bool(errors))
        self.empty.setVisible(not self.rows)
        if not self.rows:
            if query:
                text = "No videos match this filter."
            elif not channels:
                text = "Add your favourite channels, then check for uploads."
            elif state == "pending":
                text = "Nothing waiting. New uploads will appear here after the next check.\nOlder uploads are excluded when a channel is first added."
            else:
                text = "No videos in this list."
            self.empty.setText(text)
        self.selection_changed()

    def selection_changed(self):
        selected = self.selected()
        single = len(selected) == 1
        self.link.setText(core.video_url(selected[0]["id"]) if single else "")
        self.play_button.setText("Play in mpv" if core.settings(self.paths)["player"] == "mpv" else "Play in browser")
        self.play_button.setEnabled(single)
        self.browser_button.setEnabled(single)
        self.watched_button.setEnabled(bool(selected) and self.view.currentData() != "watched")
        self.skip_button.setEnabled(bool(selected) and self.view.currentData() != "skipped")
        self.restore_button.setEnabled(bool(selected) and self.view.currentData() != "pending")

    def change(self, state):
        selected = self.selected()
        if not selected:
            return
        core.set_status(self.paths, [r["id"] for r in selected], state)
        self.reload()

    def undo(self):
        core.undo(self.paths)
        self.reload()

    def edit_channels(self):
        if ChannelsDialog(self.paths, self).exec() == QDialog.DialogCode.Accepted:
            self.check()

    def edit_settings(self):
        SettingsDialog(self.paths, self).exec()
        self.selection_changed()

    def check(self):
        if self.worker and self.worker.isRunning():
            return
        self.refresh_button.setEnabled(False)
        self.channels_button.setEnabled(False)
        self.refresh_button.setText("Checking…")
        self.worker = CheckWorker(self.paths, self)
        self.worker.completed.connect(self.checked)
        self.worker.finished.connect(self.worker_finished)
        self.worker.start()

    def checked(self, result):
        self.reload()
        if result["errors"]:
            self.warning.setText("\n".join(result["errors"])[:1000])
            self.warning.show()
        self.statusBar().showMessage(f"{result['new']} new videos · {result['checked']} channels checked", 10000)

    def worker_finished(self):
        self.refresh_button.setText("Check now")
        self.refresh_button.setEnabled(True)
        self.channels_button.setEnabled(True)

    def browser(self):
        selected = self.selected()
        if len(selected) == 1 and not QDesktopServices.openUrl(QUrl(core.video_url(selected[0]["id"]))):
            QMessageBox.warning(self, "Browser", "Could not open your browser. Copy the link shown below the list.")

    def play(self):
        if core.settings(self.paths)["player"] == "browser":
            return self.browser()
        selected = self.selected()
        if len(selected) != 1:
            return
        if not shutil.which("mpv") or not shutil.which("yt-dlp"):
            QMessageBox.warning(self, "Player unavailable", "Install mpv and yt-dlp, or use Open in browser.")
            return
        proc = QProcess(self)
        proc.setProgram(shutil.which("mpv"))
        proc.setArguments(["--no-config", "--no-terminal", "--force-window=yes", "--keep-open=no", "--save-position-on-quit", "--resume-playback=yes", "--hwdec=auto-safe", "--ytdl=yes", "--ytdl-format=bestvideo[height<=1080]+bestaudio/best[height<=1080]/best", "--", core.video_url(selected[0]["id"])])
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.log = ""
        proc.failed = False
        proc.readyReadStandardOutput.connect(lambda: self.player_output(proc))
        proc.errorOccurred.connect(lambda _: self.player_failed(proc))
        proc.finished.connect(lambda code, _: self.player_finished(proc, code))
        self.players.append(proc)
        proc.start()

    def player_output(self, proc):
        proc.log = (proc.log + bytes(proc.readAllStandardOutput()).decode("utf-8", errors="replace"))[-5000:]

    def player_failed(self, proc):
        proc.failed = True
        QMessageBox.warning(self, "Playback failed", "mpv could not start. The video is still unwatched.\n" + proc.errorString())
        if proc.state() == QProcess.ProcessState.NotRunning and proc in self.players:
            self.players.remove(proc)

    def player_finished(self, proc, code):
        self.player_output(proc)
        if code != 0 and not proc.failed:
            message = QMessageBox(self)
            message.setWindowTitle("Playback failed")
            message.setText("mpv could not play this video. It is still unwatched.\nUse Open in browser to watch it there.")
            message.setDetailedText(proc.log or "mpv exited without an error message.")
            message.exec()
        if proc in self.players:
            self.players.remove(proc)
        proc.deleteLater()

    def closeEvent(self, event):
        # Avoid killing a check midway or terminating a video when the list is closed.
        if self.worker and self.worker.isRunning():
            self.statusBar().showMessage("Wait for the current check to finish before closing.")
            event.ignore()
            return
        if any(p.state() != QProcess.ProcessState.NotRunning for p in self.players):
            self.statusBar().showMessage("Close the playing video before closing the inbox.")
            event.ignore()
            return
        event.accept()


def run(paths):
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("YouTube Inbox")
    app.setDesktopFileName("yt-inbox")
    name = core.socket_name(paths)
    client = QLocalSocket()
    client.connectToServer(name)
    if client.waitForConnected(250):
        client.write(b"show")
        client.waitForBytesWritten(250)
        client.disconnectFromServer()
        return 0
    server = QLocalServer()
    if not server.listen(name):
        QLocalServer.removeServer(name)
        if not server.listen(name):
            raise core.InboxError("Could not create the inbox's local window connection.")
    window = Window(paths)

    def activate():
        while server.hasPendingConnections():
            connection = server.nextPendingConnection()
            connection.disconnectFromServer()
            connection.deleteLater()
        window.showNormal()
        window.raise_()
        window.activateWindow()

    server.newConnection.connect(activate)
    window.show()
    urls, _ = core.parse_channels(paths.channels.read_text(encoding="utf-8"))
    if not urls:
        QTimer.singleShot(250, window.edit_channels)
    return app.exec()
