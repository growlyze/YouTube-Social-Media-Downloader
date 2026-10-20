# Local YouTube Bulk Downloader

A free, locally hosted web app (Flask + yt-dlp + SQLite) for listing a YouTube
channel's videos and Shorts, selecting them, downloading in bulk, and uploading
to Google Drive manually or automatically. It runs on your PC and opens in Chrome at
<http://127.0.0.1:5000>. Nothing is exposed to the internet.

For requirements, installation, and step-by-step instructions, see the
[User Guide](USER_GUIDE.md).

## Status

| Phase | Feature | State |
|---|---|---|
| 2 | Project structure, SQLite, channel/playlist/Shorts discovery, Channel Library (search, filters, selection), Settings, rate-limit pause | **Done** |
| 3 | Download queue, progress, cancel/retry/resume, duplicate prevention, local files, history | **Done** |
| 4 | Google Drive OAuth, folder choice, manual + resumable uploads | **Done** (needs your Google Cloud setup) |
| 5 | Automatic upload queue | **Done** |
| 6 | Full documentation and troubleshooting | **Done** |

## Setup (Windows)

1. Python 3.11+ (3.13 tested) and Node.js (yt-dlp uses it to solve YouTube's
   JavaScript challenges; you can choose Deno/Bun/QuickJS in Settings instead).
2. **FFmpeg (required for downloading).** YouTube serves video and audio as
   separate streams that FFmpeg merges; without it most videos fail with a
   clear "FFmpeg is required" message. Install with `winget install Gyan.FFmpeg`.
   The app also looks in WinGet's install folder, so no restart is needed.
3. In this folder:

   ```powershell
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   .venv\Scripts\python app.py
   ```

4. Open <http://127.0.0.1:5000> in Chrome.

Keep yt-dlp current; YouTube changes often:
`.venv\Scripts\python -m pip install -U "yt-dlp[default]"`.

## Where things are stored

| Path | Contents |
|---|---|
| `data\app.db` | SQLite: sources, videos, selection, jobs, settings, pause state |
| `data\logs\app.log` | Rotating log (OAuth tokens are redacted) |
| `downloads\` | Default download folder (configurable in Settings) |

Override with environment variables `YTDL_DATA_DIR`, `YTDL_DOWNLOADS_DIR`, `YTDL_PORT`.
`data\` and `downloads\` are git-ignored.

The `.env` file is optional. It can hold `YTDL_DATA_DIR`, `YTDL_DOWNLOADS_DIR`,
`YTDL_PORT`, and Google configuration values. Environment variables already set
in Windows take precedence over values in `.env`. Do not share or publish `.env`:
it may contain Google client secrets.

## Downloading

1. In the Channel Library, select videos and press **Download selected**.
2. On **Download Queue**, press **Start queue**. The queue starts paused, and it
   also comes back paused after an app restart, so nothing downloads unexpectedly.
3. Files are saved as
   `<download folder>\<Channel>\[Shorts\|Live\]<YYYY-MM-DD> - <title> [<video id>].<ext>`.

How the queue behaves:

- **One download at a time** by default, with a random 5–15 s pause between
  downloads (Settings).
- **No duplicates.** A video id can only be in the queue once. Videos already
  downloaded, or already on disk with their `[id]` in the file name, are skipped
  and existing files are never overwritten. *Re-download* (Local Files) keeps the
  old file until the new one has fully finished.
- **Pause and resume.** *Pause queue* stops the current download but keeps its
  `.part` file, and yt-dlp resumes from it later. *Cancel* stops the download and
  deletes the partial file.
- **Retries.** Network-type errors are retried up to *Max retries* times, waiting
  1, 2 and then 4 minutes. Private, deleted, members-only, age-restricted and
  geo-blocked videos fail at once with a clear message.
- **Only verified files count.** A download is marked completed only when the
  final file exists, is not empty, has no leftover `.part` file and (with FFmpeg)
  passes `ffprobe`. After a crash, unfinished items go back to *pending*.
- **Exact metadata.** Upload date and duration are corrected from the full
  video page when each download finishes.

## Google Drive (manual and automatic uploads)

### One-time Google Cloud setup (free, no billing needed)

1. Create a project at <https://console.cloud.google.com>.
2. **APIs & Services → Library**: enable **Google Drive API** and **Google Picker API**.
3. **OAuth consent screen**: User type **External**, fill in the app name and your
   email, add the scope `https://www.googleapis.com/auth/drive.file`, and add your
   Google account under **Test users**.
   - While the app is in *Testing*, Google expires the sign-in after 7 days.
   - Switching it to *In production* avoids that, and `drive.file` is a
     non-sensitive scope, so Google doesn't need to review it.
4. **Credentials → Create credentials → OAuth client ID → Desktop app**. Either
   save the downloaded JSON as `data\client_secret.json`, or import it from
   PowerShell with
   `.venv\Scripts\python scripts\import_google_client.py path\to\client_secret.json`.
   The import script copies the client ID and secret into `.env`, then deletes
   the source JSON. Keep `.env` private.
5. **Credentials → Create credentials → API key**, then restrict it:
   - *Application restrictions*: **Websites**, add `http://127.0.0.1:5000/*`
   - *API restrictions*: **Google Picker API** only
6. Copy the **project number** (Dashboard → Project info). Use the digits, not the project ID.
7. In the app: **Settings → Google Drive**. Paste the API key and project number,
   press **Save settings**, then click **Connect Google Drive** and then
   **Choose Drive folder…**.

### How it works

