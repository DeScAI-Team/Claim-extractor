"""Group classified claims by dimension using mappings.json tag_index."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

CLASSIFICATION_KEYS = (
    "claim_classification_1",
    "claim_classification_2",
    "claim_classification_3",
)

# After blending verdict ratio with mean relevancy, pull score toward 0.5 when the
# dimension has few members (pseudo-count / empirical Bayes). Larger = stronger pull.
SCORE_SHRINK_PRIOR = 5.0


def _parse_relevancy_blend_exponent() -> float:
    """Exponent γ in (0, 1]: blended uses mean_rel**γ so low relevancy hurts less than linearly.

    γ=1 recovers linear `ratio × mean_rel`. γ≈0.5 is sqrt (default). Invalid or ≤0 → 0.5; >1 → 1.
    Override with env `RELEVANCY_BLEND_EXPONENT`.
    """
    raw = os.environ.get("RELEVANCY_BLEND_EXPONENT", "0.5")
    try:
        g = float(raw)
    except ValueError:
        return 0.5
    if g <= 0:
        return 0.5
    return min(g, 1.0)


RELEVANCY_BLEND_EXPONENT = _parse_relevancy_blend_exponent()

# Normalized (lower, strip, trailing ".") exact matches — extractor/validator placeholders.
_BOGUS_CLAIM_NORMALIZED: frozenset[str] = frozenset(
    {
        "no scientific claims identified in the text",
        "no scientific claims identified",
    }
)


def _is_bogus_claim(rec: dict) -> bool:
    """True for non-substantive placeholder lines that should not participate in grouping/scoring."""
    claim = str(rec.get("claim") or "").strip()
    if not claim:
        return True
    key = claim.lower().rstrip(".")
    if key in _BOGUS_CLAIM_NORMALIZED:
        return True
    return False


def load_tag_index(mappings_path: Path) -> dict[str, str]:
    data = json.loads(mappings_path.read_text(encoding="utf-8"))
    return data["tag_index"]


def _tags_for_record(rec: dict) -> list[str]:
    tags: list[str] = []
    for key in CLASSIFICATION_KEYS:
        part = rec.get(key) or []
        if isinstance(part, list):
            tags.extend(part)
    return tags


def _verdict_support_ratio(members: list[dict]) -> float | None:
    """supported / (supported + unsupported); ignores insufficient_info and other verdicts."""
    supported = unsupported = 0
    for rec in members:
        v = rec.get("verdict")
        if v == "supported":
            supported += 1
        elif v == "unsupported":
            unsupported += 1
    total = supported + unsupported
    if total == 0:
        return None
    return supported / total


def _extract_relevancy(rec: dict) -> float | None:
    """Return relevancy_score in [0, 1] if present and numeric, else None."""
    r = rec.get("relevancy_score")
    if r is None:
        return None
    try:
        v = float(r)
    except (TypeError, ValueError):
        return None
    if v < 0.0 or v > 1.0:
        return None
    return v


def _mean_member_relevancy(members: list[dict]) -> float | None:
    """Mean relevancy_score over members that have a valid numeric score."""
    vals: list[float] = []
    for rec in members:
        x = _extract_relevancy(rec)
        if x is not None:
            vals.append(x)
    if not vals:
        return None
    return sum(vals) / len(vals)


def _effective_relevancy_for_blend(mean_rel: float, exponent: float = RELEVANCY_BLEND_EXPONENT) -> float:
    """Concave transform on [0, 1]: raises low group-mean relevancy vs raw mean (exponent in (0, 1])."""
    return float(mean_rel) ** exponent


def _shrink_towards_half(score: float, n: int, prior: float = SCORE_SHRINK_PRIOR) -> float:
    """Pull score toward 0.5 when n (member count) is small: score * n/(n+prior) + 0.5 * prior/(n+prior)."""
    if n <= 0 or prior <= 0:
        return score
    w = n / (n + prior)
    return score * w + 0.5 * (1.0 - w)


def group_dimension_score(members: list[dict]) -> float | None:
    """Verdict ratio, relevancy blend, then small-n shrink toward 0.5.

    support_ratio = supported / (supported + unsupported) over members (unchanged rule).
    mean_rel = mean of relevancy_score over members with a valid 0–1 score; if none, the
    blended value is support_ratio only. If present, blend uses
    support_ratio * (mean_rel ** RELEVANCY_BLEND_EXPONENT) (default exponent 0.5 = sqrt)
    so low mean relevancy is less punishing than a straight product. Then shrink: with
    n = len(members) and prior SCORE_SHRINK_PRIOR, final = blended * n/(n+prior) + 0.5 *
    prior/(n+prior), clamped to [0, 1] and rounded to 4 decimals. insufficient_info
    unchanged for the verdict ratio.
    """
    ratio = _verdict_support_ratio(members)
    if ratio is None:
        return None
    mean_rel = _mean_member_relevancy(members)
    if mean_rel is None:
        blended = ratio
    else:
        eff_rel = _effective_relevancy_for_blend(mean_rel)
        blended = ratio * eff_rel
    n = len(members)
    shrunk = _shrink_towards_half(blended, n)
    return round(max(0.0, min(1.0, shrunk)), 4)


def claim_scan(
    records: list[dict], tag_index: dict[str, str]
) -> dict[str, list[int]]:
    """Map each line index to group ids (dimensions); unknown tags skipped."""
    out: dict[str, list[int]] = {}
    for i, rec in enumerate(records):
        groups: set[str] = set()
        for tag in _tags_for_record(rec):
            g = tag_index.get(tag)
            if g is not None:
                groups.add(g)
        for g in groups:
            out.setdefault(g, []).append(i)
    return out


def claim_group(
    records: list[dict], scanned: dict[str, list[int]]
) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for g, idxs in scanned.items():
        members = [records[i] for i in idxs]
        out[g] = {"score": group_dimension_score(members), "members": members}
    return out


def main() -> None:
    default_mappings = Path(__file__).resolve().parent / "mappings.json"
    p = argparse.ArgumentParser(description="Group classifier JSONL by dimension.")
    p.add_argument("jsonl", type=Path, help="Input JSONL (one claim object per line)")
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
        help="Path to mappings.json",
    )
    args = p.parse_args()

    tag_index = load_tag_index(args.mappings)
    lines = args.jsonl.read_text(encoding="utf-8").splitlines()
    raw: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        raw.append(json.loads(line))

    before = len(raw)
    records = [r for r in raw if not _is_bogus_claim(r)]
    dropped = before - len(records)
    if dropped:
        print(f"group.py: dropped {dropped} bogus claim record(s)", file=sys.stderr)

    scanned = claim_scan(records, tag_index)
    grouped = claim_group(records, scanned)
    text = json.dumps(grouped, indent=2, ensure_ascii=False) + "\n"

    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
