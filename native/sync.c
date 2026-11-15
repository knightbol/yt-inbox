/* Per-record logical clocks, compatible with syncing.py.
 * Never hold a SQLite transaction while waiting for the network.
 * Commit merged records before projecting channels.txt; retries are idempotent. */
#include "inbox.h"
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/random.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

static Json *empty_document(void)
{
    Json *j = json_object_new_object();
    jsetstr(j, "format", "yt-inbox-sync");
    json_object_object_add(j, "version", json_object_new_int(1));
    json_object_object_add(j, "channels", json_object_new_object());
    json_object_object_add(j, "videos", json_object_new_object());
    return j;
}
static bool hex_id(const char *s, size_t n)
{
    if (strlen(s) != n)
        return false;
    for (; *s; s++)
        if (!(*s >= '0' && *s <= '9') && !(*s >= 'a' && *s <= 'f'))
            return false;
    return true;
}
static size_t characters(const char *s)
{
    size_t n = 0;
    for (; *s; s++)
        if (((unsigned char)*s & 0xc0) != 0x80)
            n++;
    return n;
}
static bool text_valid(Json *j)
{
    if (!json_object_is_type(j, json_type_string))
        return false;
    const char *s = json_object_get_string(j);
    if (strlen(s) != (size_t)json_object_get_string_len(j) || characters(s) > 4096)
        return false;
    for (; *s; s++)
        if ((unsigned char)*s < 32)
            return false;
    return true;
}
static char *metadata_clean(const char *s)
{
    Buffer b = {0};
    size_t count = 0;
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c < 32)
            continue;
        if ((c & 0xc0) != 0x80 && ++count > 4096)
            break;
        buffer_add(&b, s, 1);
    }
    if (!b.data)
        buffer_add(&b, "", 0);
    return b.data;
}
static int state_rank(const char *state)
{
    const char *values[] = {"baseline", "pending", "skipped", "watched"};
    for (int i = 0; i < 4; i++)
        if (!strcmp(state, values[i]))
            return i;
    return -1;
}

int sync_validate(App *a, Json *doc)
{
    if (strcmp(jstr(doc, "format"), "yt-inbox-sync") ||
        !json_object_is_type(jget(doc, "version"), json_type_int) ||
        json_object_get_int64(jget(doc, "version")) != 1)
        return fail(a, "Unsupported sync format");
    const char *kinds[] = {"channels", "videos"};
    for (int i = 0; i < 2; i++) {
        Json *records = jget(doc, kinds[i]);
        if (!json_object_is_type(records, json_type_object) ||
            json_object_object_length(records) > 100000)
            return fail(a, "Invalid sync record collection");
        json_object_object_foreach(records, key, record)
        {
            Json *rev = jget(record, "rev");
            if (!json_object_is_type(rev, json_type_array) || json_object_array_length(rev) != 2)
                return fail(a, "Invalid sync revision");
            Json *counter = json_object_array_get_idx(rev, 0),
                 *device = json_object_array_get_idx(rev, 1);
            if (!json_object_is_type(counter, json_type_int) ||
                json_object_get_int64(counter) < 0 ||
                json_object_get_int64(counter) >= INT64_C(9007199254740992) ||
                !json_object_is_type(device, json_type_string) ||
                !hex_id(json_object_get_string(device), 32))
                return fail(a, "Invalid sync clock/device");
            const char *cid = jstr(record, "channel_id");
            if (!json_object_is_type(jget(record, "channel_id"), json_type_string) ||
                (!valid_channel(cid) && (i || *cid)))
                return fail(a, "Invalid sync channel ID");
            if (!text_valid(jget(record, "name")))
                return fail(a, "Invalid sync channel name");
            if (!i) {
                char *url = strlen(key) <= 2048 ? canonical_channel(a, key) : NULL;
                bool valid = url && !strcmp(url, key) &&
                             json_object_is_type(jget(record, "active"), json_type_boolean);
                free(url);
                if (!valid)
                    return fail(a, "Invalid sync subscription");
            } else {
                Json *stamp = jget(record, "published"), *type = jget(record, "short");
                double date = json_object_get_double(stamp);
                if (!valid_video(key) || state_rank(jstr(record, "status")) < 0 ||
                    !text_valid(jget(record, "title")) ||
                    (!json_object_is_type(stamp, json_type_int) &&
                     !json_object_is_type(stamp, json_type_double)) ||
                    !isfinite(date) || date < 0 || date > 253402214400.0 ||
                    (type && !json_object_is_type(type, json_type_boolean)))
                    return fail(a, "Invalid synced video");
            }
        }
    }
    return 0;
}

static int key_compare(const void *left, const void *right)
{
    return strcmp(*(const char *const *)left, *(const char *const *)right);
}
static Json *sorted_copy(Json *j)
{
    if (json_object_is_type(j, json_type_object)) {
        size_t n = (size_t)json_object_object_length(j), i = 0;
        const char **keys = xmalloc(n * sizeof *keys);
        json_object_object_foreach(j, key, value)
        {
            (void)value;
            keys[i++] = key;
        }
        qsort(keys, n, sizeof *keys, key_compare);
        Json *out = json_object_new_object();
        for (i = 0; i < n; i++)
            json_object_object_add(out, keys[i], sorted_copy(jget(j, keys[i])));
        free(keys);
        return out;
    }
    if (json_object_is_type(j, json_type_array)) {
        Json *out = json_object_new_array();
        for (size_t i = 0; i < json_object_array_length(j); i++)
            json_object_array_add(out, sorted_copy(json_object_array_get_idx(j, i)));
        return out;
    }
    return jclone(j);
}
/* The v1 protocol's final tie-break is Python json.dumps(sort_keys=True).
 * json-c uses 17-digit floats; Python uses shortest round-tripping decimals.
 * Find that precision, then apply Python's fixed/scientific thresholds. */
