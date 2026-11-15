/* YouTube input boundary: validate identifiers and ownership before storage.
 * RSS is cheap; yt-dlp supplies archives, video types and a bounded fallback. */
#include "inbox.h"
#include <curl/curl.h>
#include <expat.h>
#include <ctype.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <wordexp.h>

static bool identifier(const char *s, size_t n)
{
    if (strlen(s) != n)
        return false;
    for (; *s; s++)
        if (!((*s >= 'a' && *s <= 'z') || (*s >= 'A' && *s <= 'Z') || (*s >= '0' && *s <= '9') ||
              *s == '_' || *s == '-'))
            return false;
    return true;
}
bool valid_video(const char *s)
{
    return identifier(s, 11);
}
bool valid_channel(const char *s)
{
    return !strncmp(s, "UC", 2) && identifier(s, 24);
}

char *canonical_channel(App *a, const char *input)
{
    char *value = strdup(input);
    char *s = value;
    while (isspace((unsigned char)*s))
        s++;
    size_t n = strlen(s);
    while (n && isspace((unsigned char)s[n - 1]))
        s[--n] = 0;
    if (valid_channel(s)) {
        char *out = strf("https://www.youtube.com/channel/%s", s);
        free(value);
        return out;
    }
    char *url = *s == '@' ? strf("https://www.youtube.com/%s", s)
                : (!strncmp(s, "youtube.com/", 12) || !strncmp(s, "www.youtube.com/", 16) ||
                   !strncmp(s, "m.youtube.com/", 14))
                    ? strf("https://%s", s)
                    : strdup(s);
    free(value);
    CURLU *u = curl_url();
    char *scheme = NULL, *host = NULL, *port = NULL, *user = NULL, *path = NULL, *out = NULL;
    if (curl_url_set(u, CURLUPART_URL, url, 0))
        goto done;
    curl_url_get(u, CURLUPART_SCHEME, &scheme, 0);
    curl_url_get(u, CURLUPART_HOST, &host, 0);
    curl_url_get(u, CURLUPART_PORT, &port, 0);
    curl_url_get(u, CURLUPART_USER, &user, 0);
    curl_url_get(u, CURLUPART_PATH, &path, 0);
    if (!scheme || (strcmp(scheme, "https") && strcmp(scheme, "http")) || !host ||
        (strcasecmp(host, "youtube.com") && strcasecmp(host, "www.youtube.com") &&
         strcasecmp(host, "m.youtube.com")) ||
        user || (port && strcmp(port, "80") && strcmp(port, "443")) || !path)
        goto done;
    char *parts[5] = {0}, *save = NULL, *part;
    int count = 0;
    for (part = strtok_r(path, "/", &save); part && count < 5; part = strtok_r(NULL, "/", &save))
        parts[count++] = part;
    int base = 0;
    if (count && parts[0][0] == '@' && parts[0][1])
        base = 1;
    else if (count >= 2 && (!strcmp(parts[0], "channel") || !strcmp(parts[0], "c") ||
                            !strcmp(parts[0], "user"))) {
        if (!strcmp(parts[0], "channel") && !valid_channel(parts[1]))
            goto done;
        base = 2;
    }
    if (!base || count > base + 1)
        goto done;
    if (count > base && strcmp(parts[base], "videos") && strcmp(parts[base], "shorts") &&
        strcmp(parts[base], "streams") && strcmp(parts[base], "featured") &&
        strcmp(parts[base], "about"))
        goto done;
    out = base == 1 ? strf("https://www.youtube.com/%s", parts[0])
                    : strf("https://www.youtube.com/%s/%s", parts[0], parts[1]);
done:
    curl_free(scheme);
    curl_free(host);
    curl_free(port);
    curl_free(user);
    curl_free(path);
    curl_url_cleanup(u);
    free(url);
    if (!out)
        fail(a, "Use a YouTube channel URL, channel ID or @handle");
    return out;
}

Json *parse_channels(App *a, const char *text)
{
    Json *urls = json_object_new_array();
    char *copy = strdup(text), *save, *line;
    for (line = strtok_r(copy, "\n", &save); line; line = strtok_r(NULL, "\n", &save)) {
        char *comment = strchr(line, '#');
        if (comment)
            *comment = 0;
        while (isspace((unsigned char)*line))
            line++;
        if (!*line)
            continue;
        char *url = canonical_channel(a, line);
        if (!url) {
            json_object_put(urls);
            urls = NULL;
            break;
        }
        if (!jhas(urls, url))
            json_object_array_add(urls, json_object_new_string(url));
        free(url);
    }
    free(copy);
    return urls;
}

