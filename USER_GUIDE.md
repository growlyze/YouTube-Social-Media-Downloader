# Local YouTube Bulk Downloader — User Guide

This guide covers the requirements, installation, and everyday use of the
locally hosted downloader. The app runs on your Windows PC and is opened in a
browser at <http://127.0.0.1:5000>.

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
- **Internet access** is needed to discover and download YouTube videos, and to
  connect to Google Drive if you use uploads.
- **Google Cloud setup is optional.** It is only needed for Drive uploads.
- Allow enough free disk space for the videos you plan to download. The app does
  not delete local video files when you clear queue or history records.

## Install and start

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

### Queue controls

- **Pause queue** pauses an active download and preserves its partial file so it
  can resume later. **Cancel** cancels an item and removes its partial file.
- Temporary network errors are retried with backoff. Some errors—such as
  private, deleted, members-only, age-restricted, geo-blocked, or upcoming
  videos—may not be fixable by retrying.
- A YouTube rate-limit or bot-check pauses YouTube activity. Follow the pause
  banner's suggested wait, then press **Resume**.
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
| The page will not open | Confirm `app.py` is still running in PowerShell and open `http://127.0.0.1:5000`. If the port is in use, set `YTDL_PORT` and restart the app. |
| Many videos fail because FFmpeg is required | Install FFmpeg with `winget install Gyan.FFmpeg`, then retry failed items. |
| JavaScript challenge or runtime error | Install Node.js or another supported runtime, select it in Settings, and update yt-dlp. |
| YouTube requests are paused | Wait for the cooldown shown in the banner, then press **Resume**. Discovery and downloads share this pause. |
| A video is unavailable or restricted | Check the failure message. Private, deleted, members-only, age-restricted, geo-blocked, and upcoming content may not be accessible. |
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
