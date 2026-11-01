import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
import tmux_playback

REPO = Path(__file__).resolve().parents[1]


class TmuxTests(unittest.TestCase):
    def inspect(self, clients, pane_tty="/dev/pts/2", destroy="off", exit_unattached="off"):
        with patch.dict(os.environ, {"TMUX": "/tmp/socket,123,0", "TMUX_PANE": "%1"}), \
             patch("shutil.which", return_value="/usr/bin/tmux"), patch("os.ttyname", return_value="/dev/pts/2"), \
             patch("tmux_playback.query", side_effect=[f"$0\n{pane_tty}\n/tmp/socket", clients, destroy, exit_unattached]):
            return tmux_playback.local_client()

    def test_local_client_is_discovered_from_its_real_tty(self):
        self.assertEqual(self.inspect("/dev/tty3\t123\t0\t0"),
                         {"program": "/usr/bin/tmux", "socket": "/tmp/socket", "session": "$0", "tty": "/dev/tty3", "pid": 123})

    def test_remote_nested_and_detached_clients_are_not_local_consoles(self):
        for clients in ("", "/dev/pts/0\t123\t0\t0", "/dev/tty0\t123\t0\t0", "/dev/console\t123\t0\t0"):
            with self.subTest(clients=clients):
                self.assertIsNone(self.inspect(clients))

    def test_ambiguous_readonly_and_control_clients_are_rejected(self):
        for clients in ("/dev/tty3\t123\t0\t0\n/dev/pts/1\t124\t0\t0",
                        "/dev/tty3\t123\t1\t0", "/dev/tty3\t123\t0\t1"):
            with self.subTest(clients=clients), self.assertRaises(core.InboxError):
                self.inspect(clients)
        with self.assertRaisesRegex(core.InboxError, "does not match"):
            self.inspect("/dev/tty3\t123\t0\t0", pane_tty="/dev/pts/9")

    def test_plain_terminal_does_not_query_tmux(self):
        with patch.dict(os.environ, {}, clear=True), patch("tmux_playback.query") as query:
            self.assertIsNone(tmux_playback.local_client())
            query.assert_not_called()

    def test_sessions_configured_to_be_destroyed_on_detach_are_preserved(self):
        for destroy, exit_unattached in (("on", "off"), ("off", "on"), ("keep-last", "off")):
            with self.subTest(destroy=destroy, exit_unattached=exit_unattached), self.assertRaisesRegex(core.InboxError, "will not detach"):
                self.inspect("/dev/tty3\t123\t0\t0", destroy=destroy, exit_unattached=exit_unattached)

    def test_changed_client_is_not_detached(self):
        with patch("tmux_playback.local_client", return_value=None), patch("tmux_playback.query") as query:
            with self.assertRaisesRegex(core.InboxError, "client changed"):
                tmux_playback.play(None, ["mpv"], {"tty": "/dev/tty3"})
            query.assert_not_called()

    def test_worker_captures_launch_error_and_reattaches_to_exact_server_session(self):
        class Attached(BaseException):
            pass
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            client = {"program": "/usr/bin/tmux", "socket": "/tmp/a socket", "session": "$2", "tty": "/dev/tty3"}
            request = job / "request.json"
            request.write_text(json.dumps({"command": ["mpv", "a file"], "client": client, "env": {"MPV_HOME": "/tmp/config"}}))
            with patch("os.ttyname", return_value="/dev/tty3"), patch("termios.tcgetattr", return_value=None), patch("subprocess.Popen", side_effect=OSError("fixture launch failure")), \
                 patch("os.execve", side_effect=Attached) as attach, contextlib.redirect_stdout(io.StringIO()), \
                 patch.dict(os.environ, {"TMUX": "old", "TMUX_PANE": "%0", "TERM": "linux"}):
                with self.assertRaises(Attached):
                    tmux_playback.player_job(request)
            result = json.loads((job / "result.json").read_text())
            self.assertEqual(result["code"], 1)
            self.assertIn("fixture launch failure", result["error"])
            self.assertEqual(attach.call_args.args[1], ["/usr/bin/tmux", "-S", "/tmp/a socket", "attach-session", "-E", "-t", "$2"])
            self.assertNotIn("TMUX", attach.call_args.args[2])
            self.assertNotIn("TMUX_PANE", attach.call_args.args[2])
            self.assertEqual(attach.call_args.args[2]["TERM"], "linux")

    def test_dead_helper_returns_an_error_without_hanging(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = core.Paths(Path(directory) / "config", Path(directory) / "data", Path(directory) / "state")
            paths.ensure()
            client = {"program": "tmux", "socket": "/tmp/socket", "session": "$0", "tty": "/dev/tty3", "pid": 123}
            with patch("tmux_playback.local_client", return_value=client), patch("tmux_playback.query"), patch("tmux_playback.alive", return_value=False):
                with self.assertRaisesRegex(core.InboxError, "helper exited"):
                    tmux_playback.play(paths, ["mpv"], client)
            self.assertEqual(list(paths.state.glob("tmux-player-*")), [])

    def test_handoff_reports_success_failure_and_cancel_and_removes_job_files(self):
        for code in (0, 2, 130):
            with self.subTest(code=code), tempfile.TemporaryDirectory(prefix="yt job ' ") as directory:
                root = Path(directory)
                paths = core.Paths(root / "config", root / "data", root / "state")
                paths.ensure()
                client = {"program": "tmux", "socket": "/tmp/socket", "session": "$0", "tty": "/dev/tty3", "pid": os.getpid()}
                command = ["mpv", "--", "/a file with ' quotes.mkv"]
                def accept(program, *args):
                    self.assertEqual(args[:6], ("-S", "/tmp/socket", "detach-client", "-t", "/dev/tty3", "-E"))
                    helper = shlex.split(args[6])
                    self.assertEqual(helper[:3], ["exec", sys.executable, str(REPO / "tmux_playback.py")])
                    request = Path(helper[3])
                    self.assertEqual(json.loads(request.read_text())["command"], command)
                    core.atomic_write(request.parent / "result.json", json.dumps({"code": code, "error": "fixture player error"}))
                with patch("tmux_playback.local_client", return_value=client), patch("tmux_playback.query", side_effect=accept):
                    if code == 0:
                        self.assertEqual(tmux_playback.play(paths, command, client), 0)
                    elif code == 130:
                        with self.assertRaises(KeyboardInterrupt):
                            tmux_playback.play(paths, command, client)
                    else:
                        with self.assertRaisesRegex(core.InboxError, "fixture player error"):
                            tmux_playback.play(paths, command, client)
                self.assertEqual(list(paths.state.glob("tmux-player-*")), [])


class PlayerWorkerPtyTests(unittest.TestCase):
    def scenario(self, mode):
        with tempfile.TemporaryDirectory(prefix="yt-worker ' ") as directory:
            root = Path(directory)
            player = root / "player.py"
            player.write_text('''import os,sys,termios,tty
from pathlib import Path
root=Path(sys.argv[1]); mode=sys.argv[2]
assert os.tcgetpgrp(0)==os.getpgrp()
assert termios.tcgetattr(0)[3]&termios.ICANON
(root/'tty').write_text(os.ttyname(0))
(root/'config-env').write_text(os.environ.get('MPV_HOME',''))
if mode=='error':
    print('fixture decoder error',file=sys.stderr)
    sys.exit(2)
tty.setraw(0)
(root/'ready').touch()
while os.read(0,1)!=b'q': pass
# Leave raw mode deliberately: the worker must restore it, even after failure.
''')
            attach = root / "tmux"
            attach.write_text(f"#!{sys.executable}\n" + '''import json,sys,termios
from pathlib import Path
root=Path(__file__).resolve().parent
(root/'attached.json').write_text(json.dumps({'args':sys.argv[1:],'canonical':bool(termios.tcgetattr(0)[3]&termios.ICANON)}))
''')
            attach.chmod(0o755)
            master, slave = pty.openpty()
            modes = termios.tcgetattr(slave)
            client = {"program": str(attach), "socket": str(root / "socket"), "session": "$2", "tty": os.ttyname(slave)}
            request = root / "request.json"
            request.write_text(json.dumps({"command": [sys.executable, "-S", str(player), str(root), mode], "client": client, "env": {"MPV_HOME": str(root / "mpv config")}}))
            bootstrap = "import os,fcntl,termios,sys; os.setsid(); fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execv(sys.argv[1],sys.argv[1:])"
            proc = subprocess.Popen([sys.executable, "-S", "-c", bootstrap, sys.executable, "-S", str(REPO / "tmux_playback.py"), str(request)],
                                    stdin=slave, stdout=slave, stderr=slave)
            output = bytearray()
            def wait_for(condition):
                deadline = time.monotonic() + 5
                while not condition() and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.05)[0]:
                        output.extend(os.read(master, 65536))
                self.assertTrue(condition(), output.decode(errors="replace"))
            try:
                if mode != "error":
                    wait_for(lambda: (root / "ready").exists())
                    if mode == "cancel":
                        os.kill(proc.pid, signal.SIGINT)
                    else:
                        os.write(master, b"q")
                self.assertEqual(proc.wait(timeout=5), 0)
                result = json.loads((root / "result.json").read_text())
                self.assertEqual(result["code"], {"success": 0, "error": 2, "cancel": 130}[mode])
                if mode == "error":
                    self.assertIn("fixture decoder error", result["error"])
                self.assertEqual((root / "tty").read_text(), os.ttyname(slave))
                self.assertEqual((root / "config-env").read_text(), str(root / "mpv config"))
                attached = json.loads((root / "attached.json").read_text())
                self.assertTrue(attached["canonical"])
                self.assertEqual(attached["args"], ["-S", client["socket"], "attach-session", "-E", "-t", "$2"])
                self.assertEqual(termios.tcgetattr(slave), modes)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                os.close(master)
                os.close(slave)

    def test_keyboard_and_terminal_modes_are_restored_before_reattach(self):
        self.scenario("success")

    def test_player_error_is_preserved_before_reattach(self):
        self.scenario("error")

    def test_interrupt_stops_player_restores_terminal_and_reattaches(self):
        self.scenario("cancel")


