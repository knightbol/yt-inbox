/* Durable local state. SQLite remains the interchange format with the GUI. */
#include "inbox.h"
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

int db_exec(App *a, const char *format, ...)
{
    va_list ap;
    va_start(ap, format);
    char *sql = sqlite3_vmprintf(format, ap);
    va_end(ap);
    if (!sql)
        return fail(a, "Cannot allocate SQL statement");
    char *message = NULL;
    int rc = sqlite3_exec(a->db, sql, NULL, NULL, &message);
    sqlite3_free(sql);
    if (rc != SQLITE_OK)
        fail(a, "Database: %s", message ? message : sqlite3_errmsg(a->db));
    sqlite3_free(message);
    return rc == SQLITE_OK ? 0 : -1;
}

Json *db_rows(App *a, const char *format, ...)
{
    va_list ap;
    va_start(ap, format);
    char *sql = sqlite3_vmprintf(format, ap);
    va_end(ap);
    sqlite3_stmt *statement = NULL;
    int rc = sql ? sqlite3_prepare_v2(a->db, sql, -1, &statement, NULL) : SQLITE_NOMEM;
    sqlite3_free(sql);
    if (rc != SQLITE_OK) {
        fail(a, "Database: %s", sqlite3_errmsg(a->db));
        return NULL;
    }
    Json *rows = json_object_new_array();
    while ((rc = sqlite3_step(statement)) == SQLITE_ROW) {
        Json *row = json_object_new_object();
        for (int i = 0; i < sqlite3_column_count(statement); i++) {
            Json *v = NULL;
            switch (sqlite3_column_type(statement, i)) {
            case SQLITE_INTEGER:
                v = json_object_new_int64(sqlite3_column_int64(statement, i));
                break;
            case SQLITE_FLOAT:
                v = json_object_new_double(sqlite3_column_double(statement, i));
                break;
            case SQLITE_TEXT:
                v = json_object_new_string((const char *)sqlite3_column_text(statement, i));
                break;
            default:
                break;
            }
            json_object_object_add(row, sqlite3_column_name(statement, i), v);
        }
        json_object_array_add(rows, row);
    }
    sqlite3_finalize(statement);
    if (rc != SQLITE_DONE) {
        json_object_put(rows);
        fail(a, "Database: %s", sqlite3_errmsg(a->db));
        return NULL;
    }
    return rows;
}

