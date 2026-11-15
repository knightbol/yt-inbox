/* Delegate extraction/decoding to their maintained tools. The inbox owns
 * selection, bounded formats, download state and explicit user actions. */
#include "inbox.h"
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <ftw.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static bool media_extension(const char *path)
{
    const char *dot = strrchr(path, '.');
    if (!dot)
        return false;
    const char *extensions[] = {".mp4", ".mkv", ".webm", ".mov", ".m4v",
                                ".3gp", ".flv", ".avi",  ".ts",  NULL};
    for (int i = 0; extensions[i]; i++)
        if (!strcasecmp(dot, extensions[i]))
            return true;
    return false;
}
static char *valid_file(App *a, const char *id, const char *path)
{
    if (!valid_video(id) || !*path)
        return NULL;
    char *folder = strf("%s/downloads/%s", a->data, id), root[PATH_MAX], resolved[PATH_MAX];
    bool ok = realpath(folder, root) && realpath(path, resolved);
    free(folder);
    if (!ok)
        return NULL;
    struct stat st;
    char *last = strrchr(resolved, '/');
    if (!last || stat(resolved, &st) || !S_ISREG(st.st_mode) || st.st_size <= 0 ||
        !media_extension(resolved))
        return NULL;
    *last = 0;
    ok = !strcmp(root, resolved);
    *last = '/';
    return ok ? strdup(resolved) : NULL;
}
char *local_video(App *a, const char *id)
{
    Json *rows = db_rows(a, "SELECT path FROM downloads WHERE id=%Q AND state='complete'", id);
    if (!rows)
        return NULL;
    char *path = valid_file(a, id, jstr(jfirst(rows), "path"));
    json_object_put(rows);
    return path;
}

static char *format_selector(int height, int fps, bool compatible, bool h264)
{
    char *limits = strf("[height<=%d][fps<=?%d]", height, fps);
    const char *codec = compatible ? "[vcodec!^=av01]" : "";
    char *bounded = strf("bv*%s%s+ba/b%s%s", limits, codec, limits, codec);
    char *result =
        h264 ? strf("bv*%s[vcodec^=avc1]+ba/b%s[vcodec^=avc1]/%s", limits, limits, bounded)
             : strdup(bounded);
    free(limits);
    free(bounded);
    return result;
}
static char *download_format(App *a)
{
    int height = jget(a->settings, "download_height")
                     ? json_object_get_int(jget(a->settings, "download_height"))
                     : 720;
    const char *mode = playback_mode(a);
    if (!strcmp(mode, "custom"))
        return strf("bv*[height<=%d]+ba/b[height<=%d]", height, height);
    return format_selector(height, !strcmp(mode, "low") ? 30 : 60, strcmp(mode, "high") != 0,
                           !strcmp(mode, "auto") || !strcmp(mode, "low"));
}

