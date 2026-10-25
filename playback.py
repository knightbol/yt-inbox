"""Bounded playback formats and small, read-only mpv status messages."""
import json
import math
from pathlib import Path


MODES = {
    "auto": "Auto — adapt to this screen and decoder",
    "low": "Low power — up to 720p / 30 fps",
    "balanced": "Balanced — up to 1080p",
    "high": "High quality — up to 4K",
    "custom": "Use my mpv configuration",
}
DEFAULT_MODE = "auto"
MARKER = "YT_INBOX_PLAYBACK:"


def screen_limit(height):
    """Use a conservative standard tier; never infer GPU capability from its name."""
    if not isinstance(height, (int, float)) or not math.isfinite(height) or height <= 0:
        return 1080
    return next((limit for limit in (2160, 1440, 1080) if height >= limit), 720)


def format_selector(height, fps=60, compatible=True, prefer_h264=False):
    limits = f"[height<={height}][fps<=?{fps}]"
    codec = "[vcodec!^=av01]" if compatible else ""
    bounded = f"bv*{limits}{codec}+ba/b{limits}{codec}"
    if prefer_h264:
        avc = limits + "[vcodec^=avc1]"
        return f"bv*{avc}+ba/b{avc}/" + bounded
    return bounded


def auto_formats(height, refresh_rate=60):
    limit = screen_limit(height)
    fps = 30 if 0 < refresh_rate < 50 else 60
    return [format_selector(limit, fps),
            format_selector(min(limit, 1080), 30, prefer_h264=True),
            format_selector(min(limit, 720), 30, prefer_h264=True),
            format_selector(480, 30, prefer_h264=True)]


def download_format(height, mode=DEFAULT_MODE):
    # Offline files cannot change codec during playback. Prefer portable copies.
    if mode == "custom":
        return f"bv*[height<={height}]+ba/b[height<={height}]"
    return format_selector(height, 30 if mode == "low" else 60,
                           compatible=mode not in ("high", "custom"),
                           prefer_h264=mode in ("auto", "low"))


def arguments(source, mode=DEFAULT_MODE, local=False, height=1080, refresh_rate=60, stage=0):
    if mode not in MODES:
        raise ValueError("Unknown playback mode")
    formats = auto_formats(height, refresh_rate)
    stage = min(3, max(0, int(stage))) if mode == "auto" and not local else 0
    # A pipe captures errors without creating a terminal or taking keyboard input.
    args = ["--terminal=yes", "--input-terminal=no", "--term-status-msg=", "--msg-level=all=warn",
            "--script=" + str(Path(__file__).with_name("playback.lua").resolve())]
    opts = {"mode": mode, "online": "no" if local else "yes", "stage": stage}
    opts.update({"format" + str(index): value for index, value in enumerate(formats)})
    # Map-option append takes ONE key/value pair; commas become part of its value.
    args.extend("--script-opts-append=yt_inbox-" + key + "=" + str(value) for key, value in opts.items())
    if mode != "custom":
        args.append("--hwdec=auto")
    if local:
        args.append("--ytdl=no")
    elif mode != "custom":
        selection = formats[stage] if mode == "auto" else {
            "low": format_selector(720, 30, prefer_h264=True),
            "balanced": format_selector(1080),
            "high": format_selector(2160, compatible=False),
        }[mode]
        args.extend(["--ytdl=yes", "--ytdl-format=" + selection])
    return args + ["--", str(source)]


def status_message(data):
    """Treat player output as data; cap lengths and never render rich text."""
    if not isinstance(data, dict):
        return ""
    if data.get("event") == "adjusting":
        return "Auto: switching to a lighter format and resuming playback…"
    height = data.get("height")
    if not isinstance(height, (int, float)) or not math.isfinite(height) or height <= 0:
        return ""
    codec = str(data.get("codec") or "unknown codec").upper()[:32]
    decoder = str(data.get("hwdec") or "unknown")[:32]
    detail = "software decoding" if decoder == "no" else "decoder unconfirmed" if decoder == "unknown" else f"hardware decoding ({decoder})"
    prefix = "Offline" if data.get("online") is False else "Playing"
    return f"{prefix} · {int(height)}p {codec} · {detail}"


def read_messages(buffer, chunk):
    """Keep incomplete lines across QProcess reads, with a fixed memory bound."""
    buffer += chunk
    lines = buffer.split(b"\n")
    messages, log = [], []
    for raw in lines[:-1]:
        line = raw.decode("utf-8", errors="replace")
        if line.startswith(MARKER):
            try:
                data = json.loads(line[len(MARKER):])
                if isinstance(data, dict):
                    messages.append(data)
            except (ValueError, TypeError):
                pass
        else:
            log.append(line)
    return lines[-1][-16384:], messages, "\n".join(log)
