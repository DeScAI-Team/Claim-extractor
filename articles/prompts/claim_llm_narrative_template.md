# Claim narrative template (LLM-facing)

Plain-text rendering of each claim for downstream prompts. Placeholders are filled by `group-and-score/prep.py`.

## Relevancy tiers

`relevancy_score` is clamped to `[0.0, 1.0]`, then bucketed (lower bound inclusive, upper exclusive, except the last bucket includes `1.0`):

| Range | Verbal label |
|-------|----------------|
| 0.0 – 0.2 | low relevancy |
| 0.2 – 0.4 | slightly relevant |
| 0.4 – 0.6 | moderately relevant |
| 0.6 – 0.8 | very relevant |
| 0.8 – 1.0 | extremely relevant |

Missing or non-numeric scores use: **relevancy unknown**.

## Sentence template

{doc_name} presents the claim '{claim}' in the {section_heading} section. This claim was deemed {verdict} as {rationale} This claim is rated as {relevancy_label} for the core of this research.
