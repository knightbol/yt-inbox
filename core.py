"""YouTube Inbox: feed fetching, durable queue and scheduling state (stdlib only)."""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import html.parser
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

VERSION = "1.3.1"
RECENT_UPLOADS = 15
FALLBACK_SECONDS = 120
CHANNEL_RE = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
VIDEO_RE = re.compile(r"[A-Za-z0-9_-]{11}\Z")
ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"
DEFAULT_CHANNELS = "# One YouTube channel URL or @handle per line.\n# Start with each channel's latest video, then track new uploads.\n# Example: https://www.youtube.com/@TechnologyConnections\n"


class InboxError(Exception):
    pass


class Busy(InboxError):
    pass


@dataclass
class Paths:
    config: Path
    data: Path
    state: Path

    @classmethod
    def discover(cls):
        if os.environ.get("YT_INBOX_HOME"):
            root = Path(os.environ["YT_INBOX_HOME"]).expanduser().resolve()
            return cls(root / "config", root / "data", root / "state")
        home = Path.home()
        return cls(Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "yt-inbox",
                   Path(os.environ.get("XDG_DATA_HOME", home / ".local/share")) / "yt-inbox",
                   Path(os.environ.get("XDG_STATE_HOME", home / ".local/state")) / "yt-inbox")

    @property
    def channels(self):
        return self.config / "channels.txt"

    @property
    def database(self):
        return self.data / "inbox.sqlite3"

    def ensure(self):
        for directory in (self.config, self.data, self.state):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.channels.exists():
            atomic_write(self.channels, DEFAULT_CHANNELS)
        if not (self.config / "settings.json").exists():
            save_settings(self, {"daily_time": "19:00", "player": "mpv", "hide_shorts": True})


