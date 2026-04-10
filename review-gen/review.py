"""
Review generation pipeline.

Transforms grouped claim JSON (prepped.json) into a structured review JSON
by extracting narratives, generating rationales via a local vLLM endpoint,
condensing multi-chunk rationales, and producing a top-level review statement.
"""

import json
import os
import re
import sys
from datetime import date
from pathlib import Path

from openai import OpenAI

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
VLLM_API_KEY = os.environ.get("VLLM_API_KEY", "none")
MODEL = os.environ.get("VALIDATOR_MODEL", "/model")

MAX_RETRIES = 4
RATIONALE_MAX_TOKENS = 4096
TOKEN_CHUNK_TARGET = 1000

BASE_DIR = Path(__file__).resolve().parent.parent
PREPPED_PATH = BASE_DIR / "group-and-score" / "prepped.json"
MAPPINGS_PATH = BASE_DIR / "group-and-score" / "mappings.json"
PROMPTS_DIR = BASE_DIR / "prompts"
OUTPUT_PATH = Path(__file__).resolve().parent / "review.json"


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: word_count / 0.75."""
    return int(len(text.split()) / 0.75)


def _load_prompt(filename: str) -> str:
    path = PROMPTS_DIR / filename
    return path.read_text(encoding="utf-8")


def _llm_call(client: OpenAI, system_prompt: str, user_content: str) -> str:
    """Single LLM call with retry logic matching existing codebase patterns."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                max_tokens=RATIONALE_MAX_TOKENS,
                temperature=0,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            )
            text = response.choices[0].message.content.strip()
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            text = re.sub(r"<think>.*", "", text, flags=re.DOTALL).strip()
            return text
        except Exception as exc:
            print(f"  [attempt {attempt}/{MAX_RETRIES}] LLM error: {exc}")
            if attempt == MAX_RETRIES:
                raise
    return ""


def _get_label(group_key: str, mappings: dict) -> str:
    """Resolve human-readable label from mappings.json."""
    if group_key in mappings.get("dimensions", {}):
        return mappings["dimensions"][group_key]["label"]
    if group_key == "cross_cutting":
        return mappings.get("cross_cutting", {}).get("label", "Cross-Cutting / Flags")
    return group_key.replace("_", " ").title()


# ---------------------------------------------------------------------------
# Stage 1 – narrative_finder
# ---------------------------------------------------------------------------

def narrative_finder(prepped: dict, mappings: dict) -> dict:
    """
    Extract claim_narrative fields per group and chunk them into ~1000-token
    segments.  Never splits a narrative mid-sentence.

    Returns:
        {
            "group_key": {
                "score": float,
                "label": str,
                "doc_name": str,
                "narrative_chunks": [str, ...]
            },
            ...
        }
    """
    result = {}

    for group_key, group_data in prepped.items():
        score = group_data["score"]
        members = group_data.get("members", [])
        label = _get_label(group_key, mappings)

        doc_name = ""
        narratives = []
        for member in members:
            narratives.append(member["claim_narrative"])
            if not doc_name:
                doc_name = member.get("doc_name", "")

        chunks: list[str] = []
        current_chunk_parts: list[str] = []
        current_tokens = 0

        for narrative in narratives:
            narr_tokens = _estimate_tokens(narrative)
            if current_tokens + narr_tokens > TOKEN_CHUNK_TARGET and current_chunk_parts:
                chunks.append("\n\n".join(current_chunk_parts))
                current_chunk_parts = [narrative]
                current_tokens = narr_tokens
            else:
                current_chunk_parts.append(narrative)
                current_tokens += narr_tokens

        if current_chunk_parts:
            chunks.append("\n\n".join(current_chunk_parts))

        result[group_key] = {
            "score": score,
            "label": label,
            "doc_name": doc_name,
            "narrative_chunks": chunks,
        }

    return result


# ---------------------------------------------------------------------------
# Stage 2 – rationale_gen
# ---------------------------------------------------------------------------

