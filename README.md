# AI Tumor Board

A multi-agent web app where 7 specialist agents (powered by **Google Gemini 3.1 Pro**) independently research a clinical case, then discuss it across multiple rounds until they converge on a consensus recommendation. You watch the discussion happen live.

> Default model is **`gemini-3.1-pro-preview`** (paid-tier required). For free-tier testing, set `MEDBOARD_MODEL=gemini-2.5-flash` in `.env`.

**⚠️ Research / educational demo only. Not for clinical use.**

## The board

Every specialist has the base literature stack (PubMed, Europe PMC, Semantic Scholar, web fallback). Specialty-specific tools layer on top:

| Specialist | Specialty-specific tools | PubMed bias |
|---|---|---|
| Radiation Oncologist | — | Radiotherapy[MeSH] |
| Medical Oncologist | clinical_trials, fda_approvals, dailymed | Antineoplastic Agents[MeSH] |
| Surgical Oncologist | — | Surgical Procedures, Operative[MeSH] |
| Clinical Pharmacist | drug_interactions, fda_approvals, dailymed | Drug Interactions[MeSH] |
| Molecular Oncologist (conditional) | civic, clinical_trials, fda_approvals | Mutation, Biomarkers, Tumor[MeSH] |
| Pathologist (conditional) | — | Pathology, Immunohistochemistry[MeSH] |
| Clinical Trial Matcher (conditional) | clinical_trial_match_search, clinical_trial_details | Clinical Trials as Topic, Patient Selection[MeSH] |

The conditional agents **self-skip** when their domain is irrelevant:
- The **molecular** agent skips when the case has no NGS / IHC / MSI / TMB / mutation list.
- The **pathologist** skips when the diagnosis and biomarkers are clear and unambiguous; it engages when there is equivocal IHC (e.g., HER2 2+), NOS / undifferentiated tumors, unclear primary site, or "favor / suspicious for" diagnostic language.
- The **trial matcher** skips a clearly-curable early-stage case on standard therapy; it engages for advanced / metastatic / biomarker-driven disease.

When the molecular agent or pathologist engages, its findings are prepended to every other specialist's context in round 2+ so it can "update" them.

### Clinical Trial Matcher

