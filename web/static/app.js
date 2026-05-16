"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

async function getJSON(url) {
  const r = await fetch(url);
  return r.json();
}
async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : null,
  });
  return { ok: r.ok, status: r.status, data: await r.json() };
}

// ── Navigation ───────────────────────────────────────────
$$(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".nav-item").forEach((b) => b.classList.remove("active"));
    $$(".view").forEach((v) => v.classList.remove("active"));
    btn.classList.add("active");
    $("#view-" + btn.dataset.view).classList.add("active");
    if (btn.dataset.view === "history") loadRuns();
    if (btn.dataset.view === "reports") loadReports();
  });
});

// ── Clock ────────────────────────────────────────────────
function tickClock() {
  $("#clock").textContent = new Date().toLocaleString("en-US", {
    weekday: "short", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit",
  });
}
setInterval(tickClock, 1000);
tickClock();

// ── Mode badge ───────────────────────────────────────────
getJSON("/api/meta").then((m) => {
  const b = $("#mode-badge");
  if (m.paper) { b.textContent = "PAPER"; b.classList.add("paper"); }
  else { b.textContent = "LIVE"; b.classList.add("live"); }
});

// ── Scheduler ────────────────────────────────────────────
function fmtWhen(iso) {
  const d = new Date(iso);
  return d.toLocaleString("en-US", {
    weekday: "short", hour: "numeric", minute: "2-digit",
  });
}

async function refreshScheduler() {
  const s = await getJSON("/api/scheduler");
  const pill = $("#sched-pill");
  pill.className = "pill pill-" + (s.status || "unknown");
  pill.textContent = "scheduler: " + (s.status || "unknown");

  const detail = $("#sched-detail");
  if (s.status === "stopped") {
    detail.innerHTML = "Not running.";
  } else {
    let age = s.last_tick_age_seconds;
    detail.innerHTML =
      `PID <b>${s.pid}</b> · last tick ${age != null ? age + "s ago" : "?"}` +
      (s.started_at ? ` · up since ${fmtWhen(s.started_at)}` : "") +
      (s.status === "stale" ? ` · <span style="color:var(--amber)">heartbeat silent</span>` : "");
  }

  const up = $("#sched-upcoming");
  if (s.upcoming && s.upcoming.length) {
    up.innerHTML =
      "<div class='muted' style='margin-top:10px'>Upcoming jobs:</div>" +
      s.upcoming
        .map((j) => `<div>· ${fmtWhen(j.at)} — <b>${j.name}</b> <span class="muted">(${j.command})</span></div>`)
        .join("");
  } else {
    up.innerHTML = "";
  }

  $("#sched-start").disabled = s.status === "running" || s.status === "stale";
  $("#sched-stop").disabled = s.status === "stopped";
}

async function schedAction(action) {
  const r = await postJSON(`/api/scheduler/${action}`);
  if (!r.ok) alert(r.data.error || "Action failed");
  setTimeout(refreshScheduler, 1000);
}
$("#sched-start").addEventListener("click", () => schedAction("start"));
$("#sched-stop").addEventListener("click", () => {
  if (confirm("Stop the scheduler? Automated scans/monitors will halt.")) schedAction("stop");
});
$("#sched-restart").addEventListener("click", () => schedAction("restart"));
setInterval(refreshScheduler, 5000);
refreshScheduler();

// ── Portfolio status ─────────────────────────────────────
$("#status-refresh").addEventListener("click", async () => {
  const body = $("#status-body");
  body.textContent = "Loading…";
  const s = await getJSON("/api/status");
  if (!s.ok) { body.innerHTML = `<span style="color:var(--red)">${s.error}</span>`; return; }
  let html = `<div class="kv">
    <div>Portfolio <b>$${s.portfolio_value.toLocaleString()}</b></div>
    <div>Buying power <b>$${s.buying_power.toLocaleString()}</b></div>
    <div>Stocks <b>${s.stock_positions}</b></div>
    <div>Open calls <b>${s.open_calls}</b></div></div>`;
  if (s.tracked.length) {
    html += "<div style='margin-top:12px'><b>Tracked covered calls</b></div>";
    s.tracked.forEach((p) => {
      html += `<div class="muted">${p.symbol} · $${p.strike} · exp ${p.expiry} · DTE ${p.dte} · x${p.contracts} @ $${p.entry_premium}</div>`;
    });
  }
  body.innerHTML = html;
});

