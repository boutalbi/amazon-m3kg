#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Users shared between categories: cross-domain on the interaction side.

For each pair, how many USERS interacted in both, which conditions any
transfer. Scope: reviews on a released product only, to stay consistent with
the exported splits. Two counts per category (users, and users with at least
five interactions), and per pair: shared users, Jaccard, share of the smaller
category.

Reviews are read with a regular expression rather than json.loads: ten times
faster, and sufficient since both fields are strings without escapes.

Writes out_final/shared_users.json, the LaTeX table and the macros.

    python analyze_shared_users.py -o out_final --reviews data
"""
import argparse
import gzip
import io
import itertools
import json
import os
import re
import sys
import time
from collections import Counter

RACINE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, RACINE)
from archive_reader import set_output_dir, open_file, available_categories  # noqa: E402
CHIFFRES = os.path.join(RACINE, "paper", "tables", "numbers.tex")
_USER = re.compile(r'"user_id":\s*"([^"]+)"')
_ITEM = re.compile(r'"parent_asin":\s*"([^"]+)"')
ACTIF = 5


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def released_products(base):
    s = set()
    with io.open(base, encoding="utf-8", errors="replace") as fh:
        for l in fh:
            if l.startswith("# "):
                continue
            p = l.rstrip("\n").split("\t")
            if len(p) >= 2:
                s.add(p[1])
    return s


def users_of(chemin, retenus):
    """Counter user -> number of interactions on released products."""
    c = Counter()
    n = 0
    with gzip.open(chemin, "rt", encoding="utf-8", errors="replace") as fh:
        for l in fh:
            n += 1
            if n % 5000000 == 0:
                log("  ... %s reviews" % format(n, ","))
            m = _ITEM.search(l)
            if not m or m.group(1) not in retenus:
                continue
            u = _USER.search(l)
            if u:
                c[u.group(1)] += 1
    return n, c


def users_from_inter(cat):
    """Same count, read from the exported <CAT>.inter (already filtered to the
    released products, one row per pair): the originals are deleted after the
    export. The .inter is read from out_final/recbole/ or from the global archive."""
    c = Counter()
    n = 0
    with open_file(cat, "recbole/%s.inter" % cat) as fh:
        next(fh)
        for l in fh:
            n += 1
            c[l.split("\t", 1)[0]] += 1
    return n, c


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--reviews", "--avis", dest="avis", required=True, help="folder holding the <CAT>.jsonl.gz files")
    ap.add_argument("--category", action="append", default=[])
    ap.add_argument("--tex", default=os.path.join(RACINE, "paper", "tables", "shared_users.tex"))
    args = ap.parse_args(argv)
    set_output_dir(args.out)
    cache_p = os.path.join(args.out, "users_per_category.json")
    cache = json.load(io.open(cache_p, encoding="utf-8")) if os.path.exists(cache_p) else {}

    cats = args.category or available_categories()
    par_cat = {}
    for c in cats:
        avis = os.path.join(args.avis, c + ".jsonl.gz")
        inter_ok = open_file(c, "recbole/%s.inter" % c) is not None
        link = os.path.join(args.out, c + ".link")
        if not inter_ok and not (os.path.exists(avis) and os.path.exists(link)):
            log("%s: neither exported .inter nor reviews, skipped" % c)
            continue
        # cache: the exported .inter is frozen (an export is final), so the
        # signature is the file name; for raw reviews, the .link
        sig = "inter" if inter_ok else "%d:%d" % (os.path.getsize(link), int(os.path.getmtime(link)))
        if c in cache and cache[c].get("sig") in (sig, "inter"):
            par_cat[c] = cache[c]
            log("%s: from the cache (%s users)" % (c, format(len(cache[c]["u"]), ",")))
            continue
        t0 = time.time()
        if inter_ok:
            n, cu = users_from_inter(c)
        else:
            retenus = released_products(link)
            n, cu = users_of(avis, retenus)
        par_cat[c] = {"sig": sig, "avis": n, "interactions": sum(cu.values()),
                      "u": list(cu), "actifs": [u for u, k in cu.items() if k >= ACTIF]}
        cache[c] = par_cat[c]
        json.dump(cache, io.open(cache_p, "w", encoding="utf-8"))
        log("%s: %s reviews, %s on released products, %s users of which %s active (%.0f s)"
            % (c, format(n, ","), format(par_cat[c]["interactions"], ","), format(len(cu), ","),
               format(len(par_cat[c]["actifs"]), ","), time.time() - t0))
    cats = [c for c in cats if c in par_cat]
    if len(cats) < 2:
        sys.exit("fewer than two categories with reviews")

    U = {c: set(par_cat[c]["u"]) for c in cats}
    A = {c: set(par_cat[c]["actifs"]) for c in cats}
    tous = set().union(*U.values())
    nb_cat_par_u = Counter()
    for c in cats:
        for u in U[c]:
            nb_cat_par_u[u] += 1
    multi = sum(1 for u, k in nb_cat_par_u.items() if k >= 2)
    res = {"categories": cats, "par_categorie": {c: {"utilisateurs": len(U[c]), "actifs": len(A[c]),
                                                    "interactions": par_cat[c]["interactions"]} for c in cats},
           "utilisateurs_total": len(tous), "utilisateurs_multi_categories": multi,
           "part_multi": round(multi / max(1, len(tous)), 4), "paires": {}}
    for a, b in itertools.combinations(cats, 2):
        commun = U[a] & U[b]
        commun_act = A[a] & A[b]
        res["paires"]["%s|%s" % (a, b)] = {
            "communs": len(commun), "actifs_communs": len(commun_act),
            "jaccard": round(len(commun) / max(1, len(U[a] | U[b])), 5),
            "part_du_plus_petit": round(len(commun) / max(1, min(len(U[a]), len(U[b]))), 4),
        }
    with io.open(os.path.join(args.out, "shared_users.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)

    # --- LaTeX table: half matrix of shared users (thousands) ---------------
    court = {c: c.replace("_and_", " & ").replace("_", " ") for c in cats}
    abrev = {c: "".join(w[0] for w in court[c].replace("&", "").split())[:4] for c in cats}
    t = []
    a_ = t.append
    a_(r"% GENERATED BY analyze_shared_users.py -- DO NOT EDIT BY HAND")
    a_(r"\begin{table}[t]")
    a_(r"\centering")
    a_(r"\caption{Users shared between categories (thousands), counting only "
       r"interactions on released products. Diagonal: users of the category. "
       r"Column labels abbreviate the row names.}")
    a_(r"\label{tab:utilisateurs}")
    a_(r"\setlength{\tabcolsep}{2.2pt}")
    a_(r"\scriptsize")
    a_(r"\begin{tabular}{l" + "r" * len(cats) + "}")
    a_(r"\toprule")
    a_(" & " + " & ".join(abrev[c] for c in cats) + r" \\")
    a_(r"\midrule")
    for i, a in enumerate(cats):
        cellules = []
        for j, b in enumerate(cats):
            if j < i:
                cellules.append("")
            elif j == i:
                cellules.append(r"\textbf{%.0f}" % (len(U[a]) / 1000))
            else:
                cellules.append("%.1f" % (res["paires"]["%s|%s" % (a, b)]["communs"] / 1000))
        a_(court[a].replace("&", r"\&") + " & " + " & ".join(cellules) + r" \\")
    a_(r"\bottomrule")
    a_(r"\end{tabular}")
    a_(r"\end{table}")
    os.makedirs(os.path.dirname(args.tex), exist_ok=True)
    io.open(args.tex, "w", encoding="utf-8").write("\n".join(t) + "\n")

    if os.path.exists(CHIFFRES) and not args.category:
        pire = max(res["paires"].items(), key=lambda kv: kv[1]["communs"])
        pa, pb = pire[0].split("|")
        s = io.open(CHIFFRES, encoding="utf-8").read()
        s = "\n".join(l for l in s.splitlines() if "Utilisateurs" not in l)
        s += ("\n\\newcommand{\\nbUtilisateursTotal}{%s}\n\\newcommand{\\nbUtilisateursMultiCat}{%s}\n"
              "\\newcommand{\\partUtilisateursMultiCat}{%.1f}\n\\newcommand{\\paireUtilisateursMax}{%s--%s}\n"
              "\\newcommand{\\nbUtilisateursPaireMax}{%s}\n\\newcommand{\\nbCategoriesAvecAvis}{%d}\n"
              % ("{:,}".format(len(tous)).replace(",", "{,}"), "{:,}".format(multi).replace(",", "{,}"),
                 100 * res["part_multi"], court[pa].replace("&", "\\&"), court[pb].replace("&", "\\&"),
                 "{:,}".format(pire[1]["communs"]).replace(",", "{,}"), len(cats)))
        io.open(CHIFFRES, "w", encoding="utf-8").write(s)

    log("%s distinct users over %d categories, %s (%.1f %%) present in at least two"
        % (format(len(tous), ","), len(cats), format(multi, ","), 100 * res["part_multi"]))
    for cle, v in sorted(res["paires"].items(), key=lambda kv: -kv[1]["communs"])[:8]:
        a, b = cle.split("|")
        print("  %-26s %-26s %9s shared  %6s active  J=%.4f  %.1f %% of the smaller"
              % (a, b, format(v["communs"], ","), format(v["actifs_communs"], ","), v["jaccard"],
                 100 * v["part_du_plus_petit"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
