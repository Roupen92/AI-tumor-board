"""Tumor-board orchestrator: round loop, consensus judge, final synthesizer."""
import asyncio
import json
import logging
import time
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
        "THIS IS THE ADVERSARIAL REVIEW ROUND — do not just restate your draft. Do two things:\n"
        "1) CHALLENGE the others: identify the weakest or most questionable claim in another "
        "specialist's recommendation above and explain, with a `[N]` citation, why it is wrong, "
        "unsupported, or needs reconsideration. Call out any unsafe drug interaction, sequencing "
        "error, staging/diagnosis issue, or contraindication you see. (If you genuinely find no "
        "flaw in the others, say so in one line.)\n"
        "2) DEFEND or REVISE your own recommendation against their positions and any points the "
        "board flagged. Retrieve additional evidence ONLY if needed to settle a disagreement; "
        "keep or revise prior citations. Then produce your updated, citation-grounded recommendation."
    )
    return "\n".join(parts)


def _coerce_score(val) -> float:
    """Clamp the judge's agreement_score to a float in [0, 1]. The judge is an LLM
    returning free-form JSON, so the field can be a stringified number ("0.9"), a
    word ("high"), or missing — any of which would otherwise crash float() in the
    round loop / synthesizer and .toFixed() in the frontend."""
    try:
        f = float(val)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, f))


