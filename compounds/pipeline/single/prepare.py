#!/usr/bin/env python3
"""Prepare local ``discover.py`` JSON reports into review or agent-oriented artifacts (offline, stdlib-only).

Canonical documentation: ``REVIEW_LOGIC.md`` (same directory). Quick usage: ``README.md``.
"""
from __future__ import annotations

import argparse
import glob as glob_lib
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent.parent / "data"

# SPL fields copied into ``risks.drug_labels`` (subset of discover LABELS).
RISK_SPL_FIELDS = (
    "adverse_reactions",
    "adverse_reactions_table",
    "boxed_warning",
    "boxed_warning_table",
    "contraindications",
    "drug_interactions",
)
# SPL fields surfaced as mechanistic context in research.label_mechanism_hypotheses.
MECHANISM_SPL_FIELDS = ("mechanism_of_action", "clinical_pharmacology", "pharmacokinetics")

# FAERS reaction-term list cap in risks.faers_summary (review format).
FAERS_TERM_PREVIEW_MAX = 80

# --- Agent-context digest caps (balance context size vs. recall) ---
LITERATURE_DIGEST_MAX = 35
LITERATURE_ABSTRACT_MAX = 850
TRIALS_DIGEST_MAX = 45
TRIAL_TITLE_MAX = 220
MECHANISM_BLOCK_MAX = 1800
RISK_BOXED_MAX = 1400
RISK_CONTRA_MAX = 1200
RISK_INTERACTION_MAX = 1200
RISK_ADVERSE_EXCERPT_MAX = 2800
FAERS_DIGEST_TERMS = 35

# Substrings used only for literature digest ranking (_article_score); discover already
# filtered queries (longevity / aging / lifespan).
LONGEVITY_TERMS = (
    "longevity",
    "lifespan",
    "aging",
    "senescence",
    "healthspan",
    "anti-aging",
    "antiaging",
    "mitochondrial",
    "mtor",
    "autophagy",
    "daf-16",
    "daf-2",
    "c. elegans",
    "caenorhabditis",
    "health span",
    "geroscience",
    "rapamycin",
    "calorie restriction",
)

STATIC_DISCLAIMERS = (
    "OpenFDA FAERS (adverse_events) reaction terms are spontaneous reports; they do not represent incidence, prevalence, or controlled trial rates.",
    "Prepared artifacts are for research screening only and are not medical advice.",
)

AGENT_CTX = "agent_context"


