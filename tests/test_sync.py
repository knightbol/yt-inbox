import base64
import copy
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli
import core
import install
import syncing

C1, C2 = "UC" + "a" * 22, "UC" + "b" * 22
V1, V2, V3 = "a" * 11, "b" * 11, "c" * 11
U1, U2 = "https://www.youtube.com/channel/" + C1, "https://www.youtube.com/channel/" + C2


class Remote:
    def __init__(self):
        self.doc, self.sha, self.writes = syncing.empty(), None, 0
        self.private, self.fail, self.on_write = True, False, None

    def repository(self):
        if not self.private:
            raise core.InboxError("PRIVATE repository required")
        if self.fail:
            raise core.InboxError("Offline")
        return {"id": 42, "private": True}

    def read(self):
        return copy.deepcopy(self.doc), self.sha

    def write(self, doc, sha):
        if self.on_write:
            callback, self.on_write = self.on_write, None
            callback()
        self.repository()
        if sha != self.sha:
            raise syncing.Conflict("Concurrent update")
        self.doc = copy.deepcopy(doc)
        self.writes += 1
        self.sha = str(self.writes)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.a, self.b = self.device("a"), self.device("b")
        self.remote = Remote()
        self.kick = patch("syncing.kick").start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.temp.cleanup)

    def device(self, name):
        root = self.root / name
        paths = core.Paths(root / "config", root / "data", root / "state")
        paths.ensure()
        paths.channels.write_text("# My channels\n" + C1 + "\n")
        core.atomic_write(paths.config / "sync.json", syncing.encode({"repository": "owner/private", "repository_id": 42, "token": "test_token"}))
        db = core.connect(paths)
        with db:
            db.execute("INSERT INTO channels(id,name,added_at) VALUES (?, 'Test channel', 10)", (C1,))
            db.execute("INSERT INTO sources(url,channel_id,added_at) VALUES (?,?,10)", (U1, C1))
            for vid in (V1, V2):
                db.execute("INSERT INTO videos VALUES (?,?,?,100,100,'pending',0,100)", (vid, C1, "Video " + vid))
                core.record_video_type(db, vid, False)
        db.close()
        return paths

    def state(self, paths, vid=V1):
        db = core.connect(paths)
        try:
            return db.execute("SELECT status FROM videos WHERE id=?", (vid,)).fetchone()[0]
        finally:
            db.close()

    def run_sync(self, paths):
        return syncing.sync(paths, self.remote)

    def pair(self):
        self.run_sync(self.a)
        self.run_sync(self.b)
        self.run_sync(self.a)

    def test_initial_merge_keeps_existing_watched_skipped_and_second_pc_channels(self):
        core.set_status(self.a, [V1], "watched")
        core.set_status(self.b, [V2], "skipped")
        self.b.channels.write_text(C1 + "\n" + C2 + "\n")
        self.pair()
        for p in (self.a, self.b):
            self.assertEqual(self.state(p), "watched")
            self.assertEqual(self.state(p, V2), "skipped")
            self.assertEqual(set(core.parse_channels(p.channels.read_text())[0]), {U1, U2})
        self.assertIn("# My channels", self.a.channels.read_text())
        self.assertEqual([r["id"] for r in core.queue_rows(self.b, "watched")], [V1])

    def test_offline_changes_on_different_videos_are_joined_without_shared_uptime(self):
        self.pair()
        core.set_status(self.a, [V1], "watched")
        core.set_status(self.b, [V2], "skipped")
        self.remote.fail = True
        with self.assertRaisesRegex(core.InboxError, "Offline"):
            self.run_sync(self.a)
        self.assertEqual(self.state(self.a), "watched")
        self.remote.fail = False
        self.run_sync(self.a)
        self.run_sync(self.b)
        self.run_sync(self.a)
        for p in (self.a, self.b):
            self.assertEqual(self.state(p), "watched")
            self.assertEqual(self.state(p, V2), "skipped")

    def test_same_video_conflict_converges_then_explicit_restore_wins(self):
        self.pair()
        core.set_status(self.a, [V1], "watched")
        core.set_status(self.b, [V1], "skipped")
        a, b = syncing.snapshot(self.a), syncing.snapshot(self.b)
        self.assertEqual(syncing.merge(a, b), syncing.merge(b, a))
        expected = syncing.merge(a, b)["videos"][V1]["status"]
        for p in (self.a, self.b, self.a):
            self.run_sync(p)
        self.assertEqual(self.state(self.a), expected)
        self.assertEqual(self.state(self.b), expected)
        core.set_status(self.b, [V1], "pending")
        self.run_sync(self.b)
        self.run_sync(self.a)
        self.assertEqual(self.state(self.a), "pending")

    def test_channel_removal_does_not_return_from_offline_pc_and_readd_works(self):
        self.pair()
        self.a.channels.write_text("# kept comment\n")
        self.b.channels.write_text(C1 + "\n" + C2 + "\n")
        for p in (self.a, self.b, self.a):
            self.run_sync(p)
        for p in (self.a, self.b):
            self.assertEqual(core.parse_channels(p.channels.read_text())[0], [U2])
            self.assertEqual(self.state(p), "pending")  # History retained.
        self.a.channels.write_text(C1 + "\n" + C2 + "\n")
        self.run_sync(self.a)
        self.run_sync(self.b)
        self.assertEqual(set(core.parse_channels(self.b.channels.read_text())[0]), {U1, U2})

    def test_manual_edit_during_network_read_is_captured(self):
        self.pair()
        original = self.remote.read
        def read():
            self.a.channels.write_text(C2 + "\n")
            core.set_status(self.a, [V1], "watched")
            return original()
        with patch.object(self.remote, "read", side_effect=read):
            self.run_sync(self.a)
        self.run_sync(self.b)
        self.assertEqual(core.parse_channels(self.b.channels.read_text())[0], [U2])
        self.assertEqual(self.state(self.b), "watched")

    def test_stale_gui_channel_editor_applies_only_its_own_delta(self):
        self.pair()
        original = self.a.channels.read_text()
        self.b.channels.write_text(C1 + "\n" + C2 + "\n")
        self.run_sync(self.b)
        self.run_sync(self.a)
        syncing.save_channels(self.a, "# Remove the original channel\n", original)
        self.run_sync(self.a)
        self.run_sync(self.b)
        self.assertEqual(core.parse_channels(self.b.channels.read_text())[0], [U2])

    def test_edit_during_upload_survives_until_next_sync(self):
        self.pair()
        core.set_status(self.a, [V1], "watched")
        self.remote.on_write = lambda: core.set_status(self.a, [V1], "skipped")
        self.run_sync(self.a)
        self.assertEqual(self.state(self.a), "skipped")
        self.run_sync(self.a)
        self.run_sync(self.b)
        self.assertEqual(self.state(self.b), "skipped")

    def test_remote_compare_and_swap_retry_preserves_both_writes(self):
        self.pair()
        core.set_status(self.a, [V1], "watched")
        core.set_status(self.b, [V2], "skipped")
        self.remote.on_write = lambda: self.run_sync(self.b)
        self.run_sync(self.a)
        self.run_sync(self.b)
        self.assertEqual(self.state(self.b), "watched")
        self.assertEqual(self.state(self.a, V2), "skipped")

    def test_undo_is_a_new_revision_and_remote_state_invalidates_stale_undo(self):
        self.pair()
        core.set_status(self.a, [V1], "watched")
        self.run_sync(self.a)
        self.run_sync(self.b)
        self.assertTrue(core.undo(self.a))
        self.run_sync(self.a)
        self.run_sync(self.b)
        self.assertEqual(self.state(self.b), "pending")
        core.set_status(self.a, [V1], "watched")
        self.run_sync(self.a)
        self.run_sync(self.b)
        core.set_status(self.b, [V1], "skipped")
        self.run_sync(self.b)
        self.run_sync(self.a)
        self.assertFalse(core.undo(self.a))

    def test_archived_video_and_type_arrive_without_catalogue_download_or_settings(self):
        self.pair()
        db = core.connect(self.a)
        with db:
            db.execute("INSERT INTO catalogue VALUES (?,?,?,1,1)", (V3, C1, "Older video"))
            core.record_video_type(db, V3, False)
            db.execute("INSERT INTO downloads VALUES (?,'complete','/private/local/video.mkv','',1)", (V1,))
        db.close()
        values = core.settings(self.a)
        core.save_settings(self.a, dict(values, playback_mode="low", terminal_only=True))
        core.set_status(self.a, [V3], "watched")
        self.run_sync(self.a)
        self.run_sync(self.b)
        self.assertEqual(self.state(self.b, V3), "watched")
        self.assertIn(V3, [r["id"] for r in core.queue_rows(self.b, "watched")])
        self.assertNotEqual(core.settings(self.a), core.settings(self.b))
        data = syncing.encode(self.remote.doc)
        self.assertNotIn("/private", data)
        self.assertNotIn("test_token", data)
        self.assertNotIn("playback_mode", data)
        db = core.connect(self.b)
        self.assertEqual(db.execute("SELECT count(*) FROM downloads").fetchone()[0], 0)
        db.close()

    def test_invalid_remote_invalid_channels_and_public_repository_do_not_overwrite(self):
        self.pair()
        before = self.a.channels.read_text()
        self.remote.doc["videos"][V1]["status"] = "garbage"
        with self.assertRaises(ValueError):
            self.run_sync(self.a)
        self.assertEqual(self.state(self.a), "pending")
        self.assertEqual(self.a.channels.read_text(), before)
        self.remote.doc["videos"][V1]["status"] = "pending"
        self.remote.private = False
        count = self.remote.writes
        with self.assertRaisesRegex(core.InboxError, "PRIVATE"):
            self.run_sync(self.a)
        self.assertEqual(self.remote.writes, count)
        self.remote.private = True
        self.a.channels.write_text("not a channel")
        with self.assertRaisesRegex(core.InboxError, "Fix channels.txt"):
            self.run_sync(self.a)
        self.assertEqual(self.remote.writes, count)

    def test_crash_between_register_commit_and_channel_projection_recovers(self):
        self.pair()
        self.b.channels.write_text(C2 + "\n")
        self.run_sync(self.b)
        with patch("core.atomic_write", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.run_sync(self.a)
        self.run_sync(self.a)
        self.assertEqual(core.parse_channels(self.a.channels.read_text())[0], [U2])
        self.run_sync(self.b)
        self.assertEqual(core.parse_channels(self.b.channels.read_text())[0], [U2])

    def test_idempotent_sync_does_not_commit_and_channel_lock_defers(self):
        self.pair()
        count = self.remote.writes
        self.run_sync(self.a)
        self.run_sync(self.b)
        self.assertEqual(self.remote.writes, count)
        with syncing.channel_lock(self.a), self.assertRaises(core.Busy):
            self.run_sync(self.a)

    def test_crash_after_channel_projection_does_not_create_new_channel_edits(self):
        self.pair()
        self.b.channels.write_text(C2 + "\n")
        self.run_sync(self.b)
        old_set = core.meta_set
        calls = 0
        def meta_set(db, key, value):
            nonlocal calls
            if key == "sync_seen_channels":
                calls += 1
                if calls == 3:  # snapshot, capture, then final projection acknowledgement
                    raise OSError("interrupted after replace")
            return old_set(db, key, value)
        with patch("core.meta_set", side_effect=meta_set), self.assertRaises(OSError):
            self.run_sync(self.a)
        count = self.remote.writes
        self.run_sync(self.a)
        self.assertEqual(core.parse_channels(self.a.channels.read_text())[0], [U2])
        self.assertEqual(self.remote.writes, count)

    def test_disabled_sync_does_not_access_network_and_cli_busy_reports_failure(self):
        (self.a.config / "sync.json").unlink()
        with patch("syncing.GitHub") as client:
            syncing.try_sync(self.a)
            self.assertIn("not configured", syncing.sync(self.a))
        client.assert_not_called()
        with patch.dict(os.environ, {"YT_INBOX_HOME": str(self.a.config.parent)}), \
                patch("syncing.sync", side_effect=core.Busy("busy")), patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(cli.main(["sync"]), 1)

    def test_setup_rejects_noninteractive_token_entry_and_unrelated_repository(self):
        with patch("sys.stdin.isatty", return_value=False), self.assertRaisesRegex(core.InboxError, "interactive"):
            syncing.setup(self.a, "owner/private")
        with patch("syncing.GitHub", return_value=self.remote):
            syncing.setup(self.a, "owner/private", token="test_token")
        client = Mock()
        client.repository.return_value = {"id": 99, "private": True}
        with patch("syncing.GitHub", return_value=client), self.assertRaisesRegex(core.InboxError, "different repository"):
            syncing.setup(self.a, "owner/different", token="other_token")
        self.assertEqual(syncing.configuration(self.a)["token"], "test_token")

    def test_setup_and_install_keep_secrets_local_with_backup_and_optional_timer(self):
        (self.a.config / "sync.json").unlink()
        with patch.dict(os.environ, {"YT_INBOX_HOME": str(self.a.config.parent)}):
            install.install(self.a, enable=False, terminal_only=True)
            with patch("syncing.GitHub", return_value=self.remote), patch("install.systemctl") as ctl:
                syncing.setup(self.a, "owner/private", token="test_token")
            ctl.assert_any_call("enable", "--now", "yt-inbox-sync.timer")
            self.assertEqual((self.a.config / "sync.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual((self.a.data / "before-sync.sqlite3").stat().st_mode & 0o777, 0o600)
            db = sqlite3.connect(self.a.data / "before-sync.sqlite3")
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            db.close()
            self.assertTrue((self.a.data / "app" / "syncing.py").is_file())
            self.assertNotIn("test_token", syncing.status(self.a))
            with patch("subprocess.run"):
                syncing.disable(self.a)
            self.assertFalse(syncing.configured(self.a))
            self.assertEqual(self.state(self.a), "pending")


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.client = syncing.GitHub({"repository": "owner/private", "repository_id": 42, "token": "secret_token"})

    def test_large_content_uses_immutable_blob_and_existing_sha_guards_update(self):
        raw = syncing.encode(syncing.empty()).encode()
        sha = "a" * 40
        with patch.object(self.client, "request", side_effect=[{"type": "file", "size": 1_100_000, "sha": sha, "encoding": "none"},
                {"encoding": "base64", "content": base64.b64encode(raw).decode()}]) as request:
            doc, version = self.client.read()
        self.assertEqual(doc, syncing.empty())
        self.assertEqual(request.call_args.args[0], "/repos/owner/private/git/blobs/" + sha)
        with patch.object(self.client, "repository") as private, patch.object(self.client, "request") as request:
            self.client.write(doc, version)
        private.assert_called_once()
        self.assertEqual(request.call_args.args[1]["sha"], sha)

    def test_permissions_rate_limits_and_conflicts_do_not_reveal_credentials(self):
        for code in (301, 401, 403, 404, 409, 422, 429, 500):
            exc = urllib.error.HTTPError("https://api.github.com", code, "secret_token", {}, io.BytesIO(b"secret_token"))
            with patch.object(self.client.opener, "open", side_effect=exc), self.assertRaises(core.InboxError) as caught:
                self.client.request("/repos/owner/private", {"content": "x"})
            self.assertNotIn("secret_token", str(caught.exception))
            self.assertEqual(isinstance(caught.exception, syncing.Conflict), code in (409, 422))
        self.assertIsNone(syncing.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))

    def test_public_or_replaced_repository_is_rejected(self):
        for info in ({"id": 42, "private": False}, {"id": 43, "private": True}):
            with patch.object(self.client, "request", return_value=info), self.assertRaises(core.InboxError):
                self.client.repository()


if __name__ == "__main__":
    unittest.main()
