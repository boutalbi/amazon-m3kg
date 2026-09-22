#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Confronts the 'discover' outputs with configs/categories.yaml.

Answers a single question, for each category:
   does the attribute list frozen in the YAML match the actual data?

Three verdicts per attribute:
  MANQUANT   present and recommended in the data, captured by no relation
  MORT       declared in the YAML (sources), absent from the sampled data
  COUVERT    declared and present

Usage:
    python compare_attributes.py -c configs/categories.yaml -d out_discover/
    python compare_attributes.py -c configs/categories.yaml -d out_discover/ --category Baby_Products
"""

import argparse
import fnmatch
import json
import os
import sys

try:
    import yaml
except ImportError:
    raise SystemExit("pyyaml required: pip install pyyaml")


def load_yaml_config(chemin):
    with open(chemin, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def resolve_category_config(cfg, categorie):
    """Reproduces the 'inherit' mechanism of build_trimodal_kg.py."""
    cats = cfg.get("categories") or {}
    cat = cats.get(categorie)
    if cat is None:
        return None
    vus = set()
    chaine = []
    courant = dict(cat)
    while True:
        chaine.append(courant)
        parent = courant.get("inherit")
        if not parent or parent in vus:
            break
        vus.add(parent)
        suivant = cats.get(parent)
        if suivant is None:
            break
        courant = dict(suivant)
    relations = {}
    for niveau in reversed(chaine):
        for rel, spec in (niveau.get("relations") or {}).items():
            if spec is None or spec is False:
                relations.pop(rel, None)
            else:
                relations[rel] = spec
    return relations


def declared_sources(cfg, categorie):
    """Returns {source -> [relations]} for the category, global relations included."""
    par_source = {}
    globales = ((cfg.get("defaults") or {}).get("global_relations") or {})
    relations = dict(globales)
    propres = resolve_category_config(cfg, categorie)
    if propres is None:
        return None
    for rel, spec in propres.items():
        relations[rel] = spec
    for rel, spec in relations.items():
        if not isinstance(spec, dict):
            continue
        for src in spec.get("sources") or []:
            par_source.setdefault(str(src), []).append(rel)
    return par_source


def captured_by(cle, motifs):
    """An attribute is captured if a source equals it or filters it (wildcard).
    The comparison is case-insensitive: match_sources() of the build script
    resolves sources on lowercased keys, and the Amazon keys vary
    ('Number Of Items' / 'Number of Items')."""
    cl = cle.lower()
    for motif in motifs:
        ml = motif.lower()
        if ml == cl:
            return motif
        if any(ch in motif for ch in "*?[") and fnmatch.fnmatch(cl, ml):
            return motif
    return None


def compare_category(cfg, discovery, categorie, couverture_min=0.02):
    par_source = declared_sources(cfg, categorie)
    if par_source is None:
        return None
    motifs = list(par_source)
    presents = {a["key"]: a for a in discovery["attributes"]}

    manquants, couverts = [], []
    for cle, a in presents.items():
        motif = captured_by(cle, motifs)
        if motif:
            couverts.append((cle, a, motif, par_source[motif]))
        elif a["recommended"]:
            manquants.append((cle, a))

    presents_bas = {k.lower() for k in presents}
    morts = []
    for motif in motifs:
        ml = motif.lower()
        if any(ch in motif for ch in "*?["):
            if not any(fnmatch.fnmatch(k, ml) for k in presents_bas):
                morts.append((motif, par_source[motif]))
        elif ml not in presents_bas:
            morts.append((motif, par_source[motif]))

    manquants.sort(key=lambda x: -x[1]["coverage"])
    couverts.sort(key=lambda x: -x[1]["coverage"])
    return {
        "categorie": categorie,
        "echantillon": discovery["sampled_products"],
        "manquants": manquants,
        "morts": morts,
        "couverts": couverts,
        "redondances": discovery.get("redundancy_candidates", []),
    }


def display(res, seuil_alerte=0.05, max_lignes=25):
    lignes = []
    a = lignes.append
    a("=" * 84)
    a("%s   (echantillon %s produits)"
      % (res["categorie"], format(res["echantillon"], ",")))
    a("=" * 84)

    graves = [(k, at) for k, at in res["manquants"] if at["coverage"] >= seuil_alerte]
    a("  ATTRIBUTS MANQUANTS DANS LE YAML : %d dont %d au-dessus de %d%% de couverture"
      % (len(res["manquants"]), len(graves), 100 * seuil_alerte))
    if res["manquants"]:
        a("    %-42s%8s%10s  EXEMPLES" % ("ATTRIBUT", "COUV.", "DISTINCT"))
        for cle, at in res["manquants"][:max_lignes]:
            ex = ", ".join(str(x)[:18] for x in at.get("atomic_examples", [])[:3])
            a("    %-42s%7.1f%%%10s  %s"
              % (cle[:41], 100 * at["coverage"],
                 format(at["distinct_normalized"], ","), ex[:34]))

    if res["morts"]:
        a("  SOURCES DECLAREES MAIS ABSENTES DES DONNEES : %d" % len(res["morts"]))
        for motif, rels in res["morts"][:max_lignes]:
            a("    %-52s <- %s" % (motif[:51], ", ".join(rels)))

    a("  SOURCES EFFECTIVEMENT UTILISEES : %d" % len(res["couverts"]))
    for cle, at, motif, rels in res["couverts"][:max_lignes]:
        via = "" if motif == cle else "  (via %s)" % motif
        a("    %-42s%7.1f%%  -> %s%s"
          % (cle[:41], 100 * at["coverage"], ", ".join(rels), via))

    fusions = [r for r in res["redondances"] if r.get("verdict", "").startswith("FUSION")]
    if fusions:
        a("  REDONDANCES A FUSIONNER : %d" % len(fusions))
        for r in fusions[:12]:
            a("    %-34s <-> %-30s jaccard=%s" % (r["a"][:33], r["b"][:29], r["jaccard"]))
    return "\n".join(lignes)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Confronte les resultats de 'discover' a categories.yaml")
    ap.add_argument("-c", "--config", required=True)
    ap.add_argument("-d", "--discover-dir", required=True)
    ap.add_argument("--category", action="append")
    ap.add_argument("--threshold", "--seuil", dest="seuil", type=float, default=0.05,
                    help="couverture au-dela de laquelle un attribut manquant est grave")
    ap.add_argument("--json", help="write the summary to this file")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    cfg = load_yaml_config(args.config)
    fichiers = sorted(f for f in os.listdir(args.discover_dir)
                      if f.endswith(".discovery.json"))
    if args.category:
        voulus = set(args.category)
        fichiers = [f for f in fichiers
                    if f[: -len(".discovery.json")] in voulus]
    if not fichiers:
        raise SystemExit("No usable .discovery.json in %s" % args.discover_dir)

    synthese = []
    for nom in fichiers:
        categorie = nom[: -len(".discovery.json")]
        with open(os.path.join(args.discover_dir, nom), encoding="utf-8") as fh:
            discovery = json.load(fh)
        res = compare_category(cfg, discovery, categorie)
        if res is None:
            print("!! %s absent from categories.yaml" % categorie)
            continue
        print(display(res, seuil_alerte=args.seuil))
        print()
        graves = [k for k, at in res["manquants"] if at["coverage"] >= args.seuil]
        synthese.append({
            "categorie": categorie,
            "manquants": len(res["manquants"]),
            "manquants_graves": graves,
            "morts": [m for m, _ in res["morts"]],
            "couverts": len(res["couverts"]),
        })

    print("=" * 84)
    print("SUMMARY")
    print("  %-32s | %8s | %9s | %6s | %5s"
          % ("CATEGORIE", "COUVERTS", "MANQUANTS", "GRAVES", "MORTS"))
    print("  " + "-" * 74)
    for s in synthese:
        print("  %-32s | %8d | %9d | %6d | %5d"
              % (s["categorie"], s["couverts"], s["manquants"],
                 len(s["manquants_graves"]), len(s["morts"])))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(synthese, fh, ensure_ascii=False, indent=2)
        print("\nSummary: %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
