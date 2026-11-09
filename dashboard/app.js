"use strict";

const taskNames = {
  "scanner-daily": "Daily market scan",
  "scanner-orb": "Opening-range scan",
  news: "News monitor",
  entry: "Entry check",
  "exit-plan": "Exit planner",
  timemachine: "Time Machine replay",
  budget: "Position budget",
  risk: "Risk check",
  tickets: "Broker tickets (view only)",
  "alerts-check": "Alert check",
  "alerts-clock": "NYSE clock",
  "alerts-list": "Alert rules",
  analyst: "Earnings analyst",
  pelosi: "Pelosi filings",
  "pelosi-new": "New Congress filings",
  quote: "Quote",
  "alerts-plan": "Plan alerts",
  universe: "Stock universe"
};
const symbolTasks = ["news", "entry", "analyst", "quote"];
const workflowFiles = ["watchlist.json", "entry.json", "plan.json", "timemachine.json", "orders.json", "state/risk.json"];
const SVG_NS = "http://www.w3.org/2000/svg";
const activityItems = new Map();
const runningJobs = new Set();
const $ = (selector, root = document) => root.querySelector(selector);

function nyClock() {
  const clock = $("#ny-clock");
  clock.textContent = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: true
  }).format(new Date());
}

function getFile(state, name) {
  return state && state.files ? state.files[name] : null;
}

function formatTime(value) {
  if (!value) return "Not recorded";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit"
  }).format(date);
}

function setMode(state) {
  const tag = $("#account-mode");
  const mode = state.paper || "unknown";
  tag.className = "mode-tag " + mode;
  tag.querySelector("b").textContent =
    mode === "paper" ? "PAPER CONFIG" : mode === "live" ? "LIVE CONFIG" : "MODE UNKNOWN";
  tag.title = mode === "live"
    ? "The local config says paper trading is disabled. The dashboard still cannot submit broker orders."
    : mode === "paper"
      ? "The local config says paper trading is enabled."
      : "No readable trader/config.json with a paper setting was found.";
}

function updateWorkflowRow(filename, exists, detail) {
  const row = document.querySelector('[data-file="' + filename + '"]');
  if (!row) return;
  const stateCell = row.querySelector(".state-cell");
  stateCell.classList.toggle("present", Boolean(exists));
  stateCell.querySelector("b").textContent = exists ? detail : defaultRowLabel(filename);
}

function defaultRowLabel(filename) {
  const labels = {
    "watchlist.json": "No watchlist",
    "entry.json": "No entry check",
    "plan.json": "No plan",
    "timemachine.json": "No replay",
    "orders.json": "No order proposal",
    "state/risk.json": "No risk verdict"
  };
  return labels[filename] || "Not saved";
}

