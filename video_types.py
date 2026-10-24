"""Cached YouTube Shorts classification; no title/length guesses or media downloads."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

import core

TYPE_BATCH = 12
TYPE_RETRY = 15 * 60
TYPE_BUDGET = 40


def inspect_type(video_id, channel_id, timeout=20):
    data = core.ytdlp_json(core.video_url(video_id), [
        "--no-playlist", "--ignore-no-formats-error", "--extractor-args",
        "youtube:player_client=web;player_skip=js,configs;skip=hls,dash",
    ], timeout=timeout)
    if data.get("id") != video_id or data.get("channel_id") != channel_id:
        raise core.InboxError("Video type metadata belongs to a different video or channel.")
    kind = core.short_from_metadata(data)
    if kind is None:
        raise core.InboxError("YouTube did not supply the video type. Update yt-dlp and retry Check now.")
    return kind


def resolve_types(paths, rows, stop=None):
    """Use two workers and a bounded batch, retaining each completed result."""
    deadline = time.monotonic() + TYPE_BUDGET

    def lookup(row):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or (stop and stop()):
            return None
        return inspect_type(row["id"], row["channel_id"], timeout=min(20, remaining))

    checked = 0
    db = core.connect(paths)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(lookup, row): row["id"] for row in rows}
            for future in as_completed(futures):
                video_id = futures[future]
                try:
                    kind = future.result()
                    if kind is not None:
                        with db:
                            core.record_video_type(db, video_id, kind)
                        checked += 1
                except Exception as exc:
                    now = time.time()
                    with db:
                        db.execute("INSERT INTO video_types VALUES (?,NULL,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                                   "checked=excluded.checked,retry_after=excluded.retry_after,error=excluded.error "
                                   "WHERE video_types.is_short IS NULL",
                                   (video_id, now, now + TYPE_RETRY, str(exc)[:600]))
        return checked
    finally:
        db.close()


def classify_entries(paths, channel_id, entries, force=False):
    """Classify the current feed before choosing its latest non-Short video."""
    db = core.connect(paths)
    try:
        with db:
            for item in entries:
                core.record_video_type(db, item["id"], item.get("is_short"))
        records = {r["id"]: dict(r) for r in db.execute("SELECT * FROM video_types")}
        pending = [{"id": e["id"], "channel_id": channel_id} for e in entries
                   if records.get(e["id"], {}).get("is_short") is None
                   and (force or records.get(e["id"], {}).get("retry_after", 0) <= time.time())]
        if pending:
            try:
                with core.exclusive_lock(paths.state / "video-types.lock"):
                    resolve_types(paths, pending)
            except core.Busy:
                pass
        records = {r["id"]: dict(r) for r in db.execute("SELECT * FROM video_types")}
        errors = []
        for item in entries:
            record = records.get(item["id"], {})
            if record.get("is_short") is None:
                errors.append(record.get("error") or "Video type checks are pending; the next check will retry.")
            else:
                item["is_short"] = bool(record["is_short"])
        return list(dict.fromkeys(errors))
    finally:
        db.close()


SAVED_VIDEOS = """
  WITH saved AS (
    SELECT v.id,v.channel_id,CASE WHEN v.id IN (SELECT id FROM downloads) THEN 0
      WHEN v.status='pending' THEN 1 ELSE 2 END AS priority,v.published
      FROM videos v JOIN channels c ON c.id=v.channel_id
      WHERE c.active=1 OR v.id IN (SELECT id FROM downloads)
    UNION ALL
    SELECT a.id,a.channel_id,CASE WHEN a.id IN (SELECT id FROM downloads) THEN 0 ELSE 3 END AS priority,0 AS published FROM catalogue a
      JOIN channels c ON c.id=a.channel_id
      WHERE c.active=1 OR a.id IN (SELECT id FROM downloads)
  )
"""


def pending_count(paths, ready_only=False):
    db = core.connect(paths)
    try:
        ready = " AND COALESCE(t.retry_after,0)<=?" if ready_only else ""
        return db.execute(SAVED_VIDEOS + "SELECT count(DISTINCT s.id) FROM saved s LEFT JOIN video_types t ON t.id=s.id "
                          "WHERE t.is_short IS NULL" + ready, (time.time(),) if ready_only else ()).fetchone()[0]
    finally:
        db.close()


def classify_saved(paths, stop=None):
    if not core.hide_shorts(paths):
        return 0
    try:
        with core.exclusive_lock(paths.state / "video-types.lock"):
            db = core.connect(paths)
            try:
                rows = [dict(r) for r in db.execute(SAVED_VIDEOS +
                    "SELECT s.id,s.channel_id FROM saved s LEFT JOIN video_types t ON t.id=s.id "
                    "WHERE t.is_short IS NULL AND COALESCE(t.retry_after,0)<=? GROUP BY s.id "
                    "ORDER BY min(s.priority),max(s.published) DESC,s.id LIMIT ?", (time.time(), TYPE_BATCH))]
            finally:
                db.close()
            return resolve_types(paths, rows, stop) if rows else 0
    except core.Busy:
        return 0
