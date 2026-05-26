"""Europe PMC REST API — broader than PubMed (adds preprints, EU pubs, agricultural)."""
import datetime
import logging
import httpx

log = logging.getLogger(__name__)

_API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


def _default_min_year() -> int:
    return datetime.date.today().year - 10


SCHEMA = {
    "name": "europe_pmc_search",
    "description": (
        "Search Europe PMC for biomedical articles, including PubMed records plus "
        "preprints (bioRxiv, medRxiv), European publications, and agricultural/life "
        "sciences sources. By default the search prioritizes recent papers "
        "(last 10 years) sorted newest first. Use this when a regular PubMed "
        "search returns thin results or when you want preprint-level recency. "
        "Returns title, journal, year, PMID (if PubMed-sourced), and a stable URL."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Free-text query. Supports field tags like TITLE_ABS, AUTH, JOURNAL.",
            },
            "max_results": {
                "type": "integer",
                "default": 6,
                "description": "Number of articles to return (default 6, max 15).",
            },
            "min_year": {
                "type": "integer",
                "description": (
                    "OPTIONAL recency filter. Only return papers published in this year "
                    "or later. Omit (default) to search all years — important for rare "
                    "cancers and landmark trials whose seminal papers are older."
                ),
            },
        },
        "required": ["query"],
    },
}


async def _fetch(client: httpx.AsyncClient, query: str, limit: int) -> dict:
    params = {
        "query": query,
        "format": "json",
        "pageSize": limit,
        "resultType": "core",
        "sort": "FIRST_PDATE_D desc",   # newest first
    }
    r = await client.get(_API, params=params)
    r.raise_for_status()
    return r.json()


async def run(args: dict, ctx) -> str:
    original = (args.get("query") or "").strip()
    if not original:
        return "Error: empty query."
    limit = max(1, min(int(args.get("max_results") or 6), 15))
    # Opt-in recency: only filter when the agent explicitly passes min_year.
    min_year_arg = args.get("min_year")
    min_year = int(min_year_arg) if min_year_arg else None
    effective_query = (
        f"({original}) AND (PUB_YEAR:[{min_year} TO 3000])" if min_year else original
    )

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            data = await _fetch(client, effective_query, limit)
            # Fallback: if the year filter starved the result list, retry without it.
            results = (data.get("resultList") or {}).get("result") or []
            if len(results) < 3 and min_year:
                data = await _fetch(client, original, limit)
    except httpx.HTTPStatusError as e:
        log.warning("Europe PMC HTTP %s for %r: %s", e.response.status_code, original[:80], e)
        return (
            f"Europe PMC query failed: API returned {e.response.status_code}. "
            "Try a different query or another tool."
        )[:200]
    except httpx.RequestError as e:
        log.warning("Europe PMC request error for %r: %s", original[:80], e)
        return "Europe PMC query failed: network error or timeout. Try a different query or another tool."[:200]
    except httpx.HTTPError as e:
        log.warning("Europe PMC HTTP error for %r: %s", original[:80], e)
        return "Europe PMC query failed: HTTP error. Try a different query or another tool."[:200]
    except ValueError as e:
        log.warning("Europe PMC JSON decode error for %r: %s", original[:80], e)
        return "Europe PMC query failed: malformed response. Try a different query or another tool."[:200]

    try:
        results = (data.get("resultList") or {}).get("result") or []
    except (KeyError, TypeError, AttributeError) as e:
        log.warning("Europe PMC unexpected response shape for %r: %s", original[:80], e)
        return "Europe PMC query failed: unexpected response shape. Try a different query or another tool."[:200]
    if not results:
        return f"No Europe PMC results for: {original}"

    filter_note = f"{min_year}-present" if min_year else "all years"
    lines = [
        f"Europe PMC results for: {original}",
        f"(filter: {filter_note}, sorted newest first)",
        "",
    ]
    for art in results:
        source = art.get("source") or ""
        ext_id = art.get("id") or art.get("pmid") or ""
        pmid = art.get("pmid") or ""
        title = art.get("title") or ""
        journal = art.get("journalTitle") or ""
        year = str(art.get("pubYear") or "")
        abstract = art.get("abstractText") or ""

        # Build a stable URL
        if pmid:
            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
            source_kind = "pubmed"
            source_id = pmid
        elif source == "PPR":   # preprint
            url = f"https://europepmc.org/article/PPR/{ext_id}"
            source_kind = "europe_pmc_preprint"
            source_id = ext_id
        else:
            url = f"https://europepmc.org/article/{source}/{ext_id}"
            source_kind = "europe_pmc"
            source_id = f"{source}:{ext_id}" if source else ext_id

        entry = ctx.ledger.add(
            source_kind=source_kind,
            source_id=source_id,
            title=title,
            journal=journal,
            year=year,
            url=url,
            summary=abstract[:1200] if abstract else "",
            retrieved_by=ctx.specialist_id,
        )
        types_tag = "PREPRINT" if source == "PPR" else source
        lines.append(
            f"[{entry.label}] {types_tag} {ext_id} ({year}) — {title}\n"
            f"  Journal: {journal}\n"
            f"  URL: {url}\n"
            f"  Abstract: {(abstract[:600] + '…') if len(abstract) > 600 else (abstract or '(no abstract)')}\n"
        )
    return "\n".join(lines)
