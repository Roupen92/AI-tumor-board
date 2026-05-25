"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  sid: null,
  source: null,
  specialists: [],           // [{id, display_name, color}]
  panels: new Map(),         // id -> {el, status, toolEvents, draftEl, refsEl, color}
  ledger: new Map(),         // label -> evidence entry
  transcript: [],            // [{kind, ...}] mirror of transcript DOM, for re-rendering
  currentRound: 0,
  maxRounds: 2,
};

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "dataset") Object.assign(e.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
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
  const entry = state.ledger.get(`E${n}`);
  const title = entry ? `${entry.title} — ${entry.journal || ""} ${entry.year || ""}`.trim() : `E${n}`;
  const href = entry?.url || "#";
  return `<a class="cite" href="${escapeHtml(href)}" target="_blank" rel="noopener" title="${escapeHtml(title)}">[E${n}]</a>`;
}

function transformCitations(html) {
  // Matches single labels, lists, and ranges:
  //   [E1]            -> 1 link
  //   [E1][E2]        -> 2 links (each bracket matches separately)
  //   [E1, E2]        -> 2 links
  //   [E1–E2] / [E1-E2] -> expands range to all labels in [start..end]
  return html.replace(/\[E\d+(?:\s*[-–,;]\s*E?\d+)*\]/g, (match) => {
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
    return labels.map(citationLink).join("");
  });
}

function renderTranscriptText(text) {
  return transformCitations(escapeHtml(text));
}

function renderMarkdown(text) {
  const raw = window.marked ? window.marked.parse(text || "") : escapeHtml(text || "").replace(/\n/g, "<br>");
  return transformCitations(raw);
}

function buildPanel(spec) {
  const tpl = $("#panel-template").content.cloneNode(true);
  const panel = tpl.querySelector(".panel");
  panel.style.setProperty("border-left-color", spec.color);
  tpl.querySelector(".panel-color").style.background = spec.color;
  tpl.querySelector(".panel-name").textContent = spec.display_name;
  $("#panels").appendChild(panel);

  state.panels.set(spec.id, {
    el: panel,
    status: panel.querySelector(".status-pill"),
    draftEl: panel.querySelector(".draft"),
    refsEl: panel.querySelector(".evidence-refs"),
    color: spec.color,
  });
}

function setStatus(id, label) {
  const p = state.panels.get(id);
  if (!p) return;
  p.status.dataset.state = label.toLowerCase().replace(/\s+/g, "-");
  p.status.textContent = label;
}

// Intentionally a no-op: per UX feedback, tool activity is hidden from end users.
// Status pill transitions (researching -> retrieving -> drafting -> done) provide
// the liveness signal instead.
function addToolEvent(_id, _text, _kind = "call") { /* hidden */ }

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

function appendTranscript(spec, round, text, status) {
  state.transcript.push({ kind: "spec", spec, round, text, status });
  const li = buildTranscriptLi({ kind: "spec", spec, round, text, status });
  $("#transcript").appendChild(li);
  li.scrollIntoView({ behavior: "smooth", block: "end" });
}

function buildTranscriptLi(turn) {
  if (turn.kind === "spec") {
    const li = el("li", { class: turn.status === "skipped" ? "skipped" : "" });
    const who = el("span", { class: "who" });
    who.appendChild(el("span", { class: "dot" })).style.background = turn.spec.color;
    who.appendChild(document.createTextNode(turn.spec.display_name));
    who.appendChild(el("span", { class: "round-tag", title: roundLabel(turn.round) }, `Turn ${turn.round}`));
    li.appendChild(who);
    const textEl = el("span", { class: "text" });
    textEl.innerHTML = renderTranscriptText(turn.text);
    li.appendChild(textEl);
    return li;
  }
  if (turn.kind === "judge") {
    const j = turn.judge;
    const li = el("li", { class: j.agree ? "judge-row agreed" : "judge-row" });
    const who = el("span", { class: "who" });
    who.appendChild(document.createTextNode("🧑‍⚖️ Board chair"));
    who.appendChild(el("span", { class: "round-tag", title: roundLabel(turn.round) }, `Turn ${turn.round}`));
    li.appendChild(who);
    const score = (j.agreement_score ?? 0).toFixed(2);
    const lines = [];
    lines.push(`<strong>${j.agree ? "Consensus reached" : "Disagreement"}</strong> (score ${score})`);
    if (j.disagreements?.length) {
      lines.push("Disagreements: " + j.disagreements.map((d) => escapeHtml(d.topic)).join("; "));
    }
    if (j.open_questions_for_next_round?.length) {
      lines.push("Open for next round: " + j.open_questions_for_next_round.map(escapeHtml).join("; "));
    }
    const text = el("span", { class: "text" });
    text.innerHTML = lines.join("<br>");
    li.appendChild(text);
    return li;
  }
  return el("li");
}

