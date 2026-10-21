import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.error
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
        # Begin with an empty channel so these tests focus on a later new upload.
        self.feeds[C1] = feed(C1, [])
        self.check()
        self.feeds[C1] = feed(C1, [(V1, "Old upload", T0 - 86400), (V2, "New upload", T0 + 30)])
        self.check(T0 + 60)

    def test_baseline_and_id_dedup_survive_title_changes(self):
        self.assertEqual(self.check()["new"], 1)
        self.assertEqual(core.queue_rows(self.paths)[0]["id"], V1)
        core.set_status(self.paths, [V1], "watched")
        self.feeds[C1] = feed(C1, [(V1, "Old, renamed", T0 - 86400), (V2, "New upload", T0 + 30)])
        self.assertEqual(self.check(T0 + 60)["new"], 1)
        core.set_status(self.paths, [V2], "watched")
        self.feeds[C1] = feed(C1, [(V2, "New, renamed", T0 + 30)])
        self.assertEqual(self.check(T0 + 120)["new"], 0)
        self.assertEqual(core.queue_rows(self.paths), [])
        self.assertEqual(core.queue_rows(self.paths, "watched")[0]["title"], "New, renamed")

    def test_only_latest_existing_video_is_added_once(self):
        # Feed order is not necessarily publication order.
        videos = [(V1, "Oldest", T0 - 86400), (V2, "Latest", T0 - 60), (V3, "Middle", T0 - 3600)]
        self.feeds[C1] = feed(C1, videos)
        self.assertEqual(self.check()["new"], 1)
        self.assertEqual([r["id"] for r in core.queue_rows(self.paths)], [V2])
        core.set_status(self.paths, [V2], "skipped")
        self.assertEqual(self.check(T0 + 60)["new"], 0)
        # Neither a shorter feed nor removing/re-adding the channel loads older videos.
        self.feeds[C1] = feed(C1, [videos[0], videos[2]])
        self.assertEqual(self.check(T0 + 120)["new"], 0)
        self.paths.channels.write_text("")
        self.check(T0 + 180)
        self.paths.channels.write_text(C1)
        self.feeds[C1] = feed(C1, videos)
        self.assertEqual(self.check(T0 + 240)["new"], 0)
        self.assertFalse(core.queue_rows(self.paths))
        self.assertEqual(core.queue_rows(self.paths, "skipped")[0]["id"], V2)

    def legacy_channel(self, latest_status="baseline"):
        """Populate the persisted state written by 1.0.0/1.0.1."""
        db = core.connect(self.paths)
        with db:
            db.execute("INSERT INTO channels(id,name,added_at,last_check) VALUES (?,?,?,?)", (C1, "Existing channel", T0, T0))
            for vid, stamp, status in ((V1, T0 - 86400, "baseline"), (V2, T0 - 60, latest_status)):
                db.execute("INSERT INTO videos VALUES (?,?,?,?,?,?,?,?)", (vid, C1, "Existing upload", stamp, T0, status, 1, T0))
        db.close()
        self.feeds[C1] = feed(C1, [(V1, "Older", T0 - 86400), (V2, "Latest", T0 - 60)])

    def test_upgrade_adds_cached_latest_and_makes_it_eligible_for_digest(self):
        self.legacy_channel()
        self.assertFalse(core.queue_rows(self.paths))
        self.assertEqual(self.check(T0 + 60)["new"], 1)
        self.assertEqual([r["id"] for r in core.queue_rows(self.paths)], [V2])
        db = core.connect(self.paths)
        self.assertEqual([r["id"] for r in core.notify_candidates(db)], [V2])
        self.assertEqual(db.execute("SELECT status FROM videos WHERE id=?", (V1,)).fetchone()[0], "baseline")
        db.close()
        self.assertEqual(self.check(T0 + 120)["new"], 0)

    def test_upgrade_preserves_watched_or_skipped_latest_without_loading_older(self):
        for status in ("watched", "skipped"):
            with self.subTest(status=status):
                root = self.root / status
                self.paths = core.Paths(root / "config", root / "data", root / "state")
                self.paths.ensure()
                self.paths.channels.write_text(C1)
                self.legacy_channel(status)
                self.assertEqual(self.check(T0 + 60)["new"], 0)
                self.assertFalse(core.queue_rows(self.paths))
                self.assertEqual(core.queue_rows(self.paths, status)[0]["id"], V2)
                self.feeds[C1] = feed(C1, [(V1, "Older", T0 - 86400)])
                self.assertEqual(self.check(T0 + 120)["new"], 0)
                self.assertFalse(core.queue_rows(self.paths))

    def test_partial_fallback_waits_for_latest_before_starting_inbox(self):
        self.unavailable_feed()
        missing_date = {"id": V2, "channel_id": C1}
        with patch("core.ytdlp_json", side_effect=[self.upload_list([V2, V1]), missing_date, self.video_info(V1, T0 - 86400)]):
            result = self.check()
        self.assertTrue(result["errors"])
        self.assertEqual(result["new"], 0)
        self.assertFalse(core.queue_rows(self.paths))
        with patch("core.ytdlp_json", side_effect=[self.upload_list([V2, V1]), self.video_info(V2, T0 - 60)]):
            result = self.check(T0 + 60)
        self.assertFalse(result["errors"])
        self.assertEqual(result["new"], 1)
        self.assertEqual([r["id"] for r in core.queue_rows(self.paths)], [V2])

    def test_latest_video_is_selected_independently_per_channel(self):
        self.paths.channels.write_text(C1 + "\n" + C2)
        self.feeds[C2] = feed(C2, [(V2, "Older", T0 - 3600), (V3, "Latest", T0 - 60)])
        self.assertEqual(self.check()["new"], 2)
        self.assertEqual({r["id"] for r in core.queue_rows(self.paths)}, {V1, V3})
        self.assertEqual(self.check(T0 + 60)["new"], 0)

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

    def test_channel_browse_is_bounded_and_does_not_change_inbox_or_check_state(self):
        self.paths.channels.write_text(C1 + "\n" + C2)
        self.feeds[C1] = feed(C1, [(f"{i:011d}", f"Upload {i}", T0 - 1000 + i) for i in range(20)]
                             + [("f" * 11, "Future premiere", T0 + 86400)])
        self.feeds[C2] = feed(C2, [(V1, "Other channel", T0 - 10)])
        self.check(scheduled=True)
        core.set_status(self.paths, [f"{18:011d}"], "watched")
        core.set_status(self.paths, [f"{17:011d}"], "skipped")
        db = core.connect(self.paths)
        before = list(db.iterdump())
        try:
            self.assertEqual([c["id"] for c in core.channel_rows(self.paths)], [C1, C2])
            rows = core.recent_channel_rows(self.paths, C1, now=T0)
            self.assertEqual([r["id"] for r in rows], [f"{i:011d}" for i in range(19, 4, -1)])
            self.assertEqual({r["status"] for r in rows}, {"baseline", "pending", "watched", "skipped"})
            self.assertEqual({r["channel_id"] for r in rows}, {C1})
            self.assertEqual(list(db.iterdump()), before)
        finally:
            db.close()

    def test_adding_an_older_browsed_video_survives_checks_and_can_be_undone(self):
        self.add_new()
        self.assertEqual(core.recent_channel_rows(self.paths, C1, now=T0 + 60)[1]["status"], "baseline")
        core.set_status(self.paths, [V1], "pending")
        self.assertEqual(self.check(T0 + 90)["new"], 0)
        self.assertEqual({r["id"] for r in core.queue_rows(self.paths)}, {V1, V2})
        db = core.connect(self.paths)
        try:
            # An older upload chosen manually is not a newly discovered upload.
            self.assertEqual([r["id"] for r in core.notify_candidates(db)], [V2])
        finally:
            db.close()
        self.assertTrue(core.undo(self.paths))
        self.assertEqual([r["id"] for r in core.queue_rows(self.paths)], [V2])
        self.assertEqual(core.recent_channel_rows(self.paths, C1, now=T0 + 90)[1]["status"], "baseline")

    def test_browse_hides_removed_channels_and_preserves_readded_history(self):
        self.check()
        core.set_status(self.paths, [V1], "watched")
        self.paths.channels.write_text("")
        self.check(T0 + 60)
        self.assertFalse(core.channel_rows(self.paths))
        self.assertFalse(core.recent_channel_rows(self.paths, C1, now=T0 + 60))
        self.paths.channels.write_text(C1)
        self.check(T0 + 120)
        self.assertEqual([c["id"] for c in core.channel_rows(self.paths)], [C1])
        rows = core.recent_channel_rows(self.paths, C1, now=T0 + 120)
        self.assertEqual([(r["id"], r["status"]) for r in rows], [(V1, "watched")])
        self.assertFalse(core.queue_rows(self.paths))

    def test_failed_fetch_preserves_queue_and_retries_same_cycle(self):
        self.paths.channels.write_text(C1 + "\n" + C2 + "\n")
        self.check(scheduled=True)
        core.set_status(self.paths, [V1], "watched")
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

    def unavailable_feed(self, code=404):
        self.feeds[C1] = urllib.error.HTTPError(
            "https://www.youtube.com/feeds/videos.xml?channel_id=" + C1,
            code, "Feed unavailable", {}, None)

    def upload_list(self, ids):
        return {"id": "UU" + C1[2:], "channel_id": C1, "channel": "Test channel",
                "entries": [{"id": vid, "channel_id": C1, "title": "Upload " + vid} for vid in ids]}

    def video_info(self, vid, stamp):
        return {"id": vid, "channel_id": C1, "timestamp": stamp}

    def test_rss_failures_use_fallback_and_reuse_known_dates(self):
        self.add_new()
        core.set_status(self.paths, [V2], "watched")
        for code in (404, 500, 502, 503, 504):
            with self.subTest(code=code):
                self.unavailable_feed(code)
                with patch("core.ytdlp_json", return_value=self.upload_list([V2, V1])) as metadata:
                    result = self.check(T0 + 120)
                self.assertFalse(result["errors"])
                self.assertEqual(result["checked"], 1)
                self.assertEqual(result["new"], 0)
                self.assertEqual(metadata.call_count, 1)  # No video requests for known IDs.
                self.assertFalse(core.queue_rows(self.paths))
                self.assertEqual(core.queue_rows(self.paths, "watched")[0]["id"], V2)

    def test_fallback_adds_new_upload_and_rss_recovery_does_not_duplicate(self):
        self.add_new()
        core.set_status(self.paths, [V2], "skipped")
        self.unavailable_feed()
        with patch("core.ytdlp_json", side_effect=[self.upload_list([V3, V2, V1]), self.video_info(V3, T0 + 90)]):
            result = self.check(T0 + 120)
        self.assertFalse(result["errors"])
        self.assertEqual(result["new"], 1)
        self.assertEqual(core.queue_rows(self.paths)[0]["id"], V3)
        core.set_status(self.paths, [V3], "watched")
        self.feeds[C1] = feed(C1, [(V3, "Exact feed title", T0 + 90), (V2, "Skipped", T0 + 30)])
        with patch("core.ytdlp_json") as metadata:
            self.assertEqual(self.check(T0 + 180)["new"], 0)
            metadata.assert_not_called()
        self.assertFalse(core.queue_rows(self.paths))
        self.assertEqual(core.queue_rows(self.paths, "watched")[0]["id"], V3)
        self.assertEqual(core.queue_rows(self.paths, "skipped")[0]["id"], V2)

    def test_fallback_after_initial_failure_keeps_original_tracking_time(self):
        self.feeds[C1] = OSError("offline")
        self.check(T0)
        self.unavailable_feed()
        with patch("core.ytdlp_json", side_effect=[self.upload_list([V2, V1]),
                   self.video_info(V2, T0 + 60), self.video_info(V1, T0 - 86400)]):
            result = self.check(T0 + 3600)
        self.assertFalse(result["errors"])
        self.assertEqual(result["new"], 1)
        self.assertEqual([r["id"] for r in core.queue_rows(self.paths)], [V2])

    def test_incomplete_fallback_saves_progress_and_retries_missing_dates(self):
        self.check(scheduled=True)
        self.unavailable_feed()
        no_exact_time = {"id": V3, "channel_id": C1, "upload_date": "20261016"}
        with patch("core.ytdlp_json", side_effect=[self.upload_list([V3, V2, V1]),
                   no_exact_time, self.video_info(V2, T0 + 60)]):
            result = self.check(T0 + 86400, scheduled=True)
        self.assertEqual(result["new"], 1)
        self.assertEqual(result["checked"], 0)
        self.assertIn("Publication time unavailable", result["errors"][0])
        db = core.connect(self.paths)
        self.assertEqual(db.execute("SELECT last_check FROM channels WHERE id=?", (C1,)).fetchone()[0], T0)
        self.assertEqual(core.meta_get(db, "last_success"), str(T0))
        db.close()
        with patch("core.ytdlp_json", side_effect=[self.upload_list([V3, V2, V1]),
                   self.video_info(V3, T0 + 90)]) as metadata:
            result = self.check(T0 + 86460, scheduled=True)
        self.assertEqual(metadata.call_count, 2)
        self.assertFalse(result["errors"])
        self.assertEqual(result["new"], 1)
        self.assertEqual(result["checked"], 1)
        with patch("core.ytdlp_json") as metadata:
            self.check(T0 + 86520, scheduled=True)
            metadata.assert_not_called()

    def test_fallback_does_not_bypass_access_denials_or_rate_limits(self):
        self.add_new()
        for code in (401, 403, 429):
            with self.subTest(code=code):
                self.unavailable_feed(code)
                with patch("core.ytdlp_json") as metadata:
                    result = self.check(T0 + 120)
                    metadata.assert_not_called()
                self.assertTrue(result["errors"])
                self.assertEqual(core.queue_rows(self.paths)[0]["id"], V2)

    def test_fallback_rejects_wrong_channel_and_wrong_video_metadata(self):
        self.check()
        self.unavailable_feed()
        wrong_list = self.upload_list([V2])
        wrong_list["channel_id"] = C2
        cases = [[wrong_list],
                 [self.upload_list([V2]), {"id": V2, "channel_id": C2, "timestamp": T0 + 60}],
                 [self.upload_list([V2]), self.video_info(V3, T0 + 60)]]
        for responses in cases:
            with self.subTest(responses=responses), patch("core.ytdlp_json", side_effect=responses):
                result = self.check(T0 + 120)
                self.assertTrue(result["errors"])
                self.assertEqual([r["id"] for r in core.queue_rows(self.paths)], [V1])

    def test_fallback_time_budget_preserves_partial_results(self):
        with patch("core.time.monotonic", side_effect=[0, 1, core.FALLBACK_SECONDS + 1]), \
             patch("core.ytdlp_json", side_effect=[self.upload_list([V2, V3]), self.video_info(V2, T0 + 60)]):
            name, entries, errors = core.fallback_uploads(C1, {}, "Test channel")
        self.assertEqual([e["id"] for e in entries], [V2])
        self.assertIn("time limit", errors[0])

    def test_missing_or_failing_ytdlp_preserves_queue(self):
        self.add_new()
        self.unavailable_feed()
        with patch("core.shutil.which", return_value=None):
            result = self.check(T0 + 120)
        self.assertIn("Install yt-dlp", result["errors"][0])
        failures = [subprocess.TimeoutExpired("yt-dlp", 60),
                    subprocess.CompletedProcess([], 1, "", "Metadata unavailable"),
                    subprocess.CompletedProcess([], 0, "not JSON", "")]
        for failure in failures:
            with self.subTest(failure=failure), patch("core.shutil.which", return_value="yt-dlp"), \
                 patch("core.subprocess.run", side_effect=[failure]):
                result = self.check(T0 + 120)
                self.assertTrue(result["errors"])
                self.assertEqual(core.queue_rows(self.paths)[0]["id"], V2)

    def test_ytdlp_metadata_calls_do_not_download_media_or_load_user_config(self):
        output = subprocess.CompletedProcess([], 0, json.dumps(self.upload_list([])), "")
        with patch("core.shutil.which", return_value="yt-dlp"), patch("core.subprocess.run", return_value=output) as run:
            self.assertEqual(core.ytdlp_json("https://www.youtube.com/playlist?list=UU" + C1[2:], ["--flat-playlist"])["entries"], [])
        args = run.call_args.args[0]
        self.assertIn("--ignore-config", args)
        self.assertIn("--skip-download", args)
        self.assertIn("--", args)
        self.assertLessEqual(run.call_args.kwargs["timeout"], core.FALLBACK_SECONDS)

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
