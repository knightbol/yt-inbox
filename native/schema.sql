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
      CREATE TABLE IF NOT EXISTS sync_records(
        kind TEXT NOT NULL, key TEXT NOT NULL, record TEXT NOT NULL,
        PRIMARY KEY(kind,key));
