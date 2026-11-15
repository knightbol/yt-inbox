#include "inbox.h"
#include <curl/curl.h>
#include <locale.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifndef YTI_NO_MAIN
static void signal_handler(int signo)
{
    interrupted = signo;
}
#endif
static bool option(int argc, char **argv, const char *name)
{
    for (int i = 0; i < argc; i++)
        if (!strcmp(argv[i], name))
            return true;
    return false;
}
static void help(void)
{
    puts("YouTube Inbox " VERSION " — native C CLI/TUI\n"
         "  tui [--video-output auto|drm|window]     terminal interface (default)\n"
         "  check [--scheduled] [--no-notify]       fetch new uploads\n"
         "  list [--json]                          pending videos\n"
         "  channels [--json]                      followed channels\n"
         "  browse CHANNEL_ID [--older|--refresh] [--json]\n"
         "  watched|skip|restore VIDEO_ID...        explicit history changes\n"
         "  undo                                  undo last status change\n"
         "  download|play VIDEO_ID                 save offline / play\n"
         "  remove-download VIDEO_ID --yes         remove offline files only\n"
         "  types                                 classify a bounded batch\n"
         "  edit                                  edit channels.txt\n"
         "  config [hide_shorts|playback_mode|download_height VALUE]\n"
         "  sync [setup OWNER/REPO|status|disable] [--quiet]\n"
         "  status | notify | test-notification\n"
         "  install [--primary] [--no-enable]       install for this user\n"
         "  gui                                   launch the optional existing Python GUI\n"
         "No API key needed. Watched state is always manual.");
}
static int print_rows(App *a, Json *rows, bool as_json, bool channels)
{
    if (!rows)
        return -1;
    for (size_t i = 0; i < json_object_array_length(rows); i++) {
        Json *r = json_object_array_get_idx(rows, i);
        char *url =
            strf("https://www.youtube.com/%s%s", channels ? "channel/" : "watch?v=", jstr(r, "id"));
        jsetstr(r, "url", url);
        free(url);
        if (!as_json) {
            char *line = strf("%s%s%s", channels ? jstr(r, "name") : jstr(r, "channel"),
                              channels ? "" : " — ", channels ? "" : jstr(r, "title"));
            print_plain(line);
            free(line);
            puts(jstr(r, "url"));
            putchar('\n');
        }
    }
    if (as_json)
        puts(json_object_to_json_string_ext(rows, JSON_C_TO_STRING_PRETTY |
                                                      JSON_C_TO_STRING_NOSLASHESCAPE));
    json_object_put(rows);
    (void)a;
    return 0;
}

