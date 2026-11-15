#include "inbox.h"
#include <curl/curl.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <poll.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>
#include <wchar.h>
#include <wctype.h>

volatile sig_atomic_t interrupted;

void *xmalloc(size_t n)
{
    void *p = malloc(n ? n : 1);
    if (!p) {
        fputs("Out of memory\n", stderr);
        exit(1);
    }
    return p;
}

char *strf(const char *format, ...)
{
    va_list ap;
    va_start(ap, format);
    char *s = NULL;
    int n = vasprintf(&s, format, ap);
    va_end(ap);
    if (n < 0) {
        fputs("Out of memory\n", stderr);
        exit(1);
    }
    return s;
}

int fail(App *a, const char *format, ...)
{
    va_list ap;
    va_start(ap, format);
    vsnprintf(a->error, sizeof a->error, format, ap);
    va_end(ap);
    return -1;
}

void buffer_add(Buffer *b, const void *data, size_t n)
{
    if (n > SIZE_MAX - b->len - 1) {
        fputs("Buffer overflow\n", stderr);
        exit(1);
    }
    size_t need = b->len + n + 1;
    if (need > b->cap) {
        size_t cap = need > SIZE_MAX / 2 ? need : need * 2;
        char *p = realloc(b->data, cap);
        if (!p) {
            fputs("Out of memory\n", stderr);
            exit(1);
        }
        b->data = p;
        b->cap = cap;
    }
    memcpy(b->data + b->len, data, n);
    b->len += n;
    b->data[b->len] = 0;
}

void buffer_free(Buffer *b)
{
    free(b->data);
    memset(b, 0, sizeof *b);
}

char *clean_text(const char *s)
{
    Buffer b = {0};
    mbstate_t state = {0};
    while (*s) {
        wchar_t wc;
        size_t n = mbrtowc(&wc, s, MB_CUR_MAX, &state);
        if (n == (size_t)-1 || n == (size_t)-2) {
            buffer_add(&b, "?", 1);
            s++;
            memset(&state, 0, sizeof state);
        } else {
            if (!n)
                break;
            if (iswprint(wc))
                buffer_add(&b, s, n);
            else
                buffer_add(&b, " ", 1);
            s += n;
        }
    }
    if (!b.data)
        buffer_add(&b, "", 0);
    return b.data;
}

void print_plain(const char *s)
{
    char *plain = clean_text(s);
    puts(plain);
    free(plain);
}

int mkdirs(App *a, const char *path)
{
    if (strlen(path) >= PATH_MAX)
        return fail(a, "Path is too long");
    char copy[PATH_MAX];
    strcpy(copy, path);
    for (char *p = copy + 1;; p++) {
        if (*p && *p != '/')
            continue;
        char c = *p;
        *p = 0;
        if (mkdir(copy, 0700) && errno != EEXIST)
            return fail(a, "mkdir: %s", strerror(errno));
        struct stat st;
        if (stat(copy, &st) || !S_ISDIR(st.st_mode))
            return fail(a, "Not a directory: %s", copy);
        *p = c;
        if (!c)
            break;
    }
    return 0;
}

char *read_file(App *a, const char *path, size_t limit)
{
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        fail(a, "Cannot read %s: %s", path, strerror(errno));
        return NULL;
    }
    Buffer b = {0};
    char block[8192];
    ssize_t n;
    while ((n = read(fd, block, sizeof block)) != 0) {
        if (n < 0 && errno == EINTR)
            continue;
        if (n < 0 || b.len + (size_t)n > limit) {
            fail(a, "Cannot read file or file exceeds size limit: %s", path);
            close(fd);
            buffer_free(&b);
            return NULL;
        }
        buffer_add(&b, block, (size_t)n);
    }
    close(fd);
    if (!b.data)
        buffer_add(&b, "", 0);
    if (memchr(b.data, 0, b.len)) {
        fail(a, "Unexpected NUL in text file");
        buffer_free(&b);
        return NULL;
    }
    return b.data;
}

