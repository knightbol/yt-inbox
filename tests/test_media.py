import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
import media

C1 = "UC" + "a" * 22
V1, V2 = "a" * 11, "b" * 11


class MediaFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = core.Paths(self.root / "config", self.root / "data", self.root / "state")
        self.paths.ensure()
        db = core.connect(self.paths)
        with db:
            db.execute("INSERT INTO channels(id,name,added_at) VALUES (?,?,?)", (C1, "Test channel", 100))
        core.ingest(db, C1, "Test channel", [
            {"id": V1, "title": "First video", "published": 200},
            {"id": V2, "title": "Second video", "published": 300}], 400, cycle="original cycle")
        db.close()

    def tearDown(self):
        self.temp.cleanup()

    def playlist(self, start, stop):
        return {"id": "UU" + C1[2:], "channel_id": C1,
                "entries": [{"id": f"{i:011d}", "title": f"Upload {i}", "channel_id": C1} for i in range(start, stop)]}

    def load(self, start, stop, restart=False):
        with patch("core.ytdlp_json", return_value=self.playlist(start, stop)):
            return media.load_catalogue(self.paths, C1, restart)

    def file(self, video_id=V1):
        directory = media.download_directory(self.paths, video_id)
        directory.mkdir(parents=True, exist_ok=True)
        file = directory / "media.mkv"
        file.write_bytes(b"complete fixture")
        return file


class MediaTests(MediaFixture, unittest.TestCase):
    def test_catalogue_paging_has_no_total_cap_and_leaves_daily_state_unchanged(self):
        before = core.queue_rows(self.paths)
        self.load(0, 100)
        with patch("core.ytdlp_json", return_value=self.playlist(100, 123)) as lookup:
            media.load_catalogue(self.paths, C1)
        self.assertIn("101:200", lookup.call_args.args[1])
        rows, total, page = media.browse_page(self.paths, C1, page=2)
        self.assertEqual((total, page, len(rows)), (123, 2, 23))
        self.assertEqual(rows[-1]["id"], f"{122:011d}")
        self.assertEqual(rows[-1]["published"], 0)
        self.assertEqual(media.browse_page(self.paths, C1, "Upload 122")[1], 1)
        self.assertFalse(media.catalogue_state(self.paths, C1)["exhausted"])
        self.assertTrue(self.load(123, 123)["exhausted"])
        self.assertEqual(core.queue_rows(self.paths), before)
        db = core.connect(self.paths)
        self.assertEqual(db.execute("SELECT last_cycle FROM channels WHERE id=?", (C1,)).fetchone()[0], "original cycle")
        self.assertEqual(len(core.notify_candidates(db)), 2)
        db.close()

    def test_failed_or_wrong_owner_page_does_not_advance_cursor_or_lose_cache(self):
        self.load(0, 100)
        before = media.catalogue_state(self.paths, C1)
        bad = self.playlist(100, 103)
        bad["entries"][-1]["channel_id"] = "UC" + "z" * 22
        for outcome in (bad, core.InboxError("Network failed")):
            with patch("core.ytdlp_json", side_effect=[outcome]), self.assertRaises(core.InboxError):
                media.load_catalogue(self.paths, C1)
            self.assertEqual(media.catalogue_state(self.paths, C1), before)
            self.assertEqual(media.browse_page(self.paths, C1)[1], 100)

    def test_catalogue_refresh_preserves_choices_and_download_records(self):
        self.load(0, 100)
        chosen = f"{5:011d}"
        core.set_status(self.paths, [chosen], "watched")
        media.queue_downloads(self.paths, [chosen])
        file = self.file(chosen)
        media.finish_download(self.paths, chosen, file)
        self.load(0, 100, restart=True)
        rows, _, _ = media.browse_page(self.paths, C1, "Upload 5")
        self.assertEqual(next(r for r in rows if r["id"] == chosen)["status"], "watched")
        self.assertEqual(media.local_file(self.paths, chosen), file)
        self.assertEqual(media.catalogue_state(self.paths, C1)["generation"], 2)

    def test_archive_choice_undo_then_real_date_preserves_new_upload_tracking(self):
        self.load(0, 1)
        chosen = "0" * 11
        core.set_status(self.paths, [chosen], "pending")
        self.assertTrue(core.undo(self.paths))
        db = core.connect(self.paths)
        self.assertEqual(core.ingest(db, C1, "Test channel", [
            {"id": chosen, "title": "Actually new", "published": 500}], 600), 1)
        self.assertIn(chosen, {r["id"] for r in core.notify_candidates(db)})
        db.close()
        core.set_status(self.paths, [chosen], "watched")
        db = core.connect(self.paths)
        core.ingest(db, C1, "Test channel", [{"id": chosen, "title": "Renamed", "published": 500}], 700)
        db.close()
        self.assertEqual(core.queue_rows(self.paths, "watched")[0]["id"], chosen)

    def test_fallback_fetches_exact_date_for_archive_placeholder(self):
        self.load(0, 1)
        chosen = "0" * 11
        core.set_status(self.paths, [chosen], "watched")
        details = {"id": chosen, "channel_id": C1, "timestamp": 500}
        with patch("core.ytdlp_json", side_effect=[self.playlist(0, 1), details]) as lookup:
            _, rows, errors = core.fallback_uploads(C1, {chosen: {"published": 0}}, "Test")
        self.assertEqual(lookup.call_count, 2)
        self.assertEqual(rows[0]["published"], 500)
        self.assertFalse(errors)

    def test_download_queue_deduplicates_resumes_and_preserves_watched_state(self):
        core.set_status(self.paths, [V1], "watched")
        self.assertEqual(media.queue_downloads(self.paths, [V1, V1, V2]), 2)
        self.assertEqual(media.queue_downloads(self.paths, [V1, V2]), 0)
        media.update_download(self.paths, V1, "downloading")
        media.update_download(self.paths, V2, "failed", error="offline")
        media.recover_downloads(self.paths)
        records = media.download_records(self.paths)
        self.assertEqual(records[V1]["state"], "queued")
        self.assertEqual(records[V2]["state"], "failed")
        media.finish_download(self.paths, V1, self.file())
        self.assertEqual(media.queue_downloads(self.paths, [V1]), 0)
        self.assertEqual(core.queue_rows(self.paths, "watched")[0]["id"], V1)
        media.local_file(self.paths, V1).unlink()
        self.assertEqual(media.queue_downloads(self.paths, [V1]), 1)

    def test_completed_file_validation_and_removal_are_confined_to_app_downloads(self):
        media.queue_downloads(self.paths, [V1])
        outside = self.root / "do-not-remove.mkv"
        outside.write_bytes(b"unrelated file")
        directory = media.download_directory(self.paths, V1)
        directory.mkdir(parents=True)
        partial = directory / "media.mkv.part"
        partial.write_bytes(b"partial")
        link = directory / "media.mkv"
        link.symlink_to(outside)
        for candidate in (outside, partial, link):
            with self.assertRaises(core.InboxError):
                media.finish_download(self.paths, V1, candidate)
        with self.assertRaises(core.InboxError):
            media.remove_download(self.paths, V1)
        media.update_download(self.paths, V1, "cancelled")
        media.remove_download(self.paths, V1)
        self.assertTrue(outside.exists())
        self.assertFalse(directory.exists())
        self.assertEqual(len(core.queue_rows(self.paths)), 2)

    def test_download_command_is_single_video_bounded_quality_and_resumable(self):
        with patch("media.shutil.which", side_effect=lambda name: "/usr/bin/" + name):
            command = media.download_command(self.paths, V1)
        self.assertEqual(command[0], "/usr/bin/flock")
        self.assertIn("--ignore-config", command)
        self.assertIn("--no-playlist", command)
        self.assertIn("--continue", command)
        self.assertIn("--abort-on-unavailable-fragments", command)
        self.assertEqual(command[command.index("--format") + 1], "bv*[height<=720]+ba/b[height<=720]")
        self.assertEqual(command[-2:], ["--", core.video_url(V1)])


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from transfers import DownloadQueue
    QT = True
