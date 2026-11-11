// Lead Radar web UI. Plain JS, no build step. All logic lives in the Python API.
"use strict";

const $ = (selector) => document.querySelector(selector);

const state = {
  status: null,
  locations: [],
  estimate: null, // { signature, total }
  pollTimer: null,
};

// --- helpers -------------------------------------------------------------------------------

async function api(path, options = {}) {
  const init = { headers: {}, ...options };
  if (options.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, init);
  if (response.status === 204) return null;
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof data.detail === "string" ? data.detail : "Something went wrong.";
    throw new Error(detail);
  }
  return data;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child !== null && child !== undefined) node.append(child);
  }
  return node;
}

function plural(count, word) {
  const many = /(s|sh|ch|x)$/.test(word) ? `${word}es` : `${word}s`;
  return `${count.toLocaleString()} ${count === 1 ? word : many}`;
}

function formatDate(iso) {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function toast(message, ok = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("ok", ok);
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, ok ? 3000 : 8000);
}

function remember(key, value) {
  try { localStorage.setItem(`lead-radar:${key}`, JSON.stringify(value)); } catch { /* ignore */ }
}

function recall(key) {
  try { return JSON.parse(localStorage.getItem(`lead-radar:${key}`)); } catch { return null; }
}

function showView(id) {
  for (const view of document.querySelectorAll(".view")) view.hidden = view.id !== id;
  window.scrollTo(0, 0);
}

// --- account and history -------------------------------------------------------------------

async function loadAccount() {
  const box = $("#account");
  box.replaceChildren();
  if (!state.status.has_key) {
    box.append(el("span", { class: "pill", text: "Demo mode · no API key" }));
    return;
  }
  try {
    const account = await api("/api/account");
    const left = account.total_searches_left;
    box.append(
      el("b", { text: left === null ? "?" : left.toLocaleString() }),
      " searches left",
      account.plan_name ? el("span", { class: "plan", text: ` · ${account.plan_name}` }) : null,
    );
  } catch (error) {
    box.append(el("span", { class: "pill", text: "Account unavailable" }));
    box.title = error.message;
  }
}

async function loadHistory() {
  const list = $("#history");
  let runs = [];
  try {
    runs = await api("/api/runs");
  } catch (error) {
    toast(error.message);
  }
  list.replaceChildren();
  if (runs.length === 0) {
    list.append(el("li", { class: "empty", text: "No reports yet. Your searches will appear here." }));
  }
  for (const run of runs) {
    const meta = el("span", { class: "h-meta" },
      el("span", { class: "tag hot", text: `${run.hot} hot` }),
      el("span", { class: "tag", text: plural(run.leads, "lead") }),
      run.partial ? el("span", { class: "tag warn", text: "partial" }) : null,
      run.demo ? el("span", { class: "tag", text: "demo" }) : null,
      el("span", { text: formatDate(run.generated_at) }),
    );
    const link = el("a", { href: `#/runs/${run.id}`, "data-id": run.id },
      el("span", { class: "h-query", text: run.query }),
      el("span", { class: "h-loc", text: run.locations.join(" · ") }),
      meta,
    );
    list.append(el("li", {}, link));
  }
  markCurrentRun();
}

