"""
Review generation pipeline.

Transforms grouped claim JSON (prepped JSON) into a structured review JSON
by extracting narratives, generating rationales via a local vLLM endpoint,
condensing multi-chunk rationales, and producing a top-level review statement.
After Stage 2, per-chunk rationales are written to articles/data/pre_condensed_rationales.json
unless overridden with --pre-condensed-dump.
"""

import argparse
import json
import os
import re
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
RATIONALE_GEN_MAX_TOKENS = 512  # Lower limit for per-chunk rationales to prevent runaway
CONDENSER_MAX_TOKENS = 4096     # Higher limit for condensing multiple rationales
TOKEN_CHUNK_TARGET = 1000

_BASE = Path(__file__).resolve().parent
BASE_DIR = _BASE.parent
PREPPED_PATH = BASE_DIR / "data" / "prepped.json"
MAPPINGS_PATH = _BASE / "mappings.json"
PROMPTS_DIR = BASE_DIR / "prompts"
OUTPUT_PATH = BASE_DIR / "data" / "review.json"
# Per-chunk rationales after Stage 2, before condenser (same dir as prepped/review).
PRE_CONDENSED_RATIONALES_PATH = BASE_DIR / "data" / "pre_condensed_rationales.json"


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: word_count / 0.75."""
    return int(len(text.split()) / 0.75)


def _load_prompt(filename: str) -> str:
    path = PROMPTS_DIR / filename
    return path.read_text(encoding="utf-8")


def _llm_call(
    client: OpenAI, system_prompt: str, user_content: str, max_tokens: int = CONDENSER_MAX_TOKENS
) -> str:
    """Single LLM call with retry logic matching existing codebase patterns."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                max_tokens=max_tokens,
                temperature=0,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            )
            text = response.choices[0].message.content.strip()
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            text = re.sub(r"<think>.*", "", text, flags=re.DOTALL).strip()
            
            # If response was truncated (doesn't end with sentence-ending punctuation),
            # truncate to last complete sentence to avoid mid-sentence cutoffs
            finish_reason = response.choices[0].finish_reason
            if finish_reason == "length" or (text and text[-1] not in ".!?"):
                # Find last period, exclamation, or question mark
                last_period = max(text.rfind("."), text.rfind("!"), text.rfind("?"))
                if last_period > 0:
                    text = text[: last_period + 1].strip()
            
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
                "narrative_chunks": [str, ...],
                "total_claims": int,
                "valid_claims": int,
                "invalid_claims": int
            },
            ...
        }
    """
    result = {}

    for group_key, group_data in prepped.items():
        score = group_data["score"]
        members = group_data.get("members", [])
        label = _get_label(group_key, mappings)

        # Count dimension-level statistics from members
        total_claims = len(members)
        valid_claims = sum(1 for m in members if m.get("verdict") == "supported")
        invalid_claims = sum(1 for m in members if m.get("verdict") == "unsupported")

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
            "total_claims": total_claims,
            "valid_claims": valid_claims,
            "invalid_claims": invalid_claims,
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
            # Add context to help the model understand this is partial evidence
            chunk_context = (
                f"[This is chunk {idx + 1} of {n_chunks} for this dimension. "
                f"Analyze only these claims without repeating analysis from other chunks.]\n\n{chunk}"
            )
            rationale = _llm_call(
                client, prompt_text, chunk_context, max_tokens=RATIONALE_GEN_MAX_TOKENS
            )
            rationales.append(rationale)

        result[group_key] = {
            "score": group_data["score"],
            "label": group_data["label"],
            "doc_name": group_data["doc_name"],
            "rationales": rationales,
            "total_claims": group_data["total_claims"],
            "valid_claims": group_data["valid_claims"],
            "invalid_claims": group_data["invalid_claims"],
        }

    return result


# ---------------------------------------------------------------------------
# Stage 3 – rationale_condenser
# ---------------------------------------------------------------------------

def _deduplicate_sentences(text: str) -> str:
    """
    Remove duplicate sentences from text to prevent LLM repetition loops.
    Preserves first occurrence of each sentence, removes subsequent duplicates.
    """
    if not text:
        return text
    
    # Split into sentences (simple split on .!? followed by space or end)
    sentences = re.split(r'([.!?])\s+', text)
    
    # Reconstruct sentences with their punctuation
    full_sentences = []
    for i in range(0, len(sentences) - 1, 2):
        if i + 1 < len(sentences):
            full_sentences.append(sentences[i] + sentences[i + 1])
    # Handle last sentence if it doesn't have trailing punctuation marker
    if len(sentences) % 2 == 1:
        full_sentences.append(sentences[-1])
    
    # Track seen sentences (normalized: lowercase, stripped)
    seen = set()
    deduplicated = []
    
    for sentence in full_sentences:
        normalized = sentence.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduplicated.append(sentence)
    
    return " ".join(deduplicated).strip()


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
        label = group_data["label"]
        total = group_data["total_claims"]
        valid = group_data["valid_claims"]
        invalid = group_data["invalid_claims"]

        if len(rationales) > 1:
            print(
                f"  [{label}] condensing "
                f"{len(rationales)} rationales ..."
            )
            # Build ground-truth stats line to prepend
            stats_line = (
                f"Of {total} claims identified as relating to {label}, "
                f"{valid} were confirmed as valid and {invalid} were found to be invalid."
            )
            
            # Prepend instruction with the correct stats
            user_message = (
                f"[GROUND TRUTH STATISTICS - Use this exact opening line:]\n"
                f"{stats_line}\n\n"
                f"[CRITICAL: The partial rationales below may contain 'in this subset' statistics that are "
                f"APPROXIMATIONS and may be INCORRECT. IGNORE those chunk-level counts completely. "
                f"Use ONLY the ground truth line above for your opening sentence.]\n\n"
                f"[Now synthesize the following partial rationales into a single coherent analysis.]\n\n"
                f"---\n\n"
                + "\n\n---\n\n".join(rationales)
            )
            
            condensed = _llm_call(client, condense_prompt, user_message)
            # Apply sentence-level deduplication to catch any repetition loops
            final_rationale = _deduplicate_sentences(condensed)
        else:
            final_rationale = rationales[0] if rationales else ""

        result[group_key] = {
            "score": group_data["score"],
            "label": label,
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
    parser = argparse.ArgumentParser(
        description="Build review.json from prepped grouped claims (vLLM)."
    )
    parser.add_argument(
        "--prepped",
        type=Path,
        default=PREPPED_PATH,
        help=f"Input JSON from prep.py (default: {PREPPED_PATH})",
    )
    parser.add_argument(
        "--mappings",
        type=Path,
        default=MAPPINGS_PATH,
        help=f"mappings.json path (default: {MAPPINGS_PATH})",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=OUTPUT_PATH,
        help=f"Write review JSON here (default: {OUTPUT_PATH})",
    )
    parser.add_argument(
        "--pre-condensed-dump",
        type=Path,
        default=PRE_CONDENSED_RATIONALES_PATH,
        help=(
            "Write Stage-2 per-chunk rationales (before condenser) here "
            f"(default: {PRE_CONDENSED_RATIONALES_PATH})"
        ),
    )
    args = parser.parse_args()

    prepped_path = args.prepped.expanduser().resolve()
    mappings_path = args.mappings.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    pre_condensed_path = args.pre_condensed_dump.expanduser().resolve()

    print("Loading inputs ...")
    prepped = json.loads(prepped_path.read_text(encoding="utf-8"))
    mappings = json.loads(mappings_path.read_text(encoding="utf-8"))

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

    pre_condensed_path.parent.mkdir(parents=True, exist_ok=True)
    pre_condensed_path.write_text(
        json.dumps(with_rationales, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\nPre-condensation rationales written to {pre_condensed_path}")

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
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(review_obj, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nReview written to {output_path}")


if __name__ == "__main__":
    main()
