"""Per-session evidence ledger. Dedupes by source key, assigns [E1], [E2], ... labels."""
import re
import threading
from dataclasses import dataclass, field, asdict


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    # PubMed (and others) embed <sup>, <i>, <b>, etc. in titles and abstracts.
    # We render these fields as plain text in the UI, so strip on the way in.
    if not s:
        return s
    return _HTML_TAG_RE.sub("", s)


@dataclass
class EvidenceEntry:
    label: str                    # "1", "2", ... (plain journal-style numbering)
    source_kind: str              # "pubmed" | "clinical_trial" | "fda" | "rxnorm"
    source_id: str                # PMID, NCT id, FDA app number, RxCUI pair, ...
    title: str = ""
    journal: str = ""
    year: str = ""
    url: str = ""
    summary: str = ""             # short excerpt for the LLM (abstract or key facts)
    full_text_available: bool = False
    article_type: str = ""        # "RCT" | "Meta-analysis" | "Systematic review" | "Guideline" | "Review" | "Clinical trial" | "Observational" | "Case report" | "Other"
    article_type_raw: list[str] = field(default_factory=list)  # raw PublicationType strings
    retrieved_by: set[str] = field(default_factory=set)  # specialist_ids that fetched it via a tool
    cited_by: set[str] = field(default_factory=set)      # specialist_ids that actually cited it [N] in their draft

    def public(self) -> dict:
        d = asdict(self)
        d["retrieved_by"] = sorted(self.retrieved_by)
        d["cited_by"] = sorted(self.cited_by)
        return d


class EvidenceLedger:
    """Thread-safe in-memory ledger. One per board session."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_key: dict[tuple[str, str], EvidenceEntry] = {}
        self._order: list[str] = []   # insertion order of labels

    def _key(self, kind: str, sid: str) -> tuple[str, str]:
        return (kind, str(sid).strip())

    def add(
        self,
        *,
        source_kind: str,
        source_id: str,
        title: str = "",
        journal: str = "",
        year: str = "",
        url: str = "",
        summary: str = "",
        full_text_available: bool = False,
        article_type: str = "",
        article_type_raw: list[str] | None = None,
        retrieved_by: str | None = None,
    ) -> EvidenceEntry:
        title = _strip_html(title)
        summary = _strip_html(summary)
        journal = _strip_html(journal)
        with self._lock:
            key = self._key(source_kind, source_id)
            entry = self._by_key.get(key)
            if entry is None:
                label = f"{len(self._order) + 1}"
                entry = EvidenceEntry(
                    label=label,
                    source_kind=source_kind,
                    source_id=str(source_id).strip(),
                    title=title,
                    journal=journal,
                    year=year,
                    url=url,
                    summary=summary,
                    full_text_available=full_text_available,
                    article_type=article_type,
                    article_type_raw=list(article_type_raw or []),
                )
                self._by_key[key] = entry
                self._order.append(label)
            else:
                # Upgrade existing entry if richer data arrived
                if title and not entry.title:
                    entry.title = title
                if journal and not entry.journal:
                    entry.journal = journal
                if year and not entry.year:
                    entry.year = year
                if url and not entry.url:
                    entry.url = url
                if summary and len(summary) > len(entry.summary):
                    entry.summary = summary
                if full_text_available:
                    entry.full_text_available = True
                if article_type and not entry.article_type:
                    entry.article_type = article_type
                if article_type_raw and not entry.article_type_raw:
                    entry.article_type_raw = list(article_type_raw)
            if retrieved_by:
                entry.retrieved_by.add(retrieved_by)
            return entry

    def mark_cited(self, label: str, specialist_id: str) -> None:
        # Called from specialist.py AFTER a draft's [N] citations have been
        # verified against the ledger. Only entries marked here appear in the
        # final references panel — retrieved-but-uncited noise is hidden.
        with self._lock:
            for entry in self._by_key.values():
                if entry.label == label:
                    entry.cited_by.add(specialist_id)
                    return

    def get_by_label(self, label: str) -> EvidenceEntry | None:
        with self._lock:
            for entry in self._by_key.values():
                if entry.label == label:
                    return entry
            return None

    def all(self) -> list[EvidenceEntry]:
        with self._lock:
            return [
                next(e for e in self._by_key.values() if e.label == lbl)
                for lbl in self._order
            ]

    def count_for(self, specialist_id: str) -> int:
        # "Did this specialist retrieve anything?" — used by the retrieve-or-abstain gate.
        # Intentionally counts retrievals, not citations: an agent that searched but
        # didn't yet cite is not the same as one that never searched.
        with self._lock:
            return sum(1 for e in self._by_key.values() if specialist_id in e.retrieved_by)

    def public_list(self) -> list[dict]:
        # Only entries that some specialist actually cited make it into the UI's
        # references panel. Retrieved-but-uncited hits remain in the ledger for
        # the LLM's tool-result context but are hidden from the final report.
        return [e.public() for e in self.all() if e.cited_by]
