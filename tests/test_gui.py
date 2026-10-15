import os
from pathlib import Path
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core

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


if __name__ == "__main__":
    unittest.main()
