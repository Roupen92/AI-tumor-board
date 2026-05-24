"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  sid: null,
  source: null,
  specialists: [],           // [{id, display_name, color}]
  panels: new Map(),         // id -> {el, status, toolEvents, draftEl, refsEl, color}
  ledger: new Map(),         // label -> evidence entry
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

function transformCitations(html) {
  return html.replace(/\[E(\d+)\]/g, (_, n) => {
    const entry = state.ledger.get(`E${n}`);
    const title = entry ? `${entry.title} — ${entry.journal || ""} ${entry.year || ""}`.trim() : `E${n}`;
    const href = entry?.url || "#";
    return `<a class="cite" href="${escapeHtml(href)}" target="_blank" rel="noopener" title="${escapeHtml(title)}">[E${n}]</a>`;
  });
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
    toolCount: panel.querySelector(".tool-count"),
    toolEvents: panel.querySelector(".tool-events"),
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

function addToolEvent(id, text, kind = "call") {
  const p = state.panels.get(id);
  if (!p) return;
  const li = el("li", {});
  li.innerHTML = `<span class="tool-name">${escapeHtml(kind)}</span> ${escapeHtml(text)}`;
  p.toolEvents.appendChild(li);
  p.toolCount.textContent = p.toolEvents.children.length;
  p.toolEvents.scrollTop = p.toolEvents.scrollHeight;
}

function renderRoundStepper(current, max) {
  const stepper = $("#round-stepper");
  stepper.innerHTML = "";
  for (let i = 1; i <= max; i++) {
    const cls = i < current ? "step done" : i === current ? "step active" : "step";
    stepper.appendChild(el("span", { class: cls }, `R${i}`));
  }
}

function appendTranscript(spec, round, text, status) {
  const li = el("li", { class: status === "skipped" ? "skipped" : "" });
  const who = el("span", { class: "who" });
  who.appendChild(el("span", { class: "dot" })).style.background = spec.color;
  who.appendChild(document.createTextNode(spec.display_name));
  who.appendChild(el("span", { class: "round-tag" }, `R${round}`));
  li.appendChild(who);
  li.appendChild(el("span", { class: "text" }, text));
  $("#transcript").appendChild(li);
  li.scrollIntoView({ behavior: "smooth", block: "end" });
}

function appendJudgeTurn(round, judge) {
  const li = el("li", { class: judge.agree ? "judge-row agreed" : "judge-row" });
  const who = el("span", { class: "who" });
  who.appendChild(document.createTextNode("🧑‍⚖️ Consensus judge"));
  who.appendChild(el("span", { class: "round-tag" }, `R${round}`));
  li.appendChild(who);

  const score = (judge.agreement_score ?? 0).toFixed(2);
  const lines = [];
  lines.push(`<strong>${judge.agree ? "Consensus reached" : "Disagreement"}</strong> (score ${score})`);
  if (judge.disagreements?.length) {
    lines.push("Disagreements: " + judge.disagreements.map(d => d.topic).join("; "));
  }
  if (judge.open_questions_for_next_round?.length) {
    lines.push("Open for next round: " + judge.open_questions_for_next_round.join("; "));
  }
  const text = el("span", { class: "text" });
  text.innerHTML = lines.join("<br>");
  li.appendChild(text);
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
};

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
      renderRoundStepper(1, state.maxRounds);
      break;
    }
    case "round_started": {
      state.currentRound = ev.payload.round;
      renderRoundStepper(state.currentRound, state.maxRounds);
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
      renderRoundStepper(state.currentRound, state.maxRounds);
      // Repopulate the ledger so citation tooltips work in the final card.
      state.ledger.clear();
      for (const ref of ev.payload.references || []) {
        state.ledger.set(ref.label, ref);
      }
      const wrap = $("#final");
      wrap.classList.remove("empty");
      const verdictClass = ev.payload.agree ? "verdict agreed" : "verdict no-consensus";
      const verdictText = ev.payload.agree
        ? `Consensus reached at round ${ev.payload.round_reached} (score ${(ev.payload.agreement_score || 0).toFixed(2)})`
        : `No full consensus (score ${(ev.payload.agreement_score || 0).toFixed(2)})`;
      wrap.innerHTML = `<span class="${verdictClass}">${escapeHtml(verdictText)}</span>` + renderMarkdown(ev.payload.markdown);
      renderReferences(ev.payload.references || []);
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
  const maxRounds = Number($("#max-rounds").value);
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
  state.ledger.clear();

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
  $("#round-stepper").innerHTML = "";
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
