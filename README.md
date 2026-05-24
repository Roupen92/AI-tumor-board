# AI Tumor Board

A multi-agent web app where 5 GPT-5.1 specialist agents independently research a clinical case, then discuss it across multiple rounds until they converge on a consensus recommendation. You watch the discussion happen live.

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

The molecular agent **self-skips** when the case has no molecular data (no NGS, no IHC, no MSI/TMB, no mutation list). When it does participate, its findings are prepended to every other specialist's context in round 2+ so it can "update" them.

**Retrieve-or-abstain rule:** if a specialist finishes its tool loop without registering any evidence in the session ledger, it gets one more chance to retrieve. If it still has zero evidence, it abstains rather than answering from clinical knowledge alone.

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
# Open .env and paste your OPENAI_API_KEY (the only required value)
```

Optional in `.env`:
- `MEDBOARD_MODEL=gpt-5.1` (default — override to fall back to e.g. `gpt-5` or `gpt-4.1`)
- `NCBI_EMAIL=you@example.com` and `NCBI_API_KEY=...` (raises Entrez rate limit from 3 to 10 req/s)

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
