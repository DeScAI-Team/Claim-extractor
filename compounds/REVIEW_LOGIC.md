# pump-science — Review Pipeline: Technical Reference

This document is the authoritative technical guide for developers working on the pump-science review pipeline. It covers data flow, prompt design, parsing logic, scoring, environment configuration, and practical guidance for debugging and extending the system.

**Disclaimer:** All pipeline outputs are research screening aids only — not medical advice, prescribing guidance, or regulatory submissions.

---

## Table of contents

1. [Pipeline overview](#1-pipeline-overview)
2. [Stage 1 — Discover (`discover.py`)](#2-stage-1--discover-discoverpy)
3. [Stage 2 — Prepare (`prepare.py`)](#3-stage-2--prepare-preparepy)
4. [Stage 3 — List (`list.py`)](#4-stage-3--list-listpy)
5. [Stage 4 — Tag (`tag.py`)](#5-stage-4--tag-tagpy)
   - [Round 1: section + stance](#round-1-section--stance)
   - [Round 2: risk severity](#round-2-risk-severity)
   - [Reasoning markup stripping](#reasoning-markup-stripping)
   - [LLM transport and retry logic](#llm-transport-and-retry-logic)
6. [Stage 5 — Group (`group_by_stance.py`)](#6-stage-5--group-group_by_stancepy)
7. [Stage 6 — Review (`review.py`)](#7-stage-6--review-reviewpy)
8. [Prompt reference](#8-prompt-reference)
9. [Environment variables](#9-environment-variables)
10. [Data schemas](#10-data-schemas)
11. [Known edge cases and gotchas](#11-known-edge-cases-and-gotchas)
12. [Developer guide: adding features and fixing bugs](#12-developer-guide-adding-features-and-fixing-bugs)

---

## 1. Pipeline overview

```
                 ┌──────────────────────────────────────────────────────┐
                 │              PUBLIC APIs (network)                    │
                 │  OpenFDA · ClinicalTrials.gov v2 · KEGG · Europe PMC │
                 └──────────────────────────────────────────────────────┘
                                          │
discover.py ──────────────────────────────┼──► <compound>/report_<UTC>.json
  --compound <name>                        │    (raw API payloads, one file per run)
                                          ▼
prepare.py  ──────────────────────────────────► <compound>/prepared_<stem>_agent.json
  --format agent                               (agent_context: ranked digests, coverage)
                                          │
list.py ──────────────────────────────────────► <compound>/units.jsonl
  (one JSON object per evaluation unit)        (one line = one atomic evidence fragment)
                                          │
tag.py ───────────────────────────────────────► <compound>/units_tagged.jsonl
  LLM×2 per line (vLLM / OpenAI-compat)        +report_section, +decision_relevance,
                                               +risk_severity
                                          │
group_by_stance.py ───────────────────────────► <compound>/grouped_by_stance.json
  (partition by decision_relevance)            +scores.scientific_grounding
                                          │
review.py ────────────────────────────────────► <compound>/<Compound>-review.json
  LLM×3 passes                                 scientific_grounding, risk,
                                               review_statement
```

Each stage is fully offline from the previous one. You can re-run any single stage without re-fetching upstream data.

---

## 2. Stage 1 — Discover (`discover.py`)

### Purpose

Query four public APIs for a compound name and write one consolidated UTF-8 JSON under `pump-science/<sanitized_compound>/report_<UTC>.json`.

### API sources and queries

```python
# discover.py line 19–24
FDA  = "https://api.fda.gov/"
CT   = "https://clinicaltrials.gov/api/v2/studies"
KEGG = "https://rest.kegg.jp/"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
```

| Source | Query style | What is fetched |
|--------|-------------|-----------------|
| OpenFDA FAERS | `patient.drug.medicinalproduct:<COMPOUND_UPPER>`, limit 100 | Adverse event reaction terms, report count |
| OpenFDA Labels | `openfda.generic_name:<Compound_Title>`, limit 10 | SPL fields: `adverse_reactions`, `adverse_reactions_table`, `boxed_warning`, `boxed_warning_table`, `contraindications`, `clinical_pharmacology`, `pharmacokinetics`, `mechanism_of_action`, `drug_interactions` |
| ClinicalTrials.gov v2 | `query.term=<compound>`, pageSize 100 | Slim study objects (see `slim_study()`) |
| KEGG REST | `find/drug/<compound>` → `link/pathway/<drug_id>` → `get/<pathway_id>` | Drug IDs, pathway names, description snippets; up to `KEGG_MAX=50` pathway entries |
| Europe PMC | Three queries: `"<compound>" AND longevity/aging/lifespan`, pageSize 50 each | Deduplicated articles (pmid > pmcid > doi > id key) |

### Longevity pathway flags

```python
# discover.py lines 25–29
FLAGS = [
    ("mTOR", "mtor"), ("autophagy", "autophagy"), ("AMPK", "ampk"), ("apoptosis", "apoptosis"),
    ("cell cycle", "cell cycle"), ("oxidative stress", "oxidative stress"), ("NAD", "nad"),
    ("sirtuin", "sirtuin"), ("insulin signaling", "insulin signaling"), ("senescence", "senescence"),
]
```

Each flag is a substring match against the concatenated pathway names + description snippets. These are heuristic keyword hits — **not** experimental validation.

### Article deduplication (Europe PMC)

When the same article appears across multiple longevity/aging/lifespan query results, `discover.py` merges duplicates by keeping whichever record has the longer abstract (then higher citation count as a tiebreak), and accumulates `source_queries` across both:

```python
# discover.py lines 215–256 (simplified)
def pick(a, b):
    la, lb = len(a.get("abstract") or ""), len(b.get("abstract") or "")
    if lb > la or (la == lb and cite(b.get("citedByCount")) > cite(a.get("citedByCount"))):
        return b
    return a
```

### Output schema (raw report)

```json
{
  "compound_name": "string",
  "openfda": {
    "adverse_events": { "reaction_terms": [...], "report_count": int, "meta": {...} },
    "drug_labels": [ { "adverse_reactions": ..., "boxed_warning": ..., ... } ]
  },
  "clinical_trials": { "studies": [...], "study_count": int, "version_holder": "string|null" },
  "kegg": { "kegg_drug_ids": [...], "pathway_ids": [...], "pathways": [...], "longevity_pathway_flags": {...}, "truncated": bool },
  "europe_pmc": { "articles": [...], "unique_count": int },
  "metadata": { "timestamp": "ISO8601", "api_versions": {...}, "failures": [ { "step": "string", "reason": "string" } ] }
}
```

Any section may be `null` if the corresponding API call failed. Failures are always recorded in `metadata.failures`.

### File path logic

```python
# discover.py lines 43–69
def safe_compound_dir(compound: str) -> str:
    safe = re.sub(r"[^\w\-.]+", "_", compound, flags=re.UNICODE).strip("._- ")[:80] or "compound"
    if safe.upper() in _WIN_RESERVED:
        safe = f"_{safe}_"
    return safe
```

A **relative** `--output` path is always joined under `pump-science/<compound>/`, never the shell working directory. An **absolute** path is used as-is. `--stdout` skips writing entirely.

---

## 3. Stage 2 — Prepare (`prepare.py`)

### Purpose

Read a local `report_*.json` and emit a smaller, semantically organized artifact. No HTTP calls. Designed to:

- Rank literature by a cheap relevance heuristic
- Deduplicate SPL excerpts by SHA-256
- Build flat `evaluation_units` for per-item LLM work
- Record `metadata.coverage` so a missing API section is never confused with empty biology

### Key constants

```python
# prepare.py lines 32–67
FAERS_TERM_PREVIEW_MAX = 80       # reaction terms in review-format faers_summary
LITERATURE_DIGEST_MAX  = 35       # max literature rows in agent digest
LITERATURE_ABSTRACT_MAX = 850     # chars per abstract excerpt
TRIALS_DIGEST_MAX      = 45       # max trial rows in digest
TRIAL_TITLE_MAX        = 220
MECHANISM_BLOCK_MAX    = 1800     # chars per SPL mechanism block
RISK_BOXED_MAX         = 1400
RISK_CONTRA_MAX        = 1200
RISK_INTERACTION_MAX   = 1200
RISK_ADVERSE_EXCERPT_MAX = 2800
FAERS_DIGEST_TERMS     = 35       # FAERS terms in agent risks_overview
```

Adjusting these constants changes the token footprint of downstream agent context without altering pipeline logic.

### Literature `relevance_score`

Europe PMC does not assign a relevance score. `prepare.py` computes one solely for digest ranking:

```
relevance_score = min(citedByCount, 400)
                + Σ { 18 if term in title; else 6 if term in title+abstract }
                  for term in LONGEVITY_TERMS
```

`LONGEVITY_TERMS` includes: `longevity`, `lifespan`, `aging`, `senescence`, `healthspan`, `anti-aging`, `antiaging`, `mitochondrial`, `mtor`, `autophagy`, `daf-16`, `daf-2`, `c. elegans`, `caenorhabditis`, `health span`, `geroscience`, `rapamycin`, `calorie restriction`.

The top `LITERATURE_DIGEST_MAX` articles by this score appear in `agent_context.literature.digest_rows`. Each row stores the computed score as `relevance_score`. This is a **cheap heuristic, not systematic review quality**.

### SPL deduplication

Risk excerpt strings (boxed warnings, contraindications, interactions, adverse reactions) are deduplicated using **SHA-256 of the UTF-8 blob** before truncation. Up to `RISK_*_MAX` unique excerpts per category are kept across all label rows. This prevents the same warning text appearing verbatim from multiple label entries.

### `evaluation_units`

`agent_context.evaluation_units` is a flat ordered list built from all digest sections. Each element:

| Field | Example |
|-------|---------|
| `unit_id` | `epmc_r001`, `ct_NCT01234567`, `kegg_summary`, `kegg_pw_map01234`, `spl_mechanism_pharmacology`, `spl_boxed_0`, `faers_summary` |
| `unit_type` | `literature`, `clinical_trial`, `kegg_summary`, `kegg_pathway`, `spl_mechanism_pharmacology`, `spl_boxed_warning`, `spl_contraindication`, `spl_drug_interaction`, `spl_adverse_reaction`, `faers_headline` |
| `json_path` | `agent_context.literature.digest_rows[0]` |
| `provenance` | `{ "provider": "Europe PMC", "primary_id": "pmid:12345678", "id_type": "pmid" }` |
| `payload` | Copy of the nested row/object — callers can send one unit without walking the full tree |

The `unit_id` values are stable across re-runs of the same report (they are positional / NCT-ID–based, not random).

### `metadata.coverage`

```json
"coverage": {
  "europe_pmc":      { "present": true,  "reason": null },
  "clinical_trials": { "present": true,  "reason": null },
  "kegg":            { "present": false, "reason": "kegg section null in report; discover step may have failed (see metadata.failures)" },
  "openfda_labels":  { "present": true,  "reason": null },
  "faers":           { "present": false, "reason": "openfda_event step failed: HTTP 404: ..." }
}
```

`present: true` means the section key exists and is a non-null structure. `present: false` means the section is `null` in the raw report, and `reason` combines a generic explanation with matching `metadata.failures[].step` entries from discover. This is propagated to Pass 3 of `review.py` so the LLM can note which sources were unavailable.

### Output formats

**`review`** (default): includes full `research` + `risks` blobs plus `agent_context` and `metadata`.  
**`agent`**: includes `agent_context`, `metadata`, `disclaimers`, `preparation_warnings`, and a top-level `note`; omits bulk `research` / `risks`. Use this for LLM context — smaller token footprint.

Both formats include `source_report` (absolute path to the input report file).

---

## 4. Stage 3 — List (`list.py`)

### Purpose

Convert `agent_context.evaluation_units` from a prepared JSON into UTF-8 JSONL: one line per unit. Each line is self-contained — it includes `compound_name`, sequence metadata, `unit_id`, `unit_type`, `provenance`, and `payload` — so `tag.py` can tag each independently.

### Slim vs. audit rows

**Slim (default):** `compound_name`, `unit_sequence` (`{ "index": int, "total": int }`), `unit_id`, `unit_type`, `prepared_file` (basename, optional), `provenance`, `payload`.

**`--audit`:** additionally includes `$schema_hint` (`pump-science.evaluation_unit_jsonl.v1`), full `source_report`, absolute `source_prepared_file`, `prepare_output_format`, `report_timestamp`, `json_path_in_prepared_doc`. Add `--repeat-coverage` to also include `metadata.coverage` on every line.

### Backwards compatibility

```python
# list.py lines 38–52
def _extract_units(data: dict) -> list:
    ac = data.get("agent_context")
    units = ac.get("evaluation_units")
    if isinstance(units, list) and units:
        return units
    # Older prepared JSON: rebuild from nested agent_context sections.
    return _build_evaluation_units(
        ac.get("literature") or {},
        ac.get("clinical_trials") or {},
        ac.get("kegg") or {},
        ac.get("mechanism_hypotheses_excerpt") or {},
        ac.get("risks_overview") or {},
    )
```

If `evaluation_units` is absent (older prepared files), `list.py` imports `_build_evaluation_units` from `prepare.py` and rebuilds it. This means re-preparing old files is not required.

### `unit_sequence.index`

When multiple prepared files are passed as input, `unit_sequence.index` is **global across the batch** (incrementing from 0 across all files in order), while `unit_sequence.total` reflects the total units across all inputs. This allows downstream tooling to reference a unit by its global position.

---

## 5. Stage 4 — Tag (`tag.py`)

### Overview

For each JSONL line produced by `list.py`, `tag.py` calls the LLM **twice** using the same raw unit JSON as the user message:

- **Round 1:** section tag + stance tag (`prompts/compound-excerpt-tagging.md`)
- **Round 2:** risk severity tag (`prompts/compound-risk-profile.md`)

Tags from round 1 are **not** fed into round 2. Both rounds operate on the original unit record. Tags are appended to the record before writing to the output JSONL.

```python
# tag.py lines 395–413
user_msg = json.dumps(record, ensure_ascii=False)
raw = chat_completion(client, model, load_tagger_prompt_text(), user_msg)
sec, sta = parse_section_stance(raw, sections, stances)

risk_val = None
if not args.skip_risk:
    risk_val = risk_severity_with_retries(client, risk_model, user_msg, risk_allowed)

enriched = {**record, "report_section": sec, "decision_relevance": sta, "risk_severity": risk_val}
```

---

### Round 1: section + stance

**Prompt file:** `prompts/compound-excerpt-tagging.md`

The prompt instructs the model to output **exactly two allowlisted tokens separated by a single space** on the **final line** of its reply, with nothing else on that line.

**Section tags** (first token):

| Tag | Meaning |
|-----|---------|
| `evidence_rationale` | Peer-reviewed / preprint scientific grounding (literature units) |
| `clinical_human_data` | Interventional or observational human/clinical trial evidence |
| `mechanism_pathway_context` | Mechanism hypotheses, pathway summaries, pharmacology context (KEGG, SPL mechanism) |
| `safety_labeling` | Regulatory label text (boxed warnings, contraindications, interactions, labeled adverse reactions) |
| `surveillance_signal` | Spontaneous reporting / FAERS-style headline aggregates |

**Stance tags** (second token):

| Tag | Meaning |
|-----|---------|
| `supports_exploration` | Payload suggests scientific justification or findings motivating further longevity study |
| `raises_caution` | Payload highlights limitations, negative findings, or reasons to temper enthusiasm |
| `risk_information` | Primarily harm/safety/legal labeling content; does not map cleanly to for/against research |
| `mixed_or_unclear` | Both supportive and cautionary, or genuinely ambiguous |
| `context_only` | Background or weakly relevant; does not materially support or oppose exploration |

**Allowlist parsing:** `tag.py` loads both allowlists dynamically from the prompt file by scanning for `Tags:` headers:

```python
# tag.py lines 125–145
def parse_two_allowlists_from_prompt_md(text: str) -> tuple[frozenset, frozenset]:
    """First Tags: block = report_section; second = decision_relevance."""
    lines = text.splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == "Tags:":
            chunk = []
            i += 1
            while i < len(lines) and lines[i].strip():
                chunk.append(lines[i].strip())
                i += 1
            if chunk:
                blocks.append(chunk)
        else:
            i += 1
    if len(blocks) < 2:
        raise ValueError("compound-excerpt-tagging.md must contain two Tags: sections")
    return frozenset(blocks[0]), frozenset(blocks[1])
```

This means the allowlists in the code are always derived from the prompt file itself — **editing the prompt's `Tags:` blocks changes what tokens are accepted**. Add new tags there first, then update any downstream code that consumes the field.

**Output parsing (`parse_section_stance`):** After stripping reasoning markup, the parser tries four source strings in priority order:

1. Last non-empty line of stripped text
2. Full stripped text
3. Last line of raw text (if different from stripped last line)
4. Full raw text

For each source, it splits into tokens, normalizes each (strips trailing punctuation), and checks if positions [0] and [1] match the allowlists. If the pair is not found in order, it falls back to scanning for any section token and any stance token independently.

---

### Round 2: risk severity

**Prompt file:** `prompts/compound-risk-profile.md`

The prompt instructs the model to output **exactly one allowlisted token** on the **final line** — nothing else on that line.

**Allowlist:**

| Tag | When to use |
|-----|-------------|
| `n/a` | No medication-relevant human harm signal in this unit |
| `negligible` | Routine labeling, no emphasis on serious injury; at most mild/infrequent ADRs |
| `low` | Notable ADRs or precautions but no strong irreversible harm signal |
| `moderate` | Serious ADRs possible (organ injury, severe hypersensitivity, significant interaction burden) |
| `high` | Boxed warning language, broad contraindication, life-threatening reaction risk |
| `severe` | Imminent life-threatening risk in ordinary medication contexts described in the excerpt |

**FAERS units** are capped at `moderate` unless the excerpt itself states serious outcome patterns beyond generic term listing.

**Typo map:** `negligble` → `negligible` is applied before allowlist checking:

```python
# tag.py line 59
_RISK_TOKEN_ALIASES = {"negligble": "negligible"}
```

**Retry logic:** If no valid token is found in the model's response, `tag.py` re-issues the same completion up to `TAGGER_RISK_RETRIES` times (default 3). If all attempts fail, `risk_severity` is `null` in the output JSONL.

```python
# tag.py lines 249–266
def risk_severity_with_retries(client, model, user_content, allowed):
    system_prompt = load_risk_prompt_text()
    for attempt in range(MAX_RISK_PARSE_ATTEMPTS):
        raw = chat_completion(client, model, system_prompt, user_content)
        val = parse_risk_enum(raw, allowed)
        if val is not None:
            return val
        if attempt < MAX_RISK_PARSE_ATTEMPTS - 1:
            print(f"  [risk] parse retry {attempt+1}/{MAX_RISK_PARSE_ATTEMPTS}", file=sys.stderr)
    return None
```

---

### Reasoning markup stripping

Both prompts tell the model to put the answer tokens on the **final line** because reasoning models (Qwen, DeepSeek, etc.) emit long `<think>` / `<thinking>` / `<reasoning>` blocks before their answer. `strip_reasoning_markup` handles this:

```python
# tag.py lines 67–103
_END_THINK_MARKERS = ("</think>", "</think>", "</thinking>", "</reasoning>", "</thought>")

def strip_reasoning_markup(s: str) -> str:
    t = s.strip()
    low = t.lower()
    # Find the last end-think marker and take everything after it.
    best_idx = -1
    for m in _END_THINK_MARKERS:
        pos = low.rfind(m.lower())
        if pos > best_idx:
            best_idx = pos
            best_len = len(m)
    if best_idx >= 0:
        t = t[best_idx + best_len:].lstrip()
    # Also strip any remaining balanced blocks (handles nesting, up to 8 passes).
    block_patterns = (
        r"<think\b[^>]*>[\s\S]*?</think>",
        r"<thinking\b[^>]*>[\s\S]*?</thinking>",
        r"<reasoning\b[^>]*>[\s\S]*?</reasoning>",
        r"<thought\b[^>]*>[\s\S]*?</thought>",
        r"<redacted_thinking\b[^>]*>[\s\S]*?</think>",
    )
    for _ in range(8):
        prev = t
        for pat in block_patterns:
            t = re.sub(pat, "", t, flags=re.IGNORECASE)
        if t == prev:
            break
    return t.strip()
```

The same implementation exists in both `tag.py` and `review.py`. If you fix a stripping bug, update both files.

---

### LLM transport and retry logic

Both `tag.py` and `review.py` use the same pattern for calling the LLM:

```python
# tag.py lines 166–213
def chat_completion(client, model, system_prompt, user_content) -> str:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_content},
    ]
    kw = dict(model=model, max_tokens=MAX_TAGGER_TOKENS,
              temperature=0, top_p=1, frequency_penalty=0, presence_penalty=0, messages=messages)
    if seed_val is not None:
        kw["seed"] = seed_val
    for attempt in range(MAX_RETRIES):  # MAX_RETRIES = 4
        try:
            response = client.chat.completions.create(**kw, extra_body={"top_k": -1})
            return (response.choices[0].message.content or "").strip()
        except RateLimitError:
            time.sleep((2 ** attempt) * 5)  # 5s, 10s, 20s, 40s
        except Exception as e:
            time.sleep(2 ** attempt)        # 1s, 2s, 4s, then give up
    return ""
```

`extra_body={"top_k": -1}` is passed to encourage greedy-friendly sampling on vLLM. If the server doesn't support `extra_body`, the `TypeError` is caught and the call is retried without it.

`MAX_RETRIES = 4` is the network/transport retry count. This is **separate** from `TAGGER_RISK_RETRIES`, which is the allowlist-parse retry count for round 2.

---

## 6. Stage 5 — Group (`group_by_stance.py`)

### Purpose

Partition tagged JSONL rows by `decision_relevance` and compute a numeric `scientific_grounding` score for use in `review.py`.

### Output schema

```json
{
  "$schema_hint": "pump-science.grouped_by_stance.v1",
  "compound_name": "string",
  "source_tagged_file": "units_tagged.jsonl",
  "total_units": 93,
  "scores": {
    "scientific_grounding": {
      "supports_exploration_count": 42,
      "raises_caution_count": 18,
      "support_and_caution_total": 60,
      "score": 0.70
    }
  },
  "by_stance": {
    "supports_exploration": { "count": 42, "members": [...] },
    "raises_caution":       { "count": 18, "members": [...] },
    "risk_information":     { "count": 15, "members": [...] },
    "mixed_or_unclear":     { "count":  8, "members": [...] },
    "context_only":         { "count":  7, "members": [...] },
    "unmapped":             { "count":  3, "members": [...] }
  }
}
```

### `scientific_grounding` score formula

```
score = supports_exploration_count / (supports_exploration_count + raises_caution_count)
```

Rounded to **two decimal places**. `null` if both counts are zero (no rows in either bucket). The `risk_information`, `mixed_or_unclear`, `context_only`, and `unmapped` buckets do **not** enter this ratio.

The score is consumed by `review.py` Pass 3 as `scientific_grounding_score`. The review-statement prompt translates it to qualitative language: ≥ 0.75 → "meaningful support", 0.50–0.74 → "moderate support", < 0.50 → "limited support".

---

## 7. Stage 6 — Review (`review.py`)

### Purpose

Synthesize a three-section compound review from `grouped_by_stance.json` using three sequential LLM completions.

### Pass layout

| Pass | Input to LLM | Prompt file | Output field |
|------|-------------|-------------|-------------|
| 1 — Scientific grounding | `by_stance.supports_exploration.members` (JSON array) | `prompts/pump-science-scientific-grounding-evaluation.md` | `categories.scientific_grounding.rationale` |
| 2 — Risk statement | `by_stance.raises_caution.members` + `by_stance.risk_information.members` (merged, JSON array) | `prompts/pump-science-risk-statement-evaluation.md` | `categories.risk_assessment.rationale` |
| 3 — Review statement | Compact bundle (see below) | `prompts/pump-science-review-statement-evaluation.md` | `review_statement` |

### Pass 3 context bundle

```python
# review.py lines 180–212
def build_statement_context(compound_name, grouped, grounding_text, risk_text, prepared_ctx):
    scores = grouped.get("scores", {}).get("scientific_grounding", {})
    by_stance = grouped.get("by_stance", {})
    tag_counts = {
        stance: by_stance.get(stance, {}).get("count", 0)
        for stance in ("supports_exploration", "raises_caution", "risk_information",
                       "mixed_or_unclear", "context_only", "unmapped")
    }
    tag_counts["total"] = grouped.get("total_units", sum(tag_counts.values()))
    return {
        "compound_name": compound_name,
        "scientific_grounding": grounding_text,     # Pass 1 output
        "risk": risk_text,                          # Pass 2 output
        "scientific_grounding_score": scores.get("score"),
        "tag_counts": tag_counts,
        "coverage": prepared_ctx.get("coverage"),   # from nearest prepared_report_*.json
        "report_timestamp": prepared_ctx.get("report_timestamp"),
    }
```

### Coverage context loading

`review.py` looks for the most recent `prepared_report_*.json` (non-agent) in the same directory as `grouped_by_stance.json`. If none is found, it falls back to `prepared_report_*_agent.json`. If neither exists, `coverage` and `report_timestamp` are `null` in the Pass 3 bundle.

```python
# review.py lines 145–177
def load_prepared_report_context(compound_dir: Path) -> dict:
    candidates = sorted(
        [p for p in compound_dir.glob("prepared_report_*.json")
         if not p.name.endswith("_agent.json")],
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not candidates:
        candidates = sorted(compound_dir.glob("prepared_report_*_agent.json"), ...)
    if not candidates:
        return {"coverage": None, "report_timestamp": None}
    # Load, parse, and return meta.coverage + meta.timestamp
```

### Fallback behavior when buckets are empty

- If `supports_exploration.members` is empty: `grounding_text` is set to a canned "no units tagged supports_exploration" message without calling the LLM.
- If both `raises_caution` and `risk_information` members are empty: `risk_text` is set to a canned "risk cannot be assessed" message without calling the LLM.
- Pass 3 is always called regardless, using whatever text was produced (or the canned messages).

### Output schema

```json
{
  "compound_name": "Doxycycline",
  "review_date": "April 19, 2026",
  "review_statement": "prose paragraph...",
  "categories": {
    "scientific_grounding": {
      "score": 0.70,
      "rationale": "prose paragraph..."
    },
    "risk_assessment": {
      "score": null,
      "rationale": "prose paragraph..."
    }
  }
}
```

`categories.risk_assessment.score` is always `null` currently — `scores.aggregate_risk` is read from `grouped_by_stance.json` but that field is never populated by `group_by_stance.py`. This is a placeholder for a future aggregate risk score.

### Reasoning markup stripping in `review.py`

`review.py` applies `strip_reasoning_markup` to the raw LLM output before storing it. Unlike `tag.py` (which needs tokens on the final line), the review prompts ask for prose paragraphs, so stripping is purely for cleanliness — the full stripped output is stored as the rationale text.

---

## 8. Prompt reference

All prompts live in `prompts/` at the repo root (one level above `pump-science/`).

### `compound-excerpt-tagging.md`

**Used by:** `tag.py` round 1  
**Input:** Single JSON object (one evaluation unit)  
**Output:** Two allowlisted tokens on the final line: `<section_tag> <stance_tag>`

Key design decisions:
- Instructs the model to base `report_section` on `unit_type` and payload shape (not just the compound name).
- Instructs the model to base `decision_relevance` on the **substance** of the payload text.
- Final-line-only output rule is critical for reasoning models that emit `<think>` blocks.

The `Tags:` blocks in this file are the **ground truth** for both allowlists in `tag.py`. Edit them together.

### `compound-risk-profile.md`

**Used by:** `tag.py` round 2  
**Input:** Same JSON object as round 1 (original unit, no round 1 tags)  
**Output:** Single allowlisted token on the final line

Key design decisions:
- Severity scale uses **deterministic cues**: boxed warning → `high`, FAERS term list → capped at `moderate`, literature → map only from explicit human harm claims.
- Instructs the model to output `n/a` when the content contains no medication-relevant human harm signal (prevents false positives from purely mechanistic units).

### `pump-science-scientific-grounding-evaluation.md`

**Used by:** `review.py` Pass 1  
**Input:** JSON array of `supports_exploration` evaluation units  
**Output:** One prose paragraph (5–10 sentences)

Key design decisions:
- Mandatory citation format: `[unit_id — Title (Year), DOI]` after every factual claim.
- Instructs the model to distinguish compound-specific findings from appearances as a research tool (e.g. doxycycline as a Tet-inducible system tool vs. as a direct longevity intervention).
- Calibrated language required: "the digests suggest…", "findings in *C. elegans* indicate…" — no promotional language.

### `pump-science-risk-statement-evaluation.md`

**Used by:** `review.py` Pass 2  
**Input:** JSON array of `raises_caution` + `risk_information` evaluation units  
**Output:** One prose paragraph (4–8 sentences)

Key design decisions:
- Distinguishes label-based risks (SPL units) from literature-derived cautions.
- Explicitly asks the model to call out unknowns for chronic/off-label exposure when the provided units give grounds for concern.
- Instructs the model to acknowledge narrow coverage explicitly ("the units reviewed here do not cover…").

### `pump-science-review-statement-evaluation.md`

**Used by:** `review.py` Pass 3  
**Input:** Compact JSON bundle (grounding text, risk text, score, tag counts, coverage, timestamp)  
**Output:** One prose paragraph (3–5 sentences)

Key design decisions:
- Score-to-language translation is explicit in the prompt:
  ```
  score ≥ 0.75: "meaningful support"
  0.5–0.74:     "moderate support"
  < 0.5:        "limited support"
  ```
- Model must synthesize, not paste — no repeating full citation lists from the prior paragraphs.
- Coverage section tells the model which data source categories were present or absent.

### `pump_science_longevity_evaluation_prompt.md` (optional, not wired into pipeline)

A holistic single-prompt evaluation of an entire prepared JSON (`agent` or `review` bundle) — executive summary, evidence map, risks, etc. Use when you want a one-completion assessment outside the JSONL pipeline. Not called by any script.

---

## 9. Environment variables

| Variable | Used by | Default | Purpose |
|----------|---------|---------|---------|
| `VLLM_BASE_URL` | `tag.py`, `review.py` | `http://localhost:8000/v1` | OpenAI-compatible API base URL |
| `VLLM_API_KEY` | `tag.py`, `review.py` | `none` | API key (often unused for local vLLM) |
| `TAGGER_MODEL` | `tag.py` | falls back to `CLASSIFIER_MODEL` → `VALIDATOR_MODEL` → `mixtral-8x7b-instruct` | Model for tagging (both rounds unless `TAGGER_RISK_MODEL` is set) |
| `TAGGER_RISK_MODEL` | `tag.py` | *(same as `TAGGER_MODEL`)* | Override model for risk severity round only |
| `TAGGER_MAX_TOKENS` | `tag.py` | `2048` | Completion budget; increase for long reasoning traces |
| `TAGGER_RISK_RETRIES` | `tag.py` | `3` | Re-ask attempts when risk output fails allowlist parsing |
| `TAGGER_SEED` | `tag.py` | *(unset)* | Passed as `seed` when the server supports it (digits only) |
| `REVIEWER_MODEL` | `review.py` | falls back to `TAGGER_MODEL` → `CLASSIFIER_MODEL` → `VALIDATOR_MODEL` → `mixtral-8x7b-instruct` | Model for all three review passes |
| `REVIEWER_MAX_TOKENS` | `review.py` | `2048` | Completion budget per review pass |
| `CLASSIFIER_MODEL` | `tag.py`, `review.py` | — | Legacy fallback model name |
| `VALIDATOR_MODEL` | `tag.py`, `review.py` | `mixtral-8x7b-instruct` | Last-resort fallback model name |

`tag.py` and `review.py` both load `.env` from the repo root via `python-dotenv` if it is installed (gracefully skipped if not).

---

## 10. Data schemas

### `units.jsonl` (slim row)

```json
{
  "compound_name": "Doxycycline",
  "unit_sequence": { "index": 0, "total": 93 },
  "unit_id": "epmc_r001",
  "unit_type": "literature",
  "prepared_file": "prepared_report_20260419_062655Z_agent.json",
  "provenance": { "provider": "Europe PMC", "primary_id": "pmid:12345678", "id_type": "pmid" },
  "payload": { "title": "...", "year": "2023", "doi": "10.1234/...", "abstract_excerpt_truncated": "...", ... }
}
```

### `units_tagged.jsonl`

Same as `units.jsonl` plus:

```json
{
  "report_section":    "evidence_rationale | clinical_human_data | mechanism_pathway_context | safety_labeling | surveillance_signal | null",
  "decision_relevance": "supports_exploration | raises_caution | risk_information | mixed_or_unclear | context_only | null",
  "risk_severity":     "n/a | negligible | low | moderate | high | severe | null"
}
```

`null` means parsing failed after all retries. `risk_severity` is omitted (not present) only if `--skip-risk` was passed.

### `grouped_by_stance.json`

See [Stage 5 output schema](#output-schema-1).

### `<Compound>-review.json`

See [Stage 6 output schema](#output-schema-2).

---

## 11. Known edge cases and gotchas

### vLLM `generation_config.json` overrides `temperature`

vLLM may load a model-bundled `generation_config.json` (e.g. `temperature: 0.6`) and ignore the client's `temperature=0`. The scripts send `temperature=0`, `top_p=1`, `top_k=-1`, but these only take effect if vLLM is launched with `--generation-config vllm`. If your tags are inconsistent across runs, this is the first thing to check.

### `risk_severity` is `null` in output

This means all `TAGGER_RISK_RETRIES` attempts produced output that `parse_risk_enum` could not match to the allowlist. Common causes:
- Model outputs a multi-word phrase instead of the single token (e.g. "low risk" instead of `low`)
- Model outputs the token surrounded by markdown backticks — the parser does strip backtick fences (```` ``` ````), but inline backticks may not be caught
- `TAGGER_MAX_TOKENS` is too low and the response is truncated before the token

To debug: run `tag.py` on a single unit with a high `TAGGER_MAX_TOKENS` value and inspect the raw response.

### `coverage` is `null` in review output

`review.py` could not find a `prepared_report_*.json` in the compound directory. The review still completes but the Pass 3 context bundle has `"coverage": null`. The review-statement prompt handles this gracefully. To fix: run `prepare.py` in `review` format (not just `agent`) for the same report before running `review.py`.

### Windows path issues with `discover.py`

`discover.py` uses `os.path.abspath(__file__)` (not `Path(__file__).resolve()`) to anchor `_SCRIPT_DIR` because `pathlib` can produce drive-relative paths on Windows when the working directory is on a different drive. All compound output directories are constructed from this absolute anchor.

### Europe PMC `citedByCount` as float

Some Europe PMC API responses return `citedByCount` as a float string (e.g. `"12.0"`). `prepare.py`'s `_article_score` function handles this:

```python
# In discover.py pick() helper:
def cite(x):
    try: return int(x)
    except (TypeError, ValueError):
        try: return int(float(str(x)))
        except: return 0
```

### `evaluation_units` ordering

Units are ordered deterministically: literature rows first (by relevance_score descending), then clinical trials (has_results first, then NCT id), then KEGG summary, then KEGG pathways, then SPL mechanism, then SPL risk rows, then FAERS headline. This ordering affects `unit_sequence.index` in JSONL but has no other semantic effect.

### Multiple `report_*.json` files in a compound directory

`prepare.py --format agent` processes **all** matching reports when run without arguments. `review.py` looks for the most recently modified `prepared_report_*.json` when loading coverage context. If you have multiple prepared files, the most recent by file mtime is used. To be explicit, point `review.py` at the specific `grouped_by_stance.json` produced from the desired prepared file.

---

## 12. Developer guide: adding features and fixing bugs

### Adding a new section tag or stance tag

1. Edit `prompts/compound-excerpt-tagging.md`: add the new token to the appropriate `Tags:` block. The block must remain a blank-line-separated list directly after `Tags:`.
2. No code changes needed in `tag.py` — allowlists are loaded from the prompt file at runtime.
3. Update `group_by_stance.py` if the new stance tag needs to appear in the `by_stance` output (currently it only buckets known stances plus `unmapped`).
4. If the new stance should feed into `review.py` passes, update `review.py`'s member selection logic (lines 258–261).

### Adding a new risk severity level

1. Edit `prompts/compound-risk-profile.md`: add the new token to the `Tags:` block.
2. `tag.py` loads the risk allowlist from the prompt file dynamically — no code change needed.
3. If the new level needs special handling downstream (e.g. a new aggregate score), add it to `group_by_stance.py`.

### Adding a new API source to `discover.py`

1. Add the fetch logic in `run()` following the existing pattern: attempt the request, record failures in `fail`, set the key to `None` on failure.
2. Add the new key to the return dict.
3. Add a coverage entry in `prepare.py`'s `_build_coverage()` function with `present` logic and a failure-matching `reason`.
4. Decide whether the new source should produce `evaluation_units`: if so, add a builder in `prepare.py`'s `_build_evaluation_units()` with appropriate `unit_type` and `unit_id` format.

### Changing digest sizes / token budget

Edit the constants at the top of `prepare.py` (lines 32–67). The most commonly adjusted:
- `LITERATURE_DIGEST_MAX` — increase if you want more literature coverage at the cost of larger context
- `LITERATURE_ABSTRACT_MAX` — increase if abstract truncation is hiding key information
- `RISK_ADVERSE_EXCERPT_MAX` — the largest single block; reduce if context is too large for your model

### Fixing `strip_reasoning_markup`

The same function is copy-pasted into `tag.py` and `review.py`. If you fix a bug there, update both files. A future refactor should extract it into a shared `utils.py`.

### Adding a fourth review pass

`review.py` currently runs three sequential completions. To add a fourth:
1. Add a new prompt file under `prompts/`.
2. Add a `FOURTH_PROMPT_PATH` constant to `review.py`.
3. Call `call_llm` after Pass 3 with the new prompt.
4. Add the new field to the output `review` dict.
5. Document the new pass in this file under [Stage 6](#7-stage-6--review-reviewpy).

### Debugging tag quality

To inspect what the model actually outputs before parsing:
1. Add a `print(repr(raw), file=sys.stderr)` line immediately after the `chat_completion` call in `tag.py`.
2. Run on a single-unit JSONL file.
3. Check whether the model is producing the right tokens but with unexpected surrounding text, or whether it is producing the wrong tokens entirely.

For wrong tokens: adjust the prompt. For unexpected surrounding text: adjust `strip_reasoning_markup` or `_normalize_tag_token`.

### Running the pipeline on a new compound

```bash
cd pump-science

# 1. Fetch data
python discover.py --compound "your-compound"

# 2. Prepare (inspect output before proceeding)
python prepare.py "your-compound/report_*.json" --format agent

# 3. List units
python list.py "your-compound/prepared_*_agent.json" -o "your-compound/units.jsonl"

# 4. Tag (requires vLLM running; set TAGGER_MODEL)
export VLLM_BASE_URL=http://localhost:8000/v1
export TAGGER_MODEL=your-model-id
python tag.py "your-compound/units.jsonl" -o "your-compound/units_tagged.jsonl"

# 5. Group
python group_by_stance.py "your-compound/units_tagged.jsonl"

# 6. Review
python review.py "your-compound/grouped_by_stance.json"
```
