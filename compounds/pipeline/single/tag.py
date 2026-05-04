#!/usr/bin/env python3
r"""Tag each pump-science evaluation unit JSONL line via vLLM (OpenAI-compatible).

Reads JSONL produced by ``list.py`` (one JSON object per line). Appends:

1. ``report_section`` / ``decision_relevance`` — ``prompts/compound-excerpt-tagging.md``
2. ``risk_severity`` — ``prompts/compound-risk-profile.md`` (second completion per line)

Use ``--skip-risk`` for round 1 only. See ``REVIEW_LOGIC.md`` for semantics.

vLLM may load a model ``generation_config.json`` (e.g. temperature 0.6) and ignore
client ``temperature=0`` until the server is started with
``--generation-config vllm`` so request parameters take effect. This script
still sends ``temperature=0``, ``top_p=1``, and ``extra_body top_k=-1`` for
greedy-friendly sampling when the server honors them.

Optional: ``TAGGER_SEED`` (digits only) maps to OpenAI ``seed`` when supported.
Optional: ``TAGGER_MAX_TOKENS`` sets completion budget (default 2048) for reasoning models.
Optional: ``TAGGER_RISK_MODEL`` overrides the model for round 2 only.
Optional: ``TAGGER_RISK_RETRIES`` parse/re-ask attempts for risk allowlist (default 3).
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from openai import OpenAI, RateLimitError


def get_available_model(client: OpenAI, env_var_name: str, fallback_env_vars: list[str]) -> str:
    """
    Discover available model from vLLM server or environment variables.
    
    Priority:
    1. Query vLLM /v1/models endpoint
    2. Check env_var_name (e.g., TAGGER_MODEL, REVIEWER_MODEL)
    3. Check fallback_env_vars (e.g., CLASSIFIER_MODEL, VALIDATOR_MODEL)
    4. Raise error with helpful message
    """
    # Try to query vLLM for available models
    try:
        models = client.models.list()
        if models.data and len(models.data) > 0:
            model_id = models.data[0].id
            print(f"  Auto-discovered model: {model_id}", file=sys.stderr)
            return model_id
    except Exception as e:
        print(f"  Could not auto-discover models from vLLM: {e}", file=sys.stderr)
    
    # Fallback to environment variables
    for env_name in [env_var_name] + fallback_env_vars:
        val = os.environ.get(env_name)
        if val:
            print(f"  Using model from {env_name}: {val}", file=sys.stderr)
            return val
    
    # No model found - provide helpful error
    env_list = ", ".join([env_var_name] + fallback_env_vars)
    raise ValueError(
        f"No model specified and could not auto-discover from vLLM.\n"
        f"Please set one of these environment variables: {env_list}\n"
        f"Or ensure vLLM is running at {os.environ.get('VLLM_BASE_URL', 'http://localhost:8000/v1')}"
    )


REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = REPO_ROOT / "prompts" / "compound-excerpt-tagging.md"
PROMPT_RISK_PATH = REPO_ROOT / "prompts" / "compound-risk-profile.md"

if load_dotenv is not None:
    load_dotenv(REPO_ROOT / ".env")

VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
VLLM_API_KEY = os.environ.get("VLLM_API_KEY", "none")

# Model discovery happens later in main() after client is created

MAX_RETRIES = 4
# Reasoning models emit long ``<think>`` blocks; 128 tokens often truncates before the final
# two-tag line. Override with TAGGER_MAX_TOKENS (default 2048).
MAX_TAGGER_TOKENS = max(128, int(os.environ.get("TAGGER_MAX_TOKENS", "2048")))
MAX_RISK_PARSE_ATTEMPTS = max(1, int(os.environ.get("TAGGER_RISK_RETRIES", "3")))

_RISK_TOKEN_ALIASES = {"negligble": "negligible"}

# OpenAI-compatible servers may ignore client ``temperature`` if the model ships a
# ``generation_config.json`` (vLLM warns and applies e.g. temperature=0.6). Prefer
# launching vLLM with ``--generation-config vllm`` so request params win; we still send
# explicit greedy-friendly sampling here.


_END_THINK_MARKERS = (
    "</think>",
    "</think>",
    "</thinking>",
    "</reasoning>",
    "</thought>",
)


def strip_reasoning_markup(s: str) -> str:
    """Drop chain-of-thought wrappers; prefer text after the last end-thinking marker."""
    t = s.strip()
    low = t.lower()
    best_idx = -1
    best_len = 0
    for m in _END_THINK_MARKERS:
        pos = low.rfind(m.lower())
        if pos > best_idx:
            best_idx = pos
            best_len = len(m)
    if best_idx >= 0:
        t = t[best_idx + best_len :].lstrip()
    # Remove any balanced blocks that remain (repeat for nesting).
    block_patterns = (
        r"<think\b[^>]*>[\s\S]*?</think>",
        r"<thinking\b[^>]*>[\s\S]*?</thinking>",
        r"<reasoning\b[^>]*>[\s\S]*?</reasoning>",
        r"<thought\b[^>]*>[\s\S]*?</thought>",
        r"<redacted_thinking\b[^>]*>[\s\S]*?</think>",
    )
    for _ in range(8):
        prev = t
        for pat in block_patterns:
            t = re.sub(pat, "", t, flags=re.IGNORECASE)
        if t == prev:
            break
    return t.strip()


def _normalize_tag_token(tok: str) -> str:
    t = tok.strip()
    while len(t) > 1 and t[-1] in ".,;:!?'\")]}":
        t = t[:-1].rstrip()
    while len(t) > 1 and t[0] in ".,;: '\"({[":
        t = t[1:].lstrip()
    return t


@functools.lru_cache(maxsize=1)
def load_tagger_prompt_text() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


@functools.lru_cache(maxsize=1)
def load_risk_prompt_text() -> str:
    return PROMPT_RISK_PATH.read_text(encoding="utf-8")


def parse_two_allowlists_from_prompt_md(text: str) -> tuple[frozenset[str], frozenset[str]]:
    """First ``Tags:`` block = report_section; second = decision_relevance."""
    lines = text.splitlines()
    blocks: list[list[str]] = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == "Tags:":
            chunk: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip():
                chunk.append(lines[i].strip())
                i += 1
            if chunk:
                blocks.append(chunk)
        else:
            i += 1
    if len(blocks) < 2:
        raise ValueError(
            "compound-excerpt-tagging.md must contain two Tags: sections (section, then stance)"
        ) from None
    return frozenset(blocks[0]), frozenset(blocks[1])


def parse_first_tags_allowlist(text: str) -> frozenset[str]:
    """First ``Tags:`` block only (risk prompt has a single block)."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip() == "Tags:":
            chunk: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip():
                chunk.append(lines[i].strip())
                i += 1
            if chunk:
                return frozenset(chunk)
        else:
            i += 1
    raise ValueError("compound-risk-profile.md must contain a Tags: section") from None