char *meta_get(App *a, const char *key)
{
    Json *rows = db_rows(a, "SELECT value FROM meta WHERE key=%Q", key);
    if (!rows)
        return NULL;
    char *s = strdup(rows && json_object_array_length(rows) ? jstr(jfirst(rows), "value") : "");
    json_object_put(rows);
    return s;
}
int meta_set(App *a, const char *key, const char *value)
{
    return db_exec(
        a, "INSERT INTO meta VALUES(%Q,%Q) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        key, value);
}
int db_begin(App *a)
{
    return db_exec(a, "BEGIN IMMEDIATE");
}
int db_commit(App *a)
{
    return db_exec(a, "COMMIT");
}
void db_rollback(App *a)
{
    sqlite3_exec(a->db, "ROLLBACK", NULL, NULL, NULL);
}

int db_backup(App *a, const char *path)
{
    if (!access(path, F_OK)) {
        sqlite3 *existing = NULL;
        sqlite3_stmt *st = NULL;
        int ok = sqlite3_open_v2(path, &existing, SQLITE_OPEN_READONLY, NULL) == SQLITE_OK &&
                 sqlite3_prepare_v2(existing, "PRAGMA quick_check", -1, &st, NULL) == SQLITE_OK &&
                 sqlite3_step(st) == SQLITE_ROW &&
                 !strcmp((const char *)sqlite3_column_text(st, 0), "ok");
        sqlite3_finalize(st);
        st = NULL;
        if (ok)
            ok = sqlite3_prepare_v2(existing,
                                    "SELECT count(*) FROM sqlite_master WHERE type='table' AND "
                                    "name IN('videos','channels','meta')",
                                    -1, &st, NULL) == SQLITE_OK &&
                 sqlite3_step(st) == SQLITE_ROW && sqlite3_column_int(st, 0) == 3;
        sqlite3_finalize(st);
        sqlite3_close(existing);
        return ok ? 0 : fail(a, "Existing backup is invalid; move it aside before retrying");
    }
    char *temp = strf("%s.tmp-XXXXXX", path);
    int fd = mkostemp(temp, O_CLOEXEC);
    if (fd < 0) {
        free(temp);
        return fail(a, "Cannot create backup");
    }
    close(fd);
    sqlite3 *dest = NULL;
    int rc = sqlite3_open(temp, &dest);
    if (rc == SQLITE_OK) {
        sqlite3_backup *b = sqlite3_backup_init(dest, "main", a->db, "main");
        if (b) {
            rc = sqlite3_backup_step(b, -1);
            int finished = sqlite3_backup_finish(b);
            if (rc == SQLITE_DONE)
                rc = finished;
        } else
            rc = sqlite3_errcode(dest);
    }
    sqlite3_close(dest);
    if (rc == SQLITE_OK) {
        fd = open(temp, O_RDONLY | O_CLOEXEC);
        if (fd < 0 || fsync(fd) || link(temp, path))
            rc = SQLITE_IOERR;
        if (fd >= 0)
            close(fd);
        char *parent = strdup(path);
        *strrchr(parent, '/') = 0;
        fd = open(parent, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
        free(parent);
        if (fd < 0 || fsync(fd))
            rc = SQLITE_IOERR;
        if (fd >= 0)
            close(fd);
    }
    unlink(temp);
    free(temp);
    return rc == SQLITE_OK ? 0 : fail(a, "Could not finish consistent SQLite backup");
}

Json *list_rows(App *a, const char *view, const char *channel)
{
    const char *visible =
        hide_shorts(a) ? "EXISTS (SELECT 1 FROM video_types t WHERE t.id=v.id AND t.is_short=0)"
                       : "1";
    if (!strcmp(view, "channels"))
        return db_rows(a, "SELECT * FROM channels WHERE active=1 ORDER BY name COLLATE NOCASE,id");
    if (!strcmp(view, "downloads")) {
        const char *filter =
            hide_shorts(a) ? "EXISTS(SELECT 1 FROM video_types t WHERE t.id=d.id AND t.is_short=0)"
                           : "1";
        return db_rows(
            a,
            "SELECT d.id,COALESCE(v.channel_id,x.channel_id) "
            "channel_id,COALESCE(v.title,x.title,d.id) title,"
            "COALESCE(v.published,0) published,COALESCE(v.status,'baseline') "
            "status,COALESCE(c.name,'Removed channel') channel,d.state download_state "
            "FROM downloads d LEFT JOIN videos v ON v.id=d.id LEFT JOIN catalogue x ON x.id=d.id "
            "LEFT JOIN channels c ON c.id=COALESCE(v.channel_id,x.channel_id) WHERE %s ORDER BY "
            "d.updated DESC,d.id",
            filter);
    }
    if (channel) {
        Json *state = db_rows(a, "SELECT 1 FROM catalogue_state WHERE channel_id=%Q", channel);
        if (!state)
            return NULL;
        bool cached = json_object_array_length(state) > 0;
        json_object_put(state);
        if (cached) {
            const char *filter =
                hide_shorts(a)
                    ? "EXISTS(SELECT 1 FROM video_types t WHERE t.id=x.id AND t.is_short=0)"
                    : "1";
            return db_rows(a,
                           "SELECT x.id,x.channel_id,COALESCE(v.title,x.title) "
                           "title,COALESCE(v.published,0) published,"
                           "COALESCE(v.status,'baseline') status,c.name channel,d.state "
                           "download_state FROM catalogue x JOIN channels c ON c.id=x.channel_id "
                           "LEFT JOIN videos v ON v.id=x.id LEFT JOIN downloads d ON d.id=x.id "
                           "WHERE c.active=1 AND x.channel_id=%Q AND %s "
                           "ORDER BY x.generation DESC,x.position,x.id",
                           channel, filter);
        }
        return db_rows(a,
                       "SELECT v.*,c.name channel,d.state download_state FROM videos v JOIN "
                       "channels c ON c.id=v.channel_id "
                       "LEFT JOIN downloads d ON d.id=v.id WHERE c.active=1 AND v.channel_id=%Q "
                       "AND v.published<=%lld AND %s ORDER BY v.published DESC,v.id",
                       channel, (long long)time(NULL), visible);
    }
    return db_rows(a,
                   "SELECT v.*,c.name channel,d.state download_state FROM videos v JOIN channels c "
                   "ON c.id=v.channel_id "
                   "LEFT JOIN downloads d ON d.id=v.id WHERE c.active=1 AND v.status=%Q AND %s "
                   "ORDER BY v.published DESC,v.id",
                   view, visible);
}

int set_status(App *a, int count, char **ids, const char *status)
{
    if (strcmp(status, "pending") && strcmp(status, "watched") && strcmp(status, "skipped"))
        return fail(a, "Invalid status");
    if (db_begin(a))
        return -1;
    for (int i = 0; i < count; i++) {
        if (!valid_video(ids[i])) {
            fail(a, "Invalid video ID");
            goto error;
        }
        long long now = (long long)time(NULL);
        if (db_exec(a,
                    "INSERT OR IGNORE INTO videos SELECT "
                    "id,channel_id,title,0,%lld,'baseline',1,%lld FROM catalogue WHERE id=%Q",
                    now, now, ids[i]))
            goto error;
        Json *rows = db_rows(a, "SELECT status FROM videos WHERE id=%Q", ids[i]);
        if (!rows)
            goto error;
        if (!json_object_array_length(rows)) {
            json_object_put(rows);
            fail(a, "Unknown video. Load its channel first.");
            goto error;
        }
        const char *old = jstr(jfirst(rows), "status");
        if (strcmp(old, status)) {
            int rc = db_exec(a,
                             "INSERT INTO actions(video_id,old_status,new_status,happened) "
                             "VALUES(%Q,%Q,%Q,%lld);"
                             "UPDATE videos SET status=%Q,changed=%lld WHERE id=%Q",
                             ids[i], old, status, now, status, now, ids[i]);
            if (!rc)
                rc = sync_record_status(a, ids[i]);
            if (rc) {
                json_object_put(rows);
                goto error;
            }
        }
        json_object_put(rows);
    }
    if (db_commit(a))
        goto error;
    sync_kick(a);
    return 0;
error:
    db_rollback(a);
    return -1;
}

int undo_status(App *a)
{
    if (db_begin(a))
        return -1;
    Json *rows = db_rows(a, "SELECT * FROM actions WHERE undone=0 ORDER BY id DESC LIMIT 1");
    if (!rows) {
        db_rollback(a);
        return -1;
    }
    int rc = 0;
    if (json_object_array_length(rows)) {
        Json *r = jfirst(rows);
        rc = db_exec(a,
                     "UPDATE videos SET status=%Q,changed=%lld WHERE id=%Q; UPDATE actions SET "
                     "undone=1 WHERE id=%lld",
                     jstr(r, "old_status"), (long long)time(NULL), jstr(r, "video_id"),
                     (long long)json_object_get_int64(jget(r, "id")));
        if (!rc)
            rc = sync_record_status(a, jstr(r, "video_id"));
    }
    bool changed = json_object_array_length(rows) > 0;
    json_object_put(rows);
    if (!rc)
        rc = db_commit(a);
    if (rc)
        db_rollback(a);
    else {
        puts(changed ? "Previous status restored." : "Nothing to undo.");
        sync_kick(a);
    }
    return rc;
}

int record_type(App *a, const char *id, int type)
{
    if (type != 0 && type != 1)
        return 0;
    return db_exec(a,
                   "INSERT INTO video_types(id,is_short,checked) VALUES(%Q,%d,%lld) ON "
                   "CONFLICT(id) DO UPDATE SET "
                   "is_short=excluded.is_short,checked=excluded.checked,retry_after=0,error=''",
                   id, type, (long long)time(NULL));
}
