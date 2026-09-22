#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Archives each category of out_final/ into a zip, verifies it, then deletes
the uncompressed files; finally groups the zips into a single archive.

Per category <CAT>:
  out_final/<CAT>.{kg,link,text,image,signal,report.json,valuemap.json,nt.gz}
  out_final/recbole/<CAT>/*        (.inter, splits, .kg, .link, .recbole.json)
  -> archives/<CAT>.zip  (deflate level 6, zip64)

The small summary files (<CAT>.report.json, <CAT>.recbole.json) are also
copied into out_final/reports/, where they stay readable without
decompressing anything. Deletion happens category by category, right after
its zip is verified, starting with the smallest ones.

    python archive_categories.py --step categories   # zips + check + deletion
    python archive_categories.py --step global       # archives/Amazon-M3KG.zip (stored, no recompression)
"""
import argparse
import io
import json
import os
import shutil
import sys
import time
import zipfile

RACINE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(RACINE, "out_final")
ARCH = os.path.join(RACINE, "archives")
RAPPORTS = os.path.join(OUT, "reports")
EXT = (".kg", ".link", ".text", ".image", ".signal", ".report.json", ".valuemap.json", ".nt.gz")


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def files_of(cat):
    """[(absolute path, name inside the zip)]"""
    out = []
    for e in EXT:
        p = os.path.join(OUT, cat + e)
        if os.path.exists(p):
            out.append((p, cat + e))
    rb = os.path.join(OUT, "recbole", cat)
    if os.path.isdir(rb):
        for f in sorted(os.listdir(rb)):
            out.append((os.path.join(rb, f), "recbole/" + f))
    return out


def archive_category(cat):
    os.makedirs(ARCH, exist_ok=True)
    os.makedirs(RAPPORTS, exist_ok=True)
    zp = os.path.join(ARCH, cat + ".zip")
    fichiers = files_of(cat)
    total = sum(os.path.getsize(p) for p, _ in fichiers)
    if os.path.exists(zp) and os.path.exists(zp + ".ok"):
        log("%s: zip already verified" % cat)
        return zp
    t0 = time.time()
    log("%s: %d files, %.1f GB -> %s" % (cat, len(fichiers), total / 1e9, os.path.basename(zp)))
    with zipfile.ZipFile(zp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
        for p, nom in fichiers:
            # .nt.gz is already compressed: stored as is
            z.write(p, nom, compress_type=zipfile.ZIP_STORED if p.endswith(".gz") else zipfile.ZIP_DEFLATED)
    # --- verification ---
    with zipfile.ZipFile(zp) as z:
        mauvais = z.testzip()
        if mauvais is not None:
            raise SystemExit("%s: invalid CRC on %s, nothing is deleted" % (cat, mauvais))
        tailles = {i.filename: i.file_size for i in z.infolist()}
    for p, nom in fichiers:
        if tailles.get(nom) != os.path.getsize(p):
            raise SystemExit("%s: different size for %s, nothing is deleted" % (cat, nom))
    # copies of the summaries outside the zip
    for p, nom in fichiers:
        if nom.endswith(".report.json") or nom.endswith(".recbole.json"):
            shutil.copyfile(p, os.path.join(RAPPORTS, os.path.basename(p)))
    io.open(zp + ".ok", "w").write(json.dumps({"fichiers": len(fichiers), "octets": total,
                                              "zip": os.path.getsize(zp), "date": time.strftime("%Y-%m-%d %H:%M")}))
    log("%s: verified, %.1f GB -> %.1f GB (%.0f %%) in %.0f min"
        % (cat, total / 1e9, os.path.getsize(zp) / 1e9, 100 * os.path.getsize(zp) / total, (time.time() - t0) / 60))
    return zp


def delete_sources(cat):
    n = 0
    for p, _ in files_of(cat):
        os.remove(p)
        n += 1
    rb = os.path.join(OUT, "recbole", cat)
    if os.path.isdir(rb) and not os.listdir(rb):
        os.rmdir(rb)
    log("%s: %d uncompressed files deleted" % (cat, n))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--step", "--etape", dest="etape", choices=["categories", "global"], default="categories")
    ap.add_argument("--category", action="append", default=[])
    ap.add_argument("--keep-source", "--sans-delete_sources", dest="sans_effacer", action="store_true")
    args = ap.parse_args(argv)

    if args.etape == "categories":
        cats = args.category or sorted(
            (f[:-len(".report.json")] for f in os.listdir(OUT) if f.endswith(".report.json")),
            key=lambda c: sum(os.path.getsize(p) for p, _ in files_of(c)))
        for c in cats:
            if not files_of(c):
                log("%s: nothing to archive (already done?)" % c)
                continue
            libre = shutil.disk_usage(RACINE).free
            besoin = 0.45 * sum(os.path.getsize(p) for p, _ in files_of(c))
            if libre < besoin + 2e9:
                raise SystemExit("%s: %.1f GB free, ~%.1f needed for the zip, stopping" % (c, libre / 1e9, besoin / 1e9))
            archive_category(c)
            if not args.sans_effacer:
                delete_sources(c)
        log("categories done; %.1f GB free" % (shutil.disk_usage(RACINE).free / 1e9))
    else:
        zips = sorted(f for f in os.listdir(ARCH) if f.endswith(".zip") and f != "Amazon-M3KG.zip")
        manquants = [z for z in zips if not os.path.exists(os.path.join(ARCH, z + ".ok"))]
        if manquants:
            raise SystemExit("zips not verified: %s" % manquants)
        gz = os.path.join(ARCH, "Amazon-M3KG.zip")
        deja = set()
        if os.path.exists(gz):
            with zipfile.ZipFile(gz) as z:
                deja = set(z.namelist())
        zips = [z for z in zips if z not in deja]
        total = sum(os.path.getsize(os.path.join(ARCH, z)) for z in zips)
        if shutil.disk_usage(RACINE).free < total + 2e9:
            raise SystemExit("%.1f GB free are needed for the global archive" % (total / 1e9))
        log("global archive: +%d zips, %.1f GB (stored without recompression; %d already present)"
            % (len(zips), total / 1e9, len(deja)))
        # mode 'a': zips already archived are not copied again
        with zipfile.ZipFile(gz, "a", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
            for f in zips:
                z.write(os.path.join(ARCH, f), f)
        with zipfile.ZipFile(gz) as z:
            mauvais = z.testzip()
            tailles = {i.filename: i.file_size for i in z.infolist()}
        if mauvais is not None or any(tailles.get(f) != os.path.getsize(os.path.join(ARCH, f)) for f in zips):
            raise SystemExit("invalid global archive")
        log("global archive verified: %s (%.1f GB, %d members)" % (gz, os.path.getsize(gz) / 1e9, len(tailles)))
        for f in zips:
            os.remove(os.path.join(ARCH, f))
            os.remove(os.path.join(ARCH, f + ".ok"))
        log("%d individual zips deleted (held in the global archive)" % len(zips))
    return 0


if __name__ == "__main__":
    sys.exit(main())