function rerenderTranscript() {
  const list = $("#transcript");
  list.innerHTML = "";
  for (const turn of state.transcript) list.appendChild(buildTranscriptLi(turn));
}

function appendJudgeTurn(round, judge) {
  state.transcript.push({ kind: "judge", round, judge });
  const li = buildTranscriptLi({ kind: "judge", round, judge });
  $("#transcript").appendChild(li);
  li.scrollIntoView({ behavior: "smooth", block: "end" });
}

function specById(id) {
  return state.specialists.find((s) => s.id === id) || { id, display_name: id, color: "#666" };
}

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
  "Observational", "Comparative study", "Cohort study", "Case-control", "Review",
]);

function articleStrengthClass(type) {
  if (!type) return "strength-unknown";
  if (STRENGTH_HIGH.has(type)) return "strength-high";
  if (STRENGTH_MED.has(type)) return "strength-med";
  if (STRENGTH_LOW.has(type)) return "strength-low";
  return "strength-unknown";
}

function renderReferences(refs) {
  const list = $("#refs-list");
  const empty = $("#refs-empty");
  const count = $("#refs-count");
  list.innerHTML = "";
  if (!refs.length) {
    list.hidden = true;
    empty.hidden = false;
    count.textContent = "";
    return;
  }
  empty.hidden = true;
  list.hidden = false;
  count.textContent = `(${refs.length})`;
  for (const ref of refs) {
    const li = el("li", { class: "ref-item" });
    const head = el("div", { class: "ref-head" });
    head.appendChild(el("span", { class: "ref-label" }, ref.label));
    const kindLabel = SOURCE_KIND_LABEL[ref.source_kind] || ref.source_kind || "Source";
    head.appendChild(el("span", { class: "ref-kind" }, `${kindLabel} ${ref.source_id || ""}`.trim()));
    if (ref.article_type) {
      const strength = articleStrengthClass(ref.article_type);
      head.appendChild(el("span", { class: `ref-type ${strength}` }, ref.article_type));
    }
    if (ref.year) head.appendChild(el("span", { class: "ref-year" }, ref.year));
    li.appendChild(head);

    if (ref.title) {
      const title = el("div", { class: "ref-title" });
      if (ref.url) {
        const a = el("a", { href: ref.url, target: "_blank", rel: "noopener" }, ref.title);
        title.appendChild(a);
      } else {
        title.textContent = ref.title;
      }
      li.appendChild(title);
    }

    if (ref.journal) li.appendChild(el("div", { class: "ref-journal" }, ref.journal));

    if (ref.cited_by?.length) {
      const tags = el("div", { class: "ref-cited-by" });
      tags.appendChild(el("span", { class: "ref-cited-by-label" }, "Retrieved by: "));
      for (const sid of ref.cited_by) {
        const spec = specById(sid);
        const tag = el("span", { class: "ref-tag" }, spec.display_name);
        tag.style.background = spec.color + "22";
        tag.style.color = spec.color;
        tags.appendChild(tag);
      }
      li.appendChild(tags);
    }

    if (ref.summary) {
      const summary = el("details", { class: "ref-summary" });
      summary.appendChild(el("summary", {}, "Excerpt"));
      summary.appendChild(el("p", {}, ref.summary));
      li.appendChild(summary);
    }

    list.appendChild(li);
  }
}

