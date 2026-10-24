import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
import media

try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from PySide6.QtCore import Qt
    import gui
    QT = True
except ImportError:
    QT = False


@unittest.skipUnless(QT, "PySide6 is not installed")
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.paths = core.Paths(root / "config", root / "data", root / "state")
        self.paths.ensure()
        core.save_settings(self.paths, {"daily_time": "19:00", "player": "mpv", "hide_shorts": False})
        db = core.connect(self.paths)
        cid = "UC" + "x" * 22
        with db:
            db.execute("INSERT INTO channels(id,name,added_at) VALUES (?,?,?)", (cid, "A channel", 0))
        core.ingest(db, cid, "A channel", [{"id": "x" * 11, "title": "Example upload", "published": 100}, {"id": "y" * 11, "title": "Another upload", "published": 200}], 300)
        db.close()
        self.window = gui.Window(self.paths)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def test_select_watch_restore_and_filter(self):
        w = self.window
        self.assertEqual(w.table.rowCount(), 2)
        self.assertFalse(w.play_button.isEnabled())
        w.table.selectRow(0)
        self.assertTrue(w.play_button.isEnabled())
        self.assertIn("watch?v=", w.link.text())
        QTest.mouseClick(w.watched_button, Qt.MouseButton.LeftButton)
        self.assertEqual(w.table.rowCount(), 1)
        w.view.setCurrentIndex(1)
        self.assertEqual(w.table.rowCount(), 1)
        w.table.selectRow(0)
        QTest.mouseClick(w.restore_button, Qt.MouseButton.LeftButton)
        self.assertEqual(w.table.rowCount(), 0)
        w.view.setCurrentIndex(0)
        self.assertEqual(w.table.rowCount(), 2)
        w.search.setText("Another")
        self.assertEqual(w.table.rowCount(), 1)
        w.search.setText("no such title")
        self.assertEqual(w.table.rowCount(), 0)
        self.assertTrue(w.empty.isVisible())

    def test_channel_editor_rejects_bad_input_without_overwriting(self):
        before = self.paths.channels.read_text()
        dialog = gui.ChannelsDialog(self.paths, self.window)
        dialog.editor.setPlainText("https://example.com/not-a-channel")
        dialog.save()
        self.assertEqual(self.paths.channels.read_text(), before)
        self.assertIn("Line 1", dialog.error.text())
        dialog.editor.setPlainText("@Example\n")
        dialog.save()
        self.assertEqual(self.paths.channels.read_text(), "@Example\n")
        dialog.deleteLater()

    def add_browse_channel(self):
        cid = "UC" + "b" * 22
        db = core.connect(self.paths)
        with db:
            db.execute("INSERT INTO channels(id,name,added_at) VALUES (?,?,?)", (cid, "B channel", 1000))
        core.ingest(db, cid, "B channel", [
            {"id": f"{i:011d}", "title": f"Earlier upload {i:02d}", "published": 300 + i}
            for i in range(20)], 1100)
        db.close()
        core.set_status(self.paths, [f"{18:011d}"], "watched")
        core.set_status(self.paths, [f"{17:011d}"], "skipped")
        return cid

    def test_browse_play_add_undo_and_filter_without_resetting_history(self):
        cid = self.add_browse_channel()
        w = self.window
        w.view.setCurrentIndex(w.view.findData("browse"))
        w.browse_channel.setCurrentIndex(w.browse_channel.findData(cid))
        self.assertTrue(w.browse_channel.isVisible())
        self.assertEqual(w.table.rowCount(), 20)
        self.assertEqual([w.table.item(i, 3).text() for i in range(4)],
                         ["In inbox", "Watched", "Skipped", "Not in inbox"])
        w.table.selectRow(3)
        older_id = w.selected()[0]["id"]
        self.assertEqual(w.restore_button.text(), "Add to inbox")
        self.assertTrue(w.restore_button.isEnabled())
        with patch("gui.QDesktopServices.openUrl", return_value=True) as open_url:
            QTest.mouseClick(w.browser_button, Qt.MouseButton.LeftButton)
        self.assertEqual(open_url.call_args.args[0].toString(), core.video_url(older_id))
        self.assertEqual(core.recent_channel_rows(self.paths, cid)[3]["status"], "baseline")
        QTest.mouseClick(w.restore_button, Qt.MouseButton.LeftButton)
        self.assertIn(older_id, {r["id"] for r in core.queue_rows(self.paths)})
        self.assertEqual(w.table.rowCount(), 20)
        self.assertEqual(w.table.item(3, 3).text(), "In inbox")
        self.assertFalse(w.restore_button.isEnabled())
        self.assertEqual(w.table.item(1, 3).text(), "Watched")
        self.assertEqual(w.table.item(2, 3).text(), "Skipped")
        w.undo()
        self.assertEqual(w.table.item(3, 3).text(), "Not in inbox")
        self.assertNotIn(older_id, {r["id"] for r in core.queue_rows(self.paths)})
        QTest.mouseClick(w.watched_button, Qt.MouseButton.LeftButton)
        self.assertEqual(w.table.rowCount(), 20)
        self.assertEqual(w.table.item(3, 3).text(), "Watched")
        self.assertFalse(w.watched_button.isEnabled())
        w.search.setText("upload 16")
        self.assertEqual(w.table.rowCount(), 1)
        self.assertEqual(w.rows[0]["id"], older_id)
        w.search.clear()
        w.view.setCurrentIndex(w.view.findData("pending"))
        self.assertFalse(w.browse_channel.isVisible())
        self.assertTrue(w.table.isColumnHidden(3))
        self.assertEqual(w.table.rowCount(), 3)

    def test_browse_keeps_channel_on_reload_and_handles_removed_channels(self):
        cid = self.add_browse_channel()
        w = self.window
        w.view.setCurrentIndex(w.view.findData("browse"))
        w.browse_channel.setCurrentIndex(w.browse_channel.findData(cid))
        db = core.connect(self.paths)
        with db:
            db.execute("UPDATE channels SET name='Renamed channel' WHERE id=?", (cid,))
        w.reload()
        self.assertEqual(w.browse_channel.currentData(), cid)
        self.assertEqual(w.browse_channel.currentText(), "Renamed channel")
        with db:
            db.execute("UPDATE channels SET active=0 WHERE id=?", (cid,))
        w.reload()
        self.assertEqual(w.browse_channel.count(), 1)
        self.assertEqual(w.table.rowCount(), 2)
        with db:
            db.execute("UPDATE channels SET active=0")
        db.close()
        w.reload()
        self.assertEqual(w.browse_channel.count(), 0)
        self.assertEqual(w.table.rowCount(), 0)
        self.assertTrue(w.empty.isVisible())
        self.assertFalse(w.play_button.isEnabled())
        self.assertFalse(w.restore_button.isEnabled())

    def test_catalogue_paging_and_search_across_loaded_pages(self):
        cid = self.add_browse_channel()
        db = core.connect(self.paths)
        with db:
            db.executemany("INSERT INTO catalogue VALUES (?,?,?,?,?)", [
                (f"{i:011d}", cid, f"Archive title {i}", i + 1, 1) for i in range(120)])
            db.execute("INSERT INTO catalogue_state VALUES (?,?,?,?,?)", (cid, 201, 0, 1, 1000))
        db.close()
        w = self.window
        w.view.setCurrentIndex(w.view.findData("browse"))
        w.browse_channel.setCurrentIndex(w.browse_channel.findData(cid))
        self.assertEqual(w.table.rowCount(), 50)
        QTest.mouseClick(w.next_page, Qt.MouseButton.LeftButton)
        self.assertEqual(w.rows[0]["id"], f"{50:011d}")
        QTest.mouseClick(w.next_page, Qt.MouseButton.LeftButton)
        self.assertEqual(w.table.rowCount(), 20)
        self.assertFalse(w.next_page.isEnabled())
        w.search.setText("Archive title 119")
        self.assertEqual(w.table.rowCount(), 1)
        self.assertEqual(w.rows[0]["id"], f"{119:011d}")
        self.assertEqual(w.table.item(0, 0).text(), "—")
        w.table.selectRow(0)
        QTest.mouseClick(w.restore_button, Qt.MouseButton.LeftButton)
        self.assertIn(f"{119:011d}", {r["id"] for r in core.queue_rows(self.paths)})

    def test_offline_playback_uses_local_file_even_with_browser_preference(self):
        video_id = "x" * 11
        media.queue_downloads(self.paths, [video_id])
        folder = media.download_directory(self.paths, video_id)
        folder.mkdir(parents=True)
        file = folder / "media.mkv"
        file.write_bytes(b"test media")
        media.finish_download(self.paths, video_id, file)
        core.save_settings(self.paths, {"daily_time": "19:00", "player": "browser", "hide_shorts": False})
        w = self.window
        w.view.setCurrentIndex(w.view.findData("downloads"))
        w.table.selectRow(0)
        self.assertEqual(w.play_button.text(), "Play offline")
        with patch("gui.shutil.which", side_effect=lambda name: "/usr/bin/mpv" if name == "mpv" else None), \
             patch.object(gui.QProcess, "start"), patch("gui.QDesktopServices.openUrl") as browser:
            w.play()
        browser.assert_not_called()
        self.assertEqual(w.players[0].arguments()[-1], str(file))
        self.assertIn("--ytdl=no", w.players[0].arguments())
        self.assertEqual(len(core.queue_rows(self.paths)), 2)
        file.unlink()
        with patch.object(gui.QProcess, "start") as start, patch("gui.QMessageBox.warning") as warning:
            w.play()
        start.assert_not_called()
        warning.assert_called_once()
        self.assertFalse(w.play_button.isEnabled())

    def test_hide_shorts_setting_refreshes_lists_and_counts_without_changing_history(self):
        db = core.connect(self.paths)
        with db:
            core.record_video_type(db, "x" * 11, False)
            core.record_video_type(db, "y" * 11, True)
        before = [tuple(r) for r in db.execute("SELECT * FROM videos ORDER BY id")]
        dialog = gui.SettingsDialog(self.paths, self.window)
        self.assertFalse(dialog.hide_shorts.isChecked())
        dialog.hide_shorts.setChecked(True)
        dialog.save()
        self.window.reload()
        self.assertEqual(self.window.table.rowCount(), 1)
        self.assertTrue(self.window.summary.text().startswith("1 unwatched"))
        dialog.hide_shorts.setChecked(False)
        dialog.save()
        self.window.reload()
        self.assertEqual(self.window.table.rowCount(), 2)
        self.assertEqual([tuple(r) for r in db.execute("SELECT * FROM videos ORDER BY id")], before)
        db.close()
        dialog.deleteLater()

    def test_unclassified_videos_have_visible_pending_notice(self):
        values = core.settings(self.paths)
        values["hide_shorts"] = True
        core.save_settings(self.paths, values)
        self.window.reload()
        self.assertEqual(self.window.table.rowCount(), 0)
        self.assertTrue(self.window.type_notice.isVisible())
        self.assertIn("2 videos awaiting type checks", self.window.type_notice.text())
        with patch.object(gui.TypeWorker, "start") as start:
            self.window.check_video_types()
        start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