int atomic_write(App *a, const char *path, const void *data, size_t n, int mode)
{
    char *temp = strf("%s.tmp-XXXXXX", path);
    int fd = mkostemp(temp, O_CLOEXEC), result = -1;
    if (fd < 0) {
        free(temp);
        return fail(a, "Cannot create temporary file: %s", strerror(errno));
    }
    const char *p = data;
    size_t left = n;
    while (left) {
        ssize_t written = write(fd, p, left);
        if (written < 0 && errno == EINTR)
            continue;
        if (written <= 0)
            goto done;
        p += written;
        left -= (size_t)written;
    }
    if (fchmod(fd, (mode_t)mode) || fsync(fd) || rename(temp, path))
        goto done;
    /* Persist the directory entry as well as the contents. */
    char *parent = strdup(path), *slash = strrchr(parent, '/');
    if (slash)
        *slash = 0;
    int directory =
        open(slash ? (*parent ? parent : "/") : ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    free(parent);
    if (directory < 0)
        goto done;
    int synced = fsync(directory);
    close(directory);
    if (synced)
        goto done;
    result = 0;
done:
    if (result)
        fail(a, "Cannot save %s: %s", path, strerror(errno));
    close(fd);
    unlink(temp);
    free(temp);
    return result;
}

int lock_file(App *a, const char *name)
{
    char *path = strf("%s/%s", a->state, name);
    int fd = open(path, O_RDWR | O_CREAT | O_CLOEXEC, 0600);
    free(path);
    if (fd < 0)
        return fail(a, "Cannot open lock: %s", strerror(errno));
    if (flock(fd, LOCK_EX | LOCK_NB)) {
        close(fd);
        return fail(a, "Another operation holds %s. Try again shortly.", name);
    }
    return fd;
}

Json *jget(Json *j, const char *key)
{
    Json *v = NULL;
    if (json_object_is_type(j, json_type_object))
        json_object_object_get_ex(j, key, &v);
    return v;
}
Json *jfirst(Json *j)
{
    return json_object_is_type(j, json_type_array) && json_object_array_length(j)
               ? json_object_array_get_idx(j, 0)
               : NULL;
}
const char *jstr(Json *j, const char *key)
{
    Json *v = jget(j, key);
    return json_object_is_type(v, json_type_string) ? json_object_get_string(v) : "";
}
const char *jtext(Json *j)
{
    return json_object_to_json_string_ext(j,
                                          JSON_C_TO_STRING_PLAIN | JSON_C_TO_STRING_NOSLASHESCAPE);
}
Json *jclone(Json *j)
{
    Json *copy = NULL;
    json_object_deep_copy(j, &copy, NULL);
    return copy;
}
void jsetstr(Json *j, const char *key, const char *v)
{
    json_object_object_add(j, key, json_object_new_string(v ? v : ""));
}
bool jhas(Json *array, const char *value)
{
    for (size_t i = 0; i < json_object_array_length(array); i++)
        if (!strcmp(json_object_get_string(json_object_array_get_idx(array, i)), value))
            return true;
    return false;
}

static bool finite_numbers(Json *j)
{
    if (json_object_is_type(j, json_type_double))
        return isfinite(json_object_get_double(j));
    if (json_object_is_type(j, json_type_array)) {
        for (size_t i = 0; i < json_object_array_length(j); i++)
            if (!finite_numbers(json_object_array_get_idx(j, i)))
                return false;
    } else if (json_object_is_type(j, json_type_object)) {
        json_object_object_foreach(j, key, value)
        {
            (void)key;
            if (!finite_numbers(value))
                return false;
        }
    }
    return true;
}

Json *json_parse(App *a, const char *s)
{
    if (!s || strlen(s) > MAX_DATA * 2) {
        fail(a, "JSON exceeds size limit");
        return NULL;
    }
    /* C strings cannot represent embedded NULs, including object keys. Reject
     * that JSON escape before json-c can truncate a key at its C boundary. */
    for (const char *p = s; *p; p++)
        if (*p == '\\' && p[1]) {
            if (!strncmp(p + 1, "u0000", 5)) {
                fail(a, "Embedded NUL in JSON string");
                return NULL;
            }
            p++;
        }
    /* json-c 0.18's strict mode rejects literal non-ASCII even when UTF-8 is
     * valid. Normalize only the spelling of Unicode to JSON escapes, without
     * relaxing strict syntax or changing the resulting strings. */
    Buffer normalized = {0};
    for (const unsigned char *p = (const unsigned char *)s; *p;) {
        if (*p == '\\' && p[1]) {
            if (p[1] >= 128) {
                buffer_free(&normalized);
                fail(a, "Invalid JSON escape");
                return NULL;
            }
            buffer_add(&normalized, p, 2);
            p += 2;
            continue;
        }
        if (*p < 128) {
            buffer_add(&normalized, p++, 1);
            continue;
        }
        unsigned cp;
        int count;
        if (*p >= 0xc2 && *p <= 0xdf) {
            cp = *p & 31;
            count = 2;
        } else if (*p >= 0xe0 && *p <= 0xef) {
            cp = *p & 15;
            count = 3;
        } else if (*p >= 0xf0 && *p <= 0xf4) {
            cp = *p & 7;
            count = 4;
        } else {
            buffer_free(&normalized);
            fail(a, "Invalid UTF-8 JSON");
            return NULL;
        }
        for (int i = 1; i < count; i++) {
            if ((p[i] & 0xc0) != 0x80) {
                buffer_free(&normalized);
                fail(a, "Invalid UTF-8 JSON");
                return NULL;
            }
            cp = (cp << 6) | (p[i] & 63);
        }
        if ((count == 3 && cp < 0x800) || (count == 4 && cp < 0x10000) || cp > 0x10ffff ||
            (cp >= 0xd800 && cp <= 0xdfff)) {
            buffer_free(&normalized);
            fail(a, "Invalid UTF-8 JSON");
            return NULL;
        }
        char escaped[16];
        if (cp <= 0xffff)
            snprintf(escaped, sizeof escaped, "\\u%04x", cp);
        else {
            cp -= 0x10000;
            snprintf(escaped, sizeof escaped, "\\u%04x\\u%04x", 0xd800 + (cp >> 10),
                     0xdc00 + (cp & 1023));
        }
        buffer_add(&normalized, escaped, strlen(escaped));
        p += count;
    }
    if (!normalized.data)
        buffer_add(&normalized, "", 0);
    const char *input = normalized.data;
    struct json_tokener *tok = json_tokener_new_ex(64);
    json_tokener_set_flags(tok, JSON_TOKENER_STRICT | JSON_TOKENER_VALIDATE_UTF8);
    Json *j = json_tokener_parse_ex(tok, input, (int)normalized.len + 1);
    size_t end = json_tokener_get_parse_end(tok);
    bool ok = json_tokener_get_error(tok) == json_tokener_success;
    while (end < normalized.len && isspace((unsigned char)input[end]))
        end++;
    if (!ok || end < normalized.len || !finite_numbers(j)) {
        json_object_put(j);
        j = NULL;
        fail(a, "Invalid JSON data");
    }
    json_tokener_free(tok);
    buffer_free(&normalized);
    return j;
}

Json *json_file(App *a, const char *path)
{
    char *s = read_file(a, path, MAX_DATA);
    if (!s)
        return NULL;
    Json *j = json_parse(a, s);
    free(s);
    return j;
}

static int path_set(App *a, char *dest, const char *base, const char *tail)
{
    if (!base || *base != '/')
        return fail(a, "Home/XDG paths must be absolute");
    if (snprintf(dest, PATH_MAX, "%s/%s", base, tail) >= PATH_MAX)
        return fail(a, "Path too long");
    return 0;
}

int app_open(App *a)
{
    memset(a, 0, sizeof *a);
    umask(0077);
    const char *root = getenv("YT_INBOX_HOME"), *home = getenv("HOME");
    if (!home)
        return fail(a, "HOME is not set");
    if (root && *root) {
        char absolute[PATH_MAX];
        if (*root != '/') {
            if (!getcwd(absolute, sizeof absolute))
                return fail(a, "Cannot find working directory");
            char *joined = strf("%s/%s", absolute, root);
            if (strlen(joined) >= PATH_MAX) {
                free(joined);
                return fail(a, "Path too long");
            }
            strcpy(absolute, joined);
            free(joined);
            root = absolute;
        }
        if (path_set(a, a->config, root, "config") || path_set(a, a->data, root, "data") ||
            path_set(a, a->state, root, "state"))
            return -1;
    } else {
        const char *keys[] = {"XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"};
        const char *suffix[] = {".config", ".local/share", ".local/state"};
        char *dest[] = {a->config, a->data, a->state};
        for (int i = 0; i < 3; i++) {
            const char *env = getenv(keys[i]);
            char *base = env && *env ? strdup(env) : strf("%s/%s", home, suffix[i]);
            int rc = path_set(a, dest[i], base, "yt-inbox");
            free(base);
            if (rc)
                return -1;
        }
    }
    ssize_t n = readlink("/proc/self/exe", a->executable, sizeof a->executable - 1);
    if (n < 0 || n >= (ssize_t)sizeof a->executable - 1)
        return fail(a, "Cannot locate executable");
    a->executable[n] = 0;
    strcpy(a->assets, a->executable);
    *strrchr(a->assets, '/') = 0;
    if (mkdirs(a, a->config) || mkdirs(a, a->data) || mkdirs(a, a->state))
        return -1;
    char *file = strf("%s/channels.txt", a->config);
    const char *initial = "# One YouTube channel URL or @handle per line.\n";
    int rc = 0;
    if (access(file, F_OK))
        rc = atomic_write(a, file, initial, strlen(initial), 0600);
    free(file);
    if (rc)
        return -1;
    file = strf("%s/settings.json", a->config);
    const char *defaults = "{\"daily_time\":\"19:00\",\"player\":\"mpv\",\"hide_shorts\":true}\n";
    if (access(file, F_OK))
        rc = atomic_write(a, file, defaults, strlen(defaults), 0600);
    free(file);
    if (rc || settings_load(a))
        return -1;
    file = strf("%s/inbox.sqlite3", a->data);
    rc = sqlite3_open(file, &a->db);
    free(file);
    if (rc)
        return fail(a, "Cannot open database: %s", sqlite3_errmsg(a->db));
    sqlite3_busy_timeout(a->db, 20000);
#include "build/schema.h"
    if (db_exec(a, "PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;") ||
        db_exec(a, "%s", schema_sql))
        return -1;
    return 0;
}

void app_close(App *a)
{
    json_object_put(a->settings);
    if (a->db)
        sqlite3_close(a->db);
}

int settings_load(App *a)
{
    char *path = strf("%s/settings.json", a->config);
    Json *j = json_file(a, path);
    free(path);
    if (!j)
        return -1;
    int hour = -1, minute = -1;
    char tail;
    const char *clock = jstr(j, "daily_time"), *player = jstr(j, "player");
    bool valid = strlen(clock) == 5 && sscanf(clock, "%2d:%2d%c", &hour, &minute, &tail) == 2 &&
                 hour >= 0 && hour < 24 && minute >= 0 && minute < 60;
    valid = valid && (!strcmp(player, "mpv") || !strcmp(player, "browser"));
    const char *keys[] = {"hide_shorts", "terminal_only"};
    for (int i = 0; i < 2; i++)
        if (jget(j, keys[i]) && !json_object_is_type(jget(j, keys[i]), json_type_boolean))
            valid = false;
    Json *h = jget(j, "download_height");
    if (h && (!json_object_is_type(h, json_type_int) ||
              (json_object_get_int(h) != 480 && json_object_get_int(h) != 720 &&
               json_object_get_int(h) != 1080)))
        valid = false;
    const char *mode = jstr(j, "playback_mode");
    if (jget(j, "playback_mode") && strcmp(mode, "auto") && strcmp(mode, "low") &&
        strcmp(mode, "balanced") && strcmp(mode, "high") && strcmp(mode, "custom"))
        valid = false;
    if (!valid) {
        json_object_put(j);
        return fail(a, "Invalid settings.json");
    }
    json_object_put(a->settings);
    a->settings = j;
    return 0;
}
int settings_save(App *a)
{
    char *p = strf("%s/settings.json", a->config);
    const char *s = jtext(a->settings);
    int r = atomic_write(a, p, s, strlen(s), 0600);
    free(p);
    return r;
}
bool hide_shorts(App *a)
{
    Json *j = jget(a->settings, "hide_shorts");
    return !j || json_object_get_boolean(j);
}
const char *playback_mode(App *a)
{
    const char *s = jstr(a->settings, "playback_mode");
    return *s ? s : "auto";
}

bool available(const char *name)
{
    if (strchr(name, '/'))
        return access(name, X_OK) == 0;
    const char *p = getenv("PATH");
    if (!p)
        return false;
    char *copy = strdup(p), *save, *part;
    bool found = false;
    for (part = strtok_r(copy, ":", &save); part; part = strtok_r(NULL, ":", &save)) {
        char *path = strf("%s/%s", part, name);
        found = access(path, X_OK) == 0;
        free(path);
        if (found)
            break;
    }
    free(copy);
    return found;
}

static double monotonic(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

int process(App *a, char *const argv[], int timeout_seconds, Buffer *out, bool echo)
{
    int pipes[2];
    if (pipe2(pipes, O_CLOEXEC))
        return fail(a, "pipe: %s", strerror(errno));
    pid_t pid = fork();
    if (pid < 0) {
        close(pipes[0]);
        close(pipes[1]);
        return fail(a, "fork: %s", strerror(errno));
    }
    if (!pid) {
        setsid();
        signal(SIGINT, SIG_DFL);
        signal(SIGTERM, SIG_DFL);
        int null = open("/dev/null", O_RDWR);
        dup2(null, STDIN_FILENO);
        dup2(pipes[1], STDOUT_FILENO);
        /* Metadata stderr is not JSON and can contain signed stream URLs. */
        dup2(echo ? pipes[1] : null, STDERR_FILENO);
        close(null);
        close(pipes[0]);
        close(pipes[1]);
        execvp(argv[0], argv);
        _exit(127);
    }
    close(pipes[1]);
    fcntl(pipes[0], F_SETFL, O_NONBLOCK);
    double start = monotonic(), stopped = 0;
    int status = 0, result = 0;
    bool done = false, eof = false;
    Buffer line = {0};
    while (!done || !eof) {
        if (!stopped &&
            (interrupted || (timeout_seconds && monotonic() - start > timeout_seconds))) {
            kill(-pid, SIGTERM);
            stopped = monotonic();
            result = fail(a, interrupted ? "Cancelled" : "Command timed out");
        }
        if (stopped && monotonic() - stopped > 2) {
            kill(-pid, SIGKILL);
            if (done)
                break;
        }
        struct pollfd p = {.fd = eof ? -1 : pipes[0], .events = POLLIN};
        poll(&p, 1, 100);
        char chunk[8192];
        ssize_t n;
        int drained = 0;
        while ((n = read(pipes[0], chunk, sizeof chunk)) > 0) {
            if (out->len + (size_t)n > MAX_DATA) {
                if (echo) {
                    buffer_free(out);
                } else {
                    result = fail(a, "Command output exceeds size limit");
                    kill(-pid, SIGKILL);
                    stopped = monotonic();
                    break;
                }
            }
            buffer_add(out, chunk, (size_t)n);
            if (echo)
                for (ssize_t i = 0; i < n; i++) {
                    if (chunk[i] == '\n') {
                        if (line.data)
                            print_plain(line.data);
                        buffer_free(&line);
                    } else if (line.len < 8192)
                        buffer_add(&line, chunk + i, 1);
                }
            if (++drained >= 4)
                break;
        }
        if (n == 0)
            eof = true;
        if (!done) {
            pid_t w = waitpid(pid, &status, WNOHANG);
            if (w == pid)
                done = true;
        }
    }
    close(pipes[0]);
    buffer_free(&line);
    if (stopped)
        kill(-pid, SIGKILL); /* Also reap descendants which closed stdout. */
    if (!done)
        while (waitpid(pid, &status, 0) < 0 && errno == EINTR) {
        }
    if (!out->data)
        buffer_add(out, "", 0);
    if (result)
        return -1;
    if (!WIFEXITED(status) || WEXITSTATUS(status))
        return fail(a, "%s failed (exit %d). Check the tool installation and retry.", argv[0],
                    WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status));
    return 0;
}

int foreground(App *a, char *const argv[])
{
    struct termios saved;
    bool terminal = tcgetattr(STDIN_FILENO, &saved) == 0;
    pid_t pid = fork();
    if (pid < 0)
        return fail(a, "fork failed");
    if (!pid) {
        signal(SIGINT, SIG_DFL);
        signal(SIGTERM, SIG_DFL);
        execvp(argv[0], argv);
        _exit(127);
    }
    int status = 0;
    double stopped = 0;
    for (;;) {
        pid_t done = waitpid(pid, &status, WNOHANG);
        if (done == pid)
            break;
        if (done < 0 && errno != EINTR) {
            if (terminal)
                tcsetattr(STDIN_FILENO, TCSANOW, &saved);
            return fail(a, "waitpid failed");
        }
        if (interrupted && !stopped) {
            kill(pid, SIGTERM);
            stopped = monotonic();
        }
        if (stopped && monotonic() - stopped > 2)
            kill(pid, SIGKILL);
        struct timespec pause = {0, 100000000};
        nanosleep(&pause, NULL);
    }
    if (terminal && tcsetattr(STDIN_FILENO, TCSANOW, &saved))
        return fail(a, "Could not restore terminal modes");
    if (!WIFEXITED(status) || WEXITSTATUS(status))
        return fail(a, "%s exited unsuccessfully", argv[0]);
    return 0;
}

typedef struct {
    Buffer *buffer;
    size_t limit;
} Response;
static size_t receive(void *data, size_t size, size_t count, void *opaque)
{
    Response *r = opaque;
    if (size && count > SIZE_MAX / size)
        return 0;
    size_t n = size * count;
    if (n > r->limit - r->buffer->len)
        return 0;
    buffer_add(r->buffer, data, n);
    return n;
}
static int progress(void *unused, curl_off_t dlt, curl_off_t dln, curl_off_t ult, curl_off_t uln)
{
    (void)unused;
    (void)dlt;
    (void)dln;
    (void)ult;
    (void)uln;
    return interrupted != 0;
}

int http(App *a, const char *url, const char *token, const char *body, size_t limit, Buffer *out,
         long *status)
{
    CURL *c = curl_easy_init();
    if (!c)
        return fail(a, "Cannot initialise HTTPS");
    Response response = {out, limit};
    struct curl_slist *headers = NULL;
    headers = curl_slist_append(headers,
                                "Accept: application/vnd.github+json, application/atom+xml, */*");
    if (token) {
        char *auth = strf("Authorization: Bearer %s", token);
        headers = curl_slist_append(headers, auth);
        free(auth);
        headers = curl_slist_append(headers, "X-GitHub-Api-Version: 2026-03-10");
    }
    if (body)
        headers = curl_slist_append(headers, "Content-Type: application/json");
    curl_easy_setopt(c, CURLOPT_URL, url);
    curl_easy_setopt(c, CURLOPT_PROTOCOLS_STR, "https");
    curl_easy_setopt(c, CURLOPT_REDIR_PROTOCOLS_STR, "https");
    curl_easy_setopt(c, CURLOPT_FOLLOWLOCATION, token ? 0L : 1L);
    curl_easy_setopt(c, CURLOPT_MAXREDIRS, 3L);
    curl_easy_setopt(c, CURLOPT_CONNECTTIMEOUT, 10L);
    curl_easy_setopt(c, CURLOPT_TIMEOUT, 20L);
    curl_easy_setopt(c, CURLOPT_USERAGENT, "yt-inbox/" VERSION);
    curl_easy_setopt(c, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(c, CURLOPT_WRITEFUNCTION, receive);
    curl_easy_setopt(c, CURLOPT_WRITEDATA, &response);
    curl_easy_setopt(c, CURLOPT_NOPROGRESS, 0L);
    curl_easy_setopt(c, CURLOPT_XFERINFOFUNCTION, progress);
    if (body) {
        curl_easy_setopt(c, CURLOPT_CUSTOMREQUEST, "PUT");
        curl_easy_setopt(c, CURLOPT_POSTFIELDS, body);
    }
    CURLcode rc = curl_easy_perform(c);
    curl_easy_getinfo(c, CURLINFO_RESPONSE_CODE, status);
    curl_slist_free_all(headers);
    curl_easy_cleanup(c);
    if (rc)
        return fail(a, "HTTPS request failed: %s", curl_easy_strerror(rc));
    if (!out->data)
        buffer_add(out, "", 0);
    if (memchr(out->data, 0, out->len))
        return fail(a, "Unexpected NUL in HTTP response");
    return 0;
}

Json *ytdlp(App *a, const char *url, const char *const options[], int timeout_seconds)
{
    char *argv[64] = {"yt-dlp",
                      "--ignore-config",
                      "--dump-single-json",
                      "--skip-download",
                      "--no-warnings",
                      "--socket-timeout",
                      "10",
                      "--retries",
                      "0",
                      "--extractor-retries",
                      "0"};
    size_t n = 11;
    for (size_t i = 0; options && options[i]; i++) {
        if (n >= 60)
            return NULL;
        argv[n++] = (char *)options[i];
    }
    argv[n++] = "--";
    argv[n++] = (char *)url;
    argv[n] = NULL;
    Buffer b = {0};
    Json *j = NULL;
    if (!process(a, argv, timeout_seconds, &b, false)) {
        if (memchr(b.data, 0, b.len))
            fail(a, "Unexpected NUL in yt-dlp metadata");
        else
            j = json_parse(a, b.data);
    }
    buffer_free(&b);
    if (j && !json_object_is_type(j, json_type_object)) {
        json_object_put(j);
        j = NULL;
        fail(a, "Invalid yt-dlp metadata");
    }
    return j;
}
