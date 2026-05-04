# Self-Reported Evidence Grading — Expansion

**File changed:** `pipeline/empirical/retrieve_compare.py`  
**Date:** 2026-05-01

---

## Background

The pipeline assigns an `evidence_grade` to every triaged claim. When a claim has no inline citations (`cites` is empty), it previously fell into one of two buckets:

| Grade | Prior condition |
|---|---|
| `self_reported` | `claim_type == "Fact"` AND `semantic_category` in `{"abstract", "results"}` AND tags intersect `{"Observational", "Measurement"}` |
| `unreferenced` | everything else |

This was too narrow. It missed three real classes of citation-free claims:

1. **Method-section claims** — protocol steps (Zebrafish Husbandry, Sample Preparation, Solvent Toxicity Assessment, etc.) never need external citations but were being graded `unreferenced`.
2. **Result/abstract claims with common empirical tags** — tags like `Causal`, `Comparative`, and `Correlational` are typical in results sections but were excluded from `self_reported`.
3. **"Other" semantic-category claims** — Key Messages and similar sections store `semantic_category = "other"` even though their headings clearly identify them as summaries of the paper's own findings.

There was also a typo: the constant checked for `"results"` but the actual stored value is `"result"` (singular).

---

## What Changed

### 1. Module-level constants

```python
# Before
SELF_REPORTED_SEMANTIC: frozenset[str] = frozenset({"abstract", "results"})   # "results" typo
SELF_REPORTED_FACT_TAGS: frozenset[str] = frozenset({"Observational", "Measurement"})

# After
SELF_REPORTED_SEMANTIC: frozenset[str] = frozenset({"abstract", "result"})    # typo fixed
SELF_REPORTED_FACT_TAGS: frozenset[str] = frozenset(
    {"Observational", "Measurement", "Causal", "Comparative", "Correlational"}
)

METHOD_HEADING_HINTS: tuple[str, ...] = (
    "method", "material", "approach", "protocol", "procedure",
    "husbandry", "preparation", "processing", "assessment", "condition",
    "experiment", "solvent", "efficacy", "toxicity",
)
RESULT_HEADING_HINTS: tuple[str, ...] = (
    "result", "finding", "outcome", "observation",
    "abstract", "key message", "summary",
)
```

`METHOD_HEADING_HINTS` and `RESULT_HEADING_HINTS` are substring lists tested against `section_heading` (lowercased) when `semantic_category` is `"other"` or absent.

---

### 2. New helper — `_is_method_section(rec)`

Returns `True` when a claim lives in a methods-like section, checking `semantic_category == "method"` first, then falling back to substring matching on `section_heading` using `METHOD_HEADING_HINTS`.

---

### 3. Broadened `_is_self_reported_fact_claim(rec)`

- `claim_type` is now checked first (fail-fast).
- If `semantic_category` is not in `{"abstract", "result"}`, the function falls back to matching `section_heading` against `RESULT_HEADING_HINTS` instead of immediately returning `False`. This captures Key Messages and any other `"other"` chunks whose headings identify them as summaries.
- Uses the broadened `SELF_REPORTED_FACT_TAGS`.

---

### 4. New helper — `_self_reported_method_summary(rec)`

Produces a human-readable summary that names the specific section, e.g.:

> Procedural description of the paper's own methodology (Zebrafish Husbandry); external citations are not expected for protocol details.

---

### 5. Updated `enrich_triaged()` — both empty-`cites` branches

The priority order when `cites` is empty is now:

```
1. _is_method_section(rec)         → evidence_grade = "self_reported_method"
2. _is_self_reported_fact_claim(rec) → evidence_grade = "self_reported"
3. otherwise                       → evidence_grade = "unreferenced"  (or "pending" in skip_llm mode)
```

This applies identically to the `skip_llm` fast-path and the main LLM path.

---

## Grade Semantics After This Change

| Grade | Meaning |
|---|---|
| `self_reported_method` | Protocol/procedure description in a methods section; no external citation expected. |
| `self_reported` | Empirical finding reported by the paper itself (abstract, results, key messages); no external citation expected. |
| `unreferenced` | Claim in introduction/discussion/background that genuinely should cite something but does not. |

---

## Sections Affected by This Fix

| Section heading (examples) | Before | After |
|---|---|---|
| Abstract | `self_reported` (if Observational/Measurement only) | `self_reported` (Causal/Comparative/Correlational now included) |
| Key Messages | `unreferenced` | `self_reported` (heading fallback) |
| Results | `self_reported` (narrow tags) | `self_reported` (broadened tags) |
| Zebrafish Husbandry | `unreferenced` | `self_reported_method` |
| Sample Preparation | `unreferenced` | `self_reported_method` |
| Solvent Toxicity Assessment | `unreferenced` | `self_reported_method` |
| Product Efficacy Assessment | `unreferenced` | `self_reported_method` |
| Data Processing | `unreferenced` | `self_reported_method` |
| Experimental Conditions | `unreferenced` | `self_reported_method` |