// ── Run (SSE live terminal) ──────────────────────────────
let sse = null;
function appendTerm(el, text) {
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  el.textContent += text + "\n";
  if (atBottom) el.scrollTop = el.scrollHeight;
}
$$(".run-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const cmd = btn.dataset.cmd;
    const term = $("#terminal");
    term.textContent = "";
    $$(".run-btn").forEach((b) => (b.disabled = true));
    $("#run-state").textContent = `running ${cmd}…`;

    sse = new EventSource(`/stream/${cmd}`);
    sse.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (msg.event === "line") appendTerm(term, msg.payload);
      else if (msg.event === "start") appendTerm(term, `--- started: ${msg.payload.command} ---`);
      else if (msg.event === "done") {
        appendTerm(term, `--- finished: ${msg.payload.status} (exit ${msg.payload.exit_code}) ---`);
        sse.close();
        $$(".run-btn").forEach((b) => (b.disabled = false));
        $("#run-state").textContent = "";
      }
    };
    sse.onerror = () => {
      appendTerm(term, "--- stream error / closed ---");
      sse.close();
      $$(".run-btn").forEach((b) => (b.disabled = false));
      $("#run-state").textContent = "";
    };
  });
});

// ── Execute ──────────────────────────────────────────────
$("#exec-load").addEventListener("click", async () => {
  const d = await getJSON("/api/execute/pending");
  const notices = $("#exec-notices");
  const wrap = $("#exec-trades");
  notices.innerHTML = "";
  wrap.innerHTML = "";
  $("#exec-output").hidden = true;
  if (!d.ok) { notices.innerHTML = `<div class="notice">${d.error}</div>`; return; }

  (d.notices || []).forEach((n) => {
    notices.innerHTML += `<div class="notice">${n}</div>`;
  });
  $("#exec-summary").innerHTML = d.trades.length
    ? `<div class="kv"><div>Portfolio <b>$${d.portfolio_value.toLocaleString()}</b></div>
       <div>Slots <b>${d.open_positions}/${d.max_positions}</b></div>
       <div>Total premium <b>$${d.total_premium.toLocaleString()}</b></div></div>`
    : "";

  d.trades.forEach((t) => {
    const card = document.createElement("div");
    card.className = "trade-card";
    card.innerHTML = `
      <input type="checkbox" value="${t.symbol}">
      <div>
        <div class="sym">${t.symbol} <span class="muted">${(t.score * 100).toFixed(0)}% · ${t.stock_type}</span></div>
        <div class="muted">${t.brief}</div>
        <div class="grid">
          <div>Strike <b>$${t.recommended_strike.toFixed(2)}</b> (Δ${t.delta.toFixed(2)})</div>
          <div>Expiry <b>${t.recommended_expiry}</b></div>
          <div>Contracts <b>${t.contracts_to_write}</b></div>
          <div>Premium <b>$${t.premium_per_share.toFixed(2)}/sh = $${t.premium_total.toLocaleString()}</b></div>
        </div>
      </div>`;
    wrap.appendChild(card);
  });
  $("#exec-submit").disabled = d.trades.length === 0;
});

$("#exec-submit").addEventListener("click", async () => {
  const symbols = [...$$("#exec-trades input:checked")].map((c) => c.value);
  if (!symbols.length) { alert("Select at least one trade."); return; }
  if (!confirm(`Submit REAL sell-to-open orders for: ${symbols.join(", ")}?`)) return;

  $("#exec-submit").disabled = true;
  const r = await postJSON("/api/execute/submit", { symbols });
  const out = $("#exec-output");
  out.hidden = false;
  if (!r.ok) { out.textContent = r.data.error || "Submit failed"; return; }
  out.textContent =
    `Submitted: ${r.data.result.succeeded.join(", ") || "none"}\n` +
    `Failed: ${r.data.result.failed.join(", ") || "none"}\n\n` +
    `(run ${r.data.run.id})`;
});

// ── History ──────────────────────────────────────────────
async function loadRuns() {
  const runs = await getJSON("/api/runs");
  const list = $("#run-list");
  list.innerHTML = "";
  runs.forEach((r) => {
    const li = document.createElement("li");
    const cls = r.status === "success" ? "ok" : r.status === "error" ? "err" : "run";
    li.innerHTML = `<span class="tag ${cls}">${r.status}</span> <b>${r.command}</b>
      <div class="meta">${r.trigger} · ${new Date(r.started_at).toLocaleString()}</div>`;
    li.addEventListener("click", async () => {
      $$("#run-list li").forEach((x) => x.classList.remove("sel"));
      li.classList.add("sel");
      const d = await getJSON(`/api/runs/${r.id}`);
      $("#run-detail").textContent = d.output || "(no output)";
    });
    list.appendChild(li);
  });
}

// ── Reports ──────────────────────────────────────────────
async function loadReports() {
  const files = await getJSON("/api/reports");
  const list = $("#report-list");
  list.innerHTML = "";
  files.forEach((name) => {
    const li = document.createElement("li");
    li.textContent = name;
    li.addEventListener("click", async () => {
      $$("#report-list li").forEach((x) => x.classList.remove("sel"));
      li.classList.add("sel");
      const d = await getJSON(`/api/reports/${encodeURIComponent(name)}`);
      $("#report-detail").textContent = d.content || d.error || "(empty)";
    });
    list.appendChild(li);
  });
  if (!files.length) list.innerHTML = "<li class='muted'>No reports yet.</li>";
}