static void canonical_json(Buffer *out, Json *j)
{
    if (json_object_is_type(j, json_type_double)) {
        double value = json_object_get_double(j);
        char candidate[128];
        int digits;
        for (digits = 1; digits < 17; digits++) {
            snprintf(candidate, sizeof candidate, "%.*g", digits, value);
            if (strtod(candidate, NULL) == value)
                break;
        }
        snprintf(candidate, sizeof candidate, "%.*e", digits - 1, value);
        char *e = strchr(candidate, 'e');
        int exponent = atoi(e + 1);
        if (exponent >= -4 && exponent < 16) {
            int places = digits - 1 - exponent;
            if (places < 0)
                places = 0;
            snprintf(candidate, sizeof candidate, "%.*f", places, value);
            if (!strchr(candidate, '.'))
                strcat(candidate, ".0");
        } else {
            char *dot = strchr(candidate, '.');
            if (dot) {
                char *end = e - 1;
                while (end > dot && *end == '0')
                    end--;
                if (end == dot)
                    end--;
                memmove(end + 1, e, strlen(e) + 1);
            }
        }
        buffer_add(out, candidate, strlen(candidate));
        return;
    }
    if (json_object_is_type(j, json_type_array)) {
        buffer_add(out, "[", 1);
        for (size_t i = 0; i < json_object_array_length(j); i++) {
            if (i)
                buffer_add(out, ",", 1);
            canonical_json(out, json_object_array_get_idx(j, i));
        }
        buffer_add(out, "]", 1);
        return;
    }
    if (json_object_is_type(j, json_type_object)) {
        Json *sorted = sorted_copy(j);
        buffer_add(out, "{", 1);
        bool first = true;
        json_object_object_foreach(sorted, key, value)
        {
            if (!first)
                buffer_add(out, ",", 1);
            first = false;
            Json *name = json_object_new_string(key);
            const char *s = jtext(name);
            buffer_add(out, s, strlen(s));
            json_object_put(name);
            buffer_add(out, ":", 1);
            canonical_json(out, value);
        }
        buffer_add(out, "}", 1);
        json_object_put(sorted);
        return;
    }
    const char *s = jtext(j);
    buffer_add(out, s, strlen(s));
}
static int64_t clock_of(Json *r)
{
    return json_object_get_int64(json_object_array_get_idx(jget(r, "rev"), 0));
}
static const char *device_of(Json *r)
{
    return json_object_get_string(json_object_array_get_idx(jget(r, "rev"), 1));
}
static Json *winner(Json *a, Json *b)
{
    int64_t ca = clock_of(a), cb = clock_of(b);
    int order = ca > cb ? 1 : ca < cb ? -1 : 0;
    if (!order && !ca && jget(a, "status"))
        order = state_rank(jstr(a, "status")) - state_rank(jstr(b, "status"));
    if (!order)
        order = strcmp(device_of(a), device_of(b));
    if (!order) {
        Buffer sa = {0}, sb = {0};
        canonical_json(&sa, a);
        canonical_json(&sb, b);
        order = strcmp(sa.data, sb.data);
        buffer_free(&sa);
        buffer_free(&sb);
    }
    Json *chosen = jclone(order >= 0 ? a : b);
    const char *fields[] = {"channel_id", "name", "title"};
    for (int i = 0; i < 3; i++)
        if (jget(a, fields[i])) {
            const char *x = jstr(a, fields[i]), *y = jstr(b, fields[i]);
            size_t nx = characters(x), ny = characters(y);
            jsetstr(chosen, fields[i], nx > ny || (nx == ny && strcmp(x, y) >= 0) ? x : y);
        }
    if (jget(a, "published")) {
        Json *pa = jget(a, "published"), *pb = jget(b, "published");
        json_object_object_add(
            chosen, "published",
            jclone(json_object_get_double(pa) >= json_object_get_double(pb) ? pa : pb));
        Json *ta = jget(a, "short"), *tb = jget(b, "short");
        json_object_object_add(chosen, "short",
                               ta || tb ? json_object_new_boolean(json_object_get_boolean(ta) ||
                                                                  json_object_get_boolean(tb))
                                        : NULL);
    }
    return chosen;
}

Json *sync_merge(Json *left, Json *right)
{
    Json *result = empty_document();
    const char *kinds[] = {"channels", "videos"};
    for (int i = 0; i < 2; i++) {
        Json *out = jget(result, kinds[i]), *l = jget(left, kinds[i]), *r = jget(right, kinds[i]);
        json_object_object_foreach(l, key, value)
        {
            json_object_object_add(out, key, jclone(value));
        }
        json_object_object_foreach(r, key2, value2)
        {
            Json *old = jget(out, key2);
            json_object_object_add(out, key2, old ? winner(old, value2) : jclone(value2));
        }
    }
    return result;
}

