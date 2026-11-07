"""Opt-in, stdlib-only channel/history sync through a private GitHub repository.

SQLite stays local. Per-item logical revisions merge independently; a contents
SHA is the remote compare-and-swap guard. No network request holds a DB lock.
"""
from __future__ import annotations

import base64
import contextlib
import datetime as dt
import getpass
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import warnings

import core

REMOTE_FILE = "yt-inbox-state.json"
MAX_BYTES = 16_000_000
STATES = ("baseline", "pending", "skipped", "watched")
REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")


class Conflict(core.InboxError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def empty():
    return {"format": "yt-inbox-sync", "version": 1, "channels": {}, "videos": {}}


def configured(paths):
    return (paths.config / "sync.json").is_file()


def configuration(paths):
    try:
        value = json.loads((paths.config / "sync.json").read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not REPO_RE.fullmatch(value.get("repository", "")):
            raise ValueError()
        if not isinstance(value.get("token"), str) or not re.fullmatch(r"[A-Za-z0-9_]+", value["token"]):
            raise ValueError()
        if type(value.get("repository_id")) is not int or value["repository_id"] < 1:
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError):
        raise core.InboxError("Invalid sync configuration. Run yt-inbox sync setup OWNER/REPO again.") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the private repository token through a redirect.
        return None


class GitHub:
    def __init__(self, config):
        self.config = config
        self.base = "/repos/" + config["repository"]
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, path, data=None, missing=False):
        body = encode(data).encode() if data is not None else None
        req = urllib.request.Request("https://api.github.com" + path, data=body,
            method="PUT" if body is not None else "GET", headers={
                "Authorization": "Bearer " + self.config["token"],
                "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2026-03-10", "User-Agent": "yt-inbox/" + core.VERSION})
        try:
            with self.opener.open(req, timeout=20) as response:
                raw = response.read(MAX_BYTES * 2 + 1)
            if len(raw) > MAX_BYTES * 2:
                raise core.InboxError("Sync response exceeds the 16 MB state limit.")
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code == 404 and missing:
                return None
            if code in (409, 422) and data is not None:
                raise Conflict("Remote history changed during sync; retrying.") from None
            messages = {401: "GitHub token expired or invalid. Run sync setup again.",
                        403: "GitHub denied access or rate-limited sync. Check token permissions and retry later.",
                        404: "Private sync repository not found or token has no access.",
                        429: "GitHub rate-limited sync. It will retry later."}
            raise core.InboxError(messages.get(code, f"GitHub sync failed (HTTP {code}). Local changes are retained.")) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise core.InboxError("GitHub is unreachable. Local changes are retained for the next sync.") from None
        except (ValueError, UnicodeError):
            raise core.InboxError("GitHub returned an invalid sync response.") from None

    def repository(self):
        info = self.request(self.base)
        if not isinstance(info, dict) or info.get("private") is not True or info.get("visibility", "private") != "private":
            raise core.InboxError("Sync requires a PRIVATE repository. No history was uploaded.")
        if type(info.get("id")) is not int:
            raise core.InboxError("Invalid repository metadata.")
        expected = self.config.get("repository_id")
        if expected and info["id"] != expected:
            raise core.InboxError("The sync repository identity changed. Run sync setup again.")
        if info.get("archived") or info.get("disabled"):
            raise core.InboxError("The sync repository is archived or disabled.")
        return info

    def read(self):
        value = self.request(self.base + "/contents/" + REMOTE_FILE, missing=True)
        if value is None:
            return empty(), None
        if not isinstance(value, dict) or value.get("type") != "file" or not re.fullmatch(r"[a-f0-9]{40}", value.get("sha", "")):
            raise core.InboxError("The remote sync path is not a regular file.")
        sha = value["sha"]
        if value.get("size", MAX_BYTES + 1) > MAX_BYTES:
            raise core.InboxError("Remote sync state exceeds 16 MB; nothing was truncated.")
        # Contents omits base64 for files over 1 MB. The immutable blob endpoint
        # still gives us that exact SHA, without a second mutable-branch read.
        if value.get("encoding") != "base64":
            value = self.request(self.base + "/git/blobs/" + sha)
        try:
            raw = base64.b64decode("".join(value["content"].split()), validate=True)
            if len(raw) > MAX_BYTES:
                raise ValueError()
            doc = json.loads(raw)
            validate(doc)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise core.InboxError("Invalid remote sync data. Local history was not changed.") from None
        return doc, sha

    def write(self, doc, sha):
        validate(doc)
        self.repository()  # Recheck visibility immediately before uploading.
        raw = (encode(doc) + "\n").encode()
        if len(raw) > MAX_BYTES:
            raise core.InboxError("Sync state exceeds 16 MB; nothing was truncated or uploaded.")
        body = {"message": "Synchronize YouTube Inbox", "content": base64.b64encode(raw).decode()}
        if sha:
            body["sha"] = sha
        self.request(self.base + "/contents/" + REMOTE_FILE, body)


