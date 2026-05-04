#!/usr/bin/env python3
r"""Group tagged evaluation-unit JSONL by ``decision_relevance`` (stance).

Input: JSONL from ``tag.py`` (each line adds ``report_section`` / ``decision_relevance``).
Output: JSON object keyed by stance, same shape spirit as ``group-and-score/group.py``
(``members`` arrays + a per-group ``count``). Rows with missing or unknown stance go under
``unmapped``.

Also computes **scientific_grounding** ``score`` = ``supports_exploration`` count divided by
(``supports_exploration`` + ``raises_caution``), rounded to two decimals (null if both counts are zero).

Default output path: same folder as input → ``grouped_by_stance.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Must match prompts/compound-excerpt-tagging.md stance allowlist (order preserved in output).
KNOWN_STANCES: tuple[str, ...] = (
    "supports_exploration",
    "raises_caution",
    "risk_information",
    "mixed_or_unclear",
    "context_only",
)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def group_by_stance(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {s: [] for s in KNOWN_STANCES}
    buckets["unmapped"] = []

    for rec in rows:
        raw = rec.get("decision_relevance")
        key = raw if isinstance(raw, str) and raw in KNOWN_STANCES else "unmapped"
        buckets[key].append(rec)

    out: dict[str, dict[str, Any]] = {}
    for s in KNOWN_STANCES:
        members = buckets[s]
        out[s] = {"count": len(members), "members": members}
    out["unmapped"] = {"count": len(buckets["unmapped"]), "members": buckets["unmapped"]}
    return out


RISK_SEVERITY_ORDER: tuple[str, ...] = (
    "negligible",
    "low",
    "moderate",
    "high",
    "severe",
)

# tag index is 1-based (negligible=1 … severe=5); weight = 2^(tag-1) - 1
_RISK_WEIGHTS: dict[str, int] = {
    label: (2 ** idx) - 1
    for idx, label in enumerate(RISK_SEVERITY_ORDER, start=1)
}
# negligible→0, low→1, moderate→3, high→7, severe→15
_MAX_WEIGHT_PER_UNIT = 15  # 2^(5-1) - 1


def aggregate_risk_score(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Score = sum(2^(tag_i - 1) - 1) / (n × 15) × 100.

    ``n/a`` and ``null`` rows are excluded from both the numerator and
    denominator, so only passages with an actual severity label contribute.
    """
    total_weight = 0
    scored_count = 0
    severity_counts: dict[str, int] = {s: 0 for s in RISK_SEVERITY_ORDER}
    skipped_na = 0
    skipped_null = 0

    for rec in rows:
        sev = rec.get("risk_severity")
        if sev is None:
            skipped_null += 1
            continue
        if sev == "n/a":
            skipped_na += 1
            continue
        weight = _RISK_WEIGHTS.get(sev)
        if weight is None:
            # Unknown label — treat as skipped rather than crash.
            skipped_null += 1
            continue
        total_weight += weight
        scored_count += 1
        severity_counts[sev] += 1

    max_possible = scored_count * _MAX_WEIGHT_PER_UNIT
    score = round(total_weight / max_possible, 2) if max_possible else None

    return {
        "scored_unit_count": scored_count,
        "skipped_na_count": skipped_na,
        "skipped_null_count": skipped_null,
        "severity_counts": severity_counts,
        "total_weight": total_weight,
        "max_possible_weight": max_possible,
        "score": score,
    }


def scientific_grounding_score(grouped: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Score = supports_exploration / (supports_exploration + raises_caution), 2 decimal places."""
    n_sup = int(grouped["supports_exploration"]["count"])
    n_caut = int(grouped["raises_caution"]["count"])
    denom = n_sup + n_caut
    score = round(n_sup / denom, 2) if denom else None
    return {
        "supports_exploration_count": n_sup,
        "raises_caution_count": n_caut,
        "support_and_caution_total": denom,
        "score": score,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Group pump-science tagged JSONL by decision_relevance (stance)."
    )
    parser.add_argument(
        "input",
        type=Path,
        nargs="?",
        default=None,
        help="Tagged JSONL (default: pump-science/Doxycycline/units_tagged.jsonl)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Write JSON here (default: <input_dir>/grouped_by_stance.json)",
    )
    ns = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    default_in = repo_root / "data" / "Doxycycline" / "units_tagged.jsonl"
    input_path = (ns.input or default_in).expanduser().resolve()

    if not input_path.is_file():
        print(f"group_by_stance: not found: {input_path}", file=sys.stderr)
        return 1

    rows = load_jsonl(input_path)
    grouped = group_by_stance(rows)

    compound = None
    if rows:
        cn = rows[0].get("compound_name")
        compound = cn if isinstance(cn, str) else None

    sg = scientific_grounding_score(grouped)
    ars = aggregate_risk_score(rows)

    payload: dict[str, Any] = {
        "$schema_hint": "pump-science.grouped_by_stance.v1",
        "compound_name": compound,
        "source_tagged_file": input_path.name,
        "total_units": len(rows),
        "scores": {
            "scientific_grounding": sg,
            "aggregate_risk": ars,
        },
        "by_stance": grouped,
    }

    out_path = ns.output
    if out_path is None:
        out_path = input_path.parent / "grouped_by_stance.json"
    else:
        out_path = out_path.expanduser().resolve()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"group_by_stance: wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