static Json *document(App *a)
{
    Json *rows = db_rows(a, "SELECT kind,key,record FROM sync_records");
    if (!rows)
        return NULL;
    Json *doc = empty_document();
    for (size_t i = 0; i < json_object_array_length(rows); i++) {
        Json *r = json_object_array_get_idx(rows, i), *record = json_parse(a, jstr(r, "record")),
             *collection = jget(doc, jstr(r, "kind"));
        if (!record || !collection) {
            if (!collection)
                fail(a, "Unknown local sync record kind");
            json_object_put(record);
            json_object_put(rows);
            json_object_put(doc);
            return NULL;
        }
        json_object_object_add(collection, jstr(r, "key"), record);
    }
    json_object_put(rows);
    if (sync_validate(a, doc)) {
        json_object_put(doc);
        return NULL;
    }
    return doc;
}
static int put(App *a, const char *kind, const char *key, Json *record)
{
    return db_exec(a,
                   "INSERT INTO sync_records VALUES(%Q,%Q,%Q) ON CONFLICT(kind,key) DO UPDATE SET "
                   "record=excluded.record",
                   kind, key, jtext(record));
}

static Json *revision(App *a, bool seed)
{
    char *clock = meta_get(a, "sync_clock"), *device = meta_get(a, "sync_device");
    if (!clock || !device) {
        free(clock);
        free(device);
        return NULL;
    }
    char *end;
    errno = 0;
    int64_t n = strtoll(*clock ? clock : "0", &end, 10);
    bool valid = !errno && !*end && n >= 0 && n < INT64_C(9007199254740991) && hex_id(device, 32);
    free(clock);
    if (!valid) {
        free(device);
        fail(a, "Invalid local sync clock/device");
        return NULL;
    }
    if (!seed) {
        char value[32];
        snprintf(value, sizeof value, "%lld", (long long)++n);
        if (meta_set(a, "sync_clock", value)) {
            free(device);
            return NULL;
        }
    }
    Json *r = json_object_new_array();
    json_object_array_add(r, json_object_new_int64(seed ? 0 : n));
    json_object_array_add(r, json_object_new_string(device));
    free(device);
    return r;
}

static Json *video_record(App *a, const char *id, Json *rev)
{
    Json *rows = db_rows(a,
                         "SELECT v.*,c.name,t.is_short FROM videos v JOIN channels c ON "
                         "c.id=v.channel_id LEFT JOIN video_types t ON t.id=v.id WHERE v.id=%Q",
                         id);
    if (!rows || !jfirst(rows)) {
        json_object_put(rows);
        return NULL;
    }
    Json *r = jfirst(rows), *record = json_object_new_object();
    json_object_object_add(record, "rev", jclone(rev));
    jsetstr(record, "status", jstr(r, "status"));
    jsetstr(record, "channel_id", jstr(r, "channel_id"));
    char *title = metadata_clean(jstr(r, "title")),
         *name =
             metadata_clean(strcmp(jstr(r, "name"), jstr(r, "channel_id")) ? jstr(r, "name") : "");
    jsetstr(record, "title", title);
    jsetstr(record, "name", name);
    free(title);
    free(name);
    json_object_object_add(
        record, "published",
        json_object_new_double(fmax(0, json_object_get_double(jget(r, "published")))));
    Json *type = jget(r, "is_short");
    json_object_object_add(record, "short",
                           type ? json_object_new_boolean(json_object_get_boolean(type)) : NULL);
    json_object_put(rows);
    return record;
}

int sync_record_status(App *a, const char *id)
{
    char *device = meta_get(a, "sync_device");
    if (!device)
        return -1;
    bool configured = *device != 0;
    free(device);
    if (!configured)
        return 0;
    Json *rev = revision(a, false), *record = rev ? video_record(a, id, rev) : NULL;
    int rc = record ? put(a, "videos", id, record) : -1;
    json_object_put(record);
    json_object_put(rev);
    return rc;
}

