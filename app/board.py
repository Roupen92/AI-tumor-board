"""Tumor-board orchestrator: round loop, consensus judge, final synthesizer."""
import asyncio
import json
import logging
from typing import Callable

from app import llm, prompts
from app.config import (
    CONSENSUS_THRESHOLD,
    MAX_ROUNDS,
    PARALLEL_SPECIALISTS,
    SPECIALIST_CONFIGS,
    SPECIALIST_IDS,
    public_specialist_info,
)
from app.evidence import EvidenceLedger
from app.specialist import SpecialistResult, run_specialist

log = logging.getLogger(__name__)


def _summary_or_skip(res: SpecialistResult) -> str:
    if res.status == "skipped":
        return "(skipped — not applicable to this case)"
    if res.status == "no_evidence":
        return "(abstained — no evidence retrieved)"
    if res.status == "error":
        return f"(error: {res.error})"
    return res.recommendation_summary


# Conditional agents whose findings are broadcast to every other specialist
# (prepended to their context_prefix in round 2+).
_BROADCAST_FROM = ("molecular", "pathologist")


def _broadcast_findings_block(history: dict[str, SpecialistResult]) -> str:
    """Inject conditional-agent findings into other specialists' context."""
    blocks = []
    for sid in _BROADCAST_FROM:
        res = history.get(sid)
        if not res or res.status != "done":
            continue
        name = SPECIALIST_CONFIGS[sid]["display_name"].upper()
        blocks.append(
            f"{name} FINDINGS RELEVANT TO THIS CASE (from the {SPECIALIST_CONFIGS[sid]['display_name']}):\n"
            f"{res.recommendation_summary}\n"
            f"(See the {SPECIALIST_CONFIGS[sid]['display_name']}'s full draft for cited evidence labels.)"
        )
    return "\n\n".join(blocks)


def _build_context_prefix(
    round_idx: int,
    spec_id: str,
    history: dict[str, SpecialistResult],
    last_judge: dict | None,
) -> str:
    if round_idx == 1:
        return ""

    parts = [f"TUMOR BOARD DISCUSSION — Round {round_idx}", ""]

    own = history.get(spec_id)
    if own and own.status == "done":
        parts.append("YOUR PRIOR RECOMMENDATION (last round):")
        parts.append(own.recommendation_summary)
        parts.append("")

    parts.append("OTHER SPECIALISTS' RECOMMENDATIONS (last round):")
    for other_id in SPECIALIST_IDS:
        if other_id == spec_id:
            continue
        other = history.get(other_id)
        name = SPECIALIST_CONFIGS[other_id]["display_name"]
        if not other:
            continue
        parts.append(f"- {name}: {_summary_or_skip(other)}")
    parts.append("")

    # Inject molecular + pathology findings as privileged shared input for everyone else.
    if spec_id not in _BROADCAST_FROM:
        broadcast_block = _broadcast_findings_block(history)
        if broadcast_block:
            parts.append(broadcast_block)
            parts.append("")

    if last_judge and last_judge.get("open_questions_for_next_round"):
        parts.append("POINTS THE BOARD FLAGGED FOR YOU TO ADDRESS:")
        for q in last_judge["open_questions_for_next_round"][:8]:
            parts.append(f"- {q}")
        parts.append("")

    parts.append(
        "Please reconsider, retrieve additional evidence if needed, and produce an "
        "updated recommendation. You may keep or revise prior citations."
    )
    return "\n".join(parts)


def _run_judge(history: dict[str, SpecialistResult], case: str) -> dict:
    """Single GPT-5.1 JSON call. Skipped specialists are excluded from the consensus check."""
    summaries = []
    for sid in SPECIALIST_IDS:
        res = history.get(sid)
        if not res or res.status != "done":
            continue
        name = SPECIALIST_CONFIGS[sid]["display_name"]
        summaries.append(f"**{name} ({sid})**: {res.recommendation_summary}")

    if len(summaries) < 2:
        # Trivially "agree" if fewer than 2 specialists produced recommendations.
        return {
            "agree": True,
            "agreement_score": 1.0,
            "shared_recommendations": [],
            "disagreements": [],
            "open_questions_for_next_round": [],
            "note": "Insufficient active specialists for consensus check.",
        }

    user_content = (
        f"CASE:\n{case}\n\n"
        "SPECIALIST RECOMMENDATIONS THIS ROUND:\n" + "\n\n".join(summaries)
    )
    messages = [
        {"role": "system", "content": prompts.JUDGE},
        {"role": "user", "content": user_content},
    ]
    try:
        resp = llm.chat(messages, response_format={"type": "json_object"})
        raw = resp.choices[0].message.content or "{}"
        return json.loads(raw)
    except Exception as e:
        log.exception("Judge failed; defaulting to no-consensus.")
        return {
            "agree": False,
            "agreement_score": 0.0,
            "shared_recommendations": [],
            "disagreements": [],
            "open_questions_for_next_round": [],
            "error": str(e),
        }


