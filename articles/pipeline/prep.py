"""Add LLM-oriented narrative text to each claim in grouped JSON from group.py."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = REPO_ROOT / "prompts" / "claim_llm_narrative_template.md"

# Must match the table in claim_llm_narrative_template.md
RELEVANCY_TIERS: tuple[tuple[float, str], ...] = (
    (0.2, "low relevancy"),
    (0.4, "slightly relevant"),
    (0.6, "moderately relevant"),
    (0.8, "very relevant"),
    (1.0, "extremely relevant"),
)


def load_sentence_template(template_path: Path) -> str:
    text = template_path.read_text(encoding="utf-8")
    if "## Sentence template" not in text:
        raise ValueError(f"No '## Sentence template' section in {template_path}")
    after = text.split("## Sentence template", 1)[1]
    for line in after.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("|"):
            return line
    raise ValueError(f"No template line after '## Sentence template' in {template_path}")


def relevancy_label(score: object) -> str:
    try:
        s = float(score)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "relevancy unknown"
    s = max(0.0, min(1.0, s))
    for upper, label in RELEVANCY_TIERS:
        if s < upper or upper == 1.0 and s <= 1.0:
            return label
    return "relevancy unknown"


def verdict_phrase(verdict: object) -> str:
    if verdict is None:
        return "unknown"
    return str(verdict).strip().replace("_", " ")


def normalize_rationale(raw: str) -> str:
    s = raw.strip()
    if not s:
        s = "no rationale recorded"
    return s if s.endswith(".") else s + "."


def format_claim_narrative(template: str, rec: dict) -> str:
    doc = str(rec.get("doc_name") or "This document").strip().replace("_", " ")
    claim = str(rec.get("claim") or "")
    raw_section = rec.get("section_heading")
    section = (
        str(raw_section).strip().replace("_", " ")
        if raw_section is not None and str(raw_section).strip()
        else "unspecified"
    )
    verdict = verdict_phrase(rec.get("verdict"))
    rationale = normalize_rationale(str(rec.get("rationale") or ""))
    rel = relevancy_label(rec.get("relevancy_score"))

    def sub(field: str, value: str) -> None:
        nonlocal template
        template = template.replace("{" + field + "}", value)

    sub("doc_name", doc)
    sub("claim", claim)
    sub("section_heading", section)
    sub("verdict", verdict)
    sub("rationale", rationale)
    sub("relevancy_label", rel)
    if "{" in template:
        bad = re.findall(r"\{([^}]+)\}", template)
        if bad:
            raise ValueError(f"Unresolved placeholders in template: {bad}")
    return template


def enrich_grouped(
    grouped: dict[str, dict], sentence_template: str
) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for group_id, payload in grouped.items():
        if not isinstance(payload, dict) or "members" not in payload:
            raise ValueError(
                f"Group {group_id!r}: expected object with 'members' (group.py output)."
            )
        score = payload.get("score")
        members = []
        for rec in payload["members"]:
            if not isinstance(rec, dict):
                members.append(rec)
                continue
            row = dict(rec)
            row["claim_narrative"] = format_claim_narrative(sentence_template, rec)
            members.append(row)
        out[group_id] = {"score": score, "members": members}
    return out


def main() -> None:
    p = argparse.ArgumentParser(
        description="Add claim_narrative to each member of grouped claim JSON."
    )
    p.add_argument(
        "grouped_json",
        help="Path to JSON from group.py, or '-' to read from stdin (for pipes on Windows)",
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Write enriched JSON here (default: stdout)",
    )
    p.add_argument(
        "--template",
        type=Path,
        default=DEFAULT_TEMPLATE,
        help="Markdown file with ## Sentence template section",
    )
    args = p.parse_args()

    sentence = load_sentence_template(args.template)
    if args.grouped_json.strip() == "-":
        grouped = json.load(sys.stdin)
    else:
        grouped = json.loads(
            Path(args.grouped_json).expanduser().resolve().read_text(encoding="utf-8")
        )
    if not isinstance(grouped, dict):
        raise ValueError("Root JSON must be an object keyed by group id.")

    enriched = enrich_grouped(grouped, sentence)
    text = json.dumps(enriched, indent=2, ensure_ascii=False) + "\n"

    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