static char *capture(App *a)
{
    char *path = strf("%s/channels.txt", a->config), *text = read_file(a, path, MAX_DATA);
    free(path);
    Json *urls = text ? parse_channels(a, text) : NULL, *previous = NULL, *doc = NULL, *rows = NULL;
    char *before = NULL, *device = NULL;
    bool ok = false;
    if (!urls)
        goto done;
    device = meta_get(a, "sync_device");
    if (!device)
        goto done;
    if (!*device) {
        unsigned char bytes[16];
        size_t have = 0;
        while (have < sizeof bytes) {
            ssize_t n = getrandom(bytes + have, sizeof bytes - have, 0);
            if (n < 0 && errno == EINTR)
                continue;
            if (n <= 0) {
                fail(a, "Cannot generate device ID");
                goto done;
            }
            have += (size_t)n;
        }
        char hex[33];
        for (int i = 0; i < 16; i++)
            snprintf(hex + i * 2, 3, "%02x", bytes[i]);
        if (meta_set(a, "sync_device", hex))
            goto done;
    }
    before = meta_get(a, "sync_seen_channels");
    if (!before)
        goto done;
    previous = *before ? json_parse(a, before) : json_object_new_array();
    if (!json_object_is_type(previous, json_type_array)) {
        fail(a, "Invalid local channel snapshot");
        goto done;
    }
    for (size_t i = 0; i < json_object_array_length(previous); i++)
        if (!json_object_is_type(json_object_array_get_idx(previous, i), json_type_string)) {
            fail(a, "Invalid local channel snapshot");
            goto done;
        }
    doc = document(a);
    if (!doc)
        goto done;
    Json *all = jclone(urls);
    for (size_t i = 0; i < json_object_array_length(previous); i++) {
        const char *url = json_object_get_string(json_object_array_get_idx(previous, i));
        if (!jhas(all, url))
            json_object_array_add(all, json_object_new_string(url));
    }
    for (size_t i = 0; i < json_object_array_length(all); i++) {
        const char *url = json_object_get_string(json_object_array_get_idx(all, i));
        Json *old = jget(jget(doc, "channels"), url);
        bool active = jhas(urls, url), changed = active != jhas(previous, url);
        rows = db_rows(a,
                       "SELECT s.channel_id,c.name FROM sources s LEFT JOIN channels c ON "
                       "c.id=s.channel_id WHERE s.url=%Q",
                       url);
        if (!rows) {
            json_object_put(all);
            goto done;
        }
        const char *cid = jstr(jfirst(rows), "channel_id");
        if (!*cid && !strncmp(url, "https://www.youtube.com/channel/", 32))
            cid = strrchr(url, '/') + 1;
        char *name = metadata_clean(
            strcmp(jstr(jfirst(rows), "name"), cid) ? jstr(jfirst(rows), "name") : "");
        Json *record = NULL;
        if (!old || (changed && json_object_get_boolean(jget(old, "active")) != active)) {
            Json *rev = revision(a, !*before);
            if (rev) {
                record = json_object_new_object();
                json_object_object_add(record, "rev", rev);
                json_object_object_add(record, "active", json_object_new_boolean(active));
                jsetstr(record, "channel_id", cid);
                jsetstr(record, "name", name);
            }
        } else {
            Json *current = jclone(old);
            jsetstr(current, "channel_id", cid);
            jsetstr(current, "name", name);
            record = winner(old, current);
            json_object_put(current);
        }
        free(name);
        json_object_put(rows);
        rows = NULL;
        int rc = record ? put(a, "channels", url, record) : -1;
        json_object_put(record);
        if (rc) {
            json_object_put(all);
            goto done;
        }
    }
    json_object_put(all);
    if (meta_set(a, "sync_seen_channels", jtext(urls)))
        goto done;
    rows = db_rows(a, "SELECT id FROM videos WHERE status!='baseline'");
    if (!rows)
        goto done;
    for (size_t i = 0; i < json_object_array_length(rows); i++) {
        const char *id = jstr(json_object_array_get_idx(rows, i), "id");
        Json *old = jget(jget(doc, "videos"), id);
        Json *rev = old ? jclone(jget(old, "rev")) : revision(a, true),
             *current = rev ? video_record(a, id, rev) : NULL;
        json_object_put(rev);
        if (!current)
            goto done;
        if (old) {
            jsetstr(current, "status", jstr(old, "status"));
            Json *merged = winner(old, current);
            json_object_put(current);
            current = merged;
        }
        int rc = put(a, "videos", id, current);
        json_object_put(current);
        if (rc)
            goto done;
    }
    ok = true;
done:
    json_object_put(urls);
    json_object_put(previous);
    json_object_put(doc);
    json_object_put(rows);
    free(before);
    free(device);
    if (!ok) {
        free(text);
        return NULL;
    }
    return text;
}

Json *sync_snapshot(App *a)
{
    int lock = lock_file(a, "channels.lock");
    if (lock < 0)
        return NULL;
    Json *doc = NULL;
    if (!db_begin(a)) {
        char *text = capture(a);
        if (text)
            doc = document(a);
        free(text);
        if (!doc || db_commit(a)) {
            json_object_put(doc);
            doc = NULL;
            db_rollback(a);
        }
    }
    close(lock);
    return doc;
}

static char *render_channels(App *a, const char *text, Json *records)
{
    Buffer out = {0};
    Json *kept = json_object_new_array();
    char *copy = strdup(text), *line = copy;
    while (*line) {
        char *end = strchr(line, '\n');
        if (end)
            *end = 0;
        Json *values = parse_channels(a, line);
        if (!values) {
            buffer_free(&out);
            free(copy);
            json_object_put(kept);
            return NULL;
        }
        const char *url = jfirst(values) ? json_object_get_string(jfirst(values)) : NULL;
        if (!url ||
            (json_object_get_boolean(jget(jget(records, url), "active")) && !jhas(kept, url))) {
            buffer_add(&out, line, strlen(line));
            buffer_add(&out, "\n", 1);
            if (url)
                json_object_array_add(kept, json_object_new_string(url));
        }
        json_object_put(values);
        if (!end)
            break;
        line = end + 1;
    }
    Json *sorted = sorted_copy(records);
    json_object_object_foreach(sorted, url2, r)
    {
        if (json_object_get_boolean(jget(r, "active")) && !jhas(kept, url2)) {
            buffer_add(&out, url2, strlen(url2));
            buffer_add(&out, "\n", 1);
        }
    }
    json_object_put(sorted);
    json_object_put(kept);
    free(copy);
    while (out.len && isspace((unsigned char)out.data[out.len - 1]))
        out.len--;
    buffer_add(&out, "\n", 1);
    return out.data;
}

