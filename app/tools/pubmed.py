"""PubMed search + fetch via NCBI Entrez (Biopython)."""
import os
import asyncio
import logging
import threading
import time
from Bio import Entrez

log = logging.getLogger(__name__)

Entrez.email = os.getenv("NCBI_EMAIL", "tumor-board@example.com")
_NCBI_API_KEY = os.getenv("NCBI_API_KEY")
if _NCBI_API_KEY:
    Entrez.api_key = _NCBI_API_KEY

# NCBI allows ~10 req/s with an API key (3/s without). With PARALLEL_SPECIALISTS=7
# all specialists fire Entrez calls in the same instant, bursting far past that and
# getting HTTP 429s. This shared limiter spaces every Entrez HTTP call across all
# specialist threads. We target well UNDER the documented cap (≈5/s with key, ≈2/s
# without): NCBI's enforcement is bursty and a temporary IP penalty can linger, and
# the extra spacing costs only ~1-2s across a whole run (tools are ~12% of wall time).
# It runs inside the to_thread workers, so a threading.Lock + monotonic clock fits.
_ENTREZ_MAX_PER_SEC = 5.0 if _NCBI_API_KEY else 2.0
_ENTREZ_MIN_INTERVAL = 1.0 / _ENTREZ_MAX_PER_SEC
_ENTREZ_LOCK = threading.Lock()
_entrez_last_call = 0.0


def _entrez_throttle() -> None:
    """Block until at least _ENTREZ_MIN_INTERVAL has passed since the last Entrez call,
    serializing the 7 specialists' bursts into a polite, 429-free stream."""
    global _entrez_last_call
    with _ENTREZ_LOCK:
        wait = _ENTREZ_MIN_INTERVAL - (time.monotonic() - _entrez_last_call)
        if wait > 0:
            time.sleep(wait)
        _entrez_last_call = time.monotonic()


SEARCH_SCHEMA = {
    "name": "pubmed_search",
    "description": (
        "Search PubMed for candidate articles relevant to the case. Returns a list "
        "of PMIDs with titles, journals, years, and a categorized article type "
        "(RCT / Meta-analysis / Systematic review / Guideline / Review / Clinical trial / "
        "Observational / Case report / Other). By default the search prioritizes "
        "recent papers (last 10 years) and high-strength evidence. The query is "
        "automatically biased toward your specialty's MeSH terms."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Free-text PubMed query. You can use MeSH terms, Boolean operators, "
                    "and field tags like [Title/Abstract]."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "Number of PMIDs to return (default 8, max 20).",
                "default": 8,
            },
            "min_year": {
                "type": "integer",
                "description": (
                    "OPTIONAL recency filter. Only return papers published in this year "
                    "or later. Omit (default) to search all years — important for rare "
                    "cancers and landmark trials whose seminal papers are older. "
                    "Add (e.g., 2020) only when you specifically want recent guidelines."
                ),
            },
            "sort": {
                "type": "string",
                "enum": ["date", "relevance"],
                "default": "date",
                "description": (
                    "Sort order. 'date' (default) returns newest first. Use 'relevance' "
                    "only when recency is not important."
                ),
            },
        },
        "required": ["query"],
    },
}


FETCH_SCHEMA = {
    "name": "pubmed_fetch",
    "description": (
        "Fetch the abstract (and journal metadata) for a list of PMIDs. The abstracts "
        "are added to the evidence ledger and assigned plain numbered labels (`[1]`, "
        "`[2]`, ...) you must use when "
        "citing the article in your draft."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pmids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of PubMed IDs (strings of digits). Max 5 per call.",
            }
        },
        "required": ["pmids"],
    },
}


def _apply_bias(query: str, bias: dict | None) -> str:
    if not bias:
        return query
    terms = bias.get("mesh_terms") or []
    if not terms:
        return query
    clause = " AND (" + " OR ".join(f'"{m}"[MeSH]' for m in terms) + ")"
    return f"({query}){clause}"


# Ordered from strongest to weakest. First match wins.
_TYPE_PRIORITY = [
    ("Practice Guideline", "Guideline"),
    ("Guideline", "Guideline"),
    ("Consensus Development Conference", "Guideline"),
    ("Meta-Analysis", "Meta-analysis"),
    ("Systematic Review", "Systematic review"),
    ("Randomized Controlled Trial", "RCT"),
    ("Controlled Clinical Trial", "Controlled trial"),
    ("Clinical Trial, Phase III", "Phase III trial"),
    ("Clinical Trial, Phase II", "Phase II trial"),
    ("Clinical Trial, Phase I", "Phase I trial"),
    ("Clinical Trial", "Clinical trial"),
    ("Multicenter Study", "Multicenter study"),
    ("Observational Study", "Observational"),
    ("Comparative Study", "Comparative study"),
    ("Cohort Studies", "Cohort study"),
    ("Case-Control Studies", "Case-control"),
    ("Review", "Review"),
    ("Case Reports", "Case report"),
    ("Editorial", "Editorial"),
    ("Letter", "Letter"),
]


