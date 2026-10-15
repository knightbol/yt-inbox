#!/usr/bin/env python3
"""Per-user installation. Never changes system packages or shell configuration."""
import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import core


def locations(paths):
    if os.environ.get("YT_INBOX_HOME"):
        root = Path(os.environ["YT_INBOX_HOME"]).expanduser().resolve()
        return root / "bin", root / "systemd/user", root / "applications"
    return Path.home() / ".local/bin", paths.config.parent / "systemd/user", paths.data.parent / "applications"


def unit_quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def desktop_quote(value):
    # Desktop Entry quoting is not shell quoting. Backslashes are escaped twice.
    value = str(value).replace("\\", "\\\\\\\\").replace('"', '\\\\"').replace("`", "\\\\`").replace("$", "\\\\$").replace("%", "%%")
    return '"' + value + '"'


def timer_text(daily_time):
    return f"""[Unit]
Description=Check YouTube uploads once daily

[Timer]
OnCalendar=*-*-* {daily_time}:00
OnStartupSec=90s
Persistent=true
AccuracySec=1min
Unit=yt-inbox-check.service

[Install]
WantedBy=timers.target
"""


def systemctl(*args):
    result = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    if result.returncode:
        raise core.InboxError(result.stderr.strip() or "systemctl failed")


def update_schedule(paths, daily_time):
    _, units, _ = locations(paths)
    if not (units / "yt-inbox-check.service").exists():
        return "Install the app to activate daily checks."
    core.atomic_write(units / "yt-inbox-check.timer", timer_text(daily_time))
    systemctl("daemon-reload")
    systemctl("restart", "yt-inbox-check.timer")
    return ""


def install(paths, enable=True):
    paths.ensure()
    source = Path(__file__).resolve().parent
    app = paths.data / "app"
    app.mkdir(parents=True, exist_ok=True)
    for name in ("core.py", "cli.py", "gui.py", "install.py", "README.md"):
        target = app / name
        if source / name != target:
            # Replace code atomically. Existing preferences and database are preserved.
            core.atomic_write(target, (source / name).read_text(encoding="utf-8"))
    bindir, units, desktops = locations(paths)
    for directory in (bindir, units, desktops):
        directory.mkdir(parents=True, exist_ok=True)
    python = str(Path(sys.executable).resolve())
    launcher = bindir / "yt-inbox"
    core.atomic_write(launcher, "#!/bin/sh\nexec " + shlex.quote(python) + " " + shlex.quote(str(app / "cli.py")) + ' "$@"\n', 0o755)
    core.atomic_write(desktops / "yt-inbox.desktop", f"""[Desktop Entry]
Type=Application
Name=YouTube Inbox
Comment=Daily uploads from your chosen channels
Exec={desktop_quote(launcher)} gui
Icon=video-x-generic
Terminal=false
Categories=AudioVideo;Network;
StartupNotify=true
""")
    base = unit_quote(python) + " " + unit_quote(app / "cli.py")
    core.atomic_write(units / "yt-inbox-check.service", f"""[Unit]
Description=Fetch new YouTube uploads
StartLimitIntervalSec=0

[Service]
Type=oneshot
ExecStart={base} check --scheduled
Restart=on-failure
RestartSec=15min
TimeoutStartSec=30min
UMask=0077
""")
    core.atomic_write(units / "yt-inbox-notify.service", f"""[Unit]
Description=YouTube Inbox daily notification
After=graphical-session.target
PartOf=graphical-session.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart={base} notify
Restart=on-failure
RestartSec=15min
RuntimeMaxSec=24h
UMask=0077
""")
    core.atomic_write(units / "yt-inbox-gui.service", f"""[Unit]
Description=YouTube Inbox window
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart={base} gui
UMask=0077
""")
    core.atomic_write(units / "yt-inbox-check.timer", timer_text(core.settings(paths)["daily_time"]))
    if enable:
        variables = [name for name in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_CURRENT_DESKTOP", "QT_QPA_PLATFORMTHEME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME") if name in os.environ]
        if variables:
            systemctl("import-environment", *variables)
        systemctl("daemon-reload")
        systemctl("enable", "--now", "yt-inbox-check.timer")
    return launcher


def uninstall(paths):
    bindir, units, desktops = locations(paths)
    subprocess.run(["systemctl", "--user", "disable", "--now", "yt-inbox-check.timer"], capture_output=True)
    subprocess.run(["systemctl", "--user", "stop", "yt-inbox-check.service", "yt-inbox-notify.service", "yt-inbox-gui.service"], capture_output=True)
    for name in ("yt-inbox-check.timer", "yt-inbox-check.service", "yt-inbox-notify.service", "yt-inbox-gui.service"):
        (units / name).unlink(missing_ok=True)
    (bindir / "yt-inbox").unlink(missing_ok=True)
    (desktops / "yt-inbox.desktop").unlink(missing_ok=True)
    app = paths.data / "app"
    if app.exists():
        shutil.rmtree(app)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    print("Application removed. Your channel list, settings and watched history are preserved.")


def main():
    parser = argparse.ArgumentParser(description="Install YouTube Inbox for your user account")
    parser.add_argument("--no-enable", action="store_true", help="Write files without enabling systemd (for staging/testing)")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    if os.geteuid() == 0 and not os.environ.get("YT_INBOX_HOME"):
        parser.error("Run this installer as your normal user, without sudo.")
    paths = core.Paths.discover()
    if args.uninstall:
        uninstall(paths)
        return 0
    if not args.no_enable:
        missing = []
        try:
            import PySide6.QtWidgets
        except ImportError:
            missing.append("pyside6")
        for command, package in (("mpv", "mpv"), ("yt-dlp", "yt-dlp"), ("deno", "deno"), ("notify-send", "libnotify")):
            if not shutil.which(command):
                missing.append(package)
        if missing:
            print("Install the required Arch packages first:\n\nsudo pacman -S --needed python pyside6 mpv yt-dlp deno libnotify", file=sys.stderr)
            return 1
    try:
        launcher = install(paths, not args.no_enable)
    except (OSError, core.InboxError) as exc:
        print(f"Installation incomplete: {exc}\nRun the installer again after resolving this error.", file=sys.stderr)
        return 1
    print(f"Installed YouTube Inbox.\n\nOpen it from your application launcher, or run:\n{launcher}\n\nDaily check: {core.settings(paths)['daily_time']} local time\nChannels: {paths.channels}\nWatched history is preserved when reinstalling.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
