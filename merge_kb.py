#!/usr/bin/env python3
"""One-shot: merge the full 44-chunk KB with correct semantic_category tags.

Takes all chunks from articles/data/text_knowledge_base.jsonl (full content)
and applies semantic_category from articles/data/document (10)/pipe-test/text_knowledge_base.jsonl
(which has correct tags but only 26 chunks). Discussion-section chunks (26-43)
are tagged manually based on heading content.
"""

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent
FULL_KB = REPO / "articles" / "data" / "text_knowledge_base.jsonl"
GOOD_KB = REPO / "articles" / "data" / "document (10)" / "pipe-test" / "text_knowledge_base.jsonl"
OUT_KB = REPO / "articles" / "data" / "document (10)" / "pipe-test2" / "text_knowledge_base.jsonl"

HEADING_TO_CATEGORY: dict[str, str] = {}

with open(GOOD_KB, "r", encoding="utf-8") as f:
    for line in f:
        rec = json.loads(line)
        heading = rec.get("section_heading", "")
        cat = rec.get("semantic_category", "other")
        if heading and cat != "other":
            HEADING_TO_CATEGORY[heading] = cat

DISCUSSION_HEADINGS = {
    "Differentially Expressed Genes": "result",
    "Functional Enrichments": "result",
    "CNR-401 Induces Broad Transcriptomic Changes in BMAA-Exposed Zebrafish": "discussion",
    "Enriched Biological Processes and Pathways in CNR-401 Treatment": "discussion",
}
HEADING_TO_CATEGORY.update(DISCUSSION_HEADINGS)

METHOD_HINTS = frozenset([
    "method", "materials", "protocol", "husbandry", "toxicity",
    "sequencing", "replication", "preparation", "experimental",
    "efficacy assessment", "phenotypic analysis", "sample preparation",
    "data processing", "visualization",
])
RESULT_HINTS = frozenset([
    "differentially expressed", "enrichment", "transcriptomic",
    "efficacy", "data quality",
])
ABSTRACT_HINTS = frozenset(["abstract", "key messages"])
INTRO_HINTS = frozenset(["introduction"])


def infer_category(heading: str) -> str:
    h = heading.lower()
    if any(kw in h for kw in ABSTRACT_HINTS):
        return "abstract"
    if any(kw in h for kw in INTRO_HINTS):
        return "introduction"
    if any(kw in h for kw in METHOD_HINTS):
        return "method"
    if any(kw in h for kw in RESULT_HINTS):
        return "result"
    return "other"


def main():
    OUT_KB.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(FULL_KB, "r", encoding="utf-8") as fin, open(OUT_KB, "w", encoding="utf-8") as fout:
        for line in fin:
            rec = json.loads(line)
            heading = rec.get("section_heading", "")
            if heading in HEADING_TO_CATEGORY:
                rec["semantic_category"] = HEADING_TO_CATEGORY[heading]
            else:
                rec["semantic_category"] = infer_category(heading)
            fout.write(json.dumps(rec) + "\n")
            written += 1
    print(f"Wrote {written} chunks to {OUT_KB}")
    cats = {}
    with open(OUT_KB, "r", encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            cat = rec["semantic_category"]
            cats[cat] = cats.get(cat, 0) + 1
    print(f"Category distribution: {dict(sorted(cats.items()))}")


if __name__ == "__main__":
    main()
