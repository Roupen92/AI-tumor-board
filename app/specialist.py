"""Run a single specialist as a GPT-5.1 tool-loop, with self-check and event emission."""
import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

from app import llm, prompts
from app.config import SPECIALIST_CONFIGS, MAX_TOOL_ITERATIONS
from app.evidence import EvidenceLedger
from app.tools import ToolContext, schemas_for, dispatch

# Re-exports just so _continue_tool_loop's local import is unnecessary; kept clean below.

log = logging.getLogger(__name__)


@dataclass
class SpecialistResult:
    specialist_id: str
    status: str                          # "done" | "skipped" | "error"
    draft_markdown: str = ""
    recommendation_summary: str = ""     # 1-3 sentences for the judge
    evidence_labels: list[str] = field(default_factory=list)
    error: str = ""


SKIP_MARKER = re.compile(r"^\s*SKIP\s*:", re.IGNORECASE | re.MULTILINE)
RECOMMENDATION_MARKER = re.compile(r"RECOMMENDATION\s+SUMMARY\s*:\s*(.+)$", re.IGNORECASE | re.DOTALL)

MAX_TOOL_RESULT_CHARS_IN_HISTORY = 1800


def _extract_summary(draft: str) -> str:
    """Pull out the RECOMMENDATION SUMMARY block, or fall back to the first paragraph."""
    m = RECOMMENDATION_MARKER.search(draft)
    if m:
        text = m.group(1).strip()
        # If the summary spans several lines, keep up to ~3 sentences.
        sentences = re.split(r"(?<=[.!?])\s+", text)
        return " ".join(sentences[:3]).strip()
    # Fallback: first non-empty paragraph, capped.
    for para in draft.split("\n\n"):
        para = para.strip()
        if para:
            return para[:600]
    return draft[:300]


async def _run_tool_loop(
    spec_id: str,
    case: str,
    context_prefix: str,
    ledger: EvidenceLedger,
    emit,
) -> tuple[str, list[dict]]:
    """Drive the GPT-5.1 tool loop. Returns (final_draft, final_messages)."""
    cfg = SPECIALIST_CONFIGS[spec_id]
    tools = schemas_for(cfg["allowed_tools"])
    ctx = ToolContext(
        specialist_id=spec_id,
        pubmed_bias=cfg.get("pubmed_bias"),
        ledger=ledger,
    )

    user_content = (context_prefix + "\n\n---\n\n" + case) if context_prefix else case
    messages = [
        {"role": "system", "content": cfg["system_prompt"]},
        {"role": "user", "content": user_content},
    ]

    emit("started", {"allowed_tools": sorted(cfg["allowed_tools"])})

    for iteration in range(1, MAX_TOOL_ITERATIONS + 1):
        emit("thinking", {"iteration": iteration})
        resp = await asyncio.to_thread(llm.chat, messages, tools=tools)
        choice = resp.choices[0]
        msg = choice.message

        # Persist the assistant message into history.
        assistant_dict: dict = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            assistant_dict["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        messages.append(assistant_dict)

        # No tool calls → assistant produced the draft.
        if not msg.tool_calls:
            return msg.content or "", messages

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            emit("tool_call", {"tool": name, "args": args})
            result = await dispatch(name, args, ctx)
            preview = (result[:280] + "…") if len(result) > 280 else result
            emit("tool_result", {"tool": name, "preview": preview})
            stored_result = (
                result[:MAX_TOOL_RESULT_CHARS_IN_HISTORY]
                + "\n\n…[result truncated; full content was used to inform earlier reasoning]"
                if len(result) > MAX_TOOL_RESULT_CHARS_IN_HISTORY
                else result
            )
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": stored_result}
            )

    # Hit max iterations; ask the model to wrap up.
    emit("tool_loop_capped", {"iterations": MAX_TOOL_ITERATIONS})
    messages.append(
        {
            "role": "user",
            "content": (
                "You've reached the tool-call budget. Produce your final recommendation "
                "now using the evidence already retrieved. Use the [N] labels you've seen "
                "in tool results."
            ),
        }
    )
    resp = await asyncio.to_thread(llm.chat, messages, tools=None)
    final = resp.choices[0].message.content or ""
    messages.append({"role": "assistant", "content": final})
    return final, messages


