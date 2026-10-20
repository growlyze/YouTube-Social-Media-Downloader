# Local Media Downloader — User Guide

This guide covers the requirements, installation, and everyday use of the
locally hosted downloader. The app runs on your Windows PC and is opened in a
browser at <http://127.0.0.1:5000>. It supports YouTube, Instagram, Facebook,
TikTok, and X/Twitter public media links.

## Requirements

- **Windows** and **Python 3.11 or newer**. Python 3.13 has been tested.
- **Node.js** is recommended. yt-dlp uses a JavaScript runtime to solve
  YouTube challenges. Deno, Bun, or QuickJS can be selected in Settings if
  installed instead.
- **FFmpeg is strongly recommended and required for most downloads.** YouTube
  commonly provides video and audio separately, and FFmpeg merges them. Install
  it from PowerShell:

  ```powershell
  winget install Gyan.FFmpeg
  ```

  The app checks WinGet's install directory automatically. If FFmpeg is missing,
  many videos fail with an explanatory message; install FFmpeg and retry them.
- **Internet access** is needed to discover or download supported media and to
  connect to Google Drive if you use uploads.
- **Google Cloud setup is optional.** It is only needed for Drive uploads.
- Allow enough free disk space for the videos you plan to download. The app does
  not delete local video files when you clear queue or history records.

## Start with Docker (easiest)

With Docker, Python, Node.js, and FFmpeg come bundled in the app's image, so
there is nothing else to install.

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/)
   and start it once.
2. Double-click **`Start.bat`** in the project folder. The first start builds
   the app and takes a few minutes; later starts take seconds. The browser opens
   at <http://127.0.0.1:5000> when the app is ready.
3. Double-click **`Stop.bat`** to stop it. Videos, settings, history, and the
   Google sign-in are kept.

Downloaded videos are saved to the `downloads` folder next to `Start.bat`. To use
another folder, add a line such as `HOST_DOWNLOADS_DIR=D:/Videos` to `.env` and
run `Start.bat` again. In Docker, the download folder is chosen only this way,
so the in-app **Browse…** and **Open Folder** buttons are hidden.

Docker keeps its own database, separate from a Python install of the app, so
videos downloaded with one do not appear in the other's history. Google Drive
setup works the same way (see below); put the Google values in `.env`, not
`data\client_secret.json`. In Docker the Google sign-in is saved in the app's
private data volume instead of Windows Credential Manager.

## Install and start without Docker

1. Install Python and Node.js if they are not already installed. Install
   FFmpeg as described above.
2. Open PowerShell in the project folder and run:

   ```powershell
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   .venv\Scripts\python app.py
   ```

3. Leave that PowerShell window open while using the app.
4. Open <http://127.0.0.1:5000> in your browser.

To stop the app, press **Ctrl+C** in the PowerShell window. To update yt-dlp
later, run:

```powershell
.venv\Scripts\python -m pip install -U "yt-dlp[default]"
```

## Download videos

1. Open **Channel Library**.
2. Enter a YouTube channel URL, `@handle`, or playlist URL and start discovery.
   Discovery runs in the background; the page updates as videos are found.
3. Search or filter the library, select the videos you want, and choose
   **Download selected**.
4. Open **Download Queue** and press **Start queue**. The download queue starts
   paused after every app restart, so you must start it again when you want
   pending downloads to continue.
5. Watch progress in **Download Queue**. Completed videos appear in **Local
   Files** and **Download History**.

By default, videos are organized under the configured download folder by
channel, with Shorts and Live videos in their own subfolders. You can change the
destination in **Settings**. Videos already queued keep the destination they
were assigned when queued.

## Download from a social media link

1. Open **Download Queue**.
2. In **Download from a link**, paste one public URL per line and select
   **Inspect and add to queue**. You can submit up to 20 URLs at once.
3. The app identifies the platform and inspects each link with yt-dlp. Supported
   items appear in the regular queue with progress, retry, local-file, and
   optional Google Drive upload handling.

Supported domains are YouTube (`youtube.com`, `youtu.be`), Instagram, Facebook
(`facebook.com`, `fb.watch`), TikTok, and X/Twitter (`x.com`, `twitter.com`). If
yt-dlp identifies a playlist or collection, the app expands up to the first
100 entries per URL. Larger collections are limited and the page reports that
limit. Quality and container preferences apply when compatible formats are
available.

Support depends on the current yt-dlp extractor and media type; a platform may
not expose every post, collection, or format. This workflow does not sign in,
use account cookies, bypass private/login-required content, evade regional
restrictions, or download DRM-protected media. If a link is unavailable or
requires authentication, the app reports that instead of trying to bypass the
restriction. Respect platform terms and download only media you own or have
permission to save.

### Queue controls

- **Pause queue** pauses an active download and preserves its partial file so it
  can resume later. **Cancel** cancels an item and removes its partial file.
