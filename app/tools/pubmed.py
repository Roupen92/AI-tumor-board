"""PubMed search + fetch via NCBI Entrez (Biopython)."""
import os
import asyncio
import logging
from Bio import Entrez

log = logging.getLogger(__name__)

Entrez.email = os.getenv("NCBI_EMAIL", "tumor-board@example.com")
_NCBI_API_KEY = os.getenv("NCBI_API_KEY")
if _NCBI_API_KEY:
    Entrez.api_key = _NCBI_API_KEY


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
