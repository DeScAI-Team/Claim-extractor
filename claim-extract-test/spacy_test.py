import re
import spacy
from spacy.matcher import DependencyMatcher


nlp = spacy.load("en_core_web_sm")    #small model, might change to bigger depending on performance
dep_matcher = DependencyMatcher(nlp.vocab)


evidential_verbs = [
    "show", "exhibit", "demonstrate", "lead", "yield", "indicate",
    "prove", "suggest", "correlate", "decrease", "increase", "confirm",
    "observe", "result", "achieve", "reach",
    "find", "report", "reveal", "identify", "detect", "measure",
    "propose", "hypothesize", "conclude", "establish", "determine",
    "support", "validate", "verify", "imply", "predict", "suggest",
    "present", "describe", "characterize", "analyze", "compare",
    "highlight", "note", "state", "argue", "speculate"
]

general_claim_pattern = [
    {
        "RIGHT_ID": "action_verb",
        "RIGHT_ATTRS": {"POS": "VERB", "LEMMA": {"IN": evidential_verbs}}
    },
    {
        "LEFT_ID": "action_verb",
        "REL_OP": ">",
        "RIGHT_ID": "subject_entity",
        "RIGHT_ATTRS": {"DEP": {"IN": ["nsubj", "nsubjpass"]}, "POS": {"IN": ["NOUN", "PROPN", "PRON"]}}
    },
    {
        "LEFT_ID": "action_verb",
        "REL_OP": ">",
        "RIGHT_ID": "result_attribute",
        "RIGHT_ATTRS": {"DEP": {"IN": ["dobj", "attr", "acomp", "prep", "ccomp", "xcomp", "relcl", "advcl"]}}
    }
]

dep_matcher.add("GENERAL_CLAIM", [general_claim_pattern])

def pre_tag_chunk(chunk_text):
    """
    Identifies grammatical claims and wraps them in hints for the LLM.
    Uses char offsets to avoid str.replace() mismatches on whitespace variants.
    """
    doc = nlp(chunk_text)
    matches = dep_matcher(doc)

    matched_spans = {}  # start_char -> end_char
    for match_id, token_ids in matches:
        root_token_index = token_ids[0]
        sent = doc[root_token_index].sent
        matched_spans[sent.start_char] = sent.end_char

    if not matched_spans:
        return chunk_text

    result = []
    prev = 0
    for start in sorted(matched_spans):
        end = matched_spans[start]
        result.append(chunk_text[prev:start])
        result.append(f"<Scientific_claim>{chunk_text[start:end]}</Scientific_claim>")
        prev = end
    result.append(chunk_text[prev:])

    return "".join(result)

import json
import os

input_path = os.path.join(os.path.dirname(__file__), "text_knowledge_base.jsonl")
output_path = os.path.join(os.path.dirname(__file__), "test_output_tagged.jsonl")

with open(input_path, "r") as infile, open(output_path, "w") as outfile:
    for line in infile:
        record = json.loads(line)
        heading = record.get("section_heading", "")
        semantic_category = str(record.get("semantic_category", "")).strip().lower()
        # Skip tagging for references, using semantic category first.
        if semantic_category == "reference" or "reference" in heading.lower():
            outfile.write(json.dumps(record) + "\n")
            print(f"[chunk {record['chunk_id']}] (skipped - references section)")
            continue
        record["text"] = pre_tag_chunk(record["text"])
        record["claims"] = re.findall(r"<Scientific_claim>(.*?)</Scientific_claim>", record["text"], re.DOTALL)
        outfile.write(json.dumps(record) + "\n")
        print(f"[chunk {record['chunk_id']}] {record['text'][:120]}...")

print(f"\nDone. Tagged output written to: {output_path}")