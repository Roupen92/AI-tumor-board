"""Semantic Scholar Graph API — broader academic coverage + citation graph."""
import datetime
import logging
import httpx

log = logging.getLogger(__name__)

_API = "https://api.semanticscholar.org/graph/v1/paper/search"


def _default_min_year() -> int:
    return datetime.date.today().year - 10


SCHEMA = {
    "name": "semantic_scholar_search",
    "description": (
        "Search Semantic Scholar's academic corpus (broader than PubMed; includes "
        "non-biomedical literature and citation counts). By default restricts to "
        "the last 10 years. Useful for finding highly-cited or recent papers across "
        "all academia. Returns title, abstract, authors, year, venue, citation count."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Free-text query."},
            "max_results": {
                "type": "integer",
                "default": 5,
                "description": "Number of papers to return (default 5, max 10).",
            },
            "min_year": {
                "type": "integer",
                "description": (
                    "OPTIONAL recency filter. Only return papers published in this year "
                    "or later. Omit (default) to search all years — important for rare "
                    "cancers and landmark papers whose seminal work is older."
                ),
            },
        },
        "required": ["query"],
    },
}

_FIELDS = "title,abstract,authors,year,venue,citationCount,externalIds,url"


async def _fetch(client: httpx.AsyncClient, query: str, limit: int, year_filter: str | None) -> tuple[int, dict | str]:
    """Return (status_code, payload). On 200 payload is parsed json dict; otherwise it's a short message."""
    params = {"query": query, "limit": limit, "fields": _FIELDS}
    if year_filter:
        params["year"] = year_filter
    r = await client.get(_API, params=params)
    if r.status_code != 200:
        return r.status_code, ""
    return 200, r.json()


async def run(args: dict, ctx) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "Error: empty query."
    limit = max(1, min(int(args.get("max_results") or 5), 10))
    # Opt-in recency: only filter when the agent explicitly passes min_year.
    min_year_arg = args.get("min_year")
    min_year = int(min_year_arg) if min_year_arg else None
    year_filter = f"{min_year}-" if min_year else None

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            status, data = await _fetch(client, query, limit, year_filter)
            if status == 429:
                return "Semantic Scholar rate-limited this request. Try again or fall back to PubMed."
            if status != 200:
                log.warning("Semantic Scholar HTTP %s for %r", status, query[:80])
                return (
                    f"Semantic Scholar query failed: API returned {status}. "
                    "Try a different query or another tool."
                )[:200]
            # Fallback: if the year filter starved results, retry without it.
            papers_first = (data.get("data") if isinstance(data, dict) else None) or []
            if len(papers_first) < 3 and year_filter:
                status, data2 = await _fetch(client, query, limit, None)
                if status == 200:
                    data = data2
    except httpx.HTTPStatusError as e:
        log.warning("Semantic Scholar HTTP %s for %r: %s", e.response.status_code, query[:80], e)
        return (
            f"Semantic Scholar query failed: API returned {e.response.status_code}. "
            "Try a different query or another tool."
        )[:200]
    except httpx.RequestError as e:
        log.warning("Semantic Scholar request error for %r: %s", query[:80], e)
        return "Semantic Scholar query failed: network error or timeout. Try a different query or another tool."[:200]
    except httpx.HTTPError as e:
        log.warning("Semantic Scholar HTTP error for %r: %s", query[:80], e)
        return "Semantic Scholar query failed: HTTP error. Try a different query or another tool."[:200]
    except ValueError as e:
        log.warning("Semantic Scholar JSON decode error for %r: %s", query[:80], e)
        return "Semantic Scholar query failed: malformed response. Try a different query or another tool."[:200]

    try:
        papers = data.get("data") or []
    except (KeyError, TypeError, AttributeError) as e:
        log.warning("Semantic Scholar unexpected response shape for %r: %s", query[:80], e)
        return "Semantic Scholar query failed: unexpected response shape. Try a different query or another tool."[:200]
    if not papers:
        return f"No Semantic Scholar results for: {query}"

    filter_note = f"{min_year}-present" if min_year else "all years"
    lines = [
        f"Semantic Scholar results for: {query}",
        f"(filter: {filter_note})",
        "",
    ]
    for p in papers:
        ext = p.get("externalIds") or {}
        pmid = ext.get("PubMed") or ""
        doi = ext.get("DOI") or ""
        ss_id = p.get("paperId") or ""
        title = p.get("title") or ""
        year = str(p.get("year") or "")
        venue = p.get("venue") or ""
        cites = p.get("citationCount") or 0
        abstract = p.get("abstract") or ""
        url = p.get("url") or (f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else f"https://www.semanticscholar.org/paper/{ss_id}")

        if pmid:
            kind, sid = "pubmed", pmid
        elif doi:
            kind, sid = "doi", doi
        else:
            kind, sid = "semantic_scholar", ss_id

        entry = ctx.ledger.add(
            source_kind=kind,
            source_id=sid,
            title=title,
            journal=venue,
            year=year,
            url=url,
            summary=abstract[:1200] if abstract else "",
            retrieved_by=ctx.specialist_id,
        )
        lines.append(
            f"[{entry.label}] {title} ({year}) — {cites} citations\n"
            f"  Venue: {venue}\n"
            f"  URL: {url}\n"
            f"  Abstract: {(abstract[:600] + '…') if len(abstract) > 600 else (abstract or '(no abstract)')}\n"
        )
    return "\n".join(lines)
