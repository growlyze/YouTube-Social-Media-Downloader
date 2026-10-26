// Shared helpers: API calls with the CSRF token, toasts, and the pause banner.
"use strict";

const CSRF = document.querySelector('meta[name="csrf-token"]').content;

async function api(path, { method = "GET", body } = {}) {
  const opts = { method, headers: { "X-CSRF-Token": CSRF } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let resp;
  try {
    resp = await fetch(path, opts);
  } catch (err) {
    throw new Error("Cannot reach the local server. Is app.py still running?");
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `Request failed (${resp.status})`);
  return data;
}

let toastTimer;
function toast(message, kind = "info") {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.className = `toast toast-${kind}`;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, kind === "error" ? 8000 : 4000);
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "";
  const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(bytes), unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
  return `${value.toFixed(unit >= 2 ? 1 : 0)} ${units[unit]}`;
}

function formatEta(seconds) {
  if (seconds === null || seconds === undefined) return "";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function formatDate(yyyymmdd) {
  if (!yyyymmdd || yyyymmdd.length !== 8) return "";
  return `${yyyymmdd.slice(0, 4)}-${yyyymmdd.slice(4, 6)}-${yyyymmdd.slice(6, 8)}`;
}

function platformLabel(platform) {
  return ({ youtube: "YouTube", instagram: "Instagram", facebook: "Facebook",
    tiktok: "TikTok", x: "X (Twitter)" })[platform] || platform || "YouTube";
}

// ---- download folder ----------------------------------------------------

// The page cannot see local paths itself, so the local backend lists drives and
// folders (names only) and the user browses them in this in-page window.
// Selecting asks for confirmation; only then is the folder saved (after the
// backend creates it if needed and checks write access).
const folderUI = {
  dialog: document.getElementById("folder-dialog"),
  path: null,
  current: "",
};

function setFolderMessage(text, isError = false) {
  const node = document.getElementById("folder-message");
  node.textContent = text || "";
  node.classList.toggle("text-bad", isError);
}

async function browseFolder(path) {
  let data;
  try {
    data = await api("/api/folders/list", { method: "POST", body: path ? { path } : {} });
  } catch (err) { setFolderMessage(err.message, true); return; }
  folderUI.current = data.current;
  document.querySelector(".js-download-dir-label").textContent = data.current;
  const drives = document.getElementById("folder-drives");
  drives.replaceChildren(el("span", { class: "muted small" }, "Drives:"), ...data.drives.map((d) =>
    el("button", { type: "button", class: "btn btn-small" + (data.path && data.path.toUpperCase().startsWith(d.path.toUpperCase()) ? " active-drive" : ""),
      onclick: () => browseFolder(d.path) }, d.name)));
  folderUI.path = data.path;
  document.getElementById("folder-path").value = data.path || "";
  document.getElementById("folder-up").disabled = !data.parent;
  document.getElementById("folder-up").onclick = () => browseFolder(data.parent);
  document.getElementById("folder-select").disabled = !data.path;
  document.getElementById("folder-create").disabled = !data.path;
  const list = document.getElementById("folder-list");
  list.replaceChildren(...data.folders.map((f) => el("li", {},
    el("button", { type: "button", class: "folder-item", title: f.path,
      onclick: () => browseFolder(f.path) }, "📁 " + f.name))));
  if (!data.path) setFolderMessage("Choose a drive to start.");
  else if (!data.folders.length) setFolderMessage("No sub-folders here. You can select this folder or create a new one.");
  else setFolderMessage(data.truncated ? "Showing the first 2000 folders." : "");
}

async function saveDownloadFolder(path) {
  const ok = confirm(
    `Save new downloads to this folder?\n\n${path}\n\nCurrent folder: ${folderUI.current}\n\n` +
    "The folder is created if needed and checked for write access. " +
    "Videos already in the queue keep the folder they were queued with.");
  if (!ok) return false;
  try {
    const { settings } = await api("/api/settings", { method: "PUT", body: { download_dir: path } });
    for (const node of document.querySelectorAll(".js-download-dir")) {
      if (node.tagName === "INPUT") node.value = settings.download_dir; else node.textContent = settings.download_dir;
    }
    toast(`Downloads will be saved to ${settings.download_dir}`);
    return true;
  } catch (err) { setFolderMessage(err.message, true); toast(err.message, "error"); return false; }
}

function chooseDownloadFolder() {
  setFolderMessage("");
  document.getElementById("folder-new-name").value = "";
  folderUI.dialog.showModal();
  browseFolder(null);
}

document.getElementById("folder-close").addEventListener("click", () => folderUI.dialog.close());
document.getElementById("folder-go").addEventListener("click", () =>
  browseFolder(document.getElementById("folder-path").value.trim()));
document.getElementById("folder-path").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); browseFolder(e.target.value.trim()); }
});
document.getElementById("folder-create").addEventListener("click", async () => {
  const name = document.getElementById("folder-new-name").value.trim();
  if (!folderUI.path || !name) { setFolderMessage("Type a name for the new folder first.", true); return; }
  try {
    const { path } = await api("/api/folders/create", { method: "POST", body: { parent: folderUI.path, name } });
    document.getElementById("folder-new-name").value = "";
    await browseFolder(path);
    setFolderMessage(`Created ${path}. Press "Select this folder" to use it.`);
  } catch (err) { setFolderMessage(err.message, true); }
});
document.getElementById("folder-select").addEventListener("click", async () => {
  if (folderUI.path && await saveDownloadFolder(folderUI.path)) folderUI.dialog.close();
});
document.getElementById("folder-native").addEventListener("click", async () => {
  setFolderMessage("A Windows folder window is opening on your desktop (check the taskbar if hidden)…");
  try {
    const picked = await api("/api/settings/pick-folder", { method: "POST" });
    if (picked.cancelled) { setFolderMessage("No folder chosen in the Windows dialog."); return; }
    await browseFolder(picked.path);
  } catch (err) { setFolderMessage(err.message, true); }
});

