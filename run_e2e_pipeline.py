#!/usr/bin/env python3
"""
End-to-end DeScAi pipeline orchestrator (single PDF → review JSON under data/).

Prerequisites:
  - Local vLLM (or compatible OpenAI API) reachable via VLLM_BASE_URL; set VALIDATOR_MODEL
    to the served model id (and VLLM_API_KEY if required).
  - python-dotenv optional: place a .env in the repo root with those variables.
  - Step 1: docling, transformers, etc. (see add_data.py).
  - Step 2: spaCy + en_core_web_sm (`python -m spacy download en_core_web_sm`).
  - Steps 3–5, 8: openai package.
  - Optional step 9 (--upload): Node + npm install in Arweave-Cli, wallet .env.

Default PDF: document (8).pdf in the repo root. Steps 1–4 write intermediates under
claim-extract-test/ where spacy_test, LLM_extract, and claim_validator expect them.
Steps 5–8 write under --artifacts-dir (default: data/). Step 9 uploads review.json
when --upload is passed.
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
DEFAULT_ARTIFACTS = ROOT / "data"
MAPPINGS_JSON = ROOT / "group-and-score" / "mappings.json"
VALIDATED_CLAIMS = TEST / "validated_claims.jsonl"


def _commands_extract(pdf: Path) -> list[list[str]]:
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


def _commands_postprocess(
    py: str, artifacts: Path, upload: bool
) -> tuple[list[str], list[list[str]]]:
    """Steps 5–8 (and 9 if upload); paths must be resolved for argv."""
    a = artifacts
    classified = a / "classified_claims.jsonl"
    grouped = a / "grouped.json"
    prepped = a / "prepped.json"
    review_out = a / "review.json"
    validated = VALIDATED_CLAIMS.resolve()

    labels: list[str] = []
    cmds: list[list[str]] = []

    total = 9 if upload else 8

    labels.append(f"Step 5/{total} - Claim classification (classify_claims.py)")
    cmds.append(
        [
            py,
            str((ROOT / "claim-classifier" / "classify_claims.py").resolve()),
            "-i",
            str(validated),
            "-o",
            str(classified),
        ]
    )

    labels.append(f"Step 6/{total} - Group by dimension (group.py)")
    cmds.append(
        [
            py,
            str((ROOT / "group-and-score" / "group.py").resolve()),
            str(classified),
            "-o",
            str(grouped),
            "--mappings",
            str(MAPPINGS_JSON.resolve()),
        ]
    )

    labels.append(f"Step 7/{total} - Claim narratives (prep.py)")
    cmds.append(
        [
            py,
            str((ROOT / "group-and-score" / "prep.py").resolve()),
            str(grouped),
            "-o",
            str(prepped),
        ]
    )

    labels.append(f"Step 8/{total} - Review generation (review.py)")
    cmds.append(
        [
            py,
            str((ROOT / "review-gen" / "review.py").resolve()),
            "--prepped",
            str(prepped),
            "--mappings",
            str(MAPPINGS_JSON.resolve()),
            "-o",
            str(review_out),
        ]
    )

    if upload:
        labels.append(f"Step 9/{total} - Arweave upload (upload_orchestrator.py)")
        cmds.append(
            [
                py,
                str((ROOT / "Arweave-Cli" / "upload_orchestrator.py").resolve()),
                "--file",
                str(review_out),
                "--receipt",
                str(a / "upload_receipt.json"),
            ]
        )

    return labels, cmds


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run claim pipeline steps 1–8 in order (PDF → review JSON); optional Arweave upload."
    )
    parser.add_argument(
        "--pdf",
        type=Path,
        default=DEFAULT_PDF,
        help=f"Input PDF (default: {DEFAULT_PDF.name})",
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=DEFAULT_ARTIFACTS,
        help=f"Directory for classified/grouped/prepped/review outputs (default: {DEFAULT_ARTIFACTS.name}/)",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="After review, upload review.json via Arweave-Cli (requires Node, npm install, wallet).",
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

    artifacts_dir = args.artifacts_dir.expanduser().resolve()
    if not args.dry_run:
        artifacts_dir.mkdir(parents=True, exist_ok=True)

    py = sys.executable
    extract_cmds = _commands_extract(pdf)
    post_labels, post_cmds = _commands_postprocess(py, artifacts_dir, args.upload)

    total = 9 if args.upload else 8
    extract_labels = [
        f"Step 1/{total} - PDF to chunks (add_data.py)",
        f"Step 2/{total} - spaCy tagging (spacy_test.py)",
        f"Step 3/{total} - LLM claim extraction (LLM_extract.py)",
        f"Step 4/{total} - LLM validation (claim_validator.py)",
    ]

    all_labels = extract_labels + post_labels
    all_cmds = extract_cmds + post_cmds

    for label, cmd in zip(all_labels, all_cmds):
        print(f"\n=== {label} ===")
        print("+", " ".join(cmd))
        if args.dry_run:
            continue
        subprocess.run(cmd, check=True, cwd=str(ROOT))

    if args.dry_run:
        print("\n(dry-run: no commands executed)")
    else:
        print(f"\nDone. Validated claims: {VALIDATED_CLAIMS.resolve()}")
        print(f"Artifacts directory: {artifacts_dir}")
        print(f"Review JSON: {(artifacts_dir / 'review.json').resolve()}")


if __name__ == "__main__":
    main()
