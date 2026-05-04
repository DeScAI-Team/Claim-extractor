#!/usr/bin/env python3
r"""Generate a three-section compound review from grouped_by_stance.json via vLLM.

Reads the output of ``group_by_stance.py`` and calls the LLM three times:

1. ``prompts/pump-science-scientific-grounding-evaluation.md`` — receives ``supports_exploration``
   members; produces the ``scientific_grounding`` paragraph.
2. ``prompts/pump-science-risk-statement-evaluation.md`` — receives ``raises_caution`` +
   ``risk_information`` members; produces the ``risk`` paragraph.
3. ``prompts/pump-science-review-statement-evaluation.md`` — receives a compact context bundle
   (the two paragraphs above + quantitative metadata from grouped_by_stance.json +
   coverage from the nearest prepared_report_*.json in the compound directory);
   produces the ``review_statement`` paragraph.

Output is written as ``<compound_dir>/<compound_name>-review.json``, or to a path
supplied via ``-o``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
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
GROUNDING_PROMPT_PATH = REPO_ROOT / "prompts" / "pump-science-scientific-grounding-evaluation.md"
RISK_PROMPT_PATH = REPO_ROOT / "prompts" / "pump-science-risk-statement-evaluation.md"
STATEMENT_PROMPT_PATH = REPO_ROOT / "prompts" / "pump-science-review-statement-evaluation.md"

if load_dotenv is not None:
    load_dotenv(REPO_ROOT / ".env")

VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
VLLM_API_KEY = os.environ.get("VLLM_API_KEY", "none")

# Model discovery happens later in main() after client is created
MAX_RETRIES = 4
MAX_REVIEWER_TOKENS = max(256, int(os.environ.get("REVIEWER_MAX_TOKENS", "2048")))

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
        t = t[best_idx + best_len:].lstrip()
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
    # Strip unclosed opening think block (model output thinking but never closed the tag).
    t = re.sub(r"<think\b[^>]*>[\s\S]*$", "", t, flags=re.IGNORECASE).strip()
    return t.strip()


def call_llm(client: OpenAI, model: str, system_prompt: str, user_content: str) -> str:
    """Send a chat completion and return the stripped response text."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    def _complete() -> object:
        kw: dict[str, object] = dict(
            model=model,
            max_tokens=MAX_REVIEWER_TOKENS,
            temperature=0,
            top_p=1,
            frequency_penalty=0,
            presence_penalty=0,
            messages=messages,
        )
        try:
            return client.chat.completions.create(**kw, extra_body={"top_k": -1})
        except TypeError:
            return client.chat.completions.create(**kw)

    for attempt in range(MAX_RETRIES):
        try:
            response = _complete()
            content = response.choices[0].message.content
            return strip_reasoning_markup((content or "").strip())
        except RateLimitError:
            wait = (2 ** attempt) * 5
            print(
                f"  [RATE LIMIT] attempt {attempt + 1}/{MAX_RETRIES} — waiting {wait}s...",
                file=sys.stderr,
            )
            time.sleep(wait)
        except Exception as e:
            msg = str(e)[:120]
            print(
                f"  [ERROR] attempt {attempt + 1}/{MAX_RETRIES}: {msg}",
                file=sys.stderr,
            )
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
            else:
                return ""
    return ""


def default_output_path(grouped_input: Path, compound_name: str) -> Path:
    safe = re.sub(r'[<>:"/\\|?*]', "_", compound_name).strip()
    # Output to reviews/<compound>/ instead of data/<compound>/
    reviews_dir = REPO_ROOT.parent / "reviews" / safe
    reviews_dir.mkdir(parents=True, exist_ok=True)
    return reviews_dir / f"{safe}-review.json"


