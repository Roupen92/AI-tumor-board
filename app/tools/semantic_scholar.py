"""Semantic Scholar Graph API — broader academic coverage + citation graph."""
import httpx

_API = "https://api.semanticscholar.org/graph/v1/paper/search"

SCHEMA = {
    "name": "semantic_scholar_search",
    "description": (
        "Search Semantic Scholar's academic corpus (broader than PubMed; includes "
        "non-biomedical literature and citation counts). Useful for finding highly-cited "
        "or recent papers across all academia. Returns title, abstract, authors, year, "
        "venue, and citation count. Abstracts are added to the evidence ledger."
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
        },
        "required": ["query"],
    },
}

_FIELDS = "title,abstract,authors,year,venue,citationCount,externalIds,url"


async def run(args: dict, ctx) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "Error: empty query."
    limit = max(1, min(int(args.get("max_results") or 5), 10))

    params = {"query": query, "limit": limit, "fields": _FIELDS}
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(_API, params=params)
        if r.status_code == 429:
            return "Semantic Scholar rate-limited this request. Try again or fall back to PubMed."
        r.raise_for_status()
        data = r.json()

    papers = data.get("data") or []
    if not papers:
        return f"No Semantic Scholar results for: {query}"

    lines = [f"Semantic Scholar results for: {query}", ""]
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
            cited_by=ctx.specialist_id,
        )
        lines.append(
            f"[{entry.label}] {title} ({year}) — {cites} citations\n"
            f"  Venue: {venue}\n"
            f"  URL: {url}\n"
            f"  Abstract: {(abstract[:600] + '…') if len(abstract) > 600 else (abstract or '(no abstract)')}\n"
        )
    return "\n".join(lines)
