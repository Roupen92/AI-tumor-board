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
  agentDetail: new Map(),    // id -> { status, summary, draft, labels } — drives the hover popover
  phase: null,               // null | "judging" | "synthesizing" — post-specialist work shown in the center
  ledger: new Map(),         // label -> evidence entry
  liveEvidence: new Map(),   // label -> { label, source_kind, source_id, title, journal, year, url, article_type, cited_by } (added as panel completes)
  transcript: [],            // [{kind, ...}] mirror of transcript DOM
  currentRound: 0,
  maxRounds: 4,
  evidenceFilter: "all",
  lastStatus: { text: "", kind: "" },
  sseWarned: false,           // true while a "Reconnecting…" warning is displayed
};

// Display defaults (used if server doesn't provide). Color order matches existing config.
const AGENT_VISUALS = {
  rad_onc:   { initials: "RO", short: "Rad Onc",   mesh: "Radiotherapy" },
  med_onc:   { initials: "MO", short: "Med Onc",   mesh: "Systemic · targeted" },
  surg_onc:  { initials: "SO", short: "Surg Onc",  mesh: "Surgical · margins" },
  pharm:     { initials: "Rx", short: "Pharm",     mesh: "DailyMed · RxNorm" },
  molecular: { initials: "MX", short: "Mol Onc",   mesh: "Biomarkers · CIViC" },
  pathologist:{initials: "Pa", short: "Path",      mesh: "IHC · diagnosis" },
  trial_matcher:{initials: "CT", short: "Trial Match", mesh: "ClinicalTrials.gov" },
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
  // Remember the canonical status so transient warnings (e.g. SSE reconnect)
  // can be cleared back to what the run was actually doing.
  state.lastStatus = { text, kind: kind || "" };
}