- Temporary network errors are retried with backoff. Some errors—such as
  private, deleted, members-only, age-restricted, geo-blocked, or upcoming
  videos—may not be fixable by retrying.
- A platform rate-limit or verification check pauses automated requests. Follow
  the pause banner's suggested wait, then press **Resume**.
- Re-download is available from **Local Files**. The existing file is retained
  until its replacement finishes successfully.

## Google Drive uploads (optional)

Google Drive uploads require a Google Cloud project, an OAuth Desktop app
client, the Google Drive API, and—when using the folder picker—the Google Picker
API. The app requests the `drive.file` permission rather than access to all of
your Drive.

### Configure Google Cloud

1. Create a project in the [Google Cloud Console](https://console.cloud.google.com).
2. Enable **Google Drive API** and **Google Picker API** under **APIs & Services
   → Library**.
3. Configure the OAuth consent screen as an External app, add the
   `https://www.googleapis.com/auth/drive.file` scope, and add your account as a
   test user if the app is in Testing mode.
4. Create an OAuth client ID of type **Desktop app**. Save the downloaded JSON
   as `data\client_secret.json`, or import it with:

   ```powershell
   .venv\Scripts\python scripts\import_google_client.py path\to\client_secret.json
   ```

   The import script writes the client ID and secret to `.env` and deletes the
   source JSON. Keep `.env` private.
5. Create an API key for Google Picker. Restrict it to the Google Picker API
   and the local app website (by default `http://127.0.0.1:5000/*`).
6. Put the API key and numeric Google Cloud project number in `.env`:

   ```text
   GOOGLE_API_KEY=your-api-key
   GOOGLE_PROJECT_NUMBER=your-numeric-project-number
   ```

   Do not include quotation marks. If you change the app port, update the
   website restriction for the API key to match.
7. Restart the app if you added or changed `.env` values. In **Settings →
   Google Drive**, connect your Google account and choose a Drive folder.

Google may expire sign-in credentials after seven days while an OAuth consent
screen is in Testing mode. Reconnect the account if that happens. The refresh
token is stored in Windows Credential Manager, not in a plain-text app file.

### Upload manually

1. In **Local Files**, select completed videos and press **Upload selected to
   Drive**.
2. Open **Upload Queue** to see progress. Resume or retry uploads there if
   needed.

### Upload automatically

1. Connect Google Drive and select an upload folder in **Settings → Google
   Drive**.
2. Enable **Upload automatically after each download** and save Settings.
3. Newly completed downloads are added to the upload queue. This setting does
   not automatically upload downloads that completed before it was enabled.
4. Turn the setting off to stop adding future downloads automatically. Items
   already in the upload queue remain there.

Uploads have their own queue and retry behavior. A failed upload does not undo
or fail a completed download. If the Google sign-in expires or Drive storage is
full, resolve the issue and resume or retry the upload.

## Settings and local data

- **Settings** controls the download folder, quality, container, request delays,
  retry limits, discovery options, JavaScript runtime, and Drive configuration.
- Default database: `data\app.db`
- Default logs: `data\logs\app.log`
- Default downloads: `downloads\`
- Override data folder, download folder, and port with `YTDL_DATA_DIR`,
  `YTDL_DOWNLOADS_DIR`, and `YTDL_PORT`. These can be set in `.env` or in the
  Windows environment; existing environment variables take precedence.
- Do not share `.env` or OAuth client secrets. Logs rotate and redact common
  OAuth token strings, but check logs for personal information before sharing.

## Troubleshooting

| Problem | What to try |
|---|---|
| `Start.bat` says Docker is not running | Open Docker Desktop, wait until it shows "Engine running", then run `Start.bat` again. |
| The page will not open | With Docker, run `docker compose logs` in the project folder. Without Docker, confirm `app.py` is still running in PowerShell and open `http://127.0.0.1:5000`. If the port is in use, set `YTDL_PORT` and restart the app. |
| Many videos fail because FFmpeg is required | Install FFmpeg with `winget install Gyan.FFmpeg`, then retry failed items. |
| JavaScript challenge or runtime error | Install Node.js or another supported runtime, select it in Settings, and update yt-dlp. |
| Platform requests are paused | Wait for the cooldown shown in the banner, then press **Resume**. Discovery and downloads share this pause. |
| A media item is unavailable or restricted | Check the failure message. Private, login-required, members-only, age-restricted, geo-blocked, DRM-protected, and upcoming content may not be accessible. |
| A download stopped or the app restarted | Start the download queue. Interrupted downloads return to pending and can reuse their partial files. |
| A Drive upload failed | Check **Upload Queue**, reconnect Google Drive if required, confirm folder access or available storage, then resume or retry. |
| A download folder cannot be selected | Confirm the drive exists and your Windows account can create files there. Settings checks write access when saving the folder. |

The default log file is `data\logs\app.log`. The app is designed for local use
and binds only to `127.0.0.1`.

## Tests

Run the test suite from PowerShell in the project folder:

```powershell
.venv\Scripts\python -m pytest -q
```
