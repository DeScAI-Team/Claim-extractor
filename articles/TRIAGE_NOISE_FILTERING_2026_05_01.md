# Triage Noise Filtering & Boilerplate Bucket

**File changed:** `articles/pipeline/empirical/triage.py`
**Date:** 2026-05-01

---

## Problem

On document (10), 131 of 152 claims had `relevancy_score >= 0.95`, yet many were
clearly not empirical findings worth grading against cited literature:

- Routine protocol details routed to the evidence pipeline:
  `"Larvae were incubated at 28.5°C for 5 days"` (relevancy 0.3)
  `"Wild-type zebrafish were maintained at 28.5°C"` (relevancy 0.3)
- Figure-caption text extracted as claims:
  `"In all three graphs, statistical values indicate significant differences..."` (relevancy 0.95)
- Forward expectations stated as findings:
  `"BMAA treatment is expected to significantly reduce locomotion"` (relevancy 0.95)
- Acronym definitions:
  `"ALS stands for amyotrophic lateral sclerosis"` (relevancy 0.2)

The original noise gate only filtered three conditions: `claim_type == "None"`,
empty `claim_classification_1`, and `relevancy_score < 0.3`. Everything above those
thresholds flowed straight into evidence grading.

---

## What changed

### 1. New filtering constants

Six named constants were added after the existing tag-set block, making every
threshold and keyword list easy to tune without touching logic:

| Constant | Value | Purpose |
|---|---|---|
| `FIGURE_TABLE_SECTION_KEYWORDS` | `("figure", "table")` | Section headings that signal a caption |
| `FIGURE_TABLE_CLAIM_PREFIXES` | `("figure", "table", "in all ")` | Claim-text prefixes that signal a caption |
| `EXPECTATION_PHRASES` | 8 phrases | Substrings marking anticipation, not a reported result |
| `BOILERPLATE_METHOD_RELEVANCY_THRESHOLD` | `0.5` | Relevancy ceiling for routing SOP method claims |
| `ABSTRACT_DEDUP_SIMILARITY_THRESHOLD` | `0.85` | SequenceMatcher ratio for near-duplicate detection |
| `KEY_MESSAGES_SECTION_KEYWORDS` | `("key message", "key finding")` | Headings whose claims may duplicate the abstract |

### 2. New helper functions

Five helper functions were inserted between `_relevancy_below_threshold` and
`assign_bucket`:

**`_normalize_claim_text(text)`**
Lowercases and collapses whitespace. Used only by the deduplication pass.

**`_is_figure_table_caption(rec)`**
Returns `True` when `section_heading` contains "figure" or "table", OR when the
claim text starts with "figure", "table", or "in all ". Targets caption fragments
that the extractor occasionally lifts out of figure legends.

**`_is_expectation_claim(rec)`**
Returns `True` when the claim text contains any phrase from `EXPECTATION_PHRASES`
(e.g. "is expected to", "are anticipated to"). These are hypotheses stated before
results were collected, not empirical findings.

**`_quality_gate_reason(rec)`**
Composes the two checks above into a single gate that returns a short reason string
(`"figure_table_caption"` or `"expectation_claim"`) or `None`.

**`_is_boilerplate_method(rec)`**
Returns `True` when `semantic_category == "method"` **and**
`relevancy_score < 0.5`. Identifies routine SOP steps (temperatures, reagent kits,
feed schedules, light cycles) that are factually true and reproducibility-relevant
but are not verified against cited literature.

### 3. New `_deduplicate_key_messages()` pre-pass

Added immediately before `assign_bucket` and `_noise_gate_reason`. For each
dimension's member list it:

1. Collects normalised text of every claim with `semantic_category == "abstract"`.
2. For every claim whose `section_heading` contains `"key message"` or
   `"key finding"`, runs `difflib.SequenceMatcher` against every abstract text.
3. If the similarity ratio is ≥ `ABSTRACT_DEDUP_SIMILARITY_THRESHOLD` (0.85), the
   record is shallow-copied and tagged with `_noise_reason = "duplicate_of_abstract"`.

The injected `_noise_reason` is picked up in `triage_grouped`'s per-record loop
(Gate 1) and routes the claim to noise before any bucket assignment.

No new dependencies — `difflib` and `re` are both Python stdlib.

### 4. Updated routing order in `triage_grouped()`

The per-record loop now has three gates before `assign_bucket`:

```
Gate 1 — structural noise
    _noise_gate_reason()  →  claim_type_none / empty_classification / low_relevancy (<0.3)
    _noise_reason field   →  duplicate_of_abstract  (set by dedup pre-pass)

Gate 2 — boilerplate SOP methods  →  boilerplate_method bucket  (NOT noise)
    _is_boilerplate_method()  →  semantic_category==method AND relevancy<0.5

Gate 3 — quality noise
    _quality_gate_reason()  →  figure_table_caption / expectation_claim

assign_bucket()  →  empirical / methodological / aspirational / contextual (unchanged)
```

### 5. New `boilerplate_method` bucket

Instead of discarding routine SOP claims, they are routed to a dedicated
`boilerplate_method` bucket. Claims like "RNA was extracted using the Qiagen RNeasy
kit" are true, important for reproducibility, but are not something you verify
against cited literature. Preserving them in a named bucket means:

- They appear in `triaged.json` and are inspectable.
- `retrieve_compare.py` downstream skips them (it iterates bucket keys and
  `boilerplate_method` is simply not in its processing set).
- The bucket can be promoted to evidence grading in the future without re-running
  upstream steps.

`boilerplate_method` is included in `stats` and the `_print_stats_summary()` totals.

---

## Noise reason codes

| Code | Meaning |
|---|---|
| `claim_type_none` | (pre-existing) claim_type is the string "None" |
| `empty_claim_classification_1` | (pre-existing) no primary classification tag |
| `low_relevancy` | (pre-existing) relevancy_score < 0.3 |
| `duplicate_of_abstract` | Key Messages claim ≥ 0.85 similar to an abstract claim |
| `figure_table_caption` | Section heading or claim text signals a figure/table caption |
| `expectation_claim` | Claim contains an anticipation/expectation phrase |

---

## What was NOT changed

- `assign_bucket()` — signature and logic are unchanged.
- `retrieve_compare.py` — no changes; it iterates `buckets` by key and naturally
  skips any key it does not recognise.
- All existing test inputs and outputs remain valid; the only structural change to
  `triaged.json` is the addition of the `boilerplate_method` list inside `buckets`
  and a matching key in `stats`.

---

## Observed effect on document (10)

Running the updated triage against `document (10)/pipe-test/grouped.json`:

```
scientific_rigor:  total=137  empirical=89  methodological=10
                   boilerplate_method=5  contextual=14  aspirational=15  noise=4
ALL aggregate:     empirical=144  methodological=11  boilerplate_method=6
                   contextual=15  aspirational=24  noise=9  total=209
```

Six SOP method claims were diverted to `boilerplate_method` rather than passing
through evidence grading, and several additional claims were caught by the quality
gate and moved to noise.
