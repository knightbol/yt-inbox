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
import media
from transfers import DownloadQueue

STATUS_LABELS = {"pending": "In inbox", "watched": "Watched", "skipped": "Skipped",
                 "baseline": "Not in inbox"}


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


class CatalogueWorker(QThread):
    completed = Signal(object)

    def __init__(self, paths, channel_id, restart=False, parent=None):
        super().__init__(parent)
        self.paths, self.channel_id, self.restart = paths, channel_id, restart

    def run(self):
        try:
            self.completed.emit(media.load_catalogue(self.paths, self.channel_id, self.restart))
        except Exception as exc:
            self.completed.emit({"error": str(exc)})


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
        note = QLabel("The first complete check adds each channel's latest video. New uploads are tracked after that.")
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
        self.quality = QComboBox()
        for height in (480, 720, 1080):
            self.quality.addItem(f"Up to {height}p", height)
        self.quality.setCurrentIndex(self.quality.findData(current.get("download_height", 720)))
        layout.addRow("Offline download quality", self.quality)
        note = QLabel("Watched status is always manual. Opening or closing a video does not remove it.")
        note.setWordWrap(True)
        layout.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def save(self):
        values = {"daily_time": self.daily.time().toString("HH:mm"), "player": self.player.currentData(),
                  "download_height": self.quality.currentData()}
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
        self.catalogue_worker = None
        self.browse_page = 0
        self.browse_key = None
        self.download_records = {}
        self.players = []
        self.rows = []
        self.signature = None
        body_font = QFont(self.font())
        body_font.setPointSizeF(max(11, body_font.pointSizeF()))
        self.setFont(body_font)
        self.setWindowTitle("YouTube Inbox")
        self.setWindowIcon(QIcon.fromTheme("video-x-generic"))
        self.resize(1100, 720)
        self.setMinimumSize(760, 570)
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
        for label, state in (("Unwatched", "pending"), ("Watched", "watched"), ("Skipped", "skipped"), ("Browse channels", "browse"), ("Downloads", "downloads")):
            self.view.addItem(label, state)
        self.view.currentIndexChanged.connect(self.reload)
        filters.addWidget(self.view)
        self.browse_channel = QComboBox()
        self.browse_channel.setAccessibleName("Channel to browse")
        self.browse_channel.setMinimumContentsLength(16)
        self.browse_channel.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.browse_channel.currentIndexChanged.connect(self.reload)
        filters.addWidget(self.browse_channel, 1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by title or channel")
        self.search.textChanged.connect(self.reload)
        filters.addWidget(self.search, 1)
        layout.addLayout(filters)
        self.browse_note = QLabel("")
        self.browse_note.setTextFormat(Qt.TextFormat.PlainText)
        self.browse_note.setWordWrap(True)
        layout.addWidget(self.browse_note)
        self.browse_nav = QWidget()
        navigation = QHBoxLayout(self.browse_nav)
        navigation.setContentsMargins(0, 0, 0, 0)
        self.previous_page = QPushButton("Previous")
        self.previous_page.clicked.connect(lambda: self.turn_page(-1))
        navigation.addWidget(self.previous_page)
        self.page_label = QLabel("")
        navigation.addWidget(self.page_label)
        self.next_page = QPushButton("Next")
        self.next_page.clicked.connect(lambda: self.turn_page(1))
        navigation.addWidget(self.next_page)
        navigation.addStretch()
        self.older_button = QPushButton("Load older videos")
        self.older_button.clicked.connect(lambda: self.load_catalogue())
        navigation.addWidget(self.older_button)
        self.catalogue_refresh = QPushButton("Refresh catalogue")
        self.catalogue_refresh.clicked.connect(lambda: self.load_catalogue(restart=True))
        navigation.addWidget(self.catalogue_refresh)
        layout.addWidget(self.browse_nav)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Published", "Channel", "Video", "Status", "Download"])
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
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
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
        downloads = QHBoxLayout()
        self.download_button = QPushButton("Download offline")
        self.download_button.clicked.connect(self.download_selected)
        downloads.addWidget(self.download_button)
        self.cancel_download_button = QPushButton("Cancel download")
        self.cancel_download_button.clicked.connect(self.cancel_downloads)
        downloads.addWidget(self.cancel_download_button)
        self.remove_download_button = QPushButton("Remove files")
        self.remove_download_button.clicked.connect(self.remove_downloads)
        downloads.addWidget(self.remove_download_button)
        downloads.addStretch()
        folder = QPushButton("Open downloads folder")
        folder.clicked.connect(self.open_downloads_folder)
        downloads.addWidget(folder)
        layout.addLayout(downloads)
        self.download_info = QLabel("")
        self.download_info.setTextFormat(Qt.TextFormat.PlainText)
        self.download_info.setWordWrap(True)
        layout.addWidget(self.download_info)
        for keys, function in (("Ctrl+R", self.check), ("Ctrl+Z", self.undo), ("Ctrl+W", lambda: self.change("watched")), ("Ctrl+K", lambda: self.change("skipped")), ("Return", self.play)):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.activated.connect(function)
        self.poll = QTimer(self)
        self.poll.setInterval(3000)
        self.poll.timeout.connect(self.reload_if_changed)
        self.poll.start()
        self.download_queue = DownloadQueue(paths, self)
        self.download_queue.changed.connect(self.reload)
        self.download_queue.progress.connect(self.download_progress)
        self.reload()

    def selected(self):
        return [self.rows[index.row()] for index in self.table.selectionModel().selectedRows() if index.row() < len(self.rows)]

    def reload_if_changed(self):
        signature = tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None for p in (self.paths.database, self.paths.database.with_name(self.paths.database.name + "-wal")))
        if signature != self.signature:
            self.signature = signature
            self.reload()

    def sync_browse_channels(self):
        selected_id = self.browse_channel.currentData()
        channels = core.channel_rows(self.paths)
        self.browse_channel.blockSignals(True)
        self.browse_channel.clear()
        for channel in channels:
            self.browse_channel.addItem(channel["name"], channel["id"])
        index = self.browse_channel.findData(selected_id)
        if index >= 0:
            self.browse_channel.setCurrentIndex(index)
        self.browse_channel.blockSignals(False)
        return next((c for c in channels if c["id"] == self.browse_channel.currentData()), None)

    def reload(self, *_):
        selected_ids = {r["id"] for r in self.selected()} if hasattr(self, "table") else set()
        state = self.view.currentData()
        browsing = state == "browse"
        self.browse_channel.setVisible(browsing)
        self.browse_note.setVisible(browsing)
        self.browse_nav.setVisible(browsing)
        self.table.setColumnHidden(1, browsing)
        self.table.setColumnHidden(3, state not in ("browse", "downloads"))
        self.restore_button.setText("Add to inbox" if state in ("browse", "downloads") else "Restore")
        self.search.setPlaceholderText("Search loaded channel videos" if browsing else "Filter by title or channel")
        query = self.search.text().casefold().strip()
        if browsing:
            channel = self.sync_browse_channels()
            key = (channel["id"] if channel else None, query)
            if self.browse_key != key:
                self.browse_page = 0
                self.browse_key = key
            rows, total, self.browse_page = media.browse_page(self.paths, key[0], query, self.browse_page) if channel else ([], 0, 0)
            saved = media.catalogue_state(self.paths, key[0]) if channel else None
            last_check = saved["updated"] if saved else channel["last_check"] if channel else None
            when = dt.datetime.fromtimestamp(last_check).strftime("%d %b %Y, %H:%M") if last_check is not None else "not checked yet"
            source = "Catalogue updated" if saved else "Recent uploads checked"
            self.browse_note.setText(f"{source}: {when} · Search covers all loaded videos.\n"
                                     "Load older videos in batches of 100. Refresh catalogue starts again from the latest uploads.")
            self.page_label.setText(f"{self.browse_page + 1}/{max(1, (total + media.PAGE_SIZE - 1) // media.PAGE_SIZE)} · {total} videos")
            self.previous_page.setEnabled(self.browse_page > 0)
            self.next_page.setEnabled((self.browse_page + 1) * media.PAGE_SIZE < total)
            loading = self.catalogue_worker is not None and self.catalogue_worker.isRunning()
            exhausted = bool(saved and saved["exhausted"])
            self.older_button.setText("Loading…" if loading else "Catalogue loaded" if exhausted else "Load older videos")
            self.older_button.setEnabled(bool(channel) and not loading and not exhausted)
            self.catalogue_refresh.setEnabled(bool(channel) and not loading)
        elif state == "downloads":
            rows = media.download_rows(self.paths)
        else:
            rows = core.queue_rows(self.paths, state)
        self.rows = [r for r in rows if not query or query in (r["title"] + " " + r["channel"]).casefold()]
        self.download_records = media.download_records(self.paths)
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setRowCount(len(self.rows))
        for i, row in enumerate(self.rows):
            date_format = "%d %b %Y %H:%M" if browsing else "%d %b %H:%M"
            published = dt.datetime.fromtimestamp(row["published"]).strftime(date_format) if row["published"] else "—"
            record = self.download_records.get(row["id"], {})
            file = media.valid_local_file(self.paths, row["id"], record.get("path")) if record.get("state") == "complete" else None
            row["local_file"] = str(file) if file else ""
            row["download_state"] = record.get("state", "")
            download = {"queued": "Queued", "downloading": "Downloading", "complete": "Ready" if file else "File missing",
                        "failed": "Failed", "cancelled": "Cancelled"}.get(row["download_state"], "—")
            values = (published, row["channel"], row["title"], STATUS_LABELS[row["status"]], download)
            for j, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(i, j, item)
            self.table.item(i, 4).setToolTip(record.get("error") or row["local_file"] or download)
            if row["id"] in selected_ids:
                for col in range(self.table.columnCount()):
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
            elif state == "downloads":
                text = "Select videos in your inbox or a channel, then choose Download offline."
            elif not channels:
                text = "Add your favourite channels, then check for uploads."
            elif state == "pending":
                text = "Nothing waiting. Check now to load each channel's latest video when first added.\nAfter that, new uploads appear here until you watch or skip them."
            elif browsing:
                text = "No videos loaded for this channel. Select Load older videos or Check now."
            else:
                text = "No videos in this list."
            self.empty.setText(text)
        self.selection_changed()

    def selection_changed(self):
        selected = self.selected()
        single = len(selected) == 1
        self.link.setText(core.video_url(selected[0]["id"]) if single else "")
        offline = single and bool(selected[0].get("local_file"))
        self.play_button.setText("Play offline" if offline else "Play in mpv" if core.settings(self.paths)["player"] == "mpv" else "Play in browser")
        self.play_button.setEnabled(single and (self.view.currentData() != "downloads" or offline))
        self.browser_button.setEnabled(single)
        self.watched_button.setEnabled(any(r["status"] != "watched" for r in selected))
        self.skip_button.setEnabled(any(r["status"] != "skipped" for r in selected))
        self.restore_button.setEnabled(any(r["status"] != "pending" for r in selected))
        self.download_button.setEnabled(any(r.get("download_state") not in ("queued", "downloading") and not r.get("local_file") for r in selected))
        self.cancel_download_button.setEnabled(any(r.get("download_state") in ("queued", "downloading") for r in selected))
        playing = {getattr(p, "video_id", None) for p in self.players if p.state() != QProcess.ProcessState.NotRunning}
        self.remove_download_button.setEnabled(bool(selected) and all(r["id"] not in playing and r.get("download_state") not in ("", "queued", "downloading") for r in selected))

    def turn_page(self, direction):
        self.browse_page = max(0, self.browse_page + direction)
        self.reload()

    def load_catalogue(self, restart=False):
        channel_id = self.browse_channel.currentData()
        if not channel_id or (self.catalogue_worker and self.catalogue_worker.isRunning()):
            return
        self.catalogue_worker = CatalogueWorker(self.paths, channel_id, restart, self)
        self.catalogue_worker.completed.connect(self.catalogue_loaded)
        self.catalogue_worker.finished.connect(self.reload)
        self.catalogue_worker.start()
        self.reload()

    def catalogue_loaded(self, result):
        if result.get("error"):
            QMessageBox.warning(self, "Catalogue lookup failed", result["error"])
        else:
            self.statusBar().showMessage(f"{result['loaded']} catalogue entries loaded", 10000)
        self.reload()

    def download_progress(self, text):
        self.download_info.setText(text)
        self.download_info.setVisible(bool(text))

    def download_selected(self):
        try:
            media.queue_downloads(self.paths, [r["id"] for r in self.selected()])
            self.reload()
            self.download_queue.start_next()
        except (core.InboxError, OSError) as exc:
            QMessageBox.warning(self, "Download", str(exc))

    def cancel_downloads(self):
        self.download_queue.cancel([r["id"] for r in self.selected()])

    def remove_downloads(self):
        playing = {getattr(p, "video_id", None) for p in self.players if p.state() != QProcess.ProcessState.NotRunning}
        try:
            for row in self.selected():
                if row["id"] in playing:
                    raise core.InboxError("Close the playing video before removing its files.")
                media.remove_download(self.paths, row["id"])
        except (core.InboxError, OSError) as exc:
            QMessageBox.warning(self, "Remove download", str(exc))
        self.reload()

    def open_downloads_folder(self):
        directory = self.paths.data / "downloads"
        directory.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory.resolve())))

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
        selected = self.selected()
        if len(selected) != 1:
            return
        row = selected[0]
        local = media.local_file(self.paths, row["id"])
        if not local and (row.get("local_file") or self.view.currentData() == "downloads"):
            QMessageBox.warning(self, "Offline playback", "The video is not available offline. Download it again, or use Open in browser.")
            self.reload()
            return
        if not local and core.settings(self.paths)["player"] == "browser":
            return self.browser()
        if not shutil.which("mpv") or (not local and not shutil.which("yt-dlp")):
            QMessageBox.warning(self, "Player unavailable", "Install mpv and yt-dlp, or use Open in browser.")
            return
        proc = QProcess(self)
        proc.video_id = row["id"]
        proc.setProgram(shutil.which("mpv"))
        arguments = ["--no-config", "--no-terminal", "--force-window=yes", "--keep-open=no", "--save-position-on-quit", "--resume-playback=yes", "--hwdec=auto-safe"]
        arguments += ["--ytdl=no", "--", str(local)] if local else ["--ytdl=yes", "--ytdl-format=bestvideo[height<=1080]+bestaudio/best[height<=1080]/best", "--", core.video_url(row["id"])]
        proc.setArguments(arguments)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.log = ""
        proc.failed = False
        proc.readyReadStandardOutput.connect(lambda: self.player_output(proc))
        proc.errorOccurred.connect(lambda _: self.player_failed(proc))
        proc.finished.connect(lambda code, _: self.player_finished(proc, code))
        self.players.append(proc)
        proc.start()
        self.selection_changed()

    def player_output(self, proc):
        proc.log = (proc.log + bytes(proc.readAllStandardOutput()).decode("utf-8", errors="replace"))[-5000:]

    def player_failed(self, proc):
        proc.failed = True
        QMessageBox.warning(self, "Playback failed", "mpv could not start. The video's status has not changed.\n" + proc.errorString())
        if proc.state() == QProcess.ProcessState.NotRunning and proc in self.players:
            self.players.remove(proc)
        self.selection_changed()

    def player_finished(self, proc, code):
        self.player_output(proc)
        if code != 0 and not proc.failed:
            message = QMessageBox(self)
            message.setWindowTitle("Playback failed")
            message.setText("mpv could not play this video. Its status has not changed.\nUse Open in browser to watch it there.")
            message.setDetailedText(proc.log or "mpv exited without an error message.")
            message.exec()
        if proc in self.players:
            self.players.remove(proc)
        proc.deleteLater()
        self.selection_changed()

    def closeEvent(self, event):
        # Avoid killing a check midway or terminating a video when the list is closed.
        if self.worker and self.worker.isRunning():
            self.statusBar().showMessage("Wait for the current check to finish before closing.")
            event.ignore()
            return
        if self.catalogue_worker and self.catalogue_worker.isRunning():
            self.statusBar().showMessage("Wait for the current catalogue page to finish before closing.")
            event.ignore()
            return
        if self.download_queue.process is not None:
            self.statusBar().showMessage("Wait for downloads to finish, or cancel them in Downloads before closing.")
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
