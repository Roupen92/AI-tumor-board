"""Smoke tests for the board orchestrator with a mocked OpenAI client.

Run: pytest -v
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


class _ChatScript:
    """Replays a queue of canned responses keyed by call order."""
    def __init__(self):
        self.calls = []
        self.script: list = []

    def __call__(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if not self.script:
            return _mock_response("default draft.\nRECOMMENDATION SUMMARY: default summary.")
        return self.script.pop(0)


@pytest.fixture
def chat_script(monkeypatch):
    script = _ChatScript()
    from app import llm
    monkeypatch.setattr(llm, "chat", script)
    return script


# ---------- Tests ----------

def test_board_terminates_on_consensus(chat_script, monkeypatch):
    """Judge returns agree=true; loop must exit after round 1."""
    from app import board
    from app.config import SPECIALIST_IDS

    # With the retrieve-or-abstain rule + citation-required rule, the mocked drafts
    # must (a) have evidence in the ledger and (b) include an [E#] citation.
    monkeypatch.setattr(
        "app.evidence.EvidenceLedger.count_for", lambda self, sid: 1
    )
    specialist_draft = "Comprehensive recommendation [E1].\n\nRECOMMENDATION SUMMARY: Go with plan A [E1]."
    self_check_draft = "Reviewed [E1].\n\nRECOMMENDATION SUMMARY: Go with plan A [E1]."

    # Per specialist: 1 tool-loop call (returns draft, no tool_calls), 1 self-check call.
    for _ in SPECIALIST_IDS:
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
        max_rounds=3,
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
    from app.config import SPECIALIST_IDS

    monkeypatch.setattr(
        "app.evidence.EvidenceLedger.count_for", lambda self, sid: 1
    )

    def queue_round():
        for _ in SPECIALIST_IDS:
            chat_script.script.append(_mock_response("Draft [E1].\n\nRECOMMENDATION SUMMARY: Plan X [E1]."))
            chat_script.script.append(_mock_response("Revised [E1].\n\nRECOMMENDATION SUMMARY: Plan X [E1]."))
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
        max_rounds=2,
    ))

    round_starts = [e for e in events if e[0] == "round_started"]
    assert len(round_starts) == 2
    assert result["agree"] is False
    assert result["round_reached"] == 2


def test_molecular_skips_when_no_data(chat_script, monkeypatch):
    """A specialist returning 'SKIP:' is recorded as skipped and excluded from judge."""
    from app import board
    from app.config import SPECIALIST_IDS

    monkeypatch.setattr(
        "app.evidence.EvidenceLedger.count_for", lambda self, sid: 1
    )

    # Specialists in config.SPECIALIST_IDS order.
    for sid in SPECIALIST_IDS:
        if sid in ("molecular", "pathologist"):
            # Single response, no tool_calls, no self-check (skip short-circuits).
            chat_script.script.append(_mock_response("SKIP: not applicable."))
        else:
            chat_script.script.append(_mock_response("Draft [E1].\n\nRECOMMENDATION SUMMARY: Plan [E1]."))
            chat_script.script.append(_mock_response("Revised [E1].\n\nRECOMMENDATION SUMMARY: Plan [E1]."))

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
        max_rounds=2,
    ))

    completes = [e for e in events if e[0] == "specialist_round_complete"]
    skipped = [c for c in completes if c[1]["specialist"] in ("molecular", "pathologist")]
    assert len(skipped) == 2
    for c in skipped:
        assert c[1]["status"] == "skipped"


def test_agent_abstains_when_no_evidence_retrieved(chat_script, monkeypatch):
    """If a specialist never retrieves evidence, it must abstain (not produce a draft)."""
    from app import board
    from app.config import SPECIALIST_IDS

    # Force count_for to always return 0 — no evidence ever registered.
    monkeypatch.setattr(
        "app.evidence.EvidenceLedger.count_for", lambda self, sid: 0
    )

    # Each specialist: round 1 produces a draft (no tool_calls, no citations). The
    # retrieve-or-abstain retry then re-prompts; the agent responds with ABSTAIN.
    for _ in SPECIALIST_IDS:
        chat_script.script.append(_mock_response("Draft.\n\nRECOMMENDATION SUMMARY: Plan."))
        chat_script.script.append(_mock_response("ABSTAIN: insufficient evidence."))

    # Judge: with all abstained, the early-out returns trivial agree=true.
    # No judge call expected, but synthesizer still runs.
    chat_script.script.append(_mock_response("# Final\n(all abstained)"))

    events = []
    result = asyncio.run(board.run_board(
        "Test case where retrieval will fail.",
        lambda t, p: events.append((t, p)),
        max_rounds=1,
    ))

    completes = [e for e in events if e[0] == "specialist_round_complete"]
    assert len(completes) == len(SPECIALIST_IDS)
    for c in completes:
        assert c[1]["status"] == "no_evidence", f"{c[1]['specialist']} should have abstained"
    # Confirm no_evidence emit event surfaced for each specialist.
    no_ev_events = [e for e in events if e[0] == "specialist_event"
                    and e[1].get("type") == "no_evidence"]
    assert len(no_ev_events) == len(SPECIALIST_IDS)


def test_agent_abstains_when_draft_has_no_citations(chat_script, monkeypatch):
    """Even with evidence in the ledger, a draft with zero [E#] labels must abstain."""
    from app import board
    from app.config import SPECIALIST_IDS

    # Pretend the ledger has evidence so the first-pass retrieval check passes.
    monkeypatch.setattr(
        "app.evidence.EvidenceLedger.count_for", lambda self, sid: 1
    )

    # Each specialist: draft has no [E#] citations, self-check returns the same.
    # The post-self-check rule should force abstention.
    for _ in SPECIALIST_IDS:
        chat_script.script.append(_mock_response("Draft from training.\n\nRECOMMENDATION SUMMARY: Plan."))
        chat_script.script.append(_mock_response("Still no citations.\n\nRECOMMENDATION SUMMARY: Plan."))

    # Synthesizer still runs.
    chat_script.script.append(_mock_response("# Final\n(all abstained)"))

    events = []
    asyncio.run(board.run_board(
        "Case where the model ignores tool results.",
        lambda t, p: events.append((t, p)),
        max_rounds=1,
    ))

    completes = [e for e in events if e[0] == "specialist_round_complete"]
    for c in completes:
        assert c[1]["status"] == "no_evidence", (
            f"{c[1]['specialist']} should have been forced to abstain (no citations in draft)"
        )
