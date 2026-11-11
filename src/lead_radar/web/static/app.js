// Lead Radar web UI. Plain JS, no build step. All search, scoring and rendering logic lives
// in the Python API; this file only wires the screens together.
"use strict";

const $ = (selector) => document.querySelector(selector);

const state = {
  status: null,
  account: null,
  locations: [],
  estimate: null, // { signature, total }
  pollTimer: null,
  frameObserver: null,
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
    throw new Error(typeof data.detail === "string" ? data.detail : "Something went wrong.");
  }
  return data;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}

function icon(name, cls = "icon") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function plural(count, word) {
  const many = /(s|sh|ch|x)$/.test(word) ? `${word}es` : `${word}s`;
  return `${count.toLocaleString()} ${count === 1 ? word : many}`;
}

function formatDate(iso, withTime = true) {
  const options = withTime ? { dateStyle: "medium", timeStyle: "short" } : { month: "short", day: "numeric" };
  return new Date(iso).toLocaleString(undefined, options);
}

function toast(message, kind = "error") {
  const node = $("#toast");
  node.replaceChildren(icon(kind === "error" ? "alert" : "check"), el("span", { text: message }));
  node.className = `toast ${kind}`;
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, kind === "error" ? 7000 : 2800);
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

// --- sidebar: account and history ----------------------------------------------------------

async function loadAccount() {
  const box = $("#account");
  if (!state.status.has_key) {
    box.replaceChildren(el("div", { class: "balance-demo" },
      icon("key"),
      el("div", {},
        el("b", { text: "Demo mode" }),
        "Add SERPAPI_API_KEY to .env and restart to run live searches."),
    ));
    return;
  }
  try {
    state.account = await api("/api/account");
  } catch (error) {
    state.account = null;
    box.replaceChildren(el("div", { class: "balance-demo" }, icon("alert"),
      el("div", {}, el("b", { text: "Balance unavailable" }), error.message)));
    return;
  }
  const { total_searches_left: left, searches_per_month: perMonth, plan_name: plan,
    this_month_usage: used } = state.account;
  const share = left !== null && perMonth ? Math.max(0, Math.min(100, (left / perMonth) * 100)) : 0;
  box.replaceChildren(
    el("div", { class: "balance-top" },
      el("span", { class: "balance-label", text: "Searches left" }),
      el("span", { class: "balance-value", text: left === null ? "–" : left.toLocaleString() })),
    el("div", { class: "balance-bar" }, el("span", { style: `width:${share}%` })),
    el("div", { class: "balance-sub",
      text: [plan, used !== null && used !== undefined ? `${used.toLocaleString()} used this month` : null]
        .filter(Boolean).join(" · ") }),
  );
}

async function loadHistory() {
  let runs = [];
  try {
    runs = await api("/api/runs");
  } catch (error) {
    toast(error.message);
  }
  $("#run-count").textContent = runs.length ? String(runs.length) : "";
  const list = $("#history");
  list.replaceChildren();
  if (runs.length === 0) {
    list.append(el("li", { class: "history-empty", text: "Your reports will appear here after the first search." }));
  }
  for (const run of runs) {
    list.append(el("li", {}, el("a", { href: `#/runs/${run.id}`, "data-id": run.id },
      el("span", { class: "h-query", text: run.query }),
      el("span", { class: "h-loc", text: run.locations.join(" · ") }),
      el("span", { class: "h-meta" },
        el("span", { class: "h-hot", text: `${run.hot} hot` }),
        el("span", { text: `${run.leads} leads` }),
        el("span", { text: formatDate(run.generated_at, false) }),
        run.partial ? el("span", { class: "h-flag", text: "partial" }) : null,
        run.demo ? el("span", { class: "h-flag", text: "demo" }) : null),
    )));
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

// --- new search: form ----------------------------------------------------------------------

function renderLocations() {
  const box = $("#tags");
  const input = $("#location-input");
  for (const chip of box.querySelectorAll(".loc")) chip.remove();
  state.locations.forEach((name, index) => {
    const remove = el("button", { type: "button", "aria-label": `Remove ${name}` }, icon("x"));
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      state.locations.splice(index, 1);
      renderLocations();
      formChanged();
    });
    box.insertBefore(el("span", { class: "loc" }, name, remove), input);
  });
  input.placeholder = state.locations.length ? "Add another city" : "Tampa, FL";
}

