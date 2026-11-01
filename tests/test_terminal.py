import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli
import core
import install
import media
import terminal
import tui

C1 = "UC" + "a" * 22
V1, V2 = "a" * 11, "b" * 11
REPO = Path(__file__).resolve().parents[1]


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = core.Paths(self.root / "config", self.root / "data", self.root / "state")
        self.paths.ensure()
        db = core.connect(self.paths)
        try:
            with db:
                db.execute("INSERT INTO channels(id,name,added_at) VALUES (?, 'Test channel', 100)", (C1,))
                db.executemany("INSERT INTO videos VALUES (?,?,?,?,?,?,?,?)", [
                    (V1, C1, "First video", 200, 200, "pending", 0, 200),
                    (V2, C1, "Second video", 100, 100, "pending", 0, 100)])
                core.record_video_type(db, V1, False)
                core.record_video_type(db, V2, True)
        finally:
            db.close()

    def tearDown(self):
        self.temp.cleanup()

    def offline(self):
        folder = media.download_directory(self.paths, V1)
        folder.mkdir(parents=True, exist_ok=True)
        file = folder / "media.mkv"
        file.write_bytes(b"fixture")
        media.queue_downloads(self.paths, [V1])
        media.finish_download(self.paths, V1, file)
        return file

    def test_real_vt_detection_excludes_ssh_tmux_and_console_alias(self):
        for name, expected in (("/dev/tty3", True), ("/dev/tty12", True), ("/dev/pts/2", False),
                               ("/dev/console", False), ("/dev/tty0", False)):
            with self.subTest(name=name), patch("os.ttyname", return_value=name):
                self.assertEqual(terminal.local_vt(), expected)
        with patch("os.ttyname", side_effect=OSError):
            self.assertFalse(terminal.local_vt())

    def test_local_vt_uses_gpu_drm_keyboard_and_bounded_formats_even_with_stale_display(self):
        with patch("terminal.local_vt", return_value=True), patch("shutil.which", side_effect=lambda x: "/bin/" + x), patch.dict(os.environ, {"DISPLAY": ":0"}):
            command = terminal.play_command(self.paths, V1)
        for option in ("--vo=gpu", "--gpu-context=drm", "--gpu-api=opengl", "--input-terminal=yes",
                       "--hwdec=auto", "--script-opts-append=yt_inbox-telemetry=no"):
            self.assertIn(option, command)
        selection = next(x for x in command if x.startswith("--ytdl-format="))
        self.assertIn("height<=720", selection)
        self.assertNotIn("height<=2160", selection)
        self.assertIn("vcodec!^=av01", selection)
        self.assertNotIn("--no-config", command)
        self.assertEqual(command[-2:], ["--", core.video_url(V1)])

    def test_offline_file_and_custom_preferences_survive_terminal_playback(self):
        file = self.offline()
        values = core.settings(self.paths)
        values["playback_mode"] = "custom"
        core.save_settings(self.paths, values)
        with patch("terminal.local_vt", return_value=True), patch("shutil.which", side_effect=lambda x: "/bin/mpv" if x == "mpv" else None):
            command = terminal.play_command(self.paths, V1)
        self.assertIn("--ytdl=no", command)
        self.assertFalse(any(x.startswith(("--hwdec=", "--ytdl-format=")) for x in command))
        self.assertEqual(command[-1], str(file))
        file.unlink()
        with patch("terminal.local_vt", return_value=True), patch("shutil.which", return_value="/bin/mpv"):
            with self.assertRaisesRegex(core.InboxError, "saved file is missing"):
                terminal.play_command(self.paths, V1)

    def test_ssh_does_not_launch_drm_and_desktop_terminal_keeps_window_output(self):
        with patch("terminal.local_vt", return_value=False), patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(core.InboxError, "No graphical display"):
                terminal.play_command(self.paths, V1)
            with self.assertRaisesRegex(core.InboxError, "local Linux virtual console"):
                terminal.play_command(self.paths, V1, "drm")
            with patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}), patch("shutil.which", return_value="/bin/mpv"):
                command = terminal.play_command(self.paths, V1)
                self.assertFalse(any(x.startswith("--gpu-context=") for x in command))

    def test_shared_status_filter_search_and_explicit_channel_paging(self):
        inbox = tui.Inbox(Mock(), self.paths)
        self.assertEqual([r["id"] for r in inbox.rows], [V1])
        inbox.handle("w")
        self.assertEqual(inbox.rows, [])
        self.assertEqual(core.queue_rows(self.paths, "watched")[0]["id"], V1)
        inbox.handle("u")
        self.assertEqual(inbox.rows[0]["id"], V1)
        inbox.handle("s")
        self.assertEqual(len(inbox.rows), 2)
        entries = [{"id": f"{i:011d}", "title": f"Archive {i}", "channel_id": C1} for i in range(100)]
        with patch("core.ytdlp_json", return_value={"id": "UU" + C1[2:], "entries": entries}):
            media.load_catalogue(self.paths, C1)
        inbox.handle("5")
        inbox.handle("\n")
        self.assertEqual((inbox.total, len(inbox.rows)), (100, 50))
        with patch("media.load_catalogue") as fetch:
            inbox.handle("]")
            self.assertEqual(inbox.rows[0]["title"], "Archive 50")
            with patch.object(inbox, "prompt", return_value="Archive 99"):
                inbox.handle("/")
            self.assertEqual((inbox.total, inbox.rows[0]["title"]), (1, "Archive 99"))
            fetch.assert_not_called()
        inbox.handle("a")
        self.assertIn("00000000099", [r["id"] for r in core.queue_rows(self.paths)])

    def test_playback_success_or_failure_never_marks_watched(self):
        before = core.queue_rows(self.paths)
        proc = Mock()
        proc.wait.return_value = proc.poll.return_value = 0
        with patch("terminal.play_command", return_value=["mpv", "fixture"]), patch("subprocess.Popen", return_value=proc), contextlib.redirect_stdout(io.StringIO()):
            terminal.play(self.paths, V1)
            proc.wait.return_value = proc.poll.return_value = 1
            with self.assertRaises(core.InboxError):
                terminal.play(self.paths, V1)
        self.assertEqual(core.queue_rows(self.paths), before)

    def test_terminal_modes_are_restored_even_when_external_command_raises(self):
        events = []
        screen = Mock()
        with contextlib.ExitStack() as stack:
            for name in ("def_prog_mode", "endwin", "reset_prog_mode", "flushinp"):
                stack.enter_context(patch("curses." + name, side_effect=lambda n=name: events.append(n)))
            with self.assertRaisesRegex(RuntimeError, "fixture"):
                with tui.suspended(screen):
                    events.append("player")
                    raise RuntimeError("fixture")
        self.assertEqual(events, ["def_prog_mode", "endwin", "player", "reset_prog_mode", "flushinp"])
        screen.keypad.assert_called_with(True)
        screen.clearok.assert_called_with(True)

    def test_one_interface_owns_download_recovery_and_lock_releases_after_failure(self):
        with core.interface_lock(self.paths):
            with self.assertRaisesRegex(core.InboxError, "Another inbox interface"):
                with core.interface_lock(self.paths):
                    self.fail("Second interface acquired the lock")
        with self.assertRaises(core.Busy):
            with core.interface_lock(self.paths):
                raise core.Busy("Network job is busy")
        with core.interface_lock(self.paths):
            pass

    def test_terminal_install_has_no_notification_and_reinstall_preserves_choice_and_data(self):
        core.set_status(self.paths, [V1], "watched")
        file = self.offline()
        with patch.dict(os.environ, {"YT_INBOX_HOME": str(self.root)}):
            launcher = install.install(self.paths, enable=False, terminal_only=True)
            unit = self.root / "systemd/user/yt-inbox-check.service"
            self.assertIn("--scheduled --no-notify", unit.read_text())
            self.assertIn("Terminal=true", (self.root / "applications/yt-inbox.desktop").read_text())
            result = subprocess.run([sys.executable, "-S", str(self.paths.data / "app/cli.py"), "tui", "--help"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--video-output", result.stdout)
            install.install(self.paths, enable=False)
            self.assertTrue(core.settings(self.paths)["terminal_only"])
            self.assertIn("--no-notify", unit.read_text())
            self.assertEqual(core.queue_rows(self.paths, "watched")[0]["id"], V1)
            self.assertEqual(media.local_file(self.paths, V1), file)
            self.assertTrue(launcher.exists())
            install.install(self.paths, enable=False, terminal_only=False)
            self.assertNotIn("--no-notify", unit.read_text())
            self.assertFalse(core.settings(self.paths)["terminal_only"])

    def test_scheduled_terminal_check_skips_notification_without_marking_notified(self):
        with patch.dict(os.environ, {"YT_INBOX_HOME": str(self.root)}), patch("core.run_check", return_value={"new": 1, "checked": 1, "errors": []}), patch("cli.launch_notification") as notify, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["check", "--scheduled", "--no-notify"]), 0)
            notify.assert_not_called()
        db = core.connect(self.paths)
        try:
            self.assertEqual(len(core.notify_candidates(db, True)), 1)
        finally:
            db.close()

    def test_download_validates_completion_and_keeps_watched_status_manual(self):
        folder = media.download_directory(self.paths, V1)
        folder.mkdir(parents=True)
        file = folder / "media.mkv"
        script = "from pathlib import Path; import json; p=Path(" + repr(str(file)) + "); p.write_bytes(b'fixture'); print('YT_INBOX_FILE:'+json.dumps(str(p)))"
        with patch("media.download_command", return_value=[sys.executable, "-c", script]):
            terminal.download(self.paths, V1, write=lambda line: None)
        self.assertEqual(media.local_file(self.paths, V1), file)
        self.assertEqual(core.queue_rows(self.paths)[0]["id"], V1)
        file.unlink()
        with patch("media.download_command", return_value=[sys.executable, "-c", "print('incomplete')"]):
            with self.assertRaisesRegex(core.InboxError, "without a complete video"):
                terminal.download(self.paths, V1, write=lambda line: None)
        self.assertEqual(media.download_records(self.paths)[V1]["state"], "failed")

    def test_download_cancellation_stops_child_process_and_keeps_partial_file(self):
        folder = media.download_directory(self.paths, V1)
        folder.mkdir(parents=True)
        part = folder / "media.part"
        pidfile = self.root / "child-pid"
        script = ("import subprocess,sys,time; from pathlib import Path; "
                  "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
                  f"Path({str(pidfile)!r}).write_text(str(p.pid)); Path({str(part)!r}).write_bytes(b'partial'); "
                  "print('started',flush=True); time.sleep(120)")
        def cancel(line):
            raise KeyboardInterrupt
        with patch("media.download_command", return_value=[sys.executable, "-c", script]):
            with self.assertRaises(KeyboardInterrupt):
                terminal.download(self.paths, V1, write=cancel)
        self.assertEqual(media.download_records(self.paths)[V1]["state"], "cancelled")
        self.assertEqual(part.read_bytes(), b"partial")
        child = Path("/proc") / pidfile.read_text() / "stat"
        deadline = time.monotonic() + 2
        while child.exists() and child.read_text().split()[2] != "Z" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(not child.exists() or child.read_text().split()[2] == "Z")

    def test_terminal_metadata_removes_control_sequences(self):
        value = terminal.plain("name\x1b[2J\n\x07\u202ereversed")
        self.assertNotIn("\x1b", value)
        self.assertNotIn("\u202e", value)
        self.assertNotIn("\n", value)
        self.assertEqual(terminal.clipped("a界界b", 4), "a界")
        self.assertEqual(terminal.clipped("e\u0301xy", 2), "e\u0301x")

    def test_real_pseudoterminal_handoff_return_watched_action_and_exit_without_qt(self):
        """Exercise curses and a keyboard-reading stand-in for mpv in a real PTY."""
        bindir = self.root / "bin"
        bindir.mkdir()
        player = bindir / "mpv"
        argsfile = self.root / "player-args.json"
        player.write_text(f"#!{sys.executable}\n" + """import json,os,sys,termios,tty
from pathlib import Path
fd=sys.stdin.fileno(); old=termios.tcgetattr(fd)
assert old[3] & termios.ICANON, 'curses did not restore shell mode'
Path(os.environ['PLAYER_ARGS']).write_text(json.dumps(sys.argv[1:]))
try:
    tty.setraw(fd)
    print('FAKE_MPV_READY',flush=True)
    while os.read(fd,1)!=b'q': pass
finally:
    termios.tcsetattr(fd,termios.TCSANOW,old)
""")
        player.chmod(0o755)
        self.offline()
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 110, 0, 0))
        original_modes = termios.tcgetattr(slave)
        env = dict(os.environ, YT_INBOX_HOME=str(self.root), TERM="xterm-256color", DISPLAY=":fixture",
                   PLAYER_ARGS=str(argsfile), PATH=str(bindir) + os.pathsep + os.environ["PATH"])
        env.pop("PYTHONPATH", None)
        proc = subprocess.Popen([sys.executable, "-S", str(REPO / "cli.py"), "tui"], stdin=slave, stdout=slave, stderr=slave, env=env)
        def until(marker):
            data, deadline = b"", time.monotonic() + 6
            while marker not in data and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        data += os.read(master, 65536)
                    except OSError:
                        break
            self.assertIn(marker, data, data.decode(errors="replace"))
        try:
            until(b"First video")
            os.write(master, b"\n")
            until(b"FAKE_MPV_READY")
            os.write(master, b"q")
            until(b"Playback closed.")
            # Wait for a screen refresh after the terminal has returned to curses.
            # The previous read may already contain that refresh, so synchronize
            # on input handling by checking the durable watched state below.
            deadline = time.monotonic() + 3
            while core.queue_rows(self.paths) and time.monotonic() < deadline:
                os.write(master, b"w")
                time.sleep(0.03)
            self.assertEqual(core.queue_rows(self.paths), [])
            os.write(master, b"q")
            self.assertEqual(proc.wait(timeout=5), 0)
            self.assertEqual(termios.tcgetattr(slave), original_modes)
            args = json.loads(argsfile.read_text())
            self.assertIn("--input-terminal=yes", args)
            self.assertIn("--ytdl=no", args)
            self.assertFalse(any(x.startswith("--gpu-context=") for x in args))
            self.assertEqual(core.queue_rows(self.paths, "watched")[0]["id"], V1)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main()
