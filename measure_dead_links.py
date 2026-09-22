#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Dead-link rate of the image modality, measured and dated.

A stratified sample (the same number of MAIN-image URLs per category, fixed
seed) is checked with one HEAD request per URL. Reports the failure rate
overall and per category, a 95 % Wilson interval and the date.

Writes out_final/dead_links.json and appends the macros to the tables.

    python measure_dead_links.py -o out_final --per-category 300
"""
import argparse
import io
import json
import math
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor

RACINE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, RACINE)
from check_images import test_url  # noqa: E402
from archive_reader import set_output_dir, open_file, available_categories  # noqa: E402

CHIFFRES = os.path.join(RACINE, "paper", "tables", "numbers.tex")


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def main_urls(cat):
    out = []
    with open_file(cat, ".image") as fh:
        for l in fh:
            if l.startswith("#"):
                continue
            p = l.rstrip("\n").split("\t")
            if len(p) >= 3 and p[1] == "MAIN":
                out.append(p[2])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--per-category", "--par-categorie", dest="par_categorie", type=int, default=300)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--seed", "--graine", dest="graine", type=int, default=13)
    ap.add_argument("--category", action="append", default=[])
    args = ap.parse_args(argv)
    set_output_dir(args.out)

    cats = args.category or available_categories()
    date = time.strftime("%Y-%m-%d")
    res = {"date": date, "par_categorie": args.par_categorie, "graine": args.graine,
           "variante": "MAIN", "categories": {}}
    k_tot = n_tot = 0
    for c in cats:
        chemin = open_file(c, ".image")
        if not chemin:
            continue
        urls = main_urls(c)
        tirage = random.Random(args.graine).sample(urls, min(args.par_categorie, len(urls)))
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=args.threads) as pool:
            codes = [c_ for c_, _t in pool.map(test_url, tirage)]
        morts = sum(1 for c_ in codes if c_ != 200)
        bas, haut = wilson(morts, len(tirage))
        res["categories"][c] = {
            "testees": len(tirage), "population": len(urls), "mortes": morts,
            "taux": round(morts / max(1, len(tirage)), 4),
            "ic95": [round(bas, 4), round(haut, 4)],
            "codes": {str(k): codes.count(k) for k in set(codes)},
        }
        k_tot += morts
        n_tot += len(tirage)
        print("%-32s %4d tested  %3d dead  %5.2f %%  [%.2f, %.2f]  %.0f s"
              % (c, len(tirage), morts, 100 * morts / max(1, len(tirage)),
                 100 * bas, 100 * haut, time.time() - t0), flush=True)
    bas, haut = wilson(k_tot, n_tot)
    res["total"] = {"testees": n_tot, "mortes": k_tot,
                    "taux": round(k_tot / max(1, n_tot), 4),
                    "ic95": [round(bas, 4), round(haut, 4)]}
    print("TOTAL %d tested, %d dead: %.2f %% [%.2f, %.2f]  (%s)"
          % (n_tot, k_tot, 100 * k_tot / max(1, n_tot), 100 * bas, 100 * haut, date))
    with io.open(os.path.join(args.out, "dead_links.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)

    if os.path.exists(CHIFFRES) and not args.category:
        pire = max(res["categories"].items(), key=lambda kv: kv[1]["taux"])
        s = io.open(CHIFFRES, encoding="utf-8").read()
        s = "\n".join(l for l in s.splitlines()
                      if "LiensMort" not in l and "LiensTest" not in l)
        s += ("\n\\newcommand{\\tauxLiensMorts}{%.1f}\n\\newcommand{\\tauxLiensMortsHaut}{%.1f}\n"
              "\\newcommand{\\tauxLiensMortsMax}{%.1f}\n\\newcommand{\\catLiensMortsMax}{%s}\n"
              "\\newcommand{\\nbLiensTestes}{%s}\n\\newcommand{\\dateLiensMorts}{%s}\n"
              % (100 * res["total"]["taux"], 100 * haut, 100 * pire[1]["taux"],
                 pire[0].replace("_and_", " \\& ").replace("_", " "),
                 "{:,}".format(n_tot).replace(",", "{,}"), date))
        io.open(CHIFFRES, "w", encoding="utf-8").write(s)
        print("macros appended to", CHIFFRES)
    return 0


if __name__ == "__main__":
    sys.exit(main())
