// Space Check page: local disk and Google Drive free space, automatic stop and
// continue, and the "Recheck space and continue" button.
"use strict";

const $ = (id) => document.getElementById(id);

function setPill(node, text, kind) {
  node.textContent = text;
  node.className = `pill pill-${kind}`;
}

function countdown(iso) {
  if (!iso) return "…";
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "…";
  const seconds = Math.round((when.getTime() - Date.now()) / 1000);
  if (seconds <= 0) return "due now";
  return `in ${formatEta(seconds)}`;
}

function renderLocal(data) {
  const local = data.local || {};
  const pause = data.local_pause || {};
  const low = pause.paused || local.ok === false;
  setPill($("local-pill"),
    !data.enabled ? "checks off" : low ? "low space" : "ok",
    !data.enabled ? "pending" : low ? "failed" : "completed");
  $("local-summary").textContent = local.free !== undefined
    ? `${formatBytes(local.free)} free of ${formatBytes(local.total)} (${local.free_percent}% free, stop below ${local.threshold}%)`
    : (local.message || "Not checked yet.");
  const usedPercent = local.total ? Math.min(100, Math.round((local.used / local.total) * 100)) : 0;
  $("local-bar").style.width = `${usedPercent}%`;
  $("local-bar").style.background = low ? "var(--bad)" : "var(--accent)";
  $("local-detail").textContent = [
    local.path ? `Download folder: ${local.path}` : "",
    pause.paused ? pause.message : "",
  ].filter(Boolean).join(" · ");
}

function renderDrive(data) {
  const drive = data.drive || {};
  const pause = data.drive_pause || {};
  const low = pause.paused || drive.ok === false;
  setPill($("drive-pill"),
    drive.connected === false ? "not connected" : low ? "low space" : "ok",
    drive.connected === false ? "pending" : low ? "failed" : "completed");
  if (drive.connected === false) {
    $("drive-summary").textContent = "Google Drive is not connected; uploads are not affected.";
  } else if (drive.total) {
    $("drive-summary").textContent =
      `${formatBytes(drive.free)} free of ${formatBytes(drive.total)} (${drive.free_percent}% free, stop below ${drive.threshold}%)` +
      (drive.email ? ` · ${drive.email}` : "");
    const usedPercent = Math.min(100, Math.round((drive.used / drive.total) * 100));
    $("drive-bar").style.width = `${usedPercent}%`;
    $("drive-bar").style.background = low ? "var(--bad)" : "var(--accent)";
  } else {
    $("drive-summary").textContent = drive.message || "Google Drive reports no storage limit.";
    $("drive-bar").style.width = "0%";
  }
  $("drive-detail").textContent = pause.paused ? pause.message : (drive.message || "");
}

function renderQueues(data) {
  const downloads = data.downloads || {};
  const uploads = data.uploads || {};
  const localPaused = (data.local_pause || {}).paused;
  const drivePaused = (data.drive_pause || {}).paused;
  setPill($("dl-pill"),
    localPaused ? "stopped for space" : downloads.running ? "running" : "paused",
    localPaused ? "failed" : downloads.running ? "completed" : "pending");
  $("dl-summary").textContent = localPaused
    ? "Downloads continue automatically when space is free again."
    : downloads.active ? `${downloads.active} downloading now` : "Waiting for work.";
  setPill($("up-pill"),
    drivePaused ? "stopped for space" : uploads.running ? "running" : "paused",
    drivePaused ? "failed" : uploads.running ? "completed" : "pending");
  $("up-summary").textContent = drivePaused
    ? "Uploads continue automatically when Google Drive has space again."
    : uploads.active ? "An upload is running now" : "Waiting for work.";
}

function render(data) {
  $("sc-interval").textContent = String(data.recheck_minutes ?? 60);
  $("sc-last").textContent = data.checked_at ? `${data.checked_at} UTC` : "never";
  $("sc-next").textContent = data.next_check_at ? countdown(data.next_check_at) : "…";

  const blocked = (data.local_pause || {}).paused || (data.drive_pause || {}).paused;
  setPill($("sc-pill"), blocked ? "stopped for space" : data.enabled ? "automatic" : "checks off",
    blocked ? "failed" : data.enabled ? "completed" : "pending");
  $("sc-summary").textContent = blocked
    ? "Free space is low; the queues continue by themselves when a check finds space again."
    : data.enabled
      ? `Space is fine. A check runs every ${data.recheck_minutes} minutes automatically.`
      : "Automatic space checking is turned off in Settings.";

  renderLocal(data);
  renderDrive(data);
  renderQueues(data);

  const events = data.events || [];
  $("sc-events-empty").hidden = events.length > 0;
  $("sc-events").replaceChildren(...events.slice().reverse().map((event) =>
    el("li", {}, el("span", { class: "muted small" }, event.at), " — ", event.text)));
}

async function refresh() {
  try {
    render(await api("/api/space/status"));
  } catch (_) { /* server down */ }
}

async function recheck() {
  const status = $("sc-check-status");
  for (const button of document.querySelectorAll("#sc-recheck, #sc-recheck-2")) button.disabled = true;
  status.textContent = "Checking local disk and Google Drive…";
  try {
    const data = await api("/api/space/check", { method: "POST" });
    render(data);
    status.textContent = "Check finished.";
    toast("Space re-checked. Queues continue where space allows.");
  } catch (err) {
    status.textContent = "";
    toast(err.message, "error");
  } finally {
    for (const button of document.querySelectorAll("#sc-recheck, #sc-recheck-2")) button.disabled = false;
  }
}

$("sc-recheck").addEventListener("click", recheck);
$("sc-recheck-2").addEventListener("click", recheck);

refresh();
setInterval(refresh, 5000);
