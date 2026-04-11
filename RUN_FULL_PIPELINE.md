# Running the full pipeline (local or cluster vLLM)

Use this when your OpenAI-compatible LLM (e.g. vLLM) runs on another machine or cluster and you want a larger model for a demo.

## Prerequisites

1. **Python environment** with packages the scripts need:
   - `openai`, `python-dotenv`
   - Docling / Transformers stack for `claim-extract-test/add_data.py`
   - spaCy + English model: `python -m spacy download en_core_web_sm`

2. **vLLM (or compatible server)** exposing an OpenAI-style `/v1` chat API. Know the **exact model id** the server uses (often matches vLLM `--served-model-name`).

3. **Network** from the machine running Python to that API (cluster URL, port-forward, Ingress, etc.).

## Configuration (`.env` in repo root)

Place `.env` next to `run_e2e_pipeline.py`. LLM-backed steps (1, 3, 4, 5, and 8 — review generation) read these (see `README.md` for defaults).

| Variable | Purpose |
|----------|---------|
| `VLLM_BASE_URL` | Base URL including `/v1`, e.g. `http://cluster-node:8000/v1` |
| `VLLM_API_KEY` | API key sent to the server; use `none` if unused |
| `VALIDATOR_MODEL` | **Must match** the served model id (your demo model) |

Optional for step 5:

| Variable | Purpose |
|----------|---------|
| `CLASSIFIER_MODEL` | Override model id for claim classification only (defaults to `VALIDATOR_MODEL`) |

Optional for step 4:

| Variable | Purpose |
|----------|---------|
| `VALIDATOR_CONCURRENCY` | Parallel validation requests (default `15`; lower on small GPUs) |
| `VALIDATOR_KEY_SECTION_MAX_CHARS` | Max chars of key sections per claim (default `24000`) |

The orchestrator loads `.env` when `python-dotenv` is installed; pipeline scripts also call `load_dotenv()`.

## Option A — One command (single PDF)

From the **repository root**, `run_e2e_pipeline.py` runs **steps 1–8** (PDF through review JSON). Post-validation artifacts default to **`data/`** (override with `--artifacts-dir`). Optional **step 9** uploads `review.json` to Arweave when you pass **`--upload`** (requires `npm install` in `Arweave-Cli`, wallet `.env`, and Node).

```bash
python run_e2e_pipeline.py --dry-run
python run_e2e_pipeline.py
python run_e2e_pipeline.py --pdf path/to/paper.pdf
python run_e2e_pipeline.py --artifacts-dir path/to/run_outputs
python run_e2e_pipeline.py --upload
```

**Outputs under `claim-extract-test/`** (steps 1–4; paths fixed for downstream scripts):

- `text_knowledge_base.jsonl` — step 1
- `test_output_tagged.jsonl` — step 2
- `final_claims_for_audit.jsonl` — step 3
- `validated_claims.jsonl` — step 4

**Outputs under `--artifacts-dir`** (default `data/`; steps 5–8):

- `classified_claims.jsonl` — step 5 (`classify_claims.py -i` / `-o`)
- `grouped.json` — step 6 (`group.py -o`)
- `prepped.json` — step 7 (`prep.py -o`)
- `review.json` — step 8 (`review.py -o`)

**Optional step 9 (`--upload`):** `Arweave-Cli/upload_orchestrator.py` uploads `review.json` and writes the receipt to **`upload_receipt.json`** in the same artifacts directory (`--receipt` on that script).

### Manual steps (same as orchestrator defaults)

To run classification and downstream steps yourself (see [claim-classifier/README.md](claim-classifier/README.md)):

```bash
python claim-classifier/classify_claims.py
```

That writes `claim-classifier/classified_claims.jsonl` by default unless you pass `-o`.

### Optional — group by dimension and add LLM narratives

Aggregate by scoring dimension and attach narrative lines (see [README.md](README.md) § *Grouping & narratives*):

```bash
python group-and-score/group.py claim-classifier/classified_claims.jsonl -o group-and-score/grouped.json
python group-and-score/prep.py group-and-score/grouped.json -o group-and-score/prepped.json
```

Or pipe without an intermediate file (on Windows, use `-` for `prep.py` stdin—not `/dev/stdin`):

```bash
python group-and-score/group.py claim-classifier/classified_claims.jsonl | python group-and-score/prep.py - -o group-and-score/prepped.json
```

Review JSON from prepped output (defaults match `group-and-score/prepped.json` → `review-gen/review.json` if you omit flags):

```bash
python review-gen/review.py --prepped group-and-score/prepped.json --mappings group-and-score/mappings.json -o review-gen/review.json
```

## Option B — Step by step

From `README.md`; ensure step 1 writes `claim-extract-test/text_knowledge_base.jsonl` when running from repo root:

```bash
python claim-extract-test/add_data.py --folder /path/to/pdfs -o claim-extract-test/text_knowledge_base.jsonl
python claim-extract-test/spacy_test.py
python claim-extract-test/LLM_extract.py
python claim-extract-test/claim_validator.py
python claim-classifier/classify_claims.py
```

Single PDF: `python claim-extract-test/add_data.py --file path/to/file.pdf -o claim-extract-test/text_knowledge_base.jsonl`

Step 5 output: `claim-classifier/classified_claims.jsonl` (see [claim-classifier/README.md](claim-classifier/README.md)).

Optional grouping and narratives: same commands as in Option A above (`group-and-score/group.py`, `group-and-score/prep.py`).

## Cluster / demo checklist

- [ ] Health check against `VLLM_BASE_URL` succeeds.
- [ ] `VALIDATOR_MODEL` matches the served model id.
- [ ] Your runner can reach the API (firewall / port-forward).
- [ ] Tune `VALIDATOR_CONCURRENCY` if the server is overloaded.

## Secrets

Do not commit `.env` or real API keys. `.env` is listed in `.gitignore` in this repo.
