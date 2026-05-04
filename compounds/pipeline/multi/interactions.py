#!/usr/bin/env python3
"""Collect structured interaction evidence for a compound combination (data extraction only — no LLM).

For each named compound, reads pump-science/data/<compound>/:
  - report_*.json        → SPL drug-interaction and mechanism text, filtered to labels that
                           actually describe the queried compound (text-based heuristic; pending
                           the discover.py label-filter bugfix, unrelated labels are excluded here)
  - prepared_*_agent.json → KEGG longevity pathway flags and pathway name samples
  - units_tagged.jsonl   → mechanism/pathway-context tagged units
  - *-review.json        → review_statement, scientific_grounding score and rationale, risk rationale

Cross-references (no LLM):
  - KEGG longevity pathway overlap: which flags are True across 2+ compounds
  - Explicit name mentions: each compound's name tokens searched in others' interaction /
    mechanism text (catches direct labeling references between compounds)

Output: a structured JSON evidence bundle for downstream LLM compatibility synthesis.

Usage:
  python interactions.py --compounds Doxycycline "Epigallocatechin gallate" -o combo.json
  python interactions.py --compounds Metformin Rapamycin Doxycycline -o trio.json
  python interactions.py --compounds Metformin Rapamycin          # stdout
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PUMP_SCIENCE_DIR = Path(__file__).resolve().parent
_DATA_DIR = _PUMP_SCIENCE_DIR.parent.parent / "data"

_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}

# SPL fields stored in raw report that carry interaction/mechanism text.
_SPL_INTERACTION_FIELDS = ("drug_interactions",)
_SPL_MECHANISM_FIELDS = ("mechanism_of_action", "clinical_pharmacology", "pharmacokinetics")

# unit_type values (from units_tagged.jsonl) that carry mechanism / pathway context.
_MECHANISM_UNIT_TYPES = frozenset({
    "spl_mechanism_pharmacology",
    "kegg_summary",
    "kegg_pathway",
})

# Max chars kept per individual excerpt to keep the bundle tractable.
_INTERACTION_EXCERPT_MAX = 2000
_MECHANISM_EXCERPT_MAX = 1400
_SNIPPET_CONTEXT = 120   # chars around a mention match for the snippet field


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _safe_dir_name(compound: str) -> str:
    """Identical sanitization to discover.py and run_review.py."""
    safe = re.sub(r"[^\w\-.]+", "_", compound, flags=re.UNICODE).strip("._- ")[:80] or "compound"
    if safe.upper() in _WIN_RESERVED:
        safe = f"_{safe}_"
    return safe


def _name_tokens(compound: str) -> frozenset[str]:
    """Lowercase tokens from a compound name that are long enough to be distinctive (>= 4 chars)."""
    return frozenset(t.lower() for t in re.split(r"[\s\-/,]+", compound) if len(t) >= 4)


def _find_latest(directory: Path, pattern: str) -> Path | None:
    candidates = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  [WARN] Cannot read {path.name}: {exc}", file=sys.stderr)
        return None


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        rows.append(obj)
                except json.JSONDecodeError:
                    pass
    except Exception as exc:
        print(f"  [WARN] Cannot read {path.name}: {exc}", file=sys.stderr)
    return rows


def _str_fields(label: dict[str, Any], fields: tuple[str, ...]) -> list[str]:
    """Extract non-empty string values for the given fields from an SPL label dict."""
    out: list[str] = []
    for f in fields:
        val = label.get(f)
        if isinstance(val, str) and val.strip():
            out.append(val.strip())
        elif isinstance(val, list):
            out.extend(v.strip() for v in val if isinstance(v, str) and v.strip())
    return out


# ---------------------------------------------------------------------------
# Per-compound extraction
# ---------------------------------------------------------------------------

def _extract_spl_evidence(raw_report: dict[str, Any], compound: str) -> dict[str, Any]:
    """
    Filter raw report's openfda.drug_labels to labels that appear to describe this compound,
    then extract drug-interaction and mechanism text.

    Filter heuristic: at least one distinctive name token (>= 4 chars) of the compound name
    must appear somewhere in the label's stored text fields.  This handles the known OpenFDA
    label-search bug where unrelated drug labels are returned for supplements / non-approved
    compounds (e.g. tamsulosin labels returned when querying EGCG).
    """
    dl = (raw_report.get("openfda") or {}).get("drug_labels")
    if not isinstance(dl, list) or not dl:
        return {
            "label_matched": False,
            "match_note": "no drug_labels in raw report",
            "labels_total": 0,
            "labels_matched": 0,
            "interaction_excerpts": [],
            "mechanism_excerpt": None,
        }

    tokens = _name_tokens(compound)
    matched: list[dict[str, Any]] = []
    for label in dl:
        if not isinstance(label, dict):
            continue
        parts: list[str] = []
        for v in label.values():
            if isinstance(v, str):
                parts.append(v)
            elif isinstance(v, list):
                parts.extend(x for x in v if isinstance(x, str))
        all_text = " ".join(parts).lower()
        if tokens and any(t in all_text for t in tokens):
            matched.append(label)

    if not matched:
        return {
            "label_matched": False,
            "match_note": (
                f"none of {len(dl)} label(s) contained compound name tokens "
                f"{sorted(tokens)} — likely unrelated SPL labels returned by OpenFDA"
            ),
            "labels_total": len(dl),
            "labels_matched": 0,
            "interaction_excerpts": [],
            "mechanism_excerpt": None,
        }

    interaction_excerpts: list[str] = []
    mechanism_parts: list[str] = []
    seen: set[str] = set()

    for label in matched:
        for text in _str_fields(label, _SPL_INTERACTION_FIELDS):
            t = text[:_INTERACTION_EXCERPT_MAX]
            if t not in seen:
                seen.add(t)
                interaction_excerpts.append(t)
        for text in _str_fields(label, _SPL_MECHANISM_FIELDS):
            mechanism_parts.append(text[:_MECHANISM_EXCERPT_MAX])

    mechanism_excerpt = " | ".join(mechanism_parts[:3]) if mechanism_parts else None

    return {
        "label_matched": True,
        "match_note": f"{len(matched)} of {len(dl)} label(s) matched compound name tokens",
        "labels_total": len(dl),
        "labels_matched": len(matched),
        "interaction_excerpts": interaction_excerpts,
        "mechanism_excerpt": mechanism_excerpt,
    }


def _extract_kegg_evidence(prepared: dict[str, Any]) -> dict[str, Any]:
    """Extract KEGG pathway flags and sample pathway names from prepared agent JSON."""
    kegg = (prepared.get("agent_context") or {}).get("kegg") or {}
    flags: dict[str, bool] = kegg.get("longevity_pathway_flags") or {}
    pathway_names: list[str] = kegg.get("pathway_names_sample") or []
    if not pathway_names:
        pathway_names = [
            p["name"] for p in (kegg.get("pathways_preview") or [])
            if isinstance(p, dict) and p.get("name")
        ]
    active = [k for k, v in flags.items() if v]
    return {
        "pathway_flags": flags,
        "flags_present": active,
        "pathway_names_sample": pathway_names[:10],
        "kegg_drug_ids": kegg.get("kegg_drug_ids") or [],
        "kegg_available": bool(kegg.get("kegg_drug_ids")),
    }


def _extract_mechanism_units(tagged_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract mechanism/pathway-context units from tagged JSONL rows."""
    out: list[dict[str, Any]] = []
    for row in tagged_rows:
        unit_type = row.get("unit_type", "")
        report_section = row.get("report_section", "")
        if unit_type not in _MECHANISM_UNIT_TYPES and report_section != "mechanism_pathway_context":
            continue
        payload = row.get("payload") or {}
        snippet: str | None = None
        for field in ("excerpt", "abstract_excerpt", "description_snippet"):
            val = payload.get(field)
            if isinstance(val, str) and val.strip():
                snippet = val.strip()[:500]
                break
        if not snippet:
            blocks = payload.get("spl_hypothesis_blocks")
            if isinstance(blocks, list) and blocks:
                snippet = str(blocks[0])[:500]
        out.append({
            "unit_id": row.get("unit_id"),
            "unit_type": unit_type,
            "report_section": report_section,
            "decision_relevance": row.get("decision_relevance"),
            "snippet": snippet,
        })
    return out


