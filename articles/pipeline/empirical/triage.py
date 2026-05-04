"""Deterministic triage of grouped.json claims into empirical review buckets.

Buckets (first match wins: empirical → methodological → boilerplate_method →
aspirational → contextual):

- **empirical**: Claims verifiable with data — Fact with evidential/method-style data
  tags, or Assertion with causal/comparative/mechanistic/performance/observational tags.
- **methodological**: How the work was done (rigor), not empirical truth — Fact or
  Assertion with Methodological, Measurement paired with Methodological, or Assertion
  with Benchmark as the sole classification tag.
- **boilerplate_method**: Standard operating procedure steps (temperatures, reagent kits,
  feed schedules, light cycles) — semantic_category == "method" with relevancy < 0.5.
  True for reproducibility, but not verified against cited literature.
- **aspirational**: Gaps, hypotheses, novelty, future work — Roadmap claim type, or
  Fact/Assertion with gap/novelty/future/feasibility/impact tags.
- **contextual**: Background, definitions, framing — Fact/Assertion with definitional
  /background/synthesis/source tags, or Assertion with interpretive/prescriptive/hedge.

Noise: low relevancy, missing primary tags, claim_type None, figure/table captions,
expectation-only claims, Key Messages near-duplicate of abstract, or no bucket match.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path
from typing import Any, TextIO

CLASSIFICATION_KEYS = (
    "claim_classification_1",
    "claim_classification_2",
    "claim_classification_3",
)

# Dominance heuristic: warn when contextual+aspirational outnumber empirical on
# substantive empirical-route triage (min total in those three buckets).
DOMINANCE_WARN_MIN_TRIAD_TOTAL = 5

EMPIRICAL_FACT_TAGS: frozenset[str] = frozenset(
    {
        "Causal",
        "Correlational",
        "Comparative",
        "Mechanistic",
        "Performance",
        "Benchmark",
        "Measurement",
        "Observational",
        "NullFinding",
        "Replication",
    }
)

EMPIRICAL_ASSERTION_TAGS: frozenset[str] = frozenset(
    {
        "Causal",
        "Correlational",
        "Comparative",
        "Mechanistic",
        "Performance",
        "Observational",
    }
)

ASPIRATIONAL_TAGS: frozenset[str] = frozenset(
    {
        "GapStatement",
        "Hypothesis",
        "NoveltyAssertion",
        "FutureWork",
        "Roadmap",
        "Feasibility",
        "ImpactPotential",
    }
)

CONTEXTUAL_FACT_ASSERTION_TAGS: frozenset[str] = frozenset(
    {
        "Definitional",
        "Background",
        "Synthesis",
        "SourceAttribution",
    }
)

CONTEXTUAL_ASSERTION_ONLY_TAGS: frozenset[str] = frozenset(
    {
        "Interpretive",
        "Prescriptive",
        "Hedge",
    }
)

# --- Quality / boilerplate filter constants ---

# Section heading substrings (lowercased) that indicate figure or table captions.
FIGURE_TABLE_SECTION_KEYWORDS: tuple[str, ...] = ("figure", "table")

# Claim text prefixes (lowercased) that indicate a caption rather than a claim.
FIGURE_TABLE_CLAIM_PREFIXES: tuple[str, ...] = ("figure", "table", "in all ")

# Phrase substrings (lowercased) that mark expectation/anticipation rather than a
# reported result.  "should be" is intentionally excluded to avoid false positives on
# methodological recommendations.
EXPECTATION_PHRASES: tuple[str, ...] = (
    "is expected to",
    "are expected to",
    "was expected to",
    "were expected to",
    "is anticipated to",
    "are anticipated to",
    "should result in",
    "should show",
)

# Relevancy threshold below which a method-category claim is routed to the
# boilerplate_method bucket instead of passing through the full quality gate.
BOILERPLATE_METHOD_RELEVANCY_THRESHOLD: float = 0.5

# SequenceMatcher ratio at or above which a Key Messages claim is considered a
# near-duplicate of an abstract claim and is sent to noise.
ABSTRACT_DEDUP_SIMILARITY_THRESHOLD: float = 0.85

# Section heading substrings (lowercased) that identify Key Messages / Key Findings
# sections whose claims may duplicate the abstract.
KEY_MESSAGES_SECTION_KEYWORDS: tuple[str, ...] = ("key message", "key finding")


def load_known_tags(mappings_path: Path) -> frozenset[str]:
    """All tag strings declared in mappings.json dimensions and cross_cutting."""
    data = json.loads(mappings_path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for dim in (data.get("dimensions") or {}).values():
        if isinstance(dim, dict):
            for t in dim.get("tags") or []:
                out.add(str(t))
    cc = data.get("cross_cutting")
    if isinstance(cc, dict):
        for t in cc.get("tags") or []:
            out.add(str(t))
    return frozenset(out)


def _tags_for_member(rec: dict) -> frozenset[str]:
    tags: set[str] = set()
    for key in CLASSIFICATION_KEYS:
        part = rec.get(key) or []
        if isinstance(part, list):
            for x in part:
                tags.add(str(x).strip())
    return frozenset(tags)


def _claim_classification_1_empty(rec: dict) -> bool:
    c1 = rec.get("claim_classification_1")
    return not isinstance(c1, list) or len(c1) == 0


def _relevancy_below_threshold(rec: dict, threshold: float = 0.3) -> bool:
    r = rec.get("relevancy_score")
    try:
        v = float(r)
    except (TypeError, ValueError):
        return False
    return v < threshold


# ---------------------------------------------------------------------------
# Quality-filter helpers
# ---------------------------------------------------------------------------


def _normalize_claim_text(text: str) -> str:
    """Lowercase and collapse whitespace for fuzzy comparison."""
    return re.sub(r"\s+", " ", text.lower()).strip()


def _is_figure_table_caption(rec: dict) -> bool:
    """True if the record looks like a figure or table caption rather than a claim.

    Triggers on:
    - section_heading containing "figure" or "table" (case-insensitive)
    - claim text starting with "figure", "table", or "in all " (case-insensitive)
    """
    heading = str(rec.get("section_heading") or "").lower()
    if any(kw in heading for kw in FIGURE_TABLE_SECTION_KEYWORDS):
        return True
    claim_text = str(rec.get("claim") or "").lower().strip()
    return any(claim_text.startswith(prefix) for prefix in FIGURE_TABLE_CLAIM_PREFIXES)


def _is_expectation_claim(rec: dict) -> bool:
    """True if the claim expresses a forward expectation rather than a reported result."""
    claim_text = str(rec.get("claim") or "").lower()
    return any(phrase in claim_text for phrase in EXPECTATION_PHRASES)


def _quality_gate_reason(rec: dict) -> str | None:
    """Secondary noise gate for claim quality issues.

    Called after _noise_gate_reason and the boilerplate-method check, so only
    claims that passed relevancy and classification checks reach this point.

    Returns a short reason string if the claim should be sent to noise, else None.
    """
    if _is_figure_table_caption(rec):
        return "figure_table_caption"
    if _is_expectation_claim(rec):
        return "expectation_claim"
    return None


def _is_boilerplate_method(rec: dict) -> bool:
    """True for routine SOP-style method claims that need no evidence grading.

    Criteria: semantic_category is "method" AND relevancy_score < threshold.
    These claims (temperatures, reagent kits, feed schedules, light cycles) are
    factually true and important for reproducibility but are not verified against
    cited literature, so they bypass evidence grading.
    """
    if str(rec.get("semantic_category") or "").strip().lower() != "method":
        return False
    return _relevancy_below_threshold(rec, threshold=BOILERPLATE_METHOD_RELEVANCY_THRESHOLD)


def assign_bucket(claim_type: str, tags: frozenset[str]) -> str | None:
    """Return bucket name or None if no bucket matches (caller sends to noise)."""
    ct = str(claim_type).strip()

    if ct == "Fact" and (tags & EMPIRICAL_FACT_TAGS):
        return "empirical"
    if ct == "Assertion" and (tags & EMPIRICAL_ASSERTION_TAGS):
        return "empirical"

    if ct in ("Fact", "Assertion"):
        if "Methodological" in tags:
            return "methodological"
        if "Measurement" in tags and "Methodological" in tags:
            return "methodological"
        if len(tags) == 1 and "Benchmark" in tags:
            return "methodological"

    if ct == "Roadmap":
        return "aspirational"
    if ct in ("Fact", "Assertion") and (tags & ASPIRATIONAL_TAGS):
        return "aspirational"

    if ct in ("Fact", "Assertion") and (tags & CONTEXTUAL_FACT_ASSERTION_TAGS):
        return "contextual"
    if ct == "Assertion" and (tags & CONTEXTUAL_ASSERTION_ONLY_TAGS):
        return "contextual"

    return None


def _noise_gate_reason(rec: dict) -> str | None:
    """If this record is forced to noise before bucketing, return a short reason."""
    if str(rec.get("claim_type") or "").strip() == "None":
        return "claim_type_none"
    if _claim_classification_1_empty(rec):
        return "empty_claim_classification_1"
    if _relevancy_below_threshold(rec):
        return "low_relevancy"
    return None


def _deduplicate_key_messages(members: list[dict]) -> list[dict]:
    """Pre-pass: mark Key Messages claims that duplicate abstract claims as noise.

    Builds a list of normalised abstract-claim texts from the same dimension's
    members, then for every Key Messages / Key Findings record checks whether its
    normalised claim text is ≥ ABSTRACT_DEDUP_SIMILARITY_THRESHOLD similar to any
    abstract claim using difflib.SequenceMatcher.  Matching records are returned as
    shallow copies with ``_noise_reason = "duplicate_of_abstract"`` injected so the
    main loop can route them to noise without re-reading the full text.

    Non-Key-Messages records are returned unchanged (same object reference).
    """
    abstract_texts: list[str] = [
        _normalize_claim_text(m.get("claim") or "")
        for m in members
        if str(m.get("semantic_category") or "").strip().lower() == "abstract"
        and m.get("claim")
    ]

    if not abstract_texts:
        return members

    result: list[dict] = []
    for rec in members:
        heading = str(rec.get("section_heading") or "").lower()
        is_key_messages = any(kw in heading for kw in KEY_MESSAGES_SECTION_KEYWORDS)
        if not is_key_messages:
            result.append(rec)
            continue

        norm = _normalize_claim_text(rec.get("claim") or "")
        is_dup = any(
            difflib.SequenceMatcher(None, norm, abstract_text).ratio()
            >= ABSTRACT_DEDUP_SIMILARITY_THRESHOLD
            for abstract_text in abstract_texts
        )
        if is_dup:
            copy = dict(rec)
            copy["_noise_reason"] = "duplicate_of_abstract"
            result.append(copy)
        else:
            result.append(rec)

    return result


def triage_grouped(
    grouped: dict[str, Any],
    *,
    known_tags: frozenset[str],
    stderr: TextIO,
) -> dict[str, Any]:
    """Build triaged.json structure; emit unknown-tag and no-bucket lines to stderr."""
    warned_unknown: set[str] = set()
    out: dict[str, Any] = {}

    for dim_key, dim_val in grouped.items():
        if not isinstance(dim_val, dict):
            continue
        score = dim_val.get("score")
        members_raw = dim_val.get("members") or []
        if not isinstance(members_raw, list):
            members_raw = []

        # Pre-pass: mark Key Messages claims that duplicate abstract claims.
        members_raw = _deduplicate_key_messages(members_raw)

        buckets: dict[str, list[dict]] = {
            "empirical": [],
            "methodological": [],
            "boilerplate_method": [],
            "contextual": [],
            "aspirational": [],
        }
        noise: list[dict] = []

        for rec in members_raw:
            if not isinstance(rec, dict):
                continue

            tags = _tags_for_member(rec)
            for t in tags:
                if t and t not in known_tags and t not in warned_unknown:
                    warned_unknown.add(t)
                    print(
                        f'triage.py: unknown tag "{t}" (not in mappings.json)',
                        file=stderr,
                    )

            # Gate 1: structural noise (claim_type_none, empty classification,
            # low relevancy < 0.3, or flagged as duplicate-of-abstract).
            gate = _noise_gate_reason(rec)
            if gate is None:
                gate = rec.get("_noise_reason") or None
            if gate is not None:
                noise.append(dict(rec))
                continue

            # Gate 2: boilerplate SOP method claims — routed to dedicated bucket,
            # not noise, so they are preserved for reproducibility review.
            if _is_boilerplate_method(rec):
                placed = dict(rec)
                placed["triage_bucket"] = "boilerplate_method"
                buckets["boilerplate_method"].append(placed)
                continue

            # Gate 3: quality noise (figure/table captions, expectation-only claims).
            quality_gate = _quality_gate_reason(rec)
            if quality_gate is not None:
                noise.append(dict(rec))
                continue

            ct = str(rec.get("claim_type") or "").strip()
            bucket = assign_bucket(ct, tags)
            if bucket is None:
                cid = rec.get("chunk_id", "?")
                print(
                    f"triage.py: no bucket for dimension={dim_key} "
                    f"claim_type={ct!r} tags={sorted(tags)} chunk_id={cid}",
                    file=stderr,
                )
                noise.append(dict(rec))
                continue

            placed = dict(rec)
            placed["triage_bucket"] = bucket
            buckets[bucket].append(placed)

        stats = {
            "total": sum(len(buckets[k]) for k in buckets) + len(noise),
            "empirical": len(buckets["empirical"]),
            "methodological": len(buckets["methodological"]),
            "boilerplate_method": len(buckets["boilerplate_method"]),
            "contextual": len(buckets["contextual"]),
            "aspirational": len(buckets["aspirational"]),
            "noise": len(noise),
        }

        out[dim_key] = {
            "score": score,
            "buckets": buckets,
            "noise": noise,
            "stats": stats,
        }

    return out


def _print_stats_summary(triaged: dict[str, Any], stderr: TextIO) -> None:
    totals = {
        "empirical": 0,
        "methodological": 0,
        "boilerplate_method": 0,
        "contextual": 0,
        "aspirational": 0,
        "noise": 0,
        "total": 0,
    }
    for dim_key, dim_val in triaged.items():
        if not isinstance(dim_val, dict):
            continue
        st = dim_val.get("stats") or {}
        if not isinstance(st, dict):
            continue
        print(f"triage.py: {dim_key} - stats: {st}", file=stderr)
        for k in totals:
            if k in st and isinstance(st[k], int):
                totals[k] += st[k]
    print(f"triage.py: ALL - aggregate stats: {totals}", file=stderr)


def _maybe_dominance_warning(triaged: dict[str, Any], stderr: TextIO) -> None:
    empirical = contextual = aspirational = 0
    for dim_val in triaged.values():
        if not isinstance(dim_val, dict):
            continue
        st = dim_val.get("stats") or {}
        if not isinstance(st, dict):
            continue
        empirical += int(st.get("empirical") or 0)
        contextual += int(st.get("contextual") or 0)
        aspirational += int(st.get("aspirational") or 0)
    triad = empirical + contextual + aspirational
    if triad >= DOMINANCE_WARN_MIN_TRIAD_TOTAL and (contextual + aspirational) > empirical:
        print(
            "triage.py: warning: contextual+aspirational bucket counts exceed empirical "
            "across all dimensions. For empirical-route papers the empirical bucket "
            "should usually be largest; check upstream extraction or classification.",
            file=stderr,
        )


def main() -> None:
    default_mappings = Path(__file__).resolve().parent.parent / "mappings.json"
    p = argparse.ArgumentParser(
        description="Triage grouped.json claims into empirical review buckets (deterministic)."
    )
    p.add_argument("grouped_json", type=Path, help="Input grouped.json")
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Write JSON here (default: stdout)",
    )
    p.add_argument(
        "--mappings",
        type=Path,
        default=default_mappings,
        help="Path to mappings.json (known tags for warnings)",
    )
    args = p.parse_args()

    known_tags = load_known_tags(args.mappings)
    grouped = json.loads(args.grouped_json.read_text(encoding="utf-8"))
    if not isinstance(grouped, dict):
        print("triage.py: error: grouped JSON must be an object", file=sys.stderr)
        raise SystemExit(1)

    triaged = triage_grouped(grouped, known_tags=known_tags, stderr=sys.stderr)
    _print_stats_summary(triaged, sys.stderr)
    _maybe_dominance_warning(triaged, sys.stderr)

    text = json.dumps(triaged, indent=2, ensure_ascii=False) + "\n"
    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
