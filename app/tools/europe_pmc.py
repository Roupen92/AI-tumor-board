"""Europe PMC REST API — broader than PubMed (adds preprints, EU pubs, agricultural)."""
import logging
import httpx

log = logging.getLogger(__name__)

_API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

SCHEMA = {
    "name": "europe_pmc_search",
    "description": (
        "Search Europe PMC for biomedical articles, including PubMed records plus "
        "preprints (bioRxiv, medRxiv), European publications, and agricultural/life "
        "sciences sources. Use this when a regular PubMed search returns thin results "
        "or when you want preprint-level recency. Returns title, journal, year, "
        "PMID (if PubMed-sourced), and a stable URL. Abstracts are added to the "
        "evidence ledger."
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
        },
        "required": ["query"],
    },
}


async def run(args: dict, ctx) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "Error: empty query."
    limit = max(1, min(int(args.get("max_results") or 6), 15))

    params = {
        "query": query,
        "format": "json",
        "pageSize": limit,
        "resultType": "core",   # includes abstract
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(_API, params=params)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        log.warning("Europe PMC HTTP %s for %r: %s", e.response.status_code, query[:80], e)
        return (
            f"Europe PMC query failed: API returned {e.response.status_code}. "
            "Try a different query or another tool."
        )[:200]
    except httpx.RequestError as e:
        log.warning("Europe PMC request error for %r: %s", query[:80], e)
        return "Europe PMC query failed: network error or timeout. Try a different query or another tool."[:200]
    except httpx.HTTPError as e:
        log.warning("Europe PMC HTTP error for %r: %s", query[:80], e)
        return "Europe PMC query failed: HTTP error. Try a different query or another tool."[:200]
    except ValueError as e:
        log.warning("Europe PMC JSON decode error for %r: %s", query[:80], e)
        return "Europe PMC query failed: malformed response. Try a different query or another tool."[:200]

    try:
        results = (data.get("resultList") or {}).get("result") or []
    except (KeyError, TypeError, AttributeError) as e:
        log.warning("Europe PMC unexpected response shape for %r: %s", query[:80], e)
        return "Europe PMC query failed: unexpected response shape. Try a different query or another tool."[:200]
    if not results:
        return f"No Europe PMC results for: {query}"

    lines = [f"Europe PMC results for: {query}", ""]
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
            cited_by=ctx.specialist_id,
        )
        types_tag = "PREPRINT" if source == "PPR" else source
        lines.append(
            f"[{entry.label}] {types_tag} {ext_id} ({year}) — {title}\n"
            f"  Journal: {journal}\n"
            f"  URL: {url}\n"
            f"  Abstract: {(abstract[:600] + '…') if len(abstract) > 600 else (abstract or '(no abstract)')}\n"
        )
    return "\n".join(lines)