function updateDashboard(state) {
  const files = state.files || {};
  const config = getFile(state, "config.json");
  const scan = getFile(state, "watchlist.json");
  const entry = getFile(state, "entry.json");
  const plan = getFile(state, "plan.json");
  const orders = getFile(state, "orders.json");
  const scanData = scan && scan.data;
  const entryData = entry && entry.data;
  const planData = plan && plan.data;
  const orderData = orders && orders.data;
  setMode(state);

  const existing = Object.keys(files).filter((key) => files[key].exists);
  const latest = existing
    .map((key) => files[key].modified)
    .filter(Boolean)
    .sort()
    .pop();
  $("#updated-at").textContent = latest ? formatTime(latest) : "No saved files";
  $("#session-state").textContent = config && config.exists
    ? (state.paper === "live" ? "Live configuration detected" : state.paper === "paper" ? "Paper configuration loaded" : "Configuration found")
    : "Workspace not initialized";
  $("#session-detail").textContent = config && config.error
    ? "Configuration could not be read: " + config.error
    : config && config.exists
      ? "Risk limits and operating settings are read from trader/config.json"
      : "Run the existing tools to create trader/ outputs";

  const hits = scanData && Array.isArray(scanData.hits) ? scanData.hits : [];
  $("#scan-state").textContent = scan && scan.exists
    ? (hits.length + " matched chart" + (hits.length === 1 ? "" : "s"))
    : "No scan saved";
  $("#scan-detail").textContent = scan && scan.exists
    ? [scanData.mode, scanData.as_of ? formatTime(scanData.as_of) : null, scanData.scanned != null ? scanData.scanned + " scanned" : null].filter(Boolean).join(" · ")
    : "Run the scanner to refresh the watchlist";

  const planSymbol = planData && planData.symbol ? planData.symbol : "";
  $("#plan-state").textContent = planSymbol ? planSymbol + " long plan" : plan && plan.exists ? "Plan saved" : "No plan saved";
  $("#plan-detail").textContent = planData
    ? [planData.entry != null ? "Entry " + planData.entry : null, planData.stop != null ? "Stop " + planData.stop : null, planData.target != null ? "Target " + planData.target : null].filter(Boolean).join(" · ")
    : "Exit plans are read from trader/plan.json";

  const watchCount = hits.length;
  const replay = getFile(state, "timemachine.json");
  const replayData = replay && replay.data;
  const risk = getFile(state, "state/risk.json");
  const riskData = risk && risk.data;
  const orderList = orderData && Array.isArray(orderData.orders) ? orderData.orders : [];
  $("#workflow-count").textContent = existing.filter((key) => workflowFiles.includes(key)).length + " / " + workflowFiles.length + " FILES";
  updateWorkflowRow("watchlist.json", Boolean(scan && scan.exists), watchCount + " matched");
  updateWorkflowRow("entry.json", Boolean(entry && entry.exists), entryData && entryData.status ? [entryData.symbol, String(entryData.status).toUpperCase()].filter(Boolean).join(" ") : "Entry checked");
  updateWorkflowRow("plan.json", Boolean(plan && plan.exists), planSymbol ? planSymbol + " planned" : "Plan saved");
  updateWorkflowRow("timemachine.json", Boolean(replay && replay.exists), replayData && replayData.mc ? pct(replayData.mc.p_target) + " target first" : "Replay saved");
  updateWorkflowRow("orders.json", Boolean(orders && orders.exists), orderList.length + " proposed");
  updateWorkflowRow("state/risk.json", Boolean(risk && risk.exists), riskData ? riskData.pass + " pass · " + riskData.block + " block" : "Verdict saved");
  renderPlan(planData);
  renderReplay(replayData, planData);
  renderRisk(orderList, riskData);
  const alertFile = getFile(state, "alerts.json");
  renderAlerts(alertFile && alertFile.data && Array.isArray(alertFile.data.rules) ? alertFile.data.rules : []);

  $("#watchlist-caption").textContent = scan && scan.exists
    ? [watchCount + " matched", scanData.as_of ? "as of " + formatTime(scanData.as_of) : null, scanData.failed ? scanData.failed + " unavailable" : null].filter(Boolean).join(" · ")
    : "No saved scan results";
  renderWatchlist(hits);
  const path = Object.keys(files).find((key) => files[key].error);
  $("#workspace-path").textContent = path ? "Read issue: " + path : "Local files only · no broker connection";
}

function renderWatchlist(hits) {
  const container = $("#watchlist-rows");
  container.className = "";
  container.replaceChildren();
  if (!hits.length) {
    const empty = document.createElement("div");
    empty.className = "empty-row";
    empty.setAttribute("role", "row");
    empty.textContent = "Run a scan to populate this table. A match is a chart rule, not a recommendation.";
    container.append(empty);
    return;
  }
  hits.slice(0, 8).forEach((hit) => {
    const row = document.createElement("div");
    row.className = "watch-row";
    row.setAttribute("role", "row");
    const symbol = document.createElement("strong");
    symbol.className = "ticker-name";
    symbol.textContent = hit.symbol || "Unknown";
    const setup = document.createElement("span");
    setup.textContent = Array.isArray(hit.setups)
      ? hit.setups.join(", ")
      : hit.setup || hit.type || "Matched rule";
    const at = document.createElement("span");
    at.textContent = hit.at || hit.time || hit.close_date || "—";
    const status = document.createElement("span");
    status.className = "row-tag";
    status.textContent = "MATCHED";
    row.append(symbol, setup, at, status);
    container.append(row);
  });
  if (hits.length > 8) {
    const more = document.createElement("div");
    more.className = "watch-more";
    more.textContent = (hits.length - 8) + " more matches in trader/watchlist.json";
    container.append(more);
  }
}

function pct(value) {
  return value == null ? "—" : (value * 100).toFixed(1) + "%";
}

