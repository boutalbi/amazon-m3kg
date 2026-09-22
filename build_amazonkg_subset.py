#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Subset IDENTICAL to Amazon-KG v2.0 (Books, Movies) built with OUR graph,
so that only the graph changes in the comparison. Everything is written into
a separate folder (out_amazonkg/).

Steps, per category: take the ASINs of their <cat>.link; read the matching
2023 metadata; look the missing items up in the 2014 dump (converted to the
2023 shape, hence category, brand and price only); build the graph with the
category config, without the tri-modal filter and without the evaluation
relations; export to RecBole with their .inter as is and their .kg copied
alongside (<CAT>_amazonkg.kg).

    python build_amazonkg_subset.py --category Books \
        --akg data/amazonkg/Books
"""
import argparse
import ast
import gzip
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter

RACINE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
DL = os.environ.get("M3KG_DATA", "data")
OUT = os.path.join(RACINE, "out_amazonkg")
SIGNAL = {"hasPopularity", "hasRatingLevel"}
_ASIN_2014 = re.compile(r"'asin':\s*'([^']+)'")
_PID_2023 = re.compile(r'"parent_asin":\s*"([^"]+)"')


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def targets(dossier_akg):
    for f in os.listdir(dossier_akg):
        if f.endswith(".link"):
            s = set()
            for l in io.open(os.path.join(dossier_akg, f), encoding="utf-8", errors="replace"):
                p = l.rstrip("\n").split("\t")
                if len(p) >= 2 and not p[0].endswith(":token"):
                    s.add(p[0])
            return s, os.path.join(dossier_akg, f)
    raise SystemExit("no .link in %s" % dossier_akg)


def extract_2023(chemin, voulus, sortie):
    """Copies the JSONL lines whose parent_asin is wanted."""
    trouves = set()
    n = 0
    with gzip.open(chemin, "rt", encoding="utf-8", errors="replace") as src, \
            gzip.open(sortie, "wt", encoding="utf-8") as out:
        for l in src:
            n += 1
            if n % 1000000 == 0:
                log("  ... 2023: %s lines, %s found" % (format(n, ","), format(len(trouves), ",")))
            m = _PID_2023.search(l)
            if not m or m.group(1) not in voulus or m.group(1) in trouves:
                continue
            out.write(l if l.endswith("\n") else l + "\n")
            trouves.add(m.group(1))
    return trouves


def convert_2014(d, categorie):
    """2014 record -> minimal 2023 shape that the pipeline can read."""
    cats = d.get("categories") or []
    chemin = max(cats, key=len) if cats else []
    rec = {
        "parent_asin": d["asin"],
        "main_category": chemin[0] if chemin else categorie.replace("_and_", " & ").replace("_", " "),
        "title": html.unescape(d.get("title", "") or ""),
        "description": [html.unescape(d["description"])] if d.get("description") else [],
        "categories": [html.unescape(c) for c in chemin],
        "features": [],
        "images": [{"large": d["imUrl"], "variant": "MAIN"}] if d.get("imUrl") else [],
        "details": {},
        "source": "amazon_2014",
    }
    if d.get("brand"):
        rec["details"]["Brand"] = html.unescape(d["brand"])
    if d.get("price") is not None:
        rec["price"] = d["price"]
    return rec


def extract_2014(chemin, voulus, sortie, categorie):
    trouves = set()
    n = 0
    with gzip.open(chemin, "rt", encoding="utf-8", errors="replace") as src, \
            gzip.open(sortie, "wt", encoding="utf-8") as out:
        for l in src:
            n += 1
            m = _ASIN_2014.search(l)
            if not m or m.group(1) not in voulus or m.group(1) in trouves:
                continue
            try:
                d = ast.literal_eval(l.strip())
            except (ValueError, SyntaxError):
                continue
            out.write(json.dumps(convert_2014(d, categorie), ensure_ascii=False) + "\n")
            trouves.add(m.group(1))
    return trouves


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--category", required=True, help="Books or Movies_and_TV")
    ap.add_argument("--akg", required=True, help="Amazon-KG v2.0 folder of the category")
    ap.add_argument("--meta2023", default=None)
    ap.add_argument("--meta2014", default=None)
    args = ap.parse_args(argv)
    cat = args.category
    m23 = args.meta2023 or os.path.join(DL, "meta_%s.jsonl.gz" % cat)
    m14 = args.meta2014 or os.path.join(DL, "meta_%s.json.gz" % cat)
    os.makedirs(OUT, exist_ok=True)
    t0 = time.time()

    voulus, link_akg = targets(args.akg)
    log("%s: %s target products in Amazon-KG" % (cat, format(len(voulus), ",")))

    p23 = os.path.join(OUT, "meta_%s_2023.jsonl.gz" % cat)
    t23 = extract_2023(m23, voulus, p23)
    log("2023: %s found, %s missing" % (format(len(t23), ","), format(len(voulus - t23), ",")))

    p14 = os.path.join(OUT, "meta_%s_2014.jsonl.gz" % cat)
    t14 = set()
    if os.path.exists(m14) and voulus - t23:
        t14 = extract_2014(m14, voulus - t23, p14, cat)
        log("2014: %s of the missing ones recovered; %s not found"
            % (format(len(t14), ","), format(len(voulus - t23 - t14), ",")))
    else:
        log("2014: file missing (%s) or nothing to look for" % m14)

    # concatenation and build
    p_all = os.path.join(OUT, "meta_%s_amazonkg.jsonl.gz" % cat)
    with gzip.open(p_all, "wb") as out:
        for p in (p23, p14):
            if os.path.exists(p):
                with gzip.open(p, "rb") as src:
                    shutil.copyfileobj(src, out)
    log("building the graph on %s products" % format(len(t23 | t14), ","))
    r = subprocess.run([PY, os.path.join(RACINE, "build_trimodal_kg.py"), "build",
                        "-c", os.path.join(RACINE, "configs", "categories.yaml"),
                        "--category", cat, "-i", p_all, "-o", OUT + "/"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(r.stdout[-3000:], r.stderr[-3000:])
        sys.exit("build failed")
    for l in r.stdout.splitlines():
        if "termine :" in l or "produits lus" in l:
            log(l.strip())

    # removal of the evaluation signals, head = asin
    kg = os.path.join(OUT, cat + ".kg")
    link = os.path.join(OUT, cat + ".link")
    sujet_vers_pid = {}
    for l in io.open(link, encoding="utf-8", errors="replace"):
        if l.startswith("# "):
            continue
        p = l.rstrip("\n").split("\t")
        if len(p) >= 2:
            sujet_vers_pid[p[0]] = p[1]
    rec_dir = os.path.join(OUT, "recbole", cat)
    os.makedirs(rec_dir, exist_ok=True)
    pref = os.path.join(rec_dir, cat)
    rels = Counter()
    items_kg = set()
    n = 0
    with io.open(kg, encoding="utf-8", errors="replace") as src, \
            io.open(pref + ".kg", "w", encoding="utf-8", newline="\n") as out:
        out.write("head_id:token\trelation_id:token\ttail_id:token\n")
        for l in src:
            if l.startswith("# taxonomie"):
                break
            if l.startswith("# "):
                continue
            p = l.rstrip("\n").split("\t")
            if len(p) != 3 or p[1] in SIGNAL:
                continue
            pid = sujet_vers_pid.get(p[0])
            if pid is None:
                continue
            out.write("%s\t%s\t%s\n" % (pid, p[1], p[2]))
            rels[p[1]] += 1
            items_kg.add(pid)
            n += 1

    # their interactions, as is; their graph copied alongside
    inter_akg = [f for f in os.listdir(args.akg) if f.endswith(".inter")][0]
    shutil.copyfile(os.path.join(args.akg, inter_akg), pref + ".inter")
    kg_akg = [f for f in os.listdir(args.akg) if f.endswith(".kg")][0]
    shutil.copyfile(os.path.join(args.akg, kg_akg), pref + "_amazonkg.kg")
    shutil.copyfile(link_akg, pref + "_amazonkg.link")
    with io.open(pref + ".link", "w", encoding="utf-8", newline="\n") as fh:
        fh.write("item_id:token\tentity_id:token\n")
        for i in sorted(voulus):
            fh.write("%s\t%s\n" % (i, i))
    n_inter = sum(1 for _ in io.open(pref + ".inter", encoding="utf-8")) - 1

    resume = {
        "category": cat, "targets": len(voulus), "found_2023": len(t23), "found_2014": len(t14),
        "not_found": len(voulus - t23 - t14), "items_with_kg": len(items_kg),
        "kg_triples": n, "kg_relations": len(rels), "kg_triples_per_item": round(n / max(1, len(items_kg)), 2),
        "relations": dict(rels.most_common()), "interactions_amazonkg": n_inter,
        "amazonkg_kg_file": pref + "_amazonkg.kg", "date": time.strftime("%Y-%m-%d"),
    }
    with io.open(pref + ".recbole.json", "w", encoding="utf-8") as fh:
        json.dump(resume, fh, indent=1)
    log("%s: %s products with a graph out of %s targets; %s triples, %d relations (%.2f / product); "
        "%s interactions Amazon-KG ; %.0f s"
        % (cat, format(len(items_kg), ","), format(len(voulus), ","), format(n, ","), len(rels),
           n / max(1, len(items_kg)), format(n_inter, ","), time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
