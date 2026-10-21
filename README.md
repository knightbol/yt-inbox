# YouTube Inbox

A native desktop inbox for Arch Linux/KDE. Maintain a text file of channels,
get one daily upload notification, and keep a finite list of unwatched videos.

> **Written with AI:** The initial application, interface, tests and documentation
> were written with OpenAI ChatGPT/Codex, guided by the project owner's requirements
> and feedback.

## Project status

This is an early personal project. Automated tests cover the inbox and its interface,
and channel resolution and upload retrieval were checked against a live YouTube
channel. Native KDE notification display and mpv playback still need end-to-end
verification on an Arch Linux desktop. See **Limits** below for known constraints.

## Install

Install dependencies with your system package manager:

```sh
sudo pacman -S --needed python pyside6 mpv yt-dlp deno libnotify
```

Download the repository's ZIP from GitHub (**Code → Download ZIP**) or use the
release archive. Extract it, open a terminal in the project directory and run:

```sh
bash install.sh
~/.local/bin/yt-inbox
```

Run the installer as your normal user, without sudo. It installs only for your
account, enables a daily user timer and adds **YouTube Inbox** to your application
launcher. If `~/.local/bin` is already in PATH, `yt-inbox` works from any directory.
Reinstalling preserves all channels, settings and watched/skipped history.

## Updating an existing installation

Close the inbox, download and extract the latest repository ZIP, then run
`bash install.sh` from the newly extracted directory. Reopen the inbox and select
**Check now**. There is no need to delete the database or re-enter your channels.
Check the installed version with `~/.local/bin/yt-inbox --version`.

Version **1.1.0** adds **Browse channels**: choose a followed channel to see up to
15 recent saved uploads, play one directly, or add it to your inbox for later.
Watched and skipped labels remain visible while browsing. This includes older
uploads that were never added to Unwatched. Previous latest-video inclusion and
upload-list fallback fixes are also included; use a fresh download.

## First use

1. Select **Channels**, paste one channel URL or `@handle` per line, then Save.
2. The first complete check adds each channel's latest published video to Unwatched,
   even if it was uploaded before you added the channel. Other older uploads stay
   out of the inbox. A partial check finishes this step when its remaining lookups succeed.
3. New uploads appear after **Check now** or the next daily check. The starting video
   is added only once per channel; watching or skipping it keeps it out of Unwatched.
4. Select a video and choose **Play in mpv** or **Open in browser**. Its direct link
   appears below the list and can be copied.
5. Select **Mark watched** when finished, or **Skip** to dismiss it. Both remove it
   from Unwatched. **Restore** in Watched/Skipped or **Undo** brings it back.

Opening a video, reaching its end, or closing the player never marks it watched.
There is no automatic next-video playback or recommendations view. The dedicated
mpv launch ignores your normal mpv configuration and requests at most 1080p where
available. No video file is downloaded for offline storage; ordinary playback
buffers are used. mpv may retain a resume position locally.

Ctrl+R checks for uploads, Ctrl+W marks selected videos watched, Ctrl+K skips them,
Ctrl+Z undoes the latest individual change, and Return plays the selected video.
Multiple videos can be selected when changing status. Undo restores one video
at a time. A playing video or running check must finish/close before closing the
inbox window.

## Browse a channel's recent videos

1. Open the **Unwatched** dropdown and choose **Browse channels**.
2. Choose one of your followed channels in the channel selector.
3. Select a video to play it, open its direct link, or choose **Add to inbox**
   to save it in Unwatched for later. **Undo** reverses the most recent change.

The view shows up to 15 latest published uploads saved by your checks, newest
first, including older uploads hidden from the inbox. It shows each video's
status: **In inbox**, **Watched**, **Skipped**, or **Not in inbox**. Watching or
skipping a video keeps it in this recent list with its label; it stays out of
Unwatched unless you explicitly add it again.

Browsing and playback do not automatically change any video's status. The filter
searches this finite list. There is no infinite scroll or full-channel archive.
The channel's last complete check time appears above the videos. **Check now**
refreshes all your channels using the usual upload check; saved videos remain
available to browse when offline. Video playback needs an internet connection.

## Daily behaviour

- Default: **19:00 local time**, configurable in Settings.
- Checks run in your logged-in user session. A missed check catches up after login
  (normally within about 90 seconds) or after resume. The computer is not woken up.
- A failed check retries after 15 minutes. Already successful channels are skipped
  on retries within that daily cycle. Errors are shown in the inbox.
- If a channel feed returns HTTP 404 or a server error, the app uses yt-dlp to read
  its 15 most recent uploads. This fallback can take longer, especially on the
  first check. Each channel has a two-minute fallback budget; completed video
  lookups are saved and unfinished ones can continue on the next check.
