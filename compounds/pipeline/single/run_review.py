#!/usr/bin/env python3
"""Run the full pump-science review pipeline for a single compound.

Steps (all outputs go to pump-science/data/<compound>/):
  1. discover.py   → report_<UTC>.json
  2. prepare.py    → prepared_report_<stem>_agent.json
  3. list.py       → units.jsonl
  4. tag.py        → units_tagged.jsonl
  5. group_by_stance.py → grouped_by_stance.json
  6. review.py     → <Compound>-review.json

Usage:
  python run_review.py --compound Doxycycline
  python run_review.py --compound Metformin --model my-model-id
  python run_review.py --compound Doxycycline --skip-risk
  python run_review.py --compound Doxycycline --skip-discover
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_PUMP_SCIENCE_DIR = Path(__file__).resolve().parent
_DATA_DIR = _PUMP_SCIENCE_DIR.parent.parent / "data"

_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def _safe_dir_name(compound: str) -> str:
    """Identical sanitization logic to discover.py:safe_compound_dir."""
    safe = re.sub(r"[^\w\-.]+", "_", compound, flags=re.UNICODE).strip("._- ")[:80] or "compound"
    if safe.upper() in _WIN_RESERVED:
        safe = f"_{safe}_"
    return safe


def _run(label: str, cmd: list[str | Path]) -> None:
    """Run a subprocess step; exit immediately on failure."""
    str_cmd = [str(c) for c in cmd]
    print(f"\n[{label}] {' '.join(str_cmd)}", flush=True)
    result = subprocess.run(str_cmd)
    if result.returncode != 0:
        print(f"\n[{label}] FAILED (exit code {result.returncode})", file=sys.stderr, flush=True)
        raise SystemExit(result.returncode)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run the full pump-science review pipeline for a compound.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python run_review.py --compound Doxycycline\n"
            "  python run_review.py --compound Metformin --model my-model-id\n"
            "  python run_review.py --compound Doxycycline --skip-risk\n"
            "  python run_review.py --compound Doxycycline --skip-discover\n"
        ),
    )
    ap.add_argument("--compound", required=True, help="Compound name to review.")
    ap.add_argument(
        "--model",
        default=None,
        metavar="NAME",
        help="Override LLM model id forwarded to tag.py and review.py (--model flag).",
    )
    ap.add_argument(
        "--skip-risk",
        action="store_true",
        help="Pass --skip-risk to tag.py (section/stance tagging only; skip risk severity round).",
    )
    ap.add_argument(
        "--skip-discover",
        action="store_true",
        help=(
            "Skip step 1 (discover). Picks the most recently modified report_*.json "
            "in the compound data directory."
        ),
    )
    args = ap.parse_args()

    compound = args.compound.strip()
    compound_dir = (_DATA_DIR / _safe_dir_name(compound)).resolve()
    compound_dir.mkdir(parents=True, exist_ok=True)

    py = sys.executable

    # ------------------------------------------------------------------
    # Step 1: Discover
    # ------------------------------------------------------------------
    if args.skip_discover:
        candidates = sorted(
            compound_dir.glob("report_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            print(
                f"--skip-discover: no report_*.json found in {compound_dir}",
                file=sys.stderr,
            )
            return 1
        report_path = candidates[0]
        print(f"\n[1/6 discover] Skipped — using existing report: {report_path}", flush=True)
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
        report_path = compound_dir / f"report_{ts}.json"
        _run(
            "1/6 discover",
            [py, _PUMP_SCIENCE_DIR / "discover.py", "--compound", compound, "--output", report_path],
        )

    # ------------------------------------------------------------------
    # Step 2: Prepare (agent format, written beside the report file)
    # ------------------------------------------------------------------
    _run(
        "2/6 prepare",
        [py, _PUMP_SCIENCE_DIR / "prepare.py", str(report_path), "--format", "agent"],
    )
    # prepare.py names the output: prepared_<report_stem>_agent.json
    prepared_path = compound_dir / f"prepared_{report_path.stem}_agent.json"
    if not prepared_path.exists():
        print(f"\n[2/6 prepare] ERROR: expected output not found: {prepared_path}", file=sys.stderr)
        return 1

    # ------------------------------------------------------------------
    # Step 3: List (one JSONL line per evaluation unit)
    # ------------------------------------------------------------------
    units_path = compound_dir / "units.jsonl"
    _run(
        "3/6 list",
        [py, _PUMP_SCIENCE_DIR / "list.py", str(prepared_path), "-o", str(units_path)],
    )

    # ------------------------------------------------------------------
    # Step 4: Tag (section + stance + risk severity)
    # ------------------------------------------------------------------
    tagged_path = compound_dir / "units_tagged.jsonl"
    tag_cmd: list[str | Path] = [
        py, _PUMP_SCIENCE_DIR / "tag.py",
        str(units_path), "-o", str(tagged_path),
    ]
    if args.skip_risk:
        tag_cmd.append("--skip-risk")
    if args.model:
        tag_cmd += ["--model", args.model]
    _run("4/6 tag", tag_cmd)

    # ------------------------------------------------------------------
    # Step 5: Group by stance
    # ------------------------------------------------------------------
    _run(
        "5/6 group",
        [py, _PUMP_SCIENCE_DIR / "group_by_stance.py", str(tagged_path)],
    )
    grouped_path = compound_dir / "grouped_by_stance.json"

    # ------------------------------------------------------------------
    # Step 6: Review (three LLM passes)
    # ------------------------------------------------------------------
    review_cmd: list[str | Path] = [
        py, _PUMP_SCIENCE_DIR / "review.py", str(grouped_path),
    ]
    if args.model:
        review_cmd += ["--model", args.model]
    _run("6/6 review", review_cmd)

    # Derive the review output path the same way review.py does.
    safe_name = re.sub(r'[<>:"/\\|?*]', "_", compound).strip()
    reviews_root = _DATA_DIR.parent / "reviews"
    review_path = reviews_root / safe_name / f"{safe_name}-review.json"

    print(f"\nPipeline complete.", flush=True)
    print(f"Review: {review_path}", flush=True)
    print(f"Intermediate files in: {compound_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
