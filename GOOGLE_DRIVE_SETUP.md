# Google Drive setup

This is a one-time setup for the app owner. After it is configured, users can
connect their own Google accounts in **Settings → Google Drive** and select a
folder. They do not need to edit `.env` or use the app owner's Google account.

## Get the Google settings

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and create
   or select a project.
2. Under **APIs & Services → Library**, enable **Google Drive API** and
   **Google Picker API**.
3. Under **Google Auth Platform → Audience** (or **OAuth consent screen**),
   configure the app as **External** and add the scope
   `https://www.googleapis.com/auth/drive.file`. While the app is in **Testing**,
   add each allowed Google account as a test user; Google may expire its sign-in
   after seven days. Follow Google's publishing and verification requirements
   if you want people outside the test-user list to use it.
4. Under **APIs & Services → Credentials**, choose **Create credentials →
   OAuth client ID → Desktop app**. Download its JSON file. From the project
   folder, import it with:

   ```powershell
   .venv\Scripts\python scripts\import_google_client.py "C:\path\to\client_secret.json"
   ```

   This puts `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in the root `.env`
   file and deletes the downloaded JSON. Keep `.env` private; do not commit or
   send it to users.
5. On **Credentials**, choose **Create credentials → API key**. Restrict the
   key to **Google Picker API**. For application restrictions, select
   **Websites** and allow `http://127.0.0.1:5000/*`. If the app uses another
   port, use that port instead.
6. Find the numeric **Project number** on the project's **Dashboard → Project
   info** card (use the number, not the project ID). Add both values to `.env`:

   ```dotenv
   GOOGLE_API_KEY=your-api-key
   GOOGLE_PROJECT_NUMBER=your-numeric-project-number
   ```

   The OAuth import step already added the client ID and secret. If you use
   Docker, put all four values in the `.env` beside `docker-compose.yml`.
7. Restart the app. Open **Settings → Google Drive**, click **Connect Google
   Drive**, sign in with the user's own Google account, then choose a folder.
   Enable **Upload automatically after each download** if desired.

Each user's authorization is stored on the machine running their app. The app
only requests the `drive.file` permission for files and folders selected for it.
