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
        "of PMIDs with titles, journals, years, and article types. Prefer recent "
        "guidelines, systematic reviews, and large RCTs. The query is automatically "
        "biased toward your specialty's MeSH terms."
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
        },
        "required": ["query"],
    },
}


FETCH_SCHEMA = {
    "name": "pubmed_fetch",
    "description": (
        "Fetch the abstract (and journal metadata) for a list of PMIDs. The abstracts "
        "are added to the evidence ledger and assigned [E#] labels you must use when "
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


def _entrez_search_sync(query: str, retmax: int) -> list[str]:
    handle = Entrez.esearch(db="pubmed", term=query, retmax=retmax, sort="relevance")
    rec = Entrez.read(handle)
    handle.close()
    return list(rec.get("IdList", []))


def _entrez_summary_sync(pmids: list[str]) -> list[dict]:
    if not pmids:
        return []
    handle = Entrez.esummary(db="pubmed", id=",".join(pmids))
    docs = Entrez.read(handle)
    handle.close()
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
    """Return {pmid: {title, journal, year, abstract}}."""
    if not pmids:
        return {}
    handle = Entrez.efetch(
        db="pubmed", id=",".join(pmids), rettype="abstract", retmode="xml"
    )
    rec = Entrez.read(handle)
    handle.close()
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
            out[pmid] = {
                "title": title,
                "journal": journal,
                "year": year,
                "abstract": "\n".join(abstract_parts).strip(),
            }
        except Exception as e:  # pragma: no cover - tolerate malformed records
            log.warning("efetch parse error: %s", e)
            continue
    return out


async def run_search(args: dict, ctx) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "Error: empty query."
    max_results = int(args.get("max_results") or 8)
    max_results = max(1, min(max_results, 20))

    original = query
    biased = _apply_bias(query, ctx.pubmed_bias)

    pmids = await asyncio.to_thread(_entrez_search_sync, biased, max_results)
    if len(pmids) < 3 and ctx.pubmed_bias:
        # Specialty bias starved the query; fall back to unbiased.
        fallback = await asyncio.to_thread(_entrez_search_sync, original, max_results)
        # Merge, preserving biased order first.
        seen = set(pmids)
        for p in fallback:
            if p not in seen:
                pmids.append(p)
                seen.add(p)

    if not pmids:
        return f"No PubMed hits for query: {original}"

    summaries = await asyncio.to_thread(_entrez_summary_sync, pmids)

    lines = [f"PubMed search results for: {original}"]
    if biased != original:
        lines.append(f"(specialty bias applied: {biased})")
    lines.append("")
    for s in summaries:
        types = ", ".join(s["article_types"][:3]) if s["article_types"] else ""
        lines.append(
            f"- PMID {s['pmid']} ({s['year']}) — {s['title']}\n"
            f"  Journal: {s['journal']}\n"
            f"  Types: {types}"
        )
    lines.append("")
    lines.append("Call `pubmed_fetch` with the most relevant 2-4 PMIDs to read their abstracts.")
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
        entry = ctx.ledger.add(
            source_kind="pubmed",
            source_id=pmid,
            title=rec["title"],
            journal=rec["journal"],
            year=rec["year"],
            url=url,
            summary=rec["abstract"][:1200],
            full_text_available=False,
            cited_by=ctx.specialist_id,
        )
        abstract = rec["abstract"] or "(no abstract available)"
        lines.append(
            f"[{entry.label}] PMID {pmid} — {rec['title']}\n"
            f"  Journal: {rec['journal']} ({rec['year']})\n"
            f"  URL: {url}\n"
            f"  Abstract:\n  {abstract}\n"
        )
    lines.append(
        "Use the [E#] labels above when citing these articles in your draft."
    )
    return "\n".join(lines)
