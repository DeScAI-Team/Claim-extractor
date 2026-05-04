#!/usr/bin/env python3
"""
End-to-end article pipeline (single PDF → review JSON).

Prerequisites:
  - Local vLLM (or compatible OpenAI API) reachable via VLLM_BASE_URL; set VALIDATOR_MODEL
    to the served model id (and VLLM_API_KEY if required).
  - python-dotenv optional: place a .env in the repo root with those variables.
  - Step 1: docling, transformers, etc. (see claim-extract/add_data.py).
  - Step 2: spaCy + en_core_web_sm (`python -m spacy download en_core_web_sm`).
  - Steps 3–5, 8: openai package.

Steps 1–4 write intermediates under articles/data/ (via CLAIM_EXTRACT_DATA_DIR).
Steps 5–7 write classified/grouped/prepped JSON under --artifacts-dir (default: articles/data/).
Step 8 writes review.json under reviews/<research_name>/body/ (see --reviews-root, --research-name).

Upload / Arweave is handled by a separate top-level orchestrator, not this script.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[misc, assignment]

PIPELINE = Path(__file__).resolve().parent
ARTICLES = PIPELINE.parent
REPO_ROOT = ARTICLES.parent
CLAIM_EXTRACT = PIPELINE / "claim-extract"
DEFAULT_DATA = ARTICLES / "data"
DEFAULT_REVIEWS_ROOT = REPO_ROOT / "reviews"
MAPPINGS_JSON = PIPELINE / "mappings.json"

_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def _safe_research_name(name: str) -> str:
    """Filesystem-safe folder name (Windows-friendly)."""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" ._-") or "research"
    if len(safe) > 120:
        safe = safe[:120]
    if safe.upper() in _WIN_RESERVED:
        safe = f"_{safe}_"
    return safe


def _claim_extract_env(artifacts_dir: Path) -> dict[str, str]:
    return {**os.environ, "CLAIM_EXTRACT_DATA_DIR": str(artifacts_dir.resolve())}


def _commands_extract(pdf: Path, artifacts_dir: Path) -> list[list[str]]:
    py = sys.executable
    out_jsonl = (artifacts_dir / "text_knowledge_base.jsonl").resolve()
    return [
        [
            py,
            str((CLAIM_EXTRACT / "add_data.py").resolve()),
            "--file",
            str(pdf.resolve()),
            "-o",
            str(out_jsonl),
        ],
        [py, str((CLAIM_EXTRACT / "spacy_test.py").resolve())],
        [py, str((CLAIM_EXTRACT / "LLM_extract.py").resolve())],
        [py, str((CLAIM_EXTRACT / "claim_validator.py").resolve())],
    ]


def _commands_postprocess(
    py: str,
    artifacts: Path,
    review_out: Path,
) -> tuple[list[str], list[list[str]]]:
    """Steps 5–8; paths must be resolved for argv."""
    a = artifacts
    classified = a / "classified_claims.jsonl"
    grouped = a / "grouped.json"
    prepped = a / "prepped.json"
    validated = (a / "validated_claims.jsonl").resolve()

    labels: list[str] = []
    cmds: list[list[str]] = []
    total = 8

    labels.append(f"Step 5/{total} - Claim classification (classify_claims.py)")
    cmds.append(
        [
            py,
            str((PIPELINE / "classify_claims.py").resolve()),
            "-i",
            str(validated),
            "-o",
            str(classified.resolve()),
        ]
    )

    labels.append(f"Step 6/{total} - Group by dimension (group.py)")
    cmds.append(
        [
            py,
            str((PIPELINE / "group.py").resolve()),
            str(classified.resolve()),
            "-o",
            str(grouped.resolve()),
            "--mappings",
            str(MAPPINGS_JSON.resolve()),
        ]
    )

    labels.append(f"Step 7/{total} - Claim narratives (prep.py)")
    cmds.append(
        [
            py,
            str((PIPELINE / "prep.py").resolve()),
            str(grouped.resolve()),
            "-o",
            str(prepped.resolve()),
        ]
    )

    labels.append(f"Step 8/{total} - Review generation (review.py)")
    cmds.append(
        [
            py,
            str((PIPELINE / "review.py").resolve()),
            "--prepped",
            str(prepped.resolve()),
            "--mappings",
            str(MAPPINGS_JSON.resolve()),
            "-o",
            str(review_out.resolve()),
        ]
    )

    return labels, cmds


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run article pipeline steps 1–8 in order (PDF → review JSON under reviews/)."
    )
    parser.add_argument(
        "--pdf",
        type=Path,
        required=True,
        help="Input PDF path.",
    )
    parser.add_argument(
        "--research-name",
        type=str,
        default=None,
        metavar="NAME",
        help="Folder name under reviews/ (default: sanitized PDF stem).",
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=DEFAULT_DATA,
        help="Directory for JSONL/JSON working files (default: articles/data/).",
    )
    parser.add_argument(
        "--reviews-root",
        type=Path,
        default=DEFAULT_REVIEWS_ROOT,
        help=f"Parent directory for per-research folders (default: {DEFAULT_REVIEWS_ROOT}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands only; do not execute.",
    )
    args = parser.parse_args()

    if load_dotenv:
        load_dotenv(REPO_ROOT / ".env")

    pdf = args.pdf.expanduser().resolve()
    if not args.dry_run and not pdf.is_file():
        print(f"error: PDF not found: {pdf}", file=sys.stderr)
        sys.exit(1)
    if args.dry_run and not pdf.is_file():
        print(f"note: PDF not found (dry-run only): {pdf}", file=sys.stderr)

    artifacts_dir = args.artifacts_dir.expanduser().resolve()
    reviews_root = args.reviews_root.expanduser().resolve()

    raw_name = (args.research_name or pdf.stem).strip()
    safe_name = _safe_research_name(raw_name)
    review_out = reviews_root / safe_name / "body" / "review.json"

    if not args.dry_run:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        review_out.parent.mkdir(parents=True, exist_ok=True)

    py = sys.executable
    extract_cmds = _commands_extract(pdf, artifacts_dir)
    post_labels, post_cmds = _commands_postprocess(py, artifacts_dir, review_out)

    extract_labels = [
        "Step 1/8 - PDF to chunks (add_data.py)",
        "Step 2/8 - spaCy tagging (spacy_test.py)",
        "Step 3/8 - LLM claim extraction (LLM_extract.py)",
        "Step 4/8 - LLM validation (claim_validator.py)",
    ]

    all_labels = extract_labels + post_labels
    all_cmds = extract_cmds + post_cmds

    extract_env = _claim_extract_env(artifacts_dir)

    for i, (label, cmd) in enumerate(zip(all_labels, all_cmds)):
        print(f"\n=== {label} ===")
        print("+", " ".join(cmd))
        if args.dry_run:
            continue
        cwd = str(CLAIM_EXTRACT) if i < 4 else str(PIPELINE)
        env = extract_env if i < 4 else None
        subprocess.run(cmd, check=True, cwd=cwd, env=env)

    if args.dry_run:
        print("\n(dry-run: no commands executed)")
    else:
        validated = (artifacts_dir / "validated_claims.jsonl").resolve()
        print(f"\nDone. Validated claims: {validated}")
        print(f"Artifacts directory: {artifacts_dir}")
        print(f"Review JSON: {review_out.resolve()}")


if __name__ == "__main__":
    main()