def _extract_compound_evidence(compound: str, data_dir: Path) -> dict[str, Any]:
    """Assemble all interaction-relevant evidence for one compound from its data directory."""
    ev: dict[str, Any] = {
        "compound_name": compound,
        "data_dir": str(data_dir),
        "found": data_dir.exists(),
        "warnings": [],
    }

    if not ev["found"]:
        ev["warnings"].append(f"data directory not found: {data_dir}")
        return ev

    # ---- Review JSON ----
    # Reviews are now in reviews/<compound>/ at repo root instead of data/<compound>/
    # Use the same safe name transformation as review.py line 177
    reviews_root = data_dir.parent.parent.parent / "reviews"
    safe_compound = re.sub(r'[<>:"/\\|?*]', "_", compound).strip()
    review_dir = reviews_root / safe_compound
    review_path = _find_latest(review_dir, "*-review.json") if review_dir.exists() else None
    if review_path:
        review = _load_json(review_path)
        if isinstance(review, dict):
            cats = review.get("categories") or {}
            sg = cats.get("scientific_grounding") or {}
            ra = cats.get("risk_assessment") or {}
            ev["review_statement"] = review.get("review_statement")
            ev["scientific_grounding_score"] = sg.get("score")
            ev["scientific_grounding_rationale"] = sg.get("rationale")
            ev["risk_score"] = ra.get("score")
            ev["risk_rationale"] = ra.get("rationale")
        else:
            ev["warnings"].append("review JSON unreadable")
    else:
        ev["warnings"].append(f"no *-review.json found in {review_dir} — run review.py first")

    # ---- Raw report → SPL evidence ----
    raw_path = _find_latest(data_dir, "report_*.json")
    if raw_path:
        raw = _load_json(raw_path)
        ev["spl"] = _extract_spl_evidence(raw, compound) if isinstance(raw, dict) else {
            "label_matched": False, "match_note": "raw report unreadable",
            "interaction_excerpts": [], "mechanism_excerpt": None,
        }
    else:
        ev["warnings"].append("no report_*.json found — run discover.py first")
        ev["spl"] = {
            "label_matched": False, "match_note": "no raw report",
            "interaction_excerpts": [], "mechanism_excerpt": None,
        }

    # ---- Prepared agent JSON → KEGG ----
    prepared_path = _find_latest(data_dir, "prepared_*_agent.json")
    if prepared_path:
        prepared = _load_json(prepared_path)
        if isinstance(prepared, dict):
            ev["kegg"] = _extract_kegg_evidence(prepared)
            ev["coverage"] = (prepared.get("metadata") or {}).get("coverage") or {}
        else:
            ev["warnings"].append("prepared agent JSON unreadable")
            ev["kegg"] = {"pathway_flags": {}, "flags_present": [], "pathway_names_sample": [], "kegg_drug_ids": [], "kegg_available": False}
            ev["coverage"] = {}
    else:
        ev["warnings"].append("no prepared *_agent.json found — run prepare.py first")
        ev["kegg"] = {"pathway_flags": {}, "flags_present": [], "pathway_names_sample": [], "kegg_drug_ids": [], "kegg_available": False}
        ev["coverage"] = {}

    # ---- Tagged JSONL → mechanism units ----
    tagged_path = _find_latest(data_dir, "units_tagged.jsonl")
    if tagged_path:
        ev["mechanism_units"] = _extract_mechanism_units(_load_jsonl(tagged_path))
    else:
        ev["warnings"].append("no units_tagged.jsonl found — run tag.py first")
        ev["mechanism_units"] = []

    return ev


