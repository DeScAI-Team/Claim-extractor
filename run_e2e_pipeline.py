#!/usr/bin/env python3
"""
End-to-end DeScAi pipeline orchestrator (single PDF → validated claims JSONL).

Prerequisites:
  - Local vLLM (or compatible OpenAI API) reachable via VLLM_BASE_URL; set VALIDATOR_MODEL
    to the served model id (and VLLM_API_KEY if required).
  - python-dotenv optional: place a .env in the repo root with those variables.
  - Step 1: docling, transformers, etc. (see add_data.py).
  - Step 2: spaCy + en_core_web_sm (`python -m spacy download en_core_web_sm`).
  - Steps 3–4: openai package.

Default PDF: document (8).pdf in the repo root. Intermediate files are written under
claim-extract-test/ where spacy_test, LLM_extract, and claim_validator expect them.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[misc, assignment]

ROOT = Path(__file__).resolve().parent
TEST = ROOT / "claim-extract-test"
DEFAULT_PDF = ROOT / "document (8).pdf"


def _commands(pdf: Path) -> list[list[str]]:
    py = sys.executable
    out_jsonl = (TEST / "text_knowledge_base.jsonl").resolve()
    return [
        [
            py,
            str((TEST / "add_data.py").resolve()),
            "--file",
            str(pdf.resolve()),
            "-o",
            str(out_jsonl),
        ],
        [py, str((TEST / "spacy_test.py").resolve())],
        [py, str((TEST / "LLM_extract.py").resolve())],
        [py, str((TEST / "claim_validator.py").resolve())],
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run claim pipeline steps 1–4 in order.")
    parser.add_argument(
        "--pdf",
        type=Path,
        default=DEFAULT_PDF,
        help=f"Input PDF (default: {DEFAULT_PDF.name})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands only; do not execute.",
    )
    args = parser.parse_args()

    if load_dotenv:
        load_dotenv(ROOT / ".env")

    pdf = args.pdf
    if not pdf.is_file():
        print(f"error: PDF not found: {pdf.resolve()}", file=sys.stderr)
        sys.exit(1)

    steps = [
        "Step 1/4 - PDF to chunks (add_data.py)",
        "Step 2/4 - spaCy tagging (spacy_test.py)",
        "Step 3/4 - LLM claim extraction (LLM_extract.py)",
        "Step 4/4 - LLM validation (claim_validator.py)",
    ]
    cmds = _commands(pdf)

    for label, cmd in zip(steps, cmds):
        print(f"\n=== {label} ===")
        print("+", " ".join(cmd))
        if args.dry_run:
            continue
        subprocess.run(cmd, check=True, cwd=str(ROOT))

    if args.dry_run:
        print("\n(dry-run: no commands executed)")
    else:
        print(f"\nDone. Validated output: {(TEST / 'validated_claims.jsonl').resolve()}")


if __name__ == "__main__":
    main()
