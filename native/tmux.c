/* A local tmux pane is a PTY, not the Linux VT required by DRM.
 * Detach only its sole verified local client; that client execs this program
 * as a helper, plays on its controlling VT and then execs tmux attach again. */
#include "inbox.h"
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

static bool vt(const char *s)
{
    if (strncmp(s, "/dev/tty", 8) || s[8] < '1' || s[8] > '9')
        return false;
    for (s += 9; *s; s++)
        if (!isdigit((unsigned char)*s))
            return false;
    return true;
}
static bool numbered(const char *s, char prefix)
{
    if (*s++ != prefix || !*s)
        return false;
    for (; *s; s++)
        if (!isdigit((unsigned char)*s))
            return false;
    return true;
}
static char *query(App *a, char **args)
{
    Buffer out = {0};
    if (process(a, args, 3, &out, false)) {
        buffer_free(&out);
        return NULL;
    }
    while (out.len && (out.data[out.len - 1] == '\n' || out.data[out.len - 1] == '\r'))
        out.data[--out.len] = 0;
    return out.data;
}
static Json *client(App *a)
{
    const char *pane = getenv("TMUX_PANE");
    if (!getenv("TMUX") || !pane || !numbered(pane, '%') || !available("tmux"))
        return NULL;
    char *info_args[] = {"tmux", "display-message", "-p",
                         "-t",   (char *)pane,      "#{session_id}\n#{pane_tty}\n#{socket_path}",
                         NULL};
    char *info = query(a, info_args);
    if (!info)
        return NULL;
    char *save, *session = strtok_r(info, "\n", &save), *terminal = strtok_r(NULL, "\n", &save),
                *socket = strtok_r(NULL, "\n", &save);
    const char *own = ttyname(STDIN_FILENO);
    if (!session || !terminal || !socket || strtok_r(NULL, "\n", &save) ||
        !numbered(session, '$') || !own || strcmp(own, terminal)) {
        fail(a, "The tmux pane does not match this terminal");
        free(info);
        return NULL;
    }
    char *list_args[] = {
        "tmux", "-S",
        socket, "list-clients",
        "-t",   session,
        "-F",   "#{client_tty}\t#{client_pid}\t#{client_readonly}\t#{client_control_mode}",
        NULL};
    char *listing = query(a, list_args);
    if (!listing) {
        free(info);
        return NULL;
    }
    char *line = listing;
    int clients = 0;
    bool found = false;
    for (const char *p = listing; *p; p++)
        if (p == listing || p[-1] == '\n') {
            clients++;
            if (!strncmp(p, "/dev/tty", 8))
                found = true;
        }
    if (!found) {
        free(listing);
        free(info);
        return NULL;
    }
    if (clients != 1) {
        fail(a, "Keep only one local TTY client attached before DRM playback");
        free(listing);
        free(info);
        return NULL;
    }
    char *parts[4];
    for (int i = 0; i < 4; i++)
        parts[i] = strtok_r(i ? NULL : line, "\t", &save);
    if (!parts[0] || !vt(parts[0]) || !parts[1] || !parts[2] || !parts[3] ||
        strcmp(parts[2], "0") || strcmp(parts[3], "0")) {
        fail(a, "DRM needs a normal writable tmux client on a local VT");
        free(listing);
        free(info);
        return NULL;
    }
    char *end;
    long pid = strtol(parts[1], &end, 10);
    if (*end || pid <= 1 || pid > INT_MAX) {
        fail(a, "Invalid tmux client PID");
        free(listing);
        free(info);
        return NULL;
    }
    char *destroy_args[] = {"tmux", "-S", socket,  "show-options",       "-A",
                            "-v",   "-t", session, "destroy-unattached", NULL};
    char *exit_args[] = {"tmux", "-S", socket, "show-options", "-s", "-v", "exit-unattached", NULL};
    char *destroy = query(a, destroy_args), *exit_option = query(a, exit_args);
    bool safe = destroy && exit_option && !strcmp(destroy, "off") && !strcmp(exit_option, "off");
    free(destroy);
    free(exit_option);
    if (!safe) {
        fail(a, "tmux would destroy the detached session; play directly on the TTY");
        free(listing);
        free(info);
        return NULL;
    }
    Json *j = json_object_new_object();
    jsetstr(j, "session", session);
    jsetstr(j, "socket", socket);
    jsetstr(j, "tty", parts[0]);
    json_object_object_add(j, "pid", json_object_new_int64(pid));
    free(listing);
    free(info);
    return j;
}