def chat_completion(
    client: OpenAI,
    model: str,
    system_prompt: str,
    user_content: str,
) -> str:
    seed_raw = os.environ.get("TAGGER_SEED", "").strip()
    seed_val = int(seed_raw) if seed_raw.isdigit() else None

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    def _complete() -> object:
        kw: dict[str, object] = dict(
            model=model,
            max_tokens=MAX_TAGGER_TOKENS,
            temperature=0,
            top_p=1,
            frequency_penalty=0,
            presence_penalty=0,
            messages=messages,
        )
        if seed_val is not None:
            kw["seed"] = seed_val
        try:
            return client.chat.completions.create(**kw, extra_body={"top_k": -1})
        except TypeError:
            return client.chat.completions.create(**kw)

    for attempt in range(MAX_RETRIES):
        try:
            response = _complete()
            content = response.choices[0].message.content
            return (content or "").strip()
        except RateLimitError:
            wait = (2**attempt) * 5
            print(f"  [RATE LIMIT] attempt {attempt + 1}/{MAX_RETRIES} — waiting {wait}s...", file=sys.stderr)
            time.sleep(wait)
        except Exception as e:
            msg = str(e)[:120]
            print(f"  [ERROR] attempt {attempt + 1}/{MAX_RETRIES}: {msg}", file=sys.stderr)
            if attempt < MAX_RETRIES - 1:
                time.sleep(2**attempt)
            else:
                return ""
    return ""


def _canonical_risk_token(tok: str, allowed: frozenset[str]) -> str | None:
    t = _normalize_tag_token(tok)
    t = _RISK_TOKEN_ALIASES.get(t, t)
    return t if t in allowed else None


def parse_risk_enum(raw: str, allowed: frozenset[str]) -> str | None:
    """Single allowlisted token; prefers last line after stripping thinking blocks."""
    stripped = strip_reasoning_markup(raw)
    lines_s = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    lines_r = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    sources: list[str] = []
    if lines_s:
        sources.append(lines_s[-1])
    if stripped:
        sources.append(stripped)
    if lines_r and (not lines_s or lines_r[-1] != lines_s[-1]):
        sources.append(lines_r[-1])
    sources.append(raw.strip())

    for blob in sources:
        if not blob:
            continue
        s = re.sub(r"```(?:\w*)?|```", "", blob).strip()
        parts = [_normalize_tag_token(x) for x in s.split()]
        parts = [x for x in parts if x]
        for p in parts:
            c = _canonical_risk_token(p, allowed)
            if c is not None:
                return c
    return None


def risk_severity_with_retries(
    client: OpenAI,
    model: str,
    user_content: str,
    allowed: frozenset[str],
) -> str | None:
    system_prompt = load_risk_prompt_text()
    for attempt in range(MAX_RISK_PARSE_ATTEMPTS):
        raw = chat_completion(client, model, system_prompt, user_content)
        val = parse_risk_enum(raw, allowed)
        if val is not None:
            return val
        if attempt < MAX_RISK_PARSE_ATTEMPTS - 1:
            print(
                f"  [risk] parse retry {attempt + 1}/{MAX_RISK_PARSE_ATTEMPTS} (no allowlisted token)",
                file=sys.stderr,
            )
    return None


