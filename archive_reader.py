#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Opens a file of a category, whether it sits in the output folder or in a
global zip archive (one <CAT>.zip per category, stored without recompression).

    from archive_reader import set_output_dir, open_file, available_categories
    set_output_dir("out")                          # default: ./out_final
    with open_file("Books", ".kg") as fh:          # text, utf-8
        for line in fh: ...
    with open_file("Books", "recbole/Books.inter") as fh: ...

The output folder is read first; the archive, if present, is the fallback.
Its outer members are stored uncompressed (ZIP_STORED), so an inner ZipFile
can be opened on them without extracting anything; reading stays streamed.

The folder can also be set with the M3KG_OUT environment variable, and the
archive with M3KG_ARCHIVE.
"""
import io
import os
import zipfile

RACINE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("M3KG_OUT") or os.path.join(RACINE, "out_final")
ARCHIVE = os.environ.get("M3KG_ARCHIVE") or os.path.join(RACINE, "archives", "Amazon-M3KG.zip")
_EXTERNE = None


def set_output_dir(dossier, archive=None):
    """Points the reader at another output folder (the -o of the scripts)."""
    global OUT, ARCHIVE, _EXTERNE
    if dossier:
        OUT = dossier
    if archive:
        ARCHIVE = archive
    _EXTERNE = None


def _archive():
    global _EXTERNE
    if _EXTERNE is None and os.path.exists(ARCHIVE):
        _EXTERNE = zipfile.ZipFile(ARCHIVE)
    return _EXTERNE


def available_categories():
    """Categories present in the output folder (report) or in the archive."""
    cats = set()
    if os.path.isdir(OUT):
        cats = {f[:-len(".report.json")] for f in os.listdir(OUT) if f.endswith(".report.json")}
    z = _archive()
    if z is not None:
        cats |= {n[:-4] for n in z.namelist() if n.endswith(".zip")}
    return sorted(cats)


def open_file(cat, membre, encoding="utf-8"):
    """membre: '.kg', '.link', '.image', ... (suffix) or 'recbole/<file>'.
    Returns a text file object; None if not found."""
    if membre.startswith("recbole/"):
        local = os.path.join(OUT, "recbole", cat, membre[len("recbole/"):])
        nom_zip = membre
    else:
        local = os.path.join(OUT, cat + membre)
        nom_zip = cat + membre
    if os.path.exists(local):
        return io.open(local, encoding=encoding, errors="replace")
    z = _archive()
    if z is None or cat + ".zip" not in z.namelist():
        return None
    interne = zipfile.ZipFile(z.open(cat + ".zip"))
    if nom_zip not in interne.namelist():
        return None
    return io.TextIOWrapper(interne.open(nom_zip), encoding=encoding, errors="replace")


if __name__ == "__main__":
    import sys
    cats = available_categories()
    print(len(cats), "categories:", ", ".join(cats))
    c = sys.argv[1] if len(sys.argv) > 1 else cats[0]
    fh = open_file(c, ".kg")
    n = sum(1 for _ in fh) if fh else -1
    print(c, ".kg:", n, "rows")