def rationale_gen(
    chunked: dict, prompt_text: str, client: OpenAI
) -> dict:
    """
    For each group, for each narrative chunk, call the LLM with the rationale
    generation prompt to produce a rationale.

    Returns the same structure with 'rationales' replacing 'narrative_chunks'.
    """
    result = {}

    for group_key, group_data in chunked.items():
        rationales: list[str] = []
        n_chunks = len(group_data["narrative_chunks"])

        for idx, chunk in enumerate(group_data["narrative_chunks"]):
            print(
                f"  [{group_data['label']}] generating rationale "
                f"({idx + 1}/{n_chunks}) ..."
            )
            rationale = _llm_call(client, prompt_text, chunk)
            rationales.append(rationale)

        result[group_key] = {
            "score": group_data["score"],
            "label": group_data["label"],
            "doc_name": group_data["doc_name"],
            "rationales": rationales,
        }

    return result


# ---------------------------------------------------------------------------
# Stage 3 – rationale_condenser
# ---------------------------------------------------------------------------

def rationale_condenser(
    groups: dict, condense_prompt: str, client: OpenAI
) -> dict:
    """
    For groups with multiple rationales, condense them into a single rationale
    via the LLM.  Groups with a single rationale are left untouched.
    """
    result = {}

    for group_key, group_data in groups.items():
        rationales = group_data["rationales"]

        if len(rationales) > 1:
            print(
                f"  [{group_data['label']}] condensing "
                f"{len(rationales)} rationales ..."
            )
            combined = "\n\n---\n\n".join(rationales)
            condensed = _llm_call(client, condense_prompt, combined)
            final_rationale = condensed
        else:
            final_rationale = rationales[0] if rationales else ""

        result[group_key] = {
            "score": group_data["score"],
            "label": group_data["label"],
            "doc_name": group_data["doc_name"],
            "rationale": final_rationale,
        }

    return result


# ---------------------------------------------------------------------------
# Stage 4 – review_statement_gen
# ---------------------------------------------------------------------------

def review_statement_gen(
    review_obj: dict, statement_prompt: str, client: OpenAI
) -> str:
    """
    Generate a top-level review statement from the assembled review object.
    """
    context = json.dumps(
        {
            "research_dao_name": review_obj["research_dao_name"],
            "average_score": review_obj["average_score"],
            "categories": {
                k: {"score": v["score"], "rationale": v["rationale"]}
                for k, v in review_obj["categories"].items()
            },
        },
        indent=2,
    )

    print("  Generating top-level review statement ...")
    return _llm_call(client, statement_prompt, context)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    print("Loading inputs ...")
    prepped = json.loads(PREPPED_PATH.read_text(encoding="utf-8"))
    mappings = json.loads(MAPPINGS_PATH.read_text(encoding="utf-8"))

    rationale_prompt = _load_prompt("rationale_generation_prompt_v2.md")
    condense_prompt = _load_prompt("rationale_condenser_prompt.md")
    statement_prompt = _load_prompt("review_statement_prompt.md")

    client = OpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)

    # Stage 1
    print("\n=== Stage 1: narrative_finder ===")
    chunked = narrative_finder(prepped, mappings)
    for key, data in chunked.items():
        print(
            f"  {data['label']}: {len(data['narrative_chunks'])} chunk(s)"
        )

    # Stage 2
    print("\n=== Stage 2: rationale_gen ===")
    with_rationales = rationale_gen(chunked, rationale_prompt, client)

    # Stage 3
    print("\n=== Stage 3: rationale_condenser ===")
    condensed = rationale_condenser(with_rationales, condense_prompt, client)

    # Determine doc_name from first group that has one
    doc_name = ""
    for group_data in condensed.values():
        if group_data.get("doc_name"):
            doc_name = group_data["doc_name"]
            break

    # Build categories and compute average score
    categories = {}
    scores = []
    for group_key, group_data in condensed.items():
        scores.append(group_data["score"])
        categories[group_key] = {
            "score": group_data["score"],
            "rationale": group_data["rationale"],
        }

    average_score = round(sum(scores) / len(scores), 2) if scores else 0

    review_obj = {
        "research_dao_name": doc_name,
        "review_date": date.today().strftime("%B %d, %Y"),
        "average_score": average_score,
        "review_statement": "",
        "categories": categories,
    }

    # Stage 4
    print("\n=== Stage 4: review_statement_gen ===")
    review_obj["review_statement"] = review_statement_gen(
        review_obj, statement_prompt, client
    )

    # Write output
    OUTPUT_PATH.write_text(
        json.dumps(review_obj, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nReview written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
