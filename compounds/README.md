# pump-science

End-to-end pipeline for screening a compound's longevity research potential using public APIs and local LLM inference. All outputs are **research screening aids only — not medical advice, prescribing guidance, or regulatory submissions**.

---

## Setup

```bash
pip install -r requirements.txt
```

`prepare.py`, `list.py`, and `group_by_stance.py` use **stdlib only**. `discover.py` requires `requests`. `tag.py` and `review.py` require `openai` (and optionally `python-dotenv`).

---

## Pipeline at a glance

```
discover.py  →  report_<UTC>.json
prepare.py   →  prepared_<stem>_agent.json
list.py      →  units.jsonl
tag.py       →  units_tagged.jsonl
group_by_stance.py  →  grouped_by_stance.json
review.py    →  <Compound>-review.json
```

Each step is offline from the previous one — you can re-run any stage without re-fetching upstream data.

---

## Scripts

### `discover.py` — fetch compound data from public APIs

**Why it exists:** Consolidates regulatory, trial, pathway, and literature data from four public APIs into a single timestamped JSON artifact under `pump-science/<compound>/`. Having one canonical raw report per run means all downstream steps can be reproduced or re-prepared offline without repeated network calls.

**What it fetches:**
- **OpenFDA** — FAERS adverse event terms + drug label (SPL) fields
- **ClinicalTrials.gov v2** — slim study list (NCT id, phases, status, outcomes)
- **KEGG REST** — drug IDs, linked pathway names/descriptions, longevity keyword flags
- **Europe PMC** — up to 150 articles (50 per query) across `longevity`, `aging`, `lifespan` searches; deduplicated by pmid → pmcid → doi → id

All HTTP calls use a **10 s timeout**. Failures are recorded in `metadata.failures` and the corresponding section is `null` in the output; the file is always written.

**Label result filtering:** After fetching drug label results from OpenFDA, `discover.py` keeps only entries whose `openfda.generic_name` list contains the queried compound name as a case-insensitive substring. This prevents unrelated multi-ingredient products from being attributed to the queried compound when it has no FDA-approved label of its own. When results are dropped, `metadata.label_filter_dropped` records the count and `openfda.drug_labels` is `[]` (empty list) rather than `null` — so downstream code can distinguish a successful-but-empty query from a failed API call.

**CLI:**

```bash
# From the pump-science directory
python discover.py --compound metformin
#   → pump-science/metformin/report_<YYYYMMDD_HHMMSSZ>.json

python discover.py --compound "Doxycycline HCl"
#   → pump-science/Doxycycline_HCl/report_<timestamp>.json

# Pipe to stdout (skip file write)
python discover.py --compound metformin --stdout

# Explicit filename (relative = inside compound folder, not repo root)
python discover.py --compound metformin --output my-run.json
```

Output always lives under `pump-science/<sanitized_compound>/` unless you pass an absolute `--output` path or `--stdout`. Windows reserved names (CON, NUL, etc.) get a leading/trailing underscore.

---

### `prepare.py` — transform raw report into review-ready JSON

**Why it exists:** The raw discover report is dense and unstructured for LLM use. `prepare.py` reads it offline and produces a smaller, semantically organized artifact: literature ranked by relevance, trials sorted by result availability, SPL excerpts deduplicated by SHA-256, FAERS terms capped, and explicit `metadata.coverage` flags so a missing section is never confused with empty biology. It is **purely a local transform** — no HTTP calls.

**Two output formats:**

| Format | Suffix | Use when |
|--------|--------|----------|
| `review` (default) | `prepared_<stem>.json` | You want full research + risks blobs plus `agent_context` in one file for grep / archival. |
| `agent` | `prepared_<stem>_agent.json` | You want a smaller file for LLM context — `agent_context` only, no duplicated SPL/article blobs. Pass this to `list.py`. |

**CLI:**

```bash
cd pump-science

# Default: processes every pump-science/*/report_*.json
python prepare.py

# Single file, agent format (most common for the pipeline)
python prepare.py Doxycycline/report_20260419_062655Z.json --format agent

# Glob patterns (quote on Windows to prevent shell expansion)
python prepare.py "*/report_*.json" --format agent

# Write to a separate output tree
python prepare.py --output-root ./prepared

# Single file to stdout
python prepare.py Doxycycline/report_*.json --stdout
```

Exit code 1 if no JSON inputs matched or `--stdout` is used with multiple files.

---