def _categorize_article_types(raw_types: list[str]) -> str:
    """Pick the highest-strength category from a list of raw PublicationType strings."""
    if not raw_types:
        return "Other"
    raw_set = {t.strip() for t in raw_types if t and t.strip()}
    for keyword, category in _TYPE_PRIORITY:
        if any(keyword.lower() in t.lower() for t in raw_set):
            return category
    # Fall back to first non-generic entry
    for t in raw_types:
        if t and t.lower() not in ("journal article", "english abstract"):
            return t
    return "Other"


def _entrez_search_sync(query: str, retmax: int, sort: str = "pub+date") -> list[str]:
    try:
        _entrez_throttle()
        handle = Entrez.esearch(db="pubmed", term=query, retmax=retmax, sort=sort)
        rec = Entrez.read(handle)
        handle.close()
        return list(rec.get("IdList", []))
    except Exception as e:
        log.warning("Entrez esearch failed for %r: %s", query[:80], e)
        return []


def _entrez_summary_sync(pmids: list[str]) -> list[dict]:
    if not pmids:
        return []
    try:
        _entrez_throttle()
        handle = Entrez.esummary(db="pubmed", id=",".join(pmids))
        docs = Entrez.read(handle)
        handle.close()
    except Exception as e:
        log.warning("Entrez esummary failed for %d pmids: %s", len(pmids), e)
        return []
    out = []
    for d in docs:
        out.append(
            {
                "pmid": str(d.get("Id", "")),
                "title": str(d.get("Title", "")),
                "journal": str(d.get("FullJournalName") or d.get("Source", "")),
                "year": str(d.get("PubDate", ""))[:4],
                "article_types": [str(t) for t in d.get("PubTypeList", [])],
            }
        )
    return out


def _entrez_efetch_abstract_sync(pmids: list[str]) -> dict[str, dict]:
    """Return {pmid: {title, journal, year, abstract, article_types}}."""
    if not pmids:
        return {}
    try:
        _entrez_throttle()
        handle = Entrez.efetch(
            db="pubmed", id=",".join(pmids), rettype="abstract", retmode="xml"
        )
        rec = Entrez.read(handle)
        handle.close()
    except Exception as e:
        log.warning("Entrez efetch failed for %d pmids: %s", len(pmids), e)
        return {}
    out: dict[str, dict] = {}
    for art in rec.get("PubmedArticle", []):
        try:
            citation = art["MedlineCitation"]
            pmid = str(citation["PMID"])
            article = citation["Article"]
            title = str(article.get("ArticleTitle", ""))
            journal = str(article.get("Journal", {}).get("Title", ""))
            year = ""
            issue = article.get("Journal", {}).get("JournalIssue", {})
            pub_date = issue.get("PubDate", {}) if isinstance(issue, dict) else {}
            if isinstance(pub_date, dict):
                year = str(pub_date.get("Year") or pub_date.get("MedlineDate", ""))[:4]
            abstract_parts = []
            abstract = article.get("Abstract", {})
            if isinstance(abstract, dict):
                for piece in abstract.get("AbstractText", []) or []:
                    label = ""
                    if hasattr(piece, "attributes"):
                        label = piece.attributes.get("Label", "")
                    text = str(piece)
                    abstract_parts.append(f"{label}: {text}" if label else text)
            article_types = [
                str(t) for t in (article.get("PublicationTypeList") or [])
            ]
            out[pmid] = {
                "title": title,
                "journal": journal,
                "year": year,
                "abstract": "\n".join(abstract_parts).strip(),
                "article_types": article_types,
            }
        except Exception as e:  # pragma: no cover - tolerate malformed records
            log.warning("efetch parse error: %s", e)
            continue
    return out


def _default_min_year() -> int:
    import datetime
    return datetime.date.today().year - 10


# Category strength order (strongest first) used to rank candidates in the fused tool.
_CATEGORY_ORDER = [
    "Guideline", "Meta-analysis", "Systematic review", "RCT", "Phase III trial",
    "Controlled trial", "Multicenter study", "Phase II trial", "Phase I trial",
    "Clinical trial", "Cohort study", "Observational", "Comparative study",
    "Case-control", "Review", "Case report", "Editorial", "Letter", "Other",
]


def _strength_rank(article_types: list[str]) -> int:
    cat = _categorize_article_types(article_types)
    return _CATEGORY_ORDER.index(cat) if cat in _CATEGORY_ORDER else len(_CATEGORY_ORDER)


