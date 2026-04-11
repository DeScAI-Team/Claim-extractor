#!/usr/bin/env python3
"""
Wrapper around arweaveServiceCLI.js: uploads a file via the existing CLI and
writes a JSON receipt (default: Arweave-Cli/upload_reciept.json) with stdout,
stderr, exit code, and parsed tx id / URL when present. Use --receipt to override
the receipt path.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

RECEIPT_NAME = "upload_reciept.json"
CLI_SCRIPT = "arweaveServiceCLI.js"


def _cli_dir() -> Path:
    return Path(__file__).resolve().parent


def _parse_web_url(stdout: str) -> str | None:
    m = re.search(r"File is live at:\s*(https://arweave\.net/[A-Za-z0-9_-]+)", stdout)
    return m.group(1) if m else None


def _tx_id_from_web_url(web_url: str) -> str | None:
    # https://arweave.net/<txId>
    prefix = "https://arweave.net/"
    if web_url.startswith(prefix):
        return web_url[len(prefix) :]
    return None


def _failure_hint(stderr: str) -> str | None:
    if "ERR_MODULE_NOT_FOUND" in stderr and "dotenv" in stderr:
        return (
            "Node dependencies missing: run `npm install` in the Arweave-Cli directory."
        )
    if "Missing WALLET_PATH" in stderr:
        return (
            "Copy .env.example to .env and set WALLET_PATH to your Arweave wallet JWK JSON file."
        )
    if "ENOENT" in stderr and ("WALLET_PATH" in stderr or "keyfile" in stderr.lower()):
        return "Wallet file not found: check WALLET_PATH in .env points to a readable JWK file."
    if "Invalid file Path" in stderr or "Invalid file Path!" in stderr:
        return "Upload path invalid or not readable from the CLI working directory."
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upload a file via Arweave-Cli (arweaveServiceCLI.js) and write a JSON receipt.",
    )
    parser.add_argument(
        "--file",
        "-f",
        required=True,
        metavar="PATH",
        help="Path to the file to upload (passed through to the Node CLI as the first argument).",
    )
    parser.add_argument(
        "--receipt",
        "-r",
        type=Path,
        default=None,
        metavar="PATH",
        help=f"Receipt JSON path (default: Arweave-Cli/{RECEIPT_NAME})",
    )
    args = parser.parse_args()
    file_arg = args.file

    arweave_cli = _cli_dir()
    receipt_path = (
        args.receipt.expanduser().resolve()
        if args.receipt is not None
        else arweave_cli / RECEIPT_NAME
    )
    cli_js = arweave_cli / CLI_SCRIPT
    node_modules = arweave_cli / "node_modules"

    if not cli_js.is_file():
        sys.stderr.write(f"Missing CLI script: {cli_js}\n")
        return 2

    if not node_modules.is_dir():
        sys.stderr.write(
            "Arweave-Cli/node_modules missing — run `npm install` in Arweave-Cli first.\n"
        )

    cmd = ["node", CLI_SCRIPT, file_arg]
    proc = subprocess.run(
        cmd,
        cwd=str(arweave_cli),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    web_url = _parse_web_url(stdout)
    tx_id = _tx_id_from_web_url(web_url) if web_url else None
    success = web_url is not None and proc.returncode == 0
    hint = None if success else _failure_hint(stderr)

    receipt = {
        "success": success,
        "uploaded_file": file_arg,
        "tx_id": tx_id,
        "web_url": web_url,
        "cli_stdout": stdout,
        "cli_stderr": stderr,
        "exit_code": proc.returncode,
        "cli_command": cmd,
        "hint": hint,
    }

    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(f"Wrote receipt: {receipt_path}\n")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
