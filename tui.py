"""Finite, keyboard-only inbox for a Linux console or terminal emulator."""
import contextlib
import curses
import datetime as dt
import locale
import os
import shlex
import shutil
import subprocess
import sys
import textwrap

import core
import syncing
import media
import playback
import terminal
import video_types

VIEWS = {"1": "Unwatched", "2": "Watched", "3": "Skipped", "4": "Downloads", "5": "Channels"}
STATUSES = {"1": "pending", "2": "watched", "3": "skipped"}
LABELS = {"pending": "Inbox", "watched": "Watched", "skipped": "Skipped", "baseline": "Saved"}
HELP = [
    "Up/Down or j/k: select     PgUp/PgDn: move in this page",
    "1 Unwatched   2 Watched   3 Skipped   4 Downloads   5 Channels",
    "Enter/p: play video (saved file first); Enter opens a channel",
    "[/]: previous/next page of 50 cached videos; /: search loaded titles",
    "n: load 100 older channel uploads; N: refresh catalogue from newest",
    "w: mark watched   x: skip   a: add/restore to inbox   u: undo",
    "d: download selected video now; Ctrl+C cancels, retaining partials",
    "D: remove selected downloaded files (asks first)",
    "r: check uploads   t: classify next batch of pending video types",
    "e: edit channels with $VISUAL/$EDITOR, nano or vi; r applies changes",
    "m: cycle playback mode   v: cycle download quality   s: toggle Shorts",
    "g: reload saved lists   Esc: clear search / back to channels   q: quit",
    "mpv: Space pauses, arrows seek, q returns; it never marks watched",
    "S: sync channels/history now (configure with yt-inbox sync setup OWNER/REPO)",
    "Upload/catalogue tasks run on request. Configured sync also runs in background.",
]


@contextlib.contextmanager
def suspended(screen):
    """Restore the shell's terminal modes for a player/editor, even on failure."""
    curses.def_prog_mode()
    curses.endwin()
    try:
        yield
    finally:
        curses.reset_prog_mode()
        curses.flushinp()
        screen.keypad(True)
        screen.clearok(True)
        screen.refresh()