function money(value) {
  return value == null || Number.isNaN(Number(value)) ? "—" : "$" + Number(value).toFixed(2);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function setTag(id, text, tone) {
  const tag = $(id);
  tag.textContent = text;
  tag.className = "row-tag " + (tone || "muted-tag");
}

function empty(container, message) {
  container.replaceChildren(el("p", "card-empty", message));
}

function stat(label, value) {
  const cell = el("div", "stat");
  cell.append(el("span", null, label), el("b", null, value));
  return cell;
}

function renderPlan(plan) {
  const ladder = $("#plan-ladder");
  if (!plan || plan.entry == null) {
    setTag("#plan-card-tag", "NONE");
    empty(ladder, "Check an entry, then build a plan. The stop and target are set before any order.");
    return;
  }
  setTag("#plan-card-tag", [plan.symbol, plan.date].filter(Boolean).join(" · "), "");
  const levels = [
    ["TARGET", plan.target, plan.target_label, "level-target"],
    ["ENTRY", plan.entry, plan.entry_time ? "at " + plan.entry_time : "", "level-entry"],
    ["STOP", plan.stop, plan.stop_label, "level-stop"]
  ];
  const rows = levels.map(([name, price, note, tone]) => {
    const row = el("div", "ladder-row " + tone);
    row.append(el("span", "ladder-name", name), el("b", null, money(price)), el("small", null, note || ""));
    return row;
  });
  const stats = el("div", "stat-row");
  stats.append(
    stat("RISK / SHARE", money(plan.risk_per_share)),
    stat("REWARD : RISK", plan.reward_risk != null ? String(plan.reward_risk) : "—"),
    stat("FLAT BY", plan.flat_by ? plan.flat_by + " ET" : "—")
  );
  ladder.replaceChildren(...rows, stats);
}

function fanChart(mc, plan) {
  // bands: one row per bar after the entry, [p5, p25, p50, p75, p95]
  const bands = mc.bands;
  const width = 320;
  const height = 120;
  const lows = bands.map((b) => b[0]).concat(plan ? [plan.stop] : []);
  const highs = bands.map((b) => b[4]).concat(plan ? [plan.target] : []);
  const lo = Math.min(...lows);
  const hi = Math.max(...highs);
  const x = (i) => (i / Math.max(1, bands.length - 1)) * width;
  const y = (v) => height - ((v - lo) / Math.max(1e-9, hi - lo)) * height;
  const area = (a, b) => bands.map((r, i) => x(i) + "," + y(r[a])).join(" ") + " " +
    bands.map((r, i) => [i, r]).reverse().map(([i, r]) => x(i) + "," + y(r[b])).join(" ");
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 " + width + " " + height);
  svg.setAttribute("class", "fan-chart");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Range of replayed prices from the entry to the close");
  const add = (name, attrs) => {
    const node = document.createElementNS(SVG_NS, name);
    Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
    svg.append(node);
  };
  add("polygon", { points: area(0, 4), class: "fan-outer" });
  add("polygon", { points: area(1, 3), class: "fan-inner" });
  add("polyline", { points: bands.map((r, i) => x(i) + "," + y(r[2])).join(" "), class: "fan-median" });
  if (plan) {
    [["target", plan.target], ["entry", plan.entry], ["stop", plan.stop]].forEach(([name, v]) => {
      if (v != null) add("line", { x1: 0, x2: width, y1: y(v), y2: y(v), class: "fan-level fan-" + name });
    });
  }
  return svg;
}

function renderReplay(replay, plan) {
  const body = $("#tm-body");
  if (!replay || !replay.mc) {
    setTag("#tm-card-tag", "NO REPLAY");
    empty(body, "Replay the saved plan 10,000 times from the stock's own recent bars.");
    return;
  }
  const mc = replay.mc;
  const replayPlan = replay.plan && replay.plan.entry != null ? replay.plan : plan;
  const stale = plan && replay.plan && ["symbol", "date", "entry", "stop", "target"].some((k) => plan[k] !== replay.plan[k]);
  setTag("#tm-card-tag", stale ? "OLDER PLAN" : mc.replays.toLocaleString() + " RUNS · SEED " + mc.seed, stale ? "block-tag" : "");
  const bar = el("div", "odds-bar");
  [["odds-target", mc.p_target], ["odds-neither", mc.p_neither], ["odds-stop", mc.p_stop]].forEach(([cls, p]) => {
    const seg = el("span", cls);
    seg.style.width = Math.max(0, p * 100) + "%";
    bar.append(seg);
  });
  const legend = el("div", "odds-legend");
  legend.append(
    el("span", "key-target", "Target first " + pct(mc.p_target)),
    el("span", "key-neither", "Neither " + pct(mc.p_neither)),
    el("span", "key-stop", "Stop first " + pct(mc.p_stop))
  );
  const stats = el("div", "stat-row");
  stats.append(
    stat("GREEN", pct(mc.p_green)),
    stat("AVG R", mc.avg_R != null ? (mc.avg_R > 0 ? "+" : "") + mc.avg_R.toFixed(3) : "—"),
    stat("CLOSE 90% RANGE", money(mc.p5_close) + " – " + money(mc.p95_close))
  );
  const nodes = stale
    ? [el("p", "card-verdict", "This replay is for an older plan (stop " + money(replay.plan.stop) + ", target " +
        money(replay.plan.target) + "). Run the replay again for the saved plan.")]
    : [];
  nodes.push(bar, legend);
  if (Array.isArray(mc.bands) && mc.bands.length > 1) nodes.push(fanChart(mc, replayPlan));
  nodes.push(stats);
  const h = replay.history;
  if (h && h.sessions) {
    nodes.push(el("p", "card-note", "History: fired " + h.triggered + " of " + h.sessions + " sessions — " +
      h.target + " target, " + h.stop + " stop, " + h.close + " neither (avg R " + h.avg_R + ")."));
  }
  if (replay.verdict) nodes.push(el("p", "card-verdict", replay.verdict));
  body.replaceChildren(...nodes);
}

function renderRisk(orders, risk) {
  const body = $("#risk-body");
  if (!orders.length && !risk) {
    setTag("#risk-card-tag", "UNCHECKED");
    empty(body, "Size the plan, then run the risk check. Placing an order stays in the chat, with your yes.");
    return;
  }
  const nodes = [];
  orders.forEach((order) => {
    const line = el("div", "order-line");
    const plan = order.plan || {};
    line.append(
      el("b", null, [String(order.side || "").toUpperCase(), order.qty || order.notional, order.symbol].filter(Boolean).join(" ")),
      el("span", null, [order.type, order.order_class].filter(Boolean).join(" · ")),
      el("small", null, "stop " + (order.stop_loss_stop_price || "—") + " · target " + (order.take_profit_limit_price || "—") +
        (plan.max_loss != null ? " · max loss " + money(plan.max_loss) : ""))
    );
    nodes.push(line);
  });
  if (risk && Array.isArray(risk.results)) {
    const stale = risk.date && risk.date !== new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(new Date());
    setTag("#risk-card-tag", risk.block ? "BLOCK" : risk.pass ? "PASS" : "CHECKED", risk.block ? "block-tag" : "");
    risk.results.forEach((r) => {
      const verdict = el("div", "verdict " + (r.verdict === "PASS" ? "verdict-pass" : "verdict-block"));
      verdict.append(el("b", null, r.verdict + "  " + r.symbol + " " + (r.amount || "")));
      (r.reasons || []).concat(r.warnings || []).forEach((reason) => verdict.append(el("small", null, reason)));
      nodes.push(verdict);
    });
    nodes.push(el("p", "card-note", "Checked " + (risk.now_new_york || risk.date) + " New York" +
      (stale ? " — not today; run the check again." : risk.pass ? ". A PASS expires after 15 minutes." : ".")));
  } else {
    setTag("#risk-card-tag", "UNCHECKED");
    nodes.push(el("p", "card-note", "Not risk-checked yet."));
  }
  body.replaceChildren(...nodes);
}

function renderAlerts(rules) {
  const container = $("#alert-rows");
  $("#alerts-caption").textContent = rules.length ? rules.length + " rule" + (rules.length === 1 ? "" : "s") + " in trader/alerts.json" : "No alert rules saved";
  if (!rules.length) {
    container.replaceChildren(el("div", "empty-row", "Build a plan, then add its stop, target and flat-by alerts."));
    return;
  }
  container.replaceChildren(...rules.map((rule) => {
    const row = el("div", "watch-row");
    row.setAttribute("role", "row");
    const level = rule.price != null ? money(rule.price) : rule.pct != null ? rule.pct + "%" : rule.at ? rule.at + " ET" : "—";
    row.append(
      el("strong", "ticker-name", rule.symbol || "NYSE"),
      el("span", null, rule.label || rule.type || "rule"),
      el("span", null, level),
      el("span", "row-tag", rule.date || "ANY DAY")
    );
    return row;
  }));
}

async function refreshState() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not read the workspace.");
    updateDashboard(result);
  } catch (error) {
    $("#session-state").textContent = "Dashboard unavailable";
    $("#session-detail").textContent = error.message;
  }
}