def load_prepared_report_context(compound_dir: Path) -> dict:
    """
    Find the most recent prepared_report_*.json (non-agent) in compound_dir and
    return a slim context dict: coverage + report_timestamp.
    Falls back gracefully if no file is found or parsing fails.
    """
    candidates = sorted(
        [p for p in compound_dir.glob("prepared_report_*.json")
         if not p.name.endswith("_agent.json")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        # Also try agent variants as a fallback
        candidates = sorted(
            compound_dir.glob("prepared_report_*_agent.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    if not candidates:
        return {"coverage": None, "report_timestamp": None}

    try:
        with candidates[0].open(encoding="utf-8") as fh:
            prepared = json.load(fh)
        meta = prepared.get("metadata", {})
        return {
            "coverage": meta.get("coverage"),
            "report_timestamp": meta.get("timestamp"),
        }
    except Exception as exc:
        print(f"  [WARN] Could not load prepared report context: {exc}", file=sys.stderr)
        return {"coverage": None, "report_timestamp": None}


def build_statement_context(
    compound_name: str,
    grouped: dict,
    grounding_text: str,
    risk_text: str,
    prepared_ctx: dict,
) -> dict:
    """Assemble the compact bundle passed to the review-statement prompt."""
    scores = grouped.get("scores", {}).get("scientific_grounding", {})
    by_stance = grouped.get("by_stance", {})

    tag_counts = {
        stance: by_stance.get(stance, {}).get("count", 0)
        for stance in (
            "supports_exploration",
            "raises_caution",
            "risk_information",
            "mixed_or_unclear",
            "context_only",
            "unmapped",
        )
    }
    tag_counts["total"] = grouped.get("total_units", sum(tag_counts.values()))

    return {
        "compound_name": compound_name,
        "scientific_grounding": grounding_text,
        "risk": risk_text,
        "scientific_grounding_score": scores.get("score"),
        "tag_counts": tag_counts,
        "coverage": prepared_ctx.get("coverage"),
        "report_timestamp": prepared_ctx.get("report_timestamp"),
    }


def main() -> int:
    default_in = REPO_ROOT / "data" / "Doxycycline" / "grouped_by_stance.json"

    parser = argparse.ArgumentParser(
        description=(
            "Generate a longevity review (scientific_grounding + risk + review_statement) "
            "from grouped_by_stance.json via vLLM."
        )
    )
    parser.add_argument(
        "input",
        nargs="?",
        type=Path,
        default=default_in,
        help=f"grouped_by_stance.json produced by group_by_stance.py (default: {default_in})",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        metavar="PATH",
        help="Output JSON path (default: <compound_dir>/<compound_name>-review.json).",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        metavar="NAME",
        help="Model id to use (default: auto-discovered from vLLM or REVIEWER_MODEL/TAGGER_MODEL/CLASSIFIER_MODEL/VALIDATOR_MODEL env vars).",
    )
    args = parser.parse_args()

    in_path: Path = args.input.expanduser().resolve()

    print(f"Loading {in_path}", file=sys.stderr)
    with in_path.open(encoding="utf-8") as fh:
        grouped = json.load(fh)

    compound_name: str = grouped.get("compound_name") or in_path.parent.name
    by_stance: dict = grouped.get("by_stance", {})

    supports = by_stance.get("supports_exploration", {}).get("members", [])
    raises = by_stance.get("raises_caution", {}).get("members", [])
    risk_info = by_stance.get("risk_information", {}).get("members", [])
    risk_members = raises + risk_info

    print(
        f"  compound={compound_name!r}  "
        f"supports_exploration={len(supports)}  "
        f"raises_caution={len(raises)}  "
        f"risk_information={len(risk_info)}",
        file=sys.stderr,
    )

    # Load coverage + timestamp context from the nearest prepared report
    prepared_ctx = load_prepared_report_context(in_path.parent)
    if prepared_ctx.get("coverage"):
        present = [k for k, v in prepared_ctx["coverage"].items() if v.get("present")]
        print(f"  prepared report context: coverage present for {present}", file=sys.stderr)
    else:
        print("  prepared report context: not found (coverage will be null)", file=sys.stderr)

    grounding_prompt = GROUNDING_PROMPT_PATH.read_text(encoding="utf-8")
    risk_prompt = RISK_PROMPT_PATH.read_text(encoding="utf-8")
    statement_prompt = STATEMENT_PROMPT_PATH.read_text(encoding="utf-8")

    client = OpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)
    
    # Discover model
    model = args.model or get_available_model(
        client,
        "REVIEWER_MODEL",
        ["TAGGER_MODEL", "CLASSIFIER_MODEL", "VALIDATOR_MODEL"]
    )

    print("  [1/3] Generating scientific_grounding...", file=sys.stderr)
    if supports:
        grounding_text = call_llm(
            client,
            model,
            grounding_prompt,
            json.dumps(supports, ensure_ascii=False),
        )
    else:
        grounding_text = (
            "No units tagged supports_exploration were present in this grouped file; "
            "scientific grounding cannot be assessed from this bundle."
        )

    print("  [2/3] Generating risk statement...", file=sys.stderr)
    if risk_members:
        risk_text = call_llm(
            client,
            model,
            risk_prompt,
            json.dumps(risk_members, ensure_ascii=False),
        )
    else:
        risk_text = (
            "No units tagged raises_caution or risk_information were present in this "
            "grouped file; risk cannot be assessed from this bundle."
        )

    print("  [3/3] Generating review_statement...", file=sys.stderr)
    statement_ctx = build_statement_context(
        compound_name, grouped, grounding_text, risk_text, prepared_ctx
    )
    review_statement = call_llm(
        client,
        model,
        statement_prompt,
        json.dumps(statement_ctx, ensure_ascii=False),
    )

    _now = datetime.now(timezone.utc)
    review_date = f"{_now.strftime('%B')} {_now.day}, {_now.year}"

    scores = grouped.get("scores", {})
    grounding_score = scores.get("scientific_grounding", {}).get("score")
    risk_score = scores.get("aggregate_risk", {}).get("score")

    out_path: Path = (
        args.output.expanduser().resolve()
        if args.output is not None
        else default_output_path(in_path, compound_name)
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    review = {
        "compound_name": compound_name,
        "review_date": review_date,
        "review_statement": review_statement,
        "categories": {
            "scientific_grounding": {
                "score": grounding_score,
                "rationale": grounding_text,
            },
            "risk_assessment": {
                "score": risk_score,
                "rationale": risk_text,
            },
        },
    }

    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(review, fh, ensure_ascii=False, indent=2)

    print(f"Done. Wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