async def _self_check(draft: str, messages: list[dict], emit) -> str:
    """Re-prompt the model to downgrade unsupported claims; return the revised draft."""
    emit("self_checking", {})
    messages = messages + [{"role": "user", "content": prompts.SELF_CHECK}]
    resp = await asyncio.to_thread(llm.chat, messages, tools=None)
    return resp.choices[0].message.content or draft


RETRIEVE_OR_ABSTAIN_PROMPT = (
    "You attempted to finalize an answer but you have NOT registered any evidence "
    "in the board's evidence ledger. The board's rule is: an agent that does not "
    "retrieve information must not answer. Right now, you MUST either (a) call "
    "your retrieval tools (`pubmed_search` then `pubmed_fetch`, and any other "
    "tools available to your specialty) until at least one piece of evidence is "
    "registered with a [N] label, or (b) abstain by responding with EXACTLY "
    "this and nothing else:\n\n"
    "ABSTAIN: insufficient evidence available for me to answer responsibly.\n\n"
    "Do not produce a clinical recommendation without retrieved evidence."
)

ABSTAIN_MARKER = re.compile(r"^\s*ABSTAIN\s*:", re.IGNORECASE | re.MULTILINE)


async def run_specialist(
    spec_id: str,
    case: str,
    context_prefix: str,
    ledger: EvidenceLedger,
    emit,
) -> SpecialistResult:
    """Run one specialist end-to-end. Returns SpecialistResult."""
    try:
        draft, messages = await _run_tool_loop(spec_id, case, context_prefix, ledger, emit)

        # Conditional-agent SKIP: respect it before doing anything else.
        if SKIP_MARKER.search(draft.strip().splitlines()[0] if draft.strip() else ""):
            emit("skipped", {"reason": draft.strip()})
            return SpecialistResult(
                specialist_id=spec_id,
                status="skipped",
                draft_markdown=draft.strip(),
                recommendation_summary="(skipped — not applicable to this case)",
            )

        # Rule: if the agent retrieved no evidence, force one retry or abstention.
        if ledger.count_for(spec_id) == 0:
            emit("retrieve_or_abstain", {"reason": "no evidence registered in first pass"})
            messages.append({"role": "user", "content": RETRIEVE_OR_ABSTAIN_PROMPT})
            draft, messages = await _continue_tool_loop(spec_id, messages, ledger, emit)

            if ABSTAIN_MARKER.search(draft.strip().splitlines()[0] if draft.strip() else ""):
                emit("no_evidence", {"reason": draft.strip()})
                return SpecialistResult(
                    specialist_id=spec_id,
                    status="no_evidence",
                    draft_markdown=draft.strip(),
                    recommendation_summary="(abstained — no evidence retrieved)",
                )

            if ledger.count_for(spec_id) == 0:
                # Still no evidence after second pass → force abstention.
                emit("no_evidence", {"reason": "no evidence after retry"})
                return SpecialistResult(
                    specialist_id=spec_id,
                    status="no_evidence",
                    draft_markdown=(
                        "ABSTAIN: I was unable to retrieve evidence to support a "
                        "recommendation for this case. Per the board's rule, I am "
                        "abstaining rather than answering from clinical judgment alone."
                    ),
                    recommendation_summary="(abstained — no evidence retrieved)",
                )

        revised = await _self_check(draft, messages, emit)

        # If self-check abstained, honor it.
        if ABSTAIN_MARKER.search(revised.strip().splitlines()[0] if revised.strip() else ""):
            emit("no_evidence", {"reason": revised.strip()})
            return SpecialistResult(
                specialist_id=spec_id,
                status="no_evidence",
                draft_markdown=revised.strip(),
                recommendation_summary="(abstained — no evidence retrieved)",
            )

        # Evidence labels that the draft actually cites (plain journal-style [1] [2] ...).
        # Only treat numbers in the ledger as citations to avoid matching years like [2024].
        all_nums = sorted(set(re.findall(r"\[(\d{1,3})\]", revised)), key=int)
        labels = [n for n in all_nums if ledger.get_by_label(n) is not None]
        for label in labels:
            ledger.mark_cited(label, spec_id)

        # HARD RULE: if the revised draft has zero citations, the agent is answering
        # from training data. Force abstention.
        if not labels:
            emit("no_evidence", {"reason": "draft has no [N] citations after self-check"})
            return SpecialistResult(
                specialist_id=spec_id,
                status="no_evidence",
                draft_markdown=(
                    "ABSTAIN: my draft did not include any citations to retrieved "
                    "evidence. Per the board's rule against answering from training "
                    "knowledge, I am abstaining rather than presenting unsupported claims."
                ),
                recommendation_summary="(abstained — draft was not citation-grounded)",
            )

        summary = _extract_summary(revised)
        emit("done", {"summary": summary, "evidence_labels": labels})
        return SpecialistResult(
            specialist_id=spec_id,
            status="done",
            draft_markdown=revised,
            recommendation_summary=summary,
            evidence_labels=labels,
        )
    except llm.QuotaExceeded as e:
        log.warning("Specialist %s hit LLM quota: %s", spec_id, e)
        emit("error", {"message": "LLM quota exceeded — see Settings → Billing."})
        return SpecialistResult(
            specialist_id=spec_id,
            status="error",
            error="LLM quota exceeded. Verify billing is enabled on your provider account.",
        )
    except Exception as e:
        log.exception("Specialist %s failed", spec_id)
        # Truncate long error messages so they don't dump JSON into the UI.
        msg = str(e)
        if len(msg) > 200:
            msg = msg[:197] + "…"
        emit("error", {"message": f"{type(e).__name__}: {msg}"})
        return SpecialistResult(specialist_id=spec_id, status="error", error=msg)