int edit_channels(App *a)
{
    int lock = lock_file(a, "channels.lock");
    if (lock < 0)
        return -1;
    const char *editor = getenv("VISUAL");
    if (!editor || !*editor)
        editor = getenv("EDITOR");
    if (!editor || !*editor)
        editor = available("nano") ? "nano" : "vi";
    wordexp_t words = {0};
    int rc = -1;
    /* Parse quoted arguments, but forbid command substitution and shell code. */
    if (wordexp(editor, &words, WRDE_NOCMD) || !words.we_wordc) {
        fail(a, "Invalid EDITOR");
        goto done;
    }
    char **argv = xmalloc((words.we_wordc + 2) * sizeof *argv);
    for (size_t i = 0; i < words.we_wordc; i++)
        argv[i] = words.we_wordv[i];
    char *path = strf("%s/channels.txt", a->config);
    argv[words.we_wordc] = path;
    argv[words.we_wordc + 1] = NULL;
    rc = foreground(a, argv);
    free(argv);
    if (!rc) {
        char *text = read_file(a, path, MAX_DATA);
        Json *urls = text ? parse_channels(a, text) : NULL;
        rc = urls ? 0 : -1;
        json_object_put(urls);
        free(text);
    }
    free(path);
done:
    wordfree(&words);
    close(lock);
    if (!rc)
        sync_kick(a);
    return rc;
}

int metadata_type(Json *j)
{
    const char *type = jstr(j, "media_type");
    if (!strcmp(type, "short"))
        return 1;
    if (!strcmp(type, "video") || !strcmp(type, "livestream"))
        return 0;
    const char *keys[] = {"url", "webpage_url"};
    const char *prefixes[] = {"https://www.youtube.com/shorts/", "https://youtube.com/shorts/",
                              "https://m.youtube.com/shorts/"};
    for (int i = 0; i < 2; i++)
        for (int k = 0; k < 3; k++)
            if (!strncmp(jstr(j, keys[i]), prefixes[k], strlen(prefixes[k])))
                return 1;
    return -1;
}

static const char *const detail_options[] = {
    "--no-playlist", "--ignore-no-formats-error", "--extractor-args",
    "youtube:player_client=web;player_skip=js,configs;skip=hls,dash", NULL};
static bool owner_matches(Json *j, const char *cid)
{
    Json *owner = jget(j, "channel_id");
    return !owner || (json_object_is_type(owner, json_type_string) &&
                      !strcmp(json_object_get_string(owner), cid));
}
static double timestamp(Json *j, const char *key)
{
    Json *value = jget(j, key);
    return json_object_is_type(value, json_type_int) || json_object_is_type(value, json_type_double)
               ? json_object_get_double(value)
               : 0;
}
static Json *details(App *a, const char *id, const char *cid, int timeout_seconds)
{
    char *url = strf("https://www.youtube.com/watch?v=%s", id);
    Json *j = ytdlp(a, url, detail_options, timeout_seconds);
    free(url);
    if (j && (strcmp(jstr(j, "id"), id) || strcmp(jstr(j, "channel_id"), cid))) {
        json_object_put(j);
        j = NULL;
        fail(a, "Metadata belongs to a different video/channel");
    }
    return j;
}

static double iso_time(const char *s)
{
    struct tm t = {0};
    char *end = strptime(s, "%Y-%m-%dT%H:%M:%S", &t);
    if (!end || t.tm_sec > 59)
        return -1;
    if (*end == '.') {
        end++;
        if (!isdigit((unsigned char)*end))
            return -1;
        while (isdigit((unsigned char)*end))
            end++;
    }
    int offset = 0;
    if (!strcmp(end, "Z"))
        offset = 0;
    else {
        int h, m;
        char extra;
        if ((*end != '+' && *end != '-') || strlen(end) != 6 ||
            sscanf(end + 1, "%2d:%2d%c", &h, &m, &extra) != 2 || h > 23 || m > 59)
            return -1;
        offset = (h * 60 + m) * 60 * (*end == '-' ? -1 : 1);
    }
    struct tm before = t;
    time_t stamp = timegm(&t);
    if (before.tm_year != t.tm_year || before.tm_mon != t.tm_mon || before.tm_mday != t.tm_mday ||
        before.tm_hour != t.tm_hour || before.tm_min != t.tm_min)
        return -1;
    return (double)stamp - offset;
}

