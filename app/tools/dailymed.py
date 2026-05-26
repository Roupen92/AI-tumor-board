"""DailyMed API — FDA structured product labels (full prescribing info)."""
import logging
import httpx
import re

log = logging.getLogger(__name__)

_API_SPLS = "https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json"
_API_SPL_DETAIL = "https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/{setid}.json"

SCHEMA = {
    "name": "dailymed_lookup",
    "description": (
        "Look up the FDA structured product label (SPL) for a drug on DailyMed. "
        "Returns label set IDs, manufacturer, and a brief section excerpt covering "
        "indications, dosage adjustments, warnings, and contraindications. Use this "
        "for authoritative prescribing info beyond what openFDA gives you."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "drug_name": {
                "type": "string",
                "description": "Brand or generic drug name (e.g., 'warfarin', 'paclitaxel').",
            },
            "max_results": {
                "type": "integer",
                "default": 2,
                "description": "Number of label records to return (max 3).",
            },
        },
        "required": ["drug_name"],
    },
}


def _strip_tags(html_or_xml: str) -> str:
    if not html_or_xml:
        return ""
    # Drop tags, collapse whitespace
    text = re.sub(r"<[^>]+>", " ", html_or_xml)
    return re.sub(r"\s+", " ", text).strip()


async def run(args: dict, ctx) -> str:
    name = (args.get("drug_name") or "").strip()
    if not name:
        return "Error: drug_name is required."
    limit = max(1, min(int(args.get("max_results") or 2), 3))

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(_API_SPLS, params={"drug_name": name, "pagesize": limit})
            if r.status_code != 200:
                log.warning("DailyMed HTTP %s for %r", r.status_code, name)
                return (
                    f"DailyMed query failed: API returned {r.status_code}. "
                    "Try a different query or another tool."
                )[:200]
            try:
                data = r.json()
                results = data.get("data") or []
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                log.warning("DailyMed response parse error for %r: %s", name, e)
                return "DailyMed query failed: malformed response. Try a different query or another tool."[:200]
            if not results:
                return f"No DailyMed labels found for: {name}"

            lines = [f"DailyMed labels for: {name}", ""]
            for rec in results[:limit]:
                setid = rec.get("setid") or ""
                title = rec.get("title") or ""
                published_date = rec.get("published_date") or ""
                url = f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={setid}"

                # Pull a short excerpt from the detail endpoint
                detail_excerpt = ""
                try:
                    d = await client.get(_API_SPL_DETAIL.format(setid=setid))
                    if d.status_code == 200:
                        dj = d.json().get("data", {})
                        # The /spls/{setid}.json endpoint returns metadata; full SPL XML
                        # is at /spls/{setid}.xml. Keep it lean for the LLM.
                        detail_excerpt = (
                            f"Effective time: {dj.get('effective_time', '')}. "
                            f"Marketing status: {dj.get('marketing_status', '')}."
                        )
                except Exception:
                    pass

                entry = ctx.ledger.add(
                    source_kind="dailymed",
                    source_id=setid,
                    title=title,
                    year=str(published_date)[:4],
                    url=url,
                    summary=_strip_tags(title + ". " + detail_excerpt)[:1200],
                    retrieved_by=ctx.specialist_id,
                )
                lines.append(
                    f"[{entry.label}] {title}\n"
                    f"  Set ID: {setid}\n"
                    f"  Published: {published_date}\n"
                    f"  URL: {url}\n"
                    f"  {detail_excerpt}\n"
                )
            return "\n".join(lines)
    except httpx.HTTPStatusError as e:
        log.warning("DailyMed HTTP %s for %r: %s", e.response.status_code, name, e)
        return (
            f"DailyMed query failed: API returned {e.response.status_code}. "
            "Try a different query or another tool."
        )[:200]
    except httpx.RequestError as e:
        log.warning("DailyMed request error for %r: %s", name, e)
        return "DailyMed query failed: network error or timeout. Try a different query or another tool."[:200]
    except httpx.HTTPError as e:
        log.warning("DailyMed HTTP error for %r: %s", name, e)
        return "DailyMed query failed: HTTP error. Try a different query or another tool."[:200]
