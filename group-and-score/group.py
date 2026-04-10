"""Group classified claims by dimension using mappings.json tag_index."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

CLASSIFICATION_KEYS = (
    "claim_classification_1",
    "claim_classification_2",
    "claim_classification_3",
)


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


def group_verdict_score(members: list[dict]) -> float | None:
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
        out[g] = {"score": group_verdict_score(members), "members": members}
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
    records: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        records.append(json.loads(line))

    scanned = claim_scan(records, tag_index)
    grouped = claim_group(records, scanned)
    text = json.dumps(grouped, indent=2, ensure_ascii=False) + "\n"

    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