@unittest.skipUnless(shutil.which("tmux"), "tmux is needed for the real client handoff")
class RealTmuxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Some containers forbid local socket operations even for isolated tests.
        with tempfile.TemporaryDirectory() as directory:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                    name = str(Path(directory) / "socket")
                    server.bind(name)
                    server.listen(1)
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                        client.connect(name)
            except PermissionError:
                raise unittest.SkipTest("This environment blocks local Unix sockets needed by tmux")

    def scenario(self, mode):
        # The outer PTY substitutes for hardware only in this test's classifier.
        # mpv is replaced by a process that checks the real controlling terminal.
        with tempfile.TemporaryDirectory(prefix="yt-tmux ' ") as directory:
            root = Path(directory)
            socket = str(root / "socket")
            binary = shutil.which("tmux")
            bindir = root / "bin"
            bindir.mkdir()
            fake = bindir / "mpv"
            fake.write_text(f"#!{sys.executable}\n" + '''import json,os,sys,termios,tty,time
from pathlib import Path
root=Path(__file__).resolve().parents[1]
mode=(root/'mode').read_text()
fd=sys.stdin.fileno(); previous=termios.tcgetattr(fd)
assert previous[3]&termios.ICANON, 'client did not restore canonical mode'
assert os.tcgetpgrp(fd)==os.getpgrp(), 'player did not receive foreground terminal'
(root/'player.json').write_text(json.dumps({'tty':os.ttyname(fd),'args':sys.argv[1:],'mpv_home':os.environ.get('MPV_HOME')}))
if mode=='error':
    print('fixture decoder failure',file=sys.stderr,flush=True)
    sys.exit(2)
try:
    tty.setraw(fd)
    (root/'player-ready').touch()
    while os.read(fd,1)!=b'q': pass
finally:
    termios.tcsetattr(fd,termios.TCSANOW,previous)
''')
            fake.chmod(0o755)
            (root / "mode").write_text(mode)
            runner = root / "runner.py"
            runner.write_text('''import json,os,sys
from pathlib import Path
sys.path.insert(0,os.environ['REPO'])
import core,media,terminal,tmux_playback
root=Path(__file__).resolve().parent
paths=core.Paths(root/'config',root/'data',root/'state'); paths.ensure()
vid='a'*11; cid='UC'+'a'*22
db=core.connect(paths)
with db:
    db.execute("INSERT INTO channels(id,name,added_at) VALUES (?,'Fixture',100)",(cid,))
    db.execute("INSERT INTO videos VALUES (?,?,?,100,100,'pending',0,100)",(vid,cid,'Fixture'))
    core.record_video_type(db,vid,False)
db.close()
media.queue_downloads(paths,[vid])
folder=media.download_directory(paths,vid); folder.mkdir(parents=True)
file=folder/'media.mkv'; file.write_bytes(b'fixture'); media.finish_download(paths,vid,file)
tmux_playback.is_vt=lambda name: name==os.environ['OUTER_TTY']
os.environ['MPV_HOME']=str(root/'mpv config')
(root/'runner-ready').touch()
input('Ready: Enter to play')
try:
    message=terminal.play(paths,vid)
except KeyboardInterrupt:
    message='cancelled'
except core.InboxError as exc:
    message=str(exc)
(root/'done.json').write_text(json.dumps({'message':message,'pending':len(core.queue_rows(paths))}))
input('Returned to original pane')
''')
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 110, 0, 0))
            original_modes = termios.tcgetattr(slave)
            ttyname = os.ttyname(slave)
            env = dict(os.environ, TERM="xterm-256color", SHELL="/bin/sh", REPO=str(REPO), OUTER_TTY=ttyname,
                       PATH=str(bindir) + os.pathsep + os.environ["PATH"])
            for key in ("TMUX", "TMUX_PANE", "DISPLAY", "WAYLAND_DISPLAY", "PYTHONPATH"):
                env.pop(key, None)
            args = [binary, "-S", socket, "-f", "/dev/null", "new-session", "-s", "fixture",
                    shlex.join([sys.executable, "-S", str(runner)])]
            bootstrap = "import os,fcntl,termios,sys; os.setsid(); fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execv(sys.argv[1],sys.argv[1:])"
            proc = subprocess.Popen([sys.executable, "-S", "-c", bootstrap, *args], stdin=slave, stdout=slave, stderr=slave, env=env)
            output = bytearray()
            def wait_for(condition):
                deadline = time.monotonic() + 8
                while not condition() and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.05)[0]:
                        try:
                            output.extend(os.read(master, 65536))
                        except OSError:
                            break
                self.assertTrue(condition(), output.decode(errors="replace"))
            def tmux(*command):
                result = subprocess.run([binary, "-S", socket, *command], capture_output=True, text=True, env=env, timeout=3)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()
            try:
                wait_for(lambda: (root / "runner-ready").exists())
                tmux("split-window", "-h", "-d", "-t", "fixture", "sleep 120")
                before = tmux("list-panes", "-t", "fixture", "-F", "#{pane_id}:#{pane_pid}")
                self.assertEqual(len(before.splitlines()), 2)
                os.write(master, b"\n")
                if mode != "error":
                    wait_for(lambda: (root / "player-ready").exists())
                    self.assertEqual(tmux("list-clients", "-t", "fixture", "-F", "#{client_tty}"), "")
                    self.assertEqual(tmux("list-panes", "-t", "fixture", "-F", "#{pane_id}:#{pane_pid}"), before)
                    if mode == "cancel":
                        os.kill(proc.pid, signal.SIGINT)
                    else:
                        os.write(master, b"q")
                wait_for(lambda: (root / "done.json").exists())
                wait_for(lambda: tmux("list-clients", "-t", "fixture", "-F", "#{client_tty}") == ttyname)
                after = tmux("list-panes", "-t", "fixture", "-F", "#{pane_id}:#{pane_pid}")
                self.assertEqual(after, before)
                actual = json.loads((root / "player.json").read_text())
                self.assertEqual(actual["tty"], ttyname)
                self.assertIn("--gpu-context=drm", actual["args"])
                self.assertIn("--input-terminal=yes", actual["args"])
                self.assertIn("--ytdl=no", actual["args"])
                self.assertEqual(actual["mpv_home"], str(root / "mpv config"))
                result = json.loads((root / "done.json").read_text())
                self.assertEqual(result["pending"], 1)
                expected = "fixture decoder failure" if mode == "error" else "cancelled" if mode == "cancel" else "Playback closed."
                self.assertIn(expected, result["message"])
                self.assertEqual(list((root / "state").glob("tmux-player-*")), [])
            finally:
                subprocess.run([binary, "-S", socket, "kill-server"], capture_output=True, env=env, timeout=3)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                os.close(master)
                os.close(slave)

    def test_keyboard_handoff_and_reattach_preserve_panes_and_watch_state(self):
        self.scenario("success")

    def test_player_failure_returns_to_tmux_with_error_text(self):
        self.scenario("error")

    def test_interrupted_player_reattaches_and_preserves_watch_state(self):
        self.scenario("cancel")


if __name__ == "__main__":
    unittest.main()