def _synthesize_final(
    history: dict[str, SpecialistResult],
    last_judge: dict | None,
    case: str,
    ledger: EvidenceLedger,
) -> dict:
    """Single GPT-5.1 call that produces the final markdown recommendation."""
    drafts = []
    for sid in SPECIALIST_IDS:
        res = history.get(sid)
        if not res or res.status != "done":
            continue
        name = SPECIALIST_CONFIGS[sid]["display_name"]
        drafts.append(f"--- {name} ({sid}) ---\n{res.draft_markdown}")

    judge_summary = (
        json.dumps(last_judge, indent=2) if last_judge else "(no judge verdict)"
    )

    user_content = (
        f"CASE:\n{case}\n\n"
        f"JUDGE'S FINAL VERDICT:\n{judge_summary}\n\n"
        "SPECIALIST FINAL DRAFTS:\n\n" + "\n\n".join(drafts)
    )
    messages = [
        {"role": "system", "content": prompts.SYNTHESIZER},
        {"role": "user", "content": user_content},
    ]
    try:
        resp = llm.chat(messages, tools=None)
        markdown = resp.choices[0].message.content or "(synthesis failed)"
    except Exception as e:
        log.exception("Synthesizer failed.")
        markdown = f"Final synthesis failed: {e}"

    return {
        "agree": bool(last_judge and last_judge.get("agree")),
        "agreement_score": float(last_judge.get("agreement_score", 0.0)) if last_judge else 0.0,
        "markdown": markdown,
        "references": ledger.public_list(),
    }


async def run_board(
    case: str,
    emit: Callable[[str, dict], None],
    max_rounds: int = MAX_ROUNDS,
) -> dict:
    """Main entry. Streams events via emit(type, payload). Returns the final dict."""
    ledger = EvidenceLedger()
    history: dict[str, SpecialistResult] = {}
    last_judge: dict | None = None
    round_reached = 0

    emit(
        "board_started",
        {
            "max_rounds": max_rounds,
            "specialists": public_specialist_info(),
        },
    )

    sem = asyncio.Semaphore(PARALLEL_SPECIALISTS)

    async def run_one(spec_id: str, round_idx: int) -> tuple[str, SpecialistResult]:
        async with sem:
            prefix = _build_context_prefix(round_idx, spec_id, history, last_judge)

            def _emit(t: str, p: dict, sid=spec_id) -> None:
                emit("specialist_event", {"specialist": sid, "type": t, "payload": p})

            res = await run_specialist(spec_id, case, prefix, ledger, _emit)
            return spec_id, res

    for r in range(1, max_rounds + 1):
        round_reached = r
        emit("round_started", {"round": r})

        tasks = [run_one(sid, r) for sid in SPECIALIST_IDS]
        results = await asyncio.gather(*tasks)

        for sid, res in results:
            history[sid] = res
            emit(
                "specialist_round_complete",
                {
                    "specialist": sid,
                    "round": r,
                    "status": res.status,
                    "draft_markdown": res.draft_markdown,
                    "recommendation_summary": res.recommendation_summary,
                    "evidence_labels": res.evidence_labels,
                    "error": res.error,
                },
            )
            emit(
                "discussion_turn",
                {
                    "specialist": sid,
                    "round": r,
                    "text": _summary_or_skip(res),
                    "status": res.status,
                },
            )

        last_judge = await asyncio.to_thread(_run_judge, history, case)
        last_judge["round"] = r
        emit("consensus_check", last_judge)

        if last_judge.get("agree") and float(
            last_judge.get("agreement_score", 0.0)
        ) >= CONSENSUS_THRESHOLD:
            break

    final = await asyncio.to_thread(
        _synthesize_final, history, last_judge, case, ledger
    )
    final["round_reached"] = round_reached
    emit("final", final)
    return final