#define ATOM "http://www.w3.org/2005/Atom|"
#define YT "http://www.youtube.com/xml/schemas/2015|"
typedef struct {
    App *a;
    XML_Parser parser;
    Json *doc, *entry;
    Buffer text;
    const char *cid;
    const char *field;
    int depth, field_depth;
    bool valid;
} Feed;
static void feed_start(void *opaque, const XML_Char *name, const XML_Char **attrs)
{
    (void)attrs;
    Feed *f = opaque;
    f->depth++;
    if (f->depth == 1 && strcmp(name, ATOM "feed")) {
        f->valid = false;
        XML_StopParser(f->parser, XML_FALSE);
        return;
    }
    if (f->depth == 2 && !strcmp(name, ATOM "entry"))
        f->entry = json_object_new_object();
    if ((!f->entry && f->depth == 2) || (f->entry && f->depth == 3)) {
        f->field = !strcmp(name, ATOM "title")       ? "title"
                   : !strcmp(name, ATOM "published") ? "date"
                   : !strcmp(name, YT "videoId")     ? "id"
                   : !strcmp(name, YT "channelId")   ? "owner"
                                                     : NULL;
        if (f->field) {
            f->field_depth = f->depth;
            buffer_free(&f->text);
        }
    }
}
static void feed_text(void *opaque, const XML_Char *s, int len)
{
    Feed *f = opaque;
    if (f->field && f->text.len + (size_t)len < 65536)
        buffer_add(&f->text, s, (size_t)len);
}
static void feed_end(void *opaque, const XML_Char *name)
{
    Feed *f = opaque;
    if (f->field && f->depth == f->field_depth) {
        jsetstr(f->entry ? f->entry : f->doc, f->field, f->text.data ? f->text.data : "");
        f->field = NULL;
    }
    if (f->entry && f->depth == 2 && !strcmp(name, ATOM "entry")) {
        json_object_array_add(jget(f->doc, "entries"), f->entry);
        f->entry = NULL;
    }
    f->depth--;
}
static void reject_doctype(void *opaque, const XML_Char *name, const XML_Char *sys,
                           const XML_Char *pub, int internal)
{
    (void)name;
    (void)sys;
    (void)pub;
    (void)internal;
    Feed *f = opaque;
    f->valid = false;
    XML_StopParser(f->parser, XML_FALSE);
}

Json *parse_feed(App *a, const char *xml, const char *cid)
{
    Feed f = {.a = a, .cid = cid, .valid = true};
    f.doc = json_object_new_object();
    json_object_object_add(f.doc, "entries", json_object_new_array());
    f.parser = XML_ParserCreateNS(NULL, '|');
    if (!f.parser) {
        json_object_put(f.doc);
        fail(a, "Cannot allocate XML parser");
        return NULL;
    }
    XML_SetUserData(f.parser, &f);
    XML_SetElementHandler(f.parser, feed_start, feed_end);
    XML_SetCharacterDataHandler(f.parser, feed_text);
    XML_SetStartDoctypeDeclHandler(f.parser, reject_doctype);
    bool ok = strlen(xml) <= 5000000 &&
              XML_Parse(f.parser, xml, (int)strlen(xml), XML_TRUE) == XML_STATUS_OK && f.valid;
    XML_ParserFree(f.parser);
    buffer_free(&f.text);
    json_object_put(f.entry);
    const char *owner = jstr(f.doc, "owner");
    if (*owner && strcmp(owner, cid) && strcmp(owner, cid + 2))
        ok = false;
    Json *entries = jget(f.doc, "entries");
    for (size_t i = 0; i < json_object_array_length(entries); i++) {
        Json *e = json_object_array_get_idx(entries, i);
        double published = iso_time(jstr(e, "date"));
        const char *who = jstr(e, "owner");
        if (!valid_video(jstr(e, "id")) || published < 0 ||
            (*who && strcmp(who, cid) && strcmp(who, cid + 2)))
            ok = false;
        json_object_object_add(e, "published", json_object_new_double(published));
        if (!*jstr(e, "title"))
            jsetstr(e, "title", "Untitled video");
    }
    if (!ok) {
        json_object_put(f.doc);
        fail(a, "Invalid YouTube Atom feed or video ownership");
        return NULL;
    }
    return f.doc;
}