def validate(doc):
    if not isinstance(doc, dict) or doc.get("format") != "yt-inbox-sync" or doc.get("version") != 1:
        raise ValueError("Unsupported sync format")
    for kind in ("channels", "videos"):
        if not isinstance(doc.get(kind), dict) or len(doc[kind]) > 100_000:
            raise ValueError("Invalid records")
        for key, record in doc[kind].items():
            if not isinstance(record, dict):
                raise ValueError("Invalid record")
            rev = record.get("rev")
            if (not isinstance(rev, list) or len(rev) != 2 or type(rev[0]) is not int or
                    not 0 <= rev[0] < 2**53 or not isinstance(rev[1], str) or
                    not re.fullmatch(r"[a-f0-9]{32}", rev[1])):
                raise ValueError("Invalid revision")
            cid = record.get("channel_id")
            if not isinstance(cid, str) or not (core.CHANNEL_RE.fullmatch(cid) or kind == "channels" and cid == ""):
                raise ValueError("Invalid channel")
            for field in (("name",) if kind == "channels" else ("name", "title")):
                if not isinstance(record.get(field), str) or len(record[field]) > 4096 or any(ord(c) < 32 for c in record[field]):
                    raise ValueError("Invalid text")
            if kind == "channels":
                if len(key) > 2048 or core.canonical_channel(key) != key or type(record.get("active")) is not bool:
                    raise ValueError("Invalid subscription")
            else:
                if not core.VIDEO_RE.fullmatch(key) or record.get("status") not in STATES:
                    raise ValueError("Invalid video")
                stamp = record.get("published")
                if type(stamp) not in (int, float) or not math.isfinite(stamp) or not 0 <= stamp <= 253402214400:
                    raise ValueError("Invalid date")
                if record.get("short") is not None and type(record["short"]) is not bool:
                    raise ValueError("Invalid video type")
    return doc


def winner(a, b):
    # Existing, unsynced history starts at revision zero. During first pairing
    # watched > skipped > pending, so an unwatched second PC cannot erase history.
    def order(r):
        return (r["rev"][0], STATES.index(r["status"]) if r["rev"][0] == 0 and "status" in r else 0,
                r["rev"][1], encode(r))
    chosen = dict(max((a, b), key=order))
    # Metadata is independent of the user's state and can arrive after it.
    for field in ("channel_id", "name", "title"):
        if field in a:
            chosen[field] = max((a[field], b[field]), key=lambda s: (len(s), s))
    if "published" in a:
        chosen["published"] = max(a["published"], b["published"])
        kinds = [r.get("short") for r in (a, b) if r.get("short") is not None]
        chosen["short"] = any(kinds) if kinds else None
    return chosen


def merge(a, b):
    result = empty()
    for kind in ("channels", "videos"):
        for key in a[kind].keys() | b[kind].keys():
            result[kind][key] = winner(a[kind][key], b[kind][key]) if key in a[kind] and key in b[kind] else dict((a[kind] if key in a[kind] else b[kind])[key])
    return result


def document(db):
    doc = empty()
    for row in db.execute("SELECT kind,key,record FROM sync_records"):
        doc[row["kind"]][row["key"]] = json.loads(row["record"])
    return doc