def _provenance(
    provider: str,
    *,
    primary_id: str | None = None,
    id_type: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Stable citation bundle for downstream LLM passes (not a legal citation format)."""
    d: dict[str, Any] = {"provider": provider}
    if primary_id is not None:
        d["primary_id"] = primary_id
    if id_type is not None:
        d["id_type"] = id_type
    if note:
        d["note"] = note
    return d


def _excerpt_was_truncated(original: str, excerpt: str) -> bool:
    """True if ``excerpt`` is shorter than ``original`` after strip (ellipsis truncation)."""
    if not original or not isinstance(original, str):
        return False
    o = original.strip()
    if not o:
        return False
    return bool(excerpt.endswith("…") or len(excerpt) < len(o))


def _as_dict(x: Any) -> dict[str, Any] | None:
    return x if isinstance(x, dict) else None


def _failure_steps(meta: dict[str, Any] | None) -> list[str]:
    """Extract discover ``metadata.failures[].step`` strings for coverage hints."""
    if not meta:
        return []
    fails = meta.get("failures")
    if not isinstance(fails, list):
        return []
    out: list[str] = []
    for it in fails:
        if isinstance(it, dict):
            s = it.get("step")
            if isinstance(s, str) and s.strip():
                out.append(s.strip())
    return out


def _failure_reason_hint(steps: list[str], prefixes: tuple[str, ...]) -> str | None:
    """Return a short line listing failure steps matching subsystem prefixes (europe_pmc, kegg, …)."""
    hit = [s for s in steps if any(s.startswith(p) or p in s for p in prefixes)]
    if not hit:
        return None
    return "failure steps: " + "; ".join(sorted(set(hit))[:12])


def _strip_label_for_risks(row: dict[str, Any]) -> dict[str, Any]:
    """Keep only SPL keys used for safety/risk narration (warnings, interactions, …)."""
    out: dict[str, Any] = {}
    for k in RISK_SPL_FIELDS:
        v = row.get(k)
        if v is not None:
            out[k] = v
    return out


def _strip_labels_for_risks(labels: list[Any]) -> list[dict[str, Any]]:
    """Map each SPL row to a dict containing only ``RISK_SPL_FIELDS`` where non-null."""
    out: list[dict[str, Any]] = []
    for row in labels:
        if isinstance(row, dict):
            slim = _strip_label_for_risks(row)
            if slim:
                out.append(slim)
    return out


def _label_mechanism_hypotheses(labels: list[Any] | None) -> list[dict[str, Any]]:
    """One entry per SPL row index with MoA / clinical pharmacology / pharmacokinetics when present."""
    if not labels:
        return []
    hy: list[dict[str, Any]] = []
    for i, row in enumerate(labels):
        if not isinstance(row, dict):
            continue
        chunk: dict[str, Any] = {"spl_index": i}
        empty = True
        for k in MECHANISM_SPL_FIELDS:
            v = row.get(k)
            if v is not None:
                chunk[k] = v
                empty = False
        if not empty:
            hy.append(chunk)
    return hy


def _faers_summary(ae: Any, prep_warnings: list[str]) -> dict[str, Any] | None:
    """Build ``risks.faers_summary``: counts, capped term preview, interpretation note. Returns ``None`` if AE missing."""
    if ae is None:
        return None
    if not isinstance(ae, dict):
        prep_warnings.append("openfda.adverse_events was not an object; omitted faers_summary.")
        return None
    terms = ae.get("reaction_terms")
    term_list: list[str] = []
    if isinstance(terms, list):
        term_list = [t for t in terms if isinstance(t, str)]
    elif terms is not None:
        prep_warnings.append("openfda.adverse_events.reaction_terms was not a list; treated as empty.")
    rc = ae.get("report_count")
    report_count = int(rc) if isinstance(rc, int) else None
    if report_count is None and rc is not None:
        try:
            report_count = int(rc)
        except (TypeError, ValueError):
            prep_warnings.append("openfda.adverse_events.report_count missing or non-numeric.")
    truncated = len(term_list) > FAERS_TERM_PREVIEW_MAX
    preview = term_list[:FAERS_TERM_PREVIEW_MAX]
    summary: dict[str, Any] = {
        "report_count": report_count,
        "reaction_term_count": len(term_list),
        "reaction_terms_preview": preview,
        "reaction_terms_truncated": truncated,
        "notes": (
            "FAERS captures voluntary reports; listing does not imply causation, frequency in the general population, "
            "or comparison to placebo or active controls."
        ),
    }
    return summary


def _coverage(raw: dict[str, Any], steps: list[str]) -> dict[str, Any]:
    """Derived per-section ``present`` flags so null/failed pulls are not read as empty science."""
    def cov(present: bool, missing_reason: str | None, note: str | None = None) -> dict[str, Any]:
        d: dict[str, Any] = {"present": present, "reason": None if present else missing_reason}
        if note is not None:
            d["note"] = note
        return d

    epmc = raw.get("europe_pmc")
    ct = raw.get("clinical_trials")
    kg = raw.get("kegg")
    of = _as_dict(raw.get("openfda"))

    e_reason = None
    if epmc is None:
        e_reason = _failure_reason_hint(steps, ("europe_pmc",)) or "null in source (section missing or not retrieved)"

    ct_reason = None
    if ct is None:
        ct_reason = _failure_reason_hint(steps, ("clinical_trials",)) or "null in source (section missing or not retrieved)"

    kg_reason = None
    if kg is None:
        kg_reason = _failure_reason_hint(steps, ("kegg",)) or "null in source (section missing or not retrieved)"

    labels_present = bool(of and of.get("drug_labels") is not None)
    dl_reason = None
    dl_note: str | None = None
    if not labels_present:
        if of is None:
            dl_reason = _failure_reason_hint(steps, ("openfda_label",)) or "openfda missing or drug_labels null (label pull failed or absent)"
        else:
            dl_reason = _failure_reason_hint(steps, ("openfda_label",)) or "drug_labels null (label pull failed)"
    elif not of.get("drug_labels"):
        # Empty list: API call succeeded but no labels survived the generic_name filter.
        meta_block = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        dropped = meta_block.get("label_filter_dropped", 0)
        if dropped:
            dl_note = (
                f"API returned {dropped} label result(s) but none contained the compound name "
                f"in openfda.generic_name; drug_labels is empty after filter"
            )
        else:
            dl_note = "no FDA labels found for this compound (drug_labels is empty)"

    ae = of.get("adverse_events") if of else None
    faers_present = ae is not None
    faers_reason = None
    if not faers_present:
        faers_reason = _failure_reason_hint(steps, ("openfda_event",)) or "adverse_events null (FAERS pull failed or absent)"

    return {
        "europe_pmc": cov(epmc is not None, e_reason),
        "clinical_trials": cov(ct is not None, ct_reason),
        "kegg": cov(kg is not None, kg_reason),
        "openfda_labels": cov(labels_present, dl_reason, dl_note),
        "faers": cov(faers_present, faers_reason),
    }


def _collect_disclaimers(meta_block: dict[str, Any] | None) -> list[str]:
    """Static disclaimers plus OpenFDA API disclaimer text when embedded in ``api_versions``."""
    out: list[str] = list(STATIC_DISCLAIMERS)
    if not meta_block:
        return out
    av = meta_block.get("api_versions")
    if isinstance(av, dict):
        om = av.get("openfda")
        if isinstance(om, dict):
            d = om.get("disclaimer")
            if isinstance(d, str) and d.strip() and d.strip() not in out:
                out.append(d.strip())
    return out


def _strip_html_light(text: str | None) -> str:
    """Remove simple HTML tags from PMC abstracts for excerpt display."""
    if not text or not isinstance(text, str):
        return ""
    t = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", t).strip()


def _truncate(text: str | None, max_len: int) -> str:
    """Unicode-safe trim with ellipsis when shortened."""
    if not text or not isinstance(text, str):
        return ""
    t = text.strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1].rstrip() + "…"


def _article_score(hit: dict[str, Any]) -> int:
    """Heuristic literature ranking: capped citation count + ``LONGEVITY_TERMS`` bonuses.

    Stored as ``relevance_score`` on digest rows only; not an external bibliometric score.
    """
    cite = hit.get("citedByCount")
    try:
        sc = int(cite) if cite is not None else 0
    except (TypeError, ValueError):
        try:
            sc = int(float(str(cite)))
        except (TypeError, ValueError):
            sc = 0
    sc = min(max(sc, 0), 400)
    title = (hit.get("title") or "").lower()
    abst = ((hit.get("abstract") or hit.get("abstractText")) or "").lower()
    combined = title + " " + abst
    bonus = 0
    for term in LONGEVITY_TERMS:
        if term in title:
            bonus += 18
        elif term in combined:
            bonus += 6
    return sc + bonus


def _flatten_spl_strings(val: Any) -> list[str]:
    """Normalize SPL fields that may be a string or list of strings."""
    if val is None:
        return []
    if isinstance(val, str):
        return [val] if val.strip() else []
    if isinstance(val, list):
        out: list[str] = []
        for x in val:
            if isinstance(x, str) and x.strip():
                out.append(x)
        return out
    return []


def _unique_truncated_strings(blobs: list[str], max_items: int, each_max: int) -> tuple[list[str], int]:
    """Deduplicate SPL blobs by SHA-256(full text); truncate each excerpt. Returns (excerpts, raw_chunk_count)."""
    seen: set[str] = set()
    out: list[str] = []
    for b in blobs:
        h = hashlib.sha256(b.encode("utf-8", errors="replace")).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        out.append(_truncate(b, each_max))
        if len(out) >= max_items:
            break
    return out, len(blobs)


def _digest_literature(epmc: Any, preparation_warnings: list[str]) -> dict[str, Any]:
    """Top ``LITERATURE_DIGEST_MAX`` articles by ``_article_score``, with truncated abstracts."""
    if not isinstance(epmc, dict):
        return {"total_articles_in_report": 0, "digest_rows": [], "note": "europe_pmc missing"}
    arts = epmc.get("articles")
    if not isinstance(arts, list):
        preparation_warnings.append("europe_pmc.articles was not a list.")
        return {"total_articles_in_report": 0, "digest_rows": [], "note": "no articles list"}
    ranked: list[tuple[int, dict[str, Any]]] = []
    for h in arts:
        if isinstance(h, dict):
            ranked.append((_article_score(h), h))
    ranked.sort(key=lambda x: x[0], reverse=True)
    rows: list[dict[str, Any]] = []
    for rank, (sc, hit) in enumerate(ranked[:LITERATURE_DIGEST_MAX], start=1):
        abst_raw = hit.get("abstract") or hit.get("abstractText") or ""
        stripped = _strip_html_light(abst_raw) if abst_raw else ""
        excerpt_out = _truncate(stripped, LITERATURE_ABSTRACT_MAX)
        doi_c = hit.get("doi")
        doi_s = str(doi_c).strip() if doi_c not in (None, "") else None
        pmid_c = hit.get("pmid")
        pmid_s = str(pmid_c).strip() if pmid_c not in (None, "") else None
        pid = doi_s or pmid_s
        id_typ = "doi" if doi_s else ("pmid" if pmid_s else None)
        rows.append(
            {
                "unit_id": f"epmc_r{rank:03d}",
                "digest_rank": rank,
                "relevance_score": sc,
                "title": _truncate(hit.get("title"), 320),
                "year": hit.get("year"),
                "doi": hit.get("doi"),
                "pmid": hit.get("pmid"),
                "journal": hit.get("journal") or hit.get("journalTitle"),
                "cited_by_count": hit.get("citedByCount"),
                "source_queries": hit.get("source_queries"),
                "abstract_excerpt": excerpt_out,
                "abstract_excerpt_truncated": _excerpt_was_truncated(stripped, excerpt_out),
                "provenance": _provenance("Europe PMC", primary_id=pid, id_type=id_typ),
                "json_path": f"{AGENT_CTX}.literature.digest_rows[{rank - 1}]",
                "cite_as": "Europe PMC → verify full text via DOI or PMID where present.",
            }
        )
    return {
        "total_articles_in_report": len(arts),
        "digest_rows_included": len(rows),
        "digest_rows": rows,
        "selection_note": "Sorted by simple longevity-keyword score + citation count; not a systematic review.",
    }


def _digest_trials(ct: Any, preparation_warnings: list[str]) -> dict[str, Any]:
    """Slim trial rows for agent context; prefers ``has_results`` then sorted by NCT id."""
    if not isinstance(ct, dict):
        return {"study_count": 0, "digest_rows": [], "note": "clinical_trials missing"}
    studies = ct.get("studies")
    if not isinstance(studies, list):
        preparation_warnings.append("clinical_trials.studies was not a list.")
        return {"study_count": 0, "digest_rows": [], "note": "no studies list"}

    def sort_key(s: dict[str, Any]) -> tuple[int, str]:
        hr = s.get("has_results")
        ig = 0 if hr is True else 1
        return (ig, str(s.get("nct_id") or ""))

    slim = [s for s in studies if isinstance(s, dict)]
    slim.sort(key=sort_key)
    rows: list[dict[str, Any]] = []
    for ti, s in enumerate(slim[:TRIALS_DIGEST_MAX]):
        po = s.get("primary_outcomes")
        po_txt = ""
        if isinstance(po, list) and po:
            first = po[0]
            if isinstance(first, dict):
                po_txt = str(first.get("measure") or first.get("description") or "")[:400]
        cond = s.get("conditions")
        cond_out: Any = cond
        if isinstance(cond, list):
            cond_out = cond[:10]
        nct = s.get("nct_id")
        nct_s = str(nct).strip() if nct not in (None, "") else ""
        uid = f"ct_{nct_s}" if nct_s else f"ct_row_{ti:03d}"
        po_ex = _truncate(po_txt, 450) if po_txt else None
        rows.append(
            {
                "unit_id": uid,
                "nct_id": s.get("nct_id"),
                "brief_title": _truncate(s.get("brief_title"), TRIAL_TITLE_MAX),
                "phases": s.get("phases"),
                "overall_status": s.get("overall_status"),
                "has_results": s.get("has_results"),
                "conditions_sample": cond_out,
                "primary_outcome_excerpt": po_ex,
                "primary_outcome_truncated": _excerpt_was_truncated(po_txt, po_ex) if po_ex and po_txt else False,
                "provenance": _provenance("ClinicalTrials.gov", primary_id=nct_s or None, id_type="nct_id" if nct_s else None),
                "json_path": f"{AGENT_CTX}.clinical_trials.digest_rows[{ti}]",
                "cite_as": "ClinicalTrials.gov registry entry (design may differ from results).",
            }
        )
    sc = ct.get("study_count")
    count = int(sc) if isinstance(sc, int) else len(slim)
    return {
        "study_count": count,
        "version_holder": ct.get("version_holder"),
        "digest_rows_included": len(rows),
        "digest_rows": rows,
    }


def _digest_kegg(kg: Any) -> dict[str, Any]:
    """Compact pathway names, preview objects, and longevity keyword flags from discover's KEGG block."""
    if not isinstance(kg, dict):
        return {"present": False, "note": "kegg section null"}
    names = kg.get("pathway_names")
    name_sample = names[:35] if isinstance(names, list) else []
    pws = kg.get("pathways")
    pw_preview: list[dict[str, Any]] = []
    if isinstance(pws, list):
        for pi, p in enumerate(pws[:15]):
            if isinstance(p, dict):
                pid = p.get("pathway_id")
                pid_s = str(pid).strip() if pid not in (None, "") else ""
                dsc = p.get("description_snippet")
                dsc_s = str(dsc) if dsc is not None else ""
                dex = _truncate(dsc, 500)
                pw_preview.append(
                    {
                        "unit_id": f"kegg_pw_{pid_s}" if pid_s else f"kegg_pw_idx_{pi}",
                        "pathway_id": p.get("pathway_id"),
                        "name": p.get("name"),
                        "description_excerpt": dex,
                        "description_excerpt_truncated": _excerpt_was_truncated(dsc_s, dex) if dsc_s else False,
                        "provenance": _provenance("KEGG", primary_id=pid_s or None, id_type="pathway_id" if pid_s else None),
                        "json_path": f"{AGENT_CTX}.kegg.pathways_preview[{pi}]",
                    }
                )
    flags = kg.get("longevity_pathway_flags")
    return {
        "present": True,
        "unit_id": "kegg_summary",
        "provenance": _provenance("KEGG", note="pathway linkage + keyword flags from discover"),
        "json_path": f"{AGENT_CTX}.kegg",
        "kegg_drug_ids": kg.get("kegg_drug_ids"),
        "pathway_count": kg.get("pathway_count"),
        "pathway_get_limit": kg.get("pathway_get_limit"),
        "truncated": kg.get("truncated"),
        "longevity_pathway_flags": flags if isinstance(flags, dict) else None,
        "pathway_names_sample": name_sample,
        "pathways_preview": pw_preview,
        "note": "Flags mark keyword hints in fetched pathway text, not experimental confirmation in humans.",
    }


def _mechanism_excerpt(hy_list: list[dict[str, Any]], preparation_warnings: list[str]) -> dict[str, Any]:
    """Bounded concatenation of mechanism hypotheses for ``agent_context`` (token cap)."""
    blocks: list[str] = []
    budget = MECHANISM_BLOCK_MAX
    for h in hy_list:
        idx = h.get("spl_index")
        parts: list[str] = []
        for k in MECHANISM_SPL_FIELDS:
            v = h.get(k)
            if v is None:
                continue
            blob = " ".join(_flatten_spl_strings(v))[: budget]
            if blob:
                parts.append(f"{k}: {_truncate(blob, min(budget, MECHANISM_BLOCK_MAX))}")
        if parts:
            chunk = f"[SPL index {idx}] " + " | ".join(parts)
            blocks.append(chunk[:MECHANISM_BLOCK_MAX])
            budget -= len(blocks[-1])
            if budget < 200:
                preparation_warnings.append("Mechanism excerpt truncated heavily for size cap.")
                break
    text = "\n\n".join(blocks)
    comb = _truncate(text, MECHANISM_BLOCK_MAX * 3)
    return {
        "unit_id": "spl_mechanism_pharmacology",
        "provenance": _provenance("OpenFDA drug labels (SPL)", id_type="spl_field", note="mechanism_of_action, clinical_pharmacology, pharmacokinetics"),
        "json_path": f"{AGENT_CTX}.mechanism_hypotheses_excerpt",
        "spl_hypothesis_blocks": blocks,
        "combined_excerpt": comb,
        "combined_excerpt_truncated": _excerpt_was_truncated(text, comb),
    }


def _risks_overview(labels: list[Any] | None, ae: Any, preparation_warnings: list[str]) -> dict[str, Any]:
    """Deduped label risk excerpts plus FAERS headline sample for compact agent-facing risks."""
    boxed: list[str] = []
    contra: list[str] = []
    inter: list[str] = []
    adverse: list[str] = []
    if isinstance(labels, list):
        for row in labels:
            if not isinstance(row, dict):
                continue
            boxed.extend(_flatten_spl_strings(row.get("boxed_warning")))
            boxed.extend(_flatten_spl_strings(row.get("boxed_warning_table")))
            contra.extend(_flatten_spl_strings(row.get("contraindications")))
            inter.extend(_flatten_spl_strings(row.get("drug_interactions")))
            adverse.extend(_flatten_spl_strings(row.get("adverse_reactions")))
            adverse.extend(_flatten_spl_strings(row.get("adverse_reactions_table")))

    ub, nb = _unique_truncated_strings(boxed, 4, RISK_BOXED_MAX)
    uc, nc = _unique_truncated_strings(contra, 4, RISK_CONTRA_MAX)
    ui, ni = _unique_truncated_strings(inter, 4, RISK_INTERACTION_MAX)
    ua, na = _unique_truncated_strings(adverse, 3, RISK_ADVERSE_EXCERPT_MAX)

    faers_part: dict[str, Any] = {}
    if isinstance(ae, dict):
        terms = ae.get("reaction_terms")
        tl = [t for t in terms if isinstance(t, str)] if isinstance(terms, list) else []
        rc = ae.get("report_count")
        rc_int: int | None
        if isinstance(rc, int):
            rc_int = rc
        elif rc is not None:
            try:
                rc_int = int(rc)
            except (TypeError, ValueError):
                rc_int = None
                preparation_warnings.append("FAERS report_count not parsed in risks_overview.")
        else:
            rc_int = None
        faers_part = {
            "unit_id": "faers_summary",
            "provenance": _provenance("OpenFDA FAERS"),
            "json_path": f"{AGENT_CTX}.risks_overview.faers",
            "report_count": rc_int,
            "reaction_term_count": len(tl),
            "reaction_terms_sample": tl[:FAERS_DIGEST_TERMS],
            "terms_truncated": len(tl) > FAERS_DIGEST_TERMS,
            "interpretation": "Voluntary reports to FAERS; not incidence or controlled safety rates.",
        }
    else:
        faers_part = {
            "unit_id": "faers_summary",
            "note": "FAERS summary unavailable (missing adverse_events).",
            "provenance": _provenance("OpenFDA FAERS"),
            "json_path": f"{AGENT_CTX}.risks_overview.faers",
        }

    risk_items: list[dict[str, Any]] = []
    for i, ex in enumerate(ub):
        risk_items.append(
            {
                "unit_id": f"spl_boxed_{i}",
                "unit_type": "spl_boxed_warning",
                "excerpt": ex,
                "excerpt_truncated": ex.endswith("…"),
                "provenance": _provenance("OpenFDA SPL", note="deduped boxed warning excerpt"),
                "json_path": f"{AGENT_CTX}.risks_overview.boxed_warnings_unique_excerpts[{i}]",
            }
        )
    for i, ex in enumerate(uc):
        risk_items.append(
            {
                "unit_id": f"spl_contra_{i}",
                "unit_type": "spl_contraindication",
                "excerpt": ex,
                "excerpt_truncated": ex.endswith("…"),
                "provenance": _provenance("OpenFDA SPL", note="deduped contraindications excerpt"),
                "json_path": f"{AGENT_CTX}.risks_overview.contraindications_unique_excerpts[{i}]",
            }
        )
    for i, ex in enumerate(ui):
        risk_items.append(
            {
                "unit_id": f"spl_interaction_{i}",
                "unit_type": "spl_drug_interaction",
                "excerpt": ex,
                "excerpt_truncated": ex.endswith("…"),
                "provenance": _provenance("OpenFDA SPL", note="deduped drug interactions excerpt"),
                "json_path": f"{AGENT_CTX}.risks_overview.drug_interactions_unique_excerpts[{i}]",
            }
        )
    for i, ex in enumerate(ua):
        risk_items.append(
            {
                "unit_id": f"spl_adr_{i}",
                "unit_type": "spl_adverse_reaction",
                "excerpt": ex,
                "excerpt_truncated": ex.endswith("…"),
                "provenance": _provenance("OpenFDA SPL", note="deduped adverse reactions excerpt"),
                "json_path": f"{AGENT_CTX}.risks_overview.adverse_reactions_unique_excerpts[{i}]",
            }
        )
    risk_items.append(
        {
            "unit_id": faers_part.get("unit_id", "faers_summary"),
            "unit_type": "faers_headline",
            "json_path": faers_part.get("json_path"),
            "provenance": faers_part.get("provenance"),
            "payload": faers_part,
        }
    )

    return {
        "boxed_warnings_unique_excerpts": ub,
        "boxed_warning_raw_chunks_seen": nb,
        "contraindications_unique_excerpts": uc,
        "contraindication_raw_chunks_seen": nc,
        "drug_interactions_unique_excerpts": ui,
        "drug_interaction_raw_chunks_seen": ni,
        "adverse_reactions_unique_excerpts": ua,
        "adverse_reaction_raw_chunks_seen": na,
        "faers": faers_part,
        "risk_items": risk_items,
        "note": "Label excerpts come from OpenFDA SPL snapshots; multiple products may repeat similar text.",
    }


def _build_evaluation_units(
    lit: dict[str, Any],
    ct_d: dict[str, Any],
    kg_d: dict[str, Any],
    mech: dict[str, Any],
    risks: dict[str, Any],
) -> list[dict[str, Any]]:
    """Flatten atomic units for per-item LLM workflows (iterate ``payload`` with stable ``unit_id``)."""
    out: list[dict[str, Any]] = []
    for row in lit.get("digest_rows") or []:
        if isinstance(row, dict) and row.get("unit_id"):
            out.append(
                {
                    "unit_id": row["unit_id"],
                    "unit_type": "literature",
                    "json_path": row.get("json_path"),
                    "provenance": row.get("provenance"),
                    "payload": row,
                }
            )
    for row in ct_d.get("digest_rows") or []:
        if isinstance(row, dict) and row.get("unit_id"):
            out.append(
                {
                    "unit_id": row["unit_id"],
                    "unit_type": "clinical_trial",
                    "json_path": row.get("json_path"),
                    "provenance": row.get("provenance"),
                    "payload": row,
                }
            )
    if kg_d.get("present"):
        out.append(
            {
                "unit_id": kg_d.get("unit_id", "kegg_summary"),
                "unit_type": "kegg_summary",
                "json_path": kg_d.get("json_path"),
                "provenance": kg_d.get("provenance"),
                "payload": kg_d,
            }
        )
        for pw in kg_d.get("pathways_preview") or []:
            if isinstance(pw, dict) and pw.get("unit_id"):
                out.append(
                    {
                        "unit_id": pw["unit_id"],
                        "unit_type": "kegg_pathway",
                        "json_path": pw.get("json_path"),
                        "provenance": pw.get("provenance"),
                        "payload": pw,
                    }
                )
    if mech.get("unit_id"):
        out.append(
            {
                "unit_id": mech["unit_id"],
                "unit_type": "spl_mechanism_pharmacology",
                "json_path": mech.get("json_path"),
                "provenance": mech.get("provenance"),
                "payload": mech,
            }
        )
    for ri in risks.get("risk_items") or []:
        if not isinstance(ri, dict):
            continue
        uid = ri.get("unit_id")
        if not uid:
            continue
        payload = ri.get("payload") if isinstance(ri.get("payload"), dict) else ri
        out.append(
            {
                "unit_id": uid,
                "unit_type": ri.get("unit_type"),
                "json_path": ri.get("json_path"),
                "provenance": ri.get("provenance"),
                "payload": payload,
            }
        )
    return out


def build_agent_context(
    raw: dict[str, Any],
    compound: str | None,
    preparation_warnings: list[str],
    mechanism_hypotheses: list[dict[str, Any]],
    labels_for_risk: list[Any] | None,
    ae_raw: Any,
) -> dict[str, Any]:
    name = compound or "(unknown compound)"
    task_question = (
        f"Is there reasonable scientific justification for further testing on {name} in longevity research, and why? "
        f"What are the risks? Where did you find this information?"
    )
    model_instructions = [
        "Answer justification using agent_context.literature (peer hooks), clinical_trials (human registry context), "
        "kegg (pathway hypotheses), and mechanism_hypotheses_excerpt (label PK/MoA text). Clearly separate "
        "preclinical/model-organism findings from human evidence.",
        "Answer risks using risks_overview (label warnings + FAERS headline sample). Never treat FAERS term lists as incidence.",
        "For provenance, cite rows by DOI, NCT ID, SPL-derived excerpts in this bundle, and name the provider "
        "(Europe PMC, ClinicalTrials.gov, KEGG, OpenFDA).",
        "If metadata.coverage.*.present is false, report missing data instead of implying no biological effect.",
        "For per-item LLM passes, iterate ``evaluation_units``: each element has ``unit_id``, ``unit_type``, ``json_path``, "
        "``provenance``, and ``payload`` (duplicate of the corresponding digest row or excerpt for convenience).",
    ]
    sources_index = [
        {
            "id": "epmc",
            "provider": "Europe PMC (deduped keyword search in discover)",
            "use_for": "Published literature potentially related to compound + longevity/aging terms",
            "paths": ["agent_context.literature", "research.europe_pmc (full)"],
        },
        {
            "id": "ctgov",
            "provider": "ClinicalTrials.gov API v2 (slim fields)",
            "use_for": "Registered interventional/observational studies mentioning the compound",
            "paths": ["agent_context.clinical_trials", "research.clinical_trials (full)"],
        },
        {
            "id": "kegg",
            "provider": "KEGG REST",
            "use_for": "Drug–pathway linkage and keyword flags over pathway names/snippets",
            "paths": ["agent_context.kegg", "research.kegg (full)"],
        },
        {
            "id": "spl",
            "provider": "OpenFDA drug product labels (SPL fields)",
            "use_for": "Regulatory warnings, interactions, PK/MoA text (not longevity-specific)",
            "paths": ["agent_context.risks_overview", "risks.drug_labels (full)"],
        },
        {
            "id": "faers",
            "provider": "OpenFDA FAERS adverse event open data",
            "use_for": "Spontaneous post-marketing reports (biased, under-reported)",
            "paths": ["agent_context.risks_overview.faers", "risks.faers_summary"],
        },
    ]

    lit = _digest_literature(raw.get("europe_pmc"), preparation_warnings)
    ct_d = _digest_trials(raw.get("clinical_trials"), preparation_warnings)
    kg_d = _digest_kegg(raw.get("kegg"))
    mech = _mechanism_excerpt(mechanism_hypotheses, preparation_warnings)
    risks = _risks_overview(labels_for_risk, ae_raw, preparation_warnings)
    eval_units = _build_evaluation_units(lit, ct_d, kg_d, mech, risks)

    return {
        "task_question": task_question,
        "model_instructions": model_instructions,
        "sources_index": sources_index,
        "evaluation_units": eval_units,
        "evaluation_units_note": "Ordered flat list of atomic sources; payload mirrors nested sections for batch LLM iteration.",
        "literature": lit,
        "clinical_trials": ct_d,
        "kegg": kg_d,
        "mechanism_hypotheses_excerpt": mech,
        "risks_overview": risks,
    }


def prepare_report(
    raw: dict[str, Any], preparation_warnings: list[str], output_format: str = "review"
) -> dict[str, Any]:
    """Map a discover report dict to the prepared review schema."""
    fmt = output_format if output_format in ("review", "agent") else "review"
    meta_in = _as_dict(raw.get("metadata"))
    steps = _failure_steps(meta_in)

    compound = raw.get("compound_name")
    if compound is not None and not isinstance(compound, str):
        preparation_warnings.append("compound_name was not a string; coerced in output.")
        compound = str(compound)

    research = {
        "europe_pmc": raw.get("europe_pmc"),
        "clinical_trials": raw.get("clinical_trials"),
        "kegg": raw.get("kegg"),
        "label_mechanism_hypotheses": [],
    }

    of = _as_dict(raw.get("openfda"))
    labels = of.get("drug_labels") if of else None
    mechanism_hypotheses: list[dict[str, Any]] = []
    if isinstance(labels, list):
        mechanism_hypotheses = _label_mechanism_hypotheses(labels)
        research["label_mechanism_hypotheses"] = mechanism_hypotheses
    elif labels is not None:
        preparation_warnings.append("openfda.drug_labels was not a list; skipped mechanism hypotheses.")

    risks: dict[str, Any] = {"drug_labels": [], "faers_summary": None}
    if isinstance(labels, list):
        risks["drug_labels"] = _strip_labels_for_risks(labels)
    ae = of.get("adverse_events") if of else None
    risks["faers_summary"] = _faers_summary(ae, preparation_warnings)

    meta_out: dict[str, Any] = {}
    if meta_in:
        for k in ("timestamp", "api_versions", "failures", "label_filter_dropped"):
            if k in meta_in:
                meta_out[k] = meta_in[k]
    meta_out["coverage"] = _coverage(raw, steps)

    agent_ctx = build_agent_context(
        raw, compound, preparation_warnings, mechanism_hypotheses, labels, ae
    )

    disclaimers = _collect_disclaimers(meta_in)
    pw = list(preparation_warnings)

    if fmt == "agent":
        return {
            "prepare_output_format": "agent",
            "compound_name": compound,
            "agent_context": agent_ctx,
            "metadata": meta_out,
            "disclaimers": disclaimers,
            "preparation_warnings": pw,
            "note": "Compact agent bundle: full research/risks blobs omitted; regenerate with --format review if needed.",
        }

    return {
        "prepare_output_format": "review",
        "compound_name": compound,
        "agent_context": agent_ctx,
        "research": research,
        "risks": risks,
        "metadata": meta_out,
        "disclaimers": disclaimers,
        "preparation_warnings": pw,
    }


def resolve_inputs(positional: list[str], root: Path) -> list[Path]:
    if not positional:
        return sorted(root.glob("*/report_*.json"))
    paths: list[Path] = []
    for item in positional:
        if glob_lib.has_magic(item):
            p = Path(item)
            if p.is_absolute():
                matches = sorted(p.parent.glob(p.name))
            else:
                matches = sorted(root.glob(item))
                if not matches:
                    matches = sorted(Path.cwd().glob(item))
            paths.extend(matches)
        else:
            paths.append(Path(item))
    # unique, stable
    seen: set[str] = set()
    uniq: list[Path] = []
    for path in paths:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key not in seen:
            seen.add(key)
            uniq.append(path)
    return uniq


def prepared_output_path(report_path: Path, output_root: Path | None, output_format: str = "review") -> Path:
    stem = report_path.stem
    suffix = "_agent" if output_format == "agent" else ""
    name = f"prepared_{stem}{suffix}.json"
    if output_root is not None:
        compound_dir = report_path.resolve().parent.name
        return (output_root / compound_dir / name).resolve()
    return (report_path.resolve().parent / name).resolve()


def load_report(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("top-level JSON must be an object")
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description="Prepare review-ready JSON from discover report_*.json files (offline).")
    ap.add_argument(
        "inputs",
        nargs="*",
        metavar="PATH_OR_GLOB",
        help="Report files or glob patterns (default: all pump-science/*/report_*.json).",
    )
    ap.add_argument(
        "--output-root",
        metavar="DIR",
        type=Path,
        default=None,
        help="Write prepared files under DIR/<compound_dir>/prepared_<stem>.json instead of beside each report.",
    )
    ap.add_argument("--stdout", action="store_true", help="Write one prepared JSON to stdout (requires exactly one input file).")
    ap.add_argument(
        "--format",
        choices=("review", "agent"),
        default="review",
        help='"review": full research/risks + agent_context; "agent": compact LLM-oriented bundle (digest only).',
    )
    ns = ap.parse_args()

    paths = resolve_inputs(ns.inputs, _ROOT)
    json_paths = [p for p in paths if p.suffix.lower() == ".json"]
    skipped = [p for p in paths if p.suffix.lower() != ".json"]

    for sp in skipped:
        print(f"prepare: skipped non-.json path: {sp}", file=sys.stderr)

    if not json_paths:
        print("prepare: no report JSON files to process.", file=sys.stderr)
        return 1

    if ns.stdout and len(json_paths) != 1:
        print("prepare: --stdout requires exactly one matching report file.", file=sys.stderr)
        return 1

    out_root = ns.output_root
    if out_root is not None:
        out_root = out_root.resolve()

    exit_code = 0
    for jp in json_paths:
        pw: list[str] = []
        try:
            raw = load_report(jp)
        except OSError as e:
            print(f"prepare: could not read {jp}: {e}", file=sys.stderr)
            exit_code = 1
            continue
        except (json.JSONDecodeError, UnicodeError, ValueError) as e:
            print(f"prepare: could not parse JSON {jp}: {e}", file=sys.stderr)
            exit_code = 1
            continue

        body = prepare_report(raw, pw, output_format=ns.format)
        body["source_report"] = str(jp.resolve())

        line = json.dumps(body, indent=2, ensure_ascii=False) + "\n"
        if ns.stdout:
            sys.stdout.buffer.write(line.encode("utf-8"))
            continue

        out_path = prepared_output_path(jp, out_root, output_format=ns.format)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(line, encoding="utf-8")
        except OSError as e:
            print(f"prepare: could not write {out_path}: {e}", file=sys.stderr)
            exit_code = 1
            continue

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
