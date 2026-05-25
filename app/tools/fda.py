"""openFDA Drug Approvals API."""
import logging
import httpx

log = logging.getLogger(__name__)

_API = "https://api.fda.gov/drug/drugsfda.json"

SCHEMA = {
    "name": "fda_approvals_search",
    "description": (
        "Search the openFDA drug-approvals database for an FDA-approved drug by brand "
        "or generic name. Returns approval date, indication, sponsor, application "
        "number, and submission type."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "drug_name": {
                "type": "string",
                "description": "Brand or generic drug name (e.g., 'pembrolizumab', 'Keytruda').",
            },
            "max_results": {
                "type": "integer",
                "default": 3,
                "description": "Number of approval records to return (max 5).",
            },
        },
        "required": ["drug_name"],
    },
}


async def _query(client: httpx.AsyncClient, search: str, limit: int) -> dict:
    r = await client.get(_API, params={"search": search, "limit": limit})
    if r.status_code == 404:
        return {"results": []}
    r.raise_for_status()
    return r.json()


async def run(args: dict, ctx) -> str:
    name = (args.get("drug_name") or "").strip()
    if not name:
        return "Error: drug_name is required."
    limit = max(1, min(int(args.get("max_results") or 3), 5))

    # openFDA: try brand_name, then generic_name, then active_ingredient.
    queries = [
        f'openfda.brand_name:"{name}"',
        f'openfda.generic_name:"{name}"',
        f'products.active_ingredients.name:"{name}"',
    ]

    results: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for q in queries:
                data = await _query(client, q, limit)
                results = data.get("results") or []
                if results:
                    break
    except httpx.HTTPStatusError as e:
        log.warning("openFDA HTTP %s for %r: %s", e.response.status_code, name, e)
        return (
            f"FDA query failed: API returned {e.response.status_code}. "
            "Try a different query or another tool."
        )[:200]
    except httpx.RequestError as e:
        log.warning("openFDA request error for %r: %s", name, e)
        return "FDA query failed: network error or timeout. Try a different query or another tool."[:200]
    except httpx.HTTPError as e:
        log.warning("openFDA HTTP error for %r: %s", name, e)
        return "FDA query failed: HTTP error. Try a different query or another tool."[:200]
    except ValueError as e:
        log.warning("openFDA JSON decode error for %r: %s", name, e)
        return "FDA query failed: malformed response. Try a different query or another tool."[:200]
    except (KeyError, TypeError, AttributeError) as e:
        log.warning("openFDA unexpected response shape for %r: %s", name, e)
        return "FDA query failed: unexpected response shape. Try a different query or another tool."[:200]

    if not results:
        return f"No FDA approval records found for: {name}"

    lines = [f"FDA approval records for: {name}", ""]
    for rec in results[:limit]:
        app_no = rec.get("application_number") or ""
        sponsor = rec.get("sponsor_name") or ""
        openfda = rec.get("openfda") or {}
        brand = ", ".join(openfda.get("brand_name") or [])
        generic = ", ".join(openfda.get("generic_name") or [])
        submissions = rec.get("submissions") or []
        first_approval = ""
        for s in submissions:
            if s.get("submission_status") == "AP" and s.get("submission_type") == "ORIG":
                first_approval = s.get("submission_status_date") or ""
                break
        products = rec.get("products") or []
        indications = []
        for p in products:
            d = p.get("dosage_form") or ""
            r_ = p.get("route") or ""
            indications.append(f"{d} / {r_}")
        ind_str = "; ".join(indications[:3])

        entry = ctx.ledger.add(
            source_kind="fda",
            source_id=app_no,
            title=f"FDA approval: {brand or generic or name} ({app_no})",
            year=first_approval[:4] if first_approval else "",
            url=f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={app_no}"
            if app_no
            else "",
            summary=f"Sponsor: {sponsor}. Brand: {brand}. Generic: {generic}. "
            f"First approval: {first_approval}. Dosage forms: {ind_str}",
            cited_by=ctx.specialist_id,
        )

        lines.append(
            f"[{entry.label}] Application {app_no}\n"
            f"  Sponsor: {sponsor}\n"
            f"  Brand: {brand}\n"
            f"  Generic: {generic}\n"
            f"  First approval (AP/ORIG): {first_approval or '(unknown)'}\n"
            f"  Dosage forms/routes: {ind_str}\n"
        )
    return "\n".join(lines)