function handleEvent(ev) {
  switch (ev.type) {
    case "board_started": {
      state.specialists = ev.payload.specialists;
      state.maxRounds = ev.payload.max_rounds;
      $("#panels").innerHTML = "";
      state.panels.clear();
      for (const spec of state.specialists) buildPanel(spec);
      setStatusLine("Convening the board…", "running");
      break;
    }
    case "round_started": {
      state.currentRound = ev.payload.round;
      setStatusLine(roundLabel(state.currentRound) + " in progress…", "running");
      for (const [id] of state.panels) setStatus(id, "researching");
      break;
    }
    case "specialist_event": {
      const { specialist, type, payload } = ev.payload;
      if (type === "started") setStatus(specialist, "researching");
      else if (type === "thinking") setStatus(specialist, "thinking");
      else if (type === "tool_call") {
        setStatus(specialist, "retrieving");
        const args = payload.args || {};
        const argSummary = args.query || (args.pmids ? `pmids=${args.pmids.length}` : args.drug_name || (args.drug_names ? args.drug_names.join(",") : ""));
        addToolEvent(specialist, `${payload.tool}(${argSummary})`, "call");
      } else if (type === "tool_result") {
        addToolEvent(specialist, payload.preview, "←");
      } else if (type === "self_checking") setStatus(specialist, "self-checking");
      else if (type === "skipped") {
        setStatus(specialist, "skipped");
        const p = state.panels.get(specialist);
        if (p) p.draftEl.innerHTML = `<em>${escapeHtml(payload.reason || "Skipped.")}</em>`;
      } else if (type === "retrieve_or_abstain") {
        setStatus(specialist, "retrying");
      } else if (type === "no_evidence") {
        setStatus(specialist, "abstained");
        const p = state.panels.get(specialist);
        if (p) p.draftEl.innerHTML = `<em>Abstained — no evidence retrieved.</em>`;
      } else if (type === "done") setStatus(specialist, "done");
      else if (type === "error") setStatus(specialist, "error");
      break;
    }
    case "specialist_round_complete": {
      const p = state.panels.get(ev.payload.specialist);
      if (!p) break;
      if (ev.payload.status === "skipped") {
        setStatus(ev.payload.specialist, "skipped");
        p.draftEl.innerHTML = `<em>Not applicable to this case.</em>`;
      } else if (ev.payload.status === "no_evidence") {
        setStatus(ev.payload.specialist, "abstained");
        p.draftEl.innerHTML = `<em>Abstained — no evidence retrieved.</em>`;
      } else if (ev.payload.status === "error") {
        setStatus(ev.payload.specialist, "error");
        p.draftEl.innerHTML = `<em>Error: ${escapeHtml(ev.payload.error || "unknown")}</em>`;
      } else {
        setStatus(ev.payload.specialist, "done");
        p.draftEl.innerHTML = renderMarkdown(ev.payload.draft_markdown);
        p.refsEl.innerHTML = "";
        for (const lbl of ev.payload.evidence_labels || []) {
          p.refsEl.appendChild(el("span", { class: "ref" }, lbl));
        }
      }
      break;
    }
    case "discussion_turn": {
      const spec = specById(ev.payload.specialist);
      appendTranscript(spec, ev.payload.round, ev.payload.text, ev.payload.status);
      break;
    }
    case "consensus_check": {
      appendJudgeTurn(ev.payload.round, ev.payload);
      // Mark each specialist as agreed/disagreed based on the verdict.
      const disagreedIds = new Set();
      for (const d of ev.payload.disagreements || []) {
        for (const k of Object.keys(d.positions || {})) disagreedIds.add(k);
      }
      for (const [id, p] of state.panels) {
        const s = p.status.dataset.state;
        if (s === "skipped" || s === "abstained") continue;
        setStatus(id, disagreedIds.has(id) ? "disagreed" : "agreed");
      }
      break;
    }
    case "final": {
      // Repopulate the ledger so citation tooltips work in the final card.
      state.ledger.clear();
      for (const ref of ev.payload.references || []) {
        state.ledger.set(ref.label, ref);
      }
      rerenderTranscript();
      const wrap = $("#final");
      wrap.classList.remove("empty");
      const verdictClass = ev.payload.agree ? "verdict agreed" : "verdict no-consensus";
      const turnsWord = ev.payload.round_reached === 1 ? "turn" : "turns";
      const verdictText = ev.payload.agree
        ? `Consensus reached after ${ev.payload.round_reached} ${turnsWord} (alignment ${(ev.payload.agreement_score || 0).toFixed(2)})`
        : `No full consensus after ${ev.payload.round_reached} ${turnsWord} (alignment ${(ev.payload.agreement_score || 0).toFixed(2)})`;
      wrap.innerHTML = `<span class="${verdictClass}">${escapeHtml(verdictText)}</span>` + renderMarkdown(ev.payload.markdown);
      renderReferences(ev.payload.references || []);
      setStatusLine(ev.payload.agree ? "Consensus reached" : "Discussion ended (no consensus)", ev.payload.agree ? "ok" : "warn");
      finishRun();
      break;
    }
    case "error": {
      const wrap = $("#final");
      wrap.classList.remove("empty");
      wrap.innerHTML = `<strong style="color:#dc2626">Error:</strong> ${escapeHtml(ev.payload.message)}`;
      finishRun();
      break;
    }
  }
}

