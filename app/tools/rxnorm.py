"""RxNorm drug-drug interactions via NLM REST API."""
import asyncio
import httpx

_RXCUI = "https://rxnav.nlm.nih.gov/REST/rxcui.json"
_INTERACT = "https://rxnav.nlm.nih.gov/REST/interaction/list.json"

SCHEMA = {
    "name": "drug_interactions",
    "description": (
        "Check pairwise drug-drug interactions for a list of medications. The pharmacist "
        "should typically call this early when the case lists multiple medications. Returns "
        "any flagged interactions with severity and a short description."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "drug_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of 2-10 drug names (brand or generic). The tool will resolve "
                    "each name to an RxCUI and check pairwise interactions."
                ),
            }
        },
        "required": ["drug_names"],
    },
}


async def _resolve_rxcui(client: httpx.AsyncClient, name: str) -> str | None:
    r = await client.get(_RXCUI, params={"name": name})
    if r.status_code != 200:
        return None
    data = r.json()
    ids = (data.get("idGroup") or {}).get("rxnormId") or []
    return ids[0] if ids else None


async def _interactions(client: httpx.AsyncClient, rxcuis: list[str]) -> dict:
    r = await client.get(_INTERACT, params={"rxcuis": "+".join(rxcuis)})
    if r.status_code != 200:
        return {}
    return r.json()


async def run(args: dict, ctx) -> str:
    names = args.get("drug_names") or []
    if not isinstance(names, list) or len(names) < 2:
        return "Error: drug_names must be a list of at least 2 drug names."
    names = [n.strip() for n in names if isinstance(n, str) and n.strip()][:10]
    if len(names) < 2:
        return "Error: need at least 2 valid drug names."

    async with httpx.AsyncClient(timeout=15.0) as client:
        rxcui_tasks = [_resolve_rxcui(client, n) for n in names]
        rxcuis = await asyncio.gather(*rxcui_tasks)

        unresolved = [n for n, cui in zip(names, rxcuis) if not cui]
        resolved_pairs = [(n, cui) for n, cui in zip(names, rxcuis) if cui]
        if len(resolved_pairs) < 2:
            return (
                "Could not resolve enough drug names to RxCUI. "
                f"Unresolved: {unresolved}"
            )

        data = await _interactions(client, [cui for _, cui in resolved_pairs])

    lines = ["Drug-interaction check for: " + ", ".join(names)]
    if unresolved:
        lines.append(f"(Unresolved names ignored: {unresolved})")
    lines.append("")

    groups = (data.get("fullInteractionTypeGroup") or [])
    interactions_found = 0
    for group in groups:
        for fit in group.get("fullInteractionType") or []:
            for inter in fit.get("interactionPair") or []:
                interactions_found += 1
                drug_a = (inter.get("interactionConcept") or [{}])[0]
                drug_b = (inter.get("interactionConcept") or [{}, {}])[1] if len(inter.get("interactionConcept") or []) > 1 else {}
                a_name = drug_a.get("minConceptItem", {}).get("name", "")
                b_name = drug_b.get("minConceptItem", {}).get("name", "")
                severity = inter.get("severity") or "(unspecified)"
                desc = inter.get("description") or ""
                pair_key = f"{sorted([a_name, b_name])}"
                entry = ctx.ledger.add(
                    source_kind="rxnorm",
                    source_id=pair_key,
                    title=f"Drug interaction: {a_name} + {b_name}",
                    url="",
                    summary=f"Severity: {severity}. {desc}",
                    cited_by=ctx.specialist_id,
                )
                lines.append(
                    f"[{entry.label}] {a_name} + {b_name} — severity: {severity}\n"
                    f"  {desc}\n"
                )

    if interactions_found == 0:
        lines.append("No interactions flagged by the RxNorm interaction API.")
        lines.append(
            "(Note: the public RxNorm DDI service was deprecated in 2024 for some pairs. "
            "Absence of a hit is not proof of safety — apply clinical judgment.)"
        )

    return "\n".join(lines)