except ImportError:
    QT = False


@unittest.skipUnless(QT, "PySide6 is not installed")
class TransferTests(MediaFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.script = self.root / "fake-download.py"
        self.script.write_text('''import json, pathlib, sys, time
mode, folder = sys.argv[1:]
directory = pathlib.Path(folder)
directory.mkdir(parents=True, exist_ok=True)
if mode == "fail":
    print("fixture network failure", file=sys.stderr)
    sys.exit(2)
print("YT_INBOX_PROGRESS:25.0%", flush=True)
if mode == "wait":
    (directory / "media.mkv.part").write_bytes(b"partial")
    time.sleep(30)
file = directory / "media.mkv"
file.write_bytes(b"complete fixture")
print("YT_INBOX_FILE:" + json.dumps(str(file)), flush=True)
''')
        self.queue = DownloadQueue(self.paths)
        self.app.processEvents()

    def tearDown(self):
        self.queue.cancel(list(media.download_records(self.paths)))
        self.wait_for(lambda: self.queue.process is None)
        self.queue.deleteLater()
        self.app.processEvents()
        super().tearDown()

    def wait_for(self, condition):
        deadline = time.monotonic() + 5
        while not condition() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(condition())

    def command(self, video_id, mode):
        return [sys.executable, str(self.script), mode, str(media.download_directory(self.paths, video_id))]

    def test_failed_download_does_not_block_next_video_and_only_success_is_ready(self):
        media.queue_downloads(self.paths, [V1, V2])
        with patch("media.download_command", side_effect=lambda paths, vid: self.command(vid, "fail" if vid == V1 else "success")):
            self.queue.start_next()
            self.wait_for(lambda: media.download_records(self.paths)[V2]["state"] == "complete")
        self.assertEqual(media.download_records(self.paths)[V1]["state"], "failed")
        self.assertIn("fixture network failure", media.download_records(self.paths)[V1]["error"])
        self.assertIsNone(media.local_file(self.paths, V1))
        self.assertTrue(media.local_file(self.paths, V2).exists())
        self.assertEqual(len(core.queue_rows(self.paths)), 2)

    def test_cancel_active_and_queued_downloads_keeps_partial_file_for_retry(self):
        media.queue_downloads(self.paths, [V1, V2])
        with patch("media.download_command", side_effect=lambda paths, vid: self.command(vid, "wait")):
            self.queue.start_next()
            partial = media.download_directory(self.paths, V1) / "media.mkv.part"
            self.wait_for(partial.exists)
            self.queue.cancel([V1, V2])
            self.wait_for(lambda: self.queue.process is None)
        self.assertEqual({r["state"] for r in media.download_records(self.paths).values()}, {"cancelled"})
        self.assertTrue(partial.exists())
        self.assertIsNone(media.local_file(self.paths, V1))
        self.assertEqual(media.queue_downloads(self.paths, [V1]), 1)
        with patch("media.download_command", side_effect=lambda paths, vid: self.command(vid, "success")):
            self.queue.start_next()
            self.wait_for(lambda: media.download_records(self.paths)[V1]["state"] == "complete")


if __name__ == "__main__":
    unittest.main()
