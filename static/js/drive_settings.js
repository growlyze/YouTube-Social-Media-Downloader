// Settings → Google Drive: setup checklist, connect/disconnect, folder choice.
"use strict";

const D = (id) => document.getElementById(id);
let driveState = null;

function stepState(id, done, text, isError = false) {
  const li = D(id);
  li.classList.toggle("step-done", !!done);
  li.classList.toggle("step-error", !!isError);
  if (text !== undefined) {
    const target = { "step-client": "drive-client-state", "step-connect": "drive-account", "step-folder": "drive-folder" }[id];
    if (target) D(target).textContent = text;
  }
}

async function loadDriveStatus() {
  let s;
  try { s = await api("/api/drive/status?account=1"); } catch (err) { toast(err.message, "error"); return; }
  driveState = s;
  stepState("step-client", s.configured,
    s.configured ? (s.client_source === "env" ? "set in .env" : "found (client_secret.json)") : `${s.config_error}`,
    !s.configured);
  stepState("step-picker", s.picker_ready);
  D("drive-picker-state").textContent = s.picker_ready
    ? (s.picker_from_env ? "set in .env" : "set (legacy setting; move it to .env)") : "missing in .env";

  if (!s.configured) {
    stepState("step-connect", false, "add the OAuth client file first");
  } else if (s.connected && s.account) {
    stepState("step-connect", true, `connected as ${s.account.email}` +
      (s.account.quota_limit ? ` · ${formatBytes(s.account.quota_usage)} of ${formatBytes(s.account.quota_limit)} used` : ""));
  } else if (s.connected) {
    stepState("step-connect", false, s.account_error || "connected, but the account could not be checked", !!s.account_error);
  } else {
    stepState("step-connect", false, "not connected");
  }
  D("drive-connect").hidden = !s.configured || (s.connected && !s.account_error);
  D("drive-connect").textContent = s.connected ? "Reconnect Google Drive" : "Connect Google Drive";
  D("drive-disconnect").hidden = !s.connected;

  stepState("step-folder", !!s.folder, s.folder ? `“${s.folder.name}”` : "not chosen");
  const ready = s.connected && !s.account_error;
  D("drive-pick").disabled = !(ready && s.picker_ready);
  D("drive-pick").title = s.picker_ready ? "" : "Set GOOGLE_API_KEY and GOOGLE_PROJECT_NUMBER in .env first";
  D("drive-create-folder").disabled = !ready;
  const autoUpload = D("auto-upload");
  autoUpload.disabled = !autoUpload.checked && !(s.connected && s.folder);
  D("auto-upload-help").textContent = autoUpload.disabled
    ? "Connect Google Drive and choose an upload folder before enabling automatic uploads."
    : "Automatic uploads use the independent upload queue, so failures do not affect downloaded files.";
}

D("drive-connect").addEventListener("click", async () => {
  try {
    const { auth_url } = await api("/api/drive/connect", { method: "POST" });
    location.href = auth_url;   // Google's consent page, then back to /oauth2/callback
  } catch (err) { toast(err.message, "error"); }
});

D("drive-disconnect").addEventListener("click", async () => {
  if (!confirm("Disconnect Google Drive? The app's access is revoked and the saved sign-in is deleted " +
               "from this PC. Files already uploaded stay in your Drive.")) return;
  try {
    await api("/api/drive/disconnect", { method: "POST" });
    toast("Google Drive disconnected.");
    loadDriveStatus();
  } catch (err) { toast(err.message, "error"); }
});

async function saveDriveFolder(id, name) {
  if (!confirm(`Upload videos to this Google Drive folder?\n\n${name}`)) return;
  try {
    const { folder } = await api("/api/drive/folder", { method: "PUT", body: { id } });
    toast(`Uploads will go to “${folder.name}”.`);
    loadDriveStatus();
  } catch (err) { toast(err.message, "error"); }
}

// -- Google Picker -------------------------------------------------------------

let pickerLoaded = null;
function loadPickerLibrary() {
  if (!pickerLoaded) {
    pickerLoaded = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "https://apis.google.com/js/api.js";
      script.onload = () => window.gapi.load("picker", { callback: resolve, onerror: reject });
      script.onerror = () => reject(new Error("Could not load the Google Picker (check your internet connection)."));
      document.head.append(script);
    });
  }
  return pickerLoaded;
}

D("drive-pick").addEventListener("click", async () => {
  try {
    const cfg = await api("/api/drive/picker-config", { method: "POST" });
    await loadPickerLibrary();
    const view = new google.picker.DocsView(google.picker.ViewId.FOLDERS)
      .setIncludeFolders(true)
      .setSelectFolderEnabled(true)
      .setMimeTypes("application/vnd.google-apps.folder");
    const picker = new google.picker.PickerBuilder()
      .setTitle("Choose the Drive folder for uploads")
      .addView(view)
      .setOAuthToken(cfg.token)
      .setDeveloperKey(cfg.api_key)
      .setAppId(cfg.app_id)              // grants this app (drive.file) access to the chosen folder
      .setOrigin(`${location.protocol}//${location.host}`)
      .setCallback((data) => {
        if (data[google.picker.Response.ACTION] === google.picker.Action.PICKED) {
          const doc = data[google.picker.Response.DOCUMENTS][0];
          saveDriveFolder(doc[google.picker.Document.ID], doc[google.picker.Document.NAME]);
        }
      })
      .build();
    picker.setVisible(true);
  } catch (err) { toast(err.message || String(err), "error"); }
});

D("drive-create-folder").addEventListener("click", async () => {
  const name = D("drive-new-folder").value.trim();
  if (!name) { toast("Type a folder name first.", "warn"); return; }
  if (!confirm(`Create “${name}” in your My Drive and upload videos there?`)) return;
  try {
    const { folder } = await api("/api/drive/folder/create", { method: "POST", body: { name } });
    D("drive-new-folder").value = "";
    toast(`Created “${folder.name}”. Uploads will go there.`);
    loadDriveStatus();
  } catch (err) { toast(err.message, "error"); }
});

// Messages from the OAuth redirect.
const params = new URLSearchParams(location.search);
if (params.get("drive") === "connected") toast("Google Drive connected.");
if (params.get("drive_error")) toast(params.get("drive_error"), "error");
if (params.has("drive") || params.has("drive_error")) history.replaceState(null, "", "/settings#drive");

loadDriveStatus();
