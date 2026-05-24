"""OncoKB API — curated precision-oncology knowledge (mutation → therapy mapping)."""
import os
import httpx

_API_ANNOTATE = "https://www.oncokb.org/api/v1/annotate/mutations/byProteinChange"

SCHEMA = {
    "name": "oncokb_query",
    "description": (
        "Query OncoKB for a specific mutation in a gene. Returns oncogenicity "
        "(likely/oncogenic/inconclusive), mutation effect, the highest level of "
        "FDA-approved or guideline-recommended therapy targeting that mutation, "
        "and a clinical summary. Requires an OncoKB API key. Use this when the "
        "case provides a specific genetic alteration (e.g., BRAF V600E, EGFR L858R)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "hugo_symbol": {
                "type": "string",
                "description": "HUGO gene symbol (e.g., 'BRAF', 'EGFR', 'KRAS').",
            },
            "alteration": {
                "type": "string",
                "description": "Protein change (e.g., 'V600E', 'L858R', 'G12C').",
            },
            "tumor_type": {
                "type": "string",
                "description": "Optional OncoTree tumor type code (e.g., 'COAD' for colon adenocarcinoma).",
            },
        },
        "required": ["hugo_symbol", "alteration"],
    },
}


async def run(args: dict, ctx) -> str:
    api_key = os.getenv("ONCOKB_API_KEY")
    if not api_key:
        return (
            "OncoKB requires an API key. Set ONCOKB_API_KEY in .env "
            "(free for academic use — request at https://www.oncokb.org/apiAccess). "
            "Falling back to other sources is recommended for now."
        )

    gene = (args.get("hugo_symbol") or "").strip().upper()
    alt = (args.get("alteration") or "").strip()
    tumor = (args.get("tumor_type") or "").strip().upper()
    if not gene or not alt:
        return "Error: hugo_symbol and alteration are required."

    params = {"hugoSymbol": gene, "alteration": alt}
    if tumor:
        params["tumorType"] = tumor
    headers = {"Authorization": f"Bearer {api_key}"}

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(_API_ANNOTATE, params=params, headers=headers)
        if r.status_code == 401:
            return "OncoKB rejected the API key. Check ONCOKB_API_KEY in .env."
        if r.status_code != 200:
            return f"OncoKB query failed (HTTP {r.status_code}) for {gene} {alt}"
        data = r.json()

    oncogenic = data.get("oncogenic") or "Unknown"
    mut_effect = (data.get("mutationEffect") or {}).get("knownEffect") or "Unknown"
    summary = data.get("variantSummary") or ""
    tumor_summary = data.get("tumorTypeSummary") or ""
    highest_level = data.get("highestSensitiveLevel") or "(none)"
    treatments = data.get("treatments") or []

    rx_lines = []
    for t in treatments[:5]:
        drugs = ", ".join(d.get("drugName", "") for d in (t.get("drugs") or []))
        level = t.get("level") or ""
        ind = t.get("indication") or ""
        rx_lines.append(f"  - {drugs} (level {level}, {ind})")
    if not rx_lines:
        rx_lines = ["  (no level 1-3 sensitizing therapies in OncoKB for this alteration/context)"]

    url = f"https://www.oncokb.org/gene/{gene}/{alt}" + (f"/{tumor}" if tumor else "")
    entry = ctx.ledger.add(
        source_kind="oncokb",
        source_id=f"{gene}:{alt}" + (f":{tumor}" if tumor else ""),
        title=f"OncoKB: {gene} {alt}" + (f" in {tumor}" if tumor else ""),
        url=url,
        summary=f"{oncogenic}. {mut_effect}. {summary} {tumor_summary}".strip()[:1200],
        cited_by=ctx.specialist_id,
    )

    lines = [
        f"[{entry.label}] OncoKB annotation: {gene} {alt}" + (f" in {tumor}" if tumor else ""),
        f"  Oncogenicity: {oncogenic}",
        f"  Mutation effect: {mut_effect}",
        f"  Highest sensitizing level: {highest_level}",
        f"  Variant summary: {summary or '(none)'}",
    ]
    if tumor_summary:
        lines.append(f"  Tumor-type summary: {tumor_summary}")
    lines.append("  Treatments:")
    lines.extend(rx_lines)
    lines.append(f"  URL: {url}")
    return "\n".join(lines)
