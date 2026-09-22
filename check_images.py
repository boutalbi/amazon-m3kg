#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Checks the URLs of the IMAGE modality.

Two levels: the form of every URL offline (a valid Amazon image URL carries an
image identifier, not an ASIN, which would give a 400), and a HEAD request on
a random sample with the distribution of HTTP codes.

    python check_images.py -o out/                     # form only
    python check_images.py -o out/ --network --n 200   # + HTTP check
"""

import argparse
import gzip
import json
import os
import random
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

__version__ = "1.0"

# Amazon serves its images from these hosts.
HOTES_VALIDES = ("m.media-amazon.com", "images-na.ssl-images-amazon.com",
                 "images-eu.ssl-images-amazon.com", "images-fe.ssl-images-amazon.com")

# Three valid forms, all verified to answer 200: catalogue images/I/<id>,
# Prime Video images/S/pv-target-images/, SG catalogue images/S/sgp-catalog-
# images/. The transformation block also accepts the composite '_CLa%7C...'.
_URL_IMAGE = re.compile(
    r"^https://(?P<hote>[a-z0-9.-]+)/images/[A-Z]/"
    r"(?P<dossier>(?:[A-Za-z0-9_-]+/)*)"
    r"(?P<id>[A-Za-z0-9@+_-]{5,})"
    r"(?:\._(?P<transf>[A-Za-z0-9,_%.+()-]+)_?)?"
    r"\.(?P<ext>jpg|jpeg|png|gif|webp)$",
    re.IGNORECASE)

# An ASIN: 10 characters, starts with B0 (products) or is a numeric ISBN-10.
_ASIN = re.compile(r"^(B0[A-Z0-9]{8}|\d{9}[\dX])$", re.IGNORECASE)

AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def open_maybe_gz(chemin):
    if chemin.endswith(".gz"):
        return gzip.open(chemin, "rt", encoding="utf-8", errors="replace")
    return open(chemin, "r", encoding="utf-8", errors="replace")


def resolve_path(outdir, categorie, extension):
    for suffixe in ("", ".gz"):
        chemin = os.path.join(outdir, categorie + extension + suffixe)
        if os.path.exists(chemin):
            return chemin
    return None


# ---------------------------------------------------------- 1. form check

def diagnose_url(url):
    """Returns None if the URL is well formed, otherwise the reason for rejection."""
    if not url.startswith("http"):
        return "pas one_image URL"
    if url.startswith("http://"):
        return "http non securise"
    m = _URL_IMAGE.match(url)
    if not m:
        return "forme inattendue"
    if m.group("hote") not in HOTES_VALIDES:
        return "hote inconnu (%s)" % m.group("hote")
    if _ASIN.match(m.group("id")):
        # exact cause of the 400 Bad Request: an ASIN is not an image identifier
        return "ASIN a la place de l'identifiant d'image"
    return None


def check_form(chemin):
    total = 0
    motifs = Counter()
    variantes = Counter()
    exemples = {}
    produits = set()
    urls = []
    with open_maybe_gz(chemin) as fh:
        for ligne in fh:
            if ligne.startswith("#"):
                continue
            parts = ligne.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            pid, variante, url = parts[0], parts[1], parts[2]
            total += 1
            produits.add(pid)
            variantes[variante] += 1
            motif = diagnose_url(url)
            if motif:
                motifs[motif] += 1
                exemples.setdefault(motif, url)
            urls.append(url)
    return {"lignes": total, "produits": len(produits), "variantes": dict(variantes),
            "motifs": dict(motifs), "exemples": exemples, "urls": urls}


# ------------------------------------------------------- 2. network check

def test_url(url, timeout=12):
    """Returns (code, content type). A HEAD request downloads nothing."""
    requete = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": AGENT})
    try:
        with urllib.request.urlopen(requete, timeout=timeout) as reponse:
            return reponse.status, reponse.headers.get("Content-Type", "")
    except urllib.error.HTTPError as err:
        return err.code, ""
    except Exception as err:                       # DNS, TLS, timeout
        return type(err).__name__, ""


def check_network(urls, n=100, threads=8, graine=13):
    if not urls:
        return {}, []
    tirage = random.Random(graine).sample(urls, min(n, len(urls)))
    with ThreadPoolExecutor(max_workers=threads) as pool:
        resultats = list(pool.map(test_url, tirage))
    codes = Counter(str(code) for code, _ in resultats)
    echecs = [(u, c) for u, (c, _) in zip(tirage, resultats) if c != 200][:6]
    return dict(codes), echecs


# -------------------------------------------------------------------- report

def check_category(outdir, categorie, reseau=False, n=100):
    chemin_img = resolve_path(outdir, categorie, ".image")
    if not chemin_img:
        return None
    res = check_form(chemin_img)
    res["categorie"] = categorie

    chemin_link = resolve_path(outdir, categorie, ".link")
    attendus = 0
    if chemin_link:
        with open_maybe_gz(chemin_link) as fh:
            attendus = sum(1 for l in fh if not l.startswith("#"))
    res["produits_attendus"] = attendus
    res["couverture"] = round(100.0 * res["produits"] / attendus, 1) if attendus else 0.0

    if reseau:
        res["codes"], res["echecs"] = check_network(res["urls"], n=n)
    res.pop("urls", None)
    return res


def display(res):
    lignes = []
    a = lignes.append
    a("=" * 78)
    a("IMAGES  %s" % res["categorie"])
    a("=" * 78)
    a("  %s lignes | %s produits illustres sur %s (%s%%)"
      % (format(res["lignes"], ","), format(res["produits"], ","),
         format(res["produits_attendus"], ","), res["couverture"]))
    a("  variantes : %s" % ", ".join("%s=%s" % (k, format(v, ","))
                                     for k, v in sorted(res["variantes"].items())))
    if res["motifs"]:
        a("  URL MAL FORMEES :")
        for motif, n in sorted(res["motifs"].items(), key=lambda kv: -kv[1]):
            a("    %-44s %s  (%.1f%%)"
              % (motif, format(n, ","), 100.0 * n / max(1, res["lignes"])))
            a("        ex. %s" % res["exemples"][motif][:90])
    else:
        a("  forme : toutes les URL sont conformes")
    if "codes" in res:
        total = sum(res["codes"].values())
        a("  reponses HTTP sur %d URL tirees au hasard :" % total)
        for code, n in sorted(res["codes"].items(), key=lambda kv: -kv[1]):
            etat = "OK" if code == "200" else "ECHEC"
            a("    %-10s %5d  (%.0f%%)  %s" % (code, n, 100.0 * n / total, etat))
        for url, code in res.get("echecs", []):
            a("        %s -> %s" % (url[:80], code))
    return "\n".join(lignes)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Verification des URL d'images")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--category", action="append")
    ap.add_argument("--network", "--reseau", dest="reseau", action="store_true",
                    help="effectue des requetes HEAD sur un echantillon")
    ap.add_argument("--n", type=int, default=100, help="size of the network sample")
    ap.add_argument("--json", help="write the full report")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    categories = args.category or sorted(
        {n[: -len(s)] for n in os.listdir(args.out)
         for s in (".image", ".image.gz") if n.endswith(s)})
    if not categories:
        raise SystemExit("No .image file in %s" % args.out)

    tous = []
    for cat in categories:
        res = check_category(args.out, cat, reseau=args.reseau, n=args.n)
        if res is None:
            continue
        tous.append(res)
        print(display(res))
        print()

    if len(tous) > 1:
        print("=" * 78)
        print("SUMMARY")
        print("  %-32s %10s %10s %9s" % ("CATEGORY", "IMAGES", "MALFORMED", "COV."))
        for r in tous:
            mauvaises = sum(r["motifs"].values())
            print("  %-32s %10s %10s %8s%%"
                  % (r["categorie"], format(r["lignes"], ","),
                     format(mauvaises, ","), r["couverture"]))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(tous, fh, ensure_ascii=False, indent=2)
        print("\nReport: %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
