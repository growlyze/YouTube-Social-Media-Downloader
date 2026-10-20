// Channel Library: discovery jobs, filters, paging and selection.
"use strict";

const state = {
  source: new URLSearchParams(location.search).get("source") || "",
  page: 1,
  data: null,
  jobId: null,
  jobTimer: null,
};

const $ = (id) => document.getElementById(id);
const FILTER_IDS = ["f-q", "f-type", "f-date-from", "f-date-to", "f-min", "f-max",
                    "f-availability", "f-download", "f-selected", "f-sort"];

function currentFilters() {
  return {
    source: state.source,
    q: $("f-q").value.trim(),
    type: $("f-type").value,
    date_from: $("f-date-from").value,
    date_to: $("f-date-to").value,
    min_minutes: $("f-min").value,
    max_minutes: $("f-max").value,
    availability: $("f-availability").value,
    download: $("f-download").value,
    selected_only: $("f-selected").checked ? "1" : "",
  };
}

// ---- sources ------------------------------------------------------------

async function loadSources() {
  const { sources } = await api("/api/sources");
  const list = $("source-list");
  list.replaceChildren(sourceItem({ id: "", title: "All videos" }));
  for (const src of sources) list.append(sourceItem(src));
}

function sourceItem(src) {
  const link = el("a", {
    href: "#", class: "source-link" + (src.id === state.source ? " active" : ""),
    onclick: (e) => { e.preventDefault(); selectSource(src.id); },
  },
    src.thumbnail ? el("img", { src: src.thumbnail, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }) : null,
    el("span", { class: "source-title" }, src.title || src.id),
    src.id ? el("span", { class: "muted small" },
      `${src.total} items` + (src.shorts ? ` · ${src.shorts} Shorts` : "") +
      (src.selected ? ` · ${src.selected} selected` : "")) : null,
  );
  const item = el("li", {}, link);
  if (src.id) {
    item.append(el("div", { class: "source-actions" },
      el("button", { type: "button", class: "btn btn-small", title: "Fetch again to pick up new videos",
        onclick: () => startDiscovery(src.url) }, "Refresh"),
      el("button", { type: "button", class: "btn btn-small btn-danger", title: "Remove from library",
        onclick: () => removeSource(src) }, "Remove"),
    ));
  }
  return item;
}

function selectSource(id) {
  state.source = id;
  state.page = 1;
  const url = new URL(location.href);
  if (id) url.searchParams.set("source", id); else url.searchParams.delete("source");
  history.replaceState(null, "", url);
  loadSources();
  loadVideos();
}

async function removeSource(src) {
  if (!confirm(`Remove "${src.title || src.id}" and its videos from the library? Downloaded files are not affected.`)) return;
  try {
    await api(`/api/sources/${encodeURIComponent(src.id)}`, { method: "DELETE" });
    if (state.source === src.id) state.source = "";
    toast("Removed.");
    await loadSources();
    loadVideos();
  } catch (err) { toast(err.message, "error"); }
}

// ---- discovery ----------------------------------------------------------

async function startDiscovery(url) {
  try {
    const { job, pause } = await api("/api/discover", { method: "POST", body: { url } });
    if (pause && pause.paused) {
      toast("Queued, but YouTube requests are paused. It will start after you resume.", "warn");
    }
    watchJob(job.id);
  } catch (err) { toast(err.message, "error"); }
}

function watchJob(jobId) {
  state.jobId = jobId;
  clearTimeout(state.jobTimer);
  $("job-status").hidden = false;
  pollJob();
}

async function pollJob() {
  if (!state.jobId) return;
  let job;
  try {
    ({ job } = await api(`/api/discover/${state.jobId}`));
  } catch (err) {
    $("job-message").textContent = err.message;
    state.jobTimer = setTimeout(pollJob, 5000);
    return;
  }
  const active = job.status === "pending" || job.status === "running";
  $("job-spinner").hidden = !active;
  $("job-cancel").hidden = !active;
  const status = $("job-status");
  status.className = "job-status job-" + job.status;
  $("job-message").textContent =
    job.status === "pending" ? "Waiting to start…" : (job.message || job.status);

  if (job.source_id && job.found_count && (!state.lastFound || job.found_count - state.lastFound >= 50 || !active)) {
    state.lastFound = job.found_count;
    if (!state.source) state.source = job.source_id;
    loadSources();
    loadVideos();
  }
  if (active) {
    state.jobTimer = setTimeout(pollJob, 1500);
  } else {
    state.jobId = null;
    state.lastFound = 0;
    if (job.status === "completed") toast(job.message || "Done.");
    else if (job.status === "failed") toast(job.message || "Discovery failed.", "error");
    refreshPause();
  }
}