function addActivity(id, task, status) {
  const list = $("#activity-list");
  if (list.querySelector(".activity-empty")) list.replaceChildren();
  const item = document.createElement("article");
  item.className = "activity-item " + status;
  item.dataset.jobId = id;
  const head = document.createElement("div");
  head.className = "activity-item-head";
  const title = document.createElement("strong");
  title.textContent = taskNames[task] || task;
  const state = document.createElement("span");
  state.className = "activity-state";
  state.textContent = status.toUpperCase();
  head.append(title, state);
  const output = document.createElement("pre");
  output.className = "task-output";
  output.textContent = "Starting local Python tool…";
  item.append(head, output);
  list.prepend(item);
  activityItems.set(id, item);
  while (list.children.length > 8) list.lastElementChild.remove();
  return item;
}

function updateActivity(job) {
  const item = activityItems.get(job.id);
  if (!item) return;
  item.className = "activity-item " + job.status;
  item.querySelector(".activity-state").textContent =
    job.status === "finished" ? "EXIT " + job.exit_code : job.status.toUpperCase();
  const output = item.querySelector(".task-output");
  if (job.status === "queued" || job.status === "running") {
    output.textContent = job.status === "queued" ? "Waiting for a local worker…" : "Tool is running. This may take a few minutes.";
    return;
  }
  output.textContent = job.error
    ? job.error + (job.output ? "\n\n" + job.output : "")
    : (job.output || (job.status === "completed" ? "Task completed with no console output." : "Tool finished with exit code " + job.exit_code + "."));
  runningJobs.delete(job.id);
  $("#activity-status").textContent = runningJobs.size ? "RUNNING" : "READY";
  refreshState();
}