function addLocation(raw) {
  const name = raw.trim().replace(/\s+/g, " ");
  if (!name) return false;
  if (!state.locations.some((existing) => existing.toLowerCase() === name.toLowerCase())) {
    state.locations.push(name);
    renderLocations();
    formChanged();
  }
  return true;
}

function syncQuickPicks() {
  const value = $("#query").value.trim().toLowerCase();
  for (const button of document.querySelectorAll("#quick button")) {
    button.setAttribute("aria-pressed", String(button.dataset.niche === value));
  }
}

function formPayload() {
  // A city still being typed counts, so nobody loses it by forgetting to press Enter.
  const input = $("#location-input");
  if (input.value.trim()) {
    addLocation(input.value);
    input.value = "";
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
  renderLocations();
}

function formChanged() {
  // An estimate only authorises the exact search it was made for.
  state.estimate = null;
  syncQuickPicks();
  renderCostIdle();
}

function validate(payload) {
  if (!payload.query) return "Enter a niche, for example “roofing contractor”.";
  if (payload.locations.length === 0) return "Add at least one city.";
  return null;
}

// --- new search: cost panel ----------------------------------------------------------------

function renderCostIdle() {
  const panel = $("#cost");
  if (!state.status.has_key) {
    const demo = el("button", { class: "btn go lg block", type: "button" }, icon("play"), "Run the demo");
    demo.addEventListener("click", startDemo);
    panel.replaceChildren(
      el("div", { class: "cost-key" },
        el("span", { class: "icon-wrap" }, icon("key")),
        el("div", {},
          el("strong", { text: "Live searches need a SerpApi key" }),
          el("p", {}, "Put ", el("code", { text: "SERPAPI_API_KEY" }), " in the ", el("code", { text: ".env" }),
            " file and restart ", el("code", { text: "lead-radar web" }), "."))),
      el("p", { class: "hint", text: `Meanwhile, try the full app on sample data: “${state.status.demo.query}” in ${state.status.demo.locations.join(" and ")}. No key, no cost.` }),
      demo,
    );
    return;
  }
  const steps = [
    ["Describe", "Pick a niche and the cities to cover."],
    ["Check the cost", "See how many SerpApi searches it can use. Nothing is spent yet."],
    ["Run and download", "Leads are scored, saved here, and ready as CSV or HTML."],
  ];
  panel.replaceChildren(
    el("span", { class: "eyebrow", text: "How it works" }),
    el("ol", { class: "how" }, steps.map(([title, text], index) =>
      el("li", {}, el("span", { class: "n", text: String(index + 1) }),
        el("div", {}, el("strong", { text: title }), el("span", { text }))))),
    el("button", { class: "btn primary lg block", type: "submit", form: "search-form", id: "estimate-btn" },
      "Estimate cost", icon("arrow")),
  );
}

async function estimate(event) {
  event.preventDefault();
  if (!state.status.has_key) return;
  const payload = formPayload();
  const problem = validate(payload);
  if (problem) { toast(problem); return; }
  remember("form", payload);

  const button = $("#estimate-btn");
  if (button) { button.disabled = true; button.firstChild.textContent = "Estimating…"; }
  try {
    const data = await api("/api/estimate", { method: "POST", body: payload });
    state.estimate = { signature: JSON.stringify(payload), total: data.estimate.total };
    renderReceipt(data, payload);
  } catch (error) {
    toast(error.message);
    renderCostIdle();
  }
}

function renderReceipt(data, payload) {
  const { estimate: cost, account } = data;
  if (account) state.account = account;
  const bound = cost.exact ? "" : "up to";
  const left = account ? account.total_searches_left : null;
  const panel = $("#cost");

  const rows = el("dl", { class: "cost-rows" },
    el("div", {}, el("dt", { text: "Google Maps pages" }), el("dd", { text: `${bound} ${cost.maps_calls}`.trim() })),
    payload.with_reviews
      ? el("div", {}, el("dt", { text: "Review lookups" }), el("dd", { text: `${bound} ${cost.review_calls}`.trim() }))
      : null,
    el("div", {}, el("dt", { text: "Already saved" }),
      el("dd", { class: cost.cached_calls ? "free" : "", text: cost.cached_calls ? `${cost.cached_calls} free` : "0" })),
  );

  let after = null;
  if (left !== null && left !== undefined) {
    const spend = Math.min(cost.total, left);
    const remaining = Math.max(0, left - cost.total);
    const scale = Math.max(left, 1);
    after = el("div", { class: "after-run" },
      "After this run: ", el("b", { text: `${remaining.toLocaleString()}` }), ` of ${left.toLocaleString()} searches left`,
      el("div", { class: "meter" },
        el("span", { class: "left", style: `width:${(remaining / scale) * 100}%` }),
        el("span", { class: "spend", style: `width:${(spend / scale) * 100}%` })));
  } else if (data.account_error) {
    after = el("p", { class: "hint", text: `Could not read your balance: ${data.account_error}` });
  }

  const warning = left !== null && left !== undefined && cost.total > left
    ? el("div", { class: "notice" }, icon("alert"),
      el("span", { text: "This may need more searches than you have left. The run stops when they run out and keeps what it found." }))
    : null;

  const free = cost.total === 0
    ? el("div", { class: "cost-free" }, icon("check"), "Everything is already saved. This run is free.")
    : null;

  const run = el("button", { class: "btn go lg block", type: "button" },
    icon("play"), cost.total === 0 ? "Run search" : `Run search · ${bound} ${plural(cost.total, "search")}`.replace("·  ", "· "));
  run.addEventListener("click", () => startRun(payload, cost.total, run));
  const back = el("button", { class: "btn link", type: "button", text: "Change search" });
  back.addEventListener("click", () => { formChanged(); $("#query").focus(); });

  panel.replaceChildren(...[
    el("div", {},
      el("span", { class: "eyebrow", text: "Cost of this search" }),
      el("div", { class: "cost-total" },
        bound ? el("span", { class: "bound", text: bound }) : null,
        el("b", { text: String(cost.total) }),
        el("span", { text: cost.total === 1 ? "search" : "searches" }))),
    rows,
    free, after, warning,
    el("div", { class: "cost-stack" }, run, back),
  ].filter(Boolean));
}

async function startRun(payload, total, button) {
  if (!state.estimate || state.estimate.signature !== JSON.stringify(payload)) {
    formChanged();
    toast("The search changed. Estimate the cost again.");
    return;
  }
  button.disabled = true;
  try {
    const { job_id: jobId } = await api("/api/jobs", { method: "POST", body: { ...payload, max_searches: total } });
    state.estimate = null;
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
  $("#job-subject").textContent = "";
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

const STAGES = ["maps", "reviews", "scoring"];
const TITLES = { starting: "Getting ready", maps: "Searching Google Maps", reviews: "Reading reviews", scoring: "Scoring leads" };

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
  if (location.hash !== `#/job/${jobId}`) return; // navigated away; the run continues

  $("#job-subject").textContent = job.query ? `${job.query} · ${job.locations.join(" · ")}` : "";
  $("#job-title").textContent = job.cancelling ? "Cancelling…" : TITLES[job.stage] || "Working";
  $("#job-message").textContent = job.stage === "reviews" && job.total
    ? `${job.current + 1} of ${job.total}: ${job.message}`
    : job.message;

  const current = STAGES.indexOf(job.stage);
  for (const item of document.querySelectorAll("#job-steps li")) {
    const index = STAGES.indexOf(item.dataset.step);
    item.hidden = item.dataset.step === "reviews" && !job.with_reviews;
    item.classList.toggle("active", index === current);
    item.classList.toggle("done", current > index);
  }
  const progress = $("#job-progress");
  const determinate = job.stage === "reviews" && job.total;
  progress.classList.toggle("indeterminate", !determinate);
  progress.querySelector(".bar").style.width = determinate ? `${Math.round(((job.current + 1) / job.total) * 100)}%` : "";

  if (job.status === "running") {
    state.pollTimer = setTimeout(() => pollJob(jobId), 600);
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

function fitFrame(frame) {
  if (state.frameObserver) state.frameObserver.disconnect();
  frame.onload = () => {
    const doc = frame.contentDocument;
    if (!doc) return;
    const fit = () => { frame.style.height = `${doc.documentElement.scrollHeight}px`; };
    fit();
    state.frameObserver = new ResizeObserver(fit);
    state.frameObserver.observe(doc.body);
  };
}

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
  const used = run.demo ? "Sample data" : plural(run.api_calls, "search") + " used";
  $("#report-eyebrow").textContent = `Report · ${formatDate(run.generated_at)} · ${used}`;
  $("#report-title").textContent = run.query;
  $("#report-locs").replaceChildren(...run.locations.map((name) => el("span", { class: "loc-chip" }, icon("pin"), name)));

  const base = `/api/runs/${runId}`;
  $("#dl-csv").href = `${base}/report.csv`;
  $("#dl-csv").setAttribute("download", `${run.basename}.csv`);
  $("#dl-html").href = `${base}/report.html?download=1`;
  $("#dl-html").setAttribute("download", `${run.basename}.html`);
  $("#open-tab").href = `${base}/report.html`;

  const frame = $("#report-frame");
  fitFrame(frame);
  frame.src = `${base}/view`;

  $("#delete-btn").onclick = async () => {
    if (!confirm(`Delete the report “${run.query}”? This cannot be undone.`)) return;
    try {
      await api(base, { method: "DELETE" });
      await loadHistory();
      location.hash = "#/new";
      toast("Report deleted.", "ok");
    } catch (error) {
      toast(error.message);
    }
  };
}

// --- routing -------------------------------------------------------------------------------

function route() {
  stopPolling();
  let match;
  if ((match = location.hash.match(/^#\/runs\/([\w-]+)$/))) {
    showReport(match[1]);
  } else if ((match = location.hash.match(/^#\/job\/(\w+)$/))) {
    showJob(match[1]);
  } else {
    showView("view-new");
    markCurrentRun();
    if (!state.estimate) renderCostIdle();
  }
}

async function init() {
  try {
    state.status = await api("/api/status");
  } catch {
    document.body.replaceChildren(el("p", { class: "toast error", text: "The Lead Radar server is not reachable. Is `lead-radar web` running?" }));
    return;
  }

  const form = $("#search-form");
  form.addEventListener("submit", estimate);
  form.addEventListener("input", (event) => { if (event.target.id !== "location-input") formChanged(); });
  form.addEventListener("change", (event) => { if (event.target.id !== "location-input") formChanged(); });
  $("#with-reviews").addEventListener("change", (event) => { $("#review-limit-row").hidden = !event.target.checked; });

  for (const button of document.querySelectorAll("#quick button")) {
    button.addEventListener("click", () => {
      $("#query").value = button.dataset.niche;
      formChanged();
    });
  }

  $("#tags").addEventListener("click", () => $("#location-input").focus());
  const input = $("#location-input");
  input.addEventListener("keydown", (event) => {
    if ((event.key === "Enter" || event.key === ";") && input.value.trim()) {
      event.preventDefault();
      addLocation(input.value);
      input.value = "";
    } else if (event.key === "Backspace" && !input.value && state.locations.length) {
      state.locations.pop();
      renderLocations();
      formChanged();
    }
  });
  input.addEventListener("blur", () => { if (addLocation(input.value)) input.value = ""; });

  restoreForm();
  syncQuickPicks();
  window.addEventListener("hashchange", route);
  await Promise.all([loadHistory(), loadAccount()]);

  if (state.status.active_job && !location.hash.startsWith("#/job/")) {
    location.hash = `#/job/${state.status.active_job}`;
  } else {
    route();
  }
}

init();