async def _continue_tool_loop(
    spec_id: str,
    messages: list[dict],
    ledger: EvidenceLedger,
    emit,
) -> tuple[str, list[dict]]:
    """Resume the tool loop with existing message history. Used by the retrieve-or-abstain retry."""
    cfg = SPECIALIST_CONFIGS[spec_id]
    tools = schemas_for(cfg["allowed_tools"])
    ctx = ToolContext(
        specialist_id=spec_id,
        pubmed_bias=cfg.get("pubmed_bias"),
        ledger=ledger,
    )
    for iteration in range(1, MAX_TOOL_ITERATIONS + 1):
        emit("thinking", {"iteration": f"retry-{iteration}"})
        resp = await asyncio.to_thread(llm.chat, messages, tools=tools)
        msg = resp.choices[0].message
        assistant_dict: dict = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            assistant_dict["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
        messages.append(assistant_dict)
        if not msg.tool_calls:
            return msg.content or "", messages
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            emit("tool_call", {"tool": name, "args": args})
            result = await dispatch(name, args, ctx)
            preview = (result[:280] + "…") if len(result) > 280 else result
            emit("tool_result", {"tool": name, "preview": preview})
            stored_result = (
                result[:MAX_TOOL_RESULT_CHARS_IN_HISTORY]
                + "\n\n…[result truncated; full content was used to inform earlier reasoning]"
                if len(result) > MAX_TOOL_RESULT_CHARS_IN_HISTORY
                else result
            )
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": stored_result})
    emit("error", {"message": "Retry tool loop exhausted budget; specialist will abstain."})
    return "", messages
