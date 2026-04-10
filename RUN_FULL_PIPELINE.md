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

Place `.env` next to `run_e2e_pipeline.py`. Steps 1, 3, and 4 read these (see `README.md` for defaults).

| Variable | Purpose |
|----------|---------|
| `VLLM_BASE_URL` | Base URL including `/v1`, e.g. `http://cluster-node:8000/v1` |
| `VLLM_API_KEY` | API key sent to the server; use `none` if unused |
| `VALIDATOR_MODEL` | **Must match** the served model id (your demo model) |

Optional for step 4:

| Variable | Purpose |
|----------|---------|
| `VALIDATOR_CONCURRENCY` | Parallel validation requests (default `15`; lower on small GPUs) |
| `VALIDATOR_KEY_SECTION_MAX_CHARS` | Max chars of key sections per claim (default `24000`) |

The orchestrator loads `.env` when `python-dotenv` is installed; pipeline scripts also call `load_dotenv()`.

## Option A — One command (single PDF)

From the **repository root**:

```bash
python run_e2e_pipeline.py --dry-run
python run_e2e_pipeline.py
python run_e2e_pipeline.py --pdf path/to/paper.pdf
```

Outputs under `claim-extract-test/`:

- `text_knowledge_base.jsonl` — step 1
- `test_output_tagged.jsonl` — step 2
- `final_claims_for_audit.jsonl` — step 3
- `validated_claims.jsonl` — step 4

## Option B — Step by step

From `README.md`; ensure step 1 writes `claim-extract-test/text_knowledge_base.jsonl` when running from repo root:

```bash
python claim-extract-test/add_data.py --folder /path/to/pdfs -o claim-extract-test/text_knowledge_base.jsonl
python claim-extract-test/spacy_test.py
python claim-extract-test/LLM_extract.py
python claim-extract-test/claim_validator.py
```

Single PDF: `python claim-extract-test/add_data.py --file path/to/file.pdf -o claim-extract-test/text_knowledge_base.jsonl`

## Cluster / demo checklist

- [ ] Health check against `VLLM_BASE_URL` succeeds.
- [ ] `VALIDATOR_MODEL` matches the served model id.
- [ ] Your runner can reach the API (firewall / port-forward).
- [ ] Tune `VALIDATOR_CONCURRENCY` if the server is overloaded.

## Secrets

Do not commit `.env` or real API keys. `.env` is listed in `.gitignore` in this repo.
