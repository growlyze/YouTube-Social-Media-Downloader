// Download Queue page: live progress, queue controls, failed items.
"use strict";

const $ = (id) => document.getElementById(id);
let pollTimer;

function statusLine(item) {
  if (item.status === "downloading") {
    const parts = [];
    if (item.progress !== null) parts.push(`${item.progress.toFixed(1)}%`);
    if (item.downloaded_bytes) {
      parts.push(formatBytes(item.downloaded_bytes) + (item.total_bytes ? ` / ${formatBytes(item.total_bytes)}` : ""));
    }
    if (item.speed) parts.push(`${formatBytes(item.speed)}/s`);
    if (item.eta) parts.push(`ETA ${formatEta(item.eta)}`);
    return parts.join(" · ");
  }
  if (item.status === "pending" && item.next_attempt_at) return `Retry scheduled ${item.next_attempt_at} UTC`;
  return "";
}

function downloadRow(item) {
  const isActive = item.status === "downloading";
  const actions = el("div", { class: "dl-actions" });
  if (item.status === "pending" || isActive) {
    actions.append(el("button", { type: "button", class: "btn btn-small", onclick: () => act(item, "cancel") }, "Cancel"));
  }
  if (item.status === "failed" || item.status === "cancelled") {
    actions.append(el("button", { type: "button", class: "btn btn-small", onclick: () => act(item, "retry") }, "Retry"));
  }
  if (!isActive) {
    actions.append(el("button", { type: "button", class: "btn btn-small btn-danger", title: "Remove from the list (files are kept)",
      onclick: () => act(item, "remove") }, "Remove"));
  }
  // Width is set through the DOM: the CSP blocks inline style attributes.
  const bar = el("div", { class: "progress-bar" });
  bar.style.width = `${Math.min(100, item.progress ?? 0)}%`;
  return el("div", { class: `dl-row dl-${item.status}` },
    el("img", { class: "dl-thumb", src: item.thumbnail, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }),
    el("div", { class: "dl-main" },
      el("div", { class: "dl-title" },
        el("a", { href: item.url, target: "_blank", rel: "noopener noreferrer" }, item.title || item.video_id)),
      el("div", { class: "muted small" },
        [item.channel_name, item.video_type === "short" ? "Short" : item.video_type === "live" ? "Live" : null,
         item.duration ? formatDuration(item.duration) : null].filter(Boolean).join(" · ")),
      isActive ? el("div", { class: "progress" }, bar) : null,
      el("div", { class: "small" },
        el("span", { class: `pill pill-${item.status}` }, item.status),
        " ", isActive && item.stage ? el("span", { class: "muted" }, item.stage + " ") : null,
        el("span", {}, statusLine(item))),
      item.error_message ? el("div", { class: "small " + (item.status === "failed" ? "text-bad" : "muted") }, item.error_message) : null,
      item.download_dir ? el("div", { class: "small muted path", title: "Destination folder" }, "→ " + item.download_dir) : null,
    ),
    actions,
  );
}

async function act(item, action) {
  try {
    if (action === "remove") {
      await api(`/api/downloads/${item.id}`, { method: "DELETE" });
    } else {
      await api(`/api/downloads/${item.id}/${action}`, { method: "POST" });
    }
    refresh();
  } catch (err) { toast(err.message, "error"); }
}

function renderStatus(status) {
  const { queue, counts, pause, runtime } = status;
  $("ffmpeg-warning").hidden = runtime.ffmpeg;
  const pill = $("queue-pill");
  let label = queue.running ? "Running" : "Paused";
  if (pause.paused) label = "Stopped: YouTube rate limit";
  pill.textContent = label;
  pill.className = "pill " + (pause.paused ? "pill-failed" : queue.running ? "pill-completed" : "pill-pending");
  const bits = [`${counts.downloading} downloading`, `${counts.pending} pending`,
                `${counts.completed} completed`, `${counts.failed} failed`];
  if (queue.pacing_wait > 0 && counts.pending > 0) bits.push(`next download in ~${queue.pacing_wait}s (pacing)`);
  if (status.next_retry_at) bits.push(`next retry ${status.next_retry_at} UTC`);
  $("queue-summary").textContent = bits.join(" · ");
  $("queue-start").disabled = queue.running;
  $("queue-pause").disabled = !queue.running;
  $("queue-note").hidden = !queue.note;
  $("queue-note").textContent = queue.note || "";
}

async function refresh() {
  clearTimeout(pollTimer);
  let active = false;
  try {
    const [status, queue, failed] = await Promise.all([
      refreshPause(),
      api("/api/downloads?view=queue&page_size=200"),
      api("/api/downloads?view=failed&page_size=50"),
    ]);
    if (status) {
      renderStatus(status);
      active = status.counts.downloading > 0;
    }
    $("queue-list").replaceChildren(...queue.items.map(downloadRow));
    $("queue-empty").hidden = queue.total > 0;
    $("queue-count").textContent = queue.total ? `(${queue.total})` : "";
    $("failed-list").replaceChildren(...failed.items.map(downloadRow));
    $("failed-empty").hidden = failed.total > 0;
    $("failed-count").textContent = failed.total ? `(${failed.total})` : "";
  } catch (err) {
    toast(err.message, "error");
  }
  pollTimer = setTimeout(refresh, active ? 1000 : 4000);
}

async function control(path, message) {
  try {
    await api(path, { method: "POST" });
    if (message) toast(message);
    refresh();
  } catch (err) { toast(err.message, "error"); }
}

$("queue-start").addEventListener("click", () => control("/api/downloads/queue/start", "Queue started."));
$("queue-pause").addEventListener("click", () =>
  control("/api/downloads/queue/pause", "Queue paused. The current download keeps its partial file and resumes later."));
$("retry-failed").addEventListener("click", async () => {
  try {
    const { requeued } = await api("/api/downloads/retry-failed", { method: "POST" });
    toast(`${requeued} item(s) re-queued.`);
    refresh();
  } catch (err) { toast(err.message, "error"); }
});
$("clear-pending").addEventListener("click", async () => {
  if (!confirm("Remove all pending items from the queue?")) return;
  try {
    const { removed } = await api("/api/downloads/clear", { method: "POST", body: { statuses: ["pending"] } });
    toast(`Removed ${removed} pending item(s).`);
    refresh();
  } catch (err) { toast(err.message, "error"); }
});

refresh();