- One notification per local calendar day when there are newly discovered,
  still-unwatched videos. It includes up to three titles and an **Open inbox** action.
- Unwatched videos remain until you watch or skip them. They are not discarded
  because the date changed. Previously notified videos do not trigger new digests.
- If some channels fail, a partial digest can be sent; videos recovered later that
  day appear in the inbox and are eligible for the following day's notification.
- Notification actions need your desktop notification service. KDE Do Not Disturb
  and system notification preferences still apply. The inbox can always be opened
  from the application launcher.
- The small notification action listener ends when the notification is dismissed,
  activated, or after 23 hours. There is no web server or account login.

## Channel text file

Default path: `~/.config/yt-inbox/channels.txt`

```text
# One channel per line. Comments and blank lines are ignored.
https://www.youtube.com/@TechnologyConnections
@YourOtherChannel
```

Normal `/channel/UC…`, `/@handle`, `/c/name` and `/user/name` links are accepted.
Channel page tabs such as `/videos` are stripped. These select the whole channel,
not a video-type filter. URLs pointing to individual videos/playlists are rejected.
The channel IDs are resolved and cached automatically. An invalid line prevents
subscription changes until corrected; it cannot erase your existing list.

Removing a channel hides its videos after the next check, while preserving its
history. Re-adding it restores that history; pending uploads still present in its
recent feed can be picked up. A channel renamed on YouTube keeps the same ID;
if a handle is reassigned to a different channel, use the canonical `/channel/UC…`
link for the new channel rather than reusing the cached handle.

## Files and commands

- Settings: `~/.config/yt-inbox/settings.json`
- Queue/history: `~/.local/share/yt-inbox/inbox.sqlite3`
- Installed code: `~/.local/share/yt-inbox/app/`
- Launcher: `~/.local/bin/yt-inbox`
- Timer/services: `~/.config/systemd/user/yt-inbox-*`

Standard XDG config/data/state directory overrides are respected. `YT_INBOX_HOME`
isolates application files for development/testing; leave it unset for installation.

```sh
yt-inbox                     # Open the window
yt-inbox check               # Fetch now without a desktop notification
yt-inbox list                # Print pending titles and direct links
yt-inbox list --json
yt-inbox watched VIDEO_ID
yt-inbox skip VIDEO_ID
yt-inbox restore VIDEO_ID
yt-inbox undo
yt-inbox status              # Paths, errors and last successful check
yt-inbox test-notification   # Test the desktop notification and Open inbox action
systemctl --user list-timers yt-inbox-check.timer
journalctl --user -u yt-inbox-check.service -n 40 --no-pager
```

To pause automatic checks:

```sh
systemctl --user disable --now yt-inbox-check.timer
systemctl --user stop yt-inbox-check.service yt-inbox-notify.service
```

To remove the application while retaining channels and watched history:

```sh
python3 ~/.local/share/yt-inbox/app/install.py --uninstall
```

## Limits

YouTube's public feeds expose a limited recent upload window, not a complete
archive. The fallback also reads at most 15 uploads. A long offline period or a
very busy channel can therefore leave gaps.
This version does not backfill an entire channel or guarantee every upload.
Shorts/live announcements can appear; video-type filtering is not implemented.
Deleted, private or unavailable uploads may fail to play. Watching somewhere else
does not update this local inbox; no YouTube account history is read.

Feeds and channel resolution can temporarily fail or change upstream. Failures
are retained and retried; a failed fetch never clears the queue. A 404 from the
feed alone does not prove that the channel link is wrong. The fallback uses exact
publication times for newly discovered videos; if that metadata is unavailable,
the video is retried instead of being silently treated as an old upload. It does
not retry HTTP access denials or rate limits through a different endpoint.
Keep Arch's yt-dlp and deno packages current if fetching or playback stops working. Restricted videos can
require watching in your signed-in browser; this app does not read browser cookies.

## Verification

The package includes deterministic tests for feed ingestion, deduplication,
latest-video inclusion, finite channel browsing, adding older videos with undo,
baseline handling, status persistence, retry cycles, channel removal/re-addition,
notification delivery/action handling and isolated installation. Regression tests
cover feed outages, fallback ownership validation, partial progress, exact-date
handling and recovery without resetting watched/skipped history. Run:

```sh
python3 -m unittest discover -s tests -v
```

Qt UI verification uses the offscreen backend. Actual YouTube fetching, KDE
notification rendering and mpv streaming still depend on your local session and
network; they cannot be established by fixture tests alone.

Built with Python's standard library plus PySide6. Uses YouTube Atom feeds,
yt-dlp for channel resolution and upload-list fallback, mpv for streaming, and libnotify/systemd
for desktop notifications and scheduling. No API key or subscription required.