async function watchJob(id) {
  runningJobs.add(id);
  $("#activity-status").textContent = "RUNNING";
  while (runningJobs.has(id)) {
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
    try {
      const response = await fetch("/api/jobs/" + encodeURIComponent(id), { cache: "no-store" });
      const job = await response.json();
      if (!response.ok) throw new Error(job.error || "Could not read task status.");
      updateActivity(job);
    } catch (error) {
      updateActivity({ id, status: "failed", error: error.message });
    }
  }
}

async function startTask(task, button) {
  const payload = { task };
  if (task === "scanner-daily" || task === "scanner-orb") payload.symbols = $("#scan-symbols").value.trim();
  if (task === "scanner-orb") payload.at = $("#scan-at").value.trim();
  if (task === "exit-plan") {
    payload.stop = $("#plan-stop").value;
    payload.target = $("#plan-target").value;
  }
  if (task === "timemachine") payload.seed = $("#tm-seed").value.trim();
  if (symbolTasks.includes(task)) {
    const symbol = $("#symbol-input").value.trim().toUpperCase();
    if (!symbol) {
      $("#symbol-input").focus();
      $("#symbol-hint").textContent = "Enter a ticker before running this task.";
      $("#symbol-hint").classList.add("error-text");
      return;
    }
    payload.symbol = symbol;
  }
  $("#symbol-hint").textContent = "Use a ticker symbol; no order actions are available.";
  $("#symbol-hint").classList.remove("error-text");
  button.disabled = true;
  try {
    const response = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "The task could not be started.");
    addActivity(result.id, task, result.status);
    watchJob(result.id);
  } catch (error) {
    const id = "error-" + Date.now();
    const item = addActivity(id, task, "failed");
    item.querySelector(".task-output").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

document.querySelectorAll("[data-task]").forEach((button) => {
  button.addEventListener("click", () => startTask(button.dataset.task, button));
});
$("#refresh-button").addEventListener("click", refreshState);
window.addEventListener("DOMContentLoaded", () => {
  nyClock();
  window.setInterval(nyClock, 1000);
  refreshState();
  window.setInterval(refreshState, 30000);
});
