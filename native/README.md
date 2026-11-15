# YouTube Inbox in C

A native **C11/POSIX CLI and TUI**, alongside the existing Python desktop app.
Written with AI (OpenAI ChatGPT/Codex), at the project owner's request, including
the implementation, tests and study guide.

This is the first native release: **1.6.0-c.1**. It uses the existing SQLite
schema, settings, channel list, download directory and version-1 sync document.
There is no migration or separate watch history. The Python version remains
available while the native implementation receives real-world use.

## What is native

- Feed fetching and latest-video seeding, with a bounded yt-dlp fallback.
- Explicit watched/skipped/restore/undo actions.
- TUI browsing, 50-item pages, title/channel search and batches of 100 older uploads.
- Shorts filtering, including hiding unknown types until classification.
- Offline downloads, cancellation, resuming through yt-dlp, and safe file validation.
- mpv command construction, quality modes, configuration loading, local TTY playback
  and tmux client handoff/reattachment.
- Private GitHub synchronization: per-item revisions, removals, merge/retry,
  repository identity/privacy checks and crash-recoverable channel projection.
- User-local installer and systemd check/sync services.

## Dependencies

The native executable links libc/POSIX, **libcurl, SQLite, ncursesw, Expat and
json-c**. Their system dependencies, including TLS/certificate support, still
apply. Libraries are supplied by the distribution rather than copied into this
repository.

External programs retain their roles: **yt-dlp** extracts YouTube metadata and
media; **mpv** decodes/plays it; **ffmpeg** merges downloaded streams. Full YouTube
support also needs yt-dlp's EJS components and a supported JavaScript engine such
as Deno. Optional tools are tmux, a text editor, systemd user services, and
`notify-send` from libnotify for desktop notifications.

The native runtime never invokes Python for the core, TUI, sync or installer.
The distribution's yt-dlp package still uses Python. The optional existing GUI
is still Python/PySide6. `playback.lua` remains a small mpv extension: it observes
the actual decoder and adapts streaming quality inside mpv.

## Build on Arch

From the repository root:

```sh
sudo pacman -S --needed base-devel curl sqlite ncurses expat json-c
make -C native
./native/build/yt-inbox-c tui
```

The media packages, if not already installed:

```sh
sudo pacman -S --needed mpv yt-dlp ffmpeg deno
```

On Debian/Ubuntu the development packages are `build-essential pkg-config
libcurl4-openssl-dev libsqlite3-dev libncurses-dev libexpat1-dev libjson-c-dev`.
GCC or Clang and pkg-config/pkgconf are build-time requirements. Python 3 is
needed only for the development tests and the optional old GUI.

## Install

Close the existing inbox interface before switching.

```sh
# Add yt-inbox-c; leave the existing yt-inbox launcher and timers unchanged.
./native/build/yt-inbox-c install

# Make the native executable handle yt-inbox and the existing check/sync timers.
./native/build/yt-inbox-c install --primary
yt-inbox tui
```

Run the installer as your normal user. It writes the executable and Lua asset
under the selected data directory's `native/` folder, and a small shell launcher
under `~/.local/bin`. The launcher does not contain Python. Installation preserves
channels, settings, history, tokens, downloads and the existing Python GUI files.

Primary installation retains notification behavior for a desktop profile when
`notify-send` is installed; terminal-only profiles use `--no-notify`. XDG paths
are embedded in the service environment so the timers use the correct profile.
Native notifications are plain digests; the old actionable **Open inbox** button
is not implemented in the native notifier.

Use `--no-enable` to write installation files without starting systemd units.
Automatic checks/sync require a working user systemd session. If its activation
fails, the installer reports failure; the installed binary still works manually.

To return the launcher and timers to the Python implementation, run the existing
`bash install.sh --terminal` or `bash install.sh --desktop` from the repository.

## Everyday commands

```sh
yt-inbox-c --help
yt-inbox-c check --no-notify
yt-inbox-c list
yt-inbox-c channels
yt-inbox-c watched VIDEO_ID
yt-inbox-c skip VIDEO_ID
yt-inbox-c undo
yt-inbox-c sync
```

In the TUI: `5` opens channels, Enter opens a channel, `n` fetches older uploads,
`[` and `]` change pages, `/` searches the loaded catalogue. `t` classifies another
bounded batch of hidden video types; `s` toggles the Shorts filter. `w` marks
watched, `x` skips, `a` restores and `u` undoes. `d` downloads and Enter plays the
saved file first. Playback and downloading never change watched state.

`gui` explicitly launches the optional already-installed Python GUI. The native
default is always the TUI. The interfaces share `interface.lock`, so close one
before opening the other.

Sync setup is the same private-repository workflow as the Python version:

```sh
yt-inbox-c sync setup OWNER/PRIVATE_REPO
```

Enter the repository-scoped token only at the hidden terminal prompt. Existing
`sync.json` needs no setup again. Sync is not end-to-end encrypted: GitHub and
anyone granted access to that private repository can read its contents.

## Tests and limits

```sh
make -C native test
make -C native sanitize
```

The tests use isolated temporary profiles, fake media tools and HTTP fixtures.
The initial release passes 31 native tests and the existing 123-test Python suite
(four existing hardware/tool-dependent tests are skipped). The native tests also
pass with AddressSanitizer and UndefinedBehaviorSanitizer. LeakSanitizer could not
run in the traced development environment; leak checking remains enabled in the
default `sanitize` target for a normal local run.

They exercise C/Python state interoperability, same-revision conflict ordering,
initial pairing, removals, interrupted channel projection, privacy rechecks,
CAS conflicts, blob fallback, data validation, file ownership, cancellation,
installer errors, terminal restoration and an interactive pseudoterminal TUI.
The test-only HTTP wrapper is linked into `build/test-driver`, never the
production executable. Production HTTPS verification and fixed GitHub host
cannot be disabled through a test environment variable.

Live YouTube extraction, an actual private GitHub repository, desktop notification
rendering and real GPU/DRM playback still require testing in the user's session.
This port does not promise lower video-decoding power usage: mpv still does that
work. The C TUI classifies video types sequentially within a bounded time budget;
the Python implementation uses two workers. Channel handles are resolved with
yt-dlp; `/channel/UC...` URLs need no resolver subprocess.

For an isolated learning profile:

```sh
YT_INBOX_HOME="$PWD/native-demo" ./native/build/yt-inbox-c tui
```

Do not commit that profile, downloaded videos or credentials.

Read [ESTUDAR.md](ESTUDAR.md) for the code map and exercises.