Json *sync_apply(App *a, Json *incoming)
{
    if (sync_validate(a, incoming))
        return NULL;
    int lock = lock_file(a, "channels.lock");
    if (lock < 0)
        return NULL;
    Json *merged = NULL, *local = NULL;
    char *text = NULL, *clock = NULL, *rendered = NULL, *current = NULL;
    bool success = false;
    if (db_begin(a))
        goto done;
    text = capture(a);
    if (!text)
        goto rollback;
    local = document(a);
    if (!local)
        goto rollback;
    merged = sync_merge(local, incoming);
    if (sync_validate(a, merged))
        goto rollback;
    clock = meta_get(a, "sync_clock");
    if (!clock)
        goto rollback;
    int64_t max_clock = *clock ? strtoll(clock, NULL, 10) : 0;
    const char *kinds[] = {"channels", "videos"};
    for (int i = 0; i < 2; i++) {
        Json *records = jget(merged, kinds[i]);
        json_object_object_foreach(records, key, record)
        {
            if (clock_of(record) > max_clock)
                max_clock = clock_of(record);
            if (put(a, kinds[i], key, record))
                goto rollback;
        }
    }
    char counter[32];
    snprintf(counter, sizeof counter, "%lld", (long long)max_clock);
    if (meta_set(a, "sync_clock", counter))
        goto rollback;
    long long now = (long long)time(NULL);
    Json *videos = jget(merged, "videos");
    json_object_object_foreach(videos, id, r)
    {
        const char *cid = jstr(r, "channel_id"), *name = *jstr(r, "name") ? jstr(r, "name") : cid;
        Json *owners = db_rows(a,
                               "SELECT channel_id FROM videos WHERE id=%Q UNION SELECT channel_id "
                               "FROM catalogue WHERE id=%Q",
                               id, id);
        if (!owners)
            goto rollback;
        for (size_t i = 0; i < json_object_array_length(owners); i++)
            if (strcmp(jstr(json_object_array_get_idx(owners, i), "channel_id"), cid)) {
                json_object_put(owners);
                fail(a, "Synced video has a conflicting owner");
                goto rollback;
            }
        json_object_put(owners);
        if (db_exec(a,
                    "INSERT OR IGNORE INTO channels(id,name,added_at,active) VALUES(%Q,%Q,%lld,0);"
                    "UPDATE actions SET undone=1 WHERE video_id=%Q AND EXISTS(SELECT 1 FROM videos "
                    "WHERE id=%Q AND status!=%Q);"
                    "INSERT INTO videos VALUES(%Q,%Q,%Q,%.17g,%lld,%Q,1,%lld) ON CONFLICT(id) DO "
                    "UPDATE SET status=excluded.status,title=excluded.title,"
                    "published=MAX(videos.published,excluded.published),changed=CASE WHEN "
                    "videos.status!=excluded.status THEN excluded.changed ELSE videos.changed END",
                    cid, name, now, id, id, jstr(r, "status"), id, cid, jstr(r, "title"),
                    json_object_get_double(jget(r, "published")), now, jstr(r, "status"), now))
            goto rollback;
        if (jget(r, "short") && record_type(a, id, json_object_get_boolean(jget(r, "short"))))
            goto rollback;
    }
    if (db_exec(a, "UPDATE sources SET active=0"))
        goto rollback;
    Json *channels = jget(merged, "channels");
    json_object_object_foreach(channels, url, record)
    {
        const char *cid = jstr(record, "channel_id"),
                   *name = *jstr(record, "name") ? jstr(record, "name") : cid;
        if (*cid &&
            db_exec(a,
                    "INSERT OR IGNORE INTO channels(id,name,added_at,active) VALUES(%Q,%Q,%lld,0)",
                    cid, name, now))
            goto rollback;
        if (db_exec(a,
                    "INSERT INTO sources(url,channel_id,added_at,active) VALUES(%Q,%Q,%lld,%d) ON "
                    "CONFLICT(url) DO UPDATE SET "
                    "active=excluded.active,channel_id=COALESCE(excluded.channel_id,sources."
                    "channel_id)",
                    url, *cid ? cid : NULL, now, json_object_get_boolean(jget(record, "active"))))
            goto rollback;
    }
    if (db_exec(a, "UPDATE channels SET active=0; UPDATE channels SET active=1 WHERE id IN(SELECT "
                   "channel_id FROM sources WHERE active=1)") ||
        db_commit(a))
        goto rollback;
    rendered = render_channels(a, text, channels);
    if (!rendered)
        goto done;
    char *path = strf("%s/channels.txt", a->config);
    current = read_file(a, path, MAX_DATA);
    if (!current || strcmp(current, text)) {
        free(path);
        fail(a, "channels.txt changed during sync; retry");
        goto done;
    }
    int rc = strcmp(rendered, text) ? atomic_write(a, path, rendered, strlen(rendered), 0600) : 0;
    free(path);
    if (rc)
        goto done;
    Json *urls = parse_channels(a, rendered);
    rc = urls ? meta_set(a, "sync_seen_channels", jtext(urls)) : -1;
    json_object_put(urls);
    success = rc == 0;
    goto done;
rollback:
    db_rollback(a);
done:
    free(text);
    free(clock);
    free(rendered);
    free(current);
    json_object_put(local);
    close(lock);
    if (!success) {
        json_object_put(merged);
        return NULL;
    }
    return merged;
}

