#!/usr/bin/env python3
"""OpenFDA + ClinicalTrials.gov v2 + KEGG + Europe PMC → one JSON report."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import requests

TMO = 10
KEGG_MAX = 50
FDA, CT, KEGG, EPMC = (
    "https://api.fda.gov/",
    "https://clinicaltrials.gov/api/v2/studies",
    "https://rest.kegg.jp/",
    "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
)
FLAGS = [
    ("mTOR", "mtor"), ("autophagy", "autophagy"), ("AMPK", "ampk"), ("apoptosis", "apoptosis"),
    ("cell cycle", "cell cycle"), ("oxidative stress", "oxidative stress"), ("NAD", "nad"),
    ("sirtuin", "sirtuin"), ("insulin signaling", "insulin signaling"), ("senescence", "senescence"),
]
LABELS = (
    "adverse_reactions", "adverse_reactions_table", "boxed_warning", "boxed_warning_table",
    "contraindications", "clinical_pharmacology", "pharmacokinetics", "mechanism_of_action", "drug_interactions",
)

# Always absolute — avoids drive-relative / pathlib quirks that made cwd join land in repo root.
_SCRIPT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def safe_compound_dir(compound: str) -> str:
    safe = re.sub(r"[^\w\-.]+", "_", compound, flags=re.UNICODE).strip("._- ")[:80] or "compound"
    if safe.upper() in _WIN_RESERVED:
        safe = f"_{safe}_"
    return safe


def compound_output_dir(compound: str) -> Path:
    return (_SCRIPT_DIR.parent.parent / "data" / safe_compound_dir(compound)).resolve()


def output_file_path(compound: str, explicit: str | None) -> Path:
    """Every non-absolute path is under pump-science/<compound>/ — never cwd (repo root)."""
    folder = compound_output_dir(compound)
    if explicit is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
        return folder / f"report_{ts}.json"
    raw = Path(explicit.strip()).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    target = (folder / raw).resolve()
    try:
        target.relative_to(folder)
    except ValueError:
        print(f"Refusing --output outside compound folder: {explicit!r}", file=sys.stderr)
        raise SystemExit(2) from None
    return target


def L(x: Any) -> list[Any]:
    return [] if x is None else x if isinstance(x, list) else [x]


def req(url: str, fail: list, step: str, params=None, json_out=False):
    try:
        r = requests.get(url, params=params or {}, timeout=TMO)
        if not r.ok:
            fail.append({"step": step, "reason": f"HTTP {r.status_code}: {r.text[:500]}"})
            return None
        return r.json() if json_out else r.text
    except requests.JSONDecodeError as e:
        fail.append({"step": step, "reason": f"JSON decode error: {e}"})
        return None
    except requests.RequestException as e:
        fail.append({"step": step, "reason": str(e)})
        return None


def phases(dm: Any) -> Any:
    if not isinstance(dm, dict):
        return None
    for d in (dm, dm.get("designInfo") or {}):
        if isinstance(d, dict):
            for k in ("phases", "phase", "phasesList"):
                if d.get(k) is not None:
                    return d[k]
    return None


def slim_study(study: dict, vh: list) -> dict:
    ps = study.get("protocolSection")
    ps = ps if isinstance(ps, dict) else {}
    sm, cm, om, scm = (ps.get(k) or {} for k in ("statusModule", "conditionsModule", "outcomesModule", "sponsorCollaboratorsModule"))
    dm, idm = ps.get("designModule") or {}, ps.get("identificationModule") or {}
    misc = (study.get("derivedSection") or {}).get("miscInfoModule") or {}
    if not vh[0] and isinstance(misc.get("versionHolder"), str):
        vh[0] = misc["versionHolder"]
    lead = (scm.get("leadSponsor") or {})
    po, so = om.get("primaryOutcomes"), om.get("secondaryOutcomes")
    rs = study.get("resultsSection")
    om_mod = rs.get("outcomeMeasuresModule", {}) if isinstance(rs, dict) else {}
    ms = om_mod.get("outcomeMeasures")
    rsum = None
    if isinstance(rs, dict):
        rsum = {"has_outcome_measures_module": bool(om_mod), "outcome_measures_count": len(ms) if isinstance(ms, list) else None}
    os = ("measure", "description", "timeFrame")
    return {
        "nct_id": idm.get("nctId"), "brief_title": idm.get("briefTitle"), "phases": phases(dm),
        "conditions": cm.get("conditions"),
        "primary_outcomes": [{k: x.get(k) for k in os} for x in po if isinstance(x, dict)] if isinstance(po, list) else None,
        "secondary_outcomes": [{k: x.get(k) for k in os} for x in so if isinstance(x, dict)] if isinstance(so, list) else None,
        "overall_status": sm.get("overallStatus"), "start_date": sm.get("startDateStruct"),
        "primary_completion_date": sm.get("primaryCompletionDateStruct"), "completion_date": sm.get("completionDateStruct"),
        "study_first_submit_date": sm.get("studyFirstSubmitDate"), "has_results": study.get("hasResults"),
        "results_summary": rsum, "lead_sponsor_name": lead.get("name"), "lead_sponsor_class": lead.get("class"),
    }


def run(c: str) -> dict[str, Any]:
    fail: list[dict[str, str]] = []
    ver: dict[str, Any] = {"openfda": None, "clinical_trials_gov": None, "kegg": "KEGG REST", "europe_pmc": None}

    ev = req(urljoin(FDA, "drug/event.json"), fail, "openfda_event", {"search": f"patient.drug.medicinalproduct:{c.upper()}", "limit": 100}, True)
    if isinstance(ev, dict) and ev.get("meta"):
        ver["openfda"] = ev["meta"]
    lb = req(urljoin(FDA, "drug/label.json"), fail, "openfda_label", {"search": f"openfda.generic_name:{c.title()}", "limit": 10}, True)
    if ver["openfda"] is None and isinstance(lb, dict) and lb.get("meta"):
        ver["openfda"] = lb["meta"]

    ae, dl = None, None
    label_filter_dropped = 0
    if isinstance(ev, dict):
        rows = ev.get("results")
        if isinstance(rows, list):
            terms = set()
            for it in rows:
                for p in L(it.get("patient")):
                    if isinstance(p, dict):
                        for rx in L(p.get("reaction")):
                            if isinstance(rx, dict):
                                t = rx.get("reactionmeddrapt") or rx.get("reactionmeddraversionpt")
                                if isinstance(t, str) and t.strip():
                                    terms.add(t.strip())
            ae = {"reaction_terms": sorted(terms), "report_count": len(rows), "meta": ev.get("meta")}
        else:
            ae = {"reaction_terms": [], "report_count": 0, "meta": ev.get("meta")}
    if isinstance(lb, dict):
        rs = lb.get("results")
        if isinstance(rs, list):
            c_lower = c.lower()
            # Keep only labels whose openfda.generic_name list contains the queried
            # compound name as a case-insensitive substring.  Multi-ingredient products
            # whose generic_name field merely happens to tokenise-match an unrelated
            # compound are discarded here rather than propagating into downstream units.
            filtered = [
                x for x in rs
                if isinstance(x, dict) and any(
                    c_lower in gn.lower()
                    for gn in (x.get("openfda") or {}).get("generic_name", [])
                    if isinstance(gn, str)
                )
            ]
            label_filter_dropped = len(rs) - len(filtered)
            dl = [{k: x.get(k) for k in LABELS} for x in filtered]
        else:
            dl = []

    ct_raw = req(CT, fail, "clinical_trials", {"query.term": c, "pageSize": 100, "format": "json"}, True)
    ct_out = None
    if isinstance(ct_raw, dict):
        studies = ct_raw.get("studies")
        if not isinstance(studies, list):
            ct_out = {"studies": [], "version_holder": None}
        else:
            vh_box: list[str | None] = [None]
            slim = [slim_study(s, vh_box) for s in studies if isinstance(s, dict)]
            ct_out = {"studies": slim, "study_count": len(slim), "version_holder": vh_box[0]}
        ver["clinical_trials_gov"] = {"api": "v2", "data_version_holder": ct_out.get("version_holder")}

    # KEGG
    kg = None
    txt = req(urljoin(KEGG, f"find/drug/{quote(c, safe='')}"), fail, "kegg_find", json_out=False)
    if txt is not None:
        drs = [ln.split("\t", 1)[0] for ln in txt.strip().splitlines() if "\t" in ln and ln.split("\t", 1)[0].startswith("dr:")]
        if not drs:
            kg = {"kegg_drug_ids": [], "pathways": [], "pathway_names": [], "longevity_pathway_flags": {a: False for a, _ in FLAGS}, "truncated": False}
        else:
            pids: set[str] = set()
            for dr in drs:
                lt = req(urljoin(KEGG, f"link/pathway/{dr}"), fail, f"kegg_link:{dr}", json_out=False)
                if lt:
                    for line in lt.strip().splitlines():
                        pids.update(p[5:] for p in line.split("\t") if p.startswith("path:"))
            ordered = sorted(pids)
            ent = []
            for pid in ordered[:KEGG_MAX]:
                body = req(urljoin(KEGG, f"get/{pid}"), fail, f"kegg_get:{pid}", json_out=False) or ""
                name = next((ln[4:].strip() or None for ln in body.splitlines() if ln.startswith("NAME")), None)
                m = re.search(r"^DESCRIPTION\s+(.+?)(?=^\w+\s+|\Z)", body, re.M | re.S)
                desc = (m.group(1).strip()[:2000] or None) if m else None
                ent.append({"pathway_id": pid, "name": name, "description_snippet": desc})
            blob = " ".join(f'{e.get("name") or ""} {e.get("description_snippet") or ""}' for e in ent).lower()
            kg = {
                "kegg_drug_ids": drs, "pathway_ids": ordered, "pathway_count": len(ordered), "pathways": ent,
                "pathway_names": [e["name"] for e in ent if e.get("name")],
                "longevity_pathway_flags": {a: (b in blob) for a, b in FLAGS}, "truncated": len(ordered) > KEGG_MAX, "pathway_get_limit": KEGG_MAX,
            }

    # Europe PMC — dedupe pmid > pmcid > doi > id; merge prefers longer abstract then citations
    merged: dict[str, dict] = {}

    def cite(x):
        try:
            return int(x)
        except (TypeError, ValueError):
            try:
                return int(float(str(x)))
            except (TypeError, ValueError):
                return 0

    def pick(a, b):
        la, lb = len((a.get("abstract") or "") or ""), len((b.get("abstract") or "") or "")
        if lb > la or (la == lb and cite(b.get("citedByCount")) > cite(a.get("citedByCount"))):
            return b
        return a

    for qn, q in (("longevity", f'"{c}" AND longevity'), ("aging", f'"{c}" AND aging'), ("lifespan", f'"{c}" AND lifespan')):
        d = req(EPMC, fail, f"europe_pmc:{qn}", {"query": q, "format": "json", "pageSize": 50, "resultType": "core"}, True)
        if ver["europe_pmc"] is None and isinstance(d, dict):
            ver["europe_pmc"] = {k: d.get(k) for k in ("version", "release", "hitCount")}
        rl = d.get("resultList") if isinstance(d, dict) else None
        if not isinstance(rl, dict):
            continue
        for hit in L(rl.get("result")):
            if not isinstance(hit, dict):
                continue
            key = None
            for pref, fld in (("pmid", "pmid"), ("pmcid", "pmcid"), ("doi", "doi"), ("id", "id")):
                v = hit.get(fld)
                if v:
                    key = f"{pref}:{v}"
                    break
            if not key:
                continue
            s = hit.get("authorString")
            auth = [x.strip() for x in s.split(",") if x.strip()] if isinstance(s, str) else None
            if auth is None and isinstance(hit.get("authorList"), dict):
                auth = [x.get("fullName") for x in L(hit["authorList"].get("author")) if isinstance(x, dict)]
                auth = [n for n in auth if n] or None
            rec = {
                "title": hit.get("title"), "authors": auth, "journal": hit.get("journalTitle") or hit.get("journal"),
                "year": hit.get("pubYear"), "doi": hit.get("doi"), "abstract": hit.get("abstractText") or hit.get("abstract"),
                "citedByCount": hit.get("citedByCount"), "source_queries": [],
            }
            if key not in merged:
                rec["source_queries"] = [qn]
                merged[key] = rec
            else:
                prev = merged[key]
                w = pick(prev, rec)
                w["source_queries"] = sorted(set(prev.get("source_queries") or []) | {qn})
                merged[key] = w

    epmc = {"articles": list(merged.values()), "unique_count": len(merged)}

    return {
        "compound_name": c,
        "openfda": {"adverse_events": ae, "drug_labels": dl if lb is not None else None},
        "clinical_trials": ct_out,
        "kegg": kg,
        "europe_pmc": epmc,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "api_versions": ver,
            "failures": fail,
            "label_filter_dropped": label_filter_dropped,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Compound intel from public APIs.",
        epilog=(
            "Default: writes UTF-8 JSON to a subfolder named after the compound next to this script "
            f"(under {_SCRIPT_DIR}), as <compound>/report_<UTC>.json. That does not run if you pass --stdout "
            "or an explicit --output path. The resolved output path is printed to stderr after a successful write."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--compound", required=True)
    ap.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help=(
            f"UTF-8 JSON path. Default: timestamped file under {_SCRIPT_DIR}{os.sep}<compound>{os.sep}. "
            "If relative, it is resolved under that compound folder only (not the shell cwd)."
        ),
    )
    ap.add_argument(
        "--stdout",
        action="store_true",
        help="Print JSON to stdout only (no file write).",
    )
    ns = ap.parse_args()
    c = ns.compound.strip()
    try:
        out = json.dumps(run(c), indent=2, ensure_ascii=False) + "\n"
    except Exception as e:
        out = json.dumps(
            {
                "compound_name": c or None, "openfda": None, "clinical_trials": None, "kegg": None, "europe_pmc": None,
                "metadata": {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "api_versions": {"openfda": None, "clinical_trials_gov": None, "kegg": None, "europe_pmc": None},
                    "failures": [{"step": "fatal", "reason": str(e)}],
                },
            },
            indent=2,
            ensure_ascii=False,
        ) + "\n"
    if ns.stdout:
        sys.stdout.buffer.write(out.encode("utf-8"))
    else:
        path = output_file_path(c, ns.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(out, encoding="utf-8")
        print(f"Wrote report: {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