function markCurrentRun() {
  const match = location.hash.match(/^#\/runs\/(.+)$/);
  for (const link of document.querySelectorAll("#history a")) {
    if (match && link.dataset.id === match[1]) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

// --- new search form -----------------------------------------------------------------------

function renderTags() {
  const box = $("#tags");
  const input = $("#location-input");
  for (const chip of box.querySelectorAll(".chip")) chip.remove();
  state.locations.forEach((name, index) => {
    const remove = el("button", { type: "button", "aria-label": `Remove ${name}`, text: "×" });
    remove.addEventListener("click", () => {
      state.locations.splice(index, 1);
      renderTags();
      formChanged();
    });
    box.insertBefore(el("span", { class: "chip" }, name, remove), input);
  });
  input.placeholder = state.locations.length ? "Add another location" : "Tampa, FL";
}

function addLocation(raw) {
  const name = raw.trim().replace(/\s+/g, " ");
  if (!name) return false;
  if (!state.locations.some((existing) => existing.toLowerCase() === name.toLowerCase())) {
    state.locations.push(name);
    renderTags();
    formChanged();
  }
  return true;
}

function formPayload() {
  // A location still being typed counts, so nobody loses it by forgetting to press Enter.
  const pending = $("#location-input").value.trim();
  if (pending) {
    addLocation(pending);
    $("#location-input").value = "";
  }
  const withReviews = $("#with-reviews").checked;
  return {
    query: $("#query").value.trim(),
    locations: [...state.locations],
    max_results: Number(document.querySelector("input[name=max_results]:checked").value),
    with_reviews: withReviews,
    review_limit: withReviews ? Number($("#review-limit").value) || null : null,
    use_cache: $("#use-cache").checked,
  };
}

function restoreForm() {
  const saved = recall("form");
  if (!saved) return;
  $("#query").value = saved.query || "";
  state.locations = Array.isArray(saved.locations) ? saved.locations : [];
  const radio = document.querySelector(`input[name=max_results][value="${saved.max_results}"]`);
  if (radio) radio.checked = true;
  $("#with-reviews").checked = Boolean(saved.with_reviews);
  if (saved.review_limit) $("#review-limit").value = saved.review_limit;
  $("#use-cache").checked = saved.use_cache !== false;
  $("#review-limit-row").hidden = !saved.with_reviews;
  renderTags();
}

function formChanged() {
  // An estimate only authorises the exact search it was made for.
  state.estimate = null;
  $("#estimate").hidden = true;
}

function validate(payload) {
  if (!payload.query) return "Enter a niche, for example “roofing contractor”.";
  if (payload.locations.length === 0) return "Add at least one location.";
  return null;
}

async function estimate(event) {
  event.preventDefault();
  const payload = formPayload();
  const problem = validate(payload);
  if (problem) { toast(problem); return; }
  remember("form", payload);

  const button = $("#estimate-btn");
  button.disabled = true;
  button.textContent = "Estimating…";
  try {
    const data = await api("/api/estimate", { method: "POST", body: payload });
    state.estimate = { signature: JSON.stringify(payload), total: data.estimate.total };
    renderEstimate(data, payload);
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Estimate cost";
  }
}

function renderEstimate(data, payload) {
  const { estimate: cost, account, account_error: accountError } = data;
  const bound = cost.exact ? "" : "up to ";
  const rows = el("dl", { class: "estimate-rows" });
  const row = (label, value, cls = "") => {
    rows.append(el("dt", { class: cls, text: label }), el("dd", { class: cls, text: value }));
  };
  row("Google Maps pages", `${bound}${cost.maps_calls}`);
  if (payload.with_reviews) row("Review lookups", `${bound}${cost.review_calls}`);
  row("Already saved (free)", String(cost.cached_calls));
  row("Searches this run", `${bound}${cost.total}`, "total");

  const card = $("#estimate");
  card.replaceChildren(el("h2", { text: "Cost of this search" }), rows);

  const left = account ? account.total_searches_left : null;
  if (left !== null && left !== undefined) {
    card.append(el("p", { class: "muted", text: `You have ${plural(left, "search")} left.` }));
  } else if (accountError) {
    card.append(el("p", { class: "muted", text: `Could not read your balance: ${accountError}` }));
  }
  if (left !== null && left !== undefined && cost.total > left) {
    card.append(el("div", {
      class: "notice",
      text: "This may need more searches than you have left. The run will stop when they run out and keep what it found.",
    }));
  }
  if (cost.total === 0) {
    card.append(el("p", { class: "muted", text: "Everything is already saved, so this run is free." }));
  }

  const label = cost.total === 0 ? "Run search · free" : `Run search · ${bound}${plural(cost.total, "search")}`;
  const run = el("button", { class: "btn primary", type: "button", text: label });
  run.addEventListener("click", () => startRun(payload, cost.total, run));
  card.append(el("div", { class: "form-actions" }, run));
  card.hidden = false;
  card.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function startRun(payload, total, button) {
  if (!state.estimate || state.estimate.signature !== JSON.stringify(payload)) {
    formChanged();
    toast("The search changed. Estimate the cost again.");
    return;
  }
  button.disabled = true;
  try {
    const { job_id: jobId } = await api("/api/jobs", {
      method: "POST",
      body: { ...payload, max_searches: total },
    });
    formChanged();
    location.hash = `#/job/${jobId}`;
  } catch (error) {
    button.disabled = false;
    toast(error.message);
  }
}

async function startDemo() {
  try {
    const { job_id: jobId } = await api("/api/jobs", { method: "POST", body: { demo: true } });
    location.hash = `#/job/${jobId}`;
  } catch (error) {
    toast(error.message);
  }
}

// --- running job ---------------------------------------------------------------------------

function stopPolling() {
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

function showJob(jobId) {
  showView("view-job");
  const cancel = $("#cancel-btn");
  cancel.disabled = false;
  cancel.textContent = "Cancel";
  cancel.onclick = async () => {
    cancel.disabled = true;
    cancel.textContent = "Cancelling…";
    try { await api(`/api/jobs/${jobId}/cancel`, { method: "POST" }); } catch (error) { toast(error.message); }
  };
  pollJob(jobId);
}

async function pollJob(jobId) {
  stopPolling();
  let job;
  try {
    job = await api(`/api/jobs/${jobId}`);
  } catch (error) {
    toast(error.message);
    location.hash = "#/new";
    return;
  }
  if (location.hash !== `#/job/${jobId}`) return; // user navigated away; the run continues

  const titles = { maps: "Searching Google Maps…", reviews: "Reading reviews…", scoring: "Scoring leads…" };
  $("#job-title").textContent = job.cancelling ? "Cancelling…" : titles[job.stage] || "Starting…";
  $("#job-message").textContent = job.message;
  const progress = $("#job-progress");
  const determinate = job.stage === "reviews" && job.total;
  progress.classList.toggle("indeterminate", !determinate);
  progress.querySelector(".bar").style.width = determinate ? `${Math.round((job.current / job.total) * 100)}%` : "";

  if (job.status === "running") {
    state.pollTimer = setTimeout(() => pollJob(jobId), 700);
    return;
  }
  await Promise.all([loadHistory(), loadAccount()]);
  if (job.status === "done") {
    location.hash = `#/runs/${job.run_id}`;
  } else {
    location.hash = "#/new";
    toast(job.error || "The search failed.");
  }
}

// --- report --------------------------------------------------------------------------------

async function showReport(runId) {
  let run;
  try {
    run = await api(`/api/runs/${runId}`);
  } catch {
    showView("view-empty");
    return;
  }
  showView("view-report");
  markCurrentRun();
  $("#report-title").textContent = run.query;
  const searches = run.demo ? "sample data" : `${plural(run.api_calls, "search")} used`;
  $("#report-meta").textContent = `${run.locations.join(" · ")} — ${formatDate(run.generated_at)} — ${searches}`;

  const base = `/api/runs/${runId}`;
  $("#dl-csv").href = `${base}/report.csv`;
  $("#dl-csv").setAttribute("download", `${run.basename}.csv`);
  $("#dl-html").href = `${base}/report.html?download=1`;
  $("#dl-html").setAttribute("download", `${run.basename}.html`);
  $("#open-tab").href = `${base}/report.html`;
  $("#report-frame").src = `${base}/view`;

  const notice = $("#report-notice");
  notice.replaceChildren();
  const messages = [run.stop_reason, ...run.warnings].filter(Boolean);
  if (messages.length) {
    notice.append(el("strong", { text: run.stop_reason ? "Partial results." : "Heads up." }));
    notice.append(el("ul", {}, ...messages.map((text) => el("li", { text }))));
  }
  notice.hidden = messages.length === 0;

  $("#delete-btn").onclick = async () => {
    if (!confirm(`Delete the report “${run.query}”? This cannot be undone.`)) return;
    try {
      await api(base, { method: "DELETE" });
      await loadHistory();
      location.hash = "#/new";
      toast("Report deleted.", true);
    } catch (error) {
      toast(error.message);
    }
  };
}

// --- routing -------------------------------------------------------------------------------

function route() {
  stopPolling();
  const hash = location.hash;
  let match;
  if ((match = hash.match(/^#\/runs\/([\w-]+)$/))) {
    showReport(match[1]);
  } else if ((match = hash.match(/^#\/job\/(\w+)$/))) {
    showJob(match[1]);
  } else {
    showView("view-new");
    markCurrentRun();
    $("#no-key").hidden = state.status.has_key;
    $("#search-form").hidden = !state.status.has_key;
  }
}

async function init() {
  try {
    state.status = await api("/api/status");
  } catch {
    document.body.replaceChildren(el("p", { class: "toast", text: "Lead Radar server is not reachable. Is `lead-radar web` running?" }));
    return;
  }

  $("#search-form").addEventListener("submit", estimate);
  $("#search-form").addEventListener("input", formChanged);
  $("#demo-btn").addEventListener("click", startDemo);
  $("#with-reviews").addEventListener("change", (event) => {
    $("#review-limit-row").hidden = !event.target.checked;
  });
  $("#tags").addEventListener("click", () => $("#location-input").focus());
  const input = $("#location-input");
  input.addEventListener("keydown", (event) => {
    if ((event.key === "Enter" || event.key === ";") && input.value.trim()) {
      event.preventDefault();
      addLocation(input.value);
      input.value = "";
    } else if (event.key === "Enter") {
      // empty input: let Enter submit the form
    } else if (event.key === "Backspace" && !input.value && state.locations.length) {
      state.locations.pop();
      renderTags();
      formChanged();
    }
  });
  input.addEventListener("blur", () => {
    if (addLocation(input.value)) input.value = "";
  });

  restoreForm();
  window.addEventListener("hashchange", route);
  await Promise.all([loadHistory(), loadAccount()]);

  if (state.status.active_job && !location.hash.startsWith("#/job/")) {
    location.hash = `#/job/${state.status.active_job}`;
  } else {
    route();
  }
}

init();
