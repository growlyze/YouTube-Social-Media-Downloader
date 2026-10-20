// Upload Queue page: progress, controls, failures and recent uploads.
"use strict";

const U = (id) => document.getElementById(id);
let upTimer;

function uploadRow(item) {
  const active = item.status === "uploading";
  const bar = el("div", { class: "progress-bar" });
  bar.style.width = `${Math.min(100, item.progress ?? 0)}%`;   // CSP: no inline style attributes
  const bits = [];
  if (active || item.status === "completed") {
    if (item.progress !== null && item.status !== "completed") bits.push(`${item.progress.toFixed(1)}%`);
    if (item.uploaded_bytes) bits.push(formatBytes(item.uploaded_bytes) + (item.total_bytes && active ? ` / ${formatBytes(item.total_bytes)}` : ""));
    if (active && item.speed) bits.push(`${formatBytes(item.speed)}/s`);
  }
  if (item.status === "pending" && item.next_attempt_at) bits.push(`retry at ${item.next_attempt_at} UTC`);
  const actions = el("div", { class: "dl-actions" });
  if (item.status === "pending" || active) {
    actions.append(el("button", { type: "button", class: "btn btn-small", onclick: () => act(item, "cancel") }, "Cancel"));
  }
  if (item.status === "failed" || item.status === "cancelled") {
    actions.append(el("button", { type: "button", class: "btn btn-small", onclick: () => act(item, "retry") }, "Retry"));
  }
  if (item.status === "completed" && item.web_link) {
    actions.append(el("a", { class: "btn btn-small", href: item.web_link, target: "_blank", rel: "noopener noreferrer" }, "Open in Drive"));
  }
  if (!active) {
    actions.append(el("button", { type: "button", class: "btn btn-small btn-danger", title: "Remove from this list (Drive files are not deleted)",
      onclick: () => act(item, "remove") }, "Remove"));
  }
  return el("div", { class: `dl-row dl-${item.status}` },
    el("img", { class: "dl-thumb", src: item.thumbnail, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }),
    el("div", { class: "dl-main" },
      el("div", { class: "dl-title" }, item.title || item.video_id),
      el("div", { class: "muted small" }, `${item.channel_name || ""} → Drive folder “${item.drive_folder_name || item.drive_folder_id}”`),
      active ? el("div", { class: "progress" }, bar) : null,
      el("div", { class: "small" }, el("span", { class: `pill pill-${item.status}` }, item.status), " ", bits.join(" · ")),
      item.error_message ? el("div", { class: "small " + (item.status === "failed" ? "text-bad" : "muted") }, item.error_message) : null,
    ),
    actions);
}

async function act(item, action) {
  try {
    if (action === "remove") await api(`/api/uploads/${item.id}`, { method: "DELETE" });
    else await api(`/api/uploads/${item.id}/${action}`, { method: "POST" });
    refreshUploads();
  } catch (err) { toast(err.message, "error"); }
}

function fill(listId, emptyId, data) {
  U(listId).replaceChildren(...data.items.map(uploadRow));
  U(emptyId).hidden = data.total > 0;
}

async function refreshUploads() {
  clearTimeout(upTimer);
  let active = false;
  try {
    const [status, queue, failed, done] = await Promise.all([
      api("/api/uploads/status"), api("/api/uploads?view=queue"),
      api("/api/uploads?view=failed"), api("/api/uploads?view=completed")]);
    const { queue: q, counts } = status;
    active = counts.uploading > 0;
    U("upload-pause").hidden = !q.error_pause;
    U("upload-pause-msg").textContent = q.error_pause ? q.error_pause.message : "";
    const pill = U("up-pill");
    pill.textContent = q.error_pause ? "Paused: needs attention" : q.running ? "Running" : "Paused";
    pill.className = "pill " + (q.error_pause ? "pill-failed" : q.running ? "pill-completed" : "pill-pending");
    U("up-summary").textContent = [`${counts.uploading} uploading`, `${counts.pending} pending`,
      `${counts.completed} uploaded`, `${counts.failed} failed`].join(" · ");
    U("up-start").disabled = q.running;
    U("up-pause").disabled = !q.running;
    U("up-count").textContent = queue.total ? `(${queue.total})` : "";
    fill("up-list", "up-empty", queue);
    fill("up-failed", "up-failed-empty", failed);
    fill("up-done", "up-done-empty", done);
  } catch (err) { toast(err.message, "error"); }
  upTimer = setTimeout(refreshUploads, active ? 1000 : 4000);
}

U("up-start").addEventListener("click", async () => {
  try { await api("/api/uploads/queue/start", { method: "POST" }); toast("Uploads resumed."); refreshUploads(); }
  catch (err) { toast(err.message, "error"); }
});
U("up-pause").addEventListener("click", async () => {
  try { await api("/api/uploads/queue/pause", { method: "POST" }); toast("Uploads paused."); refreshUploads(); }
  catch (err) { toast(err.message, "error"); }
});
U("up-retry").addEventListener("click", async () => {
  try { const r = await api("/api/uploads/retry-failed", { method: "POST" }); toast(`${r.requeued} upload(s) re-queued.`); refreshUploads(); }
  catch (err) { toast(err.message, "error"); }
});

refreshUploads();
