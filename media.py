"""On-demand channel catalogue and durable, explicitly requested offline files."""
from pathlib import Path
import shutil
import time

import core

CATALOGUE_BATCH = 100
PAGE_SIZE = 50
MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".3gp", ".flv", ".avi", ".ts"}


def catalogue_state(paths, channel_id):
    db = core.connect(paths)
    try:
        row = db.execute("SELECT * FROM catalogue_state WHERE channel_id=?", (channel_id,)).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def load_catalogue(paths, channel_id, restart=False):
    """Fetch one bounded page. No media downloads or inbox changes are made."""
    if not core.CHANNEL_RE.fullmatch(channel_id):
        raise core.InboxError("Invalid channel ID.")
    with core.exclusive_lock(paths.state / "catalogue.lock"):
        db = core.connect(paths)
        try:
            channel = db.execute("SELECT * FROM channels WHERE id=? AND active=1", (channel_id,)).fetchone()
            if not channel:
                raise core.InboxError("This channel is no longer followed.")
            previous = db.execute("SELECT * FROM catalogue_state WHERE channel_id=?", (channel_id,)).fetchone()
            if previous and previous["exhausted"] and not restart:
                return {"loaded": 0, "exhausted": True}
            start = 1 if restart or not previous else previous["next_start"]
            generation = (previous["generation"] + int(restart)) if previous else 1
            playlist_id = "UU" + channel_id[2:]
            data = core.ytdlp_json("https://www.youtube.com/playlist?list=" + playlist_id,
                                   ["--flat-playlist", "--playlist-items",
                                    f"{start}:{start + CATALOGUE_BATCH - 1}"], timeout=120)
            if data.get("id") != playlist_id or data.get("channel_id") not in (None, channel_id):
                raise core.InboxError("The upload list belongs to a different channel.")
            raw = data.get("entries")
            if not isinstance(raw, list) or len(raw) > CATALOGUE_BATCH:
                raise core.InboxError("YouTube returned an invalid catalogue page.")
            entries = []
            for position, item in enumerate(raw, start):
                if item is None:  # An unavailable playlist slot still consumes a position.
                    continue
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not core.VIDEO_RE.fullmatch(item["id"]):
                    raise core.InboxError("An upload has invalid metadata. Retry this page.")
                if item.get("channel_id") not in (None, channel_id):
                    raise core.InboxError("An upload belongs to a different channel.")
                entries.append((item["id"], channel_id, str(item.get("title") or "Unavailable video"), position, generation))
            # Only an empty page proves the end; unavailable entries can shorten a page.
            exhausted = not raw
            with db:
                if not db.execute("SELECT 1 FROM channels WHERE id=? AND active=1", (channel_id,)).fetchone():
                    raise core.InboxError("This channel was removed during the lookup.")
                if isinstance(data.get("channel"), str) and data["channel"].strip():
                    db.execute("UPDATE channels SET name=? WHERE id=?", (data["channel"], channel_id))
                for entry in entries:
                    owner = db.execute("SELECT channel_id FROM catalogue WHERE id=? UNION SELECT channel_id FROM videos WHERE id=?", (entry[0], entry[0])).fetchall()
                    if any(r[0] != channel_id for r in owner):
                        raise core.InboxError("A saved video belongs to a different channel.")
                    db.execute("INSERT INTO catalogue VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                               "title=excluded.title,position=excluded.position,generation=excluded.generation", entry)
                db.execute("INSERT INTO catalogue_state VALUES (?,?,?,?,?) ON CONFLICT(channel_id) DO UPDATE SET "
                           "next_start=excluded.next_start,exhausted=excluded.exhausted,generation=excluded.generation,updated=excluded.updated",
                           (channel_id, start + CATALOGUE_BATCH, int(exhausted), generation, time.time()))
            return {"loaded": len(entries), "exhausted": exhausted}
        finally:
            db.close()


def browse_page(paths, channel_id, query="", page=0):
    db = core.connect(paths)
    try:
        # Once requested, catalogue ordering comes from YouTube's upload list.
        # Exact dates are shown only for videos already dated by the normal checks.
        if db.execute("SELECT 1 FROM catalogue_state WHERE channel_id=?", (channel_id,)).fetchone():
            rows = db.execute(
                "SELECT a.id,a.channel_id,COALESCE(v.title,a.title) AS title,COALESCE(v.published,0) AS published,"
                "COALESCE(v.status,'baseline') AS status,c.name AS channel FROM catalogue a "
                "JOIN channels c ON c.id=a.channel_id LEFT JOIN videos v ON v.id=a.id "
                "WHERE c.active=1 AND a.channel_id=? ORDER BY a.generation DESC,a.position,a.id", (channel_id,))
        else:
            rows = db.execute("SELECT v.*,c.name AS channel FROM videos v JOIN channels c ON c.id=v.channel_id "
                              "WHERE c.active=1 AND c.id=? AND v.published<=? ORDER BY v.published DESC,v.id",
                              (channel_id, time.time()))
        query = query.casefold().strip()
        matches = [dict(r) for r in rows if not query or query in (r["title"] + " " + r["channel"]).casefold()]
        page = max(0, min(page, max(0, (len(matches) - 1) // PAGE_SIZE)))
        return matches[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], len(matches), page
    finally:
        db.close()


def download_directory(paths, video_id):
    core.video_url(video_id)  # Validate before using the ID in a path.
    return paths.data / "downloads" / video_id


def valid_local_file(paths, video_id, value):
    if not value:
        return None
    path = Path(value)
    folder = download_directory(paths, video_id).resolve()
    try:
        resolved = path.resolve()
        if resolved.parent == folder and resolved.suffix.lower() in MEDIA_EXTENSIONS and resolved.is_file() and resolved.stat().st_size > 0:
            return resolved
    except OSError:
        pass
    return None


def download_records(paths):
    db = core.connect(paths)
    try:
        return {r["id"]: dict(r) for r in db.execute("SELECT * FROM downloads")}
    finally:
        db.close()


def local_file(paths, video_id):
    db = core.connect(paths)
    try:
        row = db.execute("SELECT * FROM downloads WHERE id=? AND state='complete'", (video_id,)).fetchone()
        return valid_local_file(paths, video_id, row["path"]) if row else None
    finally:
        db.close()


def download_rows(paths):
    db = core.connect(paths)
    try:
        return [dict(r) for r in db.execute(
            "SELECT d.id,COALESCE(v.channel_id,a.channel_id) AS channel_id,COALESCE(v.title,a.title,d.id) AS title,"
            "COALESCE(v.published,0) AS published,COALESCE(v.status,'baseline') AS status,"
            "COALESCE(c.name,'Removed channel') AS channel FROM downloads d "
            "LEFT JOIN videos v ON v.id=d.id LEFT JOIN catalogue a ON a.id=d.id "
            "LEFT JOIN channels c ON c.id=COALESCE(v.channel_id,a.channel_id) ORDER BY d.updated DESC,d.id")]
    finally:
        db.close()


def queue_downloads(paths, ids):
    records = download_records(paths)
    db = core.connect(paths)
    added = 0
    try:
        with db:
            for video_id in dict.fromkeys(ids):
                core.video_url(video_id)
                old = records.get(video_id)
                if old and (old["state"] in ("queued", "downloading") or
                            (old["state"] == "complete" and valid_local_file(paths, video_id, old["path"]))):
                    continue
                if not db.execute("SELECT id FROM videos WHERE id=? UNION SELECT id FROM catalogue WHERE id=?", (video_id, video_id)).fetchone():
                    raise core.InboxError("Load the video's channel first.")
                db.execute("INSERT INTO downloads(id,state,updated) VALUES (?,'queued',?) ON CONFLICT(id) "
                           "DO UPDATE SET state='queued',path='',error='',updated=excluded.updated", (video_id, time.time()))
                added += 1
        return added
    finally:
        db.close()


def update_download(paths, video_id, state, path="", error=""):
    db = core.connect(paths)
    try:
        with db:
            db.execute("UPDATE downloads SET state=?,path=?,error=?,updated=? WHERE id=?",
                       (state, str(path), error[-1500:], time.time(), video_id))
    finally:
        db.close()


def recover_downloads(paths):
    db = core.connect(paths)
    try:
        with db:
            db.execute("UPDATE downloads SET state='queued' WHERE state='downloading'")
    finally:
        db.close()


def next_download(paths):
    db = core.connect(paths)
    try:
        row = db.execute("SELECT id FROM downloads WHERE state='queued' ORDER BY updated,id LIMIT 1").fetchone()
        return row[0] if row else None
    finally:
        db.close()


def video_title(paths, video_id):
    db = core.connect(paths)
    try:
        row = db.execute("SELECT title FROM videos WHERE id=? UNION ALL SELECT title FROM catalogue WHERE id=? LIMIT 1",
                         (video_id, video_id)).fetchone()
        return " ".join(row[0].split())[:120] if row else video_id
    finally:
        db.close()


def download_command(paths, video_id):
    missing = [name for name in ("yt-dlp", "ffmpeg", "flock") if not shutil.which(name)]
    if missing:
        raise core.InboxError("Install the missing download tools: " + ", ".join(missing))
    directory = download_directory(paths, video_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    height = core.settings(paths).get("download_height", 720)
    template = str(directory.resolve()).replace("%", "%%") + "/media.%(ext)s"
    return [shutil.which("flock"), "--nonblock", "--conflict-exit-code", "75", str(directory / "download.lock"),
            shutil.which("yt-dlp"), "--ignore-config", "--no-playlist", "--no-simulate",
            "--no-warnings", "--no-colors", "--newline", "--progress", "--progress-delta", "1",
            "--socket-timeout", "20", "--retries", "3", "--fragment-retries", "3",
            "--abort-on-unavailable-fragments", "--continue", "--part", "--no-overwrites",
            "--no-wait-for-video", "--match-filter", "!is_live",
            "--format", f"bv*[height<={height}]+ba/b[height<={height}]", "--merge-output-format", "mkv",
            "--output", template, "--print", "after_move:YT_INBOX_FILE:%(filepath)j",
            "--progress-template", "download:YT_INBOX_PROGRESS:%(progress._percent_str)s",
            "--", core.video_url(video_id)]


def finish_download(paths, video_id, value):
    file = valid_local_file(paths, video_id, value)
    if not file:
        raise core.InboxError("Download finished without a complete video file. Retry the download.")
    update_download(paths, video_id, "complete", path=file)
    return file


def remove_download(paths, video_id):
    db = core.connect(paths)
    try:
        row = db.execute("SELECT state FROM downloads WHERE id=?", (video_id,)).fetchone()
        if not row:
            return
        if row[0] in ("downloading", "queued"):
            raise core.InboxError("Cancel the download before removing its files.")
        directory = download_directory(paths, video_id)
        if directory.is_symlink():
            directory.unlink()
        elif directory.exists():
            shutil.rmtree(directory)
        with db:
            db.execute("DELETE FROM downloads WHERE id=?", (video_id,))
    finally:
        db.close()
