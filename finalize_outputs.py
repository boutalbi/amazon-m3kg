#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Post-processing of the outputs of a category, without rebuilding.

Two operations, in a single pass over the large files:

1. TRI-MODAL FILTER. A product is kept only if it carries triples, a text and
   an image. Discarded products are counted in the report.

2. SEPARATION OF THE EVALUATION SIGNALS. hasPopularity and hasRatingLevel are
   aggregates computed by Amazon over ALL ratings, test split included, so
   they are moved out of the .kg into a <CAT>.signal file.

The files are rewritten in place and <CAT>.report.json is updated. Idempotent:
a second pass changes nothing.

    python finalize_outputs.py -o out_final --category Books
    python finalize_outputs.py -o out_final --all
"""
import argparse
import io
import json
import os
import sys
import time
from collections import Counter

RELATIONS_SIGNAL = ("hasPopularity", "hasRatingLevel")
SEPARATEUR_TAXO = "# taxonomie"

# A product name may start with '#' ('#1 Best Natural Fruit Acid Cleanser'),
# so only the KNOWN headers are treated as comments, and the leading '#' is
# stripped from subjects.
_ENTETES = ("# subject", "# product_id", "# taxonomie", "# WARNING", "# AVERTISSEMENT")


def is_comment(ligne):
    return ligne.startswith(_ENTETES) or ligne.startswith("# ")


def clean_subject(sujet):
    """Subject without a leading '#' or leading blank."""
    return sujet.lstrip("# 	") or sujet

ENTETE_SIGNAL = (
    "# WARNING -- evaluation signals, NOT product knowledge.\n"
    "# hasPopularity and hasRatingLevel are derived from rating_number and\n"
    "# average_rating, aggregated by Amazon over every rating, including those\n"
    "# of a test period. Feeding them to a recommendation model evaluated on\n"
    "# those same ratings is a leak.\n"
    "# They are provided for analysis (popularity bias, Section 8 of the\n"
    "# paper), never for training.\n"
    "# subject\trelation\tobject\n"
)


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def read_pids_column(chemin, colonne):
    """Set of the identifiers present in one column of a TSV."""
    pids = set()
    with io.open(chemin, encoding="utf-8", errors="replace") as fh:
        for ligne in fh:
            if is_comment(ligne):
                continue
            parts = ligne.rstrip("\n").split("\t")
            if len(parts) > colonne and parts[colonne]:
                pids.add(parts[colonne])
    return pids


def read_link(chemin):
    """subject -> pid, and the set of pids."""
    sujet_vers_pid = {}
    with io.open(chemin, encoding="utf-8", errors="replace") as fh:
        for ligne in fh:
            if is_comment(ligne):
                continue
            parts = ligne.rstrip("\n").split("\t")
            if len(parts) >= 2:
                sujet_vers_pid[parts[0]] = parts[1]
    return sujet_vers_pid


def filter_tsv(chemin, garder, colonne, colonne_sujet=None):
    """Rewrites a TSV keeping only the rows whose column is in 'garder'.
    Headers are kept. If colonne_sujet is given, the leading '#' is stripped
    there. Returns the number of rows kept."""
    tmp = chemin + ".tmp"
    n = 0
    with io.open(chemin, encoding="utf-8", errors="replace") as src, \
            io.open(tmp, "w", encoding="utf-8") as dst:
        for ligne in src:
            if is_comment(ligne):
                dst.write(ligne)
                continue
            parts = ligne.rstrip("\n").split("\t")
            if len(parts) > colonne and parts[colonne] in garder:
                if colonne_sujet is not None:
                    parts[colonne_sujet] = clean_subject(parts[colonne_sujet])
                    dst.write("\t".join(parts) + "\n")
                else:
                    dst.write(ligne)
                n += 1
    os.replace(tmp, chemin)
    return n


def process_category(outdir, cat):
    base = os.path.join(outdir, cat)
    chemins = {e: base + "." + e for e in ("kg", "link", "text", "image")}
    rapport_p = base + ".report.json"
    for p in list(chemins.values()) + [rapport_p]:
        if not os.path.exists(p):
            log("%s: %s missing, skipped" % (cat, os.path.basename(p)))
            return False

    with io.open(rapport_p, encoding="utf-8") as fh:
        rapport = json.load(fh)
    if rapport.get("trimodal_filter"):
        log("%s: already finalised (%d products), skipped"
            % (cat, rapport["trimodal_filter"]["after"]))
        return True

        log("%s: reading the identifiers" % cat)
    sujet_vers_pid = read_link(chemins["link"])
    avec_kg = set(sujet_vers_pid.values())
    avec_texte = read_pids_column(chemins["text"], 0)
    avec_image = read_pids_column(chemins["image"], 0)
    retenus = avec_kg & avec_texte & avec_image
    log("%s: kg %s | text %s | image %s | tri-modal %s (%.1f%%)"
        % (cat, format(len(avec_kg), ","), format(len(avec_texte), ","),
           format(len(avec_image), ","), format(len(retenus), ","),
           100.0 * len(retenus) / max(1, len(avec_kg))))

    # --- .kg: filter + separation of the signals, in one pass -------------
    log("%s: rewriting the .kg" % cat)
    tmp = chemins["kg"] + ".tmp"
    rel_triples = Counter()
    n_signal = 0
    n_taxo = 0
    n_diese = 0
    dans_taxo = False
    with io.open(chemins["kg"], encoding="utf-8", errors="replace") as src, \
            io.open(tmp, "w", encoding="utf-8") as dst, \
            io.open(base + ".signal", "w", encoding="utf-8") as sig:
        sig.write(ENTETE_SIGNAL)
        for ligne in src:
            if is_comment(ligne):
                dst.write(ligne)
                if ligne.startswith(SEPARATEUR_TAXO):
                    dans_taxo = True
                continue
            parts = ligne.rstrip("\n").split("\t")
            if len(parts) != 3:
                continue
            sujet, rel, obj = parts
            if sujet.startswith("#"):
                n_diese += 1
                ligne = "%s\t%s\t%s\n" % (clean_subject(sujet), rel, obj)
            if dans_taxo:
                # taxonomy edges: subject = product type, everything is kept
                dst.write(ligne)
                rel_triples[rel] += 1
                n_taxo += 1
                continue
            pid = sujet_vers_pid.get(sujet)
            if pid is None or pid not in retenus:
                continue
            if rel in RELATIONS_SIGNAL:
                sig.write(ligne)
                n_signal += 1
                continue
            dst.write(ligne)
            rel_triples[rel] += 1
    os.replace(tmp, chemins["kg"])

    # --- .link / .text / .image ---------------------------------------------
    log("%s: rewriting .link, .text, .image" % cat)
    n_link = filter_tsv(chemins["link"], retenus, 1, colonne_sujet=0)
    n_text = filter_tsv(chemins["text"], retenus, 0)
    n_img = filter_tsv(chemins["image"], retenus, 0)

    # --- report -------------------------------------------------------------
    n_triples = sum(rel_triples.values())
    avant = rapport.get("products_kept", len(avec_kg))
    rapport["trimodal_filter"] = {
        "before": avant,
        "with_text": len(avec_texte & avec_kg),
        "with_image": len(avec_image & avec_kg),
        "after": len(retenus),
        "retention": round(len(retenus) / max(1, avant), 4),
    }
    rapport["signal"] = {
        "file": os.path.basename(base + ".signal"),
        "relations": list(RELATIONS_SIGNAL),
        "triples": n_signal,
        "note": "derives des agregats d'evaluations d'Amazon ; fuite possible "
                "vers un split de test, exclus du .kg",
    }
    rapport["products_kept"] = len(retenus)
    rapport["triples"] = n_triples
    rapport["taxonomy_edges"] = n_taxo
    rapport["text_lines"] = n_text
    rapport["image_lines"] = n_img
    rapport["avg_triples_per_product"] = round(
        (n_triples - n_taxo) / max(1, len(retenus)), 2)
    for rel, stats in rapport.get("relations", {}).items():
        if isinstance(stats, dict):
            stats["triples"] = rel_triples.get(rel, 0)
    for rel in RELATIONS_SIGNAL:
        rapport["relations"].pop(rel, None)
    rapport["subjects_leading_hash_stripped"] = n_diese
    rapport["finalised"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with io.open(rapport_p, "w", encoding="utf-8") as fh:
        json.dump(rapport, fh, ensure_ascii=False, indent=1)

    log("%s: %s tri-modal products, %s triples, %s signals set apart"
        % (cat, format(len(retenus), ","), format(n_triples, ","),
           format(n_signal, ",")))
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--category", action="append", default=[])
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args(argv)

    cats = list(args.category)
    if args.all:
        cats = sorted(f[:-len(".report.json")] for f in os.listdir(args.out)
                      if f.endswith(".report.json"))
    if not cats:
        sys.exit("no category: --category X or --all")
    ok = 0
    for cat in cats:
        if process_category(args.out, cat):
            ok += 1
    log("%d/%d categories finalised" % (ok, len(cats)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