bool sync_configured(App *a)
{
    char *path = strf("%s/sync.json", a->config);
    bool yes = !access(path, F_OK);
    free(path);
    return yes;
}
static bool repo_valid(const char *repo)
{
    const char *slash = strchr(repo, '/');
    if (!slash || slash == repo || !slash[1] || strchr(slash + 1, '/'))
        return false;
    for (const char *p = repo; *p; p++)
        if (!isalnum((unsigned char)*p) && *p != '/' && *p != '_' && *p != '-' && *p != '.')
            return false;
    return isalnum((unsigned char)repo[0]) && isalnum((unsigned char)slash[1]);
}
static bool token_valid(const char *token)
{
    if (!*token)
        return false;
    for (; *token; token++)
        if (!isalnum((unsigned char)*token) && *token != '_')
            return false;
    return true;
}
static Json *configuration(App *a)
{
    char *path = strf("%s/sync.json", a->config);
    Json *config = json_file(a, path);
    free(path);
    if (!config || !repo_valid(jstr(config, "repository")) || !token_valid(jstr(config, "token")) ||
        !json_object_is_type(jget(config, "repository_id"), json_type_int) ||
        json_object_get_int64(jget(config, "repository_id")) <= 0) {
        json_object_put(config);
        fail(a, "Invalid sync configuration. Run sync setup OWNER/REPO");
        return NULL;
    }
    return config;
}
static Json *request(App *a, Json *config, const char *suffix, Json *body, long *status)
{
    char *url = strf("https://api.github.com/repos/%s%s", jstr(config, "repository"), suffix);
    Buffer b = {0};
    Json *result = NULL;
    int rc =
        http(a, url, jstr(config, "token"), body ? jtext(body) : NULL, MAX_DATA * 2, &b, status);
    free(url);
    if (!rc && *status >= 200 && *status < 300)
        result = json_parse(a, b.data);
    else if (!rc)
        fail(a,
             "GitHub HTTP %ld. Check the private repository and token permissions; local data is "
             "retained.",
             *status);
    buffer_free(&b);
    return result;
}
static Json *repository(App *a, Json *config)
{
    long status = 0;
    Json *j = request(a, config, "", NULL, &status);
    if (!j)
        return NULL;
    Json *privacy = jget(j, "private"), *id = jget(j, "id"),
         *expected = jget(config, "repository_id");
    const char *visibility = jstr(j, "visibility");
    bool ok = json_object_is_type(privacy, json_type_boolean) && json_object_get_boolean(privacy) &&
              (!*visibility || !strcmp(visibility, "private")) &&
              json_object_is_type(id, json_type_int) && json_object_get_int64(id) > 0 &&
              (!expected || json_object_get_int64(expected) == json_object_get_int64(id)) &&
              !json_object_get_boolean(jget(j, "archived")) &&
              !json_object_get_boolean(jget(j, "disabled"));
    if (!ok) {
        json_object_put(j);
        fail(a, "Sync needs the original, active PRIVATE repository. No history uploaded.");
        return NULL;
    }
    return j;
}
static const char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
static char *base64_encode(const unsigned char *s, size_t n)
{
    char *out = xmalloc(((n + 2) / 3) * 4 + 1);
    size_t j = 0;
    for (size_t i = 0; i < n; i += 3) {
        unsigned value = (unsigned)s[i] << 16;
        if (i + 1 < n)
            value |= (unsigned)s[i + 1] << 8;
        if (i + 2 < n)
            value |= s[i + 2];
        out[j++] = alphabet[value >> 18];
        out[j++] = alphabet[(value >> 12) & 63];
        out[j++] = i + 1 < n ? alphabet[(value >> 6) & 63] : '=';
        out[j++] = i + 2 < n ? alphabet[value & 63] : '=';
    }
    out[j] = 0;
    return out;
}
static char *base64_decode(App *a, const char *s)
{
    Buffer clean = {0}, out = {0};
    for (; *s; s++)
        if (!isspace((unsigned char)*s))
            buffer_add(&clean, s, 1);
    if (clean.len % 4 || clean.len > ((MAX_DATA + 2) / 3) * 4)
        goto invalid;
    for (size_t i = 0; i < clean.len; i += 4) {
        unsigned v = 0;
        int pad = 0;
        for (int k = 0; k < 4; k++) {
            char c = clean.data[i + (size_t)k];
            const char *p = strchr(alphabet, c);
            if (c == '=') {
                if (k < 2 || i + 4 != clean.len)
                    goto invalid;
                pad++;
                v <<= 6;
            } else {
                if (!p || pad)
                    goto invalid;
                v = (v << 6) | (unsigned)(p - alphabet);
            }
        }
        if (pad > 2 || (pad == 1 && (v & 255)) || (pad == 2 && (v & 65535)))
            goto invalid;
        unsigned char bytes[3] = {(unsigned char)(v >> 16), (unsigned char)(v >> 8),
                                  (unsigned char)v};
        buffer_add(&out, bytes, (size_t)(3 - pad));
    }
    buffer_free(&clean);
    if (!out.data)
        buffer_add(&out, "", 0);
    if (out.len > MAX_DATA || memchr(out.data, 0, out.len))
        goto invalid;
    return out.data;
invalid:
    buffer_free(&clean);
    buffer_free(&out);
    fail(a, "Invalid base64 sync payload");
    return NULL;
}

