// Single-file vanilla JS frontend for the YUYUTSAVA daemon.
// Connects to /stream (SSE) and renders three panes: timeline, agent, rules.

const $ = (id) => document.getElementById(id);
const fmtTime = (ts) => new Date((ts || 0) * 1000).toLocaleTimeString();

const state = {
  proposals: new Map(),  // id -> proposal
  asks: new Map(),       // id -> ask
};

function setStatus(text, cls) {
  const el = $("status");
  el.textContent = text;
  el.className = "status " + (cls || "");
}

function timelinePush({ ts, line, cls }) {
  const li = document.createElement("li");
  li.className = cls || "";
  li.innerHTML = `<span class="ts">${fmtTime(ts || Date.now() / 1000)}</span><span>${escapeHtml(line)}</span>`;
  const ol = $("timeline");
  ol.prepend(li);
  while (ol.children.length > 200) ol.lastChild.remove();
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

function renderAgentPanel() {
  const panel = $("agent-panel");
  panel.innerHTML = "";
  const items = [...state.proposals.values(), ...state.asks.values()];
  if (items.length === 0) {
    panel.innerHTML = '<div class="empty">No active proposals.</div>';
    return;
  }
  for (const p of state.proposals.values()) panel.appendChild(renderProposal(p));
  for (const a of state.asks.values())     panel.appendChild(renderAsk(a));
}

function renderProposal(p) {
  const div = document.createElement("div");
  div.className = "proposal";
  div.innerHTML = `
    <h4>${escapeHtml(p.topic)} — ${escapeHtml(p.subagent)}</h4>
    <div class="meta">Event: ${escapeHtml(p.summary)} • Urgency ${p.urgency}</div>
    <div><strong>Proposed:</strong> ${escapeHtml(p.proposed)}</div>
    <textarea class="edit" rows="2" style="display:none">${escapeHtml(p.proposed)}</textarea>
    <div class="actions">
      <button class="primary" data-decision="approve">Approve</button>
      <button data-decision="approve_remember">Approve &amp; remember</button>
      <button data-decision="modify">Modify</button>
      <button class="bad" data-decision="skip">Skip</button>
      <button class="bad" data-decision="skip_remember">Skip &amp; remember</button>
    </div>
  `;
  div.querySelectorAll("button[data-decision]").forEach((btn) => {
    btn.addEventListener("click", () => respondProposal(p.proposal_id, btn.dataset.decision, div));
  });
  return div;
}

function renderAsk(a) {
  const div = document.createElement("div");
  div.className = "ask";
  div.innerHTML = `
    <h4>${escapeHtml(a.title)}</h4>
    <div>${escapeHtml(a.body)}</div>
    <div class="actions"></div>
    <input class="freetext" placeholder="Or type a response…" style="margin-top:0.5rem; width:100%;
           background: var(--bg); color: var(--fg); border: 1px solid var(--border);
           padding: 0.4rem; border-radius: 4px;" />
  `;
  const actions = div.querySelector(".actions");
  const opts = (a.options || []).length ? a.options : ["approve", "reject"];
  for (const opt of opts) {
    const b = document.createElement("button");
    b.className = opt === "approve" ? "primary" : "";
    b.textContent = opt;
    b.addEventListener("click", () => respondAsk(a.ask_id, opt));
    actions.appendChild(b);
  }
  div.querySelector(".freetext").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.value.trim()) respondAsk(a.ask_id, e.target.value.trim());
  });
  return div;
}

async function respondProposal(id, decision, container) {
  let edited;
  if (decision === "modify") {
    const ta = container.querySelector("textarea.edit");
    ta.style.display = "block";
    edited = ta.value.trim();
    if (!edited) { ta.focus(); return; }
  }
  try {
    const r = await fetch(`/proposal/${encodeURIComponent(id)}/respond`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, edited_instruction: edited }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    state.proposals.delete(id);
    renderAgentPanel();
    timelinePush({ line: `Proposal ${decision} (${id.slice(0, 8)}…)`,
                   cls: "event-decision-" + (decision.startsWith("approve") ? "approved" : "skipped") });
  } catch (err) {
    timelinePush({ line: `Failed to respond: ${err.message}`, cls: "event-decision-skipped" });
  }
}

async function respondAsk(id, response) {
  try {
    const r = await fetch(`/ask/${encodeURIComponent(id)}/respond`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ response }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    state.asks.delete(id);
    renderAgentPanel();
    timelinePush({ line: `Ask responded: ${response}`, cls: "event-action" });
  } catch (err) {
    timelinePush({ line: `Ask failed: ${err.message}`, cls: "event-decision-skipped" });
  }
}

async function refreshRules() {
  try {
    const r = await fetch("/rules");
    const rules = await r.json();
    const ul = $("rules");
    ul.innerHTML = "";
    if (!rules.length) {
      ul.innerHTML = '<li class="empty">No rules yet.</li>';
      return;
    }
    for (const rule of rules) {
      const li = document.createElement("li");
      li.innerHTML = `<span><strong>${escapeHtml(rule.decision)}</strong>
                      ${escapeHtml(rule.topic_glob)}
                      <span style="color:var(--muted)">${escapeHtml(rule.match_json)}</span></span>`;
      const btn = document.createElement("button");
      btn.textContent = "revoke";
      btn.addEventListener("click", async () => {
        await fetch(`/rules/${encodeURIComponent(rule.rule_id)}`, { method: "DELETE" });
        refreshRules();
      });
      li.appendChild(btn);
      ul.appendChild(li);
    }
  } catch (err) { /* ignore */ }
}

function appendLive(text) {
  const pre = $("live-stream");
  pre.textContent += text;
  pre.scrollTop = pre.scrollHeight;
  if (pre.textContent.length > 8000) pre.textContent = pre.textContent.slice(-6000);
}

function connect() {
  const es = new EventSource("/stream");
  es.addEventListener("hello", () => setStatus("connected", "ok"));
  es.addEventListener("event", (e) => handleEvent(JSON.parse(e.data)));
  es.addEventListener("proposal", (e) => handleProposal(JSON.parse(e.data)));
  es.addEventListener("ask", (e) => handleAsk(JSON.parse(e.data)));
  es.onerror = () => { setStatus("disconnected", "bad"); };
  es.onopen = () => setStatus("connected", "ok");
}

function handleEvent(payload) {
  const { kind, data } = payload;
  if (kind === "timeline") {
    timelinePush({ ts: data.ts, line: data.line, cls: data.cls || "" });
  } else if (kind === "log") {
    appendLive((data.text || "") + "\n");
  } else if (kind === "token") {
    appendLive(data.text || "");
  } else if (kind === "tool_call") {
    appendLive(`\n→ ${data.name}(${JSON.stringify(data.args || {}).slice(0,120)})\n`);
  } else if (kind === "tool_result") {
    appendLive(`← ${data.name}: ${(data.preview || "").slice(0,200)}\n`);
  }
}

function handleProposal(payload) {
  const p = payload.proposal;
  state.proposals.set(p.proposal_id, p);
  renderAgentPanel();
  timelinePush({ ts: p.created_ts, line: `Proposal: ${p.proposed}`, cls: "event-action" });
}

function handleAsk(payload) {
  const a = payload.ask;
  state.asks.set(a.ask_id, a);
  renderAgentPanel();
  timelinePush({ line: `Asked: ${a.title}`, cls: "event-action" });
}

connect();
refreshRules();
setInterval(refreshRules, 30000);