def put(db, kind, key, record):
    db.execute("INSERT INTO sync_records VALUES (?,?,?) ON CONFLICT(kind,key) DO UPDATE SET record=excluded.record",
               (kind, key, encode(record)))


def revision(db, seed=False):
    clock = int(core.meta_get(db, "sync_clock", "0"))
    if not seed:
        clock += 1
        core.meta_set(db, "sync_clock", clock)
    return [0 if seed else clock, core.meta_get(db, "sync_device")]


def video_record(db, vid, rev):
    row = db.execute("SELECT v.*,c.name,t.is_short FROM videos v JOIN channels c ON c.id=v.channel_id "
                     "LEFT JOIN video_types t ON t.id=v.id WHERE v.id=?", (vid,)).fetchone()
    return {"rev": rev, "status": row["status"], "channel_id": row["channel_id"],
            "title": clean(row["title"]), "name": clean(row["name"]) if row["name"] != row["channel_id"] else "",
            "published": max(0, row["published"]),
            "short": None if row["is_short"] is None else bool(row["is_short"])}


def clean(text):
    return "".join(c for c in text if ord(c) >= 32)[:4096]


def record_status(db, vid):
    """Called inside the same transaction as a manual choice (including Undo)."""
    if core.meta_get(db, "sync_device"):
        put(db, "videos", vid, video_record(db, vid, revision(db)))


def capture(db, paths):
    text = paths.channels.read_text(encoding="utf-8")
    urls, errors = core.parse_channels(text)
    if errors:
        raise core.InboxError("Fix channels.txt before syncing: " + "; ".join(errors))
    if not core.meta_get(db, "sync_device"):
        core.meta_set(db, "sync_device", uuid.uuid4().hex)
    before = core.meta_get(db, "sync_seen_channels")
    previous = set(json.loads(before)) if before else set()
    doc = document(db)
    for url in set(urls) | previous:
        old = doc["channels"].get(url)
        active = url in urls
        row = db.execute("SELECT s.channel_id,c.name FROM sources s LEFT JOIN channels c ON c.id=s.channel_id WHERE s.url=?", (url,)).fetchone()
        cid = row["channel_id"] if row and row["channel_id"] else ""
        if not cid and url.startswith("https://www.youtube.com/channel/"):
            cid = url.rsplit("/", 1)[1]
        name = clean(row["name"]) if row and row["name"] and row["name"] != cid else ""
        # Compare to what was last on disk, not to the remote subscriptions. A
        # missing remote addition is not a user deletion before it is projected.
        changed = active != (url in previous)
        if old is None or changed and old["active"] != active:
            put(db, "channels", url, {"rev": revision(db, seed=not before), "active": active,
                                      "channel_id": cid, "name": name})
        elif cid or name:
            put(db, "channels", url, winner(old, dict(old, channel_id=cid, name=name)))
    core.meta_set(db, "sync_seen_channels", encode(urls))
    for row in db.execute("SELECT id,status FROM videos WHERE status!='baseline'").fetchall():
        old = doc["videos"].get(row["id"])
        current = video_record(db, row["id"], old["rev"] if old else revision(db, seed=True))
        if old:
            current["status"] = old["status"]  # Explicit edits are recorded atomically by record_status.
            current = winner(old, current)
        put(db, "videos", row["id"], current)
    return text


@contextlib.contextmanager
def channel_lock(paths):
    try:
        with core.exclusive_lock(paths.state / "channels.lock"):
            yield
    except core.Busy:
        raise core.Busy("Channels are being edited or synchronized. Try again shortly.") from None


def save_channels(paths, text, original=None):
    with channel_lock(paths):
        current = paths.channels.read_text(encoding="utf-8")
        if original is not None and current != original:
            # A GUI editor can remain open while another PC adds/removes a
            # channel. Apply the user's edit delta to the new list, not the
            # stale whole document from when the dialog opened.
            before, e1 = core.parse_channels(original)
            edited, e2 = core.parse_channels(text)
            latest, e3 = core.parse_channels(current)
            if e1 or e2 or e3:
                raise core.InboxError("Channels changed while editing. Close and reopen the channel editor.")
            wanted = (set(latest) | (set(edited) - set(before))) - (set(before) - set(edited))
            text = render_channels(text, wanted)
        core.atomic_write(paths.channels, text)
    kick(paths)


