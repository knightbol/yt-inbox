"""Cross-language fixtures and failure tests. Never use a real user profile.

Python is a development dependency here, not part of the native runtime.
HTTP uses a separately linked test driver; the production executable always
uses verified HTTPS and has no test URL/environment override.
"""
import base64
import copy
import fcntl
import json
import os
from pathlib import Path
import pty
import random
import select
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

NATIVE = Path(__file__).resolve().parents[1]
REPO = NATIVE.parent
sys.path.insert(0, str(REPO))
import core
import syncing

BINARY = NATIVE / "build/yt-inbox-c"
DRIVER = NATIVE / "build/test-driver"
CID = "UC" + "a" * 22
OTHER = "UC" + "b" * 22
URL = "https://www.youtube.com/channel/" + CID
VID = "v0000000001"


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yti-c-")
        self.root = Path(self.temp.name)
        self.env = dict(os.environ, YT_INBOX_HOME=str(self.root), LC_ALL="C.UTF-8")
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)
        self.paths = core.Paths(self.root / "config", self.root / "data", self.root / "state")
        self.paths.ensure()
        self.bin = self.root / "tools"
        self.bin.mkdir()
        self.env["PATH"] = str(self.bin) + os.pathsep + self.env["PATH"]
        self.run_c("status")

    def tearDown(self):
        self.temp.cleanup()

    def run_c(self, *args, driver=False, good=True, env=None):
        result = subprocess.run([str(DRIVER if driver else BINARY), *map(str, args)],
                                env=env or self.env, text=True, capture_output=True, timeout=15)
        if good:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("AddressSanitizer", result.stderr)
        self.assertNotIn("runtime error:", result.stderr)
        return result

    def db(self):
        return core.connect(self.paths)

    def seed(self, status="pending", vid=VID, cid=CID, title="Açores — história", kind=0):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO channels(id,name,added_at) VALUES(?,?,?)", (cid, "Canal", 100))
            db.execute("INSERT INTO videos VALUES(?,?,?,?,?,?,?,?)", (vid, cid, title, 200, 200, status, 0, 200))
            if kind is not None:
                core.record_video_type(db, vid, bool(kind))

    def status(self, vid=VID):
        with self.db() as db:
            return db.execute("SELECT status FROM videos WHERE id=?", (vid,)).fetchone()[0]

    def file(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def native_doc(self, action="_snapshot", value=None):
        args = [action] + ([self.file("incoming.json", value)] if value is not None else [])
        return json.loads(self.run_c(*args, driver=True).stdout)

    def fake(self, name, code):
        path = self.bin / name
        path.write_text("#!" + sys.executable + "\n" + code)
        path.chmod(0o755)
        return path

    def ytdlp(self, result, exit_code=0):
        path = self.file("metadata.json", result)
        log = self.root / "yt-args.json"
        self.fake("yt-dlp", f"import json,sys\nfrom pathlib import Path\nPath({str(log)!r}).write_text(json.dumps(sys.argv[1:]))\nprint(Path({str(path)!r}).read_text())\nsys.exit({exit_code})\n")
        return log

    def http(self, responses):
        path = self.file("http.json", {"responses": responses, "calls": []})
        self.env["YTI_TEST_HTTP"] = str(path)
        return path

    @staticmethod
    def response(path, body=None, status=200, **extra):
        return dict(path=path, body=body, status=status, **extra)

    @staticmethod
    def repo_info(private=True):
        return dict(id=123, private=private, visibility="private" if private else "public", archived=False, disabled=False)

    @staticmethod
    def remote_file(doc):
        raw = json.dumps(doc).encode()
        return dict(type="file", sha="a" * 40, size=len(raw), encoding="base64", content=base64.b64encode(raw).decode())

    def configure_sync(self):
        (self.paths.config / "sync.json").write_text(json.dumps(dict(repository="owner/private", repository_id=123, token="fake_test_token")))

    def test_default_storage_and_existing_python_visibility(self):
        self.seed(kind=None)
        self.assertEqual(json.loads(self.run_c("list", "--json").stdout), [])
        self.run_c("config", "hide_shorts", "false")
        rows = json.loads(self.run_c("list", "--json").stdout)
        self.assertEqual(rows[0]["title"], "Açores — história")
        self.assertEqual(core.queue_rows(self.paths)[0]["id"], VID)
        self.assertEqual((self.paths.config / "settings.json").stat().st_mode & 0o777, 0o600)

    def test_atomic_status_and_undo_interoperate(self):
        self.seed()
        self.run_c("watched", VID, "missing0000", good=False)
        self.assertEqual(self.status(), "pending")
        self.run_c("watched", VID)
        self.assertEqual(self.status(), "watched")
        self.assertTrue(core.undo(self.paths))
        core.set_status(self.paths, [VID], "skipped")
        self.run_c("undo")
        self.assertEqual(self.status(), "pending")

    def test_archive_selection_restores_baseline_on_undo(self):
        with self.db() as db:
            db.execute("INSERT INTO channels(id,name,added_at) VALUES(?,?,0)", (CID, "Canal"))
            db.execute("INSERT INTO catalogue VALUES(?,?,?,?,?)", (VID, CID, "Arquivo", 1, 1))
        self.run_c("watched", VID)
        self.run_c("undo")
        self.assertEqual(self.status(), "baseline")

    def test_channel_canonicalization_matches_python(self):
        for text in [CID, "@Canal", URL + "/videos", "youtube.com/@Canal/streams", "https://m.youtube.com/user/test/"]:
            got = json.loads(self.run_c("_canonical", text, driver=True).stdout)
            self.assertEqual(got, core.canonical_channel(text))
        for text in ["https://evil.example/@Canal", "https://youtube.com/watch?v=" + VID, "https://user@youtube.com/@Canal", URL + "/bad"]:
            self.run_c("_canonical", text, driver=True, good=False)

    def feed(self, cid=CID):
        return f'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"><title>Canal</title><yt:channelId>{cid[2:]}</yt:channelId><entry><yt:videoId>{VID}</yt:videoId><yt:channelId>{cid}</yt:channelId><title>A &amp; B</title><published>2026-11-01T12:00:00+01:00</published></entry></feed>'''

    def test_feed_parser_matches_python_and_rejects_untrusted_xml(self):
        path = self.root / "feed.xml"
        path.write_text(self.feed())
        got = json.loads(self.run_c("_feed", CID, path, driver=True).stdout)
        name, entries = core.parse_feed(path.read_bytes(), CID)
        self.assertEqual(got["title"], name)
        self.assertEqual(got["entries"][0]["published"], entries[0]["published"])
        for invalid in [self.feed(OTHER), self.feed().replace("2026-11-01", "2026-02-31"), '<!DOCTYPE feed [<!ENTITY x "unsafe">]>' + self.feed(), "<html/>"]:
            path.write_text(invalid)
            self.run_c("_feed", CID, path, driver=True, good=False)

    def test_rss_check_seeds_latest_then_preserves_watched(self):
        self.paths.channels.write_text(URL + "\n")
        self.run_c("config", "hide_shorts", "false")
        self.http([self.response("channel_id=" + CID, raw=self.feed())])
        self.run_c("check", "--no-notify", driver=True)
        self.assertEqual(self.status(), "pending")
        self.run_c("watched", VID)
        self.http([self.response("channel_id=" + CID, raw=self.feed().replace("A &amp; B", "New title"))])
        self.run_c("check", "--no-notify", driver=True)
        self.assertEqual(self.status(), "watched")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT title FROM videos").fetchone()[0], "New title")

    def test_invalid_channel_file_never_deactivates_existing_channels(self):
        self.seed()
        self.paths.channels.write_text("https://evil.invalid/channel/x\n")
        self.run_c("check", "--no-notify", driver=True, good=False)
        self.assertEqual(len(core.channel_rows(self.paths)), 1)

    def test_catalogue_slots_pagination_and_wrong_owner_rollback(self):
        self.seed()
        self.run_c("config", "hide_shorts", "false")
        args = self.ytdlp(dict(id="UU" + CID[2:], channel_id=CID, channel="Canal", entries=[None, dict(id="v0000000002", title="Older", media_type="video")]))
        self.run_c("browse", CID, "--older")
        self.assertIn("1:100", json.loads(args.read_text()))
        with self.db() as db:
            self.assertEqual(db.execute("SELECT position FROM catalogue").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT next_start FROM catalogue_state").fetchone()[0], 101)
        self.ytdlp(dict(id="UU" + CID[2:], entries=[dict(id="v0000000003", channel_id=42)]))
        self.run_c("browse", CID, "--older", good=False)
        with self.db() as db:
            self.assertEqual(db.execute("SELECT next_start FROM catalogue_state").fetchone()[0], 101)
            self.assertEqual(db.execute("SELECT count(*) FROM catalogue").fetchone()[0], 1)
        self.ytdlp(dict(id="UU" + CID[2:], entries=[]))
        self.run_c("browse", CID, "--older")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT exhausted FROM catalogue_state").fetchone()[0], 1)

    def test_unknown_types_are_hidden_until_positive_classification(self):
        self.seed(kind=None)
        self.ytdlp(dict(id=VID, channel_id=CID, duration=200, webpage_url="https://youtube.com/watch?v=" + VID))
        self.run_c("types", good=False)
        self.assertEqual(json.loads(self.run_c("list", "--json").stdout), [])
        with self.db() as db:
            db.execute("UPDATE video_types SET retry_after=0")
        args = self.ytdlp(dict(id=VID, channel_id=CID, media_type="video"))
        self.run_c("types")
        self.assertEqual(len(json.loads(self.run_c("list", "--json").stdout)), 1)
        self.assertEqual(json.loads(args.read_text())[-1], "https://www.youtube.com/watch?v=" + VID)

    def test_sync_snapshots_and_explicit_revisions_match_python(self):
        self.seed("watched")
        self.paths.channels.write_text("# comment\n" + URL + "\n")
        before = self.native_doc()
        self.assertEqual(before, syncing.snapshot(self.paths))
        self.run_c("restore", VID)
        after = self.native_doc()
        self.assertEqual(after, syncing.snapshot(self.paths))
        self.assertGreater(after["videos"][VID]["rev"][0], before["videos"][VID]["rev"][0])
        self.run_c("undo")
        undo = self.native_doc()
        self.assertEqual(undo["videos"][VID]["status"], "watched")
        self.assertGreater(undo["videos"][VID]["rev"][0], after["videos"][VID]["rev"][0])

    def test_first_pairing_merge_and_removal_projection(self):
        self.seed("watched")
        self.paths.channels.write_text("# keep comment\n" + URL + "\n")
        local = self.native_doc()
        remote = copy.deepcopy(local)
        remote["videos"][VID].update(status="pending", rev=[0, "f" * 32])
        other_url = "https://www.youtube.com/channel/" + OTHER
        remote["channels"][other_url] = dict(rev=[1, "f" * 32], active=True, channel_id=OTHER, name="Other")
        merged = self.native_doc("_apply", remote)
        self.assertEqual(merged["videos"][VID]["status"], "watched")
        self.assertIn(other_url, self.paths.channels.read_text())
        self.assertIn("# keep comment", self.paths.channels.read_text())
        self.paths.channels.write_text("# keep comment\n" + other_url + "\n")
        deleted = self.native_doc()
        self.assertFalse(deleted["channels"][URL]["active"])
        syncing.apply(self.paths, deleted)
        self.assertEqual(self.native_doc(), syncing.snapshot(self.paths))

    def test_remote_status_invalidates_stale_undo_and_preserves_downloads(self):
        self.seed()
        self.run_c("watched", VID)
        doc = self.native_doc()
        doc["videos"][VID].update(status="skipped", rev=[10, "f" * 32])
        with self.db() as db:
            db.execute("INSERT INTO downloads VALUES(?,'complete','/local/file','',0)", (VID,))
        self.native_doc("_apply", doc)
        self.run_c("undo")
        self.assertEqual(self.status(), "skipped")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT path FROM downloads").fetchone()[0], "/local/file")

    def test_cross_language_merge_tie_break_and_commutativity(self):
        self.seed()
        left = self.native_doc()
        right = copy.deepcopy(left)
        rng = random.Random(19)
        pairs = [(1e-7, 2e-7), (0.0, -0.0), (1e10, 1e-4)] + [(10 ** rng.uniform(-12, 10), 10 ** rng.uniform(-12, 10)) for _ in range(40)]
        for x, y in pairs:
            left["videos"][VID].update(published=x, status="watched", rev=[1, "a" * 32])
            right["videos"][VID].update(published=y, status="pending", rev=[1, "a" * 32])
            p = self.file("left.json", left)
            q = self.file("right.json", right)
            result = json.loads(self.run_c("_merge", p, q, driver=True).stdout)
            self.assertEqual(result, syncing.merge(left, right), (x, y))
            reverse = json.loads(self.run_c("_merge", q, p, driver=True).stdout)
            self.assertEqual(result, reverse)

    def test_invalid_sync_is_rejected_without_modifying_history(self):
        self.seed()
        doc = self.native_doc()
        for field, value in [("status", "watched\0ignored"), ("published", True), ("short", "false"), ("channel_id", CID + "\0bad"), ("rev", [2**53, "a" * 32])]:
            invalid = copy.deepcopy(doc)
            invalid["videos"][VID][field] = value
            self.run_c("_apply", self.file("invalid.json", invalid), driver=True, good=False)
            self.assertEqual(self.status(), "pending")
        invalid = copy.deepcopy(doc)
        invalid["videos"][VID]["unexpected"] = {"nested": [1.25]}
        path = self.file("overflow.json", invalid)
        path.write_text(path.read_text().replace("1.25", "1e309"))
        self.run_c("_merge", path, path, driver=True, good=False)
        self.assertEqual(self.status(), "pending")

    def test_sync_rechecks_privacy_before_upload(self):
        self.seed()
        self.configure_sync()
        fixture = self.http([
            self.response("/owner/private", self.repo_info()),
            self.response("/contents/yt-inbox-state.json", status=404),
            self.response("/owner/private", self.repo_info(False)),
        ])
        self.run_c("sync", driver=True, good=False)
        self.assertEqual(self.status(), "pending")
        self.assertTrue(all(c["body"] is None for c in json.loads(fixture.read_text())["calls"]))

    def test_sync_compare_and_swap_retries_and_merges_conflict(self):
        self.seed()
        doc = self.native_doc()
        remote = copy.deepcopy(doc)
        remote["videos"][VID].update(rev=[7, "f" * 32], status="watched")
        self.configure_sync()
        fixture = self.http([
            self.response("/owner/private", self.repo_info()),
            self.response("/contents/yt-inbox-state.json", status=404),
            self.response("/owner/private", self.repo_info()),
            self.response("/contents/yt-inbox-state.json", status=409),
            self.response("/contents/yt-inbox-state.json", self.remote_file(remote)),
        ])
        self.run_c("sync", driver=True)
        self.assertEqual(self.status(), "watched")
        calls = json.loads(fixture.read_text())["calls"]
        uploaded = json.loads(base64.b64decode(next(c["body"]["content"] for c in calls if c["body"])))
        syncing.validate(uploaded)

    def test_sync_blob_fallback_and_no_change_skips_upload(self):
        self.seed("watched")
        doc = self.native_doc()
        remote_file = self.remote_file(doc)
        remote_file.update(encoding="none", content="")
        self.configure_sync()
        fixture = self.http([
            self.response("/owner/private", self.repo_info()),
            self.response("/contents/yt-inbox-state.json", remote_file),
            self.response("/git/blobs/" + "a" * 40, self.remote_file(doc)),
        ])
        self.run_c("sync", driver=True)
        self.assertEqual(len(json.loads(fixture.read_text())["calls"]), 3)

    def test_download_validates_final_file_and_keeps_watch_state(self):
        self.seed()
        self.fake("ffmpeg", "pass\n")
        self.fake("yt-dlp", """import json,os,sys
from pathlib import Path
p=Path(os.environ['YT_INBOX_HOME'])/'data/downloads/v0000000001/media.mkv'
p.write_bytes(b'fixture media')
print('YT_INBOX_FILE:'+json.dumps(str(p)))
""")
        self.run_c("download", VID)
        self.assertEqual(self.status(), "pending")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT state FROM downloads").fetchone()[0], "complete")
        self.run_c("remove-download", VID, good=False)
        self.assertTrue((self.paths.data / "downloads" / VID / "media.mkv").exists())
        self.run_c("remove-download", VID, "--yes")
        self.assertEqual(self.status(), "pending")

    def test_download_rejects_file_outside_owned_directory(self):
        self.seed()
        outside = self.root / "outside.mkv"
        outside.write_bytes(b'media')
        self.fake("ffmpeg", "pass\n")
        self.fake("yt-dlp", "import json\nprint('YT_INBOX_FILE:'+json.dumps(" + repr(str(outside)) + "))\n")
        self.run_c("download", VID, good=False)
        self.assertTrue(outside.exists())
        with self.db() as db:
            self.assertEqual(db.execute("SELECT state FROM downloads").fetchone()[0], "failed")

    def test_player_loads_user_config_and_never_marks_watched(self):
        self.seed()
        log = self.root / "mpv.json"
        self.fake("mpv", "import json,sys\nfrom pathlib import Path\nPath(" + repr(str(log)) + ").write_text(json.dumps(sys.argv[1:]))\n")
        self.fake("yt-dlp", "pass\n")
        env = dict(self.env, DISPLAY=":99")
        self.run_c("play", VID, "--video-output", "window", env=env)
        args = json.loads(log.read_text())
        self.assertFalse(any(s.startswith("--no-config") or s.startswith("--config=no") for s in args))
        self.assertIn("--hwdec=auto", args)
        self.assertEqual(self.status(), "pending")
        self.run_c("config", "playback_mode", "custom")
        self.run_c("play", VID, "--video-output", "window", env=env)
        self.assertNotIn("--hwdec=auto", json.loads(log.read_text()))

    def test_backup_is_consistent_and_install_preserves_data(self):
        self.seed("watched")
        before = self.paths.channels.read_bytes()
        backup = self.root / "backup.sqlite3"
        self.run_c("_backup", backup, driver=True)
        with sqlite3.connect(backup) as db:
            self.assertEqual(db.execute("SELECT status FROM videos").fetchone()[0], "watched")
        self.run_c("install", "--primary", "--no-enable")
        self.assertEqual(self.paths.channels.read_bytes(), before)
        self.assertEqual(self.status(), "watched")
        service = (self.root / "systemd/user/yt-inbox-check.service").read_text()
        self.assertIn("YT_INBOX_HOME=", service)
        self.assertNotIn("python", service)
        p = subprocess.run([str(self.root / "bin/yt-inbox"), "--version"], env=self.env, text=True, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("-c.", p.stdout)

    def test_install_error_is_not_success(self):
        target = self.root / "systemd/user/yt-inbox-sync.timer"
        target.mkdir(parents=True)
        self.run_c("install", "--primary", "--no-enable", good=False)

    def test_setup_never_reads_tokens_from_piped_stdin(self):
        self.run_c("sync", "setup", "owner/private", good=False)
        self.assertFalse((self.paths.config / "sync.json").exists())

    def test_backup_rejects_empty_leftover_file(self):
        path = self.root / "interrupted-backup.sqlite3"
        path.touch()
        self.run_c("_backup", path, driver=True, good=False)

    @staticmethod
    def wait_for(path, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(.02)
        raise AssertionError(f"Timed out waiting for {path.name}")

    @staticmethod
    def process_running(pid):
        try:
            return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
        except FileNotFoundError:
            return False

    def test_download_cancellation_kills_descendants_that_close_stdout(self):
        self.seed()
        self.fake("ffmpeg", "pass\n")
        pidfile = self.root / "descendant.pid"
        self.fake("yt-dlp", f'''import os,signal,time
from pathlib import Path
pid=os.fork()
if pid==0:
 signal.signal(signal.SIGTERM,signal.SIG_IGN)
 os.close(1);os.close(2)
 Path({str(pidfile)!r}).write_text(str(os.getpid()))
 while True: time.sleep(1)
while True: time.sleep(1)
''')
        proc = subprocess.Popen([str(BINARY), "download", VID], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        descendant = None
        try:
            self.wait_for(pidfile)
            descendant = int(pidfile.read_text())
            proc.send_signal(signal.SIGINT)
            proc.communicate(timeout=6)
            deadline = time.monotonic() + 2
            while self.process_running(descendant) and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertFalse(self.process_running(descendant))
            with self.db() as db:
                self.assertEqual(db.execute("SELECT state FROM downloads").fetchone()[0], "cancelled")
            self.assertEqual(self.status(), "pending")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
            if descendant and self.process_running(descendant):
                os.kill(descendant, signal.SIGKILL)

    def test_direct_player_restores_terminal_modes_after_failure(self):
        self.seed()
        self.fake("yt-dlp", "pass\n")
        self.fake("mpv", "import tty\ntty.setraw(0)\nraise SystemExit(2)\n")
        master, slave = pty.openpty()
        before = termios.tcgetattr(slave)
        try:
            proc = subprocess.Popen([str(BINARY), "play", VID, "--video-output", "window"], env=dict(self.env, DISPLAY=":99"), stdin=slave, stdout=slave, stderr=slave)
            self.assertEqual(proc.wait(timeout=5), 1)
            self.assertEqual(termios.tcgetattr(slave), before)
        finally:
            os.close(master)
            os.close(slave)

    def test_signal_to_foreground_parent_terminates_player(self):
        self.seed()
        self.fake("yt-dlp", "pass\n")
        pidfile = self.root / "player.pid"
        self.fake("mpv", f"import os,time\nfrom pathlib import Path\nPath({str(pidfile)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n")
        proc = subprocess.Popen([str(BINARY), "play", VID, "--video-output", "window"], env=dict(self.env, DISPLAY=":99"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        player = None
        try:
            self.wait_for(pidfile)
            player = int(pidfile.read_text())
            proc.terminate()
            proc.communicate(timeout=5)
            self.assertFalse(self.process_running(player))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
            if player and self.process_running(player):
                os.kill(player, signal.SIGKILL)

    def test_tmux_helper_reattaches_even_with_bad_inherited_profile(self):
        bad = self.root / "bad/config"
        bad.mkdir(parents=True)
        (bad / "settings.json").write_text("broken")
        job = self.root / "job"
        job.mkdir()
        attached = self.root / "attached.json"
        self.fake("tmux", f"import json,sys\nfrom pathlib import Path\nPath({str(attached)!r}).write_text(json.dumps(sys.argv[1:]))\n")
        request = job / "request.json"
        request.write_text(json.dumps(dict(client=dict(tty="/dev/tty3", session="$1", socket="/tmp/test-socket"), command=["mpv"], env=dict(YT_INBOX_HOME=str(self.root)))))
        env = dict(self.env, YT_INBOX_HOME=str(bad.parent))
        p = subprocess.run([str(BINARY), "_tmux-job", str(request)], env=env, capture_output=True, timeout=5)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue((job / "result.json").exists())
        self.assertIn("attach-session", json.loads(attached.read_text()))

    def test_fully_classified_feed_does_not_need_type_lock(self):
        self.seed()
        self.paths.channels.write_text(URL + "\n")
        self.http([self.response("channel_id=" + CID, raw=self.feed())])
        with (self.paths.state / "video-types.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.run_c("check", "--no-notify", driver=True)

    def test_cached_sync_merge_recovers_interrupted_channel_projection(self):
        self.seed()
        self.paths.channels.write_text(URL + "\n")
        initial = self.native_doc()
        remote = copy.deepcopy(initial)
        other_url = "https://www.youtube.com/channel/" + OTHER
        remote["channels"][other_url] = dict(rev=[5, "f" * 32], active=True, channel_id=OTHER, name="Other")
        # Model a crash after committing registers but before channels.txt.
        with self.db() as db:
            syncing.put(db, "channels", other_url, remote["channels"][other_url])
        self.native_doc("_apply", initial)
        self.assertIn(other_url, self.paths.channels.read_text())
        self.assertEqual(self.native_doc(), syncing.snapshot(self.paths))

    def test_tui_pty_restores_terminal_and_reloads_settings(self):
        self.seed()
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 110, 0, 0))
        modes = termios.tcgetattr(slave)
        proc = subprocess.Popen([str(BINARY), "tui"], env=dict(self.env, TERM="xterm-256color"), stdin=slave, stdout=slave, stderr=slave)
        try:
            output = b""
            deadline = time.monotonic() + 5
            while b"Unwatched" not in output and time.monotonic() < deadline:
                if select.select([master], [], [], .2)[0]:
                    output += os.read(master, 65536)
            self.assertIn(b"Unwatched", output)
            self.run_c("config", "download_height", "480")
            os.write(master, b"s")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if not core.settings(self.paths)["hide_shorts"]:
                    break
                time.sleep(.02)
            self.assertEqual(core.settings(self.paths)["download_height"], 480)
            os.write(master, b"q")
            self.assertEqual(proc.wait(timeout=5), 0)
            self.assertEqual(termios.tcgetattr(slave), modes)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main()
