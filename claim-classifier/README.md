# Step 5 — Claim classification (JSONL enricher)

This step takes **validated claims** from step 4 and adds **semantic claim-type tags** using the same local vLLM (OpenAI-compatible chat API) as the rest of the pipeline. Each input record is copied through unchanged, with three new fields that hold up to three allowlisted tags in fixed slots.

## What it does

- Reads JSONL where each line is one claim object (typically `claim-extract-test/validated_claims.jsonl`).
- For each record, sends **only** the `claim` string to the model. No chunk text, section headings, or validator fields are included in the user message.
- Uses the **entire** contents of [`prompts/claim_classification_prompt_v4 (1).md`](../prompts/claim_classification_prompt_v4%20(1).md) as the **system** message (classifier instructions). That file is the single source of truth for both instructions and the official tag list.
- Parses the model reply as whitespace-separated tokens, keeps tokens that appear **exactly** in the tag list from the prompt (no typo correction), preserves order, deduplicates (first occurrence wins), and keeps at most **three** tags.
- Writes JSONL where each line is the **original object** plus `claim_classification_1`, `claim_classification_2`, and `claim_classification_3`.

## Prerequisites

- `openai` and `python-dotenv` (same as steps 3–4).
- A reachable vLLM (or compatible) server; see the root [README.md](../README.md) for base URL and model configuration.

## Run

From the **repository root**:

```bash
python claim-classifier/classify_claims.py
```

Defaults:

- **Input:** `claim-extract-test/validated_claims.jsonl`
- **Output:** `claim-classifier/classified_claims.jsonl` (parent directory is created if needed)

Override paths:

```bash
python claim-classifier/classify_claims.py --input path/to/in.jsonl --output path/to/out.jsonl
```

Processing is **sequential** (one claim per request). Progress is printed every 25 classified claims.

## Environment variables

Loaded from `.env` in the repo root (same pattern as other pipeline scripts).

| Variable | Default | Description |
|----------|---------|-------------|
| `VLLM_BASE_URL` | `http://localhost:8000/v1` | OpenAI-compatible API base URL |
| `VLLM_API_KEY` | `none` | API key if required by the server |
| `CLASSIFIER_MODEL` | value of `VALIDATOR_MODEL`, else `mixtral-8x7b-instruct` | Model id for classification |
| `VALIDATOR_MODEL` | `mixtral-8x7b-instruct` | Used as fallback when `CLASSIFIER_MODEL` is unset |

## Output schema

Each output line is the input object with these keys **always** present:

| Key | Type | Meaning |
|-----|------|--------|
| `claim_classification_1` | JSON array of 0 or 1 string | First tag after filtering, or `[]` |
| `claim_classification_2` | JSON array of 0 or 1 string | Second distinct tag, or `[]` |
| `claim_classification_3` | JSON array of 0 or 1 string | Third distinct tag, or `[]` |

Example (two tags):

```json
{
  "claim": "…",
  "verdict": "supported",
  "claim_classification_1": ["Hypothesis"],
  "claim_classification_2": ["Benchmark"],
  "claim_classification_3": []
}
```

If `claim` is missing or empty, the script **does not** call the model; all three classification fields are `[]`.

If the API fails after retries, the raw model text is treated as empty, so all three fields are `[]` for that line (the rest of the record is still written).

## Tags and strict labels

Allowed tags are parsed at runtime from the `Tags:` block in the prompt markdown. Any token from the model that is not an **exact** match is dropped. Downstream tooling should rely on these strings as stable labels.

## Next step — grouping and LLM narratives (optional)

Classified JSONL can be grouped by scoring dimension and enriched with a one-line `claim_narrative` per record using [group-and-score/group.py](../group-and-score/group.py) and [group-and-score/prep.py](../group-and-score/prep.py). See **Grouping & narratives** in the root [README.md](../README.md).

## Related documentation

- Pipeline overview and steps 1–7: [README.md](../README.md)
- Cluster / full-run notes: [RUN_FULL_PIPELINE.md](../RUN_FULL_PIPELINE.md)