- **Scope.** Only `drive.file` is requested. The app can see and change files it
  uploaded and the folder you pick in Google's Picker, nothing else in your Drive.
- **Your sign-in.** The refresh token is stored in **Windows Credential Manager**
  ("YT-Bulk-Downloader/google-drive"), never in a file. **Disconnect** revokes it
  at Google and deletes it from this PC.
- **Uploading.** In **Local Files**, tick videos and press **Upload selected to Drive**.
  Progress and errors show on **Upload Queue**.
- **Automatic uploads.** In **Settings → Google Drive**, connect the account and
  choose a Drive folder, then enable **Upload automatically after each download**
  and save settings. Every newly completed download is added to the independent
  upload queue, including a video that was already on disk and therefore did not
  need downloading again. Automatic uploads use the folder selected at completion
  time. Disable the setting to stop adding future downloads; items already in the
  upload queue remain there.
- **Reliability.** Uploads are resumable and sent in 8 MB chunks. A failed chunk
  is re-sent, and temporary errors are retried with backoff. If Drive is full or
  the sign-in is rejected, uploads pause until you fix it and press Resume.
- **No duplicates.** Each uploaded file is tagged with its YouTube video ID, so
  the same video is never uploaded twice to the same folder.
- **Independent of downloads.** Uploads never change a download. A failed upload
  can be retried without downloading again.
- **Setup requirement.** Automatic uploads can only be enabled after connecting
  Google Drive and selecting a folder. If the connection later expires, downloads
  still complete; the upload queue reports the error and can be resumed after
  reconnecting.

## Rate limits

- Discovery runs one job at a time in a background thread; requests are spaced
  by the *Delay between requests* setting (default 1 s).
- HTTP 429, "try again later", or "Sign in to confirm you're not a bot" **pauses
  all YouTube activity** (saved in the database, so it survives restarts). A
  banner shows the reason and a suggested wait; you resume manually.
- The same pause applies to downloads. The video that hit the limit returns to
  *pending* without using up one of its retries. Three HTTP 403 errors in a row
  also pause the queue.
- No proxies, IP rotation or paid services are used.

## Security

- Binds to `127.0.0.1` only; requests with any other `Host` header are rejected.
- State-changing API calls need a per-run CSRF token embedded in the pages and
  are refused when sent cross-origin.
- Strict Content-Security-Policy; video titles are rendered as text, never HTML.

## Known limitations

- YouTube's Shorts tab does not provide duration or upload date in fast listing
  mode, so Shorts show no duration/date and are excluded by duration/date filters.
- Upload dates for regular videos come from "3 weeks ago"-style text and are
  approximate (shown with `~`). Exact dates are filled in when a video is downloaded.
- Changing *Concurrent downloads* takes effect for the next download; 1 is
  strongly recommended.
- "Show in folder" and "Open folder" use Windows Explorer.

## Troubleshooting

- **The app does not start or the page does not load.** Confirm the virtual
  environment dependencies were installed and `app.py` is still running in its
  PowerShell window. Open `http://127.0.0.1:5000`; if that port is in use, set
  `YTDL_PORT` in `.env` and restart the app.
- **Most downloads fail with an FFmpeg message.** Install FFmpeg with
  `winget install Gyan.FFmpeg`, then retry failed downloads. The app checks
  WinGet's install folders automatically.
- **yt-dlp reports a JavaScript challenge/runtime error.** Install Node.js, or
  install another supported runtime and select it in Settings. Update yt-dlp
  with `.venv\Scripts\python -m pip install -U "yt-dlp[default]"`.
- **YouTube requests are paused.** Read the pause banner and wait for the
  suggested cooldown before pressing **Resume**. A 429 or bot check pauses both
  discovery and downloads. Do not use proxies or request rotation.
- **A video fails immediately.** Private, deleted, members-only, age-restricted,
  geo-blocked, upcoming, or otherwise unavailable videos may not be downloadable
  with the current account and settings. The queue's error message gives the
  category; retrying does not remove the restriction.
- **A download is stuck or was interrupted.** Pause and resume to keep and reuse
  its `.part` file. Cancel deletes partial files. After an app restart, active
  downloads return to pending, but the queue starts paused; press **Start queue**
  to continue.
- **A Drive upload fails.** Downloads remain intact. Check **Upload Queue** for
  the error. Reconnect if authorization expired, confirm the selected folder is
  still accessible, or free Drive storage if it is full; then resume or retry the
  upload. You can also retry it from Local Files without downloading again.
- **An output folder cannot be selected or written.** Verify the drive is
  available and the Windows account can create files there. Settings validates
  the destination by creating and removing a small test file.
- **A completed file is missing.** The database records its path, but removing a
  file externally does not delete its history entry. Check the configured
  download folder and available disk space before retrying.

Application logs are at `data\logs\app.log` by default (or
`%YTDL_DATA_DIR%\logs\app.log` when `YTDL_DATA_DIR` is set). Logs rotate and
redact common OAuth token strings. Do not post logs publicly without checking
them for personal information.

## Tests

```powershell
.venv\Scripts\python -m pytest -q
```

## Credits

- [yt-dlp](https://github.com/yt-dlp/yt-dlp) (Unlicense) is the download engine.
- Parts of `services/youtube_service.py` are adapted from
  [YT Channel Downloader](https://github.com/hyperfield/yt-channel-downloader),
  Copyright (c) 2024 hyperfield, MIT License. See
  `third_party_licenses/yt-channel-downloader-LICENSE.txt`.
