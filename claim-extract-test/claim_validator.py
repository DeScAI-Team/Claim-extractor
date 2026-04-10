"""
Claim Validation Module — Phase 2 of the DeScAi pipeline.

Reads extraction JSONL (final_claims_for_audit.jsonl), assembles key sections
(methodology, results, conclusion) from the source chunks for each document,
and validates each claim via async vLLM calls.

Verdicts: "supported" | "unsupported" | "insufficient_info"
Malformed / error responses are flagged with validation_error=True, not dropped.
"""

import json
import asyncio
import os
import re
from collections import defaultdict

from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

# === CONFIG ===
VLLM_BASE_URL         = os.environ.get("VLLM_BASE_URL",            "http://localhost:8000/v1")
VLLM_API_KEY          = os.environ.get("VLLM_API_KEY",             "none")

MODEL                 = os.environ.get("VALIDATOR_MODEL",           "mixtral-8x7b-instruct")
CONCURRENCY           = int(os.environ.get("VALIDATOR_CONCURRENCY", "15"))

MAX_RETRIES           = 3
KEY_SECTION_MAX_CHARS = int(os.environ.get("VALIDATOR_KEY_SECTION_MAX_CHARS", "24000"))

INPUT_CLAIMS    = os.path.join(os.path.dirname(__file__), "final_claims_for_audit.jsonl")
SOURCE_CHUNKS   = os.environ.get(
    "VALIDATOR_SOURCE_CHUNKS",
    os.path.join(os.path.dirname(__file__), "text_knowledge_base.jsonl"),
)
OUTPUT_VALIDATED = os.path.join(os.path.dirname(__file__), "validated_claims.jsonl")

KEY_SECTION_KEYWORDS = [
    "method", "result", "conclusion", "finding",
    "discussion", "experiment", "analysis", "outcome",
]
SEMANTIC_KEY_BUCKETS = {"method", "result", "conclusion"}

METHOD_SECTION_HINTS = ["method", "materials", "approach", "experiment", "protocol"]
RESULT_SECTION_HINTS = ["result", "finding", "analysis", "observation"]
CONCLUSION_SECTION_HINTS = ["conclusion", "summary", "implication", ]

VALIDATION_PROMPT = """\
You are a scientific claim validator. Given key sections of a research document \
(methodology, results, conclusion) and a single extracted claim, assess whether \
the sections support the claim. Respond ONLY in valid JSON with: \
"verdict" ("supported" | "unsupported" | "insufficient_info"), \
"rationale" (max 50 words), \
"relevancy_score" (0.0-1.0; 0.0 = general field knowledge, 1.0 = specific finding of this researcher).

KEY SECTIONS: {key_sections}

CLAIM: {claim}"""


def _extract_first_json_object(text: str) -> str:
    """Return the first balanced JSON object substring from text.

    This is resilient to leading/trailing prose and code fences.
    """
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object start found")

    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(text[start:], start=start):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError("unterminated JSON object")