async def _search_pmids(query: str, ctx, max_results: int, min_year: int | None, sort_arg: str) -> list[str]:
    """Run the biased esearch with the same fallbacks as pubmed_search."""
    original = query
    if min_year:
        effective = f'({query}) AND ("{min_year}"[Date - Publication] : "3000"[Date - Publication])'
    else:
        effective = query
    biased = _apply_bias(effective, ctx.pubmed_bias)
    pmids = await asyncio.to_thread(_entrez_search_sync, biased, max_results, sort_arg)
    if len(pmids) < 3 and ctx.pubmed_bias:
        for p in await asyncio.to_thread(_entrez_search_sync, effective, max_results, sort_arg):
            if p not in pmids:
                pmids.append(p)
    if len(pmids) < 3 and min_year:
        for p in await asyncio.to_thread(_entrez_search_sync, original, max_results, sort_arg):
            if p not in pmids:
                pmids.append(p)
    return pmids


async def run_search(args: dict, ctx) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "Error: empty query."
    max_results = int(args.get("max_results") or 8)
    max_results = max(1, min(max_results, 20))
    # Recency filter is OPT-IN: pass min_year only when the agent explicitly asks.
    # Defaulting to "last 10 years" was too aggressive for rare cancers (verrucous
    # carcinoma, etc.) where landmark series predate that window.
    min_year_arg = args.get("min_year")
    min_year = int(min_year_arg) if min_year_arg else None
    sort_mode = (args.get("sort") or "date").lower()
    sort_arg = "pub+date" if sort_mode == "date" else "relevance"

    original = query
    if min_year:
        effective_query = f'({query}) AND ("{min_year}"[Date - Publication] : "3000"[Date - Publication])'
    else:
        effective_query = query
    biased = _apply_bias(effective_query, ctx.pubmed_bias)

    pmids = await asyncio.to_thread(_entrez_search_sync, biased, max_results, sort_arg)
    if len(pmids) < 3 and ctx.pubmed_bias:
        # Specialty bias starved the query; retry without bias.
        fallback = await asyncio.to_thread(_entrez_search_sync, effective_query, max_results, sort_arg)
        seen = set(pmids)
        for p in fallback:
            if p not in seen:
                pmids.append(p)
                seen.add(p)
    if len(pmids) < 3 and min_year:
        # Date filter starved too; drop year cap as a last resort.
        fallback2 = await asyncio.to_thread(_entrez_search_sync, original, max_results, sort_arg)
        seen = set(pmids)
        for p in fallback2:
            if p not in seen:
                pmids.append(p)
                seen.add(p)

    if not pmids:
        return f"No PubMed hits for query: {original}"

    summaries = await asyncio.to_thread(_entrez_summary_sync, pmids)

    filter_note = f"{min_year}-present" if min_year else "all years"
    lines = [f"PubMed search results for: {original}"]
    lines.append(f"(filter: {filter_note}, sorted by {sort_mode}; specialty MeSH bias applied where applicable)")
    lines.append("")
    for s in summaries:
        category = _categorize_article_types(s["article_types"])
        raw_types = ", ".join(s["article_types"][:3]) if s["article_types"] else ""
        lines.append(
            f"- PMID {s['pmid']} ({s['year']}) [{category}] — {s['title']}\n"
            f"  Journal: {s['journal']}\n"
            f"  Raw types: {raw_types}"
        )
    lines.append("")
    lines.append(
        "Call `pubmed_fetch` with the most relevant 2-4 PMIDs. PREFER guidelines / "
        "meta-analyses / systematic reviews / RCTs over reviews and case reports, "
        "and PREFER the most recent papers unless an older one is a seminal landmark."
    )
    return "\n".join(lines)


async def run_fetch(args: dict, ctx) -> str:
    pmids = args.get("pmids") or []
    if not isinstance(pmids, list) or not pmids:
        return "Error: pmids must be a non-empty list of PubMed ID strings."
    pmids = [str(p).strip() for p in pmids if str(p).strip()][:5]
    if not pmids:
        return "Error: no valid PMIDs provided."

    records = await asyncio.to_thread(_entrez_efetch_abstract_sync, pmids)
    if not records:
        return f"No abstracts retrieved for PMIDs: {pmids}"

    lines = []
    for pmid in pmids:
        rec = records.get(pmid)
        if not rec:
            lines.append(f"PMID {pmid}: not found.")
            continue
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        category = _categorize_article_types(rec.get("article_types", []))
        entry = ctx.ledger.add(
            source_kind="pubmed",
            source_id=pmid,
            title=rec["title"],
            journal=rec["journal"],
            year=rec["year"],
            url=url,
            summary=rec["abstract"][:1200],
            full_text_available=False,
            article_type=category,
            article_type_raw=rec.get("article_types", []),
            retrieved_by=ctx.specialist_id,
        )
        abstract = rec["abstract"] or "(no abstract available)"
        lines.append(
            f"[{entry.label}] PMID {pmid} ({rec['year']}) [{category}] — {rec['title']}\n"
            f"  Journal: {rec['journal']}\n"
            f"  URL: {url}\n"
            f"  Abstract:\n  {abstract}\n"
        )
    lines.append(
        "Use the plain numbered labels (e.g., `[1]`, `[2]`) above when citing these "
        "articles in your draft. "
        "The [Type] tag indicates evidence strength — prefer RCT / Meta-analysis / "
        "Systematic review / Guideline citations over reviews and case reports."
    )
    return "\n".join(lines)


