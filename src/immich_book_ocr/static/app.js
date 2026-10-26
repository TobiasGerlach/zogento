"use strict";

const $ = (id) => document.getElementById(id);
const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
let currentJob = null;
let currentJobStatus = null;
let pollTimer = null;
let renderedReport = null;
const editedSplits = new Map();

async function request(path, payload) {
  const response = await fetch(path, {
    method: payload === undefined ? "GET" : "POST",
    credentials: "same-origin",
    headers: payload === undefined ? {} : { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
  let data;
  try { data = await response.json(); }
  catch { throw new Error("The server could not respond. Check that the app is still running."); }
  if (!response.ok) throw new Error(data.error || "The request could not finish.");
  return data;
}

function showError(element, message) {
  element.textContent = message || "";
  element.hidden = !message;
}

function setWorking(working) {
  $("start-button").disabled = working;
  $("start-button").firstChild.textContent = working ? "Processing your book " : currentJobStatus === "failed" ? "Retry album " : "Digitize book ";
  $("rerun-button").disabled = working;
  $("activity").hidden = !working;
  document.querySelectorAll(".split-slider").forEach((input) => { input.disabled = working; });
}

function renderReport(job) {
  const report = job.report;
  if (!report) return;
  $("job-title").textContent = report.title || "Your book";
  $("job-count").textContent = `${report.scans ?? 0} scans · ${report.pages ?? 0} pages`;
  $("result-actions").hidden = job.status !== "completed";
  $("download-link").href = `/api/jobs/${encodeURIComponent(job.id)}/text`;
  $("result-note").textContent = report.complete
    ? "UTF-8 plain text. Keep the page markers while proofreading."
    : "Partial result: some pages need attention. Check the warnings below.";
  const warnings = Array.isArray(report.warnings) ? report.warnings : [];
  $("warnings").replaceChildren();
  for (const warning of warnings) {
    const item = document.createElement("li");
    item.textContent = warning;
    $("warnings").append(item);
  }
  $("warnings").hidden = !warnings.length;
  if (renderedReport === report) return;
  renderedReport = report;
  editedSplits.clear();
  const entries = Array.isArray(report.entries) ? report.entries : [];
  $("review-section").hidden = !entries.length;
  $("review-heading").textContent = job.single_page ? "Review your scans" : "Review your page splits";
  $("review-help").textContent = job.single_page
    ? "Check the scans and any recognition warnings. Rebuild the text to retry saved scans."
    : "Check the division for each spread. Move a split if needed, then rebuild the text from your saved scans.";
  $("scan-grid").replaceChildren();
  entries.forEach((entry, index) => {
    const card = document.createElement("article");
    card.className = "scan-card";
    let marker = null;
    if (entry.preview) {
      const frame = document.createElement("div");
      frame.className = "scan-image";
      const image = document.createElement("img");
      image.src = `/api/jobs/${encodeURIComponent(job.id)}/preview/${index}?v=${Date.now()}`;
      image.alt = `Scan preview: ${entry.source}`;
      image.loading = "lazy";
      frame.append(image);
      if (entry.split_fraction != null && !job.single_page) {
        marker = document.createElement("span");
        marker.className = "split-line";
        marker.style.left = `${entry.split_fraction * 100}%`;
        frame.append(marker);
      }
      card.append(frame);
    }
    const body = document.createElement("div");
    body.className = "scan-body";
    const heading = document.createElement("h3");
    heading.textContent = entry.source || `Scan ${index + 1}`;
    body.append(heading);
    if (entry.split_fraction != null && !job.single_page) {
      const label = document.createElement("label");
      label.htmlFor = `split-${index}`;
      label.textContent = "Page division";
      const control = document.createElement("div");
      control.className = "split-control";
      const slider = document.createElement("input");
      Object.assign(slider, {type: "range", min: "5", max: "95", step: "0.1", value: String(entry.split_fraction * 100), id: `split-${index}`, className: "split-slider"});
      const value = document.createElement("output");
      value.htmlFor = slider.id;
      value.textContent = `${Number(slider.value).toFixed(1)}%`;
      slider.addEventListener("input", () => {
        const percent = Number(slider.value);
        value.textContent = `${percent.toFixed(1)}%`;
        if (marker) marker.style.left = `${percent}%`;
        editedSplits.set(entry.source, percent / 100);
      });
      control.append(slider, value);
      body.append(label, control);
      const hint = document.createElement("p");
      hint.className = "field-note";
      hint.textContent = "Orange line = split used when rebuilding.";
      body.append(hint);
    }
    const scanWarnings = [...(entry.warnings || [])];
    (entry.pages || []).forEach((page, pageIndex) => {
      for (const warning of (page.warnings || [])) scanWarnings.push(`Page ${pageIndex + 1}: ${warning}`);
    });
    for (const warning of scanWarnings) {
      const note = document.createElement("p");
      note.className = "scan-warning";
      note.textContent = warning;
      body.append(note);
    }
    card.append(body);
    $("scan-grid").append(card);
  });
}

function renderJob(job) {
  currentJobStatus = job.status;
  $("empty-state").hidden = true;
  $("job-state").hidden = false;
  const running = job.status === "running";
  setWorking(running);
  const partial = job.report && !job.report.complete;
  $("status-badge").className = `status-badge ${running ? "working" : job.status === "failed" ? "failed" : partial ? "partial" : "complete"}`;
  $("status-badge").textContent = running ? "Working" : job.status === "failed" ? "Needs attention" : partial ? "Partial text ready" : "Text ready";
  const events = job.events || [];
  const latest = events.at(-1);
  $("job-message").textContent = running ? (latest?.message || "Processing your book…") : job.status === "failed" ? "Re-enter any required credentials, then retry the album. Saved downloads will be reused." : "Your scans have been processed. Review the pages and download your text.";
  showError($("job-error"), job.error);
  $("event-log").replaceChildren();
  for (const event of events.slice().reverse()) {
    const item = document.createElement("li");
    item.textContent = event.message;
    $("event-log").append(item);
  }
  if (!running && job.report) renderReport(job);
  if (running) $("result-actions").hidden = true;
  return running;
}

async function poll() {
  if (!currentJob) return;
  try {
    const job = await request(`/api/jobs/${encodeURIComponent(currentJob)}`);
    if (renderJob(job)) pollTimer = setTimeout(poll, 1100);
  } catch (error) {
    showError($("job-error"), error.message);
    setWorking(false);
    $("status-badge").textContent = "Connection interrupted";
    $("status-badge").className = "status-badge failed";
    pollTimer = setTimeout(poll, 5000);
  }
}

$("album-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showError($("form-error"), null);
  setWorking(true);
  try {
    const job = await request("/api/jobs", {
      url: $("album-url").value.trim(), language: $("language").value.trim(),
      order: $("scan-order").value, reading_order: $("reading-order").value,
      single_page: $("single-page").checked, engine: $("ocr-engine").value,
      rotation: $("page-rotation").value, crop_paper: $("crop-paper").checked,
      password: $("share-password").value || null, api_key: $("api-key").value || null,
      resume_job: currentJobStatus === "failed" ? currentJob : null,
    });
    $("share-password").value = "";
    $("api-key").value = "";
    currentJob = job.id;
    sessionStorage.setItem("folio-job", currentJob);
    renderedReport = null;
    clearTimeout(pollTimer);
    $("review-section").hidden = true;
    $("job-count").textContent = "";
    $("job-title").textContent = "Opening your album…";
    $("warnings").hidden = true;
    $("result-actions").hidden = true;
    await poll();
  } catch (error) {
    showError($("form-error"), error.message);
    setWorking(false);
  }
});

$("rerun-button").addEventListener("click", async () => {
  if (!currentJob) return;
  showError($("review-error"), null);
  setWorking(true);
  try {
    await request(`/api/jobs/${encodeURIComponent(currentJob)}/rerun`, {splits: Object.fromEntries(editedSplits)});
    renderedReport = null;
    clearTimeout(pollTimer);
    await poll();
    $("job-panel").scrollIntoView({behavior: "smooth", block: "nearest"});
  } catch (error) {
    showError($("review-error"), error.message);
    setWorking(false);
  }
});

const previousJob = sessionStorage.getItem("folio-job");
if (previousJob && /^[a-f0-9]{32}$/.test(previousJob)) {
  request(`/api/jobs/${previousJob}`).then((job) => {
    currentJob = previousJob;
    if (renderJob(job)) pollTimer = setTimeout(poll, 1100);
  }).catch(() => sessionStorage.removeItem("folio-job"));
}