int run_command(App *a, int argc, char **argv)
{
    const char *cmd = argc ? argv[0] : "tui", *output = "auto";
    for (int i = 1; i < argc; i++)
        if (!strcmp(argv[i], "--video-output")) {
            if (i + 1 >= argc)
                return fail(a, "Missing video output");
            output = argv[++i];
        }
    if (!strcmp(cmd, "--help") || !strcmp(cmd, "help")) {
        help();
        return 0;
    }
    if (!strcmp(cmd, "--version")) {
        puts(VERSION);
        return 0;
    }
    if (!strcmp(cmd, "tui") || !strcmp(cmd, "tty")) {
        sync_kick(a);
        return tui(a, output);
    }
    if (!strcmp(cmd, "check"))
        return check_uploads(a, option(argc, argv, "--scheduled"),
                             option(argc, argv, "--scheduled") &&
                                 !option(argc, argv, "--no-notify"));
    if (!strcmp(cmd, "list"))
        return print_rows(a, list_rows(a, "pending", NULL), option(argc, argv, "--json"), false);
    if (!strcmp(cmd, "channels"))
        return print_rows(a, list_rows(a, "channels", NULL), option(argc, argv, "--json"), true);
    if (!strcmp(cmd, "browse")) {
        if (argc < 2 || !valid_channel(argv[1]))
            return fail(a, "Use browse CHANNEL_ID [--older|--refresh]");
        if ((option(argc, argv, "--older") || option(argc, argv, "--refresh")) &&
            load_catalogue(a, argv[1], option(argc, argv, "--refresh")))
            return -1;
        return print_rows(a, list_rows(a, "browse", argv[1]), option(argc, argv, "--json"), false);
    }
    if (!strcmp(cmd, "watched") || !strcmp(cmd, "skip") || !strcmp(cmd, "restore")) {
        if (argc < 2)
            return fail(a, "Specify one or more video IDs");
        return set_status(a, argc - 1, argv + 1,
                          !strcmp(cmd, "watched") ? "watched"
                          : !strcmp(cmd, "skip")  ? "skipped"
                                                  : "pending");
    }
    if (!strcmp(cmd, "undo"))
        return undo_status(a);
    if (!strcmp(cmd, "types"))
        return classify_saved(a);
    if (!strcmp(cmd, "edit"))
        return edit_channels(a);
    if (!strcmp(cmd, "play"))
        return argc < 2 ? fail(a, "Specify a video ID") : play_video(a, argv[1], output);
    if (!strcmp(cmd, "download") || !strcmp(cmd, "remove-download")) {
        if (argc < 2 || !valid_video(argv[1]))
            return fail(a, "Specify a valid video ID");
        if (!strcmp(cmd, "remove-download") && !option(argc, argv, "--yes"))
            return fail(a, "Use --yes to remove this video's offline files");
        int lock = lock_file(a, "interface.lock");
        if (lock < 0)
            return -1;
        int rc =
            !strcmp(cmd, "download") ? download_video(a, argv[1]) : remove_download(a, argv[1]);
        close(lock);
        return rc;
    }
    if (!strcmp(cmd, "sync")) {
        const char *action = argc > 1 && argv[1][0] != '-' ? argv[1] : "now";
        if (!strcmp(action, "setup"))
            return argc < 3 ? fail(a, "Use sync setup OWNER/REPO") : sync_setup(a, argv[2]);
        if (!strcmp(action, "disable"))
            return sync_disable(a);
        if (!strcmp(action, "status")) {
            sync_status(a);
            return *a->error ? -1 : 0;
        }
        if (strcmp(action, "now"))
            return fail(a, "Unknown sync action");
        return sync_now(a, option(argc, argv, "--quiet"));
    }
    if (!strcmp(cmd, "status")) {
        printf("Channels: %s/channels.txt\nDatabase: %s/inbox.sqlite3\nDaily time: %s\n", a->config,
               a->data, jstr(a->settings, "daily_time"));
        char *errors = meta_get(a, "last_errors"), *last = meta_get(a, "last_success");
        if (!errors || !last) {
            free(errors);
            free(last);
            return -1;
        }
        printf("Last complete check (Unix time): %s\n", *last ? last : "never");
        if (*errors)
            print_plain(errors);
        free(errors);
        free(last);
        sync_status(a);
        return *a->error ? -1 : 0;
    }
    if (!strcmp(cmd, "notify") || !strcmp(cmd, "test-notification"))
        return notify_digest(a, !strcmp(cmd, "test-notification"));
    if (!strcmp(cmd, "config")) {
        if (argc == 1) {
            puts(json_object_to_json_string_ext(a->settings, JSON_C_TO_STRING_PRETTY));
            return 0;
        }
        if (argc != 3)
            return fail(a, "Use config KEY VALUE");
        if (!strcmp(argv[1], "hide_shorts") &&
            (!strcmp(argv[2], "true") || !strcmp(argv[2], "false")))
            json_object_object_add(a->settings, "hide_shorts",
                                   json_object_new_boolean(!strcmp(argv[2], "true")));
        else if (!strcmp(argv[1], "download_height") &&
                 (!strcmp(argv[2], "480") || !strcmp(argv[2], "720") || !strcmp(argv[2], "1080")))
            json_object_object_add(a->settings, "download_height",
                                   json_object_new_int(atoi(argv[2])));
        else if (!strcmp(argv[1], "playback_mode") &&
                 (!strcmp(argv[2], "auto") || !strcmp(argv[2], "low") ||
                  !strcmp(argv[2], "balanced") || !strcmp(argv[2], "high") ||
                  !strcmp(argv[2], "custom")))
            jsetstr(a->settings, "playback_mode", argv[2]);
        else
            return fail(
                a, "Invalid config key/value; use hide_shorts, playback_mode or download_height");
        return settings_save(a);
    }
    if (!strcmp(cmd, "install"))
        return install_native(a, !option(argc, argv, "--no-enable"),
                              option(argc, argv, "--primary"));
    if (!strcmp(cmd, "gui")) {
        char *path = strf("%s/app/cli.py", a->data);
        if (access(path, R_OK)) {
            free(path);
            return fail(a, "The optional Python GUI is not installed; run install.sh --desktop "
                           "from the repository");
        }
        char *args[] = {"python3", path, "gui", NULL};
        int rc = foreground(a, args);
        free(path);
        return rc;
    }
    if (!strcmp(cmd, "_tmux-job"))
        return argc == 2 ? tmux_job(a, argv[1]) : fail(a, "Missing tmux job");
    return fail(a, "Unknown command. Use --help");
}

#ifndef YTI_NO_MAIN
int main(int argc, char **argv)
{
    setlocale(LC_ALL, "");
    setlocale(LC_NUMERIC, "C");
    struct sigaction action = {0};
    action.sa_handler = signal_handler;
    sigemptyset(&action.sa_mask);
    sigaction(SIGINT, &action, NULL);
    sigaction(SIGTERM, &action, NULL);
    if (argc == 2 && !strcmp(argv[1], "--version")) {
        puts(VERSION);
        return 0;
    }
    if (argc == 2 && !strcmp(argv[1], "--help")) {
        help();
        return 0;
    }
    if (curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK) {
        fputs("Cannot initialise libcurl\n", stderr);
        return 1;
    }
    App app = {0};
    int rc;
    if (argc == 3 && !strcmp(argv[1], "_tmux-job"))
        rc = tmux_job(&app, argv[2]);
    else {
        rc = app_open(&app);
        if (!rc)
            rc = run_command(&app, argc - 1, argv + 1);
    }
    if (rc) {
        char *plain = clean_text(*app.error ? app.error : "Operation failed");
        fprintf(stderr, "YouTube Inbox: %s\n", plain);
        free(plain);
    }
    app_close(&app);
    curl_global_cleanup();
    return interrupted ? 128 + interrupted : rc ? 1 : 0;
}
#endif
