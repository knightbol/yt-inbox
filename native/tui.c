/* Finite pages, explicit actions, no autoplay or recommendations. */
#include "inbox.h"
#include <curses.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <wchar.h>
#include <wctype.h>

static void put(int y, const char *text, int attr)
{
    if (y < 0 || y >= LINES)
        return;
    char *plain = clean_text(text);
    size_t length = mbstowcs(NULL, plain, 0);
    if (length == (size_t)-1) {
        free(plain);
        return;
    }
    wchar_t *wide = xmalloc((length + 1) * sizeof *wide);
    mbstowcs(wide, plain, length + 1);
    int cells = 0;
    size_t n = 0;
    for (; wide[n]; n++) {
        int w = wcwidth(wide[n]);
        if (w < 0)
            w = 0;
        if (cells + w > COLS - 1)
            break;
        cells += w;
    }
    attron(attr);
    mvaddnwstr(y, 0, wide, (int)n);
    attroff(attr);
    free(wide);
    free(plain);
}
static bool contains(const char *haystack, const char *needle)
{
    if (!*needle)
        return true;
    size_t hn = mbstowcs(NULL, haystack, 0), nn = mbstowcs(NULL, needle, 0);
    if (hn == (size_t)-1 || nn == (size_t)-1)
        return strstr(haystack, needle) != NULL;
    wchar_t *h = xmalloc((hn + 1) * sizeof *h), *n = xmalloc((nn + 1) * sizeof *n);
    mbstowcs(h, haystack, hn + 1);
    mbstowcs(n, needle, nn + 1);
    for (size_t i = 0; i < hn; i++)
        h[i] = (wchar_t)towlower(h[i]);
    for (size_t i = 0; i < nn; i++)
        n[i] = (wchar_t)towlower(n[i]);
    bool match = wcsstr(h, n) != NULL;
    free(h);
    free(n);
    return match;
}
static char *prompt(const char *label)
{
    Buffer b = {0};
    buffer_add(&b, "", 0);
    curs_set(1);
    timeout(-1);
    while (!interrupted) {
        move(LINES - 4, 0);
        clrtoeol();
        char *line = strf("%s%s", label, b.data);
        put(LINES - 4, line, 0);
        free(line);
        refresh();
        wint_t key;
        int type = get_wch(&key);
        if (type == ERR)
            continue;
        if (key == '\n' || key == '\r' || key == KEY_ENTER) {
            curs_set(0);
            return b.data;
        }
        if (key == 27)
            break;
        if (key == KEY_BACKSPACE || key == 127 || key == 8) {
            if (b.len) {
                b.len--;
                while (b.len && ((unsigned char)b.data[b.len] & 0xc0) == 0x80)
                    b.len--;
                b.data[b.len] = 0;
            }
        } else if (type != KEY_CODE_YES && iswprint(key) && b.len < 1024) {
            char out[MB_LEN_MAX];
            mbstate_t state = {0};
            size_t n = wcrtomb(out, (wchar_t)key, &state);
            if (n != (size_t)-1)
                buffer_add(&b, out, n);
        }
    }
    curs_set(0);
    buffer_free(&b);
    return NULL;
}

static const char *views[] = {"pending", "watched", "skipped", "downloads", "channels", "browse"};
static const char *labels[] = {"Unwatched", "Watched",  "Skipped",
                               "Downloads", "Channels", "Channel catalogue"};
static void help(void)
{
    puts("j/k or arrows: select | PageUp/PageDown: move within page\n"
         "1 Unwatched | 2 Watched | 3 Skipped | 4 Downloads | 5 Channels\n"
         "Enter/p: play (offline first); Enter on channel: browse\n"
         "[ / ]: previous/next page of 50 | /: search loaded titles\n"
         "n: fetch next 100 older uploads | N: refresh from newest\n"
         "w: watched | x: skip | a: inbox | u: undo\n"
         "d: download now (Ctrl+C cancels) | D: remove offline files\n"
         "r: check uploads | t: classify next batch of hidden video types\n"
         "s: toggle Shorts | m: playback mode | v: download quality\n"
         "e: edit channels | S: sync now | g: refresh saved lists\n"
         "Esc: clear search / return to channels | q: quit\n"
         "Playing, downloading and closing never mark a video watched.");
}