static double monotonic_now(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}
static int classify_entries(App *a, const char *cid, Json *entries, bool force, double deadline)
{
    if (!hide_shorts(a))
        return 0;
    int lock = -1;
    int pending = 0;
    for (size_t i = 0; i < json_object_array_length(entries) && !interrupted; i++) {
        Json *e = json_object_array_get_idx(entries, i);
        const char *id = jstr(e, "id");
        if (record_type(a, id, metadata_type(e))) {
            if (lock >= 0)
                close(lock);
            return -1;
        }
        Json *rows = db_rows(a, "SELECT * FROM video_types WHERE id=%Q", id);
        if (!rows) {
            if (lock >= 0)
                close(lock);
            return -1;
        }
        Json *known = jfirst(rows);
        bool need = !jget(known, "is_short");
        if (need && monotonic_now() < deadline &&
            (force || json_object_get_double(jget(known, "retry_after")) <= time(NULL))) {
            if (lock < 0)
                lock = lock_file(a, "video-types.lock");
            if (lock < 0) {
                pending++;
                json_object_put(rows);
                continue;
            }
            int seconds = (int)ceil(deadline - monotonic_now());
            if (seconds < 1)
                seconds = 1;
            if (seconds > 20)
                seconds = 20;
            Json *j = details(a, id, cid, seconds);
            int type = j ? metadata_type(j) : -1;
            if (type >= 0) {
                if (record_type(a, id, type)) {
                    json_object_put(j);
                    json_object_put(rows);
                    close(lock);
                    return -1;
                }
                need = false;
            } else
                db_exec(a,
                        "INSERT INTO video_types VALUES(%Q,NULL,%lld,%lld,'Video type unavailable; "
                        "retry later') ON CONFLICT(id) DO UPDATE SET "
                        "checked=excluded.checked,retry_after=excluded.retry_after,error=excluded."
                        "error WHERE video_types.is_short IS NULL",
                        id, (long long)time(NULL), (long long)time(NULL) + 900);
            json_object_put(j);
        }
        if (need)
            pending++;
        json_object_put(rows);
    }
    if (lock >= 0)
        close(lock);
    if (interrupted)
        return fail(a, "Cancelled");
    return pending ? fail(a, "%d video types pending; t checks more, s changes the Shorts filter",
                          pending)
                   : 0;
}

int classify_saved(App *a)
{
    if (!hide_shorts(a))
        return 0;
    Json *rows =
        db_rows(a,
                "SELECT s.id,s.channel_id FROM (SELECT id,channel_id,CASE WHEN status='pending' "
                "THEN 1 ELSE 2 END priority,published FROM videos UNION ALL SELECT "
                "id,channel_id,3,0 FROM catalogue) s "
                "JOIN channels c ON c.id=s.channel_id LEFT JOIN video_types t ON t.id=s.id "
                "WHERE (c.active=1 OR s.id IN(SELECT id FROM downloads)) AND t.is_short IS NULL "
                "AND COALESCE(t.retry_after,0)<=%lld GROUP BY s.id "
                "ORDER BY CASE WHEN s.id IN(SELECT id FROM downloads) THEN 0 ELSE min(s.priority) "
                "END,max(s.published) DESC,s.id LIMIT 12",
                (long long)time(NULL));
    if (!rows)
        return -1;
    int result = 0;
    double deadline = monotonic_now() + 40;
    for (size_t i = 0;
         i < json_object_array_length(rows) && monotonic_now() < deadline && !interrupted; i++) {
        Json *r = json_object_array_get_idx(rows, i), *one = json_object_new_array();
        json_object_array_add(one, json_object_get(r));
        if (classify_entries(a, jstr(r, "channel_id"), one, false, deadline))
            result = -1;
        json_object_put(one);
    }
    json_object_put(rows);
    return result;
}