int sync_now(App *a, bool quiet)
{
    if (!sync_configured(a)) {
        if (!quiet)
            puts("Sync is not configured. Run: yt-inbox-c sync setup OWNER/REPO");
        return 0;
    }
    int lock = lock_file(a, "sync.lock");
    if (lock < 0)
        return -1;
    int check = lock_file(a, "check.lock");
    if (check < 0) {
        close(lock);
        return -1;
    }
    Json *config = configuration(a), *info = NULL, *snapshot = NULL;
    int rc = -1;
    if (!config || !(info = repository(a, config)) || !(snapshot = sync_snapshot(a)))
        goto done;
    for (int attempt = 0; attempt < 4 && !interrupted; attempt++) {
        long status = 0;
        Json *file = request(a, config, "/contents/yt-inbox-state.json", NULL, &status),
             *remote = NULL, *merged = NULL;
        char *sha = NULL, *raw = NULL;
        if (!file && status == 404) {
            remote = empty_document();
            a->error[0] = 0;
        } else if (file) {
            if (strcmp(jstr(file, "type"), "file") || !hex_id(jstr(file, "sha"), 40) ||
                !json_object_is_type(jget(file, "size"), json_type_int) ||
                json_object_get_int64(jget(file, "size")) < 0 ||
                json_object_get_int64(jget(file, "size")) > MAX_DATA) {
                json_object_put(file);
                fail(a, "Remote state is not a valid file or exceeds 16 MB");
                break;
            }
            sha = strdup(jstr(file, "sha"));
            if (strcmp(jstr(file, "encoding"), "base64")) {
                char *suffix = strf("/git/blobs/%s", sha);
                json_object_put(file);
                file = request(a, config, suffix, NULL, &status);
                free(suffix);
            }
            if (file && !strcmp(jstr(file, "encoding"), "base64"))
                raw = base64_decode(a, jstr(file, "content"));
            if (raw)
                remote = json_parse(a, raw);
        }
        json_object_put(file);
        free(raw);
        if (!remote || sync_validate(a, remote)) {
            json_object_put(remote);
            free(sha);
            break;
        }
        merged = sync_apply(a, remote);
        if (!merged) {
            json_object_put(remote);
            free(sha);
            break;
        }
        bool retry = false;
        if (!json_object_equal(merged, remote)) {
            Json *private = repository(a, config);
            const char *content = jtext(merged);
            if (!private || strlen(content) > MAX_DATA) {
                json_object_put(private);
                json_object_put(merged);
                json_object_put(remote);
                free(sha);
                if (*a->error == 0)
                    fail(a, "Sync state exceeds 16 MB");
                break;
            }
            json_object_put(private);
            char *encoded = base64_encode((const unsigned char *)content, strlen(content));
            Json *body = json_object_new_object();
            jsetstr(body, "message", "Synchronize YouTube Inbox");
            jsetstr(body, "content", encoded);
            if (sha)
                jsetstr(body, "sha", sha);
            free(encoded);
            status = 0;
            Json *reply = request(a, config, "/contents/yt-inbox-state.json", body, &status);
            json_object_put(body);
            retry = status == 409 || status == 422;
            if (!reply && !retry) {
                json_object_put(merged);
                json_object_put(remote);
                free(sha);
                break;
            }
            json_object_put(reply);
        }
        json_object_put(merged);
        json_object_put(remote);
        free(sha);
        if (retry) {
            fail(a, "Sync conflict; local changes retained. Retry shortly.");
            continue;
        }
        char stamp[32];
        snprintf(stamp, sizeof stamp, "%lld", (long long)time(NULL));
        if (meta_set(a, "sync_last_success", stamp) || meta_set(a, "sync_error", ""))
            break;
        a->error[0] = 0;
        rc = 0;
        if (!quiet)
            puts("Channels and watch history synchronized.");
        break;
    }
done:
    if (rc) {
        char error[sizeof a->error];
        snprintf(error, sizeof error, "%s", *a->error ? a->error : "Sync failed");
        meta_set(a, "sync_error", error);
        snprintf(a->error, sizeof a->error, "%s", error);
    }
    json_object_put(config);
    json_object_put(info);
    json_object_put(snapshot);
    close(check);
    close(lock);
    return rc;
}

