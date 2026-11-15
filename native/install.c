/* User-local installation. No root, package changes or shell configuration. */
#include "inbox.h"
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static char *unit_quote(const char *value)
{
    Buffer b = {0};
    buffer_add(&b, "\"", 1);
    for (; *value; value++) {
        if (*value == '\\' || *value == '"')
            buffer_add(&b, "\\", 1);
        buffer_add(&b, value, 1);
        if (*value == '%' || *value == '$')
            buffer_add(&b, value, 1);
    }
    buffer_add(&b, "\"", 1);
    return b.data;
}
static char *shell_quote(const char *value)
{
    Buffer b = {0};
    buffer_add(&b, "'", 1);
    for (; *value; value++)
        if (*value == '\'')
            buffer_add(&b, "'\"'\"'", 5);
        else
            buffer_add(&b, value, 1);
    buffer_add(&b, "'", 1);
    return b.data;
}
static void environment_line(Buffer *out, const char *key, const char *value)
{
    char *prefix = strf("Environment=\"%s=", key);
    buffer_add(out, prefix, strlen(prefix));
    free(prefix);
    for (; *value; value++) {
        if (*value == '\\' || *value == '"')
            buffer_add(out, "\\", 1);
        buffer_add(out, value, 1);
        if (*value == '%')
            buffer_add(out, "%", 1);
    }
    buffer_add(out, "\"\n", 2);
}
static int write_named(App *a, const char *directory, const char *name, const char *text, int mode)
{
    char *path = strf("%s/%s", directory, name);
    int rc = atomic_write(a, path, text, strlen(text), mode);
    free(path);
    return rc;
}
static int copy_binary(App *a, const char *target)
{
    int fd = open(a->executable, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return fail(a, "Cannot read executable");
    Buffer bytes = {0};
    char block[32768];
    ssize_t n;
    int rc = 0;
    while ((n = read(fd, block, sizeof block)) != 0) {
        if (n < 0 && errno == EINTR)
            continue;
        if (n < 0 || bytes.len + (size_t)n > 100000000) {
            rc = fail(a, "Cannot copy executable");
            break;
        }
        buffer_add(&bytes, block, (size_t)n);
    }
    close(fd);
    if (!rc)
        rc = atomic_write(a, target, bytes.data, bytes.len, 0755);
    buffer_free(&bytes);
    return rc;
}

int install_native(App *a, bool enable, bool primary)
{
    if (geteuid() == 0 && !getenv("YT_INBOX_HOME"))
        return fail(a, "Install as your normal user, without sudo");
    char *directory = strf("%s/native", a->data), *binary = strf("%s/yt-inbox-c", directory),
         *script = strf("%s/playback.lua", a->assets);
    char *lua = read_file(a, script, 1000000);
    free(script);
    int rc = -1;
    char *root = NULL, *bin = NULL, *units = NULL, *quoted = NULL, *launcher = NULL, *exec = NULL;
    Buffer environment = {0};
    if (!lua || mkdirs(a, directory) || copy_binary(a, binary) ||
        write_named(a, directory, "playback.lua", lua, 0600))
        goto done;
    if (getenv("YT_INBOX_HOME")) {
        root = strdup(a->config);
        *strrchr(root, '/') = 0;
        bin = strf("%s/bin", root);
        units = strf("%s/systemd/user", root);
    } else {
        bin = strf("%s/.local/bin", getenv("HOME"));
        root = strdup(a->config);
        *strrchr(root, '/') = 0;
        units = strf("%s/systemd/user", root);
    }
    if (mkdirs(a, bin))
        goto done;
    quoted = shell_quote(binary);
    launcher = strf("#!/bin/sh\nexec %s \"$@\"\n", quoted);
    if (write_named(a, bin, "yt-inbox-c", launcher, 0755))
        goto done;
    if (!primary) {
        printf("Installed %s/yt-inbox-c. Existing launcher and timers retained.\n", bin);
        rc = 0;
        goto done;
    }
    if (write_named(a, bin, "yt-inbox", launcher, 0755) || mkdirs(a, units))
        goto done;
    exec = unit_quote(binary);
    if (getenv("YT_INBOX_HOME"))
        environment_line(&environment, "YT_INBOX_HOME", root);
    else {
        const char *names[] = {"XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"};
        const char *paths[] = {a->config, a->data, a->state};
        for (int i = 0; i < 3; i++) {
            char *parent = strdup(paths[i]);
            *strrchr(parent, '/') = 0;
            environment_line(&environment, names[i], parent);
            free(parent);
        }
    }
    bool notifications =
        !json_object_get_boolean(jget(a->settings, "terminal_only")) && available("notify-send");
    char *service = strf(
        "[Unit]\nDescription=Fetch YouTube uploads "
        "(C)\nStartLimitIntervalSec=0\n\n[Service]\nType=oneshot\n%sExecStart=%s check "
        "--scheduled%s\nRestart=on-failure\nRestartSec=15min\nTimeoutStartSec=30min\nUMask=0077\n",
        environment.data, exec, notifications ? "" : " --no-notify");
    rc = write_named(a, units, "yt-inbox-check.service", service, 0600);
    free(service);
    if (rc)
        goto done;
    char *timer = strf("[Unit]\nDescription=Daily YouTube inbox\n\n[Timer]\nOnCalendar=*-*-* "
                       "%s:00\nOnStartupSec=90s\nPersistent=true\nAccuracySec=1min\nUnit=yt-inbox-"
                       "check.service\n\n[Install]\nWantedBy=timers.target\n",
                       jstr(a->settings, "daily_time"));
    rc = write_named(a, units, "yt-inbox-check.timer", timer, 0600);
    free(timer);
    if (rc)
        goto done;
    service = strf("[Unit]\nDescription=Synchronize YouTube Inbox "
                   "(C)\n\n[Service]\nType=oneshot\n%sExecStart=%s sync "
                   "--quiet\nTimeoutStartSec=4min\nUMask=0077\n",
                   environment.data, exec);
    rc = write_named(a, units, "yt-inbox-sync.service", service, 0600);
    free(service);
    if (rc)
        goto done;
    timer = "[Unit]\nDescription=Synchronize YouTube Inbox every five "
            "minutes\n\n[Timer]\nOnStartupSec=45s\nOnUnitInactiveSec=5min\nAccuracySec=15s\nUnit="
            "yt-inbox-sync.service\n\n[Install]\nWantedBy=timers.target\n";
    rc = write_named(a, units, "yt-inbox-sync.timer", timer, 0600);
    if (rc)
        goto done;
    service = strf("[Unit]\nDescription=YouTube Inbox notification "
                   "(C)\nAfter=graphical-session.target\nPartOf=graphical-session.target\n\n["
                   "Service]\nType=oneshot\n%sExecStart=%s notify\nUMask=0077\n",
                   environment.data, exec);
    rc = write_named(a, units, "yt-inbox-notify.service", service, 0600);
    free(service);
    if (rc)
        goto done;
    if (enable) {
        Buffer out = {0};
        char *reload[] = {"systemctl", "--user", "daemon-reload", NULL};
        rc = process(a, reload, 10, &out, false);
        buffer_free(&out);
        if (rc)
            goto done;
        char *start[] = {"systemctl", "--user", "enable", "--now", "yt-inbox-check.timer", NULL};
        rc = process(a, start, 10, &out, false);
        buffer_free(&out);
        if (rc)
            goto done;
        if (sync_configured(a)) {
            char *sync[] = {"systemctl", "--user", "enable", "--now", "yt-inbox-sync.timer", NULL};
            rc = process(a, sync, 10, &out, false);
            buffer_free(&out);
            if (rc)
                goto done;
        }
    }
    printf("Installed %s/yt-inbox (C). Daily checks use %s local time; sync uses the existing "
           "private repository.\n",
           bin, jstr(a->settings, "daily_time"));
    rc = 0;
done:
    free(directory);
    free(binary);
    free(lua);
    free(root);
    free(bin);
    free(units);
    free(quoted);
    free(launcher);
    free(exec);
    buffer_free(&environment);
    return rc;
}

int notify_digest(App *a, bool test)
{
    int lock = lock_file(a, "notification.lock");
    if (lock < 0)
        return -1;
    time_t now = time(NULL);
    struct tm local;
    localtime_r(&now, &local);
    char day[32];
    strftime(day, sizeof day, "%Y-%m-%d", &local);
    char *last = meta_get(a, "last_digest_day");
    Json *rows = list_rows(a, "pending", NULL);
    int rc = -1;
    if (!last || !rows)
        goto done;
    if (!test && !strcmp(day, last)) {
        rc = 0;
        goto done;
    }
    Buffer body = {0};
    size_t count = 0;
    for (size_t i = 0; i < json_object_array_length(rows); i++) {
        Json *r = json_object_array_get_idx(rows, i);
        if (json_object_get_boolean(jget(r, "notified")))
            continue;
        if (count++ < 3) {
            char *line = strf("%s: %s\n", jstr(r, "channel"), jstr(r, "title"));
            buffer_add(&body, line, strlen(line));
            free(line);
        }
    }
    if (!count && !test) {
        rc = 0;
        buffer_free(&body);
        goto done;
    }
    if (!available("notify-send")) {
        buffer_free(&body);
        fail(a, "Install libnotify for desktop notifications, or use --no-notify");
        goto done;
    }
    /* Escape notification markup; metadata remains plain text. */
    Buffer escaped = {0};
    const char *text = test ? "Run yt-inbox to open your inbox." : body.data;
    char *plain = clean_text(text ? text : "");
    for (char *p = plain; *p; p++) {
        const char *s = *p == '&' ? "&amp;" : *p == '<' ? "&lt;" : *p == '>' ? "&gt;" : NULL;
        if (s)
            buffer_add(&escaped, s, strlen(s));
        else
            buffer_add(&escaped, p, 1);
    }
    free(plain);
    char *summary =
        test ? strdup("YouTube Inbox is ready")
             : strf("%zu new videos | %zu unwatched", count, json_object_array_length(rows));
    char *args[] = {"notify-send",
                    "--app-name=YouTube Inbox",
                    "--icon=video-x-generic",
                    "--print-id",
                    "--",
                    summary,
                    escaped.data,
                    NULL};
    Buffer out = {0};
    rc = process(a, args, 15, &out, false);
    if (!rc && (!out.len || strtol(out.data, NULL, 10) < 1))
        rc = fail(a, "Notification server did not acknowledge the notification");
    if (!rc && !test) {
        rc = db_begin(a);
        for (size_t i = 0; !rc && i < json_object_array_length(rows); i++)
            rc = db_exec(a, "UPDATE videos SET notified=1 WHERE id=%Q",
                         jstr(json_object_array_get_idx(rows, i), "id"));
        if (!rc)
            rc = meta_set(a, "last_digest_day", day);
        if (!rc)
            rc = db_commit(a);
        if (rc)
            db_rollback(a);
    }
    free(summary);
    buffer_free(&out);
    buffer_free(&body);
    buffer_free(&escaped);
done:
    free(last);
    json_object_put(rows);
    close(lock);
    return rc;
}