int ingest(App *a, const char *cid, const char *name, Json *entries, const char *cycle,
           bool complete)
{
    Json *channels = db_rows(a, "SELECT added_at FROM channels WHERE id=%Q", cid);
    if (!channels || !json_object_array_length(channels)) {
        json_object_put(channels);
        return fail(a, "Unknown channel");
    }
    double added = json_object_get_double(jget(jfirst(channels), "added_at"));
    json_object_put(channels);
    if (db_begin(a))
        return -1;
    long long now = (long long)time(NULL);
    int count = 0;
    const char *latest = NULL;
    double latest_time = -1;
    for (size_t i = 0; i < json_object_array_length(entries); i++) {
        Json *e = json_object_array_get_idx(entries, i);
        const char *id = jstr(e, "id");
        double published = json_object_get_double(jget(e, "published"));
        if (!valid_video(id) || !isfinite(published) || published < 0) {
            fail(a, "Invalid upload");
            goto error;
        }
        if (record_type(a, id, metadata_type(e)))
            goto error;
        Json *types = db_rows(a, "SELECT is_short FROM video_types WHERE id=%Q", id);
        if (!types)
            goto error;
        Json *type = jget(jfirst(types), "is_short");
        bool visible = !hide_shorts(a) || (type && json_object_get_int(type) == 0);
        json_object_put(types);
        const char *state = published >= added ? "pending" : "baseline";
        Json *old = db_rows(a, "SELECT status,published,channel_id FROM videos WHERE id=%Q", id);
        if (!old)
            goto error;
        if (json_object_array_length(old)) {
            Json *r = jfirst(old);
            if (strcmp(jstr(r, "channel_id"), cid)) {
                json_object_put(old);
                fail(a, "Saved video has a different owner");
                goto error;
            }
            if (!json_object_get_double(jget(r, "published")) &&
                !strcmp(jstr(r, "status"), "baseline")) {
                if (db_exec(a, "UPDATE videos SET status=%Q,notified=%d WHERE id=%Q", state,
                            strcmp(state, "pending") != 0, id)) {
                    json_object_put(old);
                    goto error;
                }
                count += visible && !strcmp(state, "pending");
            }
            if (db_exec(a, "UPDATE videos SET title=%Q,published=%.17g WHERE id=%Q",
                        jstr(e, "title"), published, id)) {
                json_object_put(old);
                goto error;
            }
        } else {
            if (db_exec(a, "INSERT INTO videos VALUES(%Q,%Q,%Q,%.17g,%lld,%Q,%d,%lld)", id, cid,
                        jstr(e, "title"), published, now, state, strcmp(state, "pending") != 0,
                        now)) {
                json_object_put(old);
                goto error;
            }
            count += visible && !strcmp(state, "pending");
        }
        json_object_put(old);
        if (visible && published <= now &&
            (published > latest_time ||
             (published == latest_time && latest && strcmp(id, latest) > 0))) {
            latest = id;
            latest_time = published;
        }
    }
    if (complete) {
        char *key =
            strf("%s:%s", hide_shorts(a) ? "latest_non_short_shown" : "latest_video_shown", cid);
        char *seed = meta_get(a, key);
        if (!seed) {
            free(key);
            goto error;
        }
        if (!*seed && latest) {
            if (db_exec(a,
                        "UPDATE videos SET status='pending',notified=0,changed=%lld WHERE id=%Q "
                        "AND status='baseline'",
                        now, latest)) {
                free(seed);
                free(key);
                goto error;
            }
            count += sqlite3_changes(a->db);
            if (meta_set(a, key, "1")) {
                free(seed);
                free(key);
                goto error;
            }
            char *other = strf("latest_video_shown:%s", cid);
            int rc = meta_set(a, other, "1");
            free(other);
            if (rc) {
                free(seed);
                free(key);
                goto error;
            }
        }
        free(seed);
        free(key);
        if (db_exec(
                a,
                "UPDATE channels SET "
                "name=%Q,last_check=%lld,error='',last_cycle=COALESCE(%Q,last_cycle) WHERE id=%Q",
                name, now, cycle, cid))
            goto error;
    } else if (db_exec(a, "UPDATE channels SET name=%Q WHERE id=%Q", name, cid))
        goto error;
    if (db_commit(a))
        goto error;
    return count;
error:
    db_rollback(a);
    return -1;
}

