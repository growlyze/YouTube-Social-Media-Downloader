// Download History page.
"use strict";

const $ = (id) => document.getElementById(id);
let page = 1;

async function retry(item) {
  try {
    await api(`/api/downloads/${item.id}/retry`, { method: "POST" });
    toast("Re-queued.");
    load();
  } catch (err) { toast(err.message, "error"); }
}

function row(item) {
  const details = item.status === "completed"
    ? [item.format_desc, formatBytes(item.file_size)].filter(Boolean).join(" · ")
    : item.error_message || "";
  return el("tr", {},
    el("td", { class: "ellipsis" },
      el("a", { href: item.url, target: "_blank", rel: "noopener noreferrer", title: item.title }, item.title || item.video_id),
      el("div", { class: "muted small" }, item.channel_name || "")),
    el("td", {}, el("span", { class: `pill pill-${item.status}` }, item.status)),
    el("td", { class: "small" }, item.finished_at || ""),
    el("td", { class: "small wrap" + (item.status === "failed" ? " text-bad" : "") }, details),
    el("td", {}, item.status !== "completed"
      ? el("button", { type: "button", class: "btn btn-small", onclick: () => retry(item) }, "Retry") : null),
  );
}

async function load() {
  const params = new URLSearchParams({ view: $("hist-view").value, page, page_size: 50 });
  const q = $("hist-q").value.trim();
  if (q) params.set("q", q);
  try {
    const data = await api(`/api/downloads?${params}`);
    $("hist-rows").replaceChildren(...data.items.map(row));
    $("hist-empty").hidden = data.total > 0;
    $("hist-count").textContent = `${data.total} item(s)`;
    $("hist-page").textContent = `Page ${data.page} of ${data.pages}`;
    $("hist-prev").disabled = data.page <= 1;
    $("hist-next").disabled = data.page >= data.pages;
  } catch (err) { toast(err.message, "error"); }
}

let debounce;
$("hist-q").addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(() => { page = 1; load(); }, 300); });
$("hist-view").addEventListener("change", () => { page = 1; load(); });
$("hist-prev").addEventListener("click", () => { page--; load(); });
$("hist-next").addEventListener("click", () => { page++; load(); });
$("hist-clear").addEventListener("click", async () => {
  if (!confirm("Remove all failed and cancelled records? Files on disk are not touched.")) return;
  try {
    const { removed } = await api("/api/downloads/clear", { method: "POST", body: { statuses: ["failed", "cancelled"] } });
    toast(`Removed ${removed} record(s).`);
    load();
  } catch (err) { toast(err.message, "error"); }
});
load();