int download_video(App *a, const char *id)
{
    if (!valid_video(id))
        return fail(a, "Invalid video ID");
    char *existing = local_video(a, id);
    if (existing) {
        free(existing);
        puts("Already downloaded. Enter plays offline.");
        return 0;
    }
    if (!available("yt-dlp") || !available("ffmpeg"))
        return fail(a, "Install yt-dlp and ffmpeg for offline downloads");
    Json *known = db_rows(
        a, "SELECT id FROM videos WHERE id=%Q UNION SELECT id FROM catalogue WHERE id=%Q", id, id);
    bool found = known && json_object_array_length(known);
    json_object_put(known);
    if (!found)
        return fail(a, "Load the video's channel before downloading");
    char *directory = strf("%s/downloads/%s", a->data, id);
    if (mkdirs(a, directory)) {
        free(directory);
        return -1;
    }
    char *lockpath = strf("%s/download.lock", directory);
    /* Inherited by yt-dlp: a killed inbox cannot release the lock while its
     * downloader is still running. Ordinary cancellation kills the whole job. */
    int lock = open(lockpath, O_CREAT | O_RDWR, 0600);
    free(lockpath);
    if (lock < 0 || flock(lock, LOCK_EX | LOCK_NB)) {
        if (lock >= 0)
            close(lock);
        free(directory);
        return fail(a, "This video is already downloading");
    }
    int rc = db_exec(a,
                     "INSERT INTO downloads(id,state,updated) VALUES(%Q,'downloading',%lld) ON "
                     "CONFLICT(id) DO UPDATE SET "
                     "state='downloading',path='',error='',updated=excluded.updated",
                     id, (long long)time(NULL));
    Buffer template = {0};
    for (const char *p = directory; *p; p++) {
        buffer_add(&template, p, 1);
        if (*p == '%')
            buffer_add(&template, "%", 1);
    }
    buffer_add(&template, "/media.%(ext)s", strlen("/media.%(ext)s"));
    char *format = download_format(a), *url = strf("https://www.youtube.com/watch?v=%s", id);
    char *argv[] = {"yt-dlp",
                    "--ignore-config",
                    "--no-playlist",
                    "--no-simulate",
                    "--no-warnings",
                    "--no-colors",
                    "--newline",
                    "--progress",
                    "--progress-delta",
                    "1",
                    "--socket-timeout",
                    "20",
                    "--retries",
                    "3",
                    "--fragment-retries",
                    "3",
                    "--abort-on-unavailable-fragments",
                    "--continue",
                    "--part",
                    "--no-overwrites",
                    "--no-wait-for-video",
                    "--match-filter",
                    "!is_live",
                    "--format",
                    format,
                    "--merge-output-format",
                    "mkv",
                    "--output",
                    template.data,
                    "--print",
                    "after_move:YT_INBOX_FILE:%(filepath)j",
                    "--progress-template",
                    "download:YT_INBOX_PROGRESS:%(progress._percent_str)s",
                    "--",
                    url,
                    NULL};
    Buffer output = {0};
    if (!rc)
        rc = process(a, argv, 0, &output, true);
    char *file = NULL;
    if (!rc) {
        char *save, *line;
        for (line = strtok_r(output.data, "\n", &save); line; line = strtok_r(NULL, "\n", &save))
            if (!strncmp(line, "YT_INBOX_FILE:", 14)) {
                Json *j = json_parse(a, line + 14);
                if (json_object_is_type(j, json_type_string)) {
                    free(file);
                    file = valid_file(a, id, json_object_get_string(j));
                }
                json_object_put(j);
            }
        if (!file)
            rc = fail(a, "Download ended without a complete media file; retry to resume");
    }
    char saved[sizeof a->error];
    snprintf(saved, sizeof saved, "%s", a->error);
    if (db_exec(a, "UPDATE downloads SET state=%Q,path=%Q,error=%Q,updated=%lld WHERE id=%Q",
                !rc           ? "complete"
                : interrupted ? "cancelled"
                              : "failed",
                file ? file : "", rc ? saved : "", (long long)time(NULL), id))
        rc = -1;
    else if (rc)
        snprintf(a->error, sizeof a->error, "%s", saved);
    else {
        a->error[0] = 0;
        puts("Download ready. Playback does not mark it watched.");
    }
    free(file);
    free(url);
    free(format);
    buffer_free(&output);
    buffer_free(&template);
    free(directory);
    close(lock);
    return rc;
}

static int remove_item(const char *path, const struct stat *st, int type, struct FTW *walk)
{
    (void)st;
    (void)type;
    (void)walk;
    return remove(path);
}
int remove_download(App *a, const char *id)
{
    if (!valid_video(id))
        return fail(a, "Invalid video ID");
    char *folder = strf("%s/downloads/%s", a->data, id),
         *lockpath = strf("%s/download.lock", folder);
    int lock = open(lockpath, O_RDWR | O_CLOEXEC);
    free(lockpath);
    if (lock >= 0 && flock(lock, LOCK_EX | LOCK_NB)) {
        close(lock);
        free(folder);
        return fail(a, "Video is still downloading");
    }
    struct stat st;
    int rc = 0;
    if (!lstat(folder, &st))
        rc = S_ISLNK(st.st_mode) ? unlink(folder)
                                 : nftw(folder, remove_item, 16, FTW_DEPTH | FTW_PHYS);
    else if (errno != ENOENT)
        rc = -1;
    free(folder);
    if (lock >= 0)
        close(lock);
    if (rc)
        return fail(a, "Could not remove offline files: %s", strerror(errno));
    return db_exec(a, "DELETE FROM downloads WHERE id=%Q", id);
}