for (const button of document.querySelectorAll(".js-choose-folder")) {
  button.addEventListener("click", chooseDownloadFolder);
}

// "Open Folder": show the current download folder in Windows Explorer.
async function openDownloadFolder() {
  try {
    const { path } = await api("/api/local/open", { method: "POST", body: {} });
    toast(`Opened ${path} in Windows Explorer.`);
  } catch (err) { toast(err.message, "error"); }
}

for (const button of document.querySelectorAll(".js-open-folder")) {
  button.addEventListener("click", openDownloadFolder);
}

// Any page URL ending in #choose-folder opens the folder browser directly.
if (location.hash === "#choose-folder") chooseDownloadFolder();

function currentDownloadDir() {
  const node = document.querySelector(".js-download-dir");
  return node ? (node.value || node.textContent).trim() : "";
}

// ---- pause banner -------------------------------------------------------

function renderPause(pause) {
  const banner = document.getElementById("pause-banner");
  if (!pause || !pause.paused) { banner.hidden = true; return; }
  banner.hidden = false;
  document.getElementById("pause-message").textContent = pause.message || "";
  const mins = Math.ceil((pause.cooldown_remaining || 0) / 60);
  document.getElementById("pause-cooldown").textContent = mins > 0
    ? `Suggested wait: about ${mins} more minute(s).`
    : "The suggested wait has passed; you can resume.";
}

// Low free space (local disk or Google Drive) stops a queue; the Space Check
// page continues it automatically once a check finds space again.
function renderSpaceBanner(space) {
  const banner = document.getElementById("space-banner");
  const blocked = space && (space.local_pause?.paused || space.drive_pause?.paused);
  if (!blocked) { banner.hidden = true; return; }
  banner.hidden = false;
  const message = space.local_pause?.paused ? space.local_pause.message : space.drive_pause?.message;
  document.getElementById("space-banner-message").textContent = message || "";
}

async function refreshPause() {
  try {
    const status = await api("/api/downloads/status");
    renderPause(status.pause);
    renderSpaceBanner(status.space);
    const queued = status.counts.pending + status.counts.downloading;
    const badge = document.getElementById("nav-queue-count");
    badge.hidden = queued === 0;
    badge.textContent = queued;
    return status;
  } catch (_) { return null; /* server down */ }
}

document.getElementById("pause-resume").addEventListener("click", async () => {
  const banner = document.getElementById("pause-cooldown").textContent;
  if (banner.startsWith("Suggested wait: about") &&
      !confirm("A platform recently rate-limited this connection. Resuming early may extend the block. Resume anyway?")) {
    return;
  }
  try {
    renderPause(await api("/api/queue/resume", { method: "POST" }));
    toast("Platform requests resumed.");
  } catch (err) { toast(err.message, "error"); }
});

refreshPause();
setInterval(refreshPause, 15000);
