#!/usr/bin/env python3
"""Command line entry point; network checks do not load the GUI toolkit."""
import argparse
import datetime as dt
import html
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys

import core


def launch_notification(paths):
    db = core.connect(paths)
    try:
        today = dt.date.today().isoformat()
        wanted = core.meta_get(db, "last_digest_day") != today and bool(core.notify_candidates(db))
    finally:
        db.close()
    if not wanted:
        return True
    result = subprocess.run(["systemctl", "--user", "restart", "yt-inbox-notify.service"], capture_output=True, text=True)
    if result.returncode:
        print("Could not start desktop notification: " + result.stderr.strip(), file=sys.stderr)
    return result.returncode == 0


def notification(paths, test=False):
    """Keep just a small action listener alive until this notification closes."""
    if not shutil.which("notify-send"):
        raise core.InboxError("notify-send is missing. Install libnotify.")
    with core.exclusive_lock(paths.state / "notification.lock"):
        db = core.connect(paths)
        proc = None
        try:
            today = dt.date.today().isoformat()
            rows = core.notify_candidates(db)
            if not test and (not rows or core.meta_get(db, "last_digest_day") == today):
                return 0
            total = db.execute("SELECT count(*) FROM videos v JOIN channels c ON c.id=v.channel_id WHERE c.active=1 AND v.status='pending'").fetchone()[0]
            if test:
                summary, body = "YouTube Inbox is ready", "Open your inbox to manage channels and videos."
            else:
                summary = f"{len(rows)} new video{'s' if len(rows) != 1 else ''} · {total} unwatched"
                lines = [f"{r['channel']}: {r['title']}" for r in rows[:3]]
                if len(rows) > 3:
                    lines.append(f"And {len(rows) - 3} more in your inbox.")
                body = html.escape("\n".join(lines))
            proc = subprocess.Popen(["notify-send", "--app-name=YouTube Inbox", "--icon=video-x-generic", "--hint=string:desktop-entry:yt-inbox", "--expire-time=0", "--print-id", "--wait", "--action=default=Open inbox", "--", summary, body], stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            # notify-send prints the server's ID before waiting for a user action.
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                if not selector.select(timeout=15):
                    raise core.InboxError("The desktop notification service did not respond.")
                notification_id = proc.stdout.readline().decode().strip()
            if not notification_id.isdigit() or int(notification_id) < 1:
                error = proc.stderr.read(2000).decode(errors="replace") if proc.poll() is not None else ""
                raise core.InboxError("Could not display the notification. " + error.strip())
            if not test:
                with db:
                    core.meta_set(db, "last_digest_day", today)
                    db.executemany("UPDATE videos SET notified=1 WHERE id=?", [(r["id"],) for r in rows])
            db.close()
            db = None
            stdout, _ = proc.communicate(timeout=23 * 60 * 60)
            if b"default" in stdout.splitlines():
                result = subprocess.run(["systemctl", "--user", "start", "yt-inbox-gui.service"], capture_output=True, text=True)
                if result.returncode:
                    raise core.InboxError("Could not open the inbox. " + result.stderr.strip())
            return 0
        except subprocess.TimeoutExpired:
            return 0
        finally:
            if db:
                db.close()
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            if proc:
                if proc.stdout:
                    proc.stdout.close()
                if proc.stderr:
                    proc.stderr.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="A daily YouTube inbox with explicit watched/skipped tracking.")
    parser.add_argument("--version", action="version", version=core.VERSION)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("gui", help="Open the desktop inbox (default)")
    check = sub.add_parser("check", help="Fetch new uploads now")
    check.add_argument("--scheduled", action="store_true", help="Use daily deduplication, retries and notification")
    sub.add_parser("notify", help="Deliver the daily digest (used by systemd)")
    sub.add_parser("test-notification", help="Send a test notification with an Open inbox button")
    listing = sub.add_parser("list", help="Print pending video titles and links")
    listing.add_argument("--json", action="store_true")
    for name in ("watched", "skip", "restore"):
        action = sub.add_parser(name, help=f"{name.capitalize()} one or more video IDs")
        action.add_argument("ids", nargs="+")
    sub.add_parser("undo", help="Undo the latest status change")
    sub.add_parser("status", help="Print paths, channel errors and last check")
    args = parser.parse_args(argv)
    paths = core.Paths.discover()
    paths.ensure()
    try:
        if args.command in (None, "gui"):
            from gui import run
            return run(paths)
        if args.command == "check":
            try:
                result = core.run_check(paths, scheduled=args.scheduled)
            except core.Busy as exc:
                print(exc)
                return 1 if args.scheduled else 0
            print(f"{result['new']} new · {result['checked']} channels checked · {len(result['errors'])} errors")
            for error in result["errors"]:
                print(error, file=sys.stderr)
            # Send a partial digest if a channel is down, then retry only failed channels.
            notify_ok = launch_notification(paths) if args.scheduled else True
            return int(bool(result["errors"]) or not notify_ok)
        if args.command in ("notify", "test-notification"):
            return notification(paths, test=args.command == "test-notification")
        if args.command == "list":
            rows = core.queue_rows(paths)
            for row in rows:
                row["url"] = core.video_url(row["id"])
            if args.json:
                print(json.dumps(rows, indent=2, ensure_ascii=False))
            else:
                print(f"{len(rows)} pending videos\n")
                for row in rows:
                    print(f"{row['channel']} — {row['title']}\n{row['url']}\n")
        elif args.command in ("watched", "skip", "restore"):
            core.set_status(paths, args.ids, {"watched": "watched", "skip": "skipped", "restore": "pending"}[args.command])
        elif args.command == "undo":
            print("Restored the previous status." if core.undo(paths) else "Nothing to undo.")
        elif args.command == "status":
            db = core.connect(paths)
            try:
                print(f"Channels: {paths.channels}\nDatabase: {paths.database}\nDaily check: {core.settings(paths)['daily_time']} local time")
                stamp = core.meta_get(db, "last_success")
                print("Last complete check: " + (dt.datetime.fromtimestamp(float(stamp)).strftime("%Y-%m-%d %H:%M") if stamp else "Never"))
                print("Errors: " + (core.meta_get(db, "last_errors") or "None"))
            finally:
                db.close()
        return 0
    except core.Busy:
        return 0
    except (core.InboxError, OSError, ImportError) as exc:
        print(f"YouTube Inbox: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
