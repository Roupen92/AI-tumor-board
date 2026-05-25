"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// ──────────────────────────────────────────────────────────────────
// State
// ──────────────────────────────────────────────────────────────────
const state = {
  sid: null,
  source: null,
  specialists: [],           // [{id, display_name, color}]
  agentStatus: new Map(),    // id -> "idle" | "researching" | "drafting" | "done" | "skipped" | "abstained" | "agreed" | "disagreed"
  ledger: new Map(),         // label -> evidence entry
  liveEvidence: new Map(),   // label -> { label, source_kind, source_id, title, journal, year, url, article_type, cited_by } (added as panel completes)
  transcript: [],            // [{kind, ...}] mirror of transcript DOM
  currentRound: 0,
  maxRounds: 4,
  evidenceFilter: "all",
};

// Display defaults (used if server doesn't provide). Color order matches existing config.
const AGENT_VISUALS = {
  rad_onc:   { initials: "RO", short: "Rad Onc",   mesh: "Radiotherapy" },
  med_onc:   { initials: "MO", short: "Med Onc",   mesh: "Systemic · targeted" },
  surg_onc:  { initials: "SO", short: "Surg Onc",  mesh: "Surgical · margins" },
  pharm:     { initials: "Rx", short: "Pharm",     mesh: "DailyMed · RxNorm" },
  molecular: { initials: "MX", short: "Mol Onc",   mesh: "Biomarkers · CIViC" },
  pathologist:{initials: "Pa", short: "Path",      mesh: "IHC · diagnosis" },
};

// ──────────────────────────────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────────────────────────────
function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "dataset") Object.assign(e.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, "");
    else if (v === false || v == null) {/* skip */}
    else e.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return e;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function citationLink(n) {
  const key = String(n);
  const entry = state.ledger.get(key) || state.liveEvidence.get(key);
  const title = entry ? `${entry.title} — ${entry.journal || ""} ${entry.year || ""}`.trim() : `Ref ${n}`;
  const href = entry?.url || "#";
  return `<a class="cite" href="${escapeHtml(href)}" target="_blank" rel="noopener" title="${escapeHtml(title)}">[${n}]</a>`;
}

function transformCitations(html) {
  // Match plain numbered citations: [1], [2,3], [1-3], [1; 2]. Cap at 3 digits to avoid
  // accidentally matching years like [2024]. Only transform if at least one of the
  // numbers actually resolves to a ledger entry — otherwise leave the text alone.
  return html.replace(/\[\d{1,3}(?:\s*[-–,;]\s*\d{1,3})*\]/g, (match) => {
    const nums = (match.match(/\d+/g) || []).map((n) => parseInt(n, 10));
    if (nums.length === 0) return match;
    let labels = nums;
    if (nums.length === 2 && /[-–]/.test(match)) {
      const [start, end] = nums;
      if (end >= start && end - start < 50) {
        labels = [];
        for (let i = start; i <= end; i++) labels.push(i);
      }
    }
    const hasAny = labels.some((n) => state.ledger.has(String(n)) || state.liveEvidence.has(String(n)));
    if (!hasAny) return match;        // not a citation — leave alone (likely a year or bracketed note)
    return labels.map(citationLink).join("");
  });
}

function renderMarkdown(text) {
  // Defense in depth: LLM output goes through marked then DOMPurify before
  // hitting innerHTML, so even a prompt-injected `<script>` or `<img onerror=>`
  // can't fire. transformCitations runs last on the already-sanitized HTML.
  const raw = window.marked ? window.marked.parse(text || "") : escapeHtml(text || "").replace(/\n/g, "<br>");
  const clean = window.DOMPurify
    ? window.DOMPurify.sanitize(raw, { USE_PROFILES: { html: true } })
    : raw;
  return transformCitations(clean);
}

function renderTranscriptText(text) {
  // Transcript posts are plain text from agent recommendation_summary fields,
  // so HTML-escape first then transform citations. No marked.js — these are
  // single paragraphs, not full markdown.
  return transformCitations(escapeHtml(text));
}

function specById(id) {
  return state.specialists.find((s) => s.id === id) || { id, display_name: id, color: "#666" };
}

function setStatusLine(text, kind) {
  const sl = $("#status-line");
  sl.textContent = text;
  sl.className = "status-line" + (kind ? ` ${kind}` : "");
}