function setCaseError(msg) {
  const err = $(".case-error");
  if (!err) return;
  if (msg) {
    err.textContent = msg;
    err.hidden = false;
    $("#case").setAttribute("aria-invalid", "true");
  } else {
    err.textContent = "";
    err.hidden = true;
    $("#case").removeAttribute("aria-invalid");
  }
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
  const PHASE_LABELS = { judging: "Reaching consensus…", synthesizing: "Writing recommendation…" };
  const phaseLabel = PHASE_LABELS[state.phase] || "";
  const stateLabel = phaseLabel ? "Working" : (state.currentRound === 0 ? "Ready" : `Round ${state.currentRound}`);

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
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" class="rt-svg"
         role="img" aria-label="Tumor board round table showing 6 specialist agents arranged in a circle">
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
    <div class="rt-center${phaseLabel ? " working" : ""}">
      <div class="rt-center-label">${escapeHtml(stateLabel)}</div>
      <div class="rt-center-big">${state.currentRound || "—"}<span class="rt-of">/${state.maxRounds}</span></div>
      <div class="rt-center-sub">${escapeHtml(phaseLabel || (consensusPct === "—" ? "Deliberating" : `Consensus ${consensusPct}`))}</div>
    </div>`;

  // Place nodes as absolute-positioned divs above the SVG so we can use rich HTML
  nodes.forEach(({ agent, x, y, status, active, skipped, abstained, agreed, disagreed }) => {
    const visual = AGENT_VISUALS[agent.id] || { initials: agent.id.slice(0, 2).toUpperCase(), short: agent.display_name, mesh: "" };
    const cls = ["rt-node"];
    if (active) cls.push("active");
    if (skipped) cls.push("skipped");
    if (abstained) cls.push("abstained");
    if (agreed) cls.push("agreed");
    if (disagreed) cls.push("disagreed");
    // Open the popover downward for top-half nodes, upward for bottom-half nodes.
    if (y < cy) cls.push("rt-top"); else cls.push("rt-bottom");
    const node = el("div", {
      class: cls.join(" "),
      tabindex: "0",
      style: `--agent-color: ${agent.color}; left: ${(x / W) * 100}%; top: ${(y / H) * 100}%;`,
    });
    node.innerHTML = `
      <div class="rt-avatar" style="background:${agent.color}">${escapeHtml(visual.initials)}</div>
      <div class="rt-info">
        <div class="rt-name">${escapeHtml(visual.short)}</div>
        <div class="rt-meta">${escapeHtml(skipped ? "Skipped" : abstained ? "Abstained" : status === "idle" ? visual.mesh : status)}</div>
      </div>
      ${agentPopHtml(agent, status)}
    `;
    stage.appendChild(node);
  });
}

// Build the hover/focus popover that summarizes what one agent did.
function agentPopHtml(agent, status) {
  const visual = AGENT_VISUALS[agent.id] || { initials: agent.id.slice(0, 2).toUpperCase(), mesh: "" };
  const detail = state.agentDetail.get(agent.id);
  const statusText = {
    idle: "Waiting", researching: "Researching", thinking: "Thinking",
    retrieving: "Retrieving evidence", "self-checking": "Reviewing its draft",
    drafting: "Drafting", done: "Done", skipped: "Skipped", abstained: "Abstained",
    agreed: "Agreed", disagreed: "Dissented", error: "Error",
  }[status] || status;

  let bodyHtml;
  if (!detail) {
    bodyHtml = `<p class="rt-pop-muted">${escapeHtml(visual.mesh || "Working…")}</p>`;
  } else if (detail.status === "skipped") {
    bodyHtml = `<p class="rt-pop-muted">Sat this case out — not applicable. ${escapeHtml(firstLine(detail.draft))}</p>`;
  } else if (detail.status === "no_evidence") {
    bodyHtml = `<p class="rt-pop-muted">Abstained — no citable evidence to ground a recommendation.</p>`;
  } else if (detail.status === "error") {
    bodyHtml = `<p class="rt-pop-muted">Couldn't complete: ${escapeHtml(detail.error || "error")}.</p>`;
  } else {
    const sources = agentSources(detail.labels);
    const srcHtml = sources.length
      ? `<div class="rt-pop-sec"><div class="rt-pop-h">Sources it used (${sources.length})</div><ul class="rt-pop-src">${
          sources.map((s) => `<li><a href="${escapeHtml(s.url || "#")}" target="_blank" rel="noopener">[${escapeHtml(s.label)}] ${escapeHtml(s.title || s.kind)}</a></li>`).join("")
        }</ul></div>`
      : "";
    const concl = detail.summary
      ? `<div class="rt-pop-sec"><div class="rt-pop-h">What it concluded</div><div class="rt-pop-draft">${renderMarkdown(detail.summary)}</div></div>`
      : "";
    const reasoning = detail.draft
      ? `<div class="rt-pop-sec"><div class="rt-pop-h">Full reasoning</div><div class="rt-pop-draft">${renderMarkdown(detail.draft)}</div></div>`
      : "";
    bodyHtml = concl + srcHtml + reasoning;
  }

  return `
    <div class="rt-pop" role="tooltip">
      <div class="rt-pop-head" style="--agent-color:${agent.color}">
        <span class="rt-pop-name">${escapeHtml(agent.display_name)}</span>
        <span class="rt-pop-status">${escapeHtml(statusText)}</span>
      </div>
      <div class="rt-pop-body">${bodyHtml}</div>
    </div>`;
}

function firstLine(s) {
  return String(s || "").split("\n").map((l) => l.trim()).filter(Boolean)[0] || "";
}

function agentSources(labels) {
  const out = [];
  for (const l of labels || []) {
    const e = state.ledger.get(String(l)) || state.liveEvidence.get(String(l));
    if (e) out.push({ label: l, title: e.title, kind: SOURCE_KIND_LABEL[e.source_kind] || e.source_kind, url: e.url });
  }
  return out;
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
// (The round-by-round discussion transcript was removed; each agent's full
// contribution is now shown via the hover popover on its round-table node.)

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
// Clinical trials (dedicated bottom section)
// ──────────────────────────────────────────────────────────────────
function renderTrials() {
  const sec = $("#trials-section");
  if (!sec) return;
  const trials = [...state.ledger.values()].filter((r) => r.source_kind === "clinical_trial");
  const tm = state.agentDetail.get("trial_matcher");
  const tmActive = tm && tm.status === "done";

  if (!trials.length && !tmActive) { sec.hidden = true; return; }
  sec.hidden = false;
  $("#trials-count").textContent = trials.length ? `· ${trials.length} matched` : "";

  const analysis = $("#trials-analysis");
  if (tmActive && tm.draft) {
    analysis.innerHTML = renderMarkdown(tm.draft);
    analysis.hidden = false;
  } else {
    analysis.innerHTML = "";
    analysis.hidden = true;
  }

  const grid = $("#trials-grid");
  grid.innerHTML = "";
  if (!trials.length) {
    grid.innerHTML = `<p class="muted">The trial matcher did not cite any specific trials for this case.</p>`;
    return;
  }
  for (const t of trials) {
    const card = el("div", { class: "trial-card" });
    card.innerHTML = `
      <div class="trial-nct">${escapeHtml(t.source_id || "")}</div>
      <div class="trial-title">${escapeHtml(t.title || "")}</div>
      <div class="trial-sum">${escapeHtml((t.summary || "").slice(0, 240))}</div>
      <a class="trial-link" href="${escapeHtml(t.url || "#")}" target="_blank" rel="noopener">View on ClinicalTrials.gov →</a>
    `;
    grid.appendChild(card);
  }
}

// ──────────────────────────────────────────────────────────────────
// Event handling
// ──────────────────────────────────────────────────────────────────
function handleEvent(ev) {
  switch (ev.type) {
    case "board_started": {
      state.specialists = ev.payload.specialists;
      state.maxRounds = ev.payload.max_rounds;
      state.phase = null;
      state.agentStatus.clear();
      for (const s of state.specialists) state.agentStatus.set(s.id, "idle");
      $("#table-meta").textContent = `${state.specialists.length} agents · hover to see each`;
      $("#table-section").hidden = false;
      setStatusLine("Convening the board…", "running");
      renderRoundTable();
      break;
    }
    case "round_started": {
      state.currentRound = ev.payload.round;
      state.phase = null;
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
      else if (type === "trial_stage") state.agentStatus.set(specialist, ev.payload.payload?.stage === "ranking" ? "drafting" : "retrieving");
      else if (type === "skipped") state.agentStatus.set(specialist, "skipped");
      else if (type === "no_evidence") state.agentStatus.set(specialist, "abstained");
      else if (type === "done") state.agentStatus.set(specialist, "done");
      else if (type === "error") state.agentStatus.set(specialist, "error");
      renderRoundTable();
      break;
    }
    case "specialist_round_complete": {
      if (ev.payload.status === "skipped") state.agentStatus.set(ev.payload.specialist, "skipped");
      else if (ev.payload.status === "no_evidence") state.agentStatus.set(ev.payload.specialist, "abstained");
      else if (ev.payload.status === "error") state.agentStatus.set(ev.payload.specialist, "error");
      else state.agentStatus.set(ev.payload.specialist, "done");
      // Capture the latest round's detail so the hover popover can show what the agent did.
      state.agentDetail.set(ev.payload.specialist, {
        status: ev.payload.status,
        summary: ev.payload.recommendation_summary || "",
        draft: ev.payload.draft_markdown || "",
        labels: ev.payload.evidence_labels || [],
        error: ev.payload.error || "",
      });
      // Populate live evidence so "Sources it used" and inline [N] cites resolve now,
      // before the final references payload arrives.
      for (const e of ev.payload.evidence || []) state.liveEvidence.set(String(e.label), e);
      renderRoundTable();
      break;
    }
    case "discussion_turn": {
      // Round-by-round transcript removed; per-agent detail is shown via hover popovers.
      break;
    }
    case "phase": {
      // Post-specialist work that was previously invisible (judge / synthesizer).
      state.phase = ev.payload.phase;
      setStatusLine(
        ev.payload.phase === "judging" ? "Weighing the specialists' positions…"
          : ev.payload.phase === "synthesizing" ? "Writing the final recommendation…"
          : "Working…",
        "running",
      );
      renderRoundTable();
      break;
    }
    case "consensus_check": {
      updateTableConsensus(ev.payload.agreement_score, ev.payload.agree);
      // If the judge couldn't actually evaluate consensus (error/quota, or fewer than
      // 2 active specialists), don't repaint nodes as green "agreed" — that would
      // contradict the "no consensus" verdict. Leave their existing status.
      if (ev.payload.error || ev.payload.note) { renderRoundTable(); break; }
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
      state.phase = null;
      // Populate full ledger.
      state.ledger.clear();
      for (const ref of ev.payload.references || []) state.ledger.set(ref.label, ref);
      renderFinal(ev.payload);
      $("#evidence-section").hidden = state.ledger.size === 0;
      bumpEvidenceCount();
      rebuildEvidenceFilters();
      renderEvidenceGrid();
      renderRoundTable();        // refresh popovers now that the full ledger is known
      renderTrials();            // dedicated clinical-trials section at the bottom
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
    setCaseError("Please paste a clinical case (at least 20 characters).");
    $("#case").focus();
    return;
  }
  setCaseError("");
  // Reset visual state
  state.sid = null;
  state.ledger.clear();
  state.liveEvidence.clear();
  state.agentDetail.clear();
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
  $("#trials-section").hidden = true;
  $("#trials-grid").innerHTML = "";
  $("#trials-analysis").innerHTML = "";
  setStatusLine("Starting…", "running");

  try {
    const resp = await fetch("/api/board", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        case: caseText,
        max_rounds: 2,
        patient_location: ($("#patient-location")?.value || "").trim() || null,
        enable_trial_matching: $("#enable-trials")?.checked ?? true,
      }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    state.sid = data.session_id;
    state.source = new EventSource(`/api/board/${state.sid}/stream`);
    state.sseWarned = false;
    state.source.onmessage = (e) => {
      // First valid message after a connection drop clears the warning and
      // restores whatever status the run was actually on.
      if (state.sseWarned) {
        state.sseWarned = false;
        const last = state.lastStatus || { text: "", kind: "" };
        setStatusLine(last.text, last.kind);
      }
      try { handleEvent(JSON.parse(e.data)); } catch (err) { console.error(err); }
    };
    state.source.onerror = () => {
      // EventSource auto-reconnects; surface that to the user so they aren't
      // left wondering why the board went quiet.
      if (!state.source) return;
      if (state.source.readyState === EventSource.CLOSED) return;
      if (state.sseWarned) return;
      state.sseWarned = true;
      const sl = $("#status-line");
      sl.textContent = "Reconnecting…";
      sl.className = "status-line warn";
    };
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
  state.liveEvidence.clear();
  state.agentDetail.clear();
  state.agentStatus.clear();
  state.currentRound = 0;

  $("#case").value = "";
  setCaseError("");
  $("#table-section").hidden = true;
  $("#table-stage").innerHTML = "";
  $("#final-section").hidden = true;
  $("#final-card").innerHTML = "";
  $("#final-meta").innerHTML = "";
  $("#evidence-section").hidden = true;
  $("#evidence-grid").innerHTML = "";
  $("#evidence-filters").innerHTML = "";
  $("#trials-section").hidden = true;
  $("#trials-grid").innerHTML = "";
  $("#trials-analysis").innerHTML = "";
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
  // Clear the inline validation message as soon as the user starts editing.
  const caseEl = $("#case");
  if (caseEl) {
    caseEl.addEventListener("input", () => {
      if (caseEl.value.trim().length >= 20) setCaseError("");
    });
  }
});
