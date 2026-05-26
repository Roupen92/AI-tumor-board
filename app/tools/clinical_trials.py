"""ClinicalTrials.gov v2 API — query completed trials with results."""
import logging
import httpx

log = logging.getLogger(__name__)

_API = "https://clinicaltrials.gov/api/v2/studies"

SCHEMA = {
    "name": "clinical_trials_search",
    "description": (
        "Search ClinicalTrials.gov for studies relevant to the case. Returns NCT IDs, "
        "title, phase, status, conditions, interventions, and primary outcome summary. "
        "Useful when standard-of-care evidence is limited and a trial may be appropriate."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Free-text query (e.g., 'esophageal adenocarcinoma neoadjuvant chemoradiation').",
            },
            "max_results": {
                "type": "integer",
                "description": "Number of trials to return (default 5, max 10).",
                "default": 5,
            },
            "status": {
                "type": "string",
                "description": "Optional status filter: RECRUITING, COMPLETED, ACTIVE_NOT_RECRUITING.",
            },
        },
        "required": ["query"],
    },
}


async def run(args: dict, ctx) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "Error: empty query."
    max_results = max(1, min(int(args.get("max_results") or 5), 10))
    status = (args.get("status") or "").strip()

    params = {
        "query.cond": query,
        "pageSize": max_results,
        "format": "json",
    }
    if status:
        params["filter.overallStatus"] = status.upper()

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(_API, params=params)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        log.warning("ClinicalTrials HTTP %s for %r: %s", e.response.status_code, query[:80], e)
        return (
            f"ClinicalTrials query failed: API returned {e.response.status_code}. "
            "Try a different query or another tool."
        )[:200]
    except httpx.RequestError as e:
        log.warning("ClinicalTrials request error for %r: %s", query[:80], e)
        return "ClinicalTrials query failed: network error or timeout. Try a different query or another tool."[:200]
    except httpx.HTTPError as e:
        log.warning("ClinicalTrials HTTP error for %r: %s", query[:80], e)
        return "ClinicalTrials query failed: HTTP error. Try a different query or another tool."[:200]
    except ValueError as e:
        log.warning("ClinicalTrials JSON decode error for %r: %s", query[:80], e)
        return "ClinicalTrials query failed: malformed response. Try a different query or another tool."[:200]

    try:
        studies = data.get("studies", []) or []
    except (KeyError, TypeError, AttributeError) as e:
        log.warning("ClinicalTrials unexpected response shape for %r: %s", query[:80], e)
        return "ClinicalTrials query failed: unexpected response shape. Try a different query or another tool."[:200]
    if not studies:
        return f"No ClinicalTrials.gov results for: {query}"

    lines = [f"ClinicalTrials.gov results for: {query}", ""]
    for s in studies:
        ident = s.get("protocolSection", {}).get("identificationModule", {}) or {}
        status_mod = s.get("protocolSection", {}).get("statusModule", {}) or {}
        design = s.get("protocolSection", {}).get("designModule", {}) or {}
        cond_mod = s.get("protocolSection", {}).get("conditionsModule", {}) or {}
        arms_mod = s.get("protocolSection", {}).get("armsInterventionsModule", {}) or {}
        outcomes_mod = s.get("protocolSection", {}).get("outcomesModule", {}) or {}

        nct = ident.get("nctId") or ""
        title = ident.get("briefTitle") or ""
        phases = ", ".join(design.get("phases") or [])
        overall_status = status_mod.get("overallStatus") or ""
        conditions = ", ".join(cond_mod.get("conditions") or [])
        interventions = ", ".join(
            i.get("name", "") for i in (arms_mod.get("interventions") or [])
        )
        primary = outcomes_mod.get("primaryOutcomes") or []
        primary_str = "; ".join(p.get("measure", "") for p in primary[:2])
        url = f"https://clinicaltrials.gov/study/{nct}" if nct else ""

        entry = ctx.ledger.add(
            source_kind="clinical_trial",
            source_id=nct,
            title=title,
            year=str(status_mod.get("startDateStruct", {}).get("date", ""))[:4],
            url=url,
            summary=f"{title}. Phase: {phases}. Status: {overall_status}. "
            f"Conditions: {conditions}. Interventions: {interventions}. "
            f"Primary outcome: {primary_str}",
            retrieved_by=ctx.specialist_id,
        )

        lines.append(
            f"[{entry.label}] {nct} — {title}\n"
            f"  Phase: {phases or '(unspecified)'}\n"
            f"  Status: {overall_status}\n"
            f"  Conditions: {conditions}\n"
            f"  Interventions: {interventions}\n"
            f"  Primary outcome: {primary_str}\n"
            f"  URL: {url}\n"
        )
    return "\n".join(lines)