SEARCH_AND_FETCH_SCHEMA = {
    "name": "pubmed_search_and_fetch",
    "description": (
        "PREFERRED one-shot literature retrieval: searches PubMed, ranks candidates by "
        "evidence strength (Guideline > Meta-analysis > Systematic review > RCT > ...) "
        "and recency, fetches the abstracts of the best few, registers them in the "
        "evidence ledger, and returns citation-ready `[N]` labels — all in ONE call. "
        "Use this instead of separate pubmed_search + pubmed_fetch for normal evidence "
        "gathering. (Fall back to pubmed_search/pubmed_fetch only when you need to scan a "
        "long candidate list or fetch specific PMIDs by hand.)"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Free-text PubMed query (MeSH terms, Booleans, field tags allowed).",
            },
            "max_results": {
                "type": "integer",
                "description": "How many top-ranked abstracts to fetch and return (default 3, max 5).",
                "default": 3,
            },
            "min_year": {
                "type": "integer",
                "description": (
                    "OPTIONAL recency filter (year or later). Omit to search all years — "
                    "important for landmark/older seminal trials."
                ),
            },
            "sort": {
                "type": "string",
                "enum": ["date", "relevance"],
                "default": "relevance",
                "description": "Initial PubMed sort before strength-ranking. Default 'relevance'.",
            },
        },
        "required": ["query"],
    },
}


async def run_search_and_fetch(args: dict, ctx) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "Error: empty query."
    top_k = max(1, min(int(args.get("max_results") or 3), 5))
    min_year_arg = args.get("min_year")
    min_year = int(min_year_arg) if min_year_arg else None
    sort_mode = (args.get("sort") or "relevance").lower()
    sort_arg = "pub+date" if sort_mode == "date" else "relevance"

    # Search a wider candidate pool, then rank by evidence strength + recency.
    pool = max(top_k * 4, 12)
    pmids = await _search_pmids(query, ctx, pool, min_year, sort_arg)
    if not pmids:
        return f"No PubMed hits for query: {query}"

    summaries = await asyncio.to_thread(_entrez_summary_sync, pmids)
    if not summaries:
        # Couldn't rank; fetch the first few raw.
        ranked_pmids = pmids[:top_k]
    else:
        def _year_int(s):
            try:
                return int(s.get("year") or 0)
            except ValueError:
                return 0
        summaries.sort(key=lambda s: (_strength_rank(s["article_types"]), -_year_int(s)))
        ranked_pmids = [s["pmid"] for s in summaries[:top_k]]

    records = await asyncio.to_thread(_entrez_efetch_abstract_sync, ranked_pmids)
    if not records:
        return f"No abstracts retrieved for top PMIDs: {ranked_pmids}"

    lines = [f"Top {len(ranked_pmids)} evidence items for: {query}", ""]
    for pmid in ranked_pmids:
        rec = records.get(pmid)
        if not rec:
            continue
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        category = _categorize_article_types(rec.get("article_types", []))
        entry = ctx.ledger.add(
            source_kind="pubmed",
            source_id=pmid,
            title=rec["title"],
            journal=rec["journal"],
            year=rec["year"],
            url=url,
            summary=rec["abstract"][:1200],
            full_text_available=False,
            article_type=category,
            article_type_raw=rec.get("article_types", []),
            retrieved_by=ctx.specialist_id,
        )
        abstract = rec["abstract"] or "(no abstract available)"
        lines.append(
            f"[{entry.label}] PMID {pmid} ({rec['year']}) [{category}] — {rec['title']}\n"
            f"  Journal: {rec['journal']}\n"
            f"  URL: {url}\n"
            f"  Abstract:\n  {abstract}\n"
        )
    lines.append(
        "These are the strongest-available, citation-ready sources (ranked by evidence "
        "type then recency). Cite them with their `[N]` labels. Only run another retrieval "
        "if a required recommendation still cannot be supported."
    )
    return "\n".join(lines)
