#!/usr/bin/env python3
"""
Classify a paper into a review route (empirical | protocol | theoretical)
using articles/prompts/paper-classification.md and a local OpenAI-compatible API (vLLM).

Primary input (preferred):
  python classify-paper.py articles/data/<doc>/document_profile.json

Fallback input (full markdown):
  python classify-paper.py articles/data/<doc>/full.md

Auto-discovery: if a directory is passed, the script looks for document_profile.json
then full.md inside it.

  python classify-paper.py articles/data/<doc>/
  python classify-paper.py articles/data/<doc>/document_profile.json -o route.json

Env: VLLM_BASE_URL, VLLM_API_KEY, PAPER_CLASSIFY_MODEL (or VALIDATOR_MODEL).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from openai import OpenAI

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[misc, assignment]

_BASE = Path(__file__).resolve().parent
ARTICLES = _BASE.parent
PROMPTS_DIR = ARTICLES / "prompts"
CLASSIFY_PROMPT = PROMPTS_DIR / "paper-classification.md"

VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
VLLM_API_KEY = os.environ.get("VLLM_API_KEY", "none")
MODEL = os.environ.get("PAPER_CLASSIFY_MODEL") or os.environ.get("VALIDATOR_MODEL", "/model")

MAX_RETRIES = 4
MAX_TOKENS = 2048

# Maximum words to take from any single section body (keeps intro from dominating)
MAX_SECTION_WORDS = 300
# Maximum total words for the fallback full-markdown slice
MAX_FALLBACK_WORDS = 2500

VALID_ROUTES = frozenset({"empirical", "protocol", "theoretical"})
VALID_CONFIDENCE = frozenset({"high", "medium", "low"})

# Heading patterns that map to routing-relevant roles.
# Checked case-insensitively against section headings in document_profile.json.
_HEADING_ROLES: list[tuple[str, list[str]]] = [
    ("abstract",       ["abstract"]),
    ("intro",          ["introduction"]),
    ("conclusion",     ["conclusion", "discussion", "moving forward", "moving forwards",
                        "summary", "implications", "future"]),
    ("ethics",         ["competing interest", "conflict of interest", "ethics",
                        "ethical", "funding", "acknowledgment", "acknowledgement",
                        "author contribution", "declaration"]),
]


def _role_for_heading(heading: str) -> str | None:
    h = heading.lower().strip()
    for role, patterns in _HEADING_ROLES:
        if any(p in h for p in patterns):
            return role
    return None


def _truncate_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + " [...]"


# ---------------------------------------------------------------------------
# Input extraction
# ---------------------------------------------------------------------------

def build_context_from_profile(profile: dict) -> tuple[str, str]:
    """
    Extract high-signal sections from document_profile.json.

    Returns (title, formatted_sections_text).
    Sections selected: abstract, first intro section, last conclusion/discussion
    section, and all ethics/funding/acknowledgment sections.
    """
    bib = profile.get("bibliographic", {})
    title = str(bib.get("title") or "").strip()

    # abstract_markdown shortcut (often populated)
    abstract_direct = str(profile.get("content_pointers", {}).get("abstract_markdown") or "").strip()

    structure: list[dict] = profile.get("structure", [])

    collected: dict[str, list[str]] = {
        "abstract": [],
        "intro": [],
        "conclusion": [],
        "ethics": [],
    }
    # Track whether we've taken intro/conclusion already (take first intro, last conclusion)
    intro_taken = False
    conclusion_candidates: list[str] = []

    for section in structure:
        heading = str(section.get("heading") or "")
        body = str(section.get("body") or "").strip()
        if not body:
            continue
        role = _role_for_heading(heading)
        if role is None:
            continue
        truncated = _truncate_words(body, MAX_SECTION_WORDS)
        label = f"## {heading}\n{truncated}"
        if role == "abstract":
            collected["abstract"].append(label)
        elif role == "intro" and not intro_taken:
            collected["intro"].append(label)
            intro_taken = True
        elif role == "conclusion":
            conclusion_candidates.append(label)
        elif role == "ethics":
            collected["ethics"].append(label)

    # Use only the last conclusion/discussion section found
    if conclusion_candidates:
        collected["conclusion"] = [conclusion_candidates[-1]]

    # Use abstract_markdown shortcut if structure didn't yield one
    if not collected["abstract"] and abstract_direct:
        collected["abstract"].append(f"## Abstract\n{_truncate_words(abstract_direct, MAX_SECTION_WORDS)}")

    parts: list[str] = []
    for role in ("abstract", "intro", "conclusion", "ethics"):
        parts.extend(collected[role])

    return title, "\n\n".join(parts)


def build_context_from_markdown(text: str) -> tuple[str, str]:
    """
    Fallback: take the first MAX_FALLBACK_WORDS words of the full markdown.
    Strips OCR artifact tags before counting.
    """
    clean = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    clean = re.sub(r"<(?:page_number|img|watermark)[^>]*>.*?</(?:page_number|img|watermark)>",
                   "", clean, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r"<[^>]+>", "", clean)
    truncated = _truncate_words(clean.strip(), MAX_FALLBACK_WORDS)
    return "", truncated


def resolve_input(path: Path) -> tuple[Path, str, str, str]:
    """
    Resolve the input path to (source_path, input_type, title, context_text).
    input_type is "profile" or "markdown".
    """
    path = path.expanduser().resolve()

    if path.is_dir():
        profile_candidate = path / "document_profile.json"
        md_candidate = path / "full.md"
        if profile_candidate.is_file():
            path = profile_candidate
        elif md_candidate.is_file():
            path = md_candidate
        else:
            print(f"error: no document_profile.json or full.md found in {path}", file=sys.stderr)
            sys.exit(1)

    if not path.is_file():
        print(f"error: not a file: {path}", file=sys.stderr)
        sys.exit(1)

    if path.name == "document_profile.json":
        try:
            profile = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"error: invalid JSON in {path}: {e}", file=sys.stderr)
            sys.exit(1)
        title, context = build_context_from_profile(profile)
        if not context.strip():
            # Profile has no usable structure; try sibling full.md
            md_sibling = path.parent / "full.md"
            if md_sibling.is_file():
                print("warning: document_profile.json has no usable structure sections; "
                      "falling back to full.md", file=sys.stderr)
                text = md_sibling.read_text(encoding="utf-8")
                title, context = build_context_from_markdown(text)
                return md_sibling, "markdown", title, context
            print("error: document_profile.json has no usable structure and no sibling full.md",
                  file=sys.stderr)
            sys.exit(1)
        return path, "profile", title, context

    if path.suffix.lower() in {".md", ".markdown"}:
        # Try sibling document_profile.json first
        profile_sibling = path.parent / "document_profile.json"
        if profile_sibling.is_file():
            print(f"Found sibling document_profile.json; using it instead of {path.name}",
                  file=sys.stderr)
            try:
                profile = json.loads(profile_sibling.read_text(encoding="utf-8"))
                title, context = build_context_from_profile(profile)
                if context.strip():
                    return profile_sibling, "profile", title, context
            except (json.JSONDecodeError, Exception):
                pass  # fall through to markdown
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            print(f"error: markdown file is empty: {path}", file=sys.stderr)
            sys.exit(1)
        title, context = build_context_from_markdown(text)
        return path, "markdown", title, context

    print(f"error: expected document_profile.json, a .md file, or a directory; got: {path}",
          file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# LLM call and parsing
# ---------------------------------------------------------------------------

def _strip_thinking_tags(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"<think>.*", "", text, flags=re.DOTALL).strip()
    return text


def _extract_json_object(text: str) -> str:
    """Remove optional markdown fences and return the first JSON object substring."""
    text = _strip_thinking_tags(text.strip())
    fence = re.match(r"^```(?:json)?\s*\n?(.*)\n?```\s*$", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    return text.strip()


def _load_system_prompt() -> str:
    if not CLASSIFY_PROMPT.is_file():
        raise FileNotFoundError(f"Missing prompt file: {CLASSIFY_PROMPT}")
    return CLASSIFY_PROMPT.read_text(encoding="utf-8")


def classify_llm_call(client: OpenAI, system_prompt: str, user_content: str, model: str) -> str:
    last: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                max_tokens=MAX_TOKENS,
                temperature=0,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as exc:
            last = exc
            print(f"  [attempt {attempt}/{MAX_RETRIES}] LLM error: {exc}", file=sys.stderr)
            if attempt < MAX_RETRIES:
                time.sleep(min(2**attempt, 30))
    assert last is not None
    raise last


def parse_classification(raw: str) -> dict:
    payload = _extract_json_object(raw)
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Model did not return valid JSON: {e}\n---\n{raw[:2000]}\n---"
        ) from e
    if not isinstance(obj, dict):
        raise ValueError("JSON root must be an object")
    for key in ("route", "confidence", "document_type", "reasoning"):
        if key not in obj:
            raise ValueError(f"Missing required field: {key}")
    route = str(obj["route"]).strip().lower()
    if route not in VALID_ROUTES:
        raise ValueError(f"Invalid route {route!r}; must be one of {sorted(VALID_ROUTES)}")
    conf = str(obj["confidence"]).strip().lower()
    if conf not in VALID_CONFIDENCE:
        raise ValueError(f"Invalid confidence {conf!r}; must be one of {sorted(VALID_CONFIDENCE)}")
    obj["route"] = route
    obj["confidence"] = conf
    obj["document_type"] = str(obj["document_type"]).strip()
    obj["reasoning"] = str(obj["reasoning"]).strip()
    return obj


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    repo_root = ARTICLES.parent
    if load_dotenv:
        load_dotenv(repo_root / ".env")

    p = argparse.ArgumentParser(
        description=(
            "Classify a paper into a review route (empirical | protocol | theoretical). "
            "Pass document_profile.json (preferred), full.md, or the paper's data directory."
        )
    )
    p.add_argument(
        "input",
        type=Path,
        help=(
            "document_profile.json, full.md, or directory containing either "
            "(e.g. articles/data/<doc>/)"
        ),
    )
    p.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help="Write classification JSON here (default: <input_parent>/paper_route.json)",
    )
    p.add_argument("--base-url", type=str, default=VLLM_BASE_URL)
    p.add_argument("--api-key", type=str, default=VLLM_API_KEY)
    p.add_argument("--model", type=str, default=MODEL)
    args = p.parse_args()

    source_path, input_type, title, context = resolve_input(args.input)

    if not context.strip():
        print("error: could not extract any text to classify", file=sys.stderr)
        sys.exit(1)

    out_path = args.output
    if out_path is None:
        out_path = source_path.parent / "paper_route.json"
    else:
        out_path = out_path.expanduser().resolve()

    system_prompt = _load_system_prompt()

    title_line = f"Title: {title}\n\n" if title else ""
    user_message = (
        f"Classify the following paper.\n\n"
        f"{title_line}"
        f"---BEGIN_PAPER---\n"
        f"{context}\n"
        f"---END_PAPER---"
    )

    word_count = len(context.split())
    print(
        f"Input type : {input_type}\n"
        f"Source     : {source_path}\n"
        f"Model      : {args.model}\n"
        f"Context    : ~{word_count} words",
        file=sys.stderr,
    )

    client = OpenAI(base_url=args.base_url, api_key=args.api_key)
    raw = classify_llm_call(client, system_prompt, user_message, args.model)
    result = parse_classification(raw)

    envelope = {
        "source": str(source_path),
        "input_type": input_type,
        "model": args.model,
        "base_url": args.base_url,
        "classification": result,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(envelope, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(envelope, indent=2, ensure_ascii=False))
    print(f"\nWrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
