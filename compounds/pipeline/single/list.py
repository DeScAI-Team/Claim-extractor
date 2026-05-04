#!/usr/bin/env python3
r"""Turn prepared JSON into JSONL: one record per evaluation unit.

Default lines are slim: compound, ids, provenance, payload, sequence (+ optional basename).
Use ``--audit`` for schema hints, full paths, timestamps, json_path, etc.

Requires ``prepare`` from the same directory (stdlib only).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Rebuild units if older prepared files omit ``evaluation_units``.
from prepare import _build_evaluation_units

SCHEMA_HINT = "pump-science.evaluation_unit_jsonl.v1"


def _truncate_strings(obj: Any, max_len: int, _depth: int = 0) -> Any:
    """Recursively shorten long strings in nested dict/list structures (payload trimming)."""
    if max_len <= 0:
        return obj
    if isinstance(obj, str):
        if len(obj) <= max_len:
            return obj
        return obj[: max_len - 1].rstrip() + "…"
    if isinstance(obj, dict) and _depth < 12:
        return {k: _truncate_strings(v, max_len, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, list) and _depth < 12:
        return [_truncate_strings(v, max_len, _depth + 1) for v in obj]
    return obj


def _extract_units(data: dict[str, Any]) -> list[dict[str, Any]]:
    ac = data.get("agent_context")
    if not isinstance(ac, dict):
        return []
    units = ac.get("evaluation_units")
    if isinstance(units, list) and units:
        return units
    # Older prepared JSON: rebuild from nested agent_context sections.
    return _build_evaluation_units(
        ac.get("literature") or {},
        ac.get("clinical_trials") or {},
        ac.get("kegg") or {},
        ac.get("mechanism_hypotheses_excerpt") or {},
        ac.get("risks_overview") or {},
    )


def _coverage_snapshot(meta: Any) -> dict[str, Any] | None:
    if not isinstance(meta, dict):
        return None
    cov = meta.get("coverage")
    return cov if isinstance(cov, dict) else None


def _lines_for_prepared(
    data: dict[str, Any],
    *,
    source_path: Path | None,
    truncate_payload: int,
    repeat_coverage: bool,
    audit: bool,
) -> list[dict[str, Any]]:
    compound = data.get("compound_name")
    src_report = data.get("source_report")
    fmt = data.get("prepare_output_format")
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    ts = meta.get("timestamp")
    cov = _coverage_snapshot(meta)

    units = _extract_units(data)
    out: list[dict[str, Any]] = []
    prep_name = source_path.name if source_path else None

    for u in units:
        if not isinstance(u, dict):
            continue
        uid = u.get("unit_id")
        ut = u.get("unit_type")
        jpath = u.get("json_path")
        prov = u.get("provenance")
        payload = u.get("payload")
        if truncate_payload > 0 and payload is not None:
            payload = _truncate_strings(payload, truncate_payload)

        if audit:
            row: dict[str, Any] = {
                "$schema_hint": SCHEMA_HINT,
                "compound_name": compound,
                "source_report": src_report,
                "source_prepared_file": str(source_path.resolve()) if source_path else None,
                "prepare_output_format": fmt,
                "report_timestamp": ts,
                "unit_id": uid,
                "unit_type": ut,
                "json_path_in_prepared_doc": jpath,
                "provenance": prov,
                "payload": payload,
            }
            if repeat_coverage and cov is not None:
                row["metadata_coverage_snapshot"] = cov
        else:
            row = {
                "compound_name": compound,
                "unit_id": uid,
                "unit_type": ut,
                "provenance": prov,
                "payload": payload,
            }
            if prep_name:
                row["prepared_file"] = prep_name

        out.append(row)
    return out


def load_prepared(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("prepared JSON must be an object at the top level")
    return data


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Convert prepared JSON to JSONL (one line per evaluation unit, LLM-oriented fields)."
    )
    ap.add_argument(
        "prepared",
        nargs="+",
        type=Path,
        metavar="PREPARED.json",
        help="One or more prepared JSON files (review or agent format).",
    )
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Write JSONL to this file (UTF-8). Default: stdout.",
    )
    ap.add_argument(
        "--truncate-payload",
        type=int,
        default=0,
        metavar="N",
        help="Recursively truncate string values longer than N chars inside each payload (0 = no truncation).",
    )
    ap.add_argument(
        "--repeat-coverage",
        action="store_true",
        help="With --audit: include metadata.coverage on every line.",
    )
    ap.add_argument(
        "--audit",
        action="store_true",
        help="Verbose rows: schema hint, full paths, timestamps, prepare_output_format, json_path (default is slim).",
    )
    ns = ap.parse_args()

    batch: list[dict[str, Any]] = []
    exit_code = 0
    for p in ns.prepared:
        try:
            data = load_prepared(p.expanduser().resolve())
        except OSError as e:
            print(f"list: could not read {p}: {e}", file=sys.stderr)
            exit_code = 1
            continue
        except (json.JSONDecodeError, UnicodeError, ValueError) as e:
            print(f"list: could not parse {p}: {e}", file=sys.stderr)
            exit_code = 1
            continue

        rows = _lines_for_prepared(
            data,
            source_path=p.expanduser().resolve(),
            truncate_payload=ns.truncate_payload,
            repeat_coverage=ns.repeat_coverage,
            audit=ns.audit,
        )
        if not rows:
            print(f"list: warning: no evaluation units in {p} (empty agent_context?)", file=sys.stderr)
        batch.extend(rows)

    total = len(batch)

    # Stable key order: identifiers first, payload last (audit keys follow provenance).
    key_order = [
        "compound_name",
        "unit_sequence",
        "unit_id",
        "unit_type",
        "prepared_file",
        "$schema_hint",
        "source_report",
        "source_prepared_file",
        "prepare_output_format",
        "report_timestamp",
        "json_path_in_prepared_doc",
        "provenance",
        "metadata_coverage_snapshot",
        "payload",
    ]

    def finalize(row: dict[str, Any], index: int) -> dict[str, Any]:
        seq = {"index": index, "total": total}
        m = dict(row)
        m["unit_sequence"] = seq
        out: dict[str, Any] = {}
        for k in key_order:
            if k in m:
                out[k] = m[k]
        for k, v in m.items():
            if k not in out:
                out[k] = v
        return out

    all_lines = [finalize(row, i) for i, row in enumerate(batch)]

    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in all_lines)
    if not body and not all_lines:
        body = ""
    elif body and not body.endswith("\n"):
        body += "\n"

    if ns.output is not None:
        try:
            ns.output.expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            ns.output.write_text(body, encoding="utf-8")
        except OSError as e:
            print(f"list: could not write {ns.output}: {e}", file=sys.stderr)
            return 1
    else:
        sys.stdout.buffer.write(body.encode("utf-8"))

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