def snapshot(paths):
    with channel_lock(paths):
        db = core.connect(paths)
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                capture(db, paths)
                return document(db)
        finally:
            db.close()


def render_channels(text, active):
    lines, kept = [], set()
    for line in text.splitlines():
        values, _ = core.parse_channels(line)
        if not values:
            lines.append(line)
        elif values[0] in active and values[0] not in kept:
            lines.append(line)
            kept.add(values[0])
    lines.extend(sorted(active - kept))
    return "\n".join(lines).rstrip() + "\n"


def apply(paths, incoming):
    validate(incoming)
    with channel_lock(paths):
        db = core.connect(paths)
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                text = capture(db, paths)  # Retain edits made while the network was busy.
                merged = merge(document(db), incoming)
                max_clock = max([int(core.meta_get(db, "sync_clock", "0"))] +
                                [r["rev"][0] for kind in ("channels", "videos") for r in merged[kind].values()])
                core.meta_set(db, "sync_clock", max_clock)
                now = time.time()
                for kind in ("channels", "videos"):
                    for key, record in merged[kind].items():
                        put(db, kind, key, record)
                for vid, r in merged["videos"].items():
                    db.execute("INSERT OR IGNORE INTO channels(id,name,added_at,active) VALUES (?,?,?,0)", (r["channel_id"], r["name"] or r["channel_id"], now))
                    old = db.execute("SELECT status FROM videos WHERE id=?", (vid,)).fetchone()
                    if old and old[0] != r["status"]:
                        db.execute("UPDATE actions SET undone=1 WHERE video_id=?", (vid,))
                    db.execute("INSERT INTO videos VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                               "status=excluded.status,title=excluded.title,published=MAX(videos.published,excluded.published),"
                               "changed=CASE WHEN videos.status!=excluded.status THEN excluded.changed ELSE videos.changed END",
                               (vid, r["channel_id"], r["title"], r["published"], now, r["status"], 1, now))
                    core.record_video_type(db, vid, r.get("short"))
                db.execute("UPDATE sources SET active=0")
                for url, r in merged["channels"].items():
                    cid = r["channel_id"] or None
                    if cid:
                        db.execute("INSERT OR IGNORE INTO channels(id,name,added_at,active) VALUES (?,?,?,0)", (cid, r["name"] or cid, now))
                    db.execute("INSERT INTO sources(url,channel_id,added_at,active) VALUES (?,?,?,?) ON CONFLICT(url) DO UPDATE SET "
                               "active=excluded.active,channel_id=COALESCE(excluded.channel_id,sources.channel_id)", (url, cid, now, int(r["active"])))
                db.execute("UPDATE channels SET active=0")
                db.execute("UPDATE channels SET active=1 WHERE id IN (SELECT channel_id FROM sources WHERE active=1)")
            # Commit the registers BEFORE projecting their channel file. A crash
            # before/after this replace is idempotent on the next capture.
            active = {url for url, r in merged["channels"].items() if r["active"]}
            rendered = render_channels(text, active)
            if paths.channels.read_text(encoding="utf-8") != text:
                raise core.Busy("channels.txt changed during sync; retrying on the next run.")
            if rendered != text:
                core.atomic_write(paths.channels, rendered)
            with db:
                core.meta_set(db, "sync_seen_channels", encode(core.parse_channels(rendered)[0]))
            return merged
        finally:
            db.close()


def sync(paths, client=None):
    if not configured(paths):
        return "Sync is not configured. Run: yt-inbox sync setup OWNER/REPO"
    with core.exclusive_lock(paths.state / "sync.lock"), core.exclusive_lock(paths.state / "check.lock"):
        try:
            client = client or GitHub(configuration(paths))
            client.repository()
            snapshot(paths)
            for attempt in range(4):
                remote, sha = client.read()
                merged = apply(paths, remote)
                if merged != remote:
                    try:
                        client.write(merged, sha)
                    except Conflict:
                        if attempt < 3:
                            continue
                        raise core.InboxError("Sync remained busy after four attempts. Local changes are retained.") from None
                db = core.connect(paths)
                try:
                    with db:
                        core.meta_set(db, "sync_last_success", time.time())
                        core.meta_set(db, "sync_error", "")
                finally:
                    db.close()
                return "Channels and watch history synchronized."
        except (core.InboxError, OSError, ValueError, sqlite3.Error) as exc:
            db = core.connect(paths)
            try:
                with db:
                    core.meta_set(db, "sync_error", str(exc)[:500])
            finally:
                db.close()
            raise


