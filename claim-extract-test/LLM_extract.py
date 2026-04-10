import json
import time
import os
import re
from openai import OpenAI, RateLimitError
from dotenv import load_dotenv

load_dotenv()

VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
VLLM_API_KEY = os.environ.get("VLLM_API_KEY", "none")
MODEL = os.environ.get("VALIDATOR_MODEL", "mixtral-8x7b-instruct")
MAX_RETRIES = 4

client = OpenAI(base_url=VLLM_BASE_URL, api_key=VLLM_API_KEY)


def extract_hybrid_claims(record):
    """
    Sends spaCy-tagged text to the LLM for reasoning-based claim extraction.
    <Scientific_claim> tokens act as hints; the model also surfaces hidden claims
    embedded in surrounding context.
    """
    prompt = f"""You are a scientific claim extractor working on research paper chunks.

Your job:
1. The text below may contain <Scientific_claim>...</Scientific_claim> tags, treat these as strong hints for explicit claims.
2. Also identify any additional claims hidden in the surrounding context that the tags missed.
3. Every extracted claim must be DECONTEXTUALIZED, fully self-contained without needing the surrounding text.
4. Classify each claim as one of: Fact, Assertion, or Roadmap.
   - Fact: an established or measured finding
   - Assertion: an argued or proposed interpretation
   - Roadmap: a stated goal, gap, or future direction

Respond ONLY with one JSON object per line (JSONL). No markdown, no explanation.
Fields: "claim_type", "claim"

TEXT:
{record["text"]}"""

    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                max_tokens=1024,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
            content = response.choices[0].message.content
            return (content or "").strip()
        except RateLimitError:
            wait = (2 ** attempt) * 5
            print(f"  [RATE LIMIT] chunk {record['chunk_id']}, attempt {attempt+1}/{MAX_RETRIES} — waiting {wait}s...")
            time.sleep(wait)
        except Exception as e:
            print(f"  [ERROR] chunk {record['chunk_id']}: {str(e)[:120]}")
            return ""
    print(f"  [FAILED] chunk {record['chunk_id']} exhausted retries")
    return ""

input_path = os.path.join(os.path.dirname(__file__), "test_output_tagged.jsonl")
output_path = os.path.join(os.path.dirname(__file__), "final_claims_for_audit.jsonl")


with open(input_path, "r") as infile, open(output_path, "w") as outfile:
    for line in infile:
        record = json.loads(line)

        if "claims" not in record:
            print(f"[chunk {record['chunk_id']}] skipped (reference section)")
            continue

        if not record["claims"] and len(record.get("text", "")) < 80:
            print(f"[chunk {record['chunk_id']}] skipped (no claims, minimal text)")
            continue

        print(f"Processing chunk {record['chunk_id']} | {record['section_heading'][:60]}...")

        raw_output = extract_hybrid_claims(record)

        written = 0
        for claim_line in raw_output.split("\n"):
            claim_line = claim_line.strip()
            if not claim_line:
                continue
            try:
                clean = claim_line.replace("```json", "").replace("```", "").strip()
                claim_data = json.loads(clean)

                claim_data["chunk_id"] = record["chunk_id"]
                claim_data["doc_name"] = record["doc_name"]
                claim_data["category"] = record.get("category", "")
                claim_data["section_heading"] = record.get("section_heading", "")
                claim_data["semantic_category"] = record.get("semantic_category", "other")

                outfile.write(json.dumps(claim_data) + "\n")
                written += 1
            except Exception:
                continue

        print(f"  → {written} claims extracted")
        time.sleep(4)

print(f"\nDone. Final claims written to: {output_path}")
