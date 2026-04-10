# DeScAi — Claim Extraction & Validation Pipeline

## Pipeline Overview

| Step | Script | Input | Output |
|------|--------|-------|--------|
| 1 | `add_data.py` (semantic headings via local vLLM) | PDF files | `text_knowledge_base.jsonl` |
| 2 | `spacy_test.py` | `text_knowledge_base.jsonl` | `test_output_tagged.jsonl` |
| 3 | `LLM_extract.py` (claim extraction via local vLLM) | `test_output_tagged.jsonl` | `final_claims_for_audit.jsonl` |
| 4 | `claim_validator.py` | `final_claims_for_audit.jsonl` | `validated_claims.jsonl` |

**Dependencies (LLM steps):** Install `openai` and `python-dotenv` (`pip install openai python-dotenv`). Steps 1–4 do **not** require `anthropic` or `ANTHROPIC_API_KEY`. Docling, spaCy, Transformers, etc. are still required for PDF chunking and tagging as before.

---

## Invocation

```bash
# Step 1 — Convert PDFs to chunks
python claim-extract-test/add_data.py --folder <pdf_dir> -o claim-extract-test/text_knowledge_base.jsonl

# Step 2 — spaCy dependency tagging
python claim-extract-test/spacy_test.py

# Step 3 — LLM claim extraction (local vLLM, OpenAI-compatible API)
python claim-extract-test/LLM_extract.py

# Step 4 — Claim validation (local vLLM)
python claim-extract-test/claim_validator.py
```

---

## Configuration — local vLLM (OpenAI-compatible)

Set via environment variables or a `.env` file in the project root. **Steps 1, 3, and 4** all send LLM traffic to `VLLM_BASE_URL` (same server; use `VALIDATOR_MODEL` for the served model name unless you override per deployment).

| Variable | Default | Description |
|----------|---------|-------------|
| `VLLM_BASE_URL` | `http://localhost:8000/v1` | vLLM OpenAI API base URL |
| `VLLM_API_KEY` | `none` | API key sent to vLLM (use `none` or empty if your server does not require one) |
| `VALIDATOR_MODEL` | `mixtral-8x7b-instruct` | Model id for heading classification (step 1), claim extraction (step 3), and validation (step 4) |
| `VALIDATOR_CONCURRENCY` | `15` | Max concurrent async validation requests (step 4 only) |
| `VALIDATOR_KEY_SECTION_MAX_CHARS` | `24000` | Max chars of key sections injected per claim validation prompt (step 4) |

---

## Output Schema — `validated_claims.jsonl`

Each line is a JSON object extending the extraction schema with three new fields:

```json
{
  "claim_type": "Fact | Assertion | Roadmap",
  "claim": "Decontextualized claim text.",
  "chunk_id": 1,
  "doc_name": "paper_filename",
  "category": "bio-new",
  "section_heading": "Results",
  "verdict": "supported | unsupported | insufficient_info",
  "rationale": "≤50-word explanation of the verdict.",
  "relevancy_score": 0.85,
  "validation_error": true
}
```

> `validation_error` is only present (and `true`) when the LLM returned a malformed or invalid response. These records are **flagged, not dropped**, so Phase 3 can handle or re-run them.

### Verdict meanings

| Verdict | Meaning |
|---------|---------|
| `supported` | Key sections substantiate the claim |
| `unsupported` | Key sections contradict or do not support the claim |
| `insufficient_info` | Key sections lack enough detail to judge |
| `parse_error` | LLM returned malformed JSON (flagged) |
| `validation_error` | Response was parseable but contained an invalid field value (flagged) |
| `error` | Network or API error after all retries (flagged) |
