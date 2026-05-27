"""ClinicalTrials.gov v2 tools for the Clinical Trial Matcher specialist.

Two LLM-facing tools plus an internal best-effort geocoder:
  - clinical_trial_match_search : recent + recruiting/upcoming trials, optionally
    restricted to trials with a site at/near a patient location.
  - clinical_trial_details      : full eligibility criteria + sites + dates for one NCT.

Location matching uses ClinicalTrials.gov's own `query.locn` place-name filter
(no API key, reliable). When a radius is requested we first try to geocode the
location and apply a precise `filter.geo` distance filter; if geocoding is
unavailable (public Nominatim refuses most server traffic) we fall back to the
place-name match and say so in the result.
"""
import asyncio
import gzip
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

_API = "https://clinicaltrials.gov/api/v2/studies"
_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_USER_AGENT = "AI-Tumor-Board/1.0 (oncology clinical-trial matching)"

_DEFAULT_STATUS = "RECRUITING|NOT_YET_RECRUITING|AVAILABLE"
_NCT_RE = re.compile(r"^NCT\d{8}$", re.IGNORECASE)

# Whole-module field projection — enough for a candidate snapshot AND full detail.
_FIELDS = (
    "IdentificationModule,StatusModule,DesignModule,ConditionsModule,"
    "ArmsInterventionsModule,EligibilityModule,ContactsLocationsModule"
)

_OPEN_SITE_STATUSES = {"RECRUITING", "NOT_YET_RECRUITING", "AVAILABLE"}


SCHEMA = {
    "name": "clinical_trial_match_search",
    "description": (
        "Find the MOST RECENT clinical trials that are recruiting now or about to open, "
        "for a condition and optional biomarker/intervention. Results are sorted newest-"
        "updated first and include NCT ID, title, phase, recruiting status, an eligibility "
        "snapshot (age/sex + a short criteria excerpt), recruiting-site cities, and start/"
        "last-updated dates. Optionally restrict to trials with a site at/near a patient "
        "location. Use this FIRST, then call clinical_trial_details on the best NCT IDs to "
        "read the full inclusion/exclusion criteria before judging eligibility."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "condition": {
                "type": "string",
                "description": "Disease/condition, e.g. 'non-small cell lung cancer'.",
            },
            "biomarker_or_term": {
                "type": "string",
                "description": (
                    "Optional full-text term, ideal for biomarkers/mutations, "
                    "e.g. 'EGFR L858R' or 'HER2 amplification'."
                ),
            },
            "intervention": {
                "type": "string",
                "description": "Optional drug/intervention name to focus on, e.g. 'osimertinib'.",
            },
            "status": {
                "type": "string",
                "description": (
                    "Pipe-separated overall-status filter. Default "
                    "'RECRUITING|NOT_YET_RECRUITING|AVAILABLE'. Use 'RECRUITING' alone "
                    "to require open enrollment right now."
                ),
            },
            "near_location": {
                "type": "string",
                "description": (
                    "Optional patient location (city, state, and/or country) to require a "
                    "trial site at/near, e.g. 'Boston, Massachusetts'."
                ),
            },
            "radius_miles": {
                "type": "integer",
                "description": (
                    "Optional radius in miles around near_location for a precise distance "
                    "filter (best-effort; falls back to place-name match). Default 100."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "Number of trials to return (default 5, max 10).",
                "default": 5,
            },
        },
        "required": ["condition"],
    },
}

DETAILS_SCHEMA = {
    "name": "clinical_trial_details",
    "description": (
        "Fetch the FULL eligibility criteria (inclusion/exclusion text), recruiting "
        "status, recruiting-site locations, and start/completion dates for ONE trial by "
        "NCT ID. Prefer clinical_trial_details_batch to fetch several at once in a single call."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "nct_id": {
                "type": "string",
                "description": "ClinicalTrials.gov identifier, e.g. 'NCT04890613'.",
            },
        },
        "required": ["nct_id"],
    },
}