void sync_kick(App *a)
{
    if (!sync_configured(a) || getenv("YT_INBOX_HOME") || !available("systemctl"))
        return;
    char saved[sizeof a->error];
    memcpy(saved, a->error, sizeof saved);
    char *args[] = {"systemctl", "--user", "start", "--no-block", "yt-inbox-sync.service", NULL};
    Buffer output = {0};
    process(a, args, 3, &output, false);
    buffer_free(&output);
    memcpy(a->error, saved, sizeof saved);
}
void sync_status(App *a)
{
    if (!sync_configured(a)) {
        puts("Sync is not configured.");
        return;
    }
    Json *config = configuration(a);
    if (!config)
        return;
    char *stamp = meta_get(a, "sync_last_success"), *error = meta_get(a, "sync_error");
    printf("Sync: %s\nLast success (Unix time): %s\n", jstr(config, "repository"),
           stamp && *stamp ? stamp : "never");
    if (error && *error)
        print_plain(error);
    free(stamp);
    free(error);
    json_object_put(config);
}

int sync_setup(App *a, const char *repo)
{
    if (!repo_valid(repo))
        return fail(a, "Use OWNER/REPO for a separate private repository");
    if (!isatty(STDIN_FILENO))
        return fail(a, "Run setup in an interactive terminal for the hidden token prompt");
    struct termios saved, hidden;
    if (tcgetattr(STDIN_FILENO, &saved))
        return fail(a, "Cannot hide the token prompt");
    hidden = saved;
    hidden.c_lflag &= (tcflag_t)~ECHO;
    if (tcsetattr(STDIN_FILENO, TCSAFLUSH, &hidden))
        return fail(a, "Cannot hide the token prompt");
    fputs("GitHub token (hidden; selected private repository, Contents read/write): ", stdout);
    fflush(stdout);
    char token[1024] = {0};
    char *got = fgets(token, sizeof token, stdin);
    tcsetattr(STDIN_FILENO, TCSAFLUSH, &saved);
    putchar('\n');
    bool full = got && strchr(token, '\n');
    token[strcspn(token, "\r\n")] = 0;
    if (!full || interrupted || !token_valid(token)) {
        explicit_bzero(token, sizeof token);
        return fail(a, "Invalid token or cancelled setup");
    }
    Json *config = json_object_new_object();
    jsetstr(config, "repository", repo);
    jsetstr(config, "token", token);
    explicit_bzero(token, sizeof token);
    Json *info = repository(a, config);
    if (!info) {
        json_object_put(config);
        return -1;
    }
    json_object_object_add(config, "repository_id", jclone(jget(info, "id")));
    json_object_put(info);
    int lock = lock_file(a, "sync.lock"), channel = -1, rc = -1;
    char *old = NULL;
    if (lock < 0 || (channel = lock_file(a, "channels.lock")) < 0)
        goto done;
    old = meta_get(a, "sync_repository_id");
    if (!old)
        goto done;
    char identity[32];
    snprintf(identity, sizeof identity, "%lld",
             (long long)json_object_get_int64(jget(config, "repository_id")));
    if (*old && strcmp(old, identity)) {
        fail(a, "This installation is paired with a different repository; reuse the original");
        goto done;
    }
    char *backup = strf("%s/before-sync.sqlite3", a->data);
    rc = db_backup(a, backup);
    free(backup);
    if (rc)
        goto done;
    char *path = strf("%s/channels.txt", a->config), *text = read_file(a, path, MAX_DATA);
    free(path);
    backup = strf("%s/channels.before-sync.txt", a->config);
    if (!text) {
        free(backup);
        rc = -1;
        goto done;
    }
    if (access(backup, F_OK))
        rc = atomic_write(a, backup, text, strlen(text), 0600);
    free(text);
    free(backup);
    if (rc || db_begin(a)) {
        rc = -1;
        goto done;
    }
    text = capture(a);
    rc = text ? meta_set(a, "sync_repository_id", identity) : -1;
    free(text);
    if (!rc)
        rc = db_commit(a);
    if (rc) {
        db_rollback(a);
        goto done;
    }
    path = strf("%s/sync.json", a->config);
    rc = atomic_write(a, path, jtext(config), strlen(jtext(config)), 0600);
    free(path);
done:
    free(old);
    json_object_put(config);
    if (channel >= 0)
        close(channel);
    if (lock >= 0)
        close(lock);
    if (!rc) {
        rc = sync_now(a, false);
        if (!getenv("YT_INBOX_HOME")) {
            Buffer b = {0};
            char *args[] = {"systemctl", "--user", "enable", "--now", "yt-inbox-sync.timer", NULL};
            char saved_error[sizeof a->error];
            memcpy(saved_error, a->error, sizeof saved_error);
            process(a, args, 5, &b, false);
            buffer_free(&b);
            memcpy(a->error, saved_error, sizeof saved_error);
        }
    }
    return rc;
}

int sync_disable(App *a)
{
    int lock = lock_file(a, "sync.lock");
    if (lock < 0)
        return -1;
    char *path = strf("%s/sync.json", a->config);
    int rc = unlink(path);
    free(path);
    close(lock);
    if (rc && errno != ENOENT)
        return fail(a, "Cannot remove saved sync token");
    if (!getenv("YT_INBOX_HOME")) {
        char *args[] = {"systemctl", "--user", "disable", "--now", "yt-inbox-sync.timer", NULL};
        Buffer b = {0};
        process(a, args, 5, &b, false);
        buffer_free(&b);
    }
    a->error[0] = 0;
    puts("Sync disabled; local and remote history retained.");
    return 0;
}