int tui(App *a, const char *output)
{
    if (!isatty(STDIN_FILENO) || !isatty(STDOUT_FILENO))
        return fail(a, "The TUI needs an interactive terminal; use list for plain output");
    int lock = lock_file(a, "interface.lock");
    if (lock < 0)
        return -1;
    if (!initscr()) {
        close(lock);
        return fail(a, "Cannot initialise terminal");
    }
    cbreak();
    noecho();
    keypad(stdscr, TRUE);
    curs_set(0);
    int view = 0, page = 0, cursor = 0;
    char *channel = NULL, *search = strdup("");
    char message[2048] = "e: edit channels | r: check uploads | ?: all keys";
    Json *rows = NULL;
    bool reload = true;
    int result = 0;
    while (!interrupted) {
        if (reload) {
            json_object_put(rows);
            rows = NULL;
            if (settings_load(a)) {
                snprintf(message, sizeof message, "%s", a->error);
                result = -1;
                break;
            }
            Json *all = list_rows(a, views[view], view == 5 ? channel : NULL);
            if (!all) {
                snprintf(message, sizeof message, "%s", a->error);
                result = -1;
                break;
            }
            rows = json_object_new_array();
            for (size_t i = 0; i < json_object_array_length(all); i++) {
                Json *r = json_object_array_get_idx(all, i);
                char *text =
                    strf("%s %s %s", jstr(r, "title"), jstr(r, "channel"), jstr(r, "name"));
                if (contains(text, search))
                    json_object_array_add(rows, json_object_get(r));
                free(text);
            }
            json_object_put(all);
            reload = false;
        }
        int total = (int)json_object_array_length(rows), pages = total ? (total + 49) / 50 : 1;
        if (page < 0)
            page = 0;
        if (page >= pages)
            page = pages - 1;
        int count = total - page * 50;
        if (count > 50)
            count = 50;
        if (cursor >= count)
            cursor = count - 1;
        if (cursor < 0)
            cursor = 0;
        Json *selected =
            count ? json_object_array_get_idx(rows, (size_t)(page * 50 + cursor)) : NULL;
        erase();
        if (LINES < 18 || COLS < 60)
            put(0, "Enlarge terminal to 60 columns x 18 rows. q quits.", 0);
        else {
            char *line = strf("YouTube Inbox %s | %s", VERSION, labels[view]);
            put(0, line, A_BOLD);
            free(line);
            put(1, "1 Unwatched  2 Watched  3 Skipped  4 Downloads  5 Channels", 0);
            line = strf("Page %d/%d | %d entries | / %s", page + 1, pages, total, search);
            put(2, line, 0);
            free(line);
            int height = jget(a->settings, "download_height")
                             ? json_object_get_int(jget(a->settings, "download_height"))
                             : 720;
            line = strf("Playback: %s | Download: %dp | Hide Shorts: %s", playback_mode(a), height,
                        hide_shorts(a) ? "yes" : "no");
            put(3, line, 0);
            free(line);
            int visible = LINES - 12, offset = cursor / visible * visible;
            for (int i = offset; i < count && i < offset + visible; i++) {
                Json *r = json_object_array_get_idx(rows, (size_t)(page * 50 + i));
                if (view == 4)
                    line = strf("%s%s%s", i == cursor ? "> " : "  ", jstr(r, "name"),
                                *jstr(r, "error") ? " [check error]" : "");
                else {
                    const char *state =
                        *jstr(r, "download_state") ? jstr(r, "download_state") : jstr(r, "status");
                    line = strf("%s[%s] %s | %s", i == cursor ? "> " : "  ", state,
                                jstr(r, "channel"), jstr(r, "title"));
                }
                put(4 + i - offset, line, i == cursor ? A_REVERSE : 0);
                free(line);
            }
            if (selected) {
                put(LINES - 8, view == 4 ? jstr(selected, "name") : jstr(selected, "title"),
                    A_BOLD);
                line = strf("https://www.youtube.com/%s%s",
                            view == 4 ? "channel/" : "watch?v=", jstr(selected, "id"));
                put(LINES - 6, line, 0);
                free(line);
            }
            put(LINES - 5,
                hide_shorts(a) ? "Unknown video types stay hidden. t checks a batch; s shows all."
                               : "",
                0);
            put(LINES - 4, message, 0);
            put(LINES - 3, "Enter play | w watched | x skip | a inbox | d download | / search", 0);
            put(LINES - 2, "[ ] pages | n older | r check | S sync | e channels | ? help | q quit",
                0);
        }
        refresh();
        timeout(sync_configured(a) ? 2000 : -1);
        wint_t key;
        int keytype = get_wch(&key);
        if (keytype == ERR) {
            reload = true;
            continue;
        }
        if (key == 'q')
            break;
        if (LINES < 18 || COLS < 60)
            continue;
        if (key >= '1' && key <= '5') {
            view = (int)(key - '1');
            page = cursor = 0;
            free(search);
            search = strdup("");
            reload = true;
            continue;
        }
        if (key == 'j' || key == KEY_DOWN) {
            cursor++;
            if (cursor >= count)
                cursor = count - 1;
            continue;
        }
        if (key == 'k' || key == KEY_UP) {
            cursor--;
            continue;
        }
        if (key == KEY_NPAGE) {
            cursor += LINES - 12;
            continue;
        }
        if (key == KEY_PPAGE) {
            cursor -= LINES - 12;
            continue;
        }
        if (key == '[' || key == ']') {
            page += key == '[' ? -1 : 1;
            cursor = 0;
            continue;
        }
        if (key == '/') {
            char *value = prompt("Search loaded titles (Esc cancels): ");
            if (value) {
                free(search);
                search = value;
                page = cursor = 0;
                reload = true;
            }
            continue;
        }
        if (key == 27) {
            if (*search) {
                free(search);
                search = strdup("");
            } else if (view == 5)
                view = 4;
            page = cursor = 0;
            reload = true;
            continue;
        }
        if (key == 'g' || key == KEY_RESIZE) {
            reload = true;
            continue;
        }
        if (view == 4 && selected && (key == '\n' || key == '\r' || key == KEY_ENTER)) {
            free(channel);
            channel = strdup(jstr(selected, "id"));
            view = 5;
            page = cursor = 0;
            free(search);
            search = strdup("");
            reload = true;
            continue;
        }
        if (key == 's' || key == 'm' || key == 'v') {
            if (settings_load(a)) {
                snprintf(message, sizeof message, "%s", a->error);
                continue;
            }
            if (key == 's')
                json_object_object_add(a->settings, "hide_shorts",
                                       json_object_new_boolean(!hide_shorts(a)));
            if (key == 'm') {
                const char *modes[] = {"auto", "low", "balanced", "high", "custom"};
                int index = 0;
                while (index < 4 && strcmp(playback_mode(a), modes[index]))
                    index++;
                jsetstr(a->settings, "playback_mode", modes[(index + 1) % 5]);
            }
            if (key == 'v') {
                int h = json_object_get_int(jget(a->settings, "download_height"));
                json_object_object_add(a->settings, "download_height",
                                       json_object_new_int(!h || h == 720 ? 1080
                                                           : h == 1080    ? 480
                                                                          : 720));
            }
            if (settings_save(a))
                snprintf(message, sizeof message, "%s", a->error);
            else
                snprintf(message, sizeof message, "Setting saved; shared with the desktop app.");
            reload = true;
            continue;
        }
        char *id = selected ? strdup(jstr(selected, "id")) : NULL;
        if (id && view != 4 && (key == 'w' || key == 'x' || key == 'a')) {
            const char *state = key == 'w' ? "watched" : key == 'x' ? "skipped" : "pending";
            if (set_status(a, 1, &id, state))
                snprintf(message, sizeof message, "%s", a->error);
            else
                snprintf(message, sizeof message, "Marked %s. u undoes this change.", state);
            free(id);
            reload = true;
            continue;
        }
        bool remove = false;
        if (key == 'D' && id && view != 4) {
            char *answer = prompt("Remove offline files? Type yes: ");
            remove = answer && !strcmp(answer, "yes");
            free(answer);
            if (!remove) {
                free(id);
                continue;
            }
        }
        bool play =
            id && view != 4 && (key == '\n' || key == '\r' || key == KEY_ENTER || key == 'p');
        bool download = id && view != 4 && key == 'd';
        bool older = view == 5 && (key == 'n' || key == 'N');
        if (!play && !download && !remove && !older && key != 'r' && key != 'e' && key != 'S' &&
            key != 't' && key != '?' && key != 'u') {
            free(id);
            continue;
        }
        def_prog_mode();
        endwin();
        putchar('\n');
        a->error[0] = 0;
        int rc = 0;
        if (play)
            rc = play_video(a, id, output);
        else if (download)
            rc = download_video(a, id);
        else if (remove)
            rc = remove_download(a, id);
        else if (older) {
            rc = load_catalogue(a, channel, key == 'N');
            if (!rc)
                rc = classify_saved(a);
        } else if (key == 'r')
            rc = check_uploads(a, false, false);
        else if (key == 'e')
            rc = edit_channels(a);
        else if (key == 'S')
            rc = sync_now(a, false);
        else if (key == 't')
            rc = classify_saved(a);
        else if (key == 'u')
            rc = undo_status(a);
        else
            help();
        snprintf(message, sizeof message, "%s",
                 rc     ? (*a->error ? a->error : "Operation failed")
                 : play ? "Playback closed. w marks watched."
                        : "Done.");
        print_plain(message);
        if (interrupted == SIGINT)
            interrupted = 0;
        if ((!play || rc) && !interrupted) {
            fputs("\nPress Enter to return to the inbox.", stdout);
            fflush(stdout);
            char input[8];
            if (!fgets(input, sizeof input, stdin))
                clearerr(stdin);
        }
        if (interrupted == SIGINT)
            interrupted = 0;
        reset_prog_mode();
        flushinp();
        keypad(stdscr, TRUE);
        clearok(stdscr, TRUE);
        free(id);
        reload = true;
    }
    endwin();
    json_object_put(rows);
    free(channel);
    free(search);
    close(lock);
    return result;
}