$("discover-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const url = $("discover-url").value.trim();
  if (url) startDiscovery(url);
});

$("job-cancel").addEventListener("click", async () => {
  if (!state.jobId) return;
  try { await api(`/api/discover/${state.jobId}/cancel`, { method: "POST" }); }
  catch (err) { toast(err.message, "error"); }
});

// ---- videos -------------------------------------------------------------

async function loadVideos() {
  const params = new URLSearchParams({
    ...currentFilters(), sort: $("f-sort").value, page: state.page, page_size: $("page-size").value,
  });
  for (const [k, v] of [...params]) if (v === "") params.delete(k);
  try {
    state.data = await api(`/api/videos?${params}`);
  } catch (err) { toast(err.message, "error"); return; }
  if (state.page > state.data.pages) { state.page = state.data.pages; return loadVideos(); }
  renderVideos();
}

const AVAIL_LABELS = {
  private: "Private", deleted: "Deleted", unavailable: "Unavailable",
  subscriber_only: "Members only", premium_only: "Premium", needs_auth: "Sign-in required",
  unlisted: "Unlisted",
};

function renderVideos() {
  const { items, total, selected, page, pages } = state.data;
  const grid = $("video-grid");
  grid.replaceChildren(...items.map(videoCard));
  $("empty-state").hidden = items.length > 0;
  $("count-label").textContent = `${selected} selected · ${total} matching`;
  $("queue-selected").disabled = selected === 0;
  $("queue-selected").textContent = selected ? `Download selected (${selected})` : "Download selected";
  $("page-label").textContent = `Page ${page} of ${pages}`;
  $("prev-page").disabled = page <= 1;
  $("next-page").disabled = page >= pages;
  // Keep download badges fresh while items on this page are queued/downloading.
  clearTimeout(state.badgeTimer);
  if (items.some((v) => v.download_status === "pending" || v.download_status === "downloading")) {
    state.badgeTimer = setTimeout(loadVideos, 3000);
  }
  const allOnPage = items.length > 0 && items.every((v) => v.selected);
  $("page-toggle").checked = allOnPage;
  $("page-toggle").indeterminate = !allOnPage && items.some((v) => v.selected);
}

function videoCard(v) {
  const unavailable = ["private", "deleted", "unavailable", "premium_only", "subscriber_only", "needs_auth"]
    .includes(v.availability);
  const checkbox = el("input", {
    type: "checkbox", checked: !!v.selected, "aria-label": "Select " + (v.title || v.video_id),
    onchange: (e) => toggleVideo(v, e.target.checked),
  });
  const date = formatDate(v.upload_date);
  return el("label", { class: "video" + (v.selected ? " selected" : "") + (unavailable ? " unavailable" : "") },
    el("div", { class: "thumb" + (v.video_type === "short" ? " thumb-short" : "") },
      el("img", { src: v.thumbnail, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }),
      v.duration !== null ? el("span", { class: "duration" }, formatDuration(v.duration)) : null,
      checkbox,
    ),
    el("div", { class: "video-body" },
      el("a", { class: "video-title", href: v.url, target: "_blank", rel: "noopener noreferrer",
        title: v.title || "", onclick: (e) => e.stopPropagation() }, v.title || v.video_id),
      el("div", { class: "muted small" }, v.channel_name || ""),
      el("div", { class: "meta" },
        el("span", { class: "badge badge-" + v.video_type }, v.video_type === "short" ? "Short" : v.video_type === "live" ? "Live" : "Video"),
        date ? el("span", { class: "small", title: v.date_is_approx ? "Approximate (from \"x weeks ago\")" : "" },
          (v.date_is_approx ? "~" : "") + date) : null,
        v.availability && AVAIL_LABELS[v.availability]
          ? el("span", { class: "badge " + (unavailable ? "badge-bad" : "badge-muted") }, AVAIL_LABELS[v.availability]) : null,
        downloadBadge(v),
      ),
    ),
  );
}