DETAILS_BATCH_SCHEMA = {
    "name": "clinical_trial_details_batch",
    "description": (
        "Fetch the FULL eligibility criteria + sites + dates for SEVERAL trials at once "
        "(one call, fetched in parallel). Use this right after clinical_trial_match_search "
        "on your top 2-3 candidate NCT IDs, then screen and rank them in a single pass. "
        "You may NOT recommend a trial whose criteria you have not retrieved here (or via "
        "clinical_trial_details)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "nct_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "2-4 ClinicalTrials.gov identifiers, e.g. ['NCT04181060','NCT05498428'].",
            },
        },
        "required": ["nct_ids"],
    },
}


def _http_get_json_sync(url: str, params: dict, timeout: float = 15.0):
    """Blocking GET → parsed JSON, via stdlib urllib.

    ClinicalTrials.gov's edge/WAF blocks httpx's transport fingerprint (httpx 403s
    even with browser-like headers, while urllib and curl get 200), so all
    ClinicalTrials.gov traffic goes through urllib. Wrapped by callers in
    asyncio.to_thread to stay non-blocking.
    """
    full = url + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(
        full, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return json.loads(raw)


async def _geocode(location: str) -> tuple[float, float] | None:
    """Best-effort geocode of a place name to (lat, lon) via OSM Nominatim.

    Returns None on any failure — public Nominatim refuses much automated traffic,
    so callers MUST degrade gracefully to place-name (query.locn) filtering.
    """
    try:
        data = await asyncio.to_thread(
            _http_get_json_sync, _NOMINATIM, {"q": location, "format": "json", "limit": 1}, 8.0
        )
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except (urllib.error.URLError, ValueError, KeyError, IndexError, TypeError, OSError) as e:
        log.info("Geocode of %r unavailable: %s", (location or "")[:60], e)
    return None


async def _fetch(url: str, params: dict, what: str) -> tuple[dict | None, str]:
    """GET JSON with the house error ladder. Returns (data, '') or (None, error_msg)."""
    try:
        data = await asyncio.to_thread(_http_get_json_sync, url, params)
        return data, ""
    except urllib.error.HTTPError as e:
        log.warning("ClinicalTrials HTTP %s for %r: %s", e.code, what[:80], e)
        if e.code == 404:
            return None, f"No ClinicalTrials.gov record found ({what})."
        return None, (
            f"ClinicalTrials query failed: API returned {e.code}. "
            "Try a different query or another tool."
        )[:200]
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        log.warning("ClinicalTrials request error for %r: %s", what[:80], e)
        return None, "ClinicalTrials query failed: network error or timeout. Try again or another tool."[:200]
    except (ValueError, json.JSONDecodeError) as e:
        log.warning("ClinicalTrials JSON decode error for %r: %s", what[:80], e)
        return None, "ClinicalTrials query failed: malformed response. Try again or another tool."[:200]


def _parse_common(ps: dict) -> dict:
    """Flatten the protocolSection modules we care about into a plain dict."""
    ident = ps.get("identificationModule", {}) or {}
    status_mod = ps.get("statusModule", {}) or {}
    design = ps.get("designModule", {}) or {}
    cond_mod = ps.get("conditionsModule", {}) or {}
    arms_mod = ps.get("armsInterventionsModule", {}) or {}
    locs_mod = ps.get("contactsLocationsModule", {}) or {}
    nct = ident.get("nctId") or ""
    return {
        "nct": nct,
        "title": ident.get("briefTitle") or "",
        "phases": ", ".join(design.get("phases") or []),
        "status": status_mod.get("overallStatus") or "",
        "start": status_mod.get("startDateStruct", {}) or {},
        "last_update": (status_mod.get("lastUpdatePostDateStruct", {}) or {}).get("date", ""),
        "primary_completion": (status_mod.get("primaryCompletionDateStruct", {}) or {}).get("date", ""),
        "conditions": ", ".join(cond_mod.get("conditions") or []),
        "interventions": ", ".join(i.get("name", "") for i in (arms_mod.get("interventions") or [])),
        "elig": ps.get("eligibilityModule", {}) or {},
        "locations": locs_mod.get("locations") or [],
        "url": f"https://clinicaltrials.gov/study/{nct}" if nct else "",
    }


def _age_range(elig: dict) -> str:
    lo = elig.get("minimumAge") or "no min"
    hi = elig.get("maximumAge") or "no max"
    return f"{lo} to {hi}"


def _fmt_start(start: dict) -> str:
    date = start.get("date", "")
    if not date:
        return "unspecified"
    typ = start.get("type", "")
    return f"{date} ({typ})" if typ else date


def _site_summary(locations: list[dict], limit: int = 6) -> str:
    """One-line summary preferring open (recruiting) sites."""
    if not locations:
        return "no sites listed"
    open_sites = [l for l in locations if (l.get("status") or "").upper() in _OPEN_SITE_STATUSES]
    pool = open_sites or locations
    labels: list[str] = []
    for l in pool:
        lab = ", ".join(p for p in (l.get("city"), l.get("state"), l.get("country")) if p)
        if lab and lab not in labels:
            labels.append(lab)
        if len(labels) >= limit:
            break
    head = "; ".join(labels)
    return f"{len(locations)} site(s) total; e.g. {head}" if head else f"{len(locations)} site(s)"


def _ledger_summary(info: dict, criteria: str = "") -> str:
    base = (
        f"{info['title']}. Phase: {info['phases']}. Status: {info['status']}. "
        f"Conditions: {info['conditions']}. Interventions: {info['interventions']}. "
        f"Start: {_fmt_start(info['start'])}."
    )
    if criteria:
        base += f" Eligibility: {criteria[:900]}"
    return base


async def run(args: dict, ctx) -> str:
    condition = (args.get("condition") or "").strip()
    if not condition:
        return "Error: 'condition' is required."
    term = (args.get("biomarker_or_term") or "").strip()
    intervention = (args.get("intervention") or "").strip()
    status = (args.get("status") or _DEFAULT_STATUS).strip()
    near = (args.get("near_location") or "").strip()
    try:
        radius = int(args.get("radius_miles") or 100)
    except (TypeError, ValueError):
        radius = 100
    radius = max(1, min(radius, 500))
    max_results = max(1, min(int(args.get("max_results") or 5), 10))

    params = {
        "query.cond": condition,
        "filter.overallStatus": status.upper(),
        "sort": "LastUpdatePostDate:desc",
        "fields": _FIELDS,
        "pageSize": max_results,
        "countTotal": "true",
        "format": "json",
    }
    if term:
        params["query.term"] = term
    if intervention:
        params["query.intr"] = intervention

    geo_note = ""
    if near:
        coords = await _geocode(near)
        if coords:
            params["filter.geo"] = f"distance({coords[0]},{coords[1]},{radius}mi)"
            geo_note = f", within ~{radius} mi of {near}"
        else:
            params["query.locn"] = near
            geo_note = (
                f", with a site matching '{near}' "
                "(place-name match; precise radius unavailable in this environment)"
            )

    data, err = await _fetch(_API, params, f"search {condition!r}")
    if err:
        return err
    studies = (data or {}).get("studies", []) or []
    total = (data or {}).get("totalCount")
    if not studies:
        return f"No ClinicalTrials.gov results for: {condition}{geo_note}."

    header = f"ClinicalTrials.gov matches for: {condition}"
    if term:
        header += f" + '{term}'"
    header += geo_note
    if total is not None:
        header += f"  (showing {len(studies)} of {total} matching trials, newest-updated first)"
    lines = [header, ""]

    for s in studies:
        info = _parse_common(s.get("protocolSection", {}) or {})
        if not info["nct"]:
            continue
        elig = info["elig"]
        crit = (elig.get("eligibilityCriteria") or "").strip()
        crit_snip = (crit[:700] + "…") if len(crit) > 700 else crit
        entry = ctx.ledger.add(
            source_kind="clinical_trial",
            source_id=info["nct"],
            title=info["title"],
            year=str(info["start"].get("date", ""))[:4],
            url=info["url"],
            summary=_ledger_summary(info),
            retrieved_by=ctx.specialist_id,
        )
        lines.append(
            f"[{entry.label}] {info['nct']} — {info['title']}\n"
            f"  Phase: {info['phases'] or '(unspecified)'} | Status: {info['status']}\n"
            f"  Eligibility snapshot: age {_age_range(elig)}, sex {elig.get('sex', 'ALL')}\n"
            f"  Criteria excerpt: {crit_snip or '(not in summary — fetch full details)'}\n"
            f"  Sites: {_site_summary(info['locations'])}\n"
            f"  Start: {_fmt_start(info['start'])} | Last updated: {info['last_update'] or 'n/a'}\n"
            f"  URL: {info['url']}\n"
        )
    lines.append(
        "Next: call clinical_trial_details_batch with your top 2-3 NCT IDs to read the full "
        "inclusion/exclusion criteria, then screen and rank them in one pass. Do not "
        "recommend a trial whose full criteria you have not retrieved."
    )
    return "\n".join(lines)


async def _one_detail(nct: str, ctx) -> str:
    """Fetch + register + format the full detail for a single (already-validated) NCT id."""
    data, err = await _fetch(f"{_API}/{nct}", {"fields": _FIELDS, "format": "json"}, nct)
    if err:
        return err
    ps = (data or {}).get("protocolSection", {}) or {}
    if not ps:
        return f"No detail available for {nct}."

    info = _parse_common(ps)
    elig = info["elig"]
    criteria = (elig.get("eligibilityCriteria") or "").strip() or "(no eligibility criteria text published)"

    open_sites = [l for l in info["locations"] if (l.get("status") or "").upper() in _OPEN_SITE_STATUSES]
    site_lines = []
    for l in (open_sites or info["locations"])[:12]:
        lab = ", ".join(p for p in (l.get("city"), l.get("state"), l.get("country")) if p)
        if lab:
            site_lines.append(f"    - {lab} [{l.get('status', '?')}]")
    sites_block = "\n".join(site_lines) if site_lines else "    (no sites listed)"

    entry = ctx.ledger.add(
        source_kind="clinical_trial",
        source_id=nct,
        title=info["title"],
        year=str(info["start"].get("date", ""))[:4],
        url=info["url"],
        summary=_ledger_summary(info, criteria),
        retrieved_by=ctx.specialist_id,
    )

    return (
        f"[{entry.label}] {nct} — {info['title']}\n"
        f"Phase: {info['phases'] or '(unspecified)'} | Overall status: {info['status']}\n"
        f"Start: {_fmt_start(info['start'])} | Primary completion: {info['primary_completion'] or 'n/a'}\n"
        f"Eligibility — age {_age_range(elig)}, sex {elig.get('sex', 'ALL')}, "
        f"healthy volunteers: {elig.get('healthyVolunteers', 'N/A')}, "
        f"age groups: {', '.join(elig.get('stdAges') or []) or 'n/a'}\n\n"
        f"FULL ELIGIBILITY CRITERIA (match the patient against these exact criteria; "
        f"cite as [{entry.label}]):\n{criteria}\n\n"
        f"Recruiting sites ({len(info['locations'])} total listed):\n{sites_block}\n\n"
        f"URL: {info['url']}"
    )


async def run_details(args: dict, ctx) -> str:
    nct = (args.get("nct_id") or "").strip().upper()
    if not _NCT_RE.match(nct):
        return (
            f"Error: '{args.get('nct_id')}' is not a valid NCT id "
            "(expected like NCT01234567)."
        )
    return await _one_detail(nct, ctx)


async def run_details_batch(args: dict, ctx) -> str:
    raw = args.get("nct_ids") or []
    if not isinstance(raw, list) or not raw:
        return "Error: nct_ids must be a non-empty list of NCT identifiers."
    ncts: list[str] = []
    bad: list[str] = []
    for x in raw:
        n = str(x or "").strip().upper()
        if _NCT_RE.match(n):
            if n not in ncts:
                ncts.append(n)
        else:
            bad.append(str(x))
    ncts = ncts[:4]  # bound the batch so the combined criteria stay manageable
    if not ncts:
        return f"Error: no valid NCT ids in {raw}."
    # Fetch all in parallel; each block carries its own [N] label + full criteria.
    results = await asyncio.gather(*[_one_detail(n, ctx) for n in ncts])
    out = ("\n\n" + "=" * 60 + "\n\n").join(results)
    if bad:
        out += f"\n\n(Ignored invalid ids: {', '.join(bad)})"
    return out
