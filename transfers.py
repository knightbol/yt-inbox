"""Sequential downloads without blocking the Qt event loop."""
import json
import os
import signal

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

import core
import media


def stop_group(pid, sig):
    if pid:
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass


class DownloadQueue(QObject):
    changed = Signal()
    progress = Signal(str)

    def __init__(self, paths, parent=None):
        super().__init__(parent)
        self.paths = paths
        self.process = None
        self.active_id = None
        media.recover_downloads(paths)
        QTimer.singleShot(0, self.start_next)

    def start_next(self):
        if self.process is not None:
            return
        video_id = media.next_download(self.paths)
        if not video_id:
            self.progress.emit("")
            return
        try:
            command = media.download_command(self.paths, video_id)
        except (core.InboxError, OSError) as exc:
            media.update_download(self.paths, video_id, "failed", error=str(exc))
            self.changed.emit()
            QTimer.singleShot(0, self.start_next)
            return
        self.active_id = video_id
        self.active_title = media.video_title(self.paths, video_id)
        self.cancelled = False
        self.buffer = b""
        self.log = ""
        self.output_file = ""
        self.pid = 0
        proc = QProcess(self)
        self.process = proc
        params = QProcess.UnixProcessParameters()
        params.flags = QProcess.UnixProcessFlag.CreateNewSession
        proc.setUnixProcessParameters(params)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.setProgram(command[0])
        proc.setArguments(command[1:])
        proc.started.connect(self.started)
        proc.readyReadStandardOutput.connect(lambda: self.read_output(proc))
        proc.finished.connect(lambda code, _: self.finished(proc, code))
        proc.errorOccurred.connect(lambda err: self.failed_to_start(proc, err))
        media.update_download(self.paths, video_id, "downloading")
        self.changed.emit()
        self.progress.emit(f"Downloading: {self.active_title}…")
        proc.start()

    def started(self):
        self.pid = self.process.processId()
        if self.cancelled:
            self.stop_active()

    def read_output(self, proc, final=False):
        if proc is not self.process:
            return
        self.buffer += bytes(proc.readAllStandardOutput())
        lines = self.buffer.split(b"\n")
        self.buffer = lines.pop()
        if final and self.buffer:
            lines.append(self.buffer)
            self.buffer = b""
        for raw in lines:
            line = raw.decode("utf-8", errors="replace").strip()
            self.log = (self.log + line + "\n")[-7000:]
            if line.startswith("YT_INBOX_FILE:"):
                try:
                    value = json.loads(line.partition(":")[2])
                    if isinstance(value, str):
                        self.output_file = value
                except ValueError:
                    pass
            elif line.startswith("YT_INBOX_PROGRESS:"):
                self.progress.emit(f"Downloading: {self.active_title} · {line.partition(':')[2].strip()}")
        self.buffer = self.buffer[-10000:]

    def failed_to_start(self, proc, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.log = proc.errorString()
            self.finished(proc, -1)

    def finished(self, proc, code):
        if proc is not self.process:
            return
        self.read_output(proc, final=True)
        if self.cancelled:
            # Also stop a merger/downloader child if its parent has already exited.
            stop_group(self.pid, signal.SIGKILL)
            media.update_download(self.paths, self.active_id, "cancelled")
        else:
            try:
                if code == 75:
                    raise core.InboxError("Another process is downloading this video. Retry after it finishes.")
                if code != 0:
                    raise core.InboxError(self.log or "The download process failed.")
                media.finish_download(self.paths, self.active_id, self.output_file)
            except (core.InboxError, OSError) as exc:
                media.update_download(self.paths, self.active_id, "failed", error=str(exc))
        self.process = None
        self.active_id = None
        proc.deleteLater()
        self.changed.emit()
        QTimer.singleShot(0, self.start_next)

    def stop_active(self):
        pid = self.pid
        stop_group(pid, signal.SIGTERM)
        # Kill only this process group if a child ignores termination.
        QTimer.singleShot(2000, lambda: stop_group(pid, signal.SIGKILL) if self.pid == pid and self.process is not None else None)

    def cancel(self, ids):
        records = media.download_records(self.paths)
        for video_id in ids:
            if video_id == self.active_id:
                self.cancelled = True
                self.stop_active()
            elif records.get(video_id, {}).get("state") == "queued":
                media.update_download(self.paths, video_id, "cancelled")
        self.changed.emit()
