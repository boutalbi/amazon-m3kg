#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Downloads the images of a category, resumable, failures logged.

The release ships image URLs, not pixels; this script materialises them. A
file already on disk is not downloaded again, every failure is logged in
<folder>/<CAT>.failures.tsv with its code and date (retried on the next run
unless --no-retry), and each file is named <product_id>[_<variant>].<ext> so
it joins the .image and the .link directly.

    python fetch_images.py -o out_final --category All_Beauty --folder images/
"""
import argparse
import io
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

RACINE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, RACINE)
from check_images import AGENT, open_maybe_gz, resolve_path  # noqa: E402


def read_urls(chemin, toutes):
    for l in open_maybe_gz(chemin):
        if l.startswith("#"):
            continue
        p = l.rstrip("\n").split("\t")
        if len(p) < 3 or (not toutes and p[1] != "MAIN"):
            continue
        yield p[0], p[1], p[2]


def file_name(pid, variante, url):
    ext = url.rsplit(".", 1)[-1].lower()
    if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
        ext = "jpg"
    return "%s.%s" % (pid if variante == "MAIN" else "%s_%s" % (pid, variante), ext)


def download(url, dest, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            donnees = r.read()
        if not donnees:
            return "vide"
        tmp = dest + ".part"
        with open(tmp, "wb") as fh:
            fh.write(donnees)
        os.replace(tmp, dest)
        return None
    except urllib.error.HTTPError as e:
        return str(e.code)
    except Exception as e:
        return type(e).__name__


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--category", required=True)
    ap.add_argument("--folder", "--dossier", dest="dossier", required=True, help="root folder; one sub-folder per category")
    ap.add_argument("--variants", "--variantes", dest="variantes", choices=["main", "all", "toutes"], default="main")
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--pause", type=float, default=0.0, help="seconds between two requests of the same thread")
    ap.add_argument("--no-retry", "--sans-retenter", dest="sans_retenter", action="store_true", help="do not retry the logged failures")
    ap.add_argument("--limit", "--limite", dest="limite", type=int, default=0, help="for testing: stop after N images")
    args = ap.parse_args(argv)

    chemin = resolve_path(args.out, args.category, ".image")
    if not chemin:
        sys.exit("no .image for %s" % args.category)
    dossier = os.path.join(args.dossier, args.category)
    os.makedirs(dossier, exist_ok=True)
    journal = os.path.join(args.dossier, args.category + ".failures.tsv")

    anciens_echecs = set()
    if os.path.exists(journal):
        for l in io.open(journal, encoding="utf-8"):
            if not l.startswith("#"):
                anciens_echecs.add(l.split("\t")[0])

    taches = []
    deja = 0
    for pid, variante, url in read_urls(chemin, args.variantes in ("all", "toutes")):
        dest = os.path.join(dossier, file_name(pid, variante, url))
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            deja += 1
            continue
        if args.sans_retenter and dest in anciens_echecs:
            continue
        taches.append((url, dest))
        if args.limite and len(taches) >= args.limite:
            break
    print("%s: %s already present, %s to download" % (args.category, format(deja, ","), format(len(taches), ",")), flush=True)

    ok = echecs = 0
    t0 = time.time()
    nouveau = not os.path.exists(journal)
    def one_image(t):
        r = download(t[0], t[1])
        if args.pause:
            time.sleep(args.pause)
        return t, r

    with io.open(journal, "a", encoding="utf-8") as jf, ThreadPoolExecutor(max_workers=args.threads) as pool:
        if nouveau:
            jf.write("# fichier\turl\tmotif\tdate\n")
        futures = [pool.submit(one_image, t) for t in taches]
        for i, f in enumerate(as_completed(futures), 1):
            (url, dest), motif = f.result()
            if motif is None:
                ok += 1
            else:
                echecs += 1
                jf.write("%s\t%s\t%s\t%s\n" % (dest, url, motif, time.strftime("%Y-%m-%d")))
                jf.flush()
            if i % 500 == 0 or i == len(taches):
                v = i / max(1e-9, time.time() - t0)
                print("  %s/%s  ok %s  failures %s  %.0f img/s  ~%.0f min left"
                      % (format(i, ","), format(len(taches), ","), format(ok, ","),
                         format(echecs, ","), v, (len(taches) - i) / max(v, 1e-9) / 60), flush=True)
    print("done: %s downloaded, %s failures (log %s)" % (format(ok, ","), format(echecs, ","), journal))
    return 0


if __name__ == "__main__":
    sys.exit(main())
