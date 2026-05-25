"""CIViC (Clinical Interpretation of Variants in Cancer) — public GraphQL API."""
import logging
import httpx

log = logging.getLogger(__name__)

_API = "https://civicdb.org/api/graphql"

SCHEMA = {
    "name": "civic_query",
    "description": (
        "Query CIViC for evidence on a specific cancer variant (gene + variant). "
        "Returns evidence items linking the variant to predictive, prognostic, or "
        "diagnostic implications, including the supporting therapy/drug and the "
        "evidence level/rating. Free public source — no key required."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "gene": {
                "type": "string",
                "description": "Gene symbol (e.g., 'BRAF', 'EGFR').",
            },
            "variant": {
                "type": "string",
                "description": "Variant name as CIViC stores it (e.g., 'V600E', 'L858R').",
            },
        },
        "required": ["gene", "variant"],
    },
}

_QUERY = """
query VariantEvidence($name: String!, $gene: String!) {
  variants(name: $name, entrezSymbol: $gene, first: 5) {
    nodes {
      id
      name
      gene { name }
      evidenceItems(first: 8) {
        nodes {
          id
          evidenceType
          evidenceLevel
          evidenceRating
          significance
          description
          therapies { name }
          disease { name }
          source { citation sourceUrl }
        }
      }
    }
  }
}
"""


async def run(args: dict, ctx) -> str:
    gene = (args.get("gene") or "").strip().upper()
    variant = (args.get("variant") or "").strip()
    if not gene or not variant:
        return "Error: gene and variant are required."

    payload = {"query": _QUERY, "variables": {"name": variant, "gene": gene}}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(_API, json=payload)
            if r.status_code != 200:
                log.warning("CIViC HTTP %s for %s %s", r.status_code, gene, variant)
                return (
                    f"CIViC query failed: API returned {r.status_code}. "
                    "Try a different query or another tool."
                )[:200]
            data = r.json()
    except httpx.HTTPStatusError as e:
        log.warning("CIViC HTTP %s for %s %s: %s", e.response.status_code, gene, variant, e)
        return (
            f"CIViC query failed: API returned {e.response.status_code}. "
            "Try a different query or another tool."
        )[:200]
    except httpx.RequestError as e:
        log.warning("CIViC request error for %s %s: %s", gene, variant, e)
        return "CIViC query failed: network error or timeout. Try a different query or another tool."[:200]
    except httpx.HTTPError as e:
        log.warning("CIViC HTTP error for %s %s: %s", gene, variant, e)
        return "CIViC query failed: HTTP error. Try a different query or another tool."[:200]
    except ValueError as e:
        log.warning("CIViC JSON decode error for %s %s: %s", gene, variant, e)
        return "CIViC query failed: malformed response. Try a different query or another tool."[:200]

    try:
        if data.get("errors"):
            log.warning("CIViC GraphQL errors for %s %s: %s", gene, variant, data.get("errors"))
            return "CIViC query failed: GraphQL error. Try a different query or another tool."[:200]
        variants = ((data.get("data") or {}).get("variants") or {}).get("nodes") or []
    except (KeyError, TypeError, AttributeError) as e:
        log.warning("CIViC unexpected response shape for %s %s: %s", gene, variant, e)
        return "CIViC query failed: unexpected response shape. Try a different query or another tool."[:200]
    if not variants:
        return f"No CIViC variant matched {gene} {variant}."

    lines = [f"CIViC evidence for {gene} {variant}:", ""]
    for v in variants[:3]:
        v_name = v.get("name") or variant
        v_id = v.get("id")
        url = f"https://civicdb.org/variants/{v_id}"

        items = (v.get("evidenceItems") or {}).get("nodes") or []
        for it in items[:6]:
            ev_type = it.get("evidenceType") or ""
            ev_level = it.get("evidenceLevel") or ""
            rating = it.get("evidenceRating") or ""
            sig = it.get("significance") or ""
            desc = it.get("description") or ""
            therapies = ", ".join(t.get("name", "") for t in (it.get("therapies") or []))
            disease = (it.get("disease") or {}).get("name") or ""
            src = it.get("source") or {}
            citation = src.get("citation") or ""
            src_url = src.get("sourceUrl") or url

            entry = ctx.ledger.add(
                source_kind="civic",
                source_id=f"civic:{it.get('id')}",
                title=f"CIViC {ev_type} evidence: {gene} {v_name} → {therapies or disease}",
                url=src_url,
                summary=(f"{sig}. {desc} Therapies: {therapies}. Disease: {disease}. "
                         f"Level {ev_level}, rating {rating}. Source: {citation}")[:1200],
                cited_by=ctx.specialist_id,
            )
            lines.append(
                f"[{entry.label}] {ev_type} — {sig}\n"
                f"  Therapies: {therapies or '(none)'}\n"
                f"  Disease: {disease or '(none)'}\n"
                f"  Level: {ev_level}, Rating: {rating}\n"
                f"  Source: {citation}\n"
                f"  URL: {src_url}\n"
            )
    if len(lines) <= 2:
        return f"CIViC has the variant {gene} {variant} but no evidence items returned."
    return "\n".join(lines)