static char *shell_quote(const char *s)
{
    Buffer b = {0};
    buffer_add(&b, "'", 1);
    for (; *s; s++)
        if (*s == '\'')
            buffer_add(&b, "'\"'\"'", 5);
        else
            buffer_add(&b, s, 1);
    buffer_add(&b, "'", 1);
    return b.data;
}
extern char **environ;
static bool player_env(const char *key)
{
    const char *keys[] = {"PATH",
                          "MPV_HOME",
                          "XDG_CONFIG_HOME",
                          "XDG_DATA_HOME",
                          "XDG_STATE_HOME",
                          "XDG_CACHE_HOME",
                          "XDG_RUNTIME_DIR",
                          "DBUS_SESSION_BUS_ADDRESS",
                          "PULSE_SERVER",
                          "PIPEWIRE_REMOTE",
                          "LANG",
                          "YT_INBOX_HOME",
                          NULL};
    if (!strncmp(key, "LC_", 3))
        return true;
    for (int i = 0; keys[i]; i++)
        if (!strcmp(key, keys[i]))
            return true;
    return false;
}

int tmux_play(App *a, char **argv)
{
    a->error[0] = 0;
    Json *c = client(a);
    if (!c)
        return *a->error ? -1 : 1;
    Json *again = client(a);
    bool same = again && json_object_equal(c, again);
    json_object_put(again);
    if (!same) {
        json_object_put(c);
        return fail(a, "tmux client changed; select the video again");
    }
    char *folder = strf("%s/tmux-player-XXXXXX", a->state);
    if (!mkdtemp(folder)) {
        free(folder);
        json_object_put(c);
        return fail(a, "Cannot create tmux player job");
    }
    Json *request = json_object_new_object(), *command = json_object_new_array(),
         *env = json_object_new_object();
    for (int i = 0; argv[i]; i++) {
        if (!strcmp(argv[i], "--")) {
            json_object_array_add(command, json_object_new_string("--vo=gpu"));
            json_object_array_add(command, json_object_new_string("--gpu-context=drm"));
            json_object_array_add(command, json_object_new_string("--gpu-api=opengl"));
        }
        json_object_array_add(command, json_object_new_string(argv[i]));
    }
    for (char **p = environ; *p; p++) {
        char *key = strdup(*p), *eq = strchr(key, '=');
        if (eq) {
            *eq = 0;
            if (player_env(key))
                jsetstr(env, key, eq + 1);
        }
        free(key);
    }
    json_object_object_add(request, "command", command);
    json_object_object_add(request, "client", json_object_get(c));
    json_object_object_add(request, "env", env);
    char *path = strf("%s/request.json", folder), *started = strf("%s/started", folder),
         *result = strf("%s/result.json", folder), *cancel = strf("%s/cancel", folder);
    int rc = atomic_write(a, path, jtext(request), strlen(jtext(request)), 0600);
    json_object_put(request);
    if (!rc) {
        char *program = shell_quote(a->executable), *quoted = shell_quote(path),
             *helper = strf("exec %s _tmux-job %s", program, quoted);
        free(program);
        free(quoted);
        char *args[] = {"tmux",
                        "-S",
                        (char *)jstr(c, "socket"),
                        "detach-client",
                        "-t",
                        (char *)jstr(c, "tty"),
                        "-E",
                        helper,
                        NULL};
        char *answer = query(a, args);
        rc = answer ? 0 : -1;
        free(answer);
        free(helper);
    }
    if (!rc) {
        time_t deadline = time(NULL) + 10, cancelled = 0;
        pid_t pid = (pid_t)json_object_get_int64(jget(c, "pid"));
        while (access(result, F_OK)) {
            if (interrupted && !cancelled) {
                atomic_write(a, cancel, "", 0, 0600);
                cancelled = time(NULL);
            }
            if (kill(pid, 0) && errno == ESRCH) {
                rc = fail(a, "tmux helper exited; reattach with tmux attach");
                break;
            }
            if ((access(started, F_OK) && time(NULL) > deadline) ||
                (cancelled && time(NULL) > cancelled + 5)) {
                rc = fail(a, "tmux handoff did not finish; reattach with tmux attach");
                break;
            }
            struct timespec pause = {0, 100000000};
            nanosleep(&pause, NULL);
        }
        if (!rc) {
            Json *reply = json_file(a, result);
            if (!reply || json_object_get_int(jget(reply, "code")))
                rc = fail(a, "mpv exited unsuccessfully in tmux; history unchanged");
            json_object_put(reply);
        }
    }
    /* Leave timed-out jobs for a possibly still running helper; deleting its
     * request/result directory would strand its completion report. */
    if (!access(result, F_OK) || !rc) {
        unlink(path);
        unlink(started);
        unlink(result);
        unlink(cancel);
        char *log = strf("%s/player.log", folder);
        unlink(log);
        free(log);
        rmdir(folder);
    }
    free(path);
    free(started);
    free(result);
    free(cancel);
    free(folder);
    json_object_put(c);
    return rc;
}