function roundLabel(roundIdx) {
  if (roundIdx === 1) return "Independent reviews";
  if (roundIdx === 2) return "Cross-specialty discussion";
  if (roundIdx === 3) return "Reconciling open questions";
  return `Discussion round ${roundIdx}`;
}

// ──────────────────────────────────────────────────────────────────
// Round table SVG (responsive ellipse)
// ──────────────────────────────────────────────────────────────────
function renderRoundTable() {
  const stage = $("#table-stage");
  const W = 760, H = 360;
  const cx = W / 2, cy = H / 2;
  const rx = 290, ry = 130;
  const agents = state.specialists;
  const n = agents.length;

  const consensusPct = "—";
  const stateLabel = state.currentRound === 0 ? "Ready" : `Round ${state.currentRound}`;

  const beams = [];
  const nodes = [];
  agents.forEach((agent, i) => {
    const angle = (-Math.PI / 2) + (i / n) * Math.PI * 2;
    const x = cx + rx * Math.cos(angle);
    const y = cy + ry * Math.sin(angle);
    const status = state.agentStatus.get(agent.id) || "idle";
    const active = ["researching", "drafting", "self-checking", "thinking", "retrieving"].includes(status);
    const skipped = status === "skipped";
    const abstained = status === "abstained";
    const agreed = status === "agreed";
    const disagreed = status === "disagreed";

    if (active) {
      beams.push(`<line x1="${cx}" y1="${cy}" x2="${x}" y2="${y}" stroke="${agent.color}" stroke-width="1.4" stroke-dasharray="3 5" opacity="0.55"><animate attributeName="stroke-dashoffset" from="0" to="-16" dur="0.9s" repeatCount="indefinite"/></line>`);
    }
    nodes.push({ agent, x, y, status, active, skipped, abstained, agreed, disagreed });
  });

  stage.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="rt-svg">
      <defs>
        <radialGradient id="rt-center-grad" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stop-color="rgba(255,255,255,0.95)"/>
          <stop offset="100%" stop-color="rgba(244,247,251,0.85)"/>
        </radialGradient>
      </defs>
      <ellipse cx="${cx}" cy="${cy}" rx="${rx * 0.45}" ry="${ry * 0.45}" fill="none" stroke="var(--border)" stroke-width="1" stroke-dasharray="2 4" opacity="0.6"/>
      <ellipse cx="${cx}" cy="${cy}" rx="${rx * 0.72}" ry="${ry * 0.72}" fill="none" stroke="var(--border)" stroke-width="1" stroke-dasharray="2 4" opacity="0.4"/>
      <ellipse cx="${cx}" cy="${cy}" rx="${rx}" ry="${ry}" fill="none" stroke="var(--border-strong)" stroke-width="1.5"/>
      ${beams.join("")}
    </svg>
    <div class="rt-center">
      <div class="rt-center-label">${escapeHtml(stateLabel)}</div>
      <div class="rt-center-big">${state.currentRound || "—"}<span class="rt-of">/${state.maxRounds}</span></div>
      <div class="rt-center-sub">${escapeHtml(consensusPct === "—" ? "Deliberating" : `Consensus ${consensusPct}`)}</div>
    </div>
  `;

  // Place nodes as absolute-positioned divs above the SVG so we can use rich HTML
  nodes.forEach(({ agent, x, y, status, active, skipped, abstained, agreed, disagreed }) => {
    const visual = AGENT_VISUALS[agent.id] || { initials: agent.id.slice(0, 2).toUpperCase(), short: agent.display_name, mesh: "" };
    const cls = ["rt-node"];
    if (active) cls.push("active");
    if (skipped) cls.push("skipped");
    if (abstained) cls.push("abstained");
    if (agreed) cls.push("agreed");
    if (disagreed) cls.push("disagreed");
    const node = el("div", {
      class: cls.join(" "),
      style: `--agent-color: ${agent.color}; left: ${(x / W) * 100}%; top: ${(y / H) * 100}%;`,
    });
    node.innerHTML = `
      <div class="rt-avatar" style="background:${agent.color}">${escapeHtml(visual.initials)}</div>
      <div class="rt-info">
        <div class="rt-name">${escapeHtml(visual.short)}</div>
        <div class="rt-meta">${escapeHtml(skipped ? "Skipped" : abstained ? "Abstained" : status === "idle" ? visual.mesh : status)}</div>
      </div>
    `;
    stage.appendChild(node);
  });
}

function updateTableConsensus(score, agree) {
  const center = $(".rt-center-sub", $("#table-stage"));
  if (!center) return;
  const pct = Math.round((score || 0) * 100);
  center.textContent = agree ? `Consensus · ${pct}%` : `Deliberating · ${pct}%`;
}

// ──────────────────────────────────────────────────────────────────
// Evidence
// ──────────────────────────────────────────────────────────────────
const SOURCE_KIND_LABEL = {
  pubmed: "PubMed",
  clinical_trial: "ClinicalTrials.gov",
  fda: "FDA",
  rxnorm: "RxNorm",
  dailymed: "DailyMed",
  europe_pmc: "Europe PMC",
  europe_pmc_preprint: "Preprint",
  semantic_scholar: "Semantic Scholar",
  civic: "CIViC",
  web: "Web",
  doi: "DOI",
};

const STRENGTH_HIGH = new Set(["Guideline", "Meta-analysis", "Systematic review"]);
const STRENGTH_MED = new Set([
  "RCT", "Phase III trial", "Phase II trial", "Phase I trial",
  "Controlled trial", "Clinical trial", "Multicenter study",
]);
const STRENGTH_LOW = new Set([
  "Observational", "Comparative study", "Cohort study", "Case-control", "Review", "Case report",
]);

function articleStrengthClass(type) {
  if (!type) return "strength-unknown";
  if (STRENGTH_HIGH.has(type)) return "strength-high";
  if (STRENGTH_MED.has(type)) return "strength-med";
  if (STRENGTH_LOW.has(type)) return "strength-low";
  return "strength-unknown";
}

// Filters: All + dynamic from observed types
function rebuildEvidenceFilters() {
  const filters = $("#evidence-filters");
  filters.innerHTML = "";
  const types = new Set();
  for (const e of state.ledger.values()) if (e.article_type) types.add(e.article_type);
  const ordered = [
    "Guideline", "Meta-analysis", "Systematic review",
    "RCT", "Phase III trial", "Phase II trial", "Phase I trial",
    "Clinical trial", "Cohort study", "Observational", "Review", "Case report",
  ].filter((t) => types.has(t));
  // Append anything we missed
  for (const t of types) if (!ordered.includes(t)) ordered.push(t);

  const make = (key, label) => {
    const b = el("button", {
      type: "button",
      class: "ev-filter" + (state.evidenceFilter === key ? " on" : ""),
      onclick: () => { state.evidenceFilter = key; rebuildEvidenceFilters(); renderEvidenceGrid(); },
    }, label);
    return b;
  };
  filters.appendChild(make("all", `All (${state.ledger.size})`));
  for (const t of ordered) filters.appendChild(make(t, t));
}

function renderEvidenceGrid() {
  const grid = $("#evidence-grid");
  grid.innerHTML = "";
  const all = Array.from(state.ledger.values());
  const filtered = state.evidenceFilter === "all"
    ? all
    : all.filter((e) => e.article_type === state.evidenceFilter);
  if (filtered.length === 0) {
    grid.appendChild(el("div", { class: "ev-empty" }, "No evidence in this category."));
    return;
  }
  for (const e of filtered) {
    const card = el("div", { class: "ev-card" });
    const head = el("div", { class: "ev-head" });
    head.appendChild(el("span", { class: "ev-label" }, `[${e.label}]`));
    head.appendChild(el("span", { class: "ev-source" }, SOURCE_KIND_LABEL[e.source_kind] || e.source_kind || ""));
    if (e.article_type) {
      head.appendChild(el("span", { class: `ev-type ${articleStrengthClass(e.article_type)}` }, e.article_type));
    }
    if (e.year) head.appendChild(el("span", { class: "ev-year" }, e.year));
    card.appendChild(head);

    if (e.title) {
      const title = el("div", { class: "ev-title" });
      if (e.url) {
        title.appendChild(el("a", { href: e.url, target: "_blank", rel: "noopener" }, e.title));
      } else {
        title.textContent = e.title;
      }
      card.appendChild(title);
    }
    if (e.journal) card.appendChild(el("div", { class: "ev-journal" }, e.journal));

    if (e.cited_by?.length) {
      const tags = el("div", { class: "ev-cited" });
      for (const sid of e.cited_by) {
        const spec = specById(sid);
        const tag = el("span", { class: "ev-tag", style: `background:${spec.color}1A;color:${spec.color}` }, spec.display_name);
        tags.appendChild(tag);
      }
      card.appendChild(tags);
    }
    if (e.summary) {
      const det = el("details", { class: "ev-snippet" });
      det.appendChild(el("summary", {}, "Excerpt"));
      det.appendChild(el("p", {}, e.summary));
      card.appendChild(det);
    }
    grid.appendChild(card);
  }
}

function bumpEvidenceCount() {
  $("#evidence-count").textContent = state.ledger.size ? `· ${state.ledger.size} retrieved` : "";
}

// ──────────────────────────────────────────────────────────────────
// Transcript
// ──────────────────────────────────────────────────────────────────
function appendTranscript(spec, round, text, status) {
  state.transcript.push({ kind: "spec", spec, round, text, status });
  const li = buildTranscriptLi({ kind: "spec", spec, round, text, status });
  $("#transcript").appendChild(li);
  $("#transcript-count").textContent = `${state.transcript.length} posts`;
}

function appendJudgeTurn(round, judge) {
  state.transcript.push({ kind: "judge", round, judge });
  const li = buildTranscriptLi({ kind: "judge", round, judge });
  $("#transcript").appendChild(li);
  $("#transcript-count").textContent = `${state.transcript.length} posts`;
}

function buildTranscriptLi(turn) {
  if (turn.kind === "spec") {
    const visual = AGENT_VISUALS[turn.spec.id] || { initials: turn.spec.id.slice(0, 2).toUpperCase() };
    const li = el("li", { class: "post" + (turn.status === "skipped" ? " skipped" : "") });
    const avatar = el("div", { class: "post-av", style: `background:${turn.spec.color}` }, visual.initials);
    const body = el("div", { class: "post-body" });
    const head = el("div", { class: "post-head" });
    head.appendChild(el("span", { class: "post-name" }, turn.spec.display_name));
    head.appendChild(el("span", { class: "post-tag", title: roundLabel(turn.round) }, `Turn ${turn.round}`));
    body.appendChild(head);
    const text = el("div", { class: "post-text" });
    text.innerHTML = renderTranscriptText(turn.text);
    body.appendChild(text);
    li.appendChild(avatar);
    li.appendChild(body);
    return li;
  }
  // judge
  const j = turn.judge;
  const li = el("li", { class: "post chair" + (j.agree ? " agreed" : " disagreed") });
  const avatar = el("div", { class: "post-av chair-av" }, "⚭");
  const body = el("div", { class: "post-body" });
  const head = el("div", { class: "post-head" });
  head.appendChild(el("span", { class: "post-name" }, "Board chair"));
  head.appendChild(el("span", { class: "post-tag", title: roundLabel(turn.round) }, `Turn ${turn.round}`));
  body.appendChild(head);
  const ag = agreementLabel(j.agreement_score);
  const scoreTip = `Alignment score: ${(j.agreement_score ?? 0).toFixed(2)}`;
  const lines = [];
  lines.push(`<strong>${j.agree ? "Consensus reached" : "Disagreement"}</strong> · <span class="ag-label ${ag.cls}" title="${escapeHtml(scoreTip)}">${ag.label}</span>`);
  if (j.disagreements?.length) {
    lines.push("Open: " + j.disagreements.map((d) => escapeHtml(d.topic)).join("; "));
  }
  if (j.open_questions_for_next_round?.length && !j.agree) {
    lines.push("Next: " + j.open_questions_for_next_round.map(escapeHtml).join("; "));
  }
  const txt = el("div", { class: "post-text" });
  txt.innerHTML = lines.join("<br>");
  body.appendChild(txt);
  li.appendChild(avatar);
  li.appendChild(body);
  return li;
}

function rerenderTranscript() {
  const list = $("#transcript");
  list.innerHTML = "";
  for (const turn of state.transcript) list.appendChild(buildTranscriptLi(turn));
}

function setTranscriptOpen(open) {
  const sec = $("#transcript-section");
  const toggle = $("#transcript-toggle");
  const body = $("#transcript-body");
  sec.classList.toggle("open", open);
  body.hidden = !open;
  toggle.setAttribute("aria-expanded", String(open));
}

// ──────────────────────────────────────────────────────────────────
// Final card
// ──────────────────────────────────────────────────────────────────
function agreementLabel(score) {
  const s = score || 0;
  if (s >= 0.85) return { label: "Strong agreement", cls: "strong" };
  if (s >= 0.6)  return { label: "Moderate agreement", cls: "moderate" };
  return { label: "Limited agreement", cls: "limited" };
}

function renderFinal(payload) {
  $("#final-section").hidden = false;
  const card = $("#final-card");
  const meta = $("#final-meta");
  const turnsWord = payload.round_reached === 1 ? "turn" : "turns";
  const verdict = payload.agree ? "Consensus reached" : "No full consensus";
  const verdictClass = payload.agree ? "agreed" : "no-consensus";
  const ag = agreementLabel(payload.agreement_score);
  const scoreTip = `Alignment score: ${(payload.agreement_score || 0).toFixed(2)} (internal metric, 1.0 = perfect alignment)`;
  meta.innerHTML = `<span class="verdict ${verdictClass}">${verdict}</span> · after ${payload.round_reached} ${turnsWord} · <span class="ag-label ${ag.cls}" title="${escapeHtml(scoreTip)}">${ag.label}</span>`;
  card.innerHTML = renderMarkdown(payload.markdown || "");
}

// ──────────────────────────────────────────────────────────────────
// Event handling
// ──────────────────────────────────────────────────────────────────
function handleEvent(ev) {
  switch (ev.type) {
    case "board_started": {
      state.specialists = ev.payload.specialists;
      state.maxRounds = ev.payload.max_rounds;
      state.agentStatus.clear();
      for (const s of state.specialists) state.agentStatus.set(s.id, "idle");
      $("#table-section").hidden = false;
      $("#transcript-section").hidden = false;
      setTranscriptOpen(true);
      setStatusLine("Convening the board…", "running");
      renderRoundTable();
      break;
    }
    case "round_started": {
      state.currentRound = ev.payload.round;
      for (const s of state.specialists) {
        const cur = state.agentStatus.get(s.id);
        if (cur !== "skipped" && cur !== "abstained") state.agentStatus.set(s.id, "researching");
      }
      setStatusLine(roundLabel(state.currentRound) + " in progress…", "running");
      renderRoundTable();
      break;
    }
    case "specialist_event": {
      const { specialist, type } = ev.payload;
      if (type === "started") state.agentStatus.set(specialist, "researching");
      else if (type === "thinking") state.agentStatus.set(specialist, "thinking");
      else if (type === "tool_call") state.agentStatus.set(specialist, "retrieving");
      else if (type === "self_checking") state.agentStatus.set(specialist, "self-checking");
      else if (type === "skipped") state.agentStatus.set(specialist, "skipped");
      else if (type === "no_evidence") state.agentStatus.set(specialist, "abstained");
      else if (type === "done") state.agentStatus.set(specialist, "done");
      else if (type === "error") state.agentStatus.set(specialist, "error");
      renderRoundTable();
      break;
    }
    case "specialist_round_complete": {
      // Status update only — drafts no longer rendered per-panel
      if (ev.payload.status === "skipped") state.agentStatus.set(ev.payload.specialist, "skipped");
      else if (ev.payload.status === "no_evidence") state.agentStatus.set(ev.payload.specialist, "abstained");
      else if (ev.payload.status === "error") state.agentStatus.set(ev.payload.specialist, "error");
      else state.agentStatus.set(ev.payload.specialist, "done");
      renderRoundTable();
      break;
    }
    case "discussion_turn": {
      const spec = specById(ev.payload.specialist);
      appendTranscript(spec, ev.payload.round, ev.payload.text, ev.payload.status);
      break;
    }
    case "consensus_check": {
      appendJudgeTurn(ev.payload.round, ev.payload);
      updateTableConsensus(ev.payload.agreement_score, ev.payload.agree);
      const disagreedIds = new Set();
      for (const d of ev.payload.disagreements || []) {
        for (const k of Object.keys(d.positions || {})) disagreedIds.add(k);
      }
      for (const s of state.specialists) {
        const cur = state.agentStatus.get(s.id);
        if (cur === "skipped" || cur === "abstained") continue;
        state.agentStatus.set(s.id, disagreedIds.has(s.id) ? "disagreed" : "agreed");
      }
      renderRoundTable();
      break;
    }
    case "final": {
      // Populate full ledger.
      state.ledger.clear();
      for (const ref of ev.payload.references || []) state.ledger.set(ref.label, ref);
      rerenderTranscript();
      renderFinal(ev.payload);
      $("#evidence-section").hidden = state.ledger.size === 0;
      bumpEvidenceCount();
      rebuildEvidenceFilters();
      renderEvidenceGrid();
      updateTableConsensus(ev.payload.agreement_score, ev.payload.agree);
      setStatusLine(ev.payload.agree ? "Consensus reached" : "Discussion ended (no consensus)", ev.payload.agree ? "ok" : "warn");
      finishRun();
      break;
    }
    case "error": {
      $("#final-section").hidden = false;
      $("#final-card").innerHTML = `<strong style="color:var(--danger)">Error:</strong> ${escapeHtml(ev.payload.message)}`;
      setStatusLine("Error", "warn");
      finishRun();
      break;
    }
  }
}

// ──────────────────────────────────────────────────────────────────
// Run lifecycle
// ──────────────────────────────────────────────────────────────────
async function startRun() {
  const caseText = $("#case").value.trim();
  if (caseText.length < 20) {
    alert("Please paste a clinical case (at least 20 characters).");
    return;
  }
  // Reset visual state
  state.sid = null;
  state.ledger.clear();
  state.liveEvidence.clear();
  state.transcript = [];
  state.currentRound = 0;
  state.evidenceFilter = "all";
  state.agentStatus.clear();

  $("#start").disabled = true;
  $("#cancel").disabled = false;
  $("#table-section").hidden = true;
  $("#table-stage").innerHTML = "";
  $("#final-section").hidden = true;
  $("#final-card").innerHTML = "";
  $("#final-meta").innerHTML = "";
  $("#evidence-section").hidden = true;
  $("#evidence-grid").innerHTML = "";
  $("#evidence-filters").innerHTML = "";
  $("#transcript-section").hidden = true;
  $("#transcript").innerHTML = "";
  $("#transcript-count").textContent = "0 posts";
  setStatusLine("Starting…", "running");

  try {
    const resp = await fetch("/api/board", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ case: caseText, max_rounds: 4 }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    state.sid = data.session_id;
    state.source = new EventSource(`/api/board/${state.sid}/stream`);
    state.source.onmessage = (e) => {
      try { handleEvent(JSON.parse(e.data)); } catch (err) { console.error(err); }
    };
    state.source.onerror = () => { /* EventSource auto-reconnects */ };
  } catch (e) {
    alert("Failed to start: " + e.message);
    finishRun();
  }
}

async function cancelRun() {
  if (!state.sid) return;
  try { await fetch(`/api/board/${state.sid}`, { method: "DELETE" }); } catch {}
  finishRun();
}

async function newChat() {
  if (state.sid && state.source) {
    try { await fetch(`/api/board/${state.sid}`, { method: "DELETE" }); } catch {}
  }
  if (state.source) { state.source.close(); state.source = null; }
  state.sid = null;
  state.ledger.clear();
  state.transcript = [];
  state.agentStatus.clear();
  state.currentRound = 0;

  $("#case").value = "";
  $("#table-section").hidden = true;
  $("#table-stage").innerHTML = "";
  $("#final-section").hidden = true;
  $("#final-card").innerHTML = "";
  $("#final-meta").innerHTML = "";
  $("#evidence-section").hidden = true;
  $("#evidence-grid").innerHTML = "";
  $("#evidence-filters").innerHTML = "";
  $("#transcript-section").hidden = true;
  $("#transcript").innerHTML = "";
  $("#transcript-count").textContent = "0 posts";
  setStatusLine("", "");
  $("#start").disabled = false;
  $("#cancel").disabled = true;
  $("#case").focus();
}

function finishRun() {
  if (state.source) { state.source.close(); state.source = null; }
  $("#start").disabled = false;
  $("#cancel").disabled = true;
}

// ──────────────────────────────────────────────────────────────────
// Wire up
// ──────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  $("#start").addEventListener("click", startRun);
  $("#cancel").addEventListener("click", cancelRun);
  $("#new-chat").addEventListener("click", newChat);
  $("#transcript-toggle").addEventListener("click", () => {
    const isOpen = !$("#transcript-body").hidden;
    setTranscriptOpen(!isOpen);
  });
});