const DOWNLOAD_BADGES = {
  pending: ["Queued", "badge-muted"],
  downloading: ["Downloading", "badge-short"],
  completed: ["Downloaded", "badge-ok"],
  failed: ["Download failed", "badge-bad"],
  cancelled: ["Cancelled", "badge-muted"],
};

function downloadBadge(v) {
  const [label, cls] = DOWNLOAD_BADGES[v.download_status] || ["Not downloaded", "badge-muted"];
  const text = v.download_status === "downloading" && v.download_progress !== null
    ? `${label} ${Math.round(v.download_progress)}%` : label;
  return el("span", { class: `badge ${cls}`, title: "Download status" }, text);
}

$("queue-selected").addEventListener("click", async () => {
  const filters = currentFilters();
  const count = state.data ? state.data.selected : 0;
  if (!confirm(`Download ${count} selected video(s) to:\n\n${currentDownloadDir()}\n\n` +
               "Press Cancel to keep them selected and choose a different folder first.")) return;
  try {
    const r = await api("/api/downloads/enqueue", { method: "POST", body: { from_selection: true, filters } });
    const notes = [`${r.added} added to the download queue`];
    if (r.already_downloaded) notes.push(`${r.already_downloaded} already downloaded`);
    if (r.already_queued) notes.push(`${r.already_queued} already queued`);
    if (r.skipped_unavailable) notes.push(`${r.skipped_unavailable} unavailable (private/deleted/members-only) skipped`);
    if (r.skipped_type) notes.push(`${r.skipped_type} Shorts/Live skipped by your Settings`);
    let message = notes.join(", ") + ".";
    if (r.added && !r.queue_running) message += " The queue is paused; press Start on the Download Queue page.";
    toast(message, r.added ? "info" : "warn");
    loadVideos();
    loadSources();
    refreshPause();
  } catch (err) { toast(err.message, "error"); }
});

async function toggleVideo(v, selected) {
  try {
    await api("/api/videos/select", { method: "POST", body: { ids: [v.video_id], selected } });
    v.selected = selected ? 1 : 0;
    state.data.selected += selected ? 1 : -1;
    renderVideos();
  } catch (err) { toast(err.message, "error"); loadVideos(); }
}

async function bulkSelect(body, label) {
  try {
    const { changed } = await api("/api/videos/select", { method: "POST", body });
    toast(`${label}: ${changed} video(s).`);
    loadVideos();
    loadSources();
  } catch (err) { toast(err.message, "error"); }
}

$("page-toggle").addEventListener("change", (e) => {
  const ids = (state.data?.items || []).map((v) => v.video_id);
  bulkSelect({ ids, selected: e.target.checked }, e.target.checked ? "Selected" : "Deselected");
});
$("select-all").addEventListener("click", () =>
  bulkSelect({ all_matching: true, filters: currentFilters(), selected: true }, "Selected"));
$("deselect-all").addEventListener("click", () =>
  bulkSelect({ all_matching: true, filters: currentFilters(), selected: false }, "Deselected"));

$("prev-page").addEventListener("click", () => { state.page--; loadVideos(); window.scrollTo(0, 0); });
$("next-page").addEventListener("click", () => { state.page++; loadVideos(); window.scrollTo(0, 0); });
$("page-size").addEventListener("change", () => { state.page = 1; loadVideos(); });

let debounce;
for (const id of FILTER_IDS) {
  const input = $(id);
  const handler = () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => { state.page = 1; loadVideos(); }, id === "f-q" ? 300 : 0);
  };
  input.addEventListener(input.type === "search" || input.type === "number" ? "input" : "change", handler);
}
$("f-reset").addEventListener("click", () => {
  for (const id of FILTER_IDS) {
    const input = $(id);
    if (input.type === "checkbox") input.checked = false;
    else if (input.tagName === "SELECT") input.selectedIndex = 0;
    else input.value = "";
  }
  state.page = 1;
  loadVideos();
});

// Resume watching a discovery job that is still running (e.g. after reload).
async function resumeActiveJob() {
  try {
    const { jobs } = await api("/api/discover");
    const active = jobs.find((j) => j.status === "running" || j.status === "pending");
    if (active) watchJob(active.id);
  } catch (_) { /* ignore */ }
}

loadSources();
loadVideos();
resumeActiveJob();
