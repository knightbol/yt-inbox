#ifndef YT_INBOX_H
#define YT_INBOX_H

/* Shared interfaces. A returned Json* or char* belongs to the caller unless
 * marked borrowed. Functions report errors in App.error and return -1/NULL.
 * SQL format strings are internal constants; user values always use %Q. */
#include <json-c/json.h>
#include <sqlite3.h>
#include <stdbool.h>
#include <stddef.h>
#include <signal.h>
#include <limits.h>
#include <stdint.h>

#define VERSION "1.6.0-c.1"
#define MAX_DATA (16U * 1000U * 1000U)
typedef struct json_object Json;
typedef struct {
    char *data;
    size_t len, cap;
} Buffer;
typedef struct {
    char config[PATH_MAX], data[PATH_MAX], state[PATH_MAX];
    char executable[PATH_MAX], assets[PATH_MAX], error[2048];
    sqlite3 *db;
    Json *settings;
} App;
extern volatile sig_atomic_t interrupted;

void *xmalloc(size_t n);
char *strf(const char *format, ...) __attribute__((format(printf, 1, 2)));
int fail(App *a, const char *format, ...) __attribute__((format(printf, 2, 3)));
void buffer_add(Buffer *b, const void *data, size_t n);
void buffer_free(Buffer *b);
char *clean_text(const char *s); /* Strip terminal controls, keep valid UTF-8. */
void print_plain(const char *s);
int mkdirs(App *a, const char *path);
char *read_file(App *a, const char *path, size_t limit);
int atomic_write(App *a, const char *path, const void *data, size_t n, int mode);
int lock_file(App *a, const char *name); /* Caller closes returned descriptor. */
int app_open(App *a);
void app_close(App *a);
int settings_load(App *a);
int settings_save(App *a);
bool hide_shorts(App *a);
const char *playback_mode(App *a); /* Borrowed. */

Json *jget(Json *j, const char *key);       /* Borrowed; NULL for absent/null. */
Json *jfirst(Json *j);                      /* Borrowed first row, or NULL. */
const char *jstr(Json *j, const char *key); /* Borrowed; "" for absent. */
const char *jtext(Json *j);                 /* Borrowed. */
Json *jclone(Json *j);
Json *json_parse(App *a, const char *s);
Json *json_file(App *a, const char *path);
bool jhas(Json *array, const char *value);
void jsetstr(Json *j, const char *key, const char *value);

int db_exec(App *a, const char *format, ...);
Json *db_rows(App *a, const char *format, ...);
char *meta_get(App *a, const char *key);
int meta_set(App *a, const char *key, const char *value);
int db_begin(App *a);
int db_commit(App *a);
void db_rollback(App *a);
int db_backup(App *a, const char *path);
Json *list_rows(App *a, const char *view, const char *channel);
int set_status(App *a, int count, char **ids, const char *status);
int undo_status(App *a);
int record_type(App *a, const char *id, int type);

/* Child argv is never interpreted by a shell. Capture has a size limit and
 * timeout; Ctrl+C terminates the child's process group (including ffmpeg). */
int process(App *a, char *const argv[], int timeout, Buffer *out, bool echo);
int foreground(App *a, char *const argv[]);
bool available(const char *name);
int http(App *a, const char *url, const char *token, const char *body, size_t limit, Buffer *out,
         long *status);
Json *ytdlp(App *a, const char *url, const char *const options[], int timeout);

bool valid_video(const char *s);
bool valid_channel(const char *s);
char *canonical_channel(App *a, const char *s);
Json *parse_channels(App *a, const char *text);
int edit_channels(App *a);
int check_uploads(App *a, bool scheduled, bool notify);
int load_catalogue(App *a, const char *cid, bool restart);
int classify_saved(App *a);
int metadata_type(Json *j);
Json *parse_feed(App *a, const char *xml, const char *cid);
int ingest(App *a, const char *cid, const char *name, Json *entries, const char *cycle,
           bool complete);

/* Sync data format is shared with syncing.py. Both implementations use the
 * same locks, revision ordering and atomic database/file projection. */
bool sync_configured(App *a);
int sync_record_status(App *a, const char *id);
int sync_validate(App *a, Json *doc);
Json *sync_merge(Json *left, Json *right);
Json *sync_snapshot(App *a);
Json *sync_apply(App *a, Json *incoming);
int sync_now(App *a, bool quiet);
int sync_setup(App *a, const char *repo);
int sync_disable(App *a);
void sync_status(App *a);
void sync_kick(App *a);

char *local_video(App *a, const char *id);
int download_video(App *a, const char *id);
int remove_download(App *a, const char *id);
int play_video(App *a, const char *id, const char *output);
int tmux_play(App *a, char **argv); /* 1 = not a local VT tmux client. */
int tmux_job(App *a, const char *path);
int tui(App *a, const char *output);
int install_native(App *a, bool enable, bool primary);
int notify_digest(App *a, bool test);
int run_command(App *a, int argc, char **argv);
#endif
