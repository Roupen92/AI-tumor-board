"""Smoke tests for the board orchestrator with a mocked OpenAI client.

Run: pytest -v

The existing board-level tests run with enable_trial_matching=False so they exercise
the six core specialists with the same per-call accounting they always had. The
Clinical Trial Matcher (a 3-stage pipeline with a custom call shape) is covered by
dedicated direct tests below.
"""
import asyncio
import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest


# ---------- Mock infrastructure ----------

def _mock_response(content: str, tool_calls=None):
    """Return a shape that mimics OpenAI's chat.completions.create response."""
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls or []
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


class _MockException:
    """Sentinel wrapper: when popped from a _ChatScript queue, the contained
    exception is raised instead of being returned as a response."""
    def __init__(self, exc: BaseException):
        self.exc = exc


class _ChatScript:
    """Replays a queue of canned responses keyed by call order.

    Script entries can be either mock response objects (returned as-is) or
    _MockException / BaseException instances (raised when popped).
    """
    def __init__(self):
        self.calls = []
        self.script: list = []

    def __call__(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if not self.script:
            return _mock_response("default draft.\nRECOMMENDATION SUMMARY: default summary.")
        item = self.script.pop(0)
        if isinstance(item, _MockException):
            raise item.exc
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def chat_script(monkeypatch):
    script = _ChatScript()
    from app import llm
    monkeypatch.setattr(llm, "chat", script)
    return script


def _core_ids():
    """Specialist ids excluding the trial matcher (which has a custom multi-stage runner)."""
    from app.config import SPECIALIST_IDS
    return [s for s in SPECIALIST_IDS if s != "trial_matcher"]


# ---------- Tests ----------

def test_board_terminates_on_consensus(chat_script, monkeypatch):
    """Judge returns agree=true; loop must exit after round 1."""
    from app import board

    # With the retrieve-or-abstain rule + citation-required rule, the mocked drafts
    # must (a) have evidence in the ledger and (b) include an [E#] citation.
    monkeypatch.setattr(
        "app.evidence.EvidenceLedger.count_for", lambda self, sid: 1
    )
    # Citations like [1] in the mocked draft are validated by looking up the label
    # in the ledger; return a truthy stub so they count as real citations.
    monkeypatch.setattr(
        "app.evidence.EvidenceLedger.get_by_label", lambda self, lbl: object()
    )
    specialist_draft = "Comprehensive recommendation [1].\n\nRECOMMENDATION SUMMARY: Go with plan A [1]."
    self_check_draft = "Reviewed [1].\n\nRECOMMENDATION SUMMARY: Go with plan A [1]."

    # Per specialist: 1 tool-loop call (returns draft, no tool_calls), 1 self-check call.
    for _ in _core_ids():
        chat_script.script.append(_mock_response(specialist_draft))
        chat_script.script.append(_mock_response(self_check_draft))

    # Judge: agree=true, score above threshold.
    chat_script.script.append(_mock_response(json.dumps({
        "agree": True,
        "agreement_score": 0.95,
        "shared_recommendations": ["Plan A"],
        "disagreements": [],
        "open_questions_for_next_round": [],
    })))

    # Final synthesizer.
    chat_script.script.append(_mock_response("# Final\nPlan A for everyone."))

    events = []
    result = asyncio.run(board.run_board(
        "Test case with enough length to look real.", lambda t, p: events.append((t, p)),
        max_rounds=3, enable_trial_matching=False,
    ))

    # Should have stopped after round 1.
    round_starts = [e for e in events if e[0] == "round_started"]
    assert len(round_starts) == 1, f"Expected 1 round, got {len(round_starts)}"
    assert result["agree"] is True
    assert result["round_reached"] == 1
    assert "Plan A" in result["markdown"]


def test_board_respects_max_rounds_on_disagreement(chat_script, monkeypatch):
    """Judge always returns agree=false; loop must run exactly max_rounds rounds."""
    from app import board

    monkeypatch.setattr(
        "app.evidence.EvidenceLedger.count_for", lambda self, sid: 1
    )
    monkeypatch.setattr(
        "app.evidence.EvidenceLedger.get_by_label", lambda self, lbl: object()
    )

    def queue_round():
        for _ in _core_ids():
            chat_script.script.append(_mock_response("Draft [1].\n\nRECOMMENDATION SUMMARY: Plan X [1]."))
            chat_script.script.append(_mock_response("Revised [1].\n\nRECOMMENDATION SUMMARY: Plan X [1]."))
        chat_script.script.append(_mock_response(json.dumps({
            "agree": False,
            "agreement_score": 0.4,
            "shared_recommendations": [],
            "disagreements": [{"topic": "sequencing", "positions": {"med_onc": "A", "surg_onc": "B"}}],
            "open_questions_for_next_round": ["Resolve sequencing"],
        })))

    queue_round()
    queue_round()
    chat_script.script.append(_mock_response("# Final\nNo consensus."))

    events = []
    result = asyncio.run(board.run_board(
        "Test case with enough length.", lambda t, p: events.append((t, p)),
        max_rounds=2, enable_trial_matching=False,
    ))

    round_starts = [e for e in events if e[0] == "round_started"]
    assert len(round_starts) == 2
    assert result["agree"] is False
    assert result["round_reached"] == 2


def test_molecular_skips_when_no_data(chat_script, monkeypatch):
    """A specialist returning 'SKIP:' is recorded as skipped and excluded from judge."""
    from app import board

    # This test feeds DIFFERENT scripted responses per specialist (SKIP for some),
    # so it needs deterministic, sequential execution — pin the semaphore to 1 so the
    # scripted queue maps to specialists in order regardless of PARALLEL_SPECIALISTS.
    monkeypatch.setattr("app.board.PARALLEL_SPECIALISTS", 1)
    monkeypatch.setattr(
        "app.evidence.EvidenceLedger.count_for", lambda self, sid: 1
    )
    monkeypatch.setattr(
        "app.evidence.EvidenceLedger.get_by_label", lambda self, lbl: object()
    )

    # Specialists in config order (trial matcher disabled for this test).
    for sid in _core_ids():
        if sid in ("molecular", "pathologist"):
            # Single response, no tool_calls, no self-check (skip short-circuits).
            chat_script.script.append(_mock_response("SKIP: not applicable."))
        else:
            chat_script.script.append(_mock_response("Draft [1].\n\nRECOMMENDATION SUMMARY: Plan [1]."))
            chat_script.script.append(_mock_response("Revised [1].\n\nRECOMMENDATION SUMMARY: Plan [1]."))

    chat_script.script.append(_mock_response(json.dumps({
        "agree": True,
        "agreement_score": 0.9,
        "shared_recommendations": [],
        "disagreements": [],
        "open_questions_for_next_round": [],
    })))
    chat_script.script.append(_mock_response("# Final"))

    events = []
    asyncio.run(board.run_board(
        "Test case with no biomarker information at all.",
        lambda t, p: events.append((t, p)),
        max_rounds=2, enable_trial_matching=False,
    ))

    completes = [e for e in events if e[0] == "specialist_round_complete"]
    skipped = [c for c in completes if c[1]["specialist"] in ("molecular", "pathologist")]
    assert len(skipped) == 2
    for c in skipped:
        assert c[1]["status"] == "skipped"


def test_agent_abstains_when_no_evidence_retrieved(chat_script, monkeypatch):
    """If a specialist never retrieves evidence, it must abstain (not produce a draft)."""
    from app import board

    # Force count_for to always return 0 — no evidence ever registered.
    monkeypatch.setattr(
        "app.evidence.EvidenceLedger.count_for", lambda self, sid: 0
    )

    # Each specialist: round 1 produces a draft (no tool_calls, no citations). The
    # retrieve-or-abstain retry then re-prompts; the agent responds with ABSTAIN.
    for _ in _core_ids():
        chat_script.script.append(_mock_response("Draft.\n\nRECOMMENDATION SUMMARY: Plan."))
        chat_script.script.append(_mock_response("ABSTAIN: insufficient evidence."))

    # Judge: with all abstained, the early-out returns trivial agree=true.
    # No judge call expected, but synthesizer still runs.
    chat_script.script.append(_mock_response("# Final\n(all abstained)"))

    events = []
    result = asyncio.run(board.run_board(
        "Test case where retrieval will fail.",
        lambda t, p: events.append((t, p)),
        max_rounds=1, enable_trial_matching=False,
    ))

    completes = [e for e in events if e[0] == "specialist_round_complete"]
    assert len(completes) == len(_core_ids())
    for c in completes:
        assert c[1]["status"] == "no_evidence", f"{c[1]['specialist']} should have abstained"
    # Confirm no_evidence emit event surfaced for each specialist.
    no_ev_events = [e for e in events if e[0] == "specialist_event"
                    and e[1].get("type") == "no_evidence"]
    assert len(no_ev_events) == len(_core_ids())


def test_agent_abstains_when_draft_has_no_citations(chat_script, monkeypatch):
    """Even with evidence in the ledger, a draft with zero [E#] labels must abstain."""
    from app import board

    # Pretend the ledger has evidence so the first-pass retrieval check passes.
    monkeypatch.setattr(
        "app.evidence.EvidenceLedger.count_for", lambda self, sid: 1
    )
    monkeypatch.setattr(
        "app.evidence.EvidenceLedger.get_by_label", lambda self, lbl: object()
    )

    # Each specialist: draft has no [E#] citations, self-check returns the same.
    # The post-self-check rule should force abstention.
    for _ in _core_ids():
        chat_script.script.append(_mock_response("Draft from training.\n\nRECOMMENDATION SUMMARY: Plan."))
        chat_script.script.append(_mock_response("Still no citations.\n\nRECOMMENDATION SUMMARY: Plan."))

    # Synthesizer still runs.
    chat_script.script.append(_mock_response("# Final\n(all abstained)"))

    events = []
    asyncio.run(board.run_board(
        "Case where the model ignores tool results.",
        lambda t, p: events.append((t, p)),
        max_rounds=1, enable_trial_matching=False,
    ))

    completes = [e for e in events if e[0] == "specialist_round_complete"]
    for c in completes:
        assert c[1]["status"] == "no_evidence", (
            f"{c[1]['specialist']} should have been forced to abstain (no citations in draft)"
        )


def test_uncaught_specialist_exception_does_not_crash_round(chat_script, monkeypatch):
    """If one specialist's LLM call raises uncaught, the round still completes."""
    from app import board

    monkeypatch.setattr(
        "app.evidence.EvidenceLedger.count_for", lambda self, sid: 1
    )
    monkeypatch.setattr(
        "app.evidence.EvidenceLedger.get_by_label", lambda self, lbl: object()
    )

    # Make every chat call raise the FIRST time it's called (simulates one
    # specialist crashing). Build a script where the FIRST call raises but
    # all subsequent calls return a valid draft.
    orig_response = _mock_response("Draft [1].\n\nRECOMMENDATION SUMMARY: Plan [1].")
    revised_response = _mock_response("Revised [1].\n\nRECOMMENDATION SUMMARY: Plan [1].")

    # 6 core specialists, each needs 2 calls (draft + self-check) = 12 calls.
    # We'll make call #3 raise (one of the specialists mid-flight).
    def script_factory():
        for i in range(12):
            if i == 3:
                # This raises during the gather; should be caught.
                yield _MockException(RuntimeError("simulated crash"))
            else:
                yield orig_response if i % 2 == 0 else revised_response
        # Then judge.
        yield _mock_response(json.dumps({
            "agree": True, "agreement_score": 0.95,
            "shared_recommendations": [], "disagreements": [],
            "open_questions_for_next_round": [],
        }))
        # Synthesizer.
        yield _mock_response("# Final\nPlan A.")

    script = list(script_factory())
    chat_script.script = script

    events = []
    result = asyncio.run(board.run_board(
        "Test case with enough length.", lambda t, p: events.append((t, p)),
        max_rounds=1, enable_trial_matching=False,
    ))

    # At least one specialist should have errored, but the board should still finish.
    completes = [e for e in events if e[0] == "specialist_round_complete"]
    assert len(completes) == len(_core_ids())
    error_count = sum(1 for c in completes if c[1]["status"] == "error")
    assert error_count >= 1, "expected at least one specialist to be marked error"
    assert result["round_reached"] == 1


def test_trial_matcher_disabled_excludes_it_from_roster(chat_script, monkeypatch):
    """enable_trial_matching=False keeps the matcher out of the board_started roster."""
    from app import board

    monkeypatch.setattr("app.evidence.EvidenceLedger.count_for", lambda self, sid: 1)
    monkeypatch.setattr("app.evidence.EvidenceLedger.get_by_label", lambda self, lbl: object())
    for _ in _core_ids():
        chat_script.script.append(_mock_response("Draft [1].\n\nRECOMMENDATION SUMMARY: Plan [1]."))
        chat_script.script.append(_mock_response("Revised [1].\n\nRECOMMENDATION SUMMARY: Plan [1]."))
    chat_script.script.append(_mock_response(json.dumps({
        "agree": True, "agreement_score": 0.95, "shared_recommendations": [],
        "disagreements": [], "open_questions_for_next_round": [],
    })))
    chat_script.script.append(_mock_response("# Final"))

    events = []
    asyncio.run(board.run_board(
        "Early-stage curable case.", lambda t, p: events.append((t, p)),
        max_rounds=1, enable_trial_matching=False,
    ))
    started = [e for e in events if e[0] == "board_started"][0]
    ids = {s["id"] for s in started[1]["specialists"]}
    assert "trial_matcher" not in ids
    assert ids == set(_core_ids())


# ---------- Clinical Trial Matcher pipeline (direct) ----------

def test_trial_matcher_participates_when_enabled(chat_script, monkeypatch):
    """Flattened: the matcher runs as a NORMAL specialist (1 draft + 1 self-check)."""
    from app import board
    from app.config import SPECIALIST_IDS

    monkeypatch.setattr("app.evidence.EvidenceLedger.count_for", lambda self, sid: 1)
    monkeypatch.setattr("app.evidence.EvidenceLedger.get_by_label", lambda self, lbl: object())
    for _ in SPECIALIST_IDS:  # 7 specialists, 2 calls each
        chat_script.script.append(_mock_response("Trial NCT01 [1].\n\nRECOMMENDATION SUMMARY: Plan [1]."))
        chat_script.script.append(_mock_response("Reviewed [1].\n\nRECOMMENDATION SUMMARY: Plan [1]."))
    chat_script.script.append(_mock_response(json.dumps({
        "agree": True, "agreement_score": 0.95, "shared_recommendations": [],
        "disagreements": [], "open_questions_for_next_round": [],
    })))
    chat_script.script.append(_mock_response("# Final"))

    events = []
    asyncio.run(board.run_board(
        "Advanced metastatic NSCLC, EGFR L858R.", lambda t, p: events.append((t, p)),
        max_rounds=1, enable_trial_matching=True,
    ))
    started = [e for e in events if e[0] == "board_started"][0]
    assert "trial_matcher" in {s["id"] for s in started[1]["specialists"]}
    completes = [e for e in events if e[0] == "specialist_round_complete"]
    tm = [c for c in completes if c[1]["specialist"] == "trial_matcher"]
    assert len(tm) == 1 and tm[0][1]["status"] == "done"


def test_trial_matcher_skips_via_run_specialist(chat_script, monkeypatch):
    """Flattened: trial_matcher self-SKIPs (one chat call) when there's no trial question."""
    from app.specialist import run_specialist
    from app.evidence import EvidenceLedger

    monkeypatch.setattr("app.evidence.EvidenceLedger.count_for", lambda self, sid: 1)
    chat_script.script.append(_mock_response("SKIP: no trial-relevant question for this case."))
    res = asyncio.run(run_specialist(
        "trial_matcher", "Resected stage I cancer on standard adjuvant therapy.", "",
        EvidenceLedger(), lambda t, p: None,
    ))
    assert res.status == "skipped"
    assert len(chat_script.calls) == 1


# ---------- Clinical Trial Matcher tools (no network) ----------

class _FakeLedger:
    def __init__(self):
        self.entries = {}
        self._n = 0

    def add(self, *, source_kind, source_id, **kw):
        if source_id not in self.entries:
            self._n += 1
            self.entries[source_id] = types.SimpleNamespace(
                label=str(self._n), source_kind=source_kind, source_id=source_id, **kw
            )
        return self.entries[source_id]


def _ctx(ledger):
    return types.SimpleNamespace(specialist_id="trial_matcher", pubmed_bias=None, ledger=ledger)


_FAKE_STUDY = {
    "protocolSection": {
        "identificationModule": {"nctId": "NCT00000001", "briefTitle": "Test Trial"},
        "statusModule": {
            "overallStatus": "RECRUITING",
            "startDateStruct": {"date": "2025-01", "type": "ACTUAL"},
            "lastUpdatePostDateStruct": {"date": "2026-05-01"},
            "primaryCompletionDateStruct": {"date": "2027-01"},
        },
        "designModule": {"phases": ["PHASE2"]},
        "conditionsModule": {"conditions": ["NSCLC"]},
        "armsInterventionsModule": {"interventions": [{"name": "DrugX"}]},
        "eligibilityModule": {
            "eligibilityCriteria": "Inclusion Criteria:\n* EGFR mutation\nExclusion Criteria:\n* Prior therapy",
            "sex": "ALL", "minimumAge": "18 Years", "healthyVolunteers": False,
            "stdAges": ["ADULT", "OLDER_ADULT"],
        },
        "contactsLocationsModule": {
            "locations": [{"city": "Boston", "state": "Massachusetts", "country": "United States", "status": "RECRUITING"}]
        },
    }
}


def test_match_search_sets_params_and_registers(monkeypatch):
    from app.tools import clinical_trial_matcher as m

    captured = {}

    def fake_get(url, params, timeout=15.0):
        if "nominatim" in url:
            return [{"lat": "42.36", "lon": "-71.06"}]
        captured["params"] = params
        return {"totalCount": 1, "studies": [_FAKE_STUDY]}

    monkeypatch.setattr(m, "_http_get_json_sync", fake_get)
    ledger = _FakeLedger()
    out = asyncio.run(m.run(
        {"condition": "non-small cell lung cancer", "biomarker_or_term": "EGFR L858R",
         "near_location": "Boston, MA", "max_results": 3},
        _ctx(ledger),
    ))
    p = captured["params"]
    assert p["query.cond"] == "non-small cell lung cancer"
    assert p["query.term"] == "EGFR L858R"
    assert p["sort"] == "LastUpdatePostDate:desc"
    assert p["filter.overallStatus"] == "RECRUITING|NOT_YET_RECRUITING|AVAILABLE"
    assert p["filter.geo"] == "distance(42.36,-71.06,100mi)"  # geocode succeeded
    assert "[1]" in out and "NCT00000001" in out
    e = ledger.entries["NCT00000001"]
    assert e.source_kind == "clinical_trial"


def test_match_search_geocode_failure_falls_back_to_locn(monkeypatch):
    from app.tools import clinical_trial_matcher as m

    captured = {}

    def fake_get(url, params, timeout=15.0):
        if "nominatim" in url:
            raise OSError("blocked")
        captured["params"] = params
        return {"totalCount": 1, "studies": [_FAKE_STUDY]}

    monkeypatch.setattr(m, "_http_get_json_sync", fake_get)
    asyncio.run(m.run(
        {"condition": "breast cancer", "near_location": "Boston, MA"}, _ctx(_FakeLedger())
    ))
    p = captured["params"]
    assert p.get("query.locn") == "Boston, MA"      # place-name fallback applied
    assert "filter.geo" not in p                     # no precise radius without coords


def test_details_returns_full_criteria_and_validates_nct(monkeypatch):
    from app.tools import clinical_trial_matcher as m

    monkeypatch.setattr(m, "_http_get_json_sync", lambda url, params, timeout=15.0: _FAKE_STUDY)
    ledger = _FakeLedger()
    out = asyncio.run(m.run_details({"nct_id": "NCT00000001"}, _ctx(ledger)))
    assert "FULL ELIGIBILITY CRITERIA" in out
    assert "EGFR mutation" in out and "Prior therapy" in out
    assert ledger.entries["NCT00000001"].source_kind == "clinical_trial"

    # Malformed NCT id returns a clean error string, never raises.
    bad = asyncio.run(m.run_details({"nct_id": "not-an-nct"}, _ctx(_FakeLedger())))
    assert "not a valid NCT id" in bad


def test_search_then_details_dedupe_to_one_entry(monkeypatch):
    """The search tool and details tool register the same NCT once (ledger enriches)."""
    from app.tools import clinical_trial_matcher as m

    def fake_get(url, params, timeout=15.0):
        if url.endswith("/studies"):
            return {"totalCount": 1, "studies": [_FAKE_STUDY]}
        return _FAKE_STUDY  # /studies/{nct}

    monkeypatch.setattr(m, "_http_get_json_sync", fake_get)
    ledger = _FakeLedger()
    ctx = _ctx(ledger)
    asyncio.run(m.run({"condition": "NSCLC", "max_results": 1}, ctx))
    asyncio.run(m.run_details({"nct_id": "NCT00000001"}, ctx))
    assert list(ledger.entries.keys()) == ["NCT00000001"]


def test_details_batch_fetches_multiple_and_ignores_bad(monkeypatch):
    from app.tools import clinical_trial_matcher as m

    monkeypatch.setattr(m, "_http_get_json_sync", lambda url, params, timeout=15.0: _FAKE_STUDY)
    ledger = _FakeLedger()
    out = asyncio.run(m.run_details_batch(
        {"nct_ids": ["NCT00000001", "NCT00000002", "oops"]}, _ctx(ledger)
    ))
    assert "NCT00000001" in out and "NCT00000002" in out
    assert "FULL ELIGIBILITY CRITERIA" in out
    assert "Ignored invalid ids: oops" in out
    assert set(ledger.entries.keys()) == {"NCT00000001", "NCT00000002"}


# ---------- Fused PubMed tool (no network) ----------

def test_pubmed_search_and_fetch_ranks_and_registers(monkeypatch):
    """Fused tool searches, ranks by evidence strength, fetches the top-k, registers them."""
    from app.tools import pubmed

    async def fake_search(query, ctx, max_results, min_year, sort_arg):
        return ["100", "200", "300"]  # review, guideline, RCT
    monkeypatch.setattr(pubmed, "_search_pmids", fake_search)
    monkeypatch.setattr(pubmed, "_entrez_summary_sync", lambda pmids: [
        {"pmid": "100", "title": "Old review", "journal": "J", "year": "2005", "article_types": ["Review"]},
        {"pmid": "200", "title": "NCCN guideline", "journal": "JNCCN", "year": "2024", "article_types": ["Practice Guideline"]},
        {"pmid": "300", "title": "Pivotal RCT", "journal": "NEJM", "year": "2020", "article_types": ["Randomized Controlled Trial"]},
    ])
    monkeypatch.setattr(pubmed, "_entrez_efetch_abstract_sync", lambda pmids: {
        p: {"title": f"T{p}", "journal": "J", "year": "2024", "abstract": "abstract text",
            "article_types": ["Practice Guideline"] if p == "200" else ["Review"]}
        for p in pmids
    })

    ledger = _FakeLedger()
    out = asyncio.run(pubmed.run_search_and_fetch({"query": "nsclc egfr", "max_results": 2}, _ctx(ledger)))
    assert "[1]" in out
    # Guideline (200) and RCT (300) outrank the old Review (100); top-2 are fetched.
    assert set(ledger.entries.keys()) == {"200", "300"}
    assert "100" not in ledger.entries


# ---------- _extract_summary robustness (regression: empty "What it concluded") ----------

def test_extract_summary_never_empty_for_nonempty_draft():
    """A draft that ends at the 'RECOMMENDATION SUMMARY:' header must still yield a
    non-empty summary (Rad Onc / Med Onc were showing a blank 'What it concluded')."""
    from app.specialist import _extract_summary
    assert _extract_summary("Body paragraph about the plan.\n\nRECOMMENDATION SUMMARY:")        # ends at header
    assert _extract_summary("Intro about the case.\n\nRECOMMENDATION SUMMARY:   \n\n")          # whitespace-only block
    assert _extract_summary("A draft with no summary header at all.")                          # no header
    s = _extract_summary("Discussion.\n\nRECOMMENDATION SUMMARY: Use cisplatin 100 mg/m2 [5].")
    assert "cisplatin" in s                                                                    # normal extraction intact