def _normalize_verdict(verdict) -> dict:
    """Coerce a raw judge JSON object into the shape the rest of the pipeline trusts:
    a dict with a bool `agree` and a clamped-float `agreement_score`."""
    if not isinstance(verdict, dict):
        verdict = {}
    verdict["agree"] = bool(verdict.get("agree"))
    verdict["agreement_score"] = _coerce_score(verdict.get("agreement_score", 0.0))
    return verdict


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
        # Cannot run consensus with fewer than 2 active specialists. Report this
        # honestly rather than trivially claiming agreement — a UI showing
        # "Consensus reached" when everyone errored is misleading.
        return {
            "agree": False,
            "agreement_score": 0.0,
            "shared_recommendations": [],
            "disagreements": [],
            "open_questions_for_next_round": [],
            "note": (
                f"Only {len(summaries)} specialist(s) produced a recommendation. "
                "Cannot evaluate consensus with fewer than 2 active specialists."
            ),
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
        return _normalize_verdict(json.loads(raw))
    except llm.QuotaExceeded as e:
        log.warning("Judge hit LLM quota: %s", e)
        return {
            "agree": False, "agreement_score": 0.0,
            "shared_recommendations": [], "disagreements": [],
            "open_questions_for_next_round": [],
            "error": "Consensus judge could not run — LLM quota exceeded.",
        }
    except Exception as e:
        log.exception("Judge failed; defaulting to no-consensus.")
        msg = str(e)
        if len(msg) > 200:
            msg = msg[:197] + "…"
        return {
            "agree": False, "agreement_score": 0.0,
            "shared_recommendations": [], "disagreements": [],
            "open_questions_for_next_round": [],
            "error": msg,
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
        labels = ", ".join(res.evidence_labels) if res.evidence_labels else "(none)"
        drafts.append(
            f"--- {name} ({sid}) ---\n"
            f"Evidence labels used: {labels}\n"
            f"{res.recommendation_summary}"
        )

    judge_summary = (
        json.dumps(last_judge, indent=2) if last_judge else "(no judge verdict)"
    )

    user_content = (
        f"CASE:\n{case}\n\n"
        f"JUDGE'S FINAL VERDICT:\n{judge_summary}\n\n"
        "SPECIALIST RECOMMENDATION SUMMARIES:\n\n" + "\n\n".join(drafts)
    )
    messages = [
        {"role": "system", "content": prompts.SYNTHESIZER},
        {"role": "user", "content": user_content},
    ]
    try:
        resp = llm.chat(messages, tools=None)
        markdown = resp.choices[0].message.content or "(synthesis failed)"
    except llm.QuotaExceeded as e:
        log.warning("Synthesizer hit LLM quota: %s", e)
        markdown = (
            "## Final synthesis unavailable\n\n"
            "The LLM provider returned a rate-limit error and the synthesizer "
            "could not run. Please check that billing is enabled on your "
            "provider account, then re-run the board.\n\n"
            "Each specialist's individual draft is still available in the "
            "discussion transcript below."
        )
    except Exception as e:
        log.exception("Synthesizer failed.")
        msg = str(e)
        if len(msg) > 200:
            msg = msg[:197] + "…"
        markdown = f"## Final synthesis failed\n\n`{msg}`"

    return {
        "agree": bool(last_judge and last_judge.get("agree")),
        "agreement_score": float(last_judge.get("agreement_score", 0.0)) if last_judge else 0.0,
        "markdown": markdown,
        "references": ledger.public_list(),
    }


def _build_timing_summary(timing: dict, total_s: float, rounds: int) -> dict:
    """Turn the raw timing accumulator into a UI/log-friendly breakdown.

    NOTE: per-specialist and llm/tool totals are CUMULATIVE work across all agents,
    so they sum to more than total_s (wall clock) because specialists — and tool
    calls within an agent — run in parallel. total_s is the real elapsed time.
    """
    specs, llm_total, tool_total, llm_calls, tool_calls = [], 0.0, 0.0, 0, 0
    for sid, rec in timing["specialists"].items():
        llm_total += rec["llm"]
        tool_total += rec["tool"]
        llm_calls += rec["llm_n"]
        tool_calls += rec["tool_n"]
        specs.append({
            "id": sid,
            "display_name": SPECIALIST_CONFIGS[sid]["display_name"],
            "wall_s": round(rec["wall"], 1),
            "llm_s": round(rec["llm"], 1),
            "tool_s": round(rec["tool"], 1),
            "llm_calls": rec["llm_n"],
            "tool_calls": rec["tool_n"],
        })
    specs.sort(key=lambda s: s["wall_s"], reverse=True)
    tools = sorted(
        ({"name": n, "seconds": round(v["seconds"], 1), "calls": v["calls"]}
         for n, v in timing["tools"].items()),
        key=lambda t: t["seconds"], reverse=True,
    )
    return {
        "total_s": round(total_s, 1),
        "rounds": rounds,
        "llm_s": round(llm_total + timing["judge"] + timing["synth"], 1),
        "tool_s": round(tool_total, 1),
        "judge_s": round(timing["judge"], 1),
        "synth_s": round(timing["synth"], 1),
        "llm_calls": llm_calls,
        "tool_calls": tool_calls,
        "specialists": specs,
        "tools": tools,
    }


def _format_timing(s: dict) -> str:
    lines = [
        f"=== TIMING: {s['total_s']}s wall · {s['rounds']} round(s) ===",
        f"  cumulative work — LLM {s['llm_s']}s ({s['llm_calls']} calls) | "
        f"tools {s['tool_s']}s ({s['tool_calls']} calls) | judge {s['judge_s']}s | synth {s['synth_s']}s",
        "  per specialist (wall | llm | tools | #llm | #tools):",
    ]
    for sp in s["specialists"]:
        lines.append(
            f"    {sp['display_name'][:26]:<26} {sp['wall_s']:>6}s | {sp['llm_s']:>6}s | "
            f"{sp['tool_s']:>6}s | {sp['llm_calls']:>2} | {sp['tool_calls']:>2}"
        )
    lines.append("  tool time by name (cumulative):")
    for t in s["tools"]:
        lines.append(f"    {t['name'][:30]:<30} {t['seconds']:>6}s  ({t['calls']} calls)")
    return "\n".join(lines)


async def run_board(
    case: str,
    emit: Callable[[str, dict], None],
    max_rounds: int = MAX_ROUNDS,
    enable_trial_matching: bool = True,
) -> dict:
    """Main entry. Streams events via emit(type, payload). Returns the final dict.

    When enable_trial_matching is False the Clinical Trial Matcher is left out of the
    roster entirely (it does not run and is not shown on the round table).
    """
    ledger = EvidenceLedger()
    history: dict[str, SpecialistResult] = {}
    last_judge: dict | None = None
    round_reached = 0

    board_t0 = time.perf_counter()
    timing = {"specialists": {}, "tools": {}, "judge": 0.0, "synth": 0.0}

    def _spec_rec(sid):
        return timing["specialists"].setdefault(
            sid, {"wall": 0.0, "llm": 0.0, "tool": 0.0, "llm_n": 0, "tool_n": 0}
        )

    active_ids = [
        sid for sid in SPECIALIST_IDS
        if sid != "trial_matcher" or enable_trial_matching
    ]

    roster = [s for s in public_specialist_info() if s["id"] in active_ids]
    emit(
        "board_started",
        {
            "max_rounds": max_rounds,
            "specialists": roster,
        },
    )

    sem = asyncio.Semaphore(PARALLEL_SPECIALISTS)

    async def run_one(spec_id: str, round_idx: int) -> tuple[str, SpecialistResult]:
        async with sem:
            prefix = _build_context_prefix(round_idx, spec_id, history, last_judge)

            def _emit(t: str, p: dict, sid=spec_id) -> None:
                # Accumulate instrumentation timing as events stream through.
                if t == "llm_timing":
                    rec = _spec_rec(sid)
                    rec["llm"] += p.get("seconds", 0.0)
                    rec["llm_n"] += 1
                elif t == "tool_result":
                    rec = _spec_rec(sid)
                    secs = p.get("seconds", 0.0)
                    rec["tool"] += secs
                    rec["tool_n"] += 1
                    tr = timing["tools"].setdefault(p.get("tool", "?"), {"seconds": 0.0, "calls": 0})
                    tr["seconds"] += secs
                    tr["calls"] += 1
                emit("specialist_event", {"specialist": sid, "type": t, "payload": p})

            _t0 = time.perf_counter()
            res = await run_specialist(spec_id, case, prefix, ledger, _emit)
            _spec_rec(spec_id)["wall"] += time.perf_counter() - _t0
            return spec_id, res

    for r in range(1, max_rounds + 1):
        round_reached = r
        emit("round_started", {"round": r})

        tasks = [run_one(sid, r) for sid in active_ids]
        # return_exceptions=True so a single specialist crashing uncaught doesn't
        # bring the whole round down — we synthesize a clean error result instead.
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        results = []
        for sid, res in zip(active_ids, raw_results):
            if isinstance(res, BaseException):
                log.exception("Specialist %s crashed uncaught", sid, exc_info=res)
                clean = SpecialistResult(
                    specialist_id=sid, status="error",
                    error=f"{type(res).__name__}: {str(res)[:160]}",
                )
                results.append((sid, clean))
            else:
                results.append(res)

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
                    # Full ledger entries for this specialist's cites, so the frontend
                    # can resolve "Sources it used" and inline [N] links mid-run rather
                    # than waiting for the final references payload.
                    "evidence": [
                        e.public()
                        for e in (ledger.get_by_label(l) for l in res.evidence_labels)
                        if e is not None
                    ],
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

        emit("phase", {"phase": "judging", "round": r})
        _j0 = time.perf_counter()
        last_judge = await asyncio.to_thread(_run_judge, history, case)
        timing["judge"] += time.perf_counter() - _j0
        last_judge["round"] = r
        emit("consensus_check", last_judge)

        if last_judge.get("agree") and float(
            last_judge.get("agreement_score", 0.0)
        ) >= CONSENSUS_THRESHOLD:
            break

    emit("phase", {"phase": "synthesizing"})
    _s0 = time.perf_counter()
    final = await asyncio.to_thread(
        _synthesize_final, history, last_judge, case, ledger
    )
    timing["synth"] += time.perf_counter() - _s0

    summary = _build_timing_summary(timing, time.perf_counter() - board_t0, round_reached)
    log.info("\n%s", _format_timing(summary))
    final["round_reached"] = round_reached
    final["timing"] = summary
    emit("timing_summary", summary)
    emit("final", final)
    return final