# ---------------------------------------------------------------------------
# Cross-reference
# ---------------------------------------------------------------------------

def _cross_reference(compounds_evidence: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Compute structured cross-compound interaction signals (no LLM)."""
    compound_names = list(compounds_evidence.keys())

    # --- Pathway overlap ---
    pathway_presence: dict[str, list[str]] = {}
    for name, ev in compounds_evidence.items():
        for flag, val in (ev.get("kegg") or {}).get("pathway_flags", {}).items():
            if val:
                pathway_presence.setdefault(flag, []).append(name)

    shared_pathways = sorted(
        flag for flag, names in pathway_presence.items() if len(names) >= 2
    )

    # --- Explicit name mentions ---
    # For each compound A, search its interaction + mechanism text for tokens of compound B.
    explicit_mentions: list[dict[str, Any]] = []
    for source_name, source_ev in compounds_evidence.items():
        # Build searchable text blocks with source labels.
        text_blocks: list[tuple[str, str, str]] = []  # (unit_id, source_type, text)
        for i, excerpt in enumerate(
            (source_ev.get("spl") or {}).get("interaction_excerpts", [])
        ):
            text_blocks.append((f"spl_interaction_{i}", "spl_drug_interaction", excerpt))
        mech = (source_ev.get("spl") or {}).get("mechanism_excerpt")
        if mech:
            text_blocks.append(("spl_mechanism", "spl_mechanism_pharmacology", mech))
        for mu in source_ev.get("mechanism_units", []):
            snippet = mu.get("snippet") or ""
            if snippet:
                text_blocks.append((
                    mu.get("unit_id") or "unknown",
                    mu.get("unit_type") or "unknown",
                    snippet,
                ))

        for target_name in compound_names:
            if target_name == source_name:
                continue
            tokens = _name_tokens(target_name)
            if not tokens:
                continue
            for unit_id, source_type, text in text_blocks:
                text_lower = text.lower()
                matched_tokens = [t for t in tokens if t in text_lower]
                if matched_tokens:
                    # Build a short context snippet around the first match.
                    first = matched_tokens[0]
                    idx = text_lower.find(first)
                    start = max(0, idx - 60)
                    end = min(len(text), idx + _SNIPPET_CONTEXT)
                    snippet = (
                        ("…" if start > 0 else "")
                        + text[start:end]
                        + ("…" if end < len(text) else "")
                    )
                    explicit_mentions.append({
                        "mentioned_compound": target_name,
                        "found_in_compound": source_name,
                        "source_unit_id": unit_id,
                        "source_type": source_type,
                        "matched_tokens": matched_tokens,
                        "snippet": snippet,
                    })
                    break  # one mention per (source_compound, target_compound, unit) is enough

    # --- SPL coverage summary ---
    spl_notes: list[str] = []
    for name, ev in compounds_evidence.items():
        spl = ev.get("spl") or {}
        if spl.get("label_matched"):
            spl_notes.append(f"{name}: SPL matched ({spl.get('match_note', '')})")
        else:
            spl_notes.append(f"{name}: no SPL match — {spl.get('match_note', 'unknown')}")

    return {
        "pathway_presence": pathway_presence,
        "shared_pathways": shared_pathways,
        "explicit_mentions": explicit_mentions,
        "spl_coverage_summary": " | ".join(spl_notes),
        "note": (
            "explicit_mentions are text-token matches only — verify before treating as confirmed interactions. "
            "shared_pathways are KEGG keyword flags, not experimentally validated pathway co-membership."
        ),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Collect interaction evidence for a compound combination (no LLM).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python interactions.py --compounds Doxycycline "Epigallocatechin gallate" -o combo.json\n'
            "  python interactions.py --compounds Metformin Rapamycin Doxycycline -o trio.json\n"
            "  python interactions.py --compounds Metformin Rapamycin  # stdout\n"
        ),
    )
    ap.add_argument(
        "--compounds",
        nargs="+",
        required=True,
        metavar="COMPOUND",
        help="Two or more compound names (each must have data in pump-science/data/).",
    )
    ap.add_argument(
        "--output", "-o",
        default=None,
        metavar="PATH",
        help="Output JSON file path. Omit to write to stdout.",
    )
    ap.add_argument(
        "--data-dir",
        default=None,
        metavar="DIR",
        help=f"Root directory containing per-compound data folders (default: {_DATA_DIR}).",
    )
    args = ap.parse_args()

    if len(args.compounds) < 2:
        print("ERROR: --compounds requires at least two compound names.", file=sys.stderr)
        return 1

    data_root = Path(args.data_dir).resolve() if args.data_dir else _DATA_DIR

    print(f"Compounds: {', '.join(args.compounds)}", file=sys.stderr)

    compounds_evidence: dict[str, dict[str, Any]] = {}
    for raw_name in args.compounds:
        compound = raw_name.strip()
        data_dir = data_root / _safe_dir_name(compound)
        print(f"  [{compound}] {data_dir}", file=sys.stderr)
        compounds_evidence[compound] = _extract_compound_evidence(compound, data_dir)
        warnings = compounds_evidence[compound].get("warnings", [])
        for w in warnings:
            print(f"    WARN: {w}", file=sys.stderr)

    print("Cross-referencing...", file=sys.stderr)
    cross_ref = _cross_reference(compounds_evidence)

    if cross_ref["shared_pathways"]:
        print(f"  Shared KEGG pathways: {cross_ref['shared_pathways']}", file=sys.stderr)
    else:
        print("  No shared KEGG pathway flags found.", file=sys.stderr)

    if cross_ref["explicit_mentions"]:
        for m in cross_ref["explicit_mentions"]:
            print(
                f"  Mention: '{m['mentioned_compound']}' tokens found in "
                f"'{m['found_in_compound']}' → {m['source_unit_id']}",
                file=sys.stderr,
            )
    else:
        print("  No explicit compound name mentions found in interaction/mechanism text.", file=sys.stderr)

    bundle: dict[str, Any] = {
        "$schema_hint": "pump-science.interactions_evidence.v1",
        "combination_name": " + ".join(args.compounds),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "compound_count": len(args.compounds),
        "compounds": compounds_evidence,
        "cross_reference": cross_ref,
    }

    out_text = json.dumps(bundle, indent=2, ensure_ascii=False) + "\n"

    if args.output:
        out_path = Path(args.output).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(out_text, encoding="utf-8")
        print(f"Wrote: {out_path}", file=sys.stderr)
    else:
        sys.stdout.buffer.write(out_text.encode("utf-8"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