static Json *uploads(App *a, const char *cid, bool *complete)
{
    char *url = strf("https://www.youtube.com/feeds/videos.xml?channel_id=%s", cid);
    Buffer b = {0};
    long status = 0;
    int rc = http(a, url, NULL, NULL, 5000000, &b, &status);
    free(url);
    if (rc) {
        buffer_free(&b);
        return NULL;
    }
    if (status == 200) {
        Json *doc = parse_feed(a, b.data, cid);
        buffer_free(&b);
        return doc;
    }
    buffer_free(&b);
    if (status != 404 && (status < 500 || status > 599)) {
        fail(a, "YouTube feed HTTP %ld", status);
        return NULL;
    }
    url = strf("https://www.youtube.com/playlist?list=UU%s", cid + 2);
    const char *opts[] = {"--flat-playlist", "--playlist-end", "15", NULL};
    double deadline = monotonic_now() + 120;
    Json *raw = ytdlp(a, url, opts, 60);
    free(url);
    char *playlist = strf("UU%s", cid + 2);
    if (!raw || strcmp(jstr(raw, "id"), playlist) || !owner_matches(raw, cid) ||
        !json_object_is_type(jget(raw, "entries"), json_type_array)) {
        free(playlist);
        json_object_put(raw);
        fail(a, "Invalid upload-list fallback");
        return NULL;
    }
    free(playlist);
    Json *doc = json_object_new_object(), *entries = json_object_new_array();
    jsetstr(doc, "title", *jstr(raw, "channel") ? jstr(raw, "channel") : cid);
    json_object_object_add(doc, "entries", entries);
    Json *list = jget(raw, "entries");
    for (size_t i = 0; i < json_object_array_length(list) && i < 15 && !interrupted; i++) {
        Json *e = json_object_array_get_idx(list, i);
        const char *id = jstr(e, "id");
        if (!valid_video(id) || !owner_matches(e, cid) || monotonic_now() >= deadline) {
            *complete = false;
            continue;
        }
        Json *known =
            db_rows(a, "SELECT published FROM videos WHERE id=%Q AND channel_id=%Q", id, cid);
        double stamp = json_object_get_double(jget(jfirst(known), "published"));
        json_object_put(known);
        Json *detail = NULL;
        if (stamp <= 0) {
            int seconds = (int)ceil(deadline - monotonic_now());
            if (seconds < 1)
                seconds = 1;
            if (seconds > 60)
                seconds = 60;
            detail = details(a, id, cid, seconds);
            stamp = timestamp(detail, "release_timestamp");
            if (stamp <= 0)
                stamp = timestamp(detail, "timestamp");
        }
        if (isfinite(stamp) && stamp > 0) {
            Json *item = jclone(e);
            json_object_object_add(item, "published", json_object_new_double(stamp));
            if (detail && metadata_type(detail) >= 0)
                jsetstr(item, "media_type", metadata_type(detail) ? "short" : "video");
            json_object_array_add(entries, item);
        } else
            *complete = false;
        json_object_put(detail);
    }
    json_object_put(raw);
    return doc;
}