static bool local_vt(void)
{
    const char *tty = ttyname(STDIN_FILENO);
    if (!tty || strncmp(tty, "/dev/tty", 8) || tty[8] < '1' || tty[8] > '9')
        return false;
    for (const char *p = tty + 9; *p; p++)
        if (!isdigit((unsigned char)*p))
            return false;
    return true;
}

int play_video(App *a, const char *id, const char *output)
{
    if (!valid_video(id))
        return fail(a, "Invalid video ID");
    if (strcmp(output, "auto") && strcmp(output, "drm") && strcmp(output, "window"))
        return fail(a, "Video output must be auto, drm or window");
    if (!available("mpv"))
        return fail(a, "Install mpv for video playback");
    char *file = local_video(a, id);
    if (!file) {
        Json *records = db_rows(a, "SELECT state FROM downloads WHERE id=%Q", id);
        bool missing = records && !strcmp(jstr(jfirst(records), "state"), "complete");
        json_object_put(records);
        if (missing)
            return fail(a, "Saved video is missing; download it again");
        if (!available("yt-dlp"))
            return fail(a, "Install yt-dlp for streaming");
    }
    const char *mode = playback_mode(a);
    bool drm = !strcmp(output, "drm") || (local_vt() && !strcmp(output, "auto"));
    char *args[40] = {0};
    size_t n = 0;
#define ARG(value) args[n++] = strdup(value)
    ARG("mpv");
    ARG("--terminal=yes");
    ARG("--input-terminal=yes");
    ARG("--term-status-msg=");
    ARG("--msg-level=all=warn");
    char *script = strf("%s/playback.lua", a->assets);
    if (access(script, R_OK)) {
        for (size_t i = 0; i < n; i++)
            free(args[i]);
        free(script);
        free(file);
        return fail(a, "playback.lua is missing beside the executable; rebuild or reinstall");
    }
    args[n++] = strf("--script=%s", script);
    free(script);
    args[n++] = strf("--script-opts-append=yt_inbox-mode=%s", mode);
    args[n++] = strf("--script-opts-append=yt_inbox-online=%s", file ? "no" : "yes");
    ARG("--script-opts-append=yt_inbox-stage=0");
    ARG("--script-opts-append=yt_inbox-telemetry=no");
    char *formats[4] = {format_selector(720, 60, true, false), format_selector(720, 30, true, true),
                        format_selector(720, 30, true, true), format_selector(480, 30, true, true)};
    for (int i = 0; i < 4; i++)
        args[n++] = strf("--script-opts-append=yt_inbox-format%d=%s", i, formats[i]);
    if (strcmp(mode, "custom"))
        ARG("--hwdec=auto");
    if (file)
        ARG("--ytdl=no");
    else if (strcmp(mode, "custom")) {
        char *selected = !strcmp(mode, "auto")       ? strdup(formats[0])
                         : !strcmp(mode, "low")      ? format_selector(720, 30, true, true)
                         : !strcmp(mode, "balanced") ? format_selector(1080, 60, true, false)
                                                     : format_selector(2160, 60, false, false);
        ARG("--ytdl=yes");
        args[n++] = strf("--ytdl-format=%s", selected);
        free(selected);
    }
    /* tmux decides whether its single local client is a VT before handing off.
     * Its helper adds DRM flags only for that verified terminal. */
    if (drm && local_vt()) {
        ARG("--vo=gpu");
        ARG("--gpu-context=drm");
        ARG("--gpu-api=opengl");
    }
    ARG("--");
    args[n++] = file ? strdup(file) : strf("https://www.youtube.com/watch?v=%s", id);
    args[n] = NULL;
    int rc = 1;
    if (!local_vt() && strcmp(output, "window"))
        rc = tmux_play(a, args);
    if (rc == 1) {
        if (drm && !local_vt())
            rc = fail(a, "DRM needs a local Linux VT, directly or via one attached tmux client");
        else if (!drm && !getenv("DISPLAY") && !getenv("WAYLAND_DISPLAY"))
            rc = fail(a, "No graphical display or local Linux console; browse/download here and "
                         "play locally");
        else
            rc = foreground(a, args);
    }
    for (size_t i = 0; i < n; i++)
        free(args[i]);
    for (int i = 0; i < 4; i++)
        free(formats[i]);
    free(file);
    return rc;
#undef ARG
}
