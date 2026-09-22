#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Exports a category to N-Triples (RDF), alongside the TSV.

Namespaces:
  product    <https://m3kg.example/product/{parent_asin}>
  entity     <https://m3kg.example/entity/{relation}/{encoded value}>
  relation   <https://m3kg.example/relation/{name}>
  type       <https://m3kg.example/type/{encoded value}>   (taxonomy edges)

An entity is identified by (relation, value), not by the value alone: 'Silver'
under hasColor and under hasMaterial are two nodes. The display name is
carried by rdfs:label, the text by schema:description, the image by
schema:image, and the schema.org alignment by owl:equivalentProperty.

    python export_ntriples.py -o out_final --category All_Beauty
    -> out_final/All_Beauty.nt.gz
"""
import argparse
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.parse

RACINE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(RACINE, "paper"))

BASE = "https://m3kg.example/"
RDFS_LABEL = "<http://www.w3.org/2000/01/rdf-schema#label>"
RDF_TYPE = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
OWL_EQ = "<http://www.w3.org/2002/07/owl#equivalentProperty>"
SCHEMA = "https://schema.org/"
TAXO = "# taxonomie"

try:
    from relation_vocabulary import SCHEMA_ORG
except Exception:                       # the script also runs standalone
    SCHEMA_ORG = {}


def iri(*morceaux):
    return "<" + BASE + "/".join(urllib.parse.quote(str(m), safe="") for m in morceaux) + ">"


def literal(s):
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")
    return '"%s"' % s


def is_comment(l):
    return l.startswith("# ")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--category", required=True)
    args = ap.parse_args(argv)
    base = os.path.join(args.out, args.category)
    t0 = time.time()

    sujet_vers_pid, noms = {}, {}
    with io.open(base + ".link", encoding="utf-8", errors="replace") as fh:
        for l in fh:
            if is_comment(l):
                continue
            p = l.rstrip("\n").split("\t")
            if len(p) >= 4:
                sujet_vers_pid[p[0]] = p[1]
                noms[p[1]] = p[3]

    n = 0
    relations_vues = set()
    with gzip.open(base + ".nt.gz", "wt", encoding="utf-8") as out:
        # products: type, label
        for pid, nom in noms.items():
            out.write("%s %s %s .\n" % (iri("product", pid), RDF_TYPE, iri("class", "Product")))
            out.write("%s %s %s .\n" % (iri("product", pid), RDFS_LABEL, literal(nom)))
            n += 2
        # text and image
        for ext, prop in ((".text", SCHEMA + "description"), (".image", SCHEMA + "image")):
            with io.open(base + ext, encoding="utf-8", errors="replace") as fh:
                for l in fh:
                    if is_comment(l):
                        continue
                    p = l.rstrip("\n").split("\t")
                    if len(p) < 2 or p[0] not in noms:
                        continue
                    obj = ("<%s>" % p[2]) if ext == ".image" and len(p) >= 3 else literal(p[-1])
                    out.write("%s <%s> %s .\n" % (iri("product", p[0]), prop, obj))
                    n += 1
        # graph
        dans_taxo = False
        with io.open(base + ".kg", encoding="utf-8", errors="replace") as fh:
            for l in fh:
                if is_comment(l):
                    if l.startswith(TAXO):
                        dans_taxo = True
                    continue
                p = l.rstrip("\n").split("\t")
                if len(p) != 3:
                    continue
                s, r, o = p
                relations_vues.add(r)
                if dans_taxo:
                    out.write("%s %s %s .\n" % (iri("type", s), iri("relation", r), iri("type", o)))
                    n += 1
                    continue
                pid = sujet_vers_pid.get(s)
                if pid is None:
                    continue
                ent = iri("entity", r, o)
                out.write("%s %s %s .\n" % (iri("product", pid), iri("relation", r), ent))
                n += 1
        # entity labels (once each) and schema.org alignment
        vues = set()
        dans_taxo = False
        with io.open(base + ".kg", encoding="utf-8", errors="replace") as fh:
            for l in fh:
                if is_comment(l):
                    if l.startswith(TAXO):
                        dans_taxo = True
                    continue
                p = l.rstrip("\n").split("\t")
                if len(p) != 3 or dans_taxo:
                    continue
                cle = (p[1], p[2])
                if cle in vues:
                    continue
                vues.add(cle)
                out.write("%s %s %s .\n" % (iri("entity", p[1], p[2]), RDFS_LABEL, literal(p[2])))
                n += 1
        for r in sorted(relations_vues):
            if r in SCHEMA_ORG:
                out.write("%s %s <%s> .\n" % (iri("relation", r), OWL_EQ,
                                              SCHEMA + SCHEMA_ORG[r].split(":", 1)[1]))
                n += 1
    print("%s: %s RDF triples in %.0f s -> %s.nt.gz"
          % (args.category, format(n, ","), time.time() - t0, args.category))
    return 0


if __name__ == "__main__":
    sys.exit(main())
