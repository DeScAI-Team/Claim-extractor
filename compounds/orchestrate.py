#!/usr/bin/env python3
"""Pump-science review orchestrator.

Single compound  →  run_review.py (full 6-step pipeline)
Multiple compounds → run_review.py per compound (unless --skip-individual)
                     → interactions.py  (evidence bundle)
                     → review-multiple.py (4-pass combination review)

Usage:
  python orchestrate.py --compounds Doxycycline
  python orchestrate.py --compounds Omipalisib "Ginsenoside Rh2" "Urolithin A"
  python orchestrate.py --compounds Omipalisib "Ginsenoside Rh2" --skip-individual
  python orchestrate.py --compounds Omipalisib "Ginsenoside Rh2" --model mixtral-8x7b
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_DATA = _DIR / "data"


def _run(label: str, cmd: list) -> None:
    print(f"\n[{label}]", flush=True)
    result = subprocess.run([str(c) for c in cmd])
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--compounds", nargs="+", required=True, metavar="COMPOUND")
    ap.add_argument("--model", default=None)
    ap.add_argument("--skip-risk", action="store_true")
    ap.add_argument("--skip-discover", action="store_true")
    ap.add_argument("--skip-individual", action="store_true", help="Multi only: skip per-compound pipelines.")
    args = ap.parse_args()

    py = sys.executable
    compounds = [c.strip() for c in args.compounds if c.strip()]

    def review_flags() -> list:
        f = []
        if args.model: f += ["--model", args.model]
        if args.skip_risk: f.append("--skip-risk")
        if args.skip_discover: f.append("--skip-discover")
        return f

    if len(compounds) == 1:
        _run(f"review: {compounds[0]}", [py, _DIR / "pipeline" / "single" / "run_review.py", "--compound", compounds[0]] + review_flags())
    else:
        if not args.skip_individual:
            for c in compounds:
                _run(f"review: {c}", [py, _DIR / "pipeline" / "single" / "run_review.py", "--compound", c] + review_flags())

        slug = "-".join(c[:5].lower() for c in compounds)
        bundle = _DATA / f"{slug}-bundle.json"
        _run("interactions", [py, _DIR / "pipeline" / "multi" / "interactions.py", "--compounds", *compounds, "-o", str(bundle)])

        combo_flags = ["--model", args.model] if args.model else []
        _run("review-multiple", [py, _DIR / "pipeline" / "multi" / "review-multiple.py", str(bundle)] + combo_flags)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
