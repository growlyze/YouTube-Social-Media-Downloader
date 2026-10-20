// Local Files page: completed downloads on disk, with manual Google Drive upload.
"use strict";

const $ = (id) => document.getElementById(id);
let page = 1;
let items = [];
const chosen = new Set();      // download ids ticked for upload (kept across pages)

async function openFolder(downloadId) {
  try {
    await api("/api/local/open", { method: "POST", body: { download_id: downloadId } });
  } catch (err) { toast(err.message, "error"); }
}

async function redownload(item) {
  if (!confirm(`Download "${item.title}" again? The current file is kept until the new download finishes.`)) return;
  try {
    const result = await api("/api/downloads/enqueue", { method: "POST", body: { video_ids: [item.video_id], redownload: true } });
    toast(result.added ? "Queued for re-download." : "It is already in the queue.");
    load();
  } catch (err) { toast(err.message, "error"); }
}

const UPLOAD_LABELS = {
  pending: ["Queued", "badge-muted"], uploading: ["Uploading", "badge-short"],
  completed: ["Uploaded", "badge-ok"], failed: ["Upload failed", "badge-bad"], cancelled: ["Cancelled", "badge-muted"],
};

function driveCell(item) {
  const up = item.upload;
  if (!up) return el("span", { class: "muted small" }, "Not uploaded");
  const [label, cls] = UPLOAD_LABELS[up.status] || [up.status, "badge-muted"];
  const text = up.status === "uploading" && up.progress !== null ? `${label} ${Math.round(up.progress)}%` : label;
  const badge = el("span", { class: `badge ${cls}`, title: up.error_message || "" }, text);
  return up.status === "completed" && up.web_link
    ? el("a", { href: up.web_link, target: "_blank", rel: "noopener noreferrer" }, badge)
    : badge;
}

function row(item) {
  const fileName = (item.file_path || "").split(/[\\/]/).pop();
  const box = el("input", { type: "checkbox", "aria-label": "Select for upload", checked: chosen.has(item.id),
    disabled: !item.file_exists,
    onchange: (e) => { e.target.checked ? chosen.add(item.id) : chosen.delete(item.id); updateUploadButton(); } });
  return el("tr", {},
    el("td", { class: "col-check" }, box),
    el("td", { class: "ellipsis" },
      el("a", { href: item.url, target: "_blank", rel: "noopener noreferrer", title: item.title }, item.title || item.video_id),
      el("div", { class: "muted small ellipsis path", title: item.file_path || "" },
        item.file_exists ? fileName : el("span", { class: "text-bad" }, "Missing on disk")),
      item.error_message ? el("div", { class: "muted small wrap" }, item.error_message) : null),
    el("td", {}, item.format_desc || ""),
    el("td", {}, formatBytes(item.file_size)),
    el("td", { class: "small" }, item.finished_at || ""),
    el("td", {}, driveCell(item)),
    el("td", { class: "nowrap" },
      item.file_exists ? el("button", { type: "button", class: "btn btn-small", onclick: () => openFolder(item.id) }, "Show") : null,
      " ",
      el("button", { type: "button", class: "btn btn-small", onclick: () => redownload(item) }, "Re-download")),
  );
}

function updateUploadButton() {
  const button = $("upload-selected");
  button.disabled = chosen.size === 0;
  button.textContent = chosen.size ? `Upload selected to Drive (${chosen.size})` : "Upload selected to Drive";
  const selectable = items.filter((i) => i.file_exists);
  $("local-all").checked = selectable.length > 0 && selectable.every((i) => chosen.has(i.id));
}

let refreshTimer;
async function load() {
  clearTimeout(refreshTimer);
  const params = new URLSearchParams({ page, page_size: 50 });
  const q = $("local-q").value.trim();
  if (q) params.set("q", q);
  try {
    const data = await api(`/api/local?${params}`);
    items = data.items;
    $("local-rows").replaceChildren(...items.map(row));
    $("local-empty").hidden = data.total > 0;
    $("local-count").textContent = `${data.total} file(s)`;
    $("local-page").textContent = `Page ${data.page} of ${data.pages}`;
    $("local-prev").disabled = data.page <= 1;
    $("local-next").disabled = data.page >= data.pages;
    updateUploadButton();
    if (items.some((i) => i.upload && (i.upload.status === "pending" || i.upload.status === "uploading"))) {
      refreshTimer = setTimeout(load, 3000);
    }
  } catch (err) { toast(err.message, "error"); }
}

$("local-all").addEventListener("change", (e) => {
  for (const item of items.filter((i) => i.file_exists)) {
    e.target.checked ? chosen.add(item.id) : chosen.delete(item.id);
  }
  $("local-rows").replaceChildren(...items.map(row));
  updateUploadButton();
});

$("upload-selected").addEventListener("click", async () => {
  if (!chosen.size) return;
  try {
    const r = await api("/api/uploads/enqueue", { method: "POST", body: { download_ids: [...chosen] } });
    const notes = [`${r.added} added to the upload queue`];
    if (r.already_uploaded) notes.push(`${r.already_uploaded} already uploaded to this folder`);
    if (r.already_queued) notes.push(`${r.already_queued} already queued`);
    if (r.not_downloaded) notes.push(`${r.not_downloaded} not fully downloaded`);
    let msg = notes.join(", ") + ".";
    if (r.added && !r.queue_running) msg += " Uploads are paused; resume them on the Upload Queue page.";
    toast(msg, r.added ? "info" : "warn");
    chosen.clear();
    load();
  } catch (err) { toast(err.message, "error"); }
});

let debounce;
$("local-q").addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(() => { page = 1; load(); }, 300); });
$("local-prev").addEventListener("click", () => { page--; load(); });
$("local-next").addEventListener("click", () => { page++; load(); });
load();