int check_uploads(App *a, bool scheduled, bool notify)
{
    if (sync_configured(a)) {
        sync_now(a, true);
        a->error[0] = 0;
    }
    int lock = lock_file(a, "check.lock");
    if (lock < 0)
        return -1;
    char *path = strf("%s/channels.txt", a->config), *text = read_file(a, path, MAX_DATA);
    free(path);
    Json *urls = text ? parse_channels(a, text) : NULL;
    free(text);
    int result = -1, added = 0, checked = 0;
    Buffer errors = {0};
    if (!urls)
        goto done;
    if (db_begin(a))
        goto done;
    if (db_exec(a, "UPDATE sources SET active=0")) {
        db_rollback(a);
        goto done;
    }
    for (size_t i = 0; i < json_object_array_length(urls); i++) {
        const char *url = json_object_get_string(json_object_array_get_idx(urls, i));
        if (db_exec(a,
                    "INSERT INTO sources(url,added_at) VALUES(%Q,%lld) ON CONFLICT(url) DO UPDATE "
                    "SET active=1",
                    url, (long long)time(NULL))) {
            db_rollback(a);
            goto done;
        }
    }
    if (db_exec(a, "UPDATE channels SET active=0; UPDATE channels SET active=1 WHERE id IN(SELECT "
                   "channel_id FROM sources WHERE active=1)") ||
        db_commit(a)) {
        db_rollback(a);
        goto done;
    }
    for (size_t i = 0; i < json_object_array_length(urls) && !interrupted; i++) {
        const char *url = json_object_get_string(json_object_array_get_idx(urls, i));
        Json *rows = db_rows(a, "SELECT * FROM sources WHERE url=%Q", url);
        if (!rows)
            goto done;
        Json *r = jfirst(rows);
        char *cid = strdup(jstr(r, "channel_id"));
        if (!*cid) {
            const char *tail = strrchr(url, '/') + 1;
            if (valid_channel(tail)) {
                free(cid);
                cid = strdup(tail);
            } else {
                const char *opts[] = {"--flat-playlist", "--playlist-end", "1", NULL};
                Json *j = ytdlp(a, url, opts, 75);
                if (j) {
                    free(cid);
                    cid = strdup(valid_channel(jstr(j, "channel_id")) ? jstr(j, "channel_id")
                                                                      : jstr(j, "id"));
                }
                json_object_put(j);
            }
        }
        if (valid_channel(cid)) {
            if (db_exec(a,
                        "UPDATE sources SET channel_id=%Q,error='' WHERE url=%Q; INSERT INTO "
                        "channels(id,name,added_at) VALUES(%Q,%Q,%.17g) "
                        "ON CONFLICT(id) DO UPDATE SET active=1",
                        cid, url, cid, cid, json_object_get_double(jget(r, "added_at")))) {
                free(cid);
                json_object_put(rows);
                goto done;
            }
        } else {
            const char *msg =
                "Could not resolve channel; use its /channel/UC... URL or update yt-dlp";
            db_exec(a, "UPDATE sources SET error=%Q WHERE url=%Q", msg, url);
            buffer_add(&errors, msg, strlen(msg));
            buffer_add(&errors, "\n", 1);
        }
        free(cid);
        json_object_put(rows);
    }
    time_t now = time(NULL);
    struct tm local;
    localtime_r(&now, &local);
    int hour, minute;
    sscanf(jstr(a->settings, "daily_time"), "%d:%d", &hour, &minute);
    if (local.tm_hour * 60 + local.tm_min < hour * 60 + minute) {
        local.tm_mday--;
        local.tm_isdst = -1;
        mktime(&local);
    }
    char cycle[32];
    strftime(cycle, sizeof cycle, "%Y-%m-%d", &local);
    Json *channels = db_rows(a, "SELECT * FROM channels WHERE active=1 ORDER BY id");
    if (!channels)
        goto done;
    for (size_t i = 0; i < json_object_array_length(channels) && !interrupted; i++) {
        Json *ch = json_object_array_get_idx(channels, i);
        const char *cid = jstr(ch, "id");
        if (scheduled && !strcmp(jstr(ch, "last_cycle"), cycle) && !*jstr(ch, "error"))
            continue;
        print_plain(jstr(ch, "name"));
        bool complete = true;
        Json *doc = uploads(a, cid, &complete);
        int count = -1;
        if (doc) {
            if (classify_entries(a, cid, jget(doc, "entries"), !scheduled, monotonic_now() + 40))
                complete = false;
            count = ingest(a, cid, *jstr(doc, "title") ? jstr(doc, "title") : jstr(ch, "name"),
                           jget(doc, "entries"), scheduled ? cycle : NULL, complete);
            json_object_put(doc);
        }
        if (count >= 0)
            added += count;
        if (count < 0 || !complete) {
            const char *msg = *a->error ? a->error : "Incomplete upload metadata; retry later";
            buffer_add(&errors, cid, strlen(cid));
            buffer_add(&errors, ": ", 2);
            buffer_add(&errors, msg, strlen(msg));
            buffer_add(&errors, "\n", 1);
            db_exec(a, "UPDATE channels SET error=%Q WHERE id=%Q", msg, cid);
        } else
            checked++;
    }
    json_object_put(channels);
    if (interrupted)
        buffer_add(&errors, "Cancelled\n", 10);
    char stamp[32];
    snprintf(stamp, sizeof stamp, "%lld", (long long)time(NULL));
    if (meta_set(a, "last_attempt", stamp) ||
        meta_set(a, "last_errors", errors.data ? errors.data : ""))
        goto done;
    if (!errors.len && (checked || !json_object_array_length(urls)) &&
        meta_set(a, "last_success", stamp))
        goto done;
    printf("%d new; %d channels checked\n", added, checked);
    result = errors.len ? fail(a, "%s", errors.data) : 0;
done:
    json_object_put(urls);
    buffer_free(&errors);
    close(lock);
    if (!interrupted) {
        sync_kick(a);
        if (notify && notify_digest(a, false))
            result = -1;
    }
    return result;
}