int tmux_job(App *a, const char *path)
{
    Json *request = json_file(a, path);
    if (!request)
        return -1;
    Json *c = jget(request, "client"), *command = jget(request, "command"),
         *env = jget(request, "env");
    if (!json_object_is_type(c, json_type_object) ||
        !json_object_is_type(command, json_type_array) || !json_object_array_length(command) ||
        json_object_array_length(command) > 64 || !vt(jstr(c, "tty")) ||
        !numbered(jstr(c, "session"), '$') || !*jstr(c, "socket")) {
        json_object_put(request);
        return fail(a, "Invalid tmux job");
    }
    char *folder = strdup(path), *slash = strrchr(folder, '/');
    if (!slash) {
        free(folder);
        json_object_put(request);
        return fail(a, "Invalid job path");
    }
    *slash = 0;
    char *started = strf("%s/started", folder), *result = strf("%s/result.json", folder),
         *cancel = strf("%s/cancel", folder), *log = strf("%s/player.log", folder);
    int code = 1;
    struct termios saved;
    bool modes = false;
    pid_t child = -1;
    if (atomic_write(a, started, "started", 7, 0600))
        goto finish;
    const char *tty = ttyname(STDIN_FILENO);
    if (!tty || strcmp(tty, jstr(c, "tty")))
        goto finish;
    modes = tcgetattr(STDIN_FILENO, &saved) == 0;
    if (!modes)
        goto finish;
    if (json_object_is_type(env, json_type_object)) {
        json_object_object_foreach(env, key, value)
        {
            if (player_env(key) && json_object_is_type(value, json_type_string))
                setenv(key, json_object_get_string(value), 1);
        }
    }
    size_t n = json_object_array_length(command);
    char **argv = xmalloc((n + 1) * sizeof *argv);
    for (size_t i = 0; i < n; i++) {
        Json *v = json_object_array_get_idx(command, i);
        if (!json_object_is_type(v, json_type_string)) {
            free(argv);
            goto finish;
        }
        argv[i] = (char *)json_object_get_string(v);
    }
    argv[n] = NULL;
    puts("YouTube Inbox: Space pauses; arrows seek; q returns to tmux.");
    fflush(stdout);
    int fd = open(log, O_CREAT | O_TRUNC | O_WRONLY | O_CLOEXEC, 0600);
    if (fd < 0) {
        free(argv);
        goto finish;
    }
    child = fork();
    if (!child) {
        signal(SIGINT, SIG_DFL);
        signal(SIGTERM, SIG_DFL);
        dup2(fd, STDOUT_FILENO);
        dup2(fd, STDERR_FILENO);
        close(fd);
        execvp(argv[0], argv);
        _exit(127);
    }
    close(fd);
    free(argv);
    if (child < 0)
        goto finish;
    int status = 0;
    time_t stopping = 0;
    while (waitpid(child, &status, WNOHANG) != child) {
        if ((interrupted || !access(cancel, F_OK)) && !stopping) {
            kill(child, SIGTERM);
            stopping = time(NULL);
        }
        if (stopping && time(NULL) > stopping + 2)
            kill(child, SIGKILL);
        struct timespec pause = {0, 100000000};
        nanosleep(&pause, NULL);
    }
    code = stopping ? 130 : WIFEXITED(status) ? WEXITSTATUS(status) : 1;
finish:
    if (modes && tcsetattr(STDIN_FILENO, TCSANOW, &saved))
        code = 1;
    Json *response = json_object_new_object();
    json_object_object_add(response, "code", json_object_new_int(code));
    jsetstr(response, "error", code ? "Playback failed; history unchanged" : "");
    atomic_write(a, result, jtext(response), strlen(jtext(response)), 0600);
    json_object_put(response);
    char *socket = strdup(jstr(c, "socket")), *session = strdup(jstr(c, "session"));
    free(folder);
    free(started);
    free(result);
    free(cancel);
    free(log);
    json_object_put(request);
    unsetenv("TMUX");
    unsetenv("TMUX_PANE");
    char *attach[] = {"tmux", "-S", socket, "attach-session", "-E", "-t", session, NULL};
    execvp(attach[0], attach);
    free(socket);
    free(session);
    return fail(a, "Could not reattach tmux");
}
