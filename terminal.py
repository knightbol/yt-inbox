"""Terminal playback and foreground downloads; no Qt or display server required."""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import unicodedata

import core
import media
import playback


def plain(value):
    """Do not let titles, metadata or command output inject terminal controls."""
    return "".join(c if c.isprintable() else " " for c in str(value))


def clipped(value, columns):
    """Clip by terminal cells so wide titles cannot overwrite the next row."""
    result, used = [], 0
    for char in plain(value):
        cells = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if used + cells > columns:
            break
        result.append(char)
        used += cells
    return "".join(result)


def local_vt():
    try:
        return bool(re.fullmatch(r"/dev/tty[1-9][0-9]*", os.ttyname(sys.stdin.fileno())))
    except (OSError, ValueError):
        return False


def play_command(paths, video_id, output="auto"):
    if output not in ("auto", "drm", "window"):
        raise core.InboxError("Unknown video output.")
    drm = output == "drm" or (output == "auto" and local_vt())
    if drm and not local_vt():
        raise core.InboxError("DRM playback needs a local Linux virtual console (/dev/tty1, tty2, ...), not SSH or a terminal window.")
    if not drm and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise core.InboxError("No graphical display or local Linux console found. Browse/download here; play from a local TTY or desktop session.")
    if not shutil.which("mpv"):
        raise core.InboxError("Install mpv to play videos.")
    core.video_url(video_id)
    file = media.local_file(paths, video_id)
    if not file and media.download_records(paths).get(video_id, {}).get("state") == "complete":
        raise core.InboxError("The saved file is missing. Download it again before playing offline.")
    if not file and not shutil.which("yt-dlp"):
        raise core.InboxError("Install yt-dlp to stream videos.")
    mode = core.settings(paths).get("playback_mode", playback.DEFAULT_MODE)
    # A text terminal has no reliable screen geometry. Auto starts at <=720p;
    # explicit Balanced/High choices retain their normal limits.
    return [shutil.which("mpv"), *playback.arguments(file or core.video_url(video_id), mode,
            local=bool(file), height=720, interactive=True, drm=drm)]


def play(paths, video_id, output="auto"):
    command = play_command(paths, video_id, output)
    print("mpv: Space pauses; arrows seek; q returns to the inbox.\n", flush=True)
    proc = subprocess.Popen(command)  # Keep stdin and the foreground terminal.
    try:
        code = proc.wait()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    if code:
        raise core.InboxError(f"mpv exited with code {code}. See its message above. DRM needs an active local VT and a compatible graphics driver.")
    return "Playback closed. Press w to mark watched."


def stop_download(proc):
    """Stop yt-dlp and its merger, retaining resumable partial files."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # A merger can outlive its parent; the new session belongs only to this job.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def download(paths, video_id, write=print):
    """One explicitly selected download, while holding core.interface_lock."""
    if media.local_file(paths, video_id):
        return "Already downloaded. Enter plays the saved file."
    command = media.download_command(paths, video_id)
    media.queue_downloads(paths, [video_id])
    media.update_download(paths, video_id, "downloading")
    proc, output_file, log = None, "", ""
    try:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            log = (log + plain(line) + "\n")[-7000:]
            if line.startswith("YT_INBOX_FILE:"):
                try:
                    value = json.loads(line.partition(":")[2])
                    if isinstance(value, str):
                        output_file = value
                except ValueError:
                    pass
            else:
                write(plain(line.replace("YT_INBOX_PROGRESS:", "Downloading: ")))
        code = proc.wait()
        if code == 75:
            raise core.InboxError("An earlier process is still downloading this video. Retry after it finishes.")
        if code:
            raise core.InboxError(log or f"Download failed ({code}).")
        media.finish_download(paths, video_id, output_file)
        return "Download ready. Enter plays offline."
    except KeyboardInterrupt:
        if proc:
            stop_download(proc)
        media.update_download(paths, video_id, "cancelled")
        raise
    except (OSError, core.InboxError) as exc:
        if proc and proc.poll() is None:
            stop_download(proc)
        media.update_download(paths, video_id, "failed", error=str(exc))
        raise
    finally:
        if proc and proc.stdout:
            proc.stdout.close()