def _parse_tokens_one_blob(
    blob: str,
    sections: frozenset[str],
    stances: frozenset[str],
) -> tuple[str | None, str | None]:
    s = blob.strip()
    s = re.sub(r"```(?:\w*)?|```", "", s).strip()
    parts = [_normalize_tag_token(t) for t in s.split()]
    parts = [t for t in parts if t]
    if len(parts) >= 2 and parts[0] in sections and parts[1] in stances:
        return parts[0], parts[1]
    sec = next((t for t in parts if t in sections), None)
    sta = next((t for t in parts if t in stances), None)
    return sec, sta


def parse_section_stance(
    raw: str,
    sections: frozenset[str],
    stances: frozenset[str],
) -> tuple[str | None, str | None]:
    """Prefer last line after stripping ``<think>`` blocks (reasoning models)."""
    stripped = strip_reasoning_markup(raw)
    lines_s = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    lines_r = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    sources: list[str] = []
    if lines_s:
        sources.append(lines_s[-1])
    if stripped:
        sources.append(stripped)
    if lines_r and (not lines_s or lines_r[-1] != lines_s[-1]):
        sources.append(lines_r[-1])
    sources.append(raw.strip())

    sec_out: str | None = None
    sta_out: str | None = None
    for blob in sources:
        if not blob:
            continue
        a, b = _parse_tokens_one_blob(blob, sections, stances)
        sec_out = sec_out or a
        sta_out = sta_out or b
        if sec_out and sta_out:
            return sec_out, sta_out
    return sec_out, sta_out


def main() -> int:
    default_in = REPO_ROOT / "data" / "Doxycycline" / "units.jsonl"

    parser = argparse.ArgumentParser(
        description="Enrich pump-science unit JSONL with section/stance tags and risk_severity (vLLM)."
    )
    parser.add_argument(
        "input",
        nargs="?",
        type=Path,
        default=default_in,
        help=f"Input JSONL (default: {default_in})",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Output JSONL (default: stdout if input is -, else <input_stem>_tagged.jsonl next to input).",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        metavar="NAME",
        help="Model id to use (default: auto-discovered from vLLM or TAGGER_MODEL/CLASSIFIER_MODEL/VALIDATOR_MODEL env vars).",
    )
    parser.add_argument(
        "--skip-risk",
        action="store_true",
        help="Only run section/stance tagging (skip compound-risk-profile round).",
    )
    args = parser.parse_args()

    in_path: Path = args.input

    prompt_text = load_tagger_prompt_text()
    sections, stances = parse_two_allowlists_from_prompt_md(prompt_text)
    risk_allowed = parse_first_tags_allowlist(load_risk_prompt_text())

    client = OpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)
    
    # Discover model
    model = args.model or get_available_model(
        client, 
        "TAGGER_MODEL", 
        ["CLASSIFIER_MODEL", "VALIDATOR_MODEL"]
    )
    risk_model = os.environ.get("TAGGER_RISK_MODEL") or model

    if str(in_path) == "-":
        inf = sys.stdin
        input_path_resolved = None
    else:
        input_path_resolved = in_path.expanduser().resolve()
        inf = input_path_resolved.open(encoding="utf-8")

    out_path = args.output
    if out_path is None:
        if str(in_path) == "-":
            outf = sys.stdout
        else:
            stem = input_path_resolved.stem if input_path_resolved else "units"
            parent = input_path_resolved.parent if input_path_resolved else Path.cwd()
            out_path = parent / f"{stem}_tagged.jsonl"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            outf = out_path.open("w", encoding="utf-8")
    else:
        if str(out_path) == "-":
            outf = sys.stdout
        else:
            out_path = out_path.expanduser().resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            outf = out_path.open("w", encoding="utf-8")

    n = 0
    try:
        for line in inf:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                continue

            user_msg = json.dumps(record, ensure_ascii=False)
            n += 1
            if n % 25 == 0:
                print(f"  [{n}] tagged...", file=sys.stderr)

            raw = chat_completion(client, model, load_tagger_prompt_text(), user_msg)
            sec, sta = parse_section_stance(raw, sections, stances)

            risk_val: str | None = None
            if not args.skip_risk:
                risk_val = risk_severity_with_retries(client, risk_model, user_msg, risk_allowed)

            enriched = {
                **record,
                "report_section": sec,
                "decision_relevance": sta,
                "risk_severity": risk_val,
            }
            outf.write(json.dumps(enriched, ensure_ascii=False) + "\n")
    finally:
        if inf is not sys.stdin:
            inf.close()
        if outf is not sys.stdout:
            outf.close()

    dest = out_path if out_path is not None and str(out_path) != "-" else "(stdout)"
    print(f"Done. Wrote {dest}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
