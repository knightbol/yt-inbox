import datetime as dt
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli
import core
import install

C1 = "UC" + "a" * 22
C2 = "UC" + "b" * 22
V1 = "a" * 11
V2 = "b" * 11
V3 = "c" * 11
T0 = dt.datetime(2026, 10, 15, 19, 0, tzinfo=dt.timezone.utc).timestamp()


def feed(cid, videos, name="Test channel"):
    items = "".join(f"<entry><yt:videoId>{vid}</yt:videoId><title>{escape(title)}</title><published>{dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat()}</published></entry>" for vid, title, stamp in videos)
    return f'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"><yt:channelId>{cid}</yt:channelId><title>{escape(name)}</title>{items}</feed>'.encode()


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = core.Paths(self.root / "config", self.root / "data", self.root / "state")
        self.paths.ensure()
        self.paths.channels.write_text(C1 + "\n")
        self.feeds = {C1: feed(C1, [(V1, "Old upload", T0 - 86400)]), C2: feed(C2, [])}
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def fetch(self, url):
        cid = url.split("channel_id=", 1)[1]
        self.calls.append(cid)
        value = self.feeds[cid]
        if isinstance(value, Exception):
            raise value
        return value

    def check(self, now=T0, scheduled=False):
        return core.run_check(self.paths, now=now, scheduled=scheduled, fetcher=self.fetch)

    def add_new(self):
        self.check()
        self.feeds[C1] = feed(C1, [(V1, "Old upload", T0 - 86400), (V2, "New upload", T0 + 30)])
        self.check(T0 + 60)

    def test_baseline_and_id_dedup_survive_title_changes(self):
        self.assertEqual(self.check()["new"], 0)
        self.assertEqual(core.queue_rows(self.paths), [])
        self.feeds[C1] = feed(C1, [(V1, "Old, renamed", T0 - 86400), (V2, "New upload", T0 + 30)])
        self.assertEqual(self.check(T0 + 60)["new"], 1)
        core.set_status(self.paths, [V2], "watched")
        self.feeds[C1] = feed(C1, [(V2, "New, renamed", T0 + 30)])
        self.assertEqual(self.check(T0 + 120)["new"], 0)
        self.assertEqual(core.queue_rows(self.paths), [])
        self.assertEqual(core.queue_rows(self.paths, "watched")[0]["title"], "New, renamed")

    def test_status_undo_is_durable(self):
        self.add_new()
        core.set_status(self.paths, [V2], "skipped")
        self.assertFalse(core.queue_rows(self.paths))
        self.assertEqual(core.queue_rows(self.paths, "skipped")[0]["id"], V2)
        self.assertTrue(core.undo(self.paths))
        self.assertEqual(core.queue_rows(self.paths)[0]["id"], V2)
        self.assertFalse(core.undo(self.paths))
        core.set_status(self.paths, [V2], "watched")
        core.set_status(self.paths, [V2], "pending")
        self.assertTrue(core.undo(self.paths))
        self.assertTrue(core.queue_rows(self.paths, "watched"))

    def test_failed_fetch_preserves_queue_and_retries_same_cycle(self):
        self.paths.channels.write_text(C1 + "\n" + C2 + "\n")
        self.check(scheduled=True)
        self.feeds[C1] = feed(C1, [(V2, "New", T0 + 60)])
        self.feeds[C2] = OSError("offline")
        result = self.check(T0 + 86400, scheduled=True)
        self.assertEqual(result["new"], 1)
        self.assertEqual(len(result["errors"]), 1)
        self.calls.clear()
        result = self.check(T0 + 86460, scheduled=True)
        self.assertEqual(self.calls, [C2])
        self.assertEqual(len(core.queue_rows(self.paths)), 1)
        self.feeds[C2] = feed(C2, [(V3, "Recovered", T0 + 86430)])
        self.assertEqual(self.check(T0 + 86520, scheduled=True)["new"], 1)
        self.calls.clear()
        self.check(T0 + 86580, scheduled=True)
        self.assertEqual(self.calls, [])

    def test_failed_initial_check_keeps_original_start_time(self):
        self.feeds[C1] = OSError("network unavailable")
        self.assertTrue(self.check()["errors"])
        self.feeds[C1] = feed(C1, [(V2, "Published during outage", T0 + 60)])
        self.assertEqual(self.check(T0 + 3600)["new"], 1)

    def test_remove_readd_preserves_watched_history(self):
        self.add_new()
        core.set_status(self.paths, [V2], "watched")
        self.paths.channels.write_text("# empty\n")
        self.check(T0 + 120)
        self.assertFalse(core.queue_rows(self.paths, "watched"))
        self.paths.channels.write_text(C1 + "\n")
        self.assertEqual(self.check(T0 + 180)["new"], 0)
        self.assertEqual(core.queue_rows(self.paths, "watched")[0]["id"], V2)

    def test_invalid_channels_do_not_disable_existing_subscriptions(self):
        self.add_new()
        self.paths.channels.write_text("https://example.com/invalid\n")
        self.assertTrue(self.check()["errors"])
        self.assertEqual(len(core.queue_rows(self.paths)), 1)

    def test_aliases_share_video_history_and_resolve_once(self):
        self.paths.channels.write_text("@Alpha\n" + C1 + "\n")
        with patch("core.resolve_channel"):
            resolver = unittest.mock.Mock(return_value=C1)
            core.run_check(self.paths, now=T0, fetcher=self.fetch, resolver=resolver)
            core.run_check(self.paths, now=T0 + 60, fetcher=self.fetch, resolver=resolver)
            self.assertEqual(resolver.call_count, 2)  # Each of two uncached sources once.
        self.assertEqual(self.calls, [C1, C1])

    def test_channel_link_validation(self):
        urls, errors = core.parse_channels("# comment\n@Example\nhttps://youtube.com/@Example/videos?x=1\n" + C1)
        self.assertFalse(errors)
        self.assertEqual(len(urls), 2)
        for value in ("https://evil.example/@test", "https://youtube.com/watch?v=123", "file:///etc/passwd", "https://youtube.com/playlist?list=xx", "https://youtube.com/@example/../../watch", "https://youtube.com@evil.example/@x"):
            with self.assertRaises(core.InboxError):
                core.canonical_channel(value)

    def test_feed_validation_and_escaped_titles(self):
        name, rows = core.parse_feed(feed(C1, [(V2, '<script> & "quote"', T0)], name="A & B"), C1)
        self.assertEqual(name, "A & B")
        self.assertEqual(rows[0]["title"], '<script> & "quote"')
        # Verified against a live channel feed: root channelId omits the UC prefix.
        raw = feed(C1, [(V2, "Short root ID", T0)]).replace(
            f"<yt:channelId>{C1}</yt:channelId>".encode(),
            f"<yt:channelId>{C1[2:]}</yt:channelId>".encode())
        self.assertEqual(core.parse_feed(raw, C1)[1][0]["id"], V2)
        for raw in (b"<html>Blocked</html>", b"broken", feed(C2, []), feed(C1, [("invalid", "Bad", T0)])):
            with self.assertRaises(core.InboxError):
                core.parse_feed(raw, C1)

    def test_lock_prevents_overlapping_checks(self):
        with core.exclusive_lock(self.paths.state / "check.lock"):
            with self.assertRaises(core.Busy):
                self.check()

    def test_daily_cycle_uses_local_wall_time(self):
        before = dt.datetime(2026, 10, 15, 18, 59).timestamp()
        after = dt.datetime(2026, 10, 15, 19, 0).timestamp()
        self.assertEqual(core.cycle_key("19:00", before), "2026-10-14")
        self.assertEqual(core.cycle_key("19:00", after), "2026-10-15")

    def test_notification_records_delivery_and_action_once(self):
        self.add_new()
        bindir = self.root / "bin"
        bindir.mkdir()
        notifier = bindir / "notify-send"
        notifier.write_text("#!/bin/sh\nprintf '123\\ndefault\\n'\n")
        notifier.chmod(0o755)
        with patch.dict(os.environ, {"PATH": str(bindir) + os.pathsep + os.environ["PATH"]}):
            with patch("cli.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                self.assertEqual(cli.notification(self.paths), 0)
                run.assert_called_once()
                self.assertIn("yt-inbox-gui.service", run.call_args.args[0])
                self.assertEqual(cli.notification(self.paths), 0)
                run.assert_called_once()
        db = core.connect(self.paths)
        try:
            self.assertEqual(core.meta_get(db, "last_digest_day"), dt.date.today().isoformat())
            self.assertFalse(core.notify_candidates(db))
        finally:
            db.close()

    def test_failed_notification_does_not_consume_digest(self):
        self.add_new()
        bindir = self.root / "bin"
        bindir.mkdir()
        notifier = bindir / "notify-send"
        notifier.write_text("#!/bin/sh\nprintf 'notification daemon unavailable\\n' >&2\nexit 1\n")
        notifier.chmod(0o755)
        with patch.dict(os.environ, {"PATH": str(bindir) + os.pathsep + os.environ["PATH"]}):
            with self.assertRaises(core.InboxError):
                cli.notification(self.paths)
        db = core.connect(self.paths)
        try:
            self.assertFalse(core.meta_get(db, "last_digest_day"))
            self.assertEqual(len(core.notify_candidates(db)), 1)
        finally:
            db.close()

    def test_staged_install_and_reinstall_preserve_data(self):
        self.add_new()
        core.set_status(self.paths, [V2], "watched")
        with patch.dict(os.environ, {"YT_INBOX_HOME": str(self.root)}):
            launcher = install.install(self.paths, enable=False)
            result = subprocess.run([str(launcher), "list", "--json"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "[]")
            install.install(self.paths, enable=False)
            self.assertEqual(core.queue_rows(self.paths, "watched")[0]["id"], V2)
            unit = (self.root / "systemd/user/yt-inbox-check.timer").read_text()
            self.assertIn("19:00:00", unit)
            self.assertIn("Persistent=true", unit)


if __name__ == "__main__":
    unittest.main()