def load_key_sections(source_jsonl: str) -> dict:
    """Build doc_name -> key-section text with section-aware truncation.

    We prioritize coverage across methodology/results/conclusion by reserving
    budget per bucket, then filling with "other" key sections.
    """
    doc_sections: dict = defaultdict(lambda: {
        "method": [],
        "result": [],
        "conclusion": [],
        "other": [],
    })

    def classify_heading(heading: str) -> str:
        if any(h in heading for h in METHOD_SECTION_HINTS):
            return "method"
        if any(h in heading for h in RESULT_SECTION_HINTS):
            return "result"
        if any(h in heading for h in CONCLUSION_SECTION_HINTS):
            return "conclusion"
        return "other"

    def trim_join(parts: list, budget: int) -> str:
        if budget <= 0 or not parts:
            return ""
        out = []
        used = 0
        for part in parts:
            if used >= budget:
                break
            remaining = budget - used
            if len(part) <= remaining:
                out.append(part)
                used += len(part)
            else:
                out.append(part[:remaining])
                used = budget
        return "\n\n".join(out)

    if not os.path.exists(source_jsonl):
        print(f"[WARN] Source chunks file not found: {source_jsonl}")
        return {}

    with open(source_jsonl) as f:
        for line in f:
            rec = json.loads(line)
            heading = rec.get("section_heading", "").lower()
            semantic_category = str(rec.get("semantic_category", "")).strip().lower()

            bucket = None
            if semantic_category in SEMANTIC_KEY_BUCKETS:
                bucket = semantic_category
            elif any(kw in heading for kw in KEY_SECTION_KEYWORDS):
                bucket = classify_heading(heading)

            if bucket:
                doc_sections[rec["doc_name"]][bucket].append(
                    f"[{rec['section_heading']}]\n{rec.get('text', '')}"
                )

    packed = {}
    for doc, buckets in doc_sections.items():
        core_budget = int(KEY_SECTION_MAX_CHARS * 0.9)
        core_each = max(core_budget // 3, 1)
        other_budget = max(KEY_SECTION_MAX_CHARS - (core_each * 3), 0)

        parts = [
            trim_join(buckets["method"], core_each),
            trim_join(buckets["result"], core_each),
            trim_join(buckets["conclusion"], core_each),
            trim_join(buckets["other"], other_budget),
        ]
        text = "\n\n".join(p for p in parts if p).strip()
        packed[doc] = text[:KEY_SECTION_MAX_CHARS]

    return packed

async def validate_claim(
    client: AsyncOpenAI,
    semaphore: asyncio.Semaphore,
    record: dict,
    key_sections_map: dict,
) -> dict:
    """Validate a single claim, retrying on transient errors.

    Never drops a record — malformed / error responses are flagged with
    validation_error=True so downstream phases can filter or re-run them.
    """
    key_sections = key_sections_map.get(
        record.get("doc_name", ""),
        "No key sections available for this document.",
    )
    base_prompt = VALIDATION_PROMPT.format(
        key_sections=key_sections,
        claim=record.get("claim", ""),
    )
    current_max_tokens = 256

    async with semaphore:
        for attempt in range(MAX_RETRIES):
            try:
                prompt = base_prompt
                if attempt > 0:
                    prompt += (
                        "\n\nIMPORTANT: Return one complete valid JSON object only. "
                        "Do not include markdown or extra text."
                    )
                response = await client.chat.completions.create(
                    model=MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Respond with exactly one valid JSON object and nothing else."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    # Requires OpenAI-compatible structured output; vLLM supports this for many chat models—remove if your server rejects it.
                    response_format={"type": "json_object"},
                    max_tokens=current_max_tokens,
                    temperature=0.0,
                )
                content = response.choices[0].message.content
                raw = (content or "").strip()
                clean = re.sub(r"```(?:json)?|```", "", raw).strip()
                json_blob = _extract_first_json_object(clean)
                parsed = json.loads(json_blob)

                verdict = parsed.get("verdict", "")
                if verdict not in ("supported", "unsupported", "insufficient_info"):
                    raise ValueError(f"unexpected verdict: {verdict!r}")

                relevancy = float(parsed.get("relevancy_score", 0.0))
                if not 0.0 <= relevancy <= 1.0:
                    raise ValueError(f"relevancy_score out of range: {relevancy}")

                return {
                    **record,
                    "verdict": verdict,
                    "rationale": parsed.get("rationale", ""),
                    "relevancy_score": relevancy,
                }

            except json.JSONDecodeError as exc:
                msg = f"JSON parse error (attempt {attempt + 1}): {exc}"
                print(f"  [MALFORMED] chunk {record.get('chunk_id')} — {msg}")
                current_max_tokens = min(512, current_max_tokens + 64)
                if attempt == MAX_RETRIES - 1:
                    return {
                        **record,
                        "verdict": "parse_error",
                        "rationale": msg[:120],
                        "relevancy_score": None,
                        "validation_error": True,
                    }

            except ValueError as exc:
                msg = str(exc)
                print(f"  [INVALID]   chunk {record.get('chunk_id')} — {msg}")
                if "unterminated JSON object" in msg:
                    current_max_tokens = min(512, current_max_tokens + 64)
                if attempt == MAX_RETRIES - 1:
                    return {
                        **record,
                        "verdict": "validation_error",
                        "rationale": msg[:120],
                        "relevancy_score": None,
                        "validation_error": True,
                    }

            except Exception as exc:
                msg = str(exc)[:120]
                print(f"  [ERROR]     chunk {record.get('chunk_id')} attempt {attempt + 1} — {msg}")
                if attempt == MAX_RETRIES - 1:
                    return {
                        **record,
                        "verdict": "error",
                        "rationale": msg,
                        "relevancy_score": None,
                        "validation_error": True,
                    }
                await asyncio.sleep(2 ** attempt)

    return {
        **record,
        "verdict": "error",
        "rationale": "exhausted retries",
        "relevancy_score": None,
        "validation_error": True,
    }


async def main() -> None:
    client    = AsyncOpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)
    semaphore = asyncio.Semaphore(CONCURRENCY)

    print("Loading key sections from source chunks...")
    key_sections_map = load_key_sections(SOURCE_CHUNKS)
    print(f"  {len(key_sections_map)} document(s) with key sections loaded.")

    claims: list = []
    with open(INPUT_CLAIMS) as f:
        for line in f:
            line = line.strip()
            if line:
                claims.append(json.loads(line))
    print(f"  {len(claims)} claims to validate.\n")

    missing_docs = sorted(
        {
            rec.get("doc_name", "")
            for rec in claims
            if rec.get("doc_name", "") not in key_sections_map
        }
    )
    if missing_docs:
        print(
            f"  WARNING: {len(missing_docs)} claim document(s) have no key sections "
            "in SOURCE_CHUNKS. Validation quality will be poor for those docs."
        )
        print(f"  Missing docs sample: {missing_docs[:5]}\n")

    counter = [0]

    async def tracked(record: dict) -> dict:
        result = await validate_claim(client, semaphore, record, key_sections_map)
        counter[0] += 1
        n = counter[0]
        if n % 25 == 0 or n == len(claims):
            print(f"  [{n}/{len(claims)}] processed...")
        return result

    results = await asyncio.gather(*[tracked(rec) for rec in claims])

    errors = sum(1 for r in results if r.get("validation_error"))
    with open(OUTPUT_VALIDATED, "w") as f:
        for rec in results:
            f.write(json.dumps(rec) + "\n")

    print(f"\nDone. {len(results)} records written to: {OUTPUT_VALIDATED}")
    if errors:
        print(f"  WARNING: {errors} record(s) flagged with validation_error=True — review manually.")


if __name__ == "__main__":
    asyncio.run(main())