def atomic_write(path: Path, content: str, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".inbox-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def settings(paths):
    try:
        data = json.loads((paths.config / "settings.json").read_text())
        if not isinstance(data, dict):
            raise ValueError("expected an object")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", data.get("daily_time", "")):
            raise ValueError("daily_time must be HH:MM")
        if data.get("player") not in ("mpv", "browser"):
            raise ValueError("player must be mpv or browser")
        if data.get("download_height", 720) not in (480, 720, 1080):
            raise ValueError("download_height must be 480, 720 or 1080")
        if type(data.get("hide_shorts", True)) is not bool:
            raise ValueError("hide_shorts must be true or false")
        return data
    except (OSError, ValueError, TypeError) as exc:
        raise InboxError(f"Invalid settings: {exc}") from exc


def save_settings(paths, data):
    atomic_write(paths.config / "settings.json", json.dumps(data, indent=2) + "\n")


@contextlib.contextmanager
def exclusive_lock(path):
    with open(path, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Busy("Another check is already running. The inbox will update when it finishes.") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def connect(paths):
    db = sqlite3.connect(paths.database, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS sources(
        url TEXT PRIMARY KEY, channel_id TEXT, added_at REAL NOT NULL,
        active INTEGER NOT NULL DEFAULT 1, error TEXT NOT NULL DEFAULT '');
      CREATE TABLE IF NOT EXISTS channels(
        id TEXT PRIMARY KEY, name TEXT NOT NULL, added_at REAL NOT NULL,
        active INTEGER NOT NULL DEFAULT 1, last_check REAL,
        last_cycle TEXT, error TEXT NOT NULL DEFAULT '');
      CREATE TABLE IF NOT EXISTS videos(
        id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES channels(id),
        title TEXT NOT NULL, published REAL NOT NULL, discovered REAL NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','watched','skipped','baseline')),
        notified INTEGER NOT NULL DEFAULT 0, changed REAL NOT NULL);
      CREATE INDEX IF NOT EXISTS video_queue ON videos(status, published);
      CREATE TABLE IF NOT EXISTS actions(
        id INTEGER PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(id),
        old_status TEXT NOT NULL, new_status TEXT NOT NULL, happened REAL NOT NULL,
        undone INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS catalogue(
        id TEXT PRIMARY KEY, channel_id TEXT NOT NULL REFERENCES channels(id),
        title TEXT NOT NULL, position INTEGER NOT NULL, generation INTEGER NOT NULL);
      CREATE INDEX IF NOT EXISTS catalogue_channel ON catalogue(channel_id,generation,position);
      CREATE TABLE IF NOT EXISTS catalogue_state(
        channel_id TEXT PRIMARY KEY REFERENCES channels(id), next_start INTEGER NOT NULL,
        exhausted INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL, updated REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS downloads(
        id TEXT PRIMARY KEY, state TEXT NOT NULL CHECK(state IN ('queued','downloading','complete','failed','cancelled')),
        path TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', updated REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS video_types(
        id TEXT PRIMARY KEY, is_short INTEGER CHECK(is_short IN (0,1)),
        checked REAL NOT NULL, retry_after REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
    """)
    return db


def hide_shorts(paths):
    return settings(paths).get("hide_shorts", True)


def visibility_clause(hide, alias="v"):
    # Unknown types stay hidden until confirmed; watch URLs and duration alone
    # do not establish that a video is not a Short. Aliases are internal constants.
    return f"EXISTS (SELECT 1 FROM video_types t WHERE t.id={alias}.id AND t.is_short=0)" if hide else "1"


def record_video_type(db, video_id, is_short):
    if type(is_short) is bool:
        db.execute("INSERT INTO video_types(id,is_short,checked) VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET "
                   "is_short=excluded.is_short,checked=excluded.checked,retry_after=0,error=''",
                   (video_id, int(is_short), time.time()))


def short_from_metadata(data):
    kind = data.get("media_type")
    if kind in ("short", "video", "livestream"):
        return kind == "short"
    # A Shorts navigation URL is positive evidence; a normal watch URL is not.
    for key in ("url", "webpage_url"):
        value = data.get(key)
        if isinstance(value, str):
            parsed = urllib.parse.urlparse(value)
            if parsed.hostname in ("youtube.com", "www.youtube.com", "m.youtube.com") and parsed.path.startswith("/shorts/"):
                return True
    return None


def pending_count(db, hide=False):
    return db.execute("SELECT count(*) FROM videos v JOIN channels c ON c.id=v.channel_id "
                      "WHERE c.active=1 AND v.status='pending' AND " + visibility_clause(hide)).fetchone()[0]


def meta_get(db, key, default=""):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(db, key, value):
    db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def canonical_channel(value):
    value = value.strip()
    if CHANNEL_RE.fullmatch(value):
        return "https://www.youtube.com/channel/" + value
    if value.startswith("@"):
        value = "https://www.youtube.com/" + value
    elif value.startswith(("youtube.com/", "www.youtube.com/", "m.youtube.com/")):
        value = "https://" + value
    url = urllib.parse.urlsplit(value)
    if url.scheme not in ("https", "http") or url.hostname not in ("youtube.com", "www.youtube.com", "m.youtube.com"):
        raise InboxError("Use a YouTube channel link or @handle.")
    if url.username or url.password or url.port not in (None, 80, 443):
        raise InboxError("Invalid channel address.")
    bits = [b for b in url.path.split("/") if b]
    if not bits:
        raise InboxError("A YouTube home-page link is not a channel.")
    if bits[0].startswith("@") and len(bits[0]) > 1:
        count = 1
    elif bits[0] in ("channel", "c", "user") and len(bits) >= 2:
        count = 2
        if bits[0] == "channel" and not CHANNEL_RE.fullmatch(bits[1]):
            raise InboxError("Invalid YouTube channel ID.")
    else:
        raise InboxError("Use a channel URL, not a video or playlist URL.")
    if len(bits) > count and bits[count:] not in (["videos"], ["shorts"], ["streams"], ["featured"], ["about"]):
        raise InboxError("Unrecognised channel URL.")
    return "https://www.youtube.com/" + "/".join(bits[:count])


def parse_channels(text):
    urls, errors = [], []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            url = canonical_channel(line)
            if url not in urls:
                urls.append(url)
        except (ValueError, InboxError) as exc:
            errors.append(f"Line {number}: {exc}")
    return urls, errors


def fetch(url, limit=5_000_000):
    req = urllib.request.Request(url, headers={"User-Agent": "YouTubeInbox/1.0 (personal feed reader)", "Accept": "application/atom+xml,text/html;q=0.9,*/*;q=0.5"})
    with urllib.request.urlopen(req, timeout=20) as response:
        content = response.read(limit + 1)
    if len(content) > limit:
        raise InboxError("YouTube returned an unexpectedly large response.")
    return content


class ChannelPage(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        value = None
        if tag == "meta" and attrs.get("itemprop") == "channelId":
            value = attrs.get("content")
        if tag == "link" and attrs.get("type") in ("application/rss+xml", "application/atom+xml"):
            value = urllib.parse.parse_qs(urllib.parse.urlsplit(attrs.get("href", "")).query).get("channel_id", [None])[0]
        if value and CHANNEL_RE.fullmatch(value):
            self.ids.append(value)


def resolve_channel(url):
    tail = url.rsplit("/", 1)[-1]
    if CHANNEL_RE.fullmatch(tail):
        return tail
    try:
        parser = ChannelPage()
        parser.feed(fetch(url).decode("utf-8", errors="replace"))
        ids = set(parser.ids)
        if len(ids) == 1:
            return ids.pop()
    except Exception:
        pass
    if not shutil.which("yt-dlp"):
        raise InboxError("Could not resolve the channel. Install yt-dlp or use its /channel/UC… link.")
    try:
        result = subprocess.run(["yt-dlp", "--ignore-config", "--flat-playlist", "--playlist-end", "1", "--dump-single-json", "--skip-download", "--no-warnings", "--socket-timeout", "15", "--retries", "1", "--extractor-retries", "1", "--", url], capture_output=True, text=True, timeout=75)
        if result.returncode:
            raise InboxError(result.stderr.strip()[-450:] or "Channel lookup failed.")
        data = json.loads(result.stdout)
        for candidate in (data.get("channel_id"), data.get("id")):
            if isinstance(candidate, str) and CHANNEL_RE.fullmatch(candidate):
                return candidate
    except (subprocess.TimeoutExpired, ValueError) as exc:
        raise InboxError("YouTube channel lookup timed out or returned invalid data.") from exc
    raise InboxError("Could not resolve this channel. Use its /channel/UC… link.")


def parse_feed(raw, channel_id):
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise InboxError("YouTube returned an invalid upload feed.") from exc
    if root.tag != ATOM + "feed":
        raise InboxError("YouTube returned a page instead of an upload feed.")
    owner = root.findtext(YT + "channelId")
    # Channel feed roots currently omit UC; per-entry channel IDs include it.
    if owner and owner not in (channel_id, channel_id[2:]):
        raise InboxError("The upload feed belongs to a different channel.")
    name = root.findtext(ATOM + "title") or channel_id
    entries = []
    for entry in root.findall(ATOM + "entry"):
        entry_owner = entry.findtext(YT + "channelId")
        if entry_owner and entry_owner not in (channel_id, channel_id[2:]):
            raise InboxError("An upload belongs to a different channel.")
        video_id = entry.findtext(YT + "videoId") or ""
        if not VIDEO_RE.fullmatch(video_id):
            raise InboxError("An upload has an invalid video ID; the check will be retried.")
        published = entry.findtext(ATOM + "published") or ""
        try:
            stamp = dt.datetime.fromisoformat(published.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("missing timezone")
        except ValueError as exc:
            raise InboxError("An upload has an invalid publication date; the check will be retried.") from exc
        entries.append({"id": video_id, "title": entry.findtext(ATOM + "title") or "Untitled video", "published": stamp.timestamp()})
    return name, entries


def ytdlp_json(url, options, timeout=60):
    """Read public metadata only, ignoring user config and never loading cookies."""
    if not shutil.which("yt-dlp"):
        raise InboxError("Install yt-dlp to retrieve YouTube video lists.")
    try:
        result = subprocess.run([
            "yt-dlp", "--ignore-config", "--dump-single-json", "--skip-download",
            "--no-warnings", "--socket-timeout", "10", "--retries", "0",
            "--extractor-retries", "0", *options, "--", url,
        ], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise InboxError("YouTube metadata lookup timed out; it will be retried.") from exc
    if result.returncode:
        raise InboxError(result.stderr.strip()[-450:] or "YouTube metadata lookup failed.")
    try:
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise ValueError("expected an object")
    except (ValueError, TypeError) as exc:
        raise InboxError("yt-dlp returned invalid upload metadata.") from exc
    return data


def fallback_uploads(channel_id, known, channel_name):
    """Return recent uploads and any errors, retaining successful partial results.

    Flat playlists have no exact publication times. Reuse stored dates for known
    IDs; retrieve precise metadata for new IDs instead of guessing relative dates.
    A time budget bounds each channel's work. Successful entries are saved even if
    another lookup fails, so a later retry only needs the remaining metadata.
    """
    deadline = time.monotonic() + FALLBACK_SECONDS
    playlist_id = "UU" + channel_id[2:]
    data = ytdlp_json("https://www.youtube.com/playlist?list=" + playlist_id,
                       ["--flat-playlist", "--playlist-end", str(RECENT_UPLOADS)])
    if data.get("id") != playlist_id or data.get("channel_id") not in (None, channel_id):
        raise InboxError("The upload list belongs to a different channel.")
    rows = data.get("entries")
    if not isinstance(rows, list):
        raise InboxError("YouTube did not return an upload list.")
    name = data.get("channel") or channel_name
    entries, errors = [], []
    seen = set()
    for item in rows[:RECENT_UPLOADS]:
        try:
            if not isinstance(item, dict):
                raise InboxError("An upload could not be read.")
            video_id = item.get("id")
            if not isinstance(video_id, str) or not VIDEO_RE.fullmatch(video_id):
                raise InboxError("An upload has an invalid video ID.")
            if item.get("channel_id") not in (None, channel_id):
                raise InboxError("An upload belongs to a different channel.")
            if video_id in seen:
                continue
            seen.add(video_id)
            is_short = short_from_metadata(item)
            if video_id in known and known[video_id]["published"] > 0:
                published = known[video_id]["published"]
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    errors.append("Upload lookup time limit reached; remaining videos will be retried.")
                    break
                details = ytdlp_json(video_url(video_id), [
                    "--no-playlist", "--ignore-no-formats-error", "--extractor-args",
                    "youtube:player_client=web;player_skip=js,configs;skip=hls,dash",
                ], timeout=min(60, remaining))
                if details.get("id") != video_id or details.get("channel_id") != channel_id:
                    raise InboxError("Video metadata belongs to a different video or channel.")
                is_short = short_from_metadata(details)
                published = details.get("release_timestamp") or details.get("timestamp")
                if type(published) not in (int, float) or not math.isfinite(published) or published <= 0:
                    raise InboxError(f"Publication time unavailable for {video_id}; it will be retried.")
            entries.append({"id": video_id, "title": item.get("title") or "Untitled video", "published": published,
                            "is_short": is_short})
        except (InboxError, OSError) as exc:
            errors.append(str(exc))
    return name, entries, errors


def read_uploads(channel_id, known, channel_name, fetcher=fetch):
    try:
        name, entries = parse_feed(fetcher("https://www.youtube.com/feeds/videos.xml?channel_id=" + channel_id), channel_id)
        return name, entries, []
    except urllib.error.HTTPError as exc:
        # Active channels can return 404/5xx from RSS while their upload lists work.
        # Do not retry access denials or rate limits through another endpoint.
        if exc.code != 404 and not 500 <= exc.code < 600:
            raise
        feed_error = f"YouTube feed unavailable (HTTP {exc.code})"
    try:
        name, entries, errors = fallback_uploads(channel_id, known, channel_name)
        return name, entries, [feed_error + "; " + error for error in errors]
    except (InboxError, OSError) as exc:
        raise InboxError(feed_error + "; upload-list fallback: " + str(exc)) from exc


def ingest(db, channel_id, name, entries, now, cycle=None, complete=True, filter_shorts=False):
    channel = db.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
    new_count = 0
    with db:
        for item in entries:
            record_video_type(db, item["id"], item.get("is_short"))
        visible = {r[0] for r in db.execute("SELECT id FROM video_types WHERE is_short=0")} if filter_shorts else None
        for item in entries:
            show = visible is None or item["id"] in visible
            status = "pending" if item["published"] >= channel["added_at"] else "baseline"
            existing = db.execute("SELECT status,published FROM videos WHERE id=?", (item["id"],)).fetchone()
            if existing:
                # An undone archive-only choice may have no exact date yet.
                # Once known, let normal tracking classify it; preserve explicit choices.
                if existing["published"] == 0 and existing["status"] == "baseline":
                    db.execute("UPDATE videos SET status=?,notified=? WHERE id=?",
                               (status, int(status == "baseline"), item["id"]))
                    new_count += int(status == "pending" and show)
                # Never overwrite user status when an upload title changes.
                db.execute("UPDATE videos SET title=?,published=? WHERE id=?", (item["title"], item["published"], item["id"]))
            else:
                db.execute("INSERT INTO videos VALUES (?,?,?,?,?,?,?,?)", (item["id"], channel_id, item["title"], item["published"], now, status, int(status == "baseline"), now))
                new_count += int(status == "pending" and show)
        if complete:
            # Include one starting video for new channels and upgrades from older
            # versions. Wait for a complete snapshot so partial fallback metadata
            # cannot select an older video while the newest is still unresolved.
            seed_key = ("latest_non_short_shown:" if filter_shorts else "latest_video_shown:") + channel_id
            if not meta_get(db, seed_key):
                latest = max((item for item in entries if item["published"] <= now and (visible is None or item["id"] in visible)),
                             key=lambda item: (item["published"], item["id"]), default=None)
                if latest:
                    new_count += db.execute(
                        "UPDATE videos SET status='pending',notified=0,changed=? "
                        "WHERE id=? AND channel_id=? AND status='baseline'",
                        (now, latest["id"], channel_id)).rowcount
                    # Explicit watched/skipped states count as handled as well.
                    meta_set(db, seed_key, "1")
                    meta_set(db, "latest_video_shown:" + channel_id, "1")
            db.execute("UPDATE channels SET name=?,last_check=?,error='',last_cycle=COALESCE(?,last_cycle) WHERE id=?", (name, now, cycle, channel_id))
        else:
            db.execute("UPDATE channels SET name=? WHERE id=?", (name, channel_id))
    return new_count


def cycle_key(daily_time, now=None):
    # Use local wall time on every invocation; systemd uses the same local timezone.
    local = dt.datetime.fromtimestamp(time.time() if now is None else now)
    hour, minute = map(int, daily_time.split(":"))
    day = local.date()
    if (local.hour, local.minute) < (hour, minute):
        day -= dt.timedelta(days=1)
    return day.isoformat()


def run_check(paths, scheduled=False, now=None, fetcher=fetch, resolver=resolve_channel):
    import video_types
    paths.ensure()
    now = time.time() if now is None else now
    preferences = settings(paths)
    filter_shorts = preferences.get("hide_shorts", True)
    cycle = cycle_key(preferences["daily_time"], now) if scheduled else None
    with exclusive_lock(paths.state / "check.lock"):
        urls, errors = parse_channels(paths.channels.read_text(encoding="utf-8"))
        db = connect(paths)
        try:
            # Invalid input cannot accidentally deactivate the existing subscription list.
            if errors:
                with db:
                    meta_set(db, "last_errors", "\n".join(errors))
                return {"new": 0, "checked": 0, "errors": errors, "channels": len(urls)}
            with db:
                db.execute("UPDATE sources SET active=0")
                for url in urls:
                    db.execute("INSERT INTO sources(url,added_at) VALUES (?,?) ON CONFLICT(url) DO UPDATE SET active=1", (url, now))
                db.execute("UPDATE channels SET active=0")
                db.execute("UPDATE channels SET active=1 WHERE id IN (SELECT channel_id FROM sources WHERE active=1)")
            ids = set()
            for url in urls:
                row = db.execute("SELECT * FROM sources WHERE url=?", (url,)).fetchone()
                try:
                    cid = row["channel_id"] or resolver(url)
                    if not CHANNEL_RE.fullmatch(cid):
                        raise InboxError("Invalid resolved channel ID.")
                    with db:
                        db.execute("UPDATE sources SET channel_id=?,error='' WHERE url=?", (cid, url))
                        db.execute("INSERT INTO channels(id,name,added_at) VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET active=1", (cid, cid, row["added_at"]))
                    ids.add(cid)
                except Exception as exc:
                    error = f"{url}: {exc}"
                    errors.append(error)
                    with db:
                        db.execute("UPDATE sources SET error=? WHERE url=?", (str(exc), url))
            added = checked = 0
            for cid in sorted(ids):
                row = db.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
                if scheduled and row["last_cycle"] == cycle and not row["error"]:
                    continue
                try:
                    known = {r["id"]: r for r in db.execute("SELECT id,published FROM videos WHERE channel_id=?", (cid,))}
                    name, entries, upload_errors = read_uploads(cid, known, row["name"], fetcher)
                    if filter_shorts:
                        upload_errors += video_types.classify_entries(paths, cid, entries, force=not scheduled)
                    added += ingest(db, cid, name, entries, now, cycle, complete=not upload_errors, filter_shorts=filter_shorts)
                    if upload_errors:
                        raise InboxError("; ".join(upload_errors)[:800])
                    checked += 1
                except Exception as exc:
                    errors.append(f"{row['name']}: {exc}")
                    with db:
                        db.execute("UPDATE channels SET error=? WHERE id=?", (str(exc), cid))
            with db:
                if checked or errors or not urls:
                    meta_set(db, "last_attempt", now)
                meta_set(db, "last_errors", "\n".join(errors))
                if not errors and (checked or not urls):
                    meta_set(db, "last_success", now)
            return {"new": added, "checked": checked, "errors": errors, "channels": len(ids)}
        finally:
            db.close()


def queue_rows(paths, status="pending"):
    db = connect(paths)
    try:
        return [dict(r) for r in db.execute("SELECT v.*,c.name AS channel FROM videos v JOIN channels c ON c.id=v.channel_id "
                                          "WHERE c.active=1 AND v.status=? AND " + visibility_clause(hide_shorts(paths)) +
                                          " ORDER BY v.published DESC,v.id", (status,))]
    finally:
        db.close()


def channel_rows(paths):
    """Subscribed channels available to browse, including ones awaiting a check."""
    db = connect(paths)
    try:
        return [dict(r) for r in db.execute(
            "SELECT * FROM channels WHERE active=1 ORDER BY name COLLATE NOCASE,id")]
    finally:
        db.close()


def recent_channel_rows(paths, channel_id, now=None):
    """Read a finite recent list without changing inbox, history or check state."""
    now = time.time() if now is None else now
    db = connect(paths)
    try:
        return [dict(r) for r in db.execute(
            "SELECT v.*,c.name AS channel FROM videos v JOIN channels c ON c.id=v.channel_id "
            "WHERE c.active=1 AND c.id=? AND v.published<=? AND " + visibility_clause(hide_shorts(paths)) + " "
            "ORDER BY v.published DESC,v.id LIMIT ?", (channel_id, now, RECENT_UPLOADS))]
    finally:
        db.close()


def set_status(paths, ids, status):
    if status not in ("pending", "watched", "skipped"):
        raise ValueError("Invalid video status")
    db = connect(paths)
    try:
        with db:
            for video_id in ids:
                old = db.execute("SELECT status FROM videos WHERE id=?", (video_id,)).fetchone()
                if not old:
                    archived = db.execute("SELECT * FROM catalogue WHERE id=?", (video_id,)).fetchone()
                    if archived:
                        stamp = time.time()
                        db.execute("INSERT INTO videos VALUES (?,?,?,?,?,?,?,?)",
                                   (video_id, archived["channel_id"], archived["title"], 0,
                                    stamp, "baseline", 1, stamp))
                        old = ("baseline",)
                if old and old[0] != status:
                    stamp = time.time()
                    db.execute("INSERT INTO actions(video_id,old_status,new_status,happened) VALUES (?,?,?,?)", (video_id, old[0], status, stamp))
                    db.execute("UPDATE videos SET status=?,changed=? WHERE id=?", (status, stamp, video_id))
    finally:
        db.close()


def undo(paths):
    db = connect(paths)
    try:
        with db:
            row = db.execute("SELECT * FROM actions WHERE undone=0 ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                return False
            db.execute("UPDATE videos SET status=?,changed=? WHERE id=?", (row["old_status"], time.time(), row["video_id"]))
            db.execute("UPDATE actions SET undone=1 WHERE id=?", (row["id"],))
            return True
    finally:
        db.close()


def video_url(video_id):
    if not VIDEO_RE.fullmatch(video_id):
        raise InboxError("Invalid video ID")
    return "https://www.youtube.com/watch?v=" + video_id


def notify_candidates(db, hide=False):
    return [dict(r) for r in db.execute("SELECT v.id,v.title,c.name AS channel FROM videos v JOIN channels c ON c.id=v.channel_id "
                                      "WHERE c.active=1 AND v.status='pending' AND v.notified=0 AND " + visibility_clause(hide) +
                                      " ORDER BY v.published DESC")]


def socket_name(paths):
    return "yt-inbox-" + hashlib.sha256(str(paths.database).encode()).hexdigest()[:18]