def try_sync(paths):
    if configured(paths):
        try:
            sync(paths)
        except (core.InboxError, OSError, ValueError, sqlite3.Error):
            pass  # Offline use and normal upload checks must keep working.


def kick(paths):
    """Request the existing user service; never perform HTTP in the UI thread."""
    if not configured(paths) or not (paths.data / "app" / "syncing.py").exists():
        return
    try:
        subprocess.run(["systemctl", "--user", "start", "--no-block", "yt-inbox-sync.service"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        pass


def status(paths):
    if not configured(paths):
        return "Sync is not configured. Run: yt-inbox sync setup OWNER/REPO"
    config = configuration(paths)
    db = core.connect(paths)
    try:
        stamp = core.meta_get(db, "sync_last_success")
        when = dt.datetime.fromtimestamp(float(stamp)).strftime("%Y-%m-%d %H:%M") if stamp else "never"
        return f"Sync: {config['repository']} | Last success: {when}\n" + (core.meta_get(db, "sync_error") or "No sync errors recorded.")
    finally:
        db.close()


def setup(paths, repository, token=None):
    from install import locations, systemctl
    if not REPO_RE.fullmatch(repository):
        raise core.InboxError("Use OWNER/REPO for a separate PRIVATE repository.")
    if token is None:
        if not sys.stdin.isatty():
            raise core.InboxError("Run sync setup in an interactive terminal so the token can be entered without echo.")
        print("Use a fine-grained GitHub token restricted to this private repository, Contents: Read and write.")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                token = getpass.getpass("GitHub token (hidden): ").strip()
        except (getpass.GetPassWarning, EOFError):
            raise core.InboxError("A hidden token prompt is unavailable. Run setup in a local interactive terminal.") from None
    if not token or not re.fullmatch(r"[A-Za-z0-9_]+", token):
        raise core.InboxError("Invalid token. Paste it only into the hidden local prompt.")
    config = {"repository": repository, "token": token}
    info = GitHub(config).repository()
    config["repository_id"] = info["id"]
    with core.exclusive_lock(paths.state / "sync.lock"), channel_lock(paths):
        db = core.connect(paths)
        try:
            previous = core.meta_get(db, "sync_repository_id")
            if previous and previous != str(info["id"]):
                raise core.InboxError("This installation is paired with a different repository. Reuse the original repository.")
            backup = paths.data / "before-sync.sqlite3"
            if not backup.exists():
                fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                with contextlib.closing(sqlite3.connect(backup)) as dest:
                    db.backup(dest)
                core.atomic_write(paths.config / "channels.before-sync.txt", paths.channels.read_text(encoding="utf-8"))
            with db:
                db.execute("BEGIN IMMEDIATE")
                capture(db, paths)
                core.meta_set(db, "sync_repository_id", info["id"])
            core.atomic_write(paths.config / "sync.json", encode(config) + "\n")
        finally:
            db.close()
    try:
        return sync(paths)
    finally:
        _, units, _ = locations(paths)
        if (units / "yt-inbox-sync.timer").exists():
            systemctl("daemon-reload")
            systemctl("enable", "--now", "yt-inbox-sync.timer")
        else:
            print("Run install.sh to enable automatic sync; manual sync is available now.")


def disable(paths):
    # Serialize with an in-flight run before forgetting the credentials.
    with core.exclusive_lock(paths.state / "sync.lock"):
        (paths.config / "sync.json").unlink(missing_ok=True)
    try:
        subprocess.run(["systemctl", "--user", "disable", "--now", "yt-inbox-sync.timer"],
                       capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "Sync disabled; local and remote history retained. The saved token was removed."