int load_catalogue(App *a, const char *cid, bool restart)
{
    if (!valid_channel(cid))
        return fail(a, "Invalid channel ID");
    int lock = lock_file(a, "catalogue.lock");
    if (lock < 0)
        return -1;
    Json *state = db_rows(a, "SELECT * FROM catalogue_state WHERE channel_id=%Q", cid), *doc = NULL;
    int rc = -1;
    if (!state)
        goto done;
    Json *previous = jfirst(state);
    if (previous && json_object_get_boolean(jget(previous, "exhausted")) && !restart) {
        puts("End of catalogue.");
        rc = 0;
        goto done;
    }
    int start = restart || !previous ? 1 : json_object_get_int(jget(previous, "next_start"));
    int generation = previous ? json_object_get_int(jget(previous, "generation")) + restart : 1;
    char range[64];
    snprintf(range, sizeof range, "%d:%d", start, start + 99);
    char *url = strf("https://www.youtube.com/playlist?list=UU%s", cid + 2);
    const char *opts[] = {"--flat-playlist", "--playlist-items", range, NULL};
    doc = ytdlp(a, url, opts, 120);
    free(url);
    if (!doc)
        goto done;
    Json *entries = jget(doc, "entries");
    char *playlist = strf("UU%s", cid + 2);
    bool valid = !strcmp(jstr(doc, "id"), playlist) && owner_matches(doc, cid) &&
                 json_object_is_type(entries, json_type_array) &&
                 json_object_array_length(entries) <= 100;
    free(playlist);
    if (!valid) {
        fail(a, "Invalid catalogue page");
        goto done;
    }
    if (db_begin(a))
        goto done;
    Json *channel = db_rows(a, "SELECT 1 FROM channels WHERE id=%Q AND active=1", cid);
    bool active = channel && json_object_array_length(channel);
    json_object_put(channel);
    if (!active) {
        fail(a, "Channel is not followed");
        goto rollback;
    }
    for (size_t i = 0; i < json_object_array_length(entries); i++) {
        Json *e = json_object_array_get_idx(entries, i);
        if (!e)
            continue;
        const char *id = jstr(e, "id");
        if (!valid_video(id) || !owner_matches(e, cid)) {
            fail(a, "Invalid catalogue video ownership");
            goto rollback;
        }
        Json *owners = db_rows(a,
                               "SELECT channel_id FROM catalogue WHERE id=%Q UNION SELECT "
                               "channel_id FROM videos WHERE id=%Q",
                               id, id);
        if (!owners)
            goto rollback;
        for (size_t k = 0; k < json_object_array_length(owners); k++)
            if (strcmp(jstr(json_object_array_get_idx(owners, k), "channel_id"), cid)) {
                json_object_put(owners);
                fail(a, "Saved video has a different owner");
                goto rollback;
            }
        json_object_put(owners);
        if (db_exec(
                a,
                "INSERT INTO catalogue VALUES(%Q,%Q,%Q,%d,%d) ON CONFLICT(id) DO UPDATE SET "
                "title=excluded.title,position=excluded.position,generation=excluded.generation",
                id, cid, *jstr(e, "title") ? jstr(e, "title") : "Unavailable video", start + (int)i,
                generation) ||
            record_type(a, id, metadata_type(e)))
            goto rollback;
    }
    if (*jstr(doc, "channel") &&
        db_exec(a, "UPDATE channels SET name=%Q WHERE id=%Q", jstr(doc, "channel"), cid))
        goto rollback;
    if (db_exec(a,
                "INSERT INTO catalogue_state VALUES(%Q,%d,%d,%d,%lld) ON CONFLICT(channel_id) DO "
                "UPDATE SET "
                "next_start=excluded.next_start,exhausted=excluded.exhausted,generation=excluded."
                "generation,updated=excluded.updated",
                cid, start + 100, json_object_array_length(entries) == 0, generation,
                (long long)time(NULL)) ||
        db_commit(a))
        goto rollback;
    printf("Loaded %zu slots. %s\n", json_object_array_length(entries),
           json_object_array_length(entries) ? "n loads older videos; t classifies hidden types."
                                             : "End of catalogue.");
    rc = 0;
    goto done;
rollback:
    db_rollback(a);
done:
    json_object_put(doc);
    json_object_put(state);
    close(lock);
    return rc;
}