The trial matcher is a **cooperating three-stage pipeline** that surfaces as one board voice: a **Finder** searches the most recent recruiting-now / opening-soon trials (by condition + biomarker, newest-updated first), a **Screener** pulls each candidate's full inclusion/exclusion criteria and maps the patient's features to them (MEETS / DOES NOT MEET / UNCLEAR — NEED DATA, each grounded in the trial's citation), and a **Ranker** writes the recommendation with standard-of-care preserved as the fallback.

- **Toggle:** a "Match clinical trials" checkbox on the case form turns the matcher on/off per run (on by default). When off, it is left out of the board entirely.
- **Location:** an optional "Patient location" field filters to trials with a nearby recruiting site. It uses ClinicalTrials.gov's own place-name filter (`query.locn`); when a place can be geocoded it tightens to a precise radius (`filter.geo`). Geocoding is best-effort (keyless OSM Nominatim) and degrades gracefully to place-name matching.

**Evidence-only rule (strict):** every clinical claim must be backed by an `[E#]` citation from a retrieved source. The board does NOT accept training-knowledge answers, `(judgment)` annotations, or weasel phrases like "in my experience" / "typically". A specialist that finishes with no citations in its draft is forced to abstain.

## Sources

| Source | What it gives | Key needed |
|---|---|---|
| PubMed (NCBI Entrez) | Peer-reviewed abstracts | No (optional NCBI_API_KEY raises rate limit) |
| Europe PMC | PubMed + preprints + EU pubs | No |
| Semantic Scholar | Broader academic + citation graph | No |
| ClinicalTrials.gov v2 | Trial protocols + status | No |
| openFDA | Drug approval records | No |
| DailyMed | Full FDA prescribing labels | No |
| RxNorm | Drug-drug interactions | No |
| CIViC | Community-curated variant evidence (mutation → therapy) | No (GraphQL) |
| Brave Search | General web fallback | **Brave_API** (free 2k/mo) |

## Setup

```bash
cp .env.example .env
# Open .env and paste your GEMINI_API_KEY (the only required value).
# Get a free key at https://aistudio.google.com/apikey
```

Optional in `.env`:
- `MEDBOARD_MODEL=gemini-3.1-pro-preview` (default — drop to `gemini-2.5-flash` for free-tier testing)
- `MEDBOARD_PROVIDER=openai` + `OPENAI_API_KEY=...` to flip back to GPT-class models
- `NCBI_EMAIL=you@example.com` and `NCBI_API_KEY=...` (raises Entrez rate limit from 3 to 10 req/s)
- `Brave_API=...` (enables the `web_search` tool — free tier 2k/mo)

## Run

```bash
./run.sh
```

That creates a `.venv`, installs dependencies, and starts uvicorn on `http://localhost:8000`. Open it in a browser.

## How a session flows

1. Paste a clinical case in the textarea, pick a max-rounds value (2 is a good default), click **Convene board**.
2. All 5 specialists fan out in parallel (capped at 2 concurrent calls to be polite to NCBI). You see tool calls stream into each panel's tool-activity log.
3. When all panels reach "done" (or "skipped"), the judge runs and posts its verdict to the transcript.
4. If the judge says "no consensus" and you haven't hit max rounds, round 2 begins automatically. Each specialist now sees the others' positions and the open questions the judge flagged.
5. When consensus is reached (or rounds are exhausted), the synthesizer produces the final markdown recommendation. It includes deduped references with PubMed links.

## A case that exercises all five specialists

```
62-year-old man with cT3N1M0 distal esophageal adenocarcinoma, HER2-negative,
PD-L1 CPS 5, MSS, ECOG 1, CKD stage 3 (eGFR 45), on warfarin for atrial
fibrillation. Otherwise a surgical candidate. What is the recommended management?
```

This case includes HER2 status, PD-L1, and MSI — so the molecular oncologist will activate, find that HER2-negative/MSS narrows options but PD-L1 CPS 5 may inform checkpoint selection, and update the others in round 2.

A case that should make molecular **skip**:

```
58-year-old woman with biopsy-proven cT2N0M0 anal squamous-cell carcinoma.
ECOG 0. What is the recommended management?
```

(no biomarker data → molecular replies `SKIP: no molecular findings to evaluate.`)

## Project layout

```
app/
  server.py            FastAPI + SSE
  board.py             round loop + judge + synthesizer
  specialist.py        per-specialist GPT-5.1 tool loop
  config.py            specialists, tools, limits
  prompts.py           system prompts (5 specialists + judge + synthesizer)
  llm.py               OpenAI wrapper w/ retry
  evidence.py          per-session ledger (dedupes, assigns [E1] labels)
  sessions.py          in-memory session registry + TTL cleanup
  tools/               pubmed, clinical_trials, fda, rxnorm
static/
  board.html, board.js, styles.css
tests/
  test_board.py        mocked LLM round-loop test
```

## Tests

```bash
./.venv/bin/pytest -v
```

The mocked test verifies the round loop terminates on consensus and respects `max_rounds`.

## Notes & risks

- **Cost**: 5 specialists × up to 4 rounds + judge per round + final = up to ~30 GPT-5.1 calls per case. Default `max_rounds=2` keeps it modest.
- **Latency**: 3–6 minutes for 2 rounds + final. SSE streams panel activity so you can watch what's happening.
- **NCBI rate limits**: capped at 2 parallel specialists. Add `NCBI_API_KEY` to your `.env` to lift the cap.
- **Model availability**: if `gpt-5.1` isn't on your account, set `MEDBOARD_MODEL` to a model you have access to.
- **No persistent memory**: every case starts cold — no shared knowledge base across sessions.
- **Clinical safety**: this is a demo. Don't use any output for clinical decision-making.