### `list.py` — flatten prepared JSON into per-unit JSONL

**Why it exists:** Downstream LLM tagging works best with one small, self-contained JSON object per inference call. `list.py` expands `agent_context.evaluation_units` from a prepared file into UTF-8 JSONL — one line per unit — so `tag.py` can process each independently without loading the full prepared blob.

Each output line has: `compound_name`, `unit_sequence` (index / total across the run), `unit_id`, `unit_type`, `provenance`, `payload`, and optionally `prepared_file` (basename).

Use `--audit` when you need verbose columns: `$schema_hint`, full paths, `json_path_in_prepared_doc`, `report_timestamp`, etc. Use `--repeat-coverage` (only with `--audit`) to repeat `metadata.coverage` on every line.

If `evaluation_units` is missing (older prepared JSON), `list.py` rebuilds it from the nested `agent_context.*` sections using `prepare._build_evaluation_units`.

**CLI:**

```bash
cd pump-science

# Slim JSONL (default)
python list.py Doxycycline/prepared_report_20260419_062655Z_agent.json -o Doxycycline/units.jsonl

# Multiple prepared files; unit_sequence.index is global across the batch
python list.py metformin/prepared_*_agent.json doxycycline/prepared_*_agent.json -o all_units.jsonl

# Stdout
python list.py prepared_report_*_agent.json

# Audit rows with payload truncation
python list.py prepared_*_agent.json --audit --repeat-coverage --truncate-payload 6000 -o audit.jsonl
```

---

### `tag.py` — assign section, stance, and risk tags via LLM

**Why it exists:** Each evaluation unit needs two categorical labels to route it through the review pipeline: what *kind* of evidence it is (`report_section`) and whether it *supports or cautions* longevity-oriented exploration (`decision_relevance`). A second pass adds a `risk_severity` ordinal so the review step can quantify harm signal without re-reading every unit. Splitting these into two prompt calls keeps each prompt focused and its output verifiable.

`tag.py` sends each JSONL line to a vLLM (or any OpenAI-compatible) endpoint **twice**: once for section/stance, once for risk severity. The same raw unit JSON is the user message for both rounds — tags from round 1 are not fed into round 2.

**CLI:**

```bash
cd pump-science

python tag.py Doxycycline/units.jsonl -o Doxycycline/units_tagged.jsonl

# Section/stance only (skip risk round)
python tag.py Doxycycline/units.jsonl -o out.jsonl --skip-risk

# Read from stdin, write to stdout
python tag.py - -o -
```

Default input if omitted: `pump-science/Doxycycline/units.jsonl`.  
Default output if `-o` omitted: `<input_stem>_tagged.jsonl` beside the input.

**Environment variables:**

| Variable | Default | Purpose |
|----------|---------|---------|
| `VLLM_BASE_URL` | `http://localhost:8000/v1` | API base URL |
| `VLLM_API_KEY` | `none` | API key (often unused for local vLLM) |
| `TAGGER_MODEL` | falls back to `CLASSIFIER_MODEL`, then `VALIDATOR_MODEL` | Model id for round 1 (and round 2 if `TAGGER_RISK_MODEL` is unset) |
| `TAGGER_RISK_MODEL` | *(same as `TAGGER_MODEL`)* | Override model for risk round only |
| `TAGGER_MAX_TOKENS` | `2048` | Completion budget (increase for long reasoning traces) |
| `TAGGER_RISK_RETRIES` | `3` | Re-ask attempts when risk output fails allowlist parsing |
| `TAGGER_SEED` | *(unset)* | Passed as `seed` when the server supports it |

**vLLM note:** If your vLLM server loads a `generation_config.json` that overrides `temperature`, relaunch with `--generation-config vllm` so client parameters (`temperature=0`, `top_p=1`, `top_k=-1`) take effect.

---

### `group_by_stance.py` — aggregate tagged units by stance

**Why it exists:** After tagging, `review.py` needs to know which units support exploration vs. raise caution, and how many. `group_by_stance.py` partitions rows by `decision_relevance` and computes a numeric `scientific_grounding` score so `review.py` can calibrate prose language without re-reading every unit.

**CLI:**

```bash
cd pump-science

python group_by_stance.py Doxycycline/units_tagged.jsonl
# → Doxycycline/grouped_by_stance.json

python group_by_stance.py metformin/units_tagged.jsonl -o metformin/grouped_by_stance.json
```

