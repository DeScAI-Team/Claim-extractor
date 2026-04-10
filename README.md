# DeScAi — Claim Extraction & Validation Pipeline

## Pipeline Overview

| Step | Script | Input | Output |
|------|--------|-------|--------|
| 1 | `add_data.py` | PDF files | `test_output.jsonl` |
| 2 | `spacy_test.py` | `test_output.jsonl` | `test_output_tagged.jsonl` |
| 3 | `LLM_extract.py` | `test_output_tagged.jsonl` | `final_claims_for_audit.jsonl` |
| 4 | `claim_validator.py` | `final_claims_for_audit.jsonl` | `validated_claims.jsonl` |

---

## Invocation

```bash
# Step 1 — Convert PDFs to chunks
python claim-extract-test/add_data.py --input <pdf_dir>

# Step 2 — spaCy dependency tagging
python claim-extract-test/spacy_test.py

# Step 3 — LLM claim extraction (Claude / Anthropic)
python claim-extract-test/LLM_extract.py

# Step 4 — Claim validation (vLLM)
python claim-extract-test/claim_validator.py
```

---

## Claim Validator — Config

Set via environment variables or a `.env` file in the project root:

| Variable | Default | Description |
|----------|---------|-------------|
| `VLLM_BASE_URL` | `http://localhost:8000/v1` | vLLM server base URL |
| `VLLM_API_KEY` | `token-abc123` | API key passed to vLLM |
| `VALIDATOR_MODEL` | `mistralai/Mixtral-8x7B-Instruct-v0.1` | Model served by vLLM |
| `VALIDATOR_CONCURRENCY` | `5` | Max concurrent async requests (semaphore) |
| `VALIDATOR_KEY_SECTION_MAX_CHARS` | `24000` | Max chars of key sections injected per claim validation prompt |

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
