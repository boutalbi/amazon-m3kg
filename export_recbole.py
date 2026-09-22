#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""RecBole export of a category: interactions filtered to the released
products, 5-core, frozen chronological split, graph and link table in
'atomic files' format.

Pipeline: keep the interactions whose product is released (tri-modal), reduce
a repeated (user, product) pair to its last occurrence, apply an iterative
5-core, then split per user 80/10/10 in time order (RecBole: order TO,
group_by user). The split files are the frozen artefact, so that two papers
compare the same thing.

--delete-source deletes the review file once the export is verified.
--intersection <Amazon-KG folder> further restricts the products to theirs,
for the head-to-head comparison.

Outputs, in <out>/recbole/<CAT>[_amazonkg]/:
  <CAT>.inter                every interaction on released products
  <CAT>.train.inter, .valid.inter, .test.inter   5-core, chronological
  <CAT>.kg                   head_id:token  relation_id:token  tail_id:token
  <CAT>.link                 item_id:token  entity_id:token  (identity)
  <CAT>.recbole.json         counts at each step, seed, date

    python export_recbole.py -o out_final --category All_Beauty \
        --reviews data/All_Beauty.jsonl.gz
"""
import argparse
import gzip
import io
import json
import os
import sys
import time
from collections import Counter, defaultdict

RACINE = os.path.dirname(os.path.abspath(__file__))
K_CORE = 5
TAXO = "# taxonomie"


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def released_products(base):
    """{parent_asin} of the released products (the .link is rewritten after the
    tri-modal filter, so it is authoritative), and .kg subject -> parent_asin."""
    sujet_vers_pid = {}
    with io.open(base + ".link", encoding="utf-8", errors="replace") as fh:
        for l in fh:
            if l.startswith("# "):
                continue
            p = l.rstrip("\n").split("\t")
            if len(p) >= 2:
                sujet_vers_pid[p[0]] = p[1]
    return sujet_vers_pid


def read_interactions(chemin, retenus):
    """(user, item, rating, ts) for the reviews whose parent_asin is released;
    a repeated pair keeps its last occurrence."""
    derniere = {}
    n_lus = 0
    ouvrir = gzip.open if chemin.endswith(".gz") else io.open
    with ouvrir(chemin, "rt", encoding="utf-8", errors="replace") as fh:
        for l in fh:
            n_lus += 1
            if n_lus % 2000000 == 0:
                log("  ... %s reviews read, %s kept" % (format(n_lus, ","), format(len(derniere), ",")))
            try:
                d = json.loads(l)
            except ValueError:
                continue
            item = d.get("parent_asin") or d.get("asin")
            if item not in retenus:
                continue
            u = d.get("user_id")
            ts = d.get("timestamp")
            if not u or ts is None:
                continue
            ts = int(ts) // 1000 if int(ts) > 10 ** 11 else int(ts)   # ms -> s
            cle = (u, item)
            prec = derniere.get(cle)
            if prec is None or ts > prec[1]:
                derniere[cle] = (float(d.get("rating", 0) or 0), ts)
    return n_lus, [(u, i, r, ts) for (u, i), (r, ts) in derniere.items()]


def k_core(inter, k):
    tours = 0
    while True:
        cu, ci = Counter(), Counter()
        for u, i, _r, _t in inter:
            cu[u] += 1
            ci[i] += 1
        garde = [x for x in inter if cu[x[0]] >= k and ci[x[1]] >= k]
        tours += 1
        if len(garde) == len(inter):
            return inter, tours
        inter = garde
        if not inter:
            return inter, tours


def split_per_user(inter, ratios=(0.8, 0.1, 0.1)):
    """Chronological per user: the last 10 % in test, the 10 % before that in
    validation. Rounded as RecBole does (cumulative then truncated), with at
    least one validation example and one test example per user."""
    par_u = defaultdict(list)
    for x in inter:
        par_u[x[0]].append(x)
    train, valid, test = [], [], []
    for u, xs in par_u.items():
        xs.sort(key=lambda x: (x[3], x[1]))
        n = len(xs)
        n_test = max(1, int(n * ratios[2]))
        n_valid = max(1, int(n * ratios[1]))
        n_train = n - n_valid - n_test
        train += xs[:n_train]
        valid += xs[n_train:n_train + n_valid]
        test += xs[n_train + n_valid:]
    return train, valid, test


def write_inter(chemin, inter):
    """Four columns: user, product, rating, timestamp (seconds)."""
    n = 0
    with io.open(chemin, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("user_id:token\titem_id:token\trating:float\ttimestamp:float\n")
        for u, i, r, ts in inter:
            fh.write("%s\t%s\t%g\t%d\n" % (u, i, r, ts))
            n += 1
    return n


def write_kg(base, dest, sujet_vers_pid, items):
    n = 0
    rels = Counter()
    with io.open(base + ".kg", encoding="utf-8", errors="replace") as src, \
            io.open(dest, "w", encoding="utf-8", newline="\n") as out:
        out.write("head_id:token\trelation_id:token\ttail_id:token\n")
        for l in src:
            if l.startswith("# "):
                if l.startswith(TAXO):
                    break            # taxonomy edges have no product as head
                continue
            p = l.rstrip("\n").split("\t")
            if len(p) != 3:
                continue
            pid = sujet_vers_pid.get(p[0])
            if pid is None or pid not in items:
                continue
            out.write("%s\t%s\t%s\n" % (pid, p[1], p[2]))
            rels[p[1]] += 1
            n += 1
    return n, rels


def amazonkg_items(dossier):
    for f in os.listdir(dossier):
        if f.endswith(".link"):
            s = set()
            for l in io.open(os.path.join(dossier, f), encoding="utf-8", errors="replace"):
                p = l.rstrip("\n").split("\t")
                if len(p) >= 2 and not p[0].endswith(":token"):
                    s.add(p[0])
            return s
    raise SystemExit("no .link in %s" % dossier)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--category", required=True)
    ap.add_argument("--reviews", required=True, help="<CAT>.jsonl[.gz] of the Amazon Reviews 2023 reviews")
    ap.add_argument("--intersection", help="Amazon-KG v2.0 folder (contains a *.link)")
    ap.add_argument("--k", type=int, default=K_CORE)
    ap.add_argument("--delete-source", "--supprimer-source", dest="supprimer_source", action="store_true",
                    help="efface le fichier d'avis apres un export verifie")
    args = ap.parse_args(argv)
    t0 = time.time()
    base = os.path.join(args.out, args.category)
    nom = args.category + ("_amazonkg" if args.intersection else "")
    dossier = os.path.join(args.out, "recbole", nom)
    os.makedirs(dossier, exist_ok=True)
    pref = os.path.join(dossier, args.category)

    sujet_vers_pid = released_products(base)
    retenus = set(sujet_vers_pid.values())
    log("%s: %s products released" % (args.category, format(len(retenus), ",")))
    etapes = {"products_release": len(retenus)}
    if args.intersection:
        akg = amazonkg_items(args.intersection)
        retenus &= akg
        etapes["products_amazonkg"] = len(akg)
        etapes["products_intersection"] = len(retenus)
        log("intersection with Amazon-KG: %s common products out of %s" % (format(len(retenus), ","), format(len(akg), ",")))

    log("reading the reviews %s" % args.reviews)
    n_lus, inter = read_interactions(args.reviews, retenus)
    etapes["reviews_read"] = n_lus
    etapes["interactions_on_retained_products"] = len(inter)
    log("%s reviews read, %s interactions (deduplicated pairs) on released products"
        % (format(n_lus, ","), format(len(inter), ",")))

    # 1. the complete file, which replaces the original reviews
    inter.sort(key=lambda x: (x[0], x[3], x[1]))
    n_ecrit = write_inter(pref + ".inter", inter)
    items = {x[1] for x in inter}
    etapes["inter_file"] = {"rows": n_ecrit, "users": len({x[0] for x in inter}), "items": len(items),
                            "columns": ["user_id", "item_id", "rating", "timestamp"]}

    # 2. 5-core and chronological split, frozen
    coeur, tours = k_core(list(inter), args.k)
    users_c = {x[0] for x in coeur}
    items_c = {x[1] for x in coeur}
    etapes.update({"k": args.k, "k_core_rounds": tours, "interactions": len(coeur),
                   "users": len(users_c), "items": len(items_c),
                   "density": round(len(coeur) / max(1, len(users_c) * len(items_c)), 8)})
    log("%d-core in %d rounds: %s interactions, %s users, %s products"
        % (args.k, tours, format(len(coeur), ","), format(len(users_c), ","), format(len(items_c), ",")))
    train, valid, test = split_per_user(coeur)
    write_inter(pref + ".train.inter", train)
    write_inter(pref + ".valid.inter", valid)
    write_inter(pref + ".test.inter", test)
    etapes["split"] = {"order": "chronological per user", "ratios": [0.8, 0.1, 0.1],
                       "train": len(train), "valid": len(valid), "test": len(test)}

    # 3. graph: every product of the .inter, not only those of the 5-core
    n_kg, rels = write_kg(base, pref + ".kg", sujet_vers_pid, items)
    with io.open(pref + ".link", "w", encoding="utf-8", newline="\n") as fh:
        fh.write("item_id:token\tentity_id:token\n")
        for i in sorted(items):
            fh.write("%s\t%s\n" % (i, i))
    etapes["kg_triples"] = n_kg
    etapes["kg_relations"] = len(rels)
    etapes["kg_triples_per_item"] = round(n_kg / max(1, len(items)), 2)
    etapes["date"] = time.strftime("%Y-%m-%d")
    etapes["reviews_file"] = os.path.basename(args.reviews)
    with io.open(pref + ".recbole.json", "w", encoding="utf-8") as fh:
        json.dump(etapes, fh, indent=1)
    log("graph: %s triples over %d relations (%.2f / product); done in %.0f s -> %s"
        % (format(n_kg, ","), len(rels), n_kg / max(1, len(items)), time.time() - t0, dossier))

    if args.supprimer_source:
        # the json is read back and its counts must match the file written
        controle = json.load(io.open(pref + ".recbole.json", encoding="utf-8"))
        with io.open(pref + ".inter", encoding="utf-8") as fh:
            lignes = sum(1 for _ in fh) - 1
        if controle["inter_file"]["rows"] == lignes == len(inter) and lignes > 0:
            os.remove(args.reviews)
            log("source deleted: %s (%s rows kept in %s.inter)"
                % (args.reviews, format(lignes, ","), args.category))
        else:
            log("WARNING: inconsistent counts, source kept")
    return 0


if __name__ == "__main__":
    sys.exit(main())
