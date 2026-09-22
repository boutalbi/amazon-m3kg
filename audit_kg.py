#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Quality audit of the outputs of build_trimodal_kg.py.

Measures what the reduction rate does not say: tri-modal coverage, graph
density, under-normalisation (distinct values that should have merged),
over-normalisation (learned merges joining unrelated forms), pollution
(empty, numeric, HTML or over-long values), and a 0-100 score per relation.

    python audit_kg.py -o out/ --category All_Beauty
    python audit_kg.py -o out/ --all --resume --json audit.json
"""

import argparse
import gzip
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict

__version__ = "1.0"


# ------------------------------------------------------------------- utilities

def open_maybe_gz(chemin):
    """Opens a possibly gzipped file."""
    if chemin.endswith(".gz"):
        return gzip.open(chemin, "rt", encoding="utf-8", errors="replace")
    return open(chemin, "r", encoding="utf-8", errors="replace")


def resolve_path(outdir, categorie, extension):
    """Returns the existing path for <cat><extension>, gzipped or not."""
    for suffixe in ("", ".gz"):
        chemin = os.path.join(outdir, categorie + extension + suffixe)
        if os.path.exists(chemin):
            return chemin
    return None


_PONCT = re.compile(r"[^0-9a-z]+")


def relaxed_key(valeur):
    """Lowercase, without accents, punctuation or spaces."""
    s = unicodedata.normalize("NFKD", valeur.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return _PONCT.sub("", s)


def tokens_key(valeur):
    """Sorted bag of words: detects 'Rowling, J. K.' vs 'J. K. Rowling'.

    ONE-character tokens are kept: they are initials, and dropping them would
    conflate 'C.S. Lewis', 'J. F. Lewis' and 'G.S. Lewis', which are three
    different authors."""
    toks = [t for t in _PONCT.split(_without_accents(valeur)) if t]
    return " ".join(sorted(set(toks)))


def _without_accents(valeur):
    s = unicodedata.normalize("NFKD", valeur.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def tokens(valeur):
    return set(t for t in _PONCT.split(_without_accents(valeur)) if t)


_HTML = re.compile(r"<[^>]+>|&[a-z]+;|&#\d+;")
_NUMERIQUE = re.compile(r"^[\d\s.,:/+-]+$")
# intended numeric band: '0-50', '200+', '150-200', '4.5-5'
_TRANCHE = re.compile(r"^\s*\d[\d.,]*\s*(?:[-a-z]{1,3}\s*\d[\d.,]*|\+)\s*$", re.I)
_BRUIT = {"n/a", "na", "none", "null", "unknown", "-", "--", "other", "others",
          "misc", "miscellaneous", "see description", "not applicable",
          "various", "assorted", "default", "0", "1", "no", "yes"}


def diagnose_value(valeur, relation_numerique=False):
    """
    Returns a pollution pattern, or None if the value looks sound.
    relation_numerique=True: the relation deliberately carries numbers or bands
    (hasPageCount, hasPriceRange...), so numbers are not penalised.
    """
    v = valeur.strip()
    if not v:
        return "vide"
    if v.lower() in _BRUIT and not relation_numerique:
        return "bruit"
    if _HTML.search(v):
        return "html"
    if len(v) > 80:
        return "trop_long"
    if _NUMERIQUE.match(v) and not relation_numerique:
        return "numerique_brut"
    if len(v) <= 1 and not relation_numerique:
        return "trop_court"
    return None


def relation_is_numeric(compteur):
    """True if the relation mostly carries numbers or bands."""
    if not compteur:
        return False
    n = sum(1 for v in compteur
            if _NUMERIQUE.match(v.strip()) or _TRANCHE.match(v.strip()))
    return n >= 0.5 * len(compteur)


# --------------------------------------------------------------- 1. reading

def read_kg(chemin, cap_valeurs=200000):
    """
    Streaming read of the .kg.
    Value counters are capped at cap_valeurs per relation: beyond that the
    relation is marked 'saturee' and new keys are no longer discovered.
    """
    par_relation = defaultdict(Counter)
    satures = set()
    sujets = set()
    n_triplets = 0
    n_malformes = 0
    vus = set()
    n_doublons = 0

    with open_maybe_gz(chemin) as fh:
        for ligne in fh:
            ligne = ligne.rstrip("\n")
            if not ligne or ligne.startswith("#"):
                continue
            parts = ligne.split("\t")
            if len(parts) != 3:
                n_malformes += 1
                continue
            sujet, relation, valeur = parts
            n_triplets += 1
            sujets.add(sujet)
            cle = (sujet, relation, valeur)
            if cle in vus:
                n_doublons += 1
            elif len(vus) < 5000000:
                vus.add(cle)
            compteur = par_relation[relation]
            if relation in satures:
                if valeur in compteur:
                    compteur[valeur] += 1
            else:
                compteur[valeur] += 1
                if len(compteur) > cap_valeurs:
                    satures.add(relation)
    return par_relation, sujets, n_triplets, n_malformes, n_doublons, satures


def read_link(chemin):
    """Returns (subjects, product ids, number of rows marked as homonyms)."""
    ids = set()
    sujets = set()
    homonymes = 0
    with open_maybe_gz(chemin) as fh:
        for ligne in fh:
            if ligne.startswith("#"):
                continue
            parts = ligne.rstrip("\n").split("\t")
            if len(parts) < 2:
                continue
            sujets.add(parts[0])
            ids.add(parts[1])
            if len(parts) > 2 and parts[2].strip():
                try:
                    if int(parts[2]) > 1:
                        homonymes += 1
                except ValueError:
                    pass
    return sujets, ids, homonymes


def read_ids_first_column(chemin):
    ids = set()
    with open_maybe_gz(chemin) as fh:
        for ligne in fh:
            if ligne.startswith("#"):
                continue
            pid = ligne.split("\t", 1)[0].strip()
            if pid:
                ids.add(pid)
    return ids


# ------------------------------------------- 2. under-normalisation detection

def detect_missed_merges(compteur, max_groupes=15):
    """
    Groups the final values by increasingly relaxed keys.
    Any group of size > 1 is a merge that the normalisation missed.
    """
    resultats = {"relaxed_key": [], "sac_de_mots": [], "prefixe": []}

    par_relache = defaultdict(list)
    par_tokens = defaultdict(list)
    for valeur in compteur:
        par_relache[relaxed_key(valeur)].append(valeur)
        par_tokens[tokens_key(valeur)].append(valeur)

    for cle, groupe in par_relache.items():
        if cle and len(groupe) > 1:
            resultats["relaxed_key"].append(sorted(groupe, key=lambda v: -compteur[v]))

    deja = {frozenset(g) for g in resultats["relaxed_key"]}
    for cle, groupe in par_tokens.items():
        if cle and len(groupe) > 1 and frozenset(groupe) not in deja:
            resultats["sac_de_mots"].append(sorted(groupe, key=lambda v: -compteur[v]))

    # same first word: 'Bloomsbury' vs 'Bloomsbury Publishing PLC'
    index = defaultdict(list)
    for valeur in compteur:
        toks = [t for t in _PONCT.split(_without_accents(valeur)) if t]
        if toks and len(toks[0]) > 3:
            index[toks[0]].append(valeur)
    for _, groupe in index.items():
        if len(groupe) > 1 and len({relaxed_key(v) for v in groupe}) > 1:
            resultats["prefixe"].append(sorted(groupe, key=lambda v: -compteur[v]))

    for cle in resultats:
        resultats[cle].sort(key=lambda g: -sum(compteur[v] for v in g))
        resultats[cle] = resultats[cle][:max_groupes]
    return resultats


# -------------------------------------------- 3. over-normalisation detection

def detect_abusive_merges(valuemap, relation, max_cas=15):
    """
    In the learned valuemap, reports the variants merged into a canonical form
    with which they share no word (typically a mistake).

    The valuemap written by build_trimodal_kg.py is nested:
    {relation: {variant: canonical}}.
    """
    table = valuemap.get(relation)
    if not isinstance(table, dict):
        return []
    suspects = []
    for variante, canonique in table.items():
        if not isinstance(canonique, str) or not canonique:
            continue
        if relaxed_key(variante) == relaxed_key(canonique):
            continue          # plain case/punctuation variation: normal
        tv, tc = tokens(variante), tokens(canonique)
        if not tv or not tc:
            continue
        commun = tv & tc
        if not commun:
            suspects.append({"variante": variante, "canonique": canonique,
                             "motif": "aucun mot commun"})
        elif len(commun) == 1 and min(len(tv), len(tc)) >= 2 \
                and len(next(iter(commun))) <= 3:
            suspects.append({"variante": variante, "canonique": canonique,
                             "motif": "recouvrement faible"})
    return suspects[:max_cas]


# --------------------------------------------------------------- 4. scoring

def score_relation(compteur, manquees, abusives, pollution, n_produits):
    """
    Score 0-100. Penalises: excessive singletons, quasi-identifier, missed
    merges, abusive merges, pollution.
    """
    total_occ = sum(compteur.values())
    distinct = len(compteur)
    if not total_occ or not distinct:
        return 0, ["relation vide"]

    motifs = []
    score = 100.0

    singletons = sum(1 for v in compteur.values() if v == 1)
    taux_singleton = singletons / distinct
    if taux_singleton > 0.7:
        score -= 30
        motifs.append("%.0f%% de valeurs uniques" % (100 * taux_singleton))
    elif taux_singleton > 0.5:
        score -= 15
        motifs.append("%.0f%% de singletons" % (100 * taux_singleton))

    # one value per product = attribute that cannot be shared, useless for reco
    if n_produits:
        unicite = distinct / max(1, min(total_occ, n_produits))
        if unicite > 0.8:
            score -= 25
            motifs.append("quasi identifiant (%.0f%%)" % (100 * unicite))

    # Only the first two families count: same relaxed key and same bag of words
    # are evidence. Sharing the first word is not ('John Grisham' and 'John
    # Sandford' are two authors): it is displayed for information without
    # penalising.
    n_manquees = sum(len(g) - 1 for cle in ("relaxed_key", "sac_de_mots")
                     for g in manquees.get(cle, []))
    if n_manquees:
        score -= min(30.0, 300.0 * n_manquees / distinct)
        motifs.append("%d fusions manquees" % n_manquees)

    if abusives:
        score -= min(20.0, 4.0 * len(abusives))
        motifs.append("%d fusions suspectes" % len(abusives))

    if pollution:
        part = sum(pollution.values()) / distinct
        score -= min(25.0, 100.0 * part)
        motifs.append("%.0f%% de valeurs polluees" % (100 * part))

    return max(0, int(round(score))), motifs


# ----------------------------------------------------------------- 5. audit

def audit_category(outdir, categorie, top=12, cap_valeurs=200000):
    chemins = {ext: resolve_path(outdir, categorie, "." + ext)
               for ext in ("kg", "link", "text", "image")}
    if not chemins["kg"]:
        raise SystemExit("Not found: %s" % os.path.join(outdir, categorie + ".kg"))

    par_relation, sujets_kg, n_triplets, n_malformes, n_doublons, satures = \
        read_kg(chemins["kg"], cap_valeurs)

    sujets_link, ids_produits, n_homonymes = set(), set(), 0
    if chemins["link"]:
        sujets_link, ids_produits, n_homonymes = read_link(chemins["link"])
    ids_texte = read_ids_first_column(chemins["text"]) if chemins["text"] else set()
    ids_image = read_ids_first_column(chemins["image"]) if chemins["image"] else set()

    valuemap = {}
    chemin_vm = resolve_path(outdir, categorie, ".valuemap.json")
    if chemin_vm:
        try:
            with open_maybe_gz(chemin_vm) as fh:
                valuemap = json.load(fh)
        except Exception as exc:
            print("  (unreadable valuemap: %s)" % exc, file=sys.stderr)

    rapport_build = {}
    chemin_rep = resolve_path(outdir, categorie, ".report.json")
    if chemin_rep:
        try:
            with open_maybe_gz(chemin_rep) as fh:
                rapport_build = json.load(fh)
        except Exception:
            pass

    n_prod = len(ids_produits) or rapport_build.get("products_kept") or 0
    n_sans_kg = len(sujets_link - sujets_kg) if sujets_link else None

    res = {
        "categorie": categorie,
        "version_audit": __version__,
        "couverture": {
            "produits": n_prod,
            "sujets_kg": len(sujets_kg),
            "produits_avec_texte": len(ids_texte),
            "produits_avec_image": len(ids_image),
            "pct_texte": round(100.0 * len(ids_texte) / n_prod, 1) if n_prod else 0,
            "pct_image": round(100.0 * len(ids_image) / n_prod, 1) if n_prod else 0,
            "pct_trimodal": round(100.0 * len(ids_texte & ids_image) / n_prod, 1) if n_prod else 0,
            "noms_sans_triplet": n_sans_kg,
            "homonymes": n_homonymes,
        },
        "kg": {
            "triplets": n_triplets,
            "relations": len(par_relation),
            "triplets_par_sujet": round(n_triplets / len(sujets_kg), 2) if sujets_kg else 0,
            "lignes_malformees": n_malformes,
            "triplets_dupliques": n_doublons,
        },
        "relations": {},
        "score_global": 0,
    }

    scores = []
    ordre = sorted(par_relation.items(), key=lambda kv: -sum(kv[1].values()))
    for relation, compteur in ordre:
        numerique = relation_is_numeric(compteur)
        pollution = Counter()
        for valeur in compteur:
            motif = diagnose_value(valeur, numerique)
            if motif:
                pollution[motif] += 1
        if relation in satures:
            manquees = {"relaxed_key": [], "sac_de_mots": [], "prefixe": []}
        else:
            manquees = detect_missed_merges(compteur)
        abusives = detect_abusive_merges(valuemap, relation) if valuemap else []
        score, motifs = score_relation(compteur, manquees, abusives, pollution, n_prod)
        scores.append((score, sum(compteur.values())))

        res["relations"][relation] = {
            "triplets": sum(compteur.values()),
            "valeurs_distinctes": len(compteur) if relation not in satures
                                  else ">%d" % cap_valeurs,
            "compteur_sature": relation in satures,
            "taux_singleton": round(
                sum(1 for v in compteur.values() if v == 1) / len(compteur), 3)
                if compteur else 0,
            "top_valeurs": compteur.most_common(top),
            "pollution": dict(pollution),
            "relation_numerique": numerique,
            "exemples_pollution": [v for v in list(compteur)[:5000]
                                   if diagnose_value(v, numerique)][:8],
            "fusions_manquees": manquees,
            "fusions_suspectes": abusives,
            "score": score,
            "motifs": motifs,
        }

    poids = sum(t for _, t in scores) or 1
    res["score_global"] = int(round(sum(s * t for s, t in scores) / poids))
    return res


# --------------------------------------------------------------- 6. display

def display(res, verbeux=True):
    lignes = []
    ajout = lignes.append
    couv, kg = res["couverture"], res["kg"]
    ajout("=" * 84)
    ajout("AUDIT  %s    score global %d/100" % (res["categorie"], res["score_global"]))
    ajout("=" * 84)
    ajout("  produits %s | sujets KG %s | homonymes %s"
          % (format(couv["produits"], ","), format(couv["sujets_kg"], ","),
             format(couv["homonymes"], ",")))
    ajout("  texte %s%%  image %s%%  tri-modal complet %s%%"
          % (couv["pct_texte"], couv["pct_image"], couv["pct_trimodal"]))
    if couv["noms_sans_triplet"]:
        ajout("  ATTENTION : %s noms de produit sans aucun triplet"
              % format(couv["noms_sans_triplet"], ","))
    ajout("  triplets %s sur %d relations (%s / sujet)"
          % (format(kg["triplets"], ","), kg["relations"], kg["triplets_par_sujet"]))
    if kg["lignes_malformees"] or kg["triplets_dupliques"]:
        ajout("  malformees %s | doublons %s"
              % (format(kg["lignes_malformees"], ","), format(kg["triplets_dupliques"], ",")))
    ajout("")
    ajout("  %-24s%11s%10s%8s%7s  %s"
          % ("RELATION", "TRIPLETS", "DISTINCT", "SINGL.", "SCORE", "DIAGNOSTIC"))
    ajout("  " + "-" * 80)
    for relation, r in res["relations"].items():
        ajout("  %-24s%11s%10s%8.0f%%%7d  %s"
              % (relation[:24], format(r["triplets"], ","),
                 str(r["valeurs_distinctes"]), 100 * r["taux_singleton"], r["score"],
                 ("; ".join(r["motifs"])[:34] if r["motifs"] else "ok")))

    if not verbeux:
        return "\n".join(lignes)

    for relation, r in res["relations"].items():
        ajout("")
        ajout("-" * 84)
        ajout("RELATION %s   (%s triplets, %s valeurs, score %d)"
              % (relation, format(r["triplets"], ","),
                 r["valeurs_distinctes"], r["score"]))
        ajout("  valeurs les plus frequentes :")
        for valeur, n in r["top_valeurs"]:
            ajout("      %9s  %s" % (format(n, ","), valeur[:66]))
        detail = (r["fusions_manquees"]["relaxed_key"] or
                  r["fusions_manquees"]["sac_de_mots"] or
                  r["fusions_manquees"]["prefixe"] or
                  r["fusions_suspectes"] or r["exemples_pollution"])
        if not detail:
            ajout("  aucun probleme detecte.")
            continue
        titres = (("FUSIONS MANQUEES (casse / ponctuation)", "relaxed_key"),
                  ("FUSIONS MANQUEES (memes mots, ordre different)", "sac_de_mots"),
                  ("FUSIONS POSSIBLES (meme premier mot)", "prefixe"))
        for titre, cle in titres:
            groupes = r["fusions_manquees"][cle]
            if groupes:
                ajout("  %s :" % titre)
                for groupe in groupes[:8]:
                    ajout("      " + "  |  ".join(x[:34] for x in groupe[:5]))
        if r["fusions_suspectes"]:
            ajout("  FUSIONS SUSPECTES (sur-normalisation possible) :")
            for s in r["fusions_suspectes"][:8]:
                ajout("      %-36s -> %s  [%s]"
                      % (s["variante"][:34], s["canonique"][:30], s["motif"]))
        if r["exemples_pollution"]:
            ajout("  VALEURS POLLUEES (%s) :" % r["pollution"])
            for valeur in r["exemples_pollution"]:
                ajout("      %r" % valeur[:70])
    return "\n".join(lignes)


# -------------------------------------------------------------------- 7. cli

def main(argv=None):
    ap = argparse.ArgumentParser(description="Audit de qualite des sorties tri-modales")
    ap.add_argument("-o", "--out", required=True, help="repertoire de sortie du build")
    ap.add_argument("--category", action="append", help="categorie a audit_category (repetable)")
    ap.add_argument("--all", action="store_true", help="audit_category toutes les categories trouvees")
    ap.add_argument("--top", type=int, default=12, help="nb de valeurs frequentes affichees")
    ap.add_argument("--cap", type=int, default=200000,
                    help="plafond de valeurs comptees par relation")
    ap.add_argument("--json", help="ecrire l'audit complet dans ce fichier JSON")
    ap.add_argument("--resume", action="store_true",
                    help="tableau seul, sans le detail par relation")
    args = ap.parse_args(argv)

    # the Amazon data contain CJK: the Windows console is in cp1252
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    categories = args.category or []
    if args.all or not categories:
        trouvees = set()
        for nom in sorted(os.listdir(args.out)):
            for suffixe in (".kg", ".kg.gz"):
                if nom.endswith(suffixe):
                    trouvees.add(nom[: -len(suffixe)])
        categories = sorted(trouvees)
    if not categories:
        raise SystemExit("No .kg file in %s" % args.out)

    tous = []
    for cat in categories:
        res = audit_category(args.out, cat, top=args.top, cap_valeurs=args.cap)
        tous.append(res)
        print(display(res, verbeux=not args.resume))
        print()

    if len(tous) > 1:
        print("=" * 84)
        print("SUMMARY")
        print("  %-32s%12s%12s%9s%7s"
              % ("CATEGORIE", "PRODUITS", "TRIPLETS", "T/PROD", "SCORE"))
        for r in tous:
            print("  %-32s%12s%12s%9s%7d"
                  % (r["categorie"], format(r["couverture"]["produits"], ","),
                     format(r["kg"]["triplets"], ","), r["kg"]["triplets_par_sujet"],
                     r["score_global"]))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(tous, fh, ensure_ascii=False, indent=2)
        print("\nFull audit: %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