async function startRun() {
  const caseText = $("#case").value.trim();
  if (caseText.length < 20) {
    alert("Please paste a clinical case (at least 20 characters).");
    return;
  }
  const maxRounds = 4;     // internal safety cap; loop exits early on consensus
  $("#start").disabled = true;
  $("#cancel").disabled = false;
  $("#panels").innerHTML = "";
  $("#transcript").innerHTML = "";
  $("#final").className = "final-card empty";
  $("#final").textContent = "Running…";
  $("#refs-list").innerHTML = "";
  $("#refs-list").hidden = true;
  $("#refs-empty").hidden = false;
  $("#refs-empty").textContent = "Evidence will populate as agents fetch articles…";
  $("#refs-count").textContent = "";
  setStatusLine("Starting…", "running");
  state.ledger.clear();
  state.transcript = [];

  try {
    const resp = await fetch("/api/board", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ case: caseText, max_rounds: maxRounds }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    state.sid = data.session_id;
    state.source = new EventSource(`/api/board/${state.sid}/stream`);
    state.source.onmessage = (e) => {
      try { handleEvent(JSON.parse(e.data)); } catch (err) { console.error(err); }
    };
    state.source.onerror = () => {
      // EventSource will auto-reconnect; only finalize once the server closes.
    };
  } catch (e) {
    alert("Failed to start: " + e.message);
    finishRun();
  }
}

async function cancelRun() {
  if (!state.sid) return;
  try {
    await fetch(`/api/board/${state.sid}`, { method: "DELETE" });
  } catch {}
  finishRun();
}

async function newChat() {
  // Cancel any in-flight session before clearing.
  if (state.sid && state.source) {
    try { await fetch(`/api/board/${state.sid}`, { method: "DELETE" }); } catch {}
  }
  if (state.source) {
    state.source.close();
    state.source = null;
  }
  state.sid = null;
  state.panels.clear();
  state.ledger.clear();
  state.transcript = [];
  state.currentRound = 0;

  $("#case").value = "";
  $("#panels").innerHTML = "";
  $("#transcript").innerHTML = "";
  $("#final").className = "final-card empty";
  $("#final").textContent = "No recommendation yet.";
  $("#refs-list").innerHTML = "";
  $("#refs-list").hidden = true;
  $("#refs-empty").hidden = false;
  $("#refs-empty").textContent = "No evidence yet.";
  $("#refs-count").textContent = "";
  setStatusLine("", "");
  $("#start").disabled = false;
  $("#cancel").disabled = true;
  $("#case").focus();
}

function finishRun() {
  if (state.source) {
    state.source.close();
    state.source = null;
  }
  $("#start").disabled = false;
  $("#cancel").disabled = true;
}

document.addEventListener("DOMContentLoaded", () => {
  $("#start").addEventListener("click", startRun);
  $("#cancel").addEventListener("click", cancelRun);
  $("#new-chat").addEventListener("click", newChat);
});
