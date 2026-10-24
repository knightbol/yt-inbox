from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cli
import core
import media
import video_types

CID = "UC" + "s" * 22
NORMAL, SHORT, UNKNOWN = "a" * 11, "b" * 11, "c" * 11


class ShortsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.paths = core.Paths(root / "config", root / "data", root / "state")
        self.paths.ensure()
        self.paths.channels.write_text(CID)
        db = core.connect(self.paths)
        with db:
            db.execute("INSERT INTO channels(id,name,added_at) VALUES (?,?,?)", (CID, "A channel", 1000))
        db.close()

    def tearDown(self):
        self.temp.cleanup()

    def set_filter(self, enabled):
        values = core.settings(self.paths)
        values["hide_shorts"] = enabled
        core.save_settings(self.paths, values)

    def add_videos(self):
        db = core.connect(self.paths)
        core.ingest(db, CID, "A channel", [
            {"id": NORMAL, "title": "A 20-second regular video", "published": 1100, "is_short": False},
            {"id": SHORT, "title": "A three-minute Short", "published": 1200, "is_short": True},
            {"id": UNKNOWN, "title": "Type not checked yet", "published": 1300}], 1400, complete=False)
        db.close()

    def test_filter_defaults_on_for_new_and_legacy_settings(self):
        self.assertTrue(core.hide_shorts(self.paths))
        core.save_settings(self.paths, {"daily_time": "19:00", "player": "mpv"})
        self.assertTrue(core.hide_shorts(self.paths))
        self.set_filter(False)
        self.assertFalse(core.hide_shorts(self.paths))
        self.set_filter("false")
        with self.assertRaises(core.InboxError):
            core.settings(self.paths)

    def test_all_lists_counts_notifications_and_queue_respect_reversible_filter(self):
        self.add_videos()
        ids = [NORMAL, SHORT, UNKNOWN]
        db = core.connect(self.paths)
        with db:
            db.executemany("INSERT INTO catalogue VALUES (?,?,?,?,?)", [(vid, CID, vid, i, 1) for i, vid in enumerate(ids)])
            db.execute("INSERT INTO catalogue_state VALUES (?,?,?,?,?)", (CID, 101, 0, 1, 1400))
        before = [tuple(r) for r in db.execute("SELECT * FROM videos ORDER BY id")]
        self.assertEqual(core.pending_count(db, True), 1)
        self.assertEqual([r["id"] for r in core.notify_candidates(db, True)], [NORMAL])
        db.close()
        media.queue_downloads(self.paths, ids)
        directory = media.download_directory(self.paths, SHORT)
        directory.mkdir(parents=True)
        file = directory / "media.mkv"
        file.write_bytes(b"existing offline copy")
        media.finish_download(self.paths, SHORT, file)
        self.assertEqual([r["id"] for r in core.queue_rows(self.paths)], [NORMAL])
        self.assertEqual([r["id"] for r in core.recent_channel_rows(self.paths, CID)], [NORMAL])
        self.assertEqual(media.browse_page(self.paths, CID)[1], 1)
        self.assertEqual([r["id"] for r in media.download_rows(self.paths)], [NORMAL])
        self.assertEqual(media.next_download(self.paths), NORMAL)
        self.set_filter(False)
        self.assertEqual({r["id"] for r in core.queue_rows(self.paths)}, set(ids))
        self.assertEqual(media.browse_page(self.paths, CID)[1], 3)
        self.assertEqual({r["id"] for r in media.download_rows(self.paths)}, set(ids))
        self.assertEqual(media.local_file(self.paths, SHORT), file)
        db = core.connect(self.paths)
        self.assertEqual([tuple(r) for r in db.execute("SELECT * FROM videos ORDER BY id")], before)
        self.assertEqual(db.execute("SELECT count(*) FROM actions").fetchone()[0], 0)
        db.close()

    def test_first_check_seeds_latest_non_short_and_reuses_classification(self):
        entries = [{"id": NORMAL, "title": "Regular upload", "published": 800},
                   {"id": SHORT, "title": "Newer Short", "published": 900}]
        with patch("core.read_uploads", side_effect=lambda *args: ("A channel", [dict(e) for e in entries], [])), \
             patch("video_types.inspect_type", side_effect=lambda vid, cid, timeout: vid == SHORT) as inspect:
            first = core.run_check(self.paths, now=1000)
            self.assertFalse(first["errors"])
            self.assertEqual(first["new"], 1)
            self.assertEqual([r["id"] for r in core.queue_rows(self.paths)], [NORMAL])
            self.assertEqual(inspect.call_count, 2)
            core.set_status(self.paths, [NORMAL], "watched")
            self.assertEqual(core.run_check(self.paths, now=1100)["new"], 0)
            self.assertEqual(inspect.call_count, 2)
        self.assertFalse(core.queue_rows(self.paths))
        self.assertEqual(core.queue_rows(self.paths, "watched")[0]["id"], NORMAL)

    def test_migration_from_short_starting_video_can_seed_regular_video_once(self):
        entries = [{"id": NORMAL, "title": "Regular", "published": 800, "is_short": False},
                   {"id": SHORT, "title": "Short", "published": 900, "is_short": True}]
        db = core.connect(self.paths)
        core.ingest(db, CID, "A channel", entries, 1000)
        self.assertEqual(core.ingest(db, CID, "A channel", entries, 1100, filter_shorts=True), 1)
        db.close()
        core.set_status(self.paths, [NORMAL], "skipped")
        db = core.connect(self.paths)
        self.assertEqual(core.ingest(db, CID, "A channel", entries, 1200, filter_shorts=True), 0)
        db.close()
        self.assertEqual(core.queue_rows(self.paths, "skipped")[0]["id"], NORMAL)

    def test_type_failure_stays_hidden_and_manual_check_can_retry_early(self):
        self.add_videos()
        entries = [{"id": UNKNOWN}]
        with patch("video_types.inspect_type", side_effect=core.InboxError("HTTP 429")) as inspect:
            self.assertTrue(video_types.classify_entries(self.paths, CID, entries))
            self.assertTrue(video_types.classify_entries(self.paths, CID, entries))
            self.assertEqual(inspect.call_count, 1)
        self.assertEqual(video_types.pending_count(self.paths), 1)
        self.assertEqual(video_types.pending_count(self.paths, ready_only=True), 0)
        self.assertEqual([r["id"] for r in core.queue_rows(self.paths)], [NORMAL])
        with patch("video_types.inspect_type", return_value=False):
            self.assertFalse(video_types.classify_entries(self.paths, CID, entries, force=True))
        self.assertEqual(video_types.pending_count(self.paths), 0)
        self.assertEqual({r["id"] for r in core.queue_rows(self.paths)}, {NORMAL, UNKNOWN})

    def test_short_only_inbox_does_not_start_daily_notification(self):
        self.add_videos()
        core.set_status(self.paths, [NORMAL], "watched")
        with patch("cli.subprocess.run") as launch:
            self.assertTrue(cli.launch_notification(self.paths))
        launch.assert_not_called()
        db = core.connect(self.paths)
        self.assertFalse(core.meta_get(db, "last_digest_day"))
        db.close()

    def test_background_classifies_old_cache_without_changing_status(self):
        self.add_videos()
        core.set_status(self.paths, [UNKNOWN], "watched")
        with patch("video_types.inspect_type", return_value=False) as inspect:
            self.assertEqual(video_types.classify_saved(self.paths, stop=lambda: True), 0)
            inspect.assert_not_called()
            self.assertEqual(video_types.classify_saved(self.paths), 1)
            self.assertEqual(video_types.classify_saved(self.paths), 0)
            inspect.assert_called_once()
        self.assertEqual(core.queue_rows(self.paths, "watched")[0]["id"], UNKNOWN)

    def test_type_uses_metadata_not_duration_title_or_watch_url(self):
        self.assertIs(core.short_from_metadata({"media_type": "short", "duration": 180}), True)
        self.assertIs(core.short_from_metadata({"media_type": "video", "title": "#shorts", "duration": 10}), False)
        self.assertIs(core.short_from_metadata({"media_type": "livestream"}), False)
        self.assertIsNone(core.short_from_metadata({"url": core.video_url(SHORT), "duration": 10, "title": "#shorts"}))
        self.assertIs(core.short_from_metadata({"url": "https://www.youtube.com/shorts/" + SHORT}), True)
        self.assertIsNone(core.short_from_metadata({"url": "https://example.com/shorts/" + SHORT}))
        with patch("core.ytdlp_json", return_value={"id": NORMAL, "channel_id": "UC" + "z" * 22, "media_type": "video"}):
            with self.assertRaises(core.InboxError):
                video_types.inspect_type(NORMAL, CID)
        with patch("core.ytdlp_json", return_value={"id": NORMAL, "channel_id": CID}):
            with self.assertRaisesRegex(core.InboxError, "Update yt-dlp"):
                video_types.inspect_type(NORMAL, CID)

    def test_catalogue_filter_runs_before_page_limit_and_search(self):
        db = core.connect(self.paths)
        with db:
            db.executemany("INSERT INTO catalogue VALUES (?,?,?,?,?)", [
                (f"{i:011d}", CID, f"Entry {i}", i, 1) for i in range(120)])
            db.execute("INSERT INTO catalogue_state VALUES (?,?,?,?,?)", (CID, 201, 0, 1, 1400))
            for i in range(120):
                core.record_video_type(db, f"{i:011d}", i % 2 == 0)
        db.close()
        first, total, _ = media.browse_page(self.paths, CID)
        second, _, _ = media.browse_page(self.paths, CID, page=1)
        self.assertEqual((len(first), len(second), total), (50, 10, 60))
        self.assertEqual(media.browse_page(self.paths, CID, "Entry 118")[1], 0)
        self.assertEqual(media.browse_page(self.paths, CID, "Entry 119")[1], 1)


if __name__ == "__main__":
    unittest.main()
