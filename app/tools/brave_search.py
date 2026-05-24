"""Brave Web Search API — general-web fallback when curated medical sources come up empty."""
import os
import httpx

_API = "https://api.search.brave.com/res/v1/web/search"

SCHEMA = {
    "name": "web_search",
    "description": (
        "General web search via Brave. Use ONLY when curated medical sources "
        "(PubMed, Europe PMC, Semantic Scholar, ClinicalTrials.gov, FDA, DailyMed, "
        "OncoKB, CIViC) come up empty for what you need — e.g., a brand-new guideline "
        "release, society statement, or news event not yet indexed in PubMed. "
        "Returns title, URL, and a short snippet per hit. Web results are LESS "
        "authoritative than peer-reviewed sources; prefer them only when nothing better exists."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Web search query."},
            "max_results": {
                "type": "integer",
                "default": 5,
                "description": "Number of web hits to return (default 5, max 10).",
            },
        },
        "required": ["query"],
    },
}


async def run(args: dict, ctx) -> str:
    # The .env in this project uses `Brave_API`; support both common names.
    api_key = os.getenv("Brave_API") or os.getenv("BRAVE_API_KEY") or os.getenv("BRAVE_SEARCH_API_KEY")
    if not api_key:
        return (
            "Brave Search requires an API key. Set Brave_API in .env "
            "(free tier at https://brave.com/search/api/)."
        )

    query = (args.get("query") or "").strip()
    if not query:
        return "Error: empty query."
    count = max(1, min(int(args.get("max_results") or 5), 10))

    headers = {
        "X-Subscription-Token": api_key,
        "Accept": "application/json",
    }
    params = {"q": query, "count": count, "result_filter": "web"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(_API, params=params, headers=headers)
        if r.status_code == 401:
            return "Brave rejected the API key. Check Brave_API in .env."
        if r.status_code == 429:
            return "Brave rate-limited this request. Try again in a moment."
        if r.status_code != 200:
            return f"Brave search failed (HTTP {r.status_code}): {r.text[:200]}"
        data = r.json()

    web_results = ((data.get("web") or {}).get("results") or [])
    if not web_results:
        return f"No Brave web results for: {query}"

    lines = [f"Brave web results for: {query}", ""]
    for hit in web_results[:count]:
        title = hit.get("title") or ""
        url = hit.get("url") or ""
        snippet = hit.get("description") or ""
        page_age = hit.get("page_age") or ""

        entry = ctx.ledger.add(
            source_kind="web",
            source_id=url,
            title=title,
            year=str(page_age)[:4],
            url=url,
            summary=snippet[:1200],
            cited_by=ctx.specialist_id,
        )
        lines.append(
            f"[{entry.label}] {title}\n"
            f"  URL: {url}\n"
            f"  Snippet: {snippet[:300]}\n"
        )
    lines.append(
        "(Web results are less authoritative than peer-reviewed sources. "
        "Prefer PubMed/Europe PMC/ClinicalTrials for clinical claims when possible.)"
    )
    return "\n".join(lines)
