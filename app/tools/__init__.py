"""Tool registry. Each tool exports SCHEMA + run(args, ctx).

The dispatcher is called by the specialist runner; it returns a string the LLM reads.
"""
from dataclasses import dataclass
from typing import Callable, Awaitable

from app.evidence import EvidenceLedger
from app.tools import (
    pubmed,
    clinical_trials,
    fda,
    rxnorm,
    europe_pmc,
    semantic_scholar,
    dailymed,
    oncokb,
    civic,
    brave_search,
)


@dataclass
class ToolContext:
    specialist_id: str
    pubmed_bias: dict | None
    ledger: EvidenceLedger


_REGISTRY: dict[str, tuple[dict, Callable[[dict, ToolContext], Awaitable[str]]]] = {
    "pubmed_search":           (pubmed.SEARCH_SCHEMA,            pubmed.run_search),
    "pubmed_fetch":            (pubmed.FETCH_SCHEMA,             pubmed.run_fetch),
    "clinical_trials_search":  (clinical_trials.SCHEMA,          clinical_trials.run),
    "fda_approvals_search":    (fda.SCHEMA,                      fda.run),
    "drug_interactions":       (rxnorm.SCHEMA,                   rxnorm.run),
    "europe_pmc_search":       (europe_pmc.SCHEMA,               europe_pmc.run),
    "semantic_scholar_search": (semantic_scholar.SCHEMA,         semantic_scholar.run),
    "dailymed_lookup":         (dailymed.SCHEMA,                 dailymed.run),
    "oncokb_query":            (oncokb.SCHEMA,                   oncokb.run),
    "civic_query":             (civic.SCHEMA,                    civic.run),
    "web_search":              (brave_search.SCHEMA,             brave_search.run),
}


def schemas_for(allowed: set[str]) -> list[dict]:
    """Return OpenAI tool-schema list for the allowed tool names."""
    return [
        {"type": "function", "function": _REGISTRY[name][0]}
        for name in allowed
        if name in _REGISTRY
    ]


async def dispatch(name: str, args: dict, ctx: ToolContext) -> str:
    if name not in _REGISTRY:
        return f"Tool '{name}' is not available to you."
    _, runner = _REGISTRY[name]
    try:
        return await runner(args, ctx)
    except Exception as e:  # tools should not crash the loop
        return f"Tool '{name}' failed: {type(e).__name__}: {e}"


def all_tool_names() -> list[str]:
    return list(_REGISTRY.keys())
