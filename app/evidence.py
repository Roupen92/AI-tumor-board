"""Per-session evidence ledger. Dedupes by source key, assigns [E1], [E2], ... labels."""
import threading
from dataclasses import dataclass, field, asdict


@dataclass
class EvidenceEntry:
    label: str                    # "E1", "E2", ...
    source_kind: str              # "pubmed" | "clinical_trial" | "fda" | "rxnorm"
    source_id: str                # PMID, NCT id, FDA app number, RxCUI pair, ...
    title: str = ""
    journal: str = ""
    year: str = ""
    url: str = ""
    summary: str = ""             # short excerpt for the LLM (abstract or key facts)
    full_text_available: bool = False
    cited_by: set[str] = field(default_factory=set)   # specialist_ids that cited it

    def public(self) -> dict:
        d = asdict(self)
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
        cited_by: str | None = None,
    ) -> EvidenceEntry:
        with self._lock:
            key = self._key(source_kind, source_id)
            entry = self._by_key.get(key)
            if entry is None:
                label = f"E{len(self._order) + 1}"
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
            if cited_by:
                entry.cited_by.add(cited_by)
            return entry

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
        with self._lock:
            return sum(1 for e in self._by_key.values() if specialist_id in e.cited_by)

    def public_list(self) -> list[dict]:
        return [e.public() for e in self.all()]