class Inbox:
    def __init__(self, screen, paths, output="auto"):
        self.screen, self.paths, self.output = screen, paths, output
        self.view, self.channel, self.query = "1", None, ""
        self.page, self.cursor, self.total = 0, 0, 0
        self.rows, self.downloads = [], {}
        self.message = "e: add channels | r: check uploads | ?: all keys"
        self.reload()

    def reload(self):
        if self.view == "browse":
            self.rows, self.total, self.page = media.browse_page(self.paths, self.channel["id"], self.query, self.page)
        else:
            rows = (core.channel_rows(self.paths) if self.view == "5" else
                    media.download_rows(self.paths) if self.view == "4" else
                    core.queue_rows(self.paths, STATUSES[self.view]))
            query = self.query.casefold().strip()
            rows = [r for r in rows if not query or query in
                    (r.get("title", "") + " " + r.get("channel", r.get("name", ""))).casefold()]
            self.total = len(rows)
            self.page = max(0, min(self.page, max(0, (self.total - 1) // media.PAGE_SIZE)))
            self.rows = rows[self.page * media.PAGE_SIZE:(self.page + 1) * media.PAGE_SIZE]
        self.cursor = max(0, min(self.cursor, len(self.rows) - 1))
        self.downloads = media.download_records(self.paths)
        self.preferences = core.settings(self.paths)
        self.pending_types = video_types.pending_count(self.paths) if core.hide_shorts(self.paths) else 0
        self.sync_error = ""
        if syncing.configured(self.paths):
            db = core.connect(self.paths)
            try:
                self.sync_error = core.meta_get(db, "sync_error")
            finally:
                db.close()

    def selected(self):
        return self.rows[self.cursor] if self.rows else None

    def put(self, y, text, attr=0):
        height, width = self.screen.getmaxyx()
        if 0 <= y < height:
            try:
                self.screen.addstr(y, 0, terminal.clipped(text, max(0, width - 1)), attr)
            except curses.error:
                pass

    def draw(self):
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < 18 or width < 60:
            self.put(0, "Enlarge terminal to 60 columns x 18 rows. q quits.")
            self.screen.refresh()
            return
        name = self.channel["name"] if self.view == "browse" else VIEWS[self.view]
        mode = self.preferences.get("playback_mode", playback.DEFAULT_MODE)
        self.put(0, f"YouTube Inbox {core.VERSION} | {name}", curses.A_BOLD)
        self.put(1, "1 Unwatched  2 Watched  3 Skipped  4 Downloads  5 Channels")
        self.put(2, f"Page {self.page + 1}/{max(1, (self.total + 49) // 50)} | {self.total} entries | / {self.query}")
        self.put(3, f"Playback: {mode} | Download: {self.preferences.get('download_height', 720)}p | Hide Shorts: {core.hide_shorts(self.paths)}")
        count = height - 12
        offset = (self.cursor // count) * count
        for index, row in enumerate(self.rows[offset:offset + count], offset):
            if self.view == "5":
                text = row["name"] + (" [check error]" if row.get("error") else "")
            else:
                date = dt.datetime.fromtimestamp(row["published"]).strftime("%Y-%m-%d") if row["published"] else "----------"
                state = self.downloads.get(row["id"], {}).get("state")
                badge = "Ready" if state == "complete" else state or LABELS[row["status"]]
                text = f"{date} [{badge}] {row['channel']} | {row['title']}"
            self.put(4 + index - offset, ("> " if index == self.cursor else "  ") + text,
                     curses.A_REVERSE if index == self.cursor else 0)
        row = self.selected()
        if row:
            title = row.get("title", row.get("name", ""))
            lines = textwrap.wrap(terminal.plain(title), max(1, width - 2))
            for index, line in enumerate(lines[:2]):
                self.put(height - 8 + index, line, curses.A_BOLD)
            self.put(height - 6, "https://www.youtube.com/channel/" + row["id"] if self.view == "5" else core.video_url(row["id"]))
        self.put(height - 5, "Sync: " + self.sync_error if self.sync_error else
                 f"{self.pending_types} hidden pending type checks; t checks next batch, s toggles filter." if self.pending_types else "")
        self.put(height - 4, self.message)
        self.put(height - 3, "Enter play | w watched | x skip | a inbox | d download | / search")
        self.put(height - 2, "[ ] pages | n older | r check | S sync | e channels | ? help | q quit")
        self.screen.refresh()

    def prompt(self, label):
        value = ""
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            while True:
                height, width = self.screen.getmaxyx()
                self.screen.move(height - 4, 0)
                self.screen.clrtoeol()
                self.put(height - 4, label + value[-max(1, width - len(label) - 2):])
                self.screen.refresh()
                key = self.screen.get_wch()
                if key in ("\n", "\r", curses.KEY_ENTER):
                    return value
                if key == "\x1b":
                    return None
                if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    value = value[:-1]
                elif isinstance(key, str) and key.isprintable() and len(value) < 300:
                    value += key
        finally:
            try:
                curses.curs_set(0)
            except curses.error:
                pass

    def external(self, label, action, pause=True):
        with suspended(self.screen):
            print("\n" + terminal.plain(label), flush=True)
            try:
                self.message = action() or "Done."
            except KeyboardInterrupt:
                self.message = "Cancelled. Saved progress and partial downloads are retained."
            except (core.InboxError, OSError, ValueError) as exc:
                self.message = str(exc)
                pause = True
            print(terminal.plain(self.message), flush=True)
            if pause:
                try:
                    input("\nPress Enter to return to the inbox.")
                except (KeyboardInterrupt, EOFError):
                    pass
        self.reload()

    def check(self):
        result = core.run_check(self.paths)
        for error in result["errors"]:
            print(terminal.plain(error))
        return f"{result['new']} new; {result['checked']} channels checked; {len(result['errors'])} errors."

    def older(self, restart=False):
        result = media.load_catalogue(self.paths, self.channel["id"], restart=restart)
        video_types.classify_saved(self.paths)
        return f"Loaded {result['loaded']} entries. " + ("End of catalogue." if result["exhausted"] else "n loads the next batch; t checks pending types.")

    def edit_channels(self):
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        command = shlex.split(editor) if editor else [shutil.which("nano") or shutil.which("vi") or ""]
        if not command or not command[0]:
            raise core.InboxError(f"Set EDITOR or edit {self.paths.channels} yourself.")
        with syncing.channel_lock(self.paths):
            result = subprocess.run([*command, str(self.paths.channels)])
        if result.returncode:
            raise core.InboxError("Editor exited with an error.")
        _, errors = core.parse_channels(self.paths.channels.read_text(encoding="utf-8"))
        if errors:
            raise core.InboxError("\n".join(errors))
        syncing.kick(self.paths)
        return "Channels saved. Press r to check and apply the list."

    def setting(self, key, choices):
        data = core.settings(self.paths)
        defaults = {"playback_mode": "auto", "hide_shorts": True, "download_height": 720}
        current = data.get(key, defaults[key])
        data[key] = choices[(choices.index(current) + 1) % len(choices)]
        core.save_settings(self.paths, data)
        self.message = f"{key.replace('_', ' ').capitalize()}: {data[key]} (also used by the desktop app)."
        self.reload()

    def handle(self, key):
        if key in VIEWS:
            self.view, self.query, self.page, self.cursor = key, "", 0, 0
        elif key in (curses.KEY_UP, "k", curses.KEY_DOWN, "j", curses.KEY_PPAGE, curses.KEY_NPAGE):
            step = -1 if key in (curses.KEY_UP, "k") else 1
            if key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
                step = max(1, self.screen.getmaxyx()[0] - 12) * (-1 if key == curses.KEY_PPAGE else 1)
            self.cursor = max(0, min(self.cursor + step, len(self.rows) - 1))
            return
        elif key in ("[", "]"):
            self.page += -1 if key == "[" else 1
            self.cursor = 0
        elif key == "/":
            query = self.prompt("Search loaded titles/channels (Esc cancels): ")
            if query is not None:
                self.query, self.page, self.cursor = query, 0, 0
        elif key == "\x1b":
            if self.query:
                self.query, self.page, self.cursor = "", 0, 0
            elif self.view == "browse":
                self.view, self.page, self.cursor = "5", 0, 0
        elif key == "r":
            self.external("Checking uploads... Ctrl+C cancels.", self.check)
        elif key == "S":
            self.external("Synchronizing channels and history...", lambda: syncing.sync(self.paths))
        elif key == "t":
            self.external("Checking a batch of video types...", lambda: f"Classified {video_types.classify_saved(self.paths)} videos. Press t again for another batch.")
        elif key in ("n", "N") and self.view == "browse":
            self.external("Loading channel catalogue... Ctrl+C cancels.", lambda: self.older(key == "N"))
        elif key == "e":
            self.external("Edit channels", self.edit_channels)
        elif key == "m":
            self.setting("playback_mode", list(playback.MODES))
        elif key == "v":
            self.setting("download_height", [480, 720, 1080])
        elif key == "s":
            self.setting("hide_shorts", [True, False])
        elif key == "u":
            self.message = "Previous status restored." if core.undo(self.paths) else "Nothing to undo."
        elif key == "?":
            self.external("Keyboard controls", lambda: print("\n".join(HELP)))
        elif key == "g":
            self.message = "Saved lists reloaded."
        elif self.selected():
            row = self.selected()
            if self.view == "5":
                if key in ("\n", "\r", curses.KEY_ENTER):
                    self.channel, self.view, self.page, self.cursor, self.query = row, "browse", 0, 0, ""
            elif key in ("\n", "\r", curses.KEY_ENTER, "p"):
                self.external(row["title"], lambda: terminal.play(self.paths, row["id"], self.output), pause=False)
            elif key in ("w", "x", "a"):
                status = {"w": "watched", "x": "skipped", "a": "pending"}[key]
                core.set_status(self.paths, [row["id"]], status)
                self.message = f"{LABELS[status]}: {row['title']} | u undoes this change."
            elif key == "d":
                self.external("Downloading: " + row["title"] + " (Ctrl+C cancels)", lambda: terminal.download(self.paths, row["id"]))
            elif key == "D" and self.prompt("Remove this video's offline files? Type yes: ") == "yes":
                record = self.downloads.get(row["id"], {})
                if record.get("state") == "queued":
                    media.update_download(self.paths, row["id"], "cancelled")
                media.remove_download(self.paths, row["id"])
                self.message = "Offline files removed. Watch history retained."
        self.reload()

    def run(self):
        self.screen.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        while True:
            self.draw()
            self.screen.timeout(2000 if syncing.configured(self.paths) else -1)
            try:
                key = self.screen.get_wch()
            except curses.error:
                self.reload()
                continue
            finally:
                self.screen.timeout(-1)
            if key == "q":
                return 0
            if min(self.screen.getmaxyx()[0] - 18, self.screen.getmaxyx()[1] - 60) < 0:
                continue
            try:
                self.handle(key)
            except (core.InboxError, OSError, ValueError) as exc:
                self.message = str(exc)


def run(paths, output="auto"):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise core.InboxError("The terminal inbox needs an interactive terminal. Use 'yt-inbox list' for plain text.")
    locale.setlocale(locale.LC_ALL, "")
    with core.interface_lock(paths):
        media.recover_downloads(paths)
        try:
            return curses.wrapper(lambda screen: Inbox(screen, paths, output).run())
        except KeyboardInterrupt:
            return 0
        except curses.error as exc:
            raise core.InboxError("Could not initialise the terminal. Check TERM and terminal size.") from exc
