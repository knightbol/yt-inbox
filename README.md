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
sudo pacman -S --needed python pyside6 mpv yt-dlp ffmpeg deno libnotify
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
Reinstalling preserves all channels, settings, watched/skipped history and downloads.

## Updating an existing installation

Close the inbox, download and extract the latest repository ZIP, then run
`bash install.sh` from the newly extracted directory. Reopen the inbox and select
**Check now**. There is no need to delete the database or re-enter your channels.
Check the installed version with `~/.local/bin/yt-inbox --version`.

Version **1.2.0** adds offline downloads and a channel catalogue with no fixed total
video limit. Install the new dependency if needed: `sudo pacman -S --needed ffmpeg`.
Choose individual videos to download, or load a channel's older uploads in batches
of 100. Catalogue import does not add those videos to your daily inbox.

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
available. Ordinary streaming uses playback buffers; **Download offline** saves
a persistent video file. mpv may retain a resume position locally.

Ctrl+R checks for uploads, Ctrl+W marks selected videos watched, Ctrl+K skips them,
Ctrl+Z undoes the latest individual change, and Return plays the selected video.
Multiple videos can be selected when changing status. Undo restores one video
at a time. Close a playing video and wait for checks/catalogue pages to finish
before closing the window. Finish or cancel active downloads in **Downloads**.

## Browse a channel's videos

1. Open the **Unwatched** dropdown and choose **Browse channels**.
2. Choose one of your followed channels in the channel selector.
3. Select **Load older videos** to retrieve the first 100 entries in the channel's
   upload list. Select it again to load each next batch. **Previous / Next** moves
   between pages of 50 already loaded videos.
4. Select a video to play it, download it, open its direct link, or choose **Add to inbox**
   to save it in Unwatched for later. **Undo** reverses the most recent change.

Before loading the catalogue, the view shows uploads saved by your usual checks.
The catalogue follows YouTube's upload-list order, including older uploads hidden
from the inbox. It shows each video's status: **In inbox**, **Watched**, **Skipped**,
or **Not in inbox**. Watching or skipping changes its label and keeps it out of
Unwatched unless you explicitly add it again. Exact publication dates are shown
when known from normal checks; other catalogue entries show a dash.

Browsing and playback do not automatically change any video's status. The filter
searches all loaded entries, including other pages. There is no fixed total
catalogue limit and no automatic infinite scroll. A final empty batch marks the
end of the available list. Saved catalogue entries remain available offline.

**Refresh catalogue** restarts from the newest uploads while retaining cached
entries, watched history and downloads. Use it when the upload list changes or
to retry a scan from the beginning. **Check now** continues to refresh the normal
daily inbox; it does not crawl the full catalogue. Failed catalogue pages can be
retried without losing earlier pages. Each page has a two-minute lookup timeout.

## Download videos for later

1. Select one or more videos and choose **Download offline**.
2. Select **Downloads** in the view dropdown to see the queue and saved files.
3. Wait for **Ready**, then select **Play offline**. This opens the local file in
   mpv even when your preferred streaming player is the browser.

Downloads run one at a time while the app is open. The progress line shows the
active download; merging audio and video can take a little longer after 100%.
The default maximum quality is **720p**. Settings also offers **480p** and **1080p**;
changes apply to subsequent downloads. Actual quality depends on available formats.

**Cancel download** stops selected queued/active downloads and keeps partial files.
Select **Download offline** again to retry; yt-dlp attempts to resume supported
partial downloads. If the app exits unexpectedly, interrupted queue entries are
retried when it next opens. Failed entries stay failed until you retry them; hover
over **Failed** to read the error. A failed video does not block the rest of the queue.

Files are stored under `~/.local/share/yt-inbox/downloads/VIDEO_ID/` by default.
**Open downloads folder** opens that location. **Remove files** deletes the selected
local copies and partial files, keeping the videos' watched/inbox status. Cancel
downloads and close playback before removing their files. Files are not deleted
automatically when you mark a video watched, so remove them when you need disk space.

A video is marked **Ready** only after a successful download/merge and verification
that the output file exists. Missing files can be downloaded again. Downloading,
playing and removing files never automatically mark a video watched. **Open in
browser** always uses the online YouTube link.

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
- Offline video files: `~/.local/share/yt-inbox/downloads/`
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

Daily checks use YouTube's limited recent feed window; the daily fallback also
reads at most 15 uploads. A long offline period or a very busy channel can leave
gaps in the automatic inbox. The on-demand catalogue can retrieve older uploads
without that limit, but only entries YouTube makes available in its upload list.
Large channels take multiple batches. Uploads added or removed during a scan can
shift page positions; refresh and scan again if the list changed. Cached entries
are retained when an upload disappears from YouTube.
Shorts/live announcements can appear; video-type filtering is not implemented.
Live broadcasts are not recorded by the downloader. Deleted, private, restricted
or otherwise unavailable uploads may fail to play or download. Watching somewhere else
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
latest-video inclusion, catalogue pagination, adding older videos with undo,
baseline handling, status persistence, retry cycles, channel removal/re-addition,
notification delivery/action handling and isolated installation. Regression tests
cover feed outages, fallback ownership validation, partial progress, exact-date
handling and recovery without resetting watched/skipped history. Download tests
cover sequential transfers, failures, cancellation, partial-file retention, queue
recovery, output validation, file removal and offline playback selection. Run:

```sh
python3 -m unittest discover -s tests -v
```

Qt UI verification uses the offscreen backend. Actual YouTube fetching, KDE
notification rendering and mpv streaming still depend on your local session and
network; they cannot be established by fixture tests alone.

Built with Python's standard library plus PySide6. Uses YouTube Atom feeds,
yt-dlp for channel resolution, catalogue lookup and downloads, ffmpeg for audio/video
merging, mpv for streaming and offline playback, and libnotify/systemd
for desktop notifications and scheduling. No API key or subscription required.