Default input if omitted: `pump-science/Doxycycline/units_tagged.jsonl`.  
Default output: `<input_dir>/grouped_by_stance.json`.

**`scores.scientific_grounding`:** `supports_exploration_count / (supports_exploration_count + raises_caution_count)`, rounded to two decimals. `null` if neither bucket has any rows. `risk_information`, `mixed_or_unclear`, `context_only`, and `unmapped` rows do not enter this ratio.

---

### `review.py` — synthesize final review from grouped units

**Why it exists:** The three LLM passes in `review.py` mirror how a human reviewer would process the grouped evidence: first assess the scientific case, then assess the risk picture, then write a high-level synthesis that references the score and data coverage. Separating the passes prevents the risk paragraph from anchoring the scientific grounding paragraph (and vice versa), and gives the final synthesis a compact, structured context to work from.

`review.py` reads `grouped_by_stance.json` and runs **three sequential completions**:

1. **Pass 1 — scientific grounding:** feeds `supports_exploration` members to `prompts/pump-science-scientific-grounding-evaluation.md`
2. **Pass 2 — risk statement:** feeds `raises_caution` + `risk_information` members to `prompts/pump-science-risk-statement-evaluation.md`
3. **Pass 3 — review statement:** feeds a compact bundle (Pass 1 & 2 text + `scientific_grounding_score` + tag counts + `metadata.coverage` from the nearest `prepared_report_*.json`) to `prompts/pump-science-review-statement-evaluation.md`

Output: `<compound_dir>/<compound_name>-review.json` with `compound_name`, `review_date`, `review_statement`, and nested `categories.scientific_grounding` / `categories.risk_assessment` each containing `score` and `rationale`.

**CLI:**

```bash
cd pump-science

python review.py Doxycycline/grouped_by_stance.json
# → Doxycycline/Doxycycline-review.json

python review.py Doxycycline/grouped_by_stance.json -o Doxycycline/my-review.json

# Override model
python review.py Doxycycline/grouped_by_stance.json --model my-model-id
```

Default input if omitted: `pump-science/Doxycycline/grouped_by_stance.json`.

**Environment variables:**

| Variable | Default | Purpose |
|----------|---------|---------|
| `REVIEWER_MODEL` | falls back to `TAGGER_MODEL` → `CLASSIFIER_MODEL` → `VALIDATOR_MODEL` | Model for all three passes |
| `REVIEWER_MAX_TOKENS` | `2048` | Completion budget per pass |
| `VLLM_BASE_URL`, `VLLM_API_KEY` | same as `tag.py` | API endpoint |

---

## End-to-end example

```bash
cd pump-science

python discover.py --compound Doxycycline
python prepare.py Doxycycline/report_<timestamp>.json --format agent
python list.py Doxycycline/prepared_<stem>_agent.json -o Doxycycline/units.jsonl

python tag.py Doxycycline/units.jsonl -o Doxycycline/units_tagged.jsonl
python group_by_stance.py Doxycycline/units_tagged.jsonl

python review.py Doxycycline/grouped_by_stance.json
# → Doxycycline/Doxycycline-review.json
```

Replace `<timestamp>` and `<stem>` with the actual filenames produced by each step. Each step can be re-run independently without repeating upstream steps.

---

## Output files

| File | Produced by | Contents |
|------|-------------|---------|
| `<compound>/report_<UTC>.json` | `discover.py` | Raw API payloads + metadata (includes `metadata.label_filter_dropped`) |
| `<compound>/prepared_<stem>.json` | `prepare.py --format review` | Full research + risks + agent_context |
| `<compound>/prepared_<stem>_agent.json` | `prepare.py --format agent` | agent_context only (smaller) |
| `<compound>/units.jsonl` | `list.py` | One JSON object per evaluation unit |
| `<compound>/units_tagged.jsonl` | `tag.py` | Units + report_section + decision_relevance + risk_severity |
| `<compound>/grouped_by_stance.json` | `group_by_stance.py` | Units bucketed by stance + scientific_grounding score |
| `<compound>/<Compound>-review.json` | `review.py` | Final review: grounding, risk, review_statement |

`report_*.json` and `prepared_*.json` files are listed in `.gitignore` and are not committed.

---

## Deep technical reference

For the full pipeline logic — prompt text, parsing code, allowlists, scoring formulas, environment variables, and guidance for adding features or fixing bugs — see **[REVIEW_LOGIC.md](REVIEW_LOGIC.md)**.
