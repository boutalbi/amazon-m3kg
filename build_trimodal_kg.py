#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_trimodal_kg.py
====================

Builds TRI-MODAL datasets (image / text / knowledge graph) from the Amazon
Reviews 2023 metadata (McAuley-Lab), for any product category.


Outputs per category
--------------------
  <out>/<CAT>.kg              TSV triples: product_name \t relation \t value
  <out>/<CAT>.link            product_name \t product_id \t n_homonyms
  <out>/<CAT>.text            product_id \t text (title + description)
  <out>/<CAT>.image           product_id \t variant \t url
  <out>/<CAT>.valuemap.json   learned canonicalisation of the values (pass 1)
  <out>/<CAT>.report.json     reduction / normalisation statistics
  <out>/<CAT>.discovery.json  (discover mode) candidate attributes + redundancies

Sub-commands
------------
  discover   samples a metadata file, proposes the relevant attributes and
             detects redundant relations (to be validated by a human)
  build      applies the config and generates the dataset files
  validate   checks the consistency of a config without producing any output

Project: tri-modal recommender-system dataset construction.
"""

from __future__ import annotations

import argparse
import functools
import gzip
import html
import io
import json
import math
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None


__version__ = "1.0"


# =============================================================================
# 0. General utilities
# =============================================================================

LOG_LEVEL = 1


def log(msg: str, level: int = 1) -> None:
    if level <= LOG_LEVEL:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def open_any(path: str | Path, mode: str = "rt") -> Any:
    """Opens .jsonl, .jsonl.gz, .jsonl.zst, .tsv, .csv (text, utf-8)."""
    p = str(path)
    if p.endswith(".gz"):
        return gzip.open(p, mode, encoding="utf-8", errors="replace")
    if p.endswith((".zst", ".zstd")):
        try:
            import zstandard as zstd  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise SystemExit(
                "Fichier .zst detecte mais le module 'zstandard' est absent "
                "(pip install zstandard)."
            ) from exc
        fh = open(p, "rb")
        dctx = zstd.ZstdDecompressor()
        return io.TextIOWrapper(dctx.stream_reader(fh), encoding="utf-8", errors="replace")
    return open(p, mode, encoding="utf-8", errors="replace")


def iter_meta_records(paths: Sequence[str], limit: Optional[int] = None) -> Iterator[dict]:
    """Iterates over the product records.

    Supports JSONL (one object per line, the native Amazon Reviews 2023
    format), JSON array, and already flattened TSV/CSV ('details/xxx' columns)."""
    n = 0
    for path in paths:
        p = str(path)
        if p.endswith((".tsv", ".csv")):
            for rec in _iter_tabular(p, limit, n):
                yield rec
                n += 1
                if limit and n >= limit:
                    return
            continue
        with open_any(p) as fh:
            head = fh.read(2048)
            fh.seek(0)
            stripped = head.lstrip()
            if stripped.startswith("["):
                data = json.load(fh)
                for rec in data:
                    yield rec
                    n += 1
                    if limit and n >= limit:
                        return
                continue
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                yield rec
                n += 1
                if limit and n >= limit:
                    return


def _iter_tabular(path: str, limit: Optional[int], start: int) -> Iterator[dict]:
    import csv

    csv.field_size_limit(10 ** 7)
    delim = "\t" if path.endswith(".tsv") else ","
    with open_any(path) as fh:
        reader = csv.DictReader(fh, delimiter=delim)
        for row in reader:
            rec: Dict[str, Any] = {}
            details: Dict[str, Any] = {}
            for k, v in row.items():
                if k is None or v is None or v == "":
                    continue
                if k.startswith("details/"):
                    details[k.split("/", 1)[1]] = v
                else:
                    rec[k] = v
            if details:
                rec["details"] = details
            yield rec


# --- Unicode / text normalisation -------------------------------------------

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]{0,400}>")
_CTRL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MOJIBAKE_RE = re.compile("[Ãã][-¿¡-ÿ]")


def strip_html(s: str) -> str:
    if "<" in s or "&" in s:
        s = _TAG_RE.sub(" ", s)
        s = html.unescape(s)
    return s


def collapse_ws(s: str) -> str:
    return _WS_RE.sub(" ", _CTRL_RE.sub(" ", s)).strip()


def nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s)


def deaccent(s: str) -> str:
    """Removes diacritics (only to build comparison keys)."""
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


# '\W' alone would let the underscore through, which Python counts as a word
# character: 'Air_powered' and 'Air Powered' did not merge, nor 'Not_applicable'
# with the invalid value 'not applicable'.
_PUNCT_RE = re.compile(r"[\W_]", re.UNICODE)


@functools.lru_cache(maxsize=262144)
def match_key(s: str) -> str:
    """Comparison key, insensitive to case / accents / punctuation.
    'J.K. Rowling', 'JK Rowling', 'jk rowling' -> 'jk rowling'.

    Memoised: on real data the same raw value comes back thousands of times and
    this function accounted for 65 % of the build time."""
    s = collapse_ws(deaccent(nfkc(s)).lower())
    s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def tight_key(s: str) -> str:
    """Key without any space: 'jkrowling' == 'J. K. Rowling'."""
    return match_key(s).replace(" ", "")


def looks_mojibake(s: str) -> bool:
    return bool(_MOJIBAKE_RE.search(s))


def fix_mojibake(s: str) -> str:
    """Repairs the common double encodings (é, å, ...) when it is safe."""
    if not looks_mojibake(s):
        return s
    try:
        return s.encode("cp1252", errors="strict").decode("utf-8", errors="strict")
    except Exception:
        return s


def tsv_safe(s: str) -> str:
    return s.replace("\t", " ").replace("\r", " ").replace("\n", " ")


# =============================================================================
# 1. Normalisation of person names
# =============================================================================

MC_PREFIX_RE = re.compile(r"^(mc)([a-z])(.*)$", re.IGNORECASE)
MAC_PREFIX_RE = re.compile(r"^(mac)([a-z])(.*)$", re.IGNORECASE)
APOS_PREFIX_RE = re.compile(r"^([odl])['’]([a-z])(.*)$", re.IGNORECASE)

DEFAULT_MC_EXCEPTIONS = {
    "mackie", "macy", "mack", "machine", "macho", "macaulay", "mace", "mac",
    "macedo", "maceo", "machado", "macias", "macarthur", "maceachern", "macgyver",
}

SUFFIX_TOKENS = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "esq"}
SUFFIX_DISPLAY = {"jr": "Jr.", "sr": "Sr.", "ii": "II", "iii": "III", "iv": "IV"}
NAME_PARTICLES = {"de", "del", "della", "der", "van", "von", "da", "di", "du",
                  "la", "le", "los", "las", "bin", "ibn", "af", "av", "ter", "ten"}


def _cap_word(w: str, mc_exceptions: Set[str]) -> str:
    if not w:
        return w
    low = w.lower()
    m = APOS_PREFIX_RE.match(w)
    if m and len(w) > 2:
        return m.group(1).upper() + "'" + m.group(2).upper() + m.group(3).lower()
    if low not in mc_exceptions:
        m = MC_PREFIX_RE.match(w)
        if m and len(w) > 3:
            return "Mc" + m.group(2).upper() + m.group(3).lower()
        m = MAC_PREFIX_RE.match(w)
        if m and len(w) > 5:
            return "Mac" + m.group(2).upper() + m.group(3).lower()
    return (w[0].upper() + w[1:].lower()) if w[0].isalpha() else w


def titlecase_person(name: str, mc_exceptions: Set[str]) -> str:
    """Robust capitalisation of a person name (Mc/Mac/O', initials, particles)."""
    out: List[str] = []
    tokens = [t for t in name.split(" ") if t]
    for i, tok in enumerate(tokens):
        low = tok.lower().rstrip(".")
        if low in SUFFIX_DISPLAY and i > 0:
            out.append(SUFFIX_DISPLAY[low])
            continue
        if tok.lower() in NAME_PARTICLES and 0 < i < len(tokens) - 1:
            out.append(tok.lower())
            continue
        if ("." in tok or len(tok) == 1) and re.fullmatch(r"(?:[a-z]\.?){1,3}", tok.lower()):
            letters = tok.replace(".", "").upper()
            out.append(".".join(letters) + ".")
            continue
        out.append("-".join(_cap_word(w, mc_exceptions) for w in tok.split("-")))
    return " ".join(out)


class UnionFind:
    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _accent_score(s: str) -> int:
    """Prefers forms with valid diacritics, penalises mojibake."""
    if looks_mojibake(s):
        return -1
    return sum(1 for c in s if ord(c) > 127)


def build_person_clusters(mappings: Dict[str, str]) -> Dict[str, str]:
    """Turns a possibly CYCLIC mapping dictionary (e.g. 'Chloe Sevigny' ->
    'Chloé Sévigny' AND the reverse) into clusters, then elects a canonical
    representative per cluster.

    Election: accented non-mojibake form > longest form > alphabetical order.
    Avoids the infinite loops of hand-written mapping tables.
    """
    uf = UnionFind()
    pairs = []
    indegree: Counter = Counter()
    for a, b in (mappings or {}).items():
        a, b = collapse_ws(a), collapse_ws(b)
        if a and b:
            pairs.append((a, b))
            indegree[b] += 1          # b is a TARGET written by a human
            uf.union(a, b)
    members: Dict[str, Set[str]] = defaultdict(set)
    for a, b in pairs:
        members[uf.find(a)].add(a)
        members[uf.find(a)].add(b)
    canon: Dict[str, str] = {}
    for _root, group in members.items():
        # priority: valid accented form > most designated target > short form
        best = max(group, key=lambda s: (_accent_score(s), indegree[s], -len(s), s))
        for m in group:
            if m != best:
                canon[m] = best
    return canon


@dataclass
class PersonNormalizer:
    explicit: Dict[str, str] = field(default_factory=dict)   # key = match_key
    invalid_values: Set[str] = field(default_factory=set)    # key = match_key
    invalid_patterns: List[str] = field(default_factory=list)
    mc_exceptions: Set[str] = field(default_factory=lambda: set(DEFAULT_MC_EXCEPTIONS))
    min_tokens: int = 2
    max_len: int = 80

    @classmethod
    def from_rules(cls, rules: dict) -> "PersonNormalizer":
        clusters = build_person_clusters(rules.get("person_mappings", {}) or {})
        return cls(
            explicit={match_key(k): v for k, v in clusters.items()},
            invalid_values={match_key(v) for v in rules.get("person_invalid_values", []) or []},
            invalid_patterns=[p.lower() for p in rules.get("person_invalid_patterns", []) or []],
            mc_exceptions=set(rules.get("person_mc_mac_exceptions", []) or []) | DEFAULT_MC_EXCEPTIONS,
        )

    def __call__(self, raw: str) -> Optional[str]:
        s = collapse_ws(strip_html(fix_mojibake(nfkc(raw))))
        s = s.strip(" .,;:-*•")
        if not s:
            return None
        low = s.lower()
        if match_key(s) in self.invalid_values:
            return None
        if any(pat in low for pat in self.invalid_patterns):
            return None
        if re.fullmatch(r"[\W_]+", s):
            return None
        if low.rstrip(".") in SUFFIX_TOKENS:
            return None
        toks = [t for t in s.split(" ") if t]
        if len(toks) < self.min_tokens:
            return None
        if len(s) > self.max_len:
            return None
        s = titlecase_person(s, self.mc_exceptions)
        return self.explicit.get(match_key(s), s)


# =============================================================================
# 2. Parsers for numeric / categorical values
# =============================================================================

_PAGES_RE = re.compile(r"(\d[\d,.\s]{0,9})\s*(?:pages?|p\.?|pp\.?)\b", re.IGNORECASE)
_NUM_RE = re.compile(r"(\d[\d,.\s]*)")


def parse_pages(raw: str) -> Optional[int]:
    s = collapse_ws(nfkc(raw))
    m = _PAGES_RE.search(s) or _NUM_RE.search(s)
    if not m:
        return None
    try:
        n = int(re.sub(r"[^\d]", "", m.group(1)))
    except ValueError:
        return None
    return n if 0 < n < 30000 else None


_HMS_RE = re.compile(
    r"(?:(?P<h>\d+)\s*(?:h|hr|hrs|hour|hours|heures?)\b)?\s*(?:and)?\s*"
    r"(?:(?P<m>\d+)\s*(?:m|min|mins|minutes?)\b)?",
    re.IGNORECASE,
)
_ISO_RE = re.compile(r"^P?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", re.IGNORECASE)
_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def parse_runtime_minutes(raw: str) -> Optional[int]:
    s = collapse_ws(nfkc(raw))
    if not s:
        return None
    m = _CLOCK_RE.match(s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = _ISO_RE.match(s.replace(" ", ""))
    if m and any(m.groups()):
        return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    m = _HMS_RE.search(s)
    if m and (m.group("h") or m.group("m")):
        total = int(m.group("h") or 0) * 60 + int(m.group("m") or 0)
        if 0 < total < 6000:
            return total
    m = _NUM_RE.search(s)
    if m:
        try:
            n = int(re.sub(r"[^\d]", "", m.group(1)))
        except ValueError:
            return None
        if 0 < n < 6000:
            return n
    return None


def parse_number(raw: str) -> Optional[float]:
    s = collapse_ws(nfkc(raw)).replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def bin_value(n: float, bins: Sequence[Sequence[Any]]) -> Optional[str]:
    """bins = [[label, lo, hi], ...]; bounds included; null = infinity."""
    for spec in bins:
        label, lo, hi = spec[0], spec[1], spec[2]
        lo_f = -math.inf if lo is None else float(lo)
        hi_f = math.inf if hi is None else float(hi)
        if lo_f <= n <= hi_f:
            return str(label)
    return None


_LANG_AUDIO_RE = re.compile(r"^(?P<lang>[^(\[]+)[(\[](?P<audio>[^)\]]+)[)\]]\s*$")


def split_language_audio(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """'English (Dolby Digital 5.1)' -> ('English', 'Dolby Digital 5.1')."""
    s = collapse_ws(nfkc(raw))
    m = _LANG_AUDIO_RE.match(s)
    if m:
        return collapse_ws(m.group("lang")) or None, collapse_ws(m.group("audio")) or None
    return (s or None), None


# =============================================================================
# 3. Flattening of the Amazon records
# =============================================================================

def flatten_record(rec: dict, max_depth: int = 2) -> Dict[str, List[str]]:
    """Flattens an Amazon meta record into {path: [text values]}.

    - details.{k}            -> 'details/{k}'
    - author.name            -> 'author/name'
    - categories (list)      -> 'categories' (one value per level)
    - lists of scalars       -> several values for the same path
    Complex objects (images, videos) are ignored here: they are handled by the
    image/text modality extractors.
    """
    out: Dict[str, List[str]] = defaultdict(list)

    def walk(node: Any, prefix: str, depth: int) -> None:
        if node is None:
            return
        if isinstance(node, dict):
            if depth > max_depth:
                return
            for k, v in node.items():
                key = f"{prefix}/{k}" if prefix else str(k)
                walk(v, key, depth + 1)
        elif isinstance(node, (list, tuple)):
            for item in node:
                if isinstance(item, (str, int, float, bool)):
                    walk(item, prefix, depth)
                elif isinstance(item, dict) and depth <= max_depth:
                    walk(item, prefix, depth + 1)
        elif isinstance(node, bool):
            out[prefix].append("true" if node else "false")
        elif isinstance(node, (int, float)):
            out[prefix].append(str(node))
        elif isinstance(node, str):
            s = node.strip()
            if s:
                out[prefix].append(s)

    for k, v in rec.items():
        if k in ("images", "videos", "bought_together"):
            continue
        walk(v, str(k), 0)

    # Amazon sometimes stores attribute/value pairs under an EMPTY key, glued
    # together (details[""] = "Shape Rectangular"). They are redistributed to
    # 'details/<Attribute>' when the prefix matches a known attribute.
    orphelines = out.pop("details/", None)
    if orphelines:
        connus = _known_attributes(out)
        for brut in orphelines:
            cle, valeur = _split_glued_pair(brut, connus)
            if cle:
                out[f"details/{cle}"].append(valeur)
    return dict(out)


# The most frequent Amazon attributes under an empty key, sorted by decreasing
# length so that 'Unit Count' wins over 'Unit'.
_ATTRIBUTS_ORPHELINS = [
    "Number of Compartments", "Recommended Uses For Product", "Age Range (Description)",
    "Item Package Quantity", "Number of Items", "Number of Pieces", "Unit Count",
    "Special Feature", "Product Benefits", "Closure Type", "Item Firmness",
    "Included Components", "Fabric Type", "Item Weight", "Item Form", "Care Instructions",
    "Target Audience", "Water Resistance Level", "Power Source", "Mounting Type",
    "Frame Material", "Fill Material", "Outer Material", "Material Feature",
    "Age Range", "Room Type", "Skin Type", "Hair Type", "Item Volume",
    "Brand", "Color", "Size", "Shape", "Style", "Scent", "Finish", "Pattern",
    "Material", "Department", "Theme", "Occasion", "Capacity", "Seasons",
    # completed from the values actually not recovered on Baby_Products
    "Maximum Weight Recommendation", "Minimum Weight Recommendation",
    "Is Discontinued By Manufacturer", "Specific Uses For Product",
    "Product Care Instructions", "Auto Part Position", "Is Dishwasher Safe",
    "Assembly Required", "Batteries Required", "Batteries Included",
    "Country of Origin", "Country/Region of Origin", "Target Gender",
    "Blanket Form", "Reusability", "Installation Type", "Bottle Type",
    "Item Dimensions", "Number Of Items", "Is Portable", "Is Waterproof",
    "Care Instructions", "Item Type Name", "Manufacturer", "Product Dimensions",
]


def _known_attributes(plat: Dict[str, List[str]]) -> List[str]:
    """Candidate attributes: those already present in the product, then the
    reference list. Sorted from longest to shortest (most specific prefix)."""
    presents = [k.split("/", 1)[1] for k in plat if k.startswith("details/") and "/" in k]
    return sorted(set(presents) | set(_ATTRIBUTS_ORPHELINS), key=len, reverse=True)


def _split_glued_pair(brut: str, connus: Sequence[str]) -> Tuple[Optional[str], str]:
    """'Shape Rectangular' -> ('Shape', 'Rectangular'). (None, '') if undecidable."""
    s = collapse_ws(brut)
    bas = s.lower()
    for attr in connus:
        a = attr.lower()
        if bas.startswith(a) and len(s) > len(attr):
            reste = s[len(attr):].strip(" :-\t")
            if reste:
                return attr, reste
    return None, ""


# =============================================================================
# 4. Registry of value-normalisation operations
# =============================================================================

@dataclass
class OpContext:
    relation: str
    params: Dict[str, Any]
    rules: Dict[str, Any]
    person: PersonNormalizer
    pass_no: int                      # 1 = learning, 2 = application
    collector: Dict[str, Counter]     # relation -> Counter(value)
    valuemap: Dict[str, Dict[str, str]]
    side: Dict[str, List[str]]        # derived relations (e.g. audio format)
    stats: Counter
    cache: Dict[Any, Any] = field(default_factory=dict)   # expensive objects per step
    # Triples whose SUBJECT is not the product: taxonomy edges of the form
    # (Shoes, hasSubtype, Boot). They are deduplicated globally and written
    # once at the end of the .kg, where ordinary triples are written per
    # product.
    taxo: Set[Tuple[str, str, str]] = field(default_factory=set)


OpFn = Callable[[List[str], OpContext], List[str]]
OPS: Dict[str, OpFn] = {}


def op(name: str) -> Callable[[OpFn], OpFn]:
    def deco(fn: OpFn) -> OpFn:
        OPS[name] = fn
        return fn
    return deco


def _get_rules_dict(ctx: OpContext, ref: Optional[str]) -> Dict[str, str]:
    """Resolves 'movies.studio_mappings' in the rule registry."""
    if not ref:
        return {}
    node: Any = ctx.rules
    for part in ref.split("."):
        if not isinstance(node, dict) or part not in node:
            return {}
        node = node[part]
    return node if isinstance(node, dict) else {}


def _get_rules_list(ctx: OpContext, ref: Optional[str]) -> List[Any]:
    return _resolve_rules(ctx.rules, ref, list) or []


def _resolve_rules(rules: Any, ref: Optional[str], attendu: type) -> Any:
    """Resolves 'common.keep_whole' in the rule registry, outside an OpContext.
    Usable between the two passes, where no operation context exists."""
    if not ref:
        return None
    node: Any = rules
    for part in str(ref).split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, attendu) else None


# --- Basic text operations ---------------------------------------------------

@op("strip_html")
def _op_strip_html(vals, ctx):
    return [strip_html(v) for v in vals]


@op("fix_encoding")
def _op_fix_encoding(vals, ctx):
    return [fix_mojibake(nfkc(v)) for v in vals]


@op("collapse_ws")
def _op_collapse_ws(vals, ctx):
    return [collapse_ws(v) for v in vals]


@op("lower")
def _op_lower(vals, ctx):
    return [v.lower() for v in vals]


@op("upper")
def _op_upper(vals, ctx):
    return [v.upper() for v in vals]


_SMALL_WORDS = {"a", "an", "and", "the", "of", "or", "for", "in", "on", "at", "to",
                "by", "with", "from", "de", "du", "des", "la", "le", "les", "et"}


@op("title_case")
def _op_title_case(vals, ctx):
    out = []
    for v in vals:
        toks = v.split(" ")
        res = []
        for i, t in enumerate(toks):
            if not t:
                continue
            if t.isupper() and len(t) <= 4:      # acronyms: BBC, HBO, MGM, DVD
                res.append(t)
            elif t.lower() in _SMALL_WORDS and 0 < i < len(toks) - 1:
                res.append(t.lower())
            else:
                res.append(t[0].upper() + t[1:] if t[0].isalpha() else t)
        out.append(" ".join(res))
    return out


@op("strip_chars")
def _op_strip_chars(vals, ctx):
    chars = ctx.params.get("chars", " .,;:-*_|/\\\"'")
    return [v.strip(chars) for v in vals]


@op("strip_parenthetical")
def _op_strip_parenthetical(vals, ctx):
    return [collapse_ws(re.sub(r"[(\[][^)\]]*[)\]]", " ", v)) for v in vals]


@op("truncate")
def _op_truncate(vals, ctx):
    n = int(ctx.params.get("max_len", 120))
    return [v[:n].rstrip() for v in vals]


# --- Multi-value splitting ---------------------------------------------------

_DEFAULT_SEPS = [",", ";", "|", "/", " & ", " and ", "•"]


@op("split_multi")
def _op_split_multi(vals, ctx):
    seps = ctx.params.get("separators", _DEFAULT_SEPS)
    keep_short = int(ctx.params.get("min_len", 2))
    out: List[str] = []
    for v in vals:
        parts = [v]
        for sep in seps:
            nxt: List[str] = []
            for p in parts:
                nxt.extend(p.split(sep))
            parts = nxt
        for p in parts:
            p = collapse_ws(p)
            if len(p) >= keep_short:
                out.append(p)
    return out


# --- Smart splitting (two passes) --------------------------------------------
# A weak separator does not always mean 'list of values': 'Action & Adventure'
# is two genres, 'Bath & Body Works' one brand. A weak separator is split
# only if every fragment is attested alone in the same relation.

_STRONG_SEPS = [";", "|", "•"]
_WEAK_SEPS = [" & ", " and ", " + ", "/", ","]


def _split_value(valeur: str, seps: Sequence[str], min_len: int) -> List[str]:
    parts = [valeur]
    for sep in seps:
        suivant: List[str] = []
        for p in parts:
            suivant.extend(p.split(sep))
        parts = suivant
    return [p for p in (collapse_ws(x) for x in parts) if len(p) >= min_len]


def _weak_sep_present(valeur: str, seps: Sequence[str]) -> bool:
    return any(sep in valeur for sep in seps)


# Separators that may link two parts of the SAME name ('Johnson & Johnson').
# A comma, a slash or a semicolon, never.
_CONJONCTIONS = (" & ", " and ", " + ", " et ")


def _conjunction_only(valeur: str, seps: Sequence[str]) -> bool:
    """True if the only separators present are conjunctions."""
    presents = [s for s in seps if s in valeur]
    return bool(presents) and all(s in _CONJONCTIONS for s in presents)


@op("split_smart")
def _op_split_smart(vals, ctx):
    """Always splits on the safe separators, and on the ambiguous ones
    according to 'mode':

      mode: evidence (default), only splits if the corpus attests each
            fragment on its own. For everything that carries proper nouns
            (brands, artists, publishers, studios) and for attribute
            enumerations where doubt exists.
      mode: always, splits unconditionally. For taxonomies, '&' always means
            'and': hasCategory, hasGenre.

    The choice is made per relation in categories.yaml: knowledge of the
    attribute content, not a syntactic rule, settles it.
    """
    strong = ctx.params.get("strong", _STRONG_SEPS)
    weak = ctx.params.get("weak", _WEAK_SEPS)
    min_len = int(ctx.params.get("min_len", 3))

    if str(ctx.params.get("mode", "evidence")) == "always":
        out: List[str] = []
        for v in vals:
            parts = _split_value(v, list(strong) + list(weak), min_len)
            if len(parts) > 1:
                ctx.stats[f"{ctx.relation}:split_smart"] += 1
            out.extend(parts)
        return out

    if ctx.pass_no == 1:
        atomes = ctx.collector.setdefault(ctx.relation + "::atoms", Counter())
        entiers = ctx.collector.setdefault(ctx.relation + "::wholes", Counter())
        for v in vals:
            for part in _split_value(v, strong, min_len):
                if _weak_sep_present(part, weak):
                    entiers[part] += 1
                else:
                    atomes[match_key(part)] += 1
        return vals

    decisions = ctx.valuemap.get(ctx.relation + "::weaksplit") or {}
    out: List[str] = []
    for v in vals:
        for part in _split_value(v, strong, min_len):
            frags = decisions.get(part)
            if frags:
                ctx.stats[f"{ctx.relation}:split_smart"] += 1
                out.extend(frags)
            else:
                out.append(part)
    return out


def build_weak_split_index(atomes: Counter, entiers: Counter, weak: Sequence[str],
                           min_evidence: int = 3, min_len: int = 3,
                           keep_whole: Sequence[str] = ()) -> Dict[str, List[str]]:
    """whole_form -> [fragments], only when the split is justified."""
    protege = {match_key(str(x)) for x in keep_whole}
    idx: Dict[str, List[str]] = {}
    for entier in entiers:
        if match_key(entier) in protege:
            continue
        frags = _split_value(entier, weak, min_len)
        if len(frags) < 2:
            continue
        cles = [match_key(f) for f in frags]
        # 'Johnson & Johnson': identical fragments around a CONJUNCTION
        # => proper noun, no split. Around a comma, 'Vehicle Specific,
        # Vehicle Specific' is a redundant list and is split (triple
        # deduplication does the rest).
        if len(set(cles)) < len(cles) and _conjunction_only(entier, weak):
            continue
        if all(atomes.get(k, 0) >= min_evidence for k in cles):
            idx[entier] = frags
    return idx


_ROLE_RE = re.compile(r"\(([^)]{2,30})\)\s*$")

# =============================================================================
# Cleaning of the mentions glued to person names
# =============================================================================
# 'Wayv (Artist) Format: Audio Cd' -> 'Wayv'. Closed lists rather than blind
# trimming: not every parenthesis is a role ('BMG Rights Management (US) LLC')
# and not every 'Word:' is a label ('Nikki Sixx:').

_ROLES_CONNUS = frozenset([
    "artist", "artists", "main artist", "primary artist", "featured artist",
    "performer", "performers", "composer", "composers", "author", "authors",
    "co-author", "contributor", "contributors", "conductor", "orchestra",
    "ensemble", "choir", "chorus", "soloist", "band", "group",
    "narrator", "reader", "vocals", "vocalist", "singer", "lyricist",
    "songwriter", "arranger", "remixer", "producer", "executive producer",
    "director", "actor", "actress", "cast", "star", "starring",
    "writer", "screenwriter", "editor", "illustrator", "translator",
    "adapter", "creator", "photographer", "foreword", "preface",
    "introduction", "afterword", "epilogue", "engineer", "mixer", "dj",
    "feat", "featuring", "presenter", "host", "auteur", "compositeur",
    "interprete", "realisateur", "acteur",
])

# Product-record labels that Amazon appends after the name.
_ETIQUETTES_FICHE = ["format", "rated", "by", "language", "publisher", "studio",
                     "label", "duration", "runtime", "starring", "director",
                     "actors", "asin", "edition", "series"]

_PARENTHESE_CONTENU = re.compile(r"\(([^()]{1,40})\)")
_SEP_ROLES = re.compile(r"\s*(?:[,/;&+]|\band\b|\bet\b)\s*", re.IGNORECASE)
_SUFFIXE_FICHE = re.compile(
    r"\s*\b(?:%s)\s*:.*$" % "|".join(_ETIQUETTES_FICHE), re.IGNORECASE)


@op("strip_roles")
def _op_strip_roles(vals, ctx):
    """Removes the role mention and the record labels, keeps the name.

        'Wayv (Artist) Format: Audio Cd'   -> 'Wayv'
        'BMG Rights Management (US) LLC'   -> unchanged
        'Album-Oriented Rock (AOR)'        -> unchanged

    Unlike keep_roles, this operation does not FILTER: it cleans.
    Parameters: roles (replaces the list), extra_roles (extends it),
    labels (replaces the record labels), keep_labels: true (keeps them).
    """
    roles = set(_ROLES_CONNUS)
    if ctx.params.get("roles"):
        roles = {str(r).strip().lower() for r in ctx.params["roles"]}
    roles |= {str(r).strip().lower() for r in (ctx.params.get("extra_roles") or [])}

    if ctx.params.get("keep_labels"):
        suffixe = None
    elif ctx.params.get("labels"):
        suffixe = re.compile(r"\s*\b(?:%s)\s*:.*$"
                             % "|".join(re.escape(str(x)) for x in ctx.params["labels"]),
                             re.IGNORECASE)
    else:
        suffixe = _SUFFIXE_FICHE

    def _replace_value(m):
        contenu = m.group(1).strip().lower()
        morceaux = [p.strip() for p in _SEP_ROLES.split(contenu) if p.strip()]
        if morceaux and all(p in roles for p in morceaux):
            ctx.stats[f"{ctx.relation}:role_retire"] += 1
            return " "
        return m.group(0)

    out = []
    for v in vals:
        avant = v
        if "(" in v:
            v = _PARENTHESE_CONTENU.sub(_replace_value, v)
        if suffixe is not None and ":" in v:
            v2 = suffixe.sub("", v)
            if v2 != v:
                ctx.stats[f"{ctx.relation}:etiquette_retiree"] += 1
                v = v2
        if v is not avant:
            v = collapse_ws(v).strip(" ,;:-")
        if v:
            out.append(v)
    return out


@op("keep_roles")
def _op_keep_roles(vals, ctx):
    """Keeps only the people carrying a wanted role, and removes the role.

    The 'store' field of Books is not a shop but a statement of
    responsibility:
        'Nora Roberts (Author)'                       -> Nora Roberts
        'Wu Jun (Author), Reading Room (Reader)'      -> Wu Jun
        'DK Publishing (Author)'                      -> DK Publishing
    A value WITHOUT any role parenthesis passes through untouched, which makes
    it possible to mix this source with 'author/name', already clean.
    """
    garder = {str(r).lower() for r in (ctx.params.get("roles") or ["author"])}
    out: List[str] = []
    for v in vals:
        if "(" not in v:
            out.append(v)
            continue
        morceaux = [m.strip() for m in v.split(",")]
        trouve = False
        for m in morceaux:
            mo = _ROLE_RE.search(m)
            if not mo:
                continue
            trouve = True
            if mo.group(1).strip().lower() in garder:
                nom = collapse_ws(m[: mo.start()])
                if nom:
                    out.append(nom)
                    ctx.stats[f"{ctx.relation}:role_garde"] += 1
        if not trouve:
            out.append(v)
    return out


@op("max_values")
def _op_max_values(vals, ctx):
    n = int(ctx.params.get("n", 3))
    seen: Set[str] = set()
    out: List[str] = []
    for v in vals:
        k = match_key(v)
        if k in seen:
            continue
        seen.add(k)
        out.append(v)
        if len(out) >= n:
            break
    return out


# --- Filtering ---------------------------------------------------------------

def _cache_op(ctx, cle, factory):
    """Memoises an expensive object (set, compiled regex) per pipeline step.
    The parameters of a step are fixed: it is built only once."""
    if cle not in ctx.cache:
        ctx.cache[cle] = factory()
    return ctx.cache[cle]


@op("drop_values")
def _op_drop_values(vals, ctx):
    ref = ctx.params.get("rules_ref")
    inline = tuple(ctx.params.get("values", []))
    bad = _cache_op(
        ctx, ("drop_values", ref, inline),
        lambda: {match_key(str(x))
                 for x in list(_get_rules_list(ctx, ref)) + list(inline)},
    )
    return [v for v in vals if match_key(v) not in bad]


@op("drop_matching")
def _op_drop_matching(vals, ctx):
    ref = ctx.params.get("rules_ref")
    inline = tuple(ctx.params.get("patterns", []))
    rx = _cache_op(
        ctx, ("drop_matching", ref, inline),
        lambda: _compile_patterns(
            list(inline) + [str(x) for x in _get_rules_list(ctx, ref)]),
    )
    if rx is None:
        return vals
    return [v for v in vals if not rx.search(v)]


@op("keep_matching")
def _op_keep_matching(vals, ctx):
    inline = tuple(ctx.params.get("patterns", []))
    rx = _cache_op(ctx, ("keep_matching", inline),
                   lambda: _compile_patterns(list(inline)))
    if rx is None:
        return vals
    return [v for v in vals if rx.search(v)]


def _compile_patterns(pats):
    return re.compile("|".join(pats), re.IGNORECASE) if pats else None


@op("drop_long")
def _op_drop_long(vals, ctx):
    n = int(ctx.params.get("max_len", 100))
    return [v for v in vals if len(v) <= n]


@op("drop_numeric")
def _op_drop_numeric(vals, ctx):
    return [v for v in vals if not re.fullmatch(r"[\d\s.,%-]+", v)]


# --- Explicit mapping --------------------------------------------------------

@op("map")
def _op_map(vals, ctx):
    """Explicit mapping, insensitive to case/accents/punctuation.
    One mapped value may produce several ('Action,Thriller')."""
    table = dict(ctx.params.get("table", {}))
    table.update(_get_rules_dict(ctx, ctx.params.get("rules_ref")))
    if not table:
        return vals
    lut = {match_key(k): v for k, v in table.items()}
    split_on = ctx.params.get("split_result", ",")
    strict = bool(ctx.params.get("strict", False))   # strict: rejects anything off-table
    out: List[str] = []
    for v in vals:
        hit = lut.get(match_key(v))
        if hit is None:
            hit = lut.get(tight_key(v)) if ctx.params.get("tight", False) else None
        if hit is not None:
            ctx.stats[f"{ctx.relation}:mapped"] += 1
            out.extend([p.strip() for p in str(hit).split(split_on) if p.strip()])
        elif not strict:
            out.append(v)
        else:
            ctx.stats[f"{ctx.relation}:map_rejected"] += 1
    return out


@op("regex_map")
def _op_regex_map(vals, ctx):
    """List of [pattern, replacement] applied in order.
    The first pattern that matches replaces the WHOLE value if 'whole' = true."""
    rules = ctx.params.get("rules", []) or _get_rules_list(ctx, ctx.params.get("rules_ref"))
    whole = bool(ctx.params.get("whole", True))
    if not rules:
        return vals
    compiled = [(re.compile(p, re.IGNORECASE), r) for p, r in rules]
    out = []
    for v in vals:
        new = v
        for rx, rep in compiled:
            if rx.search(new):
                new = rep if whole else rx.sub(rep, new)
                ctx.stats[f"{ctx.relation}:regex_mapped"] += 1
                break
        out.append(collapse_ws(new))
    return out


@op("prefix_map")
def _op_prefix_map(vals, ctx):
    """Consolidates by prefix: 'Sony Pictures Home Entertainment' -> 'Sony Pictures'.
    table = {canonical_brand: [prefixes/keywords]}."""
    table = dict(ctx.params.get("table", {}))
    table.update(_get_rules_dict(ctx, ctx.params.get("rules_ref")))
    if not table:
        return vals
    compiled = [(canon, [tight_key(x) for x in keys]) for canon, keys in table.items()]
    out = []
    for v in vals:
        tk = tight_key(v)
        hit = None
        for canon, keys in compiled:
            if any(k and (tk.startswith(k) or k in tk) for k in keys):
                hit = canon
                break
        if hit:
            ctx.stats[f"{ctx.relation}:prefix_mapped"] += 1
            out.append(hit)
        else:
            out.append(v)
    return out


# --- Domain operations -------------------------------------------------------

@op("person_name")
def _op_person_name(vals, ctx):
    import dataclasses
    pn = ctx.person
    over = {k: v for k, v in ctx.params.items() if k in ("min_tokens", "max_len")}
    if over:
        pn = dataclasses.replace(pn, **{k: int(v) for k, v in over.items()})
    out = []
    for v in vals:
        r = pn(v)
        if r:
            out.append(r)
        else:
            ctx.stats[f"{ctx.relation}:person_rejected"] += 1
    return out


@op("language_base")
def _op_language_base(vals, ctx):
    """Extracts the base language; the audio format goes to a derived relation."""
    audio_rel = ctx.params.get("audio_relation")
    out = []
    for v in vals:
        lang, audio = split_language_audio(v)
        if audio and audio_rel:
            ctx.side.setdefault(audio_rel, []).append(audio)
        if lang:
            out.append(lang)
    return out


@op("parse_pages_bin")
def _op_parse_pages_bin(vals, ctx):
    bins = ctx.params.get("bins") or _get_rules_list(ctx, ctx.params.get("rules_ref"))
    keep_raw_rel = ctx.params.get("raw_relation")
    out = []
    for v in vals:
        n = parse_pages(v)
        if n is None:
            ctx.stats[f"{ctx.relation}:unparsed"] += 1
            continue
        if keep_raw_rel:
            ctx.side.setdefault(keep_raw_rel, []).append(str(n))
        label = bin_value(n, bins) if bins else str(n)
        if label:
            out.append(label)
    return out


@op("parse_runtime_bin")
def _op_parse_runtime_bin(vals, ctx):
    bins = ctx.params.get("bins") or _get_rules_list(ctx, ctx.params.get("rules_ref"))
    keep_raw_rel = ctx.params.get("raw_relation")
    out = []
    for v in vals:
        n = parse_runtime_minutes(v)
        if n is None:
            ctx.stats[f"{ctx.relation}:unparsed"] += 1
            continue
        if keep_raw_rel:
            ctx.side.setdefault(keep_raw_rel, []).append(str(n))
        label = bin_value(n, bins) if bins else str(n)
        if label:
            out.append(label)
    return out


@op("parse_numeric_bin")
def _op_parse_numeric_bin(vals, ctx):
    bins = ctx.params.get("bins") or _get_rules_list(ctx, ctx.params.get("rules_ref"))
    out = []
    for v in vals:
        n = parse_number(v)
        if n is None:
            ctx.stats[f"{ctx.relation}:unparsed"] += 1
            continue
        label = bin_value(n, bins) if bins else str(n)
        if label:
            out.append(label)
    return out


# =============================================================================
# Dimensions: one unit, one spelling
# =============================================================================
# '48*48', '48x48', '48cm*48cm', '48"x48"', '12-x-12-inch' all mean the same
# thing and are brought back to '48 x 48 cm'. Amazon also writes '7 5 inch'
# for 7.5 inch, so a number followed by a short number is read as a decimal.

_VERS_CM = {
    "mm": 0.1, "millimeter": 0.1, "millimeters": 0.1, "millimetre": 0.1,
    "cm": 1.0, "centimeter": 1.0, "centimeters": 1.0, "centimetre": 1.0,
    "m": 100.0, "meter": 100.0, "meters": 100.0, "metre": 100.0,
    "in": 2.54, "inch": 2.54, "inches": 2.54, '"': 2.54, "''": 2.54, "”": 2.54,
    "ft": 30.48, "foot": 30.48, "feet": 30.48,
    "yd": 91.44, "yard": 91.44, "yards": 91.44,
}

# Units that prove the value is NOT a dimension ('146 x 38 month').
_UNITES_NON_LONGUEUR = {
    "month", "months", "year", "years", "day", "days", "week", "weeks",
    "count", "ct", "pack", "packs", "piece", "pieces", "pcs", "oz", "ounce",
    "ounces", "lb", "lbs", "pound", "pounds", "kg", "g", "gram", "grams",
    "ml", "l", "liter", "liters", "gallon", "gallons", "watt", "watts", "volt",
    "volts", "gb", "tb", "mb", "mah", "hz", "khz", "mhz",
}

_PARENTHESE = re.compile(r"\s*\([^)]*\)\s*$")
_GUILLEMETS = re.compile(r"(\d)\s*(?:''|\"|”|″|’’|inch(?:es)?\b|in\b)", re.IGNORECASE)
_SEP_DIM = re.compile(r"\s*(?:[x×*✕]|\bby\b)\s*", re.IGNORECASE)
_NOMBRE_UNITE = re.compile(
    r"^(?P<n>\d+(?:[.,]\d+)?)\s*(?P<u>[a-z\"'”″’]*)\.?$", re.IGNORECASE)
_DECIMAL_ECLATE = re.compile(
    r"^(?P<e>\d+)\s+(?P<d>\d{1,2})\s*(?P<u>[a-z\"'”″’]+)?$", re.IGNORECASE)


def _normalised_unit(u: str) -> Optional[str]:
    u = (u or "").strip().lower().rstrip(".")
    if not u:
        return ""
    if u in _VERS_CM:
        return u
    if u in _UNITES_NON_LONGUEUR:
        return None
    return None


_A_UN_SEPARATEUR = re.compile(r"\d\s*(?:[x×*✕]|\bby\b)\s*\d", re.IGNORECASE)
# lookahead variant: the occurrences overlap, so '1x2x3x4' counts three of
# them and not two.
_COMPTE_SEPARATEURS = re.compile(r"(?=\d\s*(?:[x×*✕]|\bby\b)\s*\d)", re.IGNORECASE)
_MOT = re.compile(r"[a-z]+", re.IGNORECASE)


def _has_non_length_unit(s: str) -> bool:
    """True if the value contains a unit word that is not a length."""
    return any(m.group(0).lower() in _UNITES_NON_LONGUEUR for m in _MOT.finditer(s))


def parse_dimensions(valeur: str, unite_cible: str = "cm",
                     unite_supposee: Optional[str] = None,
                     max_dims: int = 3, decimales: int = 1) -> Optional[str]:
    """'48cm*48cm' -> '48 x 48 cm'. None if the value is not a dimension.

    Three attempts, from the safest to the most permissive:
      1. the whole value is a dimension                '48cm*48cm'
      2. one side of a '/' is                          '30x40cm/12x16inch'
      3. a contiguous portion of the value is, provided it contains a
         dimension separator                           'small 4x1.5 inch',
                                                       '16x20 no frame',
                                                       '12 x 12 inch - 25 sheets'
    """
    if unite_cible not in _VERS_CM:
        unite_cible = "cm"
    if unite_supposee is None:
        unite_supposee = unite_cible

    s = collapse_ws(valeur)
    s = _PARENTHESE.sub("", s)                       # '(pack of 1)'
    s = _GUILLEMETS.sub(r"\1 in", s)                 # 48" -> 48 in
    s = re.sub(r"[-_]+", " ", s)                     # '12-x-12-inch'
    s = collapse_ws(s)
    if not re.search(r"\d", s):
        return None

    direct = _parse_dim_strict(s, unite_cible, unite_supposee, max_dims, decimales)
    if direct:
        return direct

    # '30x40cm/12x16inch': the same measurement in two units. Amazon also
    # aggregates dimension and weight in one field ('16 x 1 x 1 inches;
    # 1.76 Ounces'), the most common form of 'Product Dimensions'.
    if "/" in s or ";" in s:
        for part in re.split(r"[;/]", s):
            part = collapse_ws(part)
            if not part:
                continue
            res = _parse_dim_strict(part, unite_cible, unite_supposee,
                                     max_dims, decimales)
            if res:
                return res

    # Contiguous portion: it must contain a dimension separator, and the whole
    # value must carry no unit that is not a length ('146 x 38 month').
    if _has_non_length_unit(s):
        return None
    # '1 x 2 x 3 x 4 cm' is malformed: do not silently extract a three-
    # dimensional sub-portion from it.
    if len(_COMPTE_SEPARATEURS.findall(s)) >= max_dims:
        return None
    mots = s.split()
    for debut in range(len(mots)):
        for fin in range(len(mots), debut, -1):
            frag = " ".join(mots[debut:fin])
            if not _A_UN_SEPARATEUR.search(frag):
                continue
            res = _parse_dim_strict(frag, unite_cible, unite_supposee,
                                     max_dims, decimales)
            if res:
                return res
    return None


def _parse_dim_strict(s: str, unite_cible: str, unite_supposee: str,
                       max_dims: int, decimales: int) -> Optional[str]:
    """The WHOLE string must be a dimensional expression."""
    morceaux = [m.strip() for m in _SEP_DIM.split(s) if m.strip()]
    if not morceaux or len(morceaux) > max_dims:
        return None

    valeurs: List[float] = []
    unites: List[str] = []
    for morceau in morceaux:
        m = _NOMBRE_UNITE.match(morceau)
        if m is None:
            # '7 5 inch' = 7.5 inch: decimal split apart by the source
            m2 = _DECIMAL_ECLATE.match(morceau)
            if m2 is None:
                return None
            nombre = float("%s.%s" % (m2.group("e"), m2.group("d")))
            u = _normalised_unit(m2.group("u") or "")
        else:
            try:
                nombre = float(m.group("n").replace(",", "."))
            except ValueError:
                return None
            u = _normalised_unit(m.group("u") or "")
        if u is None:                                # unit that is not a length
            return None
        valeurs.append(nombre)
        unites.append(u)

    if not valeurs or any(v <= 0 for v in valeurs):
        return None

    # Amazon often writes the unit only once ('12x16 inches'): it applies to
    # every dimension.
    explicites = [u for u in unites if u]
    commune = explicites[-1] if explicites else unite_supposee
    facteur_cible = _VERS_CM[unite_cible]
    sortie = []
    for v, u in zip(valeurs, unites):
        cm = v * _VERS_CM[u or commune]
        converti = cm / facteur_cible
        arrondi = round(converti, decimales)
        sortie.append(("%d" % arrondi) if abs(arrondi - int(arrondi)) < 1e-9
                      else ("%.*f" % (decimales, arrondi)))
    return " x ".join(sortie) + " " + unite_cible


@op("normalize_dimensions")
def _op_normalize_dimensions(vals, ctx):
    """Brings any dimensional value back to one spelling and one unit.

    unit          target unit (default 'cm')
    assume_unit   unit assumed when the value carries none; by default the
                  target unit, so that '48 * 48' == '48cm*48cm'
    drop_unparsed true = discard what is not a dimension (default: let it
                  through untouched, since hasSize also carries 'Small'...)
    """
    cible = str(ctx.params.get("unit", "cm")).lower()
    supposee = ctx.params.get("assume_unit")
    supposee = str(supposee).lower() if supposee else None
    max_dims = int(ctx.params.get("max_dims", 3))
    decimales = int(ctx.params.get("decimals", 1))
    jeter = bool(ctx.params.get("drop_unparsed", False))
    out = []
    for v in vals:
        dim = parse_dimensions(v, cible, supposee, max_dims, decimales)
        if dim:
            ctx.stats[f"{ctx.relation}:dimension"] += 1
            out.append(dim)
        elif not jeter:
            out.append(v)
    return out


# =============================================================================
# Physical quantities: a common unit, then a class
# =============================================================================
# '1 Pounds', '16 Ounces' and '454 Grams' are the same weight under three
# entities. Converting is not enough, since the converted value stays nearly
# continuous, so the CLASS is distributed. 'oz' belongs to two families
# (mass, volume); the 'family' parameter settles it, not the value.

_UNITE_BASE = {"mass": "g", "volume": "ml", "length": "mm"}

_UNITES: Dict[str, Dict[str, float]] = {
    "mass": {
        "g": 1.0, "gr": 1.0, "gram": 1.0, "grams": 1.0, "gramme": 1.0,
        "grammes": 1.0, "gm": 1.0, "gms": 1.0,
        "mg": 0.001, "milligram": 0.001, "milligrams": 0.001,
        "kg": 1000.0, "kgs": 1000.0, "kilo": 1000.0, "kilos": 1000.0,
        "kilogram": 1000.0, "kilograms": 1000.0, "kilogramme": 1000.0,
        "oz": 28.349523, "ounce": 28.349523, "ounces": 28.349523,
        "lb": 453.59237, "lbs": 453.59237, "pound": 453.59237,
        "pounds": 453.59237,
        "ton": 907184.74, "tons": 907184.74,
    },
    "volume": {
        "ml": 1.0, "milliliter": 1.0, "milliliters": 1.0, "millilitre": 1.0,
        "millilitres": 1.0, "cc": 1.0,
        "cl": 10.0, "dl": 100.0,
        "l": 1000.0, "liter": 1000.0, "liters": 1000.0, "litre": 1000.0,
        "litres": 1000.0,
        "fl oz": 29.5735, "floz": 29.5735, "fluid ounce": 29.5735,
        "fluid ounces": 29.5735, "oz": 29.5735, "ounce": 29.5735,
        "ounces": 29.5735,
        "cup": 236.5882, "cups": 236.5882,
        "pt": 473.1765, "pint": 473.1765, "pints": 473.1765,
        "qt": 946.3529, "quart": 946.3529, "quarts": 946.3529,
        "gal": 3785.4118, "gallon": 3785.4118, "gallons": 3785.4118,
    },
    "length": {
        "mm": 1.0, "millimeter": 1.0, "millimeters": 1.0, "millimetre": 1.0,
        "millimetres": 1.0,
        "cm": 10.0, "centimeter": 10.0, "centimeters": 10.0,
        "centimetre": 10.0, "centimetres": 10.0,
        "m": 1000.0, "meter": 1000.0, "meters": 1000.0, "metre": 1000.0,
        "metres": 1000.0,
        "in": 25.4, "inch": 25.4, "inches": 25.4, '"': 25.4, "''": 25.4,
        "ft": 304.8, "foot": 304.8, "feet": 304.8,
        "yd": 914.4, "yard": 914.4, "yards": 914.4,
    },
}

# The WHOLE value must be a scalar followed by at most a two-word unit
# ('12 fl oz'). Free text is deliberately refused: '1 x 1 x 1 inches' is not a
# scalar, and '16 Teeth' carries a unit proving that the field was misused.
# the field was misused.
_QUANTITE = re.compile(
    r"""^\s*(?:about\s+|approx\.?\s+|~\s*)?
        (?P<n>\d{1,12}(?:[.,]\d{1,6})?)\s*
        (?P<u>[a-zA-Z]{1,12}(?:\s+[a-zA-Z]{1,12})?|"|''|)\s*\.?\s*$""",
    re.VERBOSE)


def parse_quantity(valeur: str, famille: str,
                   unite_supposee: Optional[str] = None) -> Optional[float]:
    """Scalar value brought back to the base unit of the family (g, ml or mm).

    None as soon as the slightest doubt remains: non-scalar value, unit
    unknown to the family, missing unit without 'assume_unit', negative or
    zero number. An unknown unit is never ignored, which keeps '16 Teeth'
    out of a capacity and '30 months' out of a diameter.
    """
    table = _UNITES.get(famille)
    if not table:
        return None
    m = _QUANTITE.match(nfkc(str(valeur)))
    if m is None:
        return None
    try:
        nombre = float(m.group("n").replace(",", "."))
    except ValueError:
        return None
    if nombre <= 0:
        return None
    unite = collapse_ws(m.group("u") or "").lower()
    if not unite:
        unite = (unite_supposee or "").lower()
        if not unite:
            return None
    facteur = table.get(unite)
    if facteur is None:
        return None
    return nombre * facteur


def _format_number(n: float, decimales: int) -> str:
    arrondi = round(n, decimales)
    if decimales <= 0 or abs(arrondi - int(arrondi)) < 1e-9:
        return "%d" % int(round(arrondi))
    return "%.*f" % (decimales, arrondi)


@op("convert_measure")
def _op_convert_measure(vals, ctx):
    """Brings a quantity back to a single unit, then possibly to its class.

    family        mass | volume | length
    to            output unit (default: g, ml, mm depending on the family)
    assume_unit   unit assumed when the value carries none
    decimals      rounding of the converted value (default 0)
    bins          [[label, lo, hi], ...] expressed IN the output unit;
    bins_ref      the same, by reference to configs/rules
                  if either is given, it is the CLASS that is emitted
    keep_unparsed true = let the original value through (default: discard it)
    """
    famille = str(ctx.params.get("family", "mass")).lower()
    table = _UNITES.get(famille)
    if table is None:
        return []
    cible = str(ctx.params.get("to") or _UNITE_BASE[famille]).lower()
    facteur_cible = table.get(cible)
    if facteur_cible is None:
        cible = _UNITE_BASE[famille]
        facteur_cible = 1.0
    supposee = ctx.params.get("assume_unit")
    decimales = int(ctx.params.get("decimals", 0))
    bins = ctx.params.get("bins") or _get_rules_list(ctx, ctx.params.get("bins_ref"))
    garder = bool(ctx.params.get("keep_unparsed", False))
    out = []
    for v in vals:
        base = parse_quantity(v, famille, supposee)
        if base is None:
            ctx.stats[f"{ctx.relation}:mesure_rejetee"] += 1
            if garder:
                out.append(v)
            continue
        converti = base / facteur_cible
        ctx.stats[f"{ctx.relation}:mesure_convertie"] += 1
        if bins:
            label = bin_value(converti, bins)
            if label:
                out.append(label)
            else:
                ctx.stats[f"{ctx.relation}:hors_classes"] += 1
        else:
            out.append(_format_number(converti, decimales) + " " + cible)
    return out


_DIM_NORMALISEE = re.compile(r"^([\d.]+(?:\s*x\s*[\d.]+)*)\s+([a-z]+)$")


@op("dimension_volume")
def _op_dimension_volume(vals, ctx):
    """Volume of a product, computed from 'Product Dimensions', then binned.

    Requires THREE dimensions: a size with two sides ('48 x 48 cm') is a
    surface, not a volume, and inventing one would be a silent mistake.

    unit      working length unit (default cm, hence a volume in cm3)
    bins      [[label, lo, hi], ...] in unit^3; bins_ref for the reference
    decimals  rounding of the raw volume when no class is requested
    """
    unite = str(ctx.params.get("unit", "cm")).lower()
    supposee = ctx.params.get("assume_unit")
    supposee = str(supposee).lower() if supposee else None
    decimales = int(ctx.params.get("decimals", 0))
    bins = ctx.params.get("bins") or _get_rules_list(ctx, ctx.params.get("bins_ref"))
    out = []
    for v in vals:
        dim = parse_dimensions(v, unite, supposee, 3, 2)
        if not dim:
            ctx.stats[f"{ctx.relation}:volume_rejete"] += 1
            continue
        m = _DIM_NORMALISEE.match(dim)
        if m is None:
            continue
        cotes = [c.strip() for c in m.group(1).split("x")]
        if len(cotes) != 3:
            ctx.stats[f"{ctx.relation}:volume_incomplet"] += 1
            continue
        try:
            volume = 1.0
            for c in cotes:
                volume *= float(c)
        except ValueError:
            continue
        if volume <= 0:
            continue
        ctx.stats[f"{ctx.relation}:volume"] += 1
        if bins:
            label = bin_value(volume, bins)
            if label:
                out.append(label)
        else:
            out.append(_format_number(volume, decimales) + " " + unite + "3")
    return out


# =============================================================================
# Boolean flags
# =============================================================================
# Only the positive case is emitted: 'hasBatteryRequirement No' would hold for
# nearly every product. What is neither yes nor no is discarded, not guessed:
# the field is misused often enough ('2 AA', 'Lithium').

_VRAI = frozenset({
    "yes", "y", "true", "t", "1", "oui", "vrai", "si",
    "required", "include", "included", "with", "available", "supported",
    "enabled",                              # Kindle features (Text to Speech, X-Ray)
})
_FAUX = frozenset({
    "no", "n", "false", "f", "0", "non", "faux",
    "none", "not required", "no batteries required", "not included",
    "without", "not available", "na", "n/a", "not enabled", "disabled",
})


@op("boolean_flag")
def _op_boolean_flag(vals, ctx):
    """Brings a yes/no field back to a single interpretable value.

    label         value emitted for the positive case (default 'Yes')
    label_false   value emitted for the negative case (default 'No')
    emit          true_only (default) | both
    true_values / false_values   extra lists, on top of the defaults
    """
    etiquette = str(ctx.params.get("label", "Yes"))
    etiquette_faux = str(ctx.params.get("label_false", "No"))
    emettre_faux = str(ctx.params.get("emit", "true_only")).lower() == "both"
    vrais = _VRAI | {str(x).strip().lower() for x in (ctx.params.get("true_values") or [])}
    faux = _FAUX | {str(x).strip().lower() for x in (ctx.params.get("false_values") or [])}
    out = []
    for v in vals:
        cle = collapse_ws(nfkc(str(v))).strip(" .!?").lower()
        if cle in vrais:
            ctx.stats[f"{ctx.relation}:booleen_vrai"] += 1
            out.append(etiquette)
        elif cle in faux:
            ctx.stats[f"{ctx.relation}:booleen_faux"] += 1
            if emettre_faux:
                out.append(etiquette_faux)
        else:
            ctx.stats[f"{ctx.relation}:booleen_rejete"] += 1
    return out


# =============================================================================
# Ages: a threshold in months, then a band
# =============================================================================
# '0-6 months', '3+', 'Toddler', '18 Months & Up': the MINIMUM age in months
# is extracted, since it carries the constraint, then binned.

# Each word is placed on the THRESHOLD of the band it designates (see
# common.age_bins), so that a named stage falls back into its own band:
# 'Teen' must give Teen, not Preteen.
_AGE_MOTS = {
    "newborn": 0, "new born": 0, "birth": 0, "0m": 0,
    "baby": 1, "babies": 1, "infant": 1, "infants": 1,
    "toddler": 12, "toddlers": 12,
    "preschool": 36, "preschooler": 36, "pre school": 36, "pre k": 36,
    "kindergarten": 60, "child": 60, "children": 60, "kid": 60, "kids": 60,
    "school age": 60, "grade school": 60, "youth": 60,
    "tween": 132, "tweens": 132, "preteen": 132, "preteens": 132,
    "teen": 168, "teens": 168, "teenager": 168, "teenagers": 168,
    "young adult": 168, "young adults": 168,
    "adult": 216, "adults": 216, "grown up": 216,
}

_AGE_NOMBRE = re.compile(
    r"(?P<n>\d{1,3}(?:[.,]\d+)?)\s*"
    r"(?P<u>months?|mos?\b|mth|years?|yrs?\b|y\b|weeks?|wks?\b)?",
    re.IGNORECASE)

# Beyond this age in years, a bare number can no longer be a target age:
# no product targets a minimum of 26 years. It is then an age in months.
_ANS_PLAUSIBLES = 25

_VERS_MOIS = {"month": 1.0, "months": 1.0, "mo": 1.0, "mos": 1.0, "mth": 1.0,
              "year": 12.0, "years": 12.0, "yr": 12.0, "yrs": 12.0, "y": 12.0,
              "week": 0.2301, "weeks": 0.2301, "wk": 0.2301, "wks": 0.2301}


def parse_age_min_months(valeur: str,
                         unite_defaut: str = "years") -> Optional[float]:
    """Minimum age, in months. None if the value carries no readable age.

    '0-6 months' -> 0        '6 Months - 3 Years' -> 6
    '3+'         -> 36       'Toddler'            -> 12
    """
    s = collapse_ws(nfkc(str(valeur))).lower()
    if not s:
        return None
    mot = re.sub(r"[^a-z ]+", " ", s)
    mot = collapse_ws(mot)
    if mot in _AGE_MOTS:
        return float(_AGE_MOTS[mot])
    trouves: List[float] = []
    for m in _AGE_NOMBRE.finditer(s):
        try:
            n = float(m.group("n").replace(",", "."))
        except ValueError:
            continue
        u = (m.group("u") or "").rstrip(".").lower()
        if u:
            facteur = _VERS_MOIS.get(u)
        elif n > _ANS_PLAUSIBLES and unite_defaut in ("year", "years", "y"):
            # Bare number too large to be an age in years: it is an age in
            # months. Amazon has a 'Manufacturer Minimum Age (MONTHS)' field
            # filled '96.00' or '168.0': read in years, 8 and 14 centuries.
            facteur = 1.0
        else:
            facteur = _VERS_MOIS.get(unite_defaut)
        if facteur is None:
            continue
        trouves.append(n * facteur)
    if trouves:
        return min(trouves)
    # Then there is the age word buried in a sentence: 'Teen and Adults',
    # 'Baby, Toddler'. When several stages are named, the youngest carries
    # the constraint, as for numeric intervals.
    presents = {v for k, v in _AGE_MOTS.items()
                if re.search(r"\b%s\b" % re.escape(k), mot)}
    if presents:
        return float(min(presents))
    return None


@op("parse_age_bin")
def _op_parse_age_bin(vals, ctx):
    """Minimum age in months, given back as its developmental band.

    default_unit  unit assumed when the number carries none (default 'years',
                  since '3+' in a toy department means 3 years)
    bins          [[label, lo, hi], ...] IN MONTHS; bins_ref by reference
    months        true = emit the raw age in months instead of the band
    """
    unite = str(ctx.params.get("default_unit", "years")).lower()
    bins = ctx.params.get("bins") or _get_rules_list(ctx, ctx.params.get("bins_ref"))
    brut = bool(ctx.params.get("months", False))
    out = []
    for v in vals:
        mois = parse_age_min_months(v, unite)
        if mois is None:
            ctx.stats[f"{ctx.relation}:age_rejete"] += 1
            continue
        ctx.stats[f"{ctx.relation}:age_lu"] += 1
        if brut or not bins:
            out.append("%d months" % int(round(mois)))
            continue
        label = bin_value(mois, bins)
        if label:
            out.append(label)
        else:
            ctx.stats[f"{ctx.relation}:hors_classes"] += 1
    return out


# =============================================================================
# Lexical extraction: reading the title when the record is empty
# =============================================================================
# On sparse categories the record is nearly empty while the title carries the
# information. It is read with a CLOSED LEXICON: a term absent from it
# produces nothing, so the values of these relations are exactly those of the
# published lexicon, which also normalises them ('tee', 'tshirt', 't-shirt'
# -> one label).
#
# Matching uses an N-GRAM INDEX rather than a regex alternation: the text is
# split into words once, then n-grams are looked up in a dictionary. Cost is
# independent of the lexicon size, and a term can no longer fire inside a
# word ('tie' does not match 'Aesthetic').

_MORCEAU = re.compile(r"[A-Za-z0-9]+")


class Lexique:
    """N-gram index: normalised key -> label."""

    __slots__ = ("index", "n_max")

    def __init__(self, index: Dict[str, str], n_max: int):
        self.index = index
        self.n_max = n_max

    def __bool__(self):
        return bool(self.index)


def compile_lexicon(entrees: Dict[str, str]) -> Lexique:
    """Builds the n-gram index of a lexicon {term: label}.

    A multi-word term is registered under two forms: the n-gram ('t shirt')
    and its concatenation ('tshirt'), so that 't-shirt', 't shirt' and
    'tshirt' are the same term with no work at matching time.
    """
    index: Dict[str, str] = {}
    n_max = 1
    for terme, libelle in entrees.items():
        morceaux = _MORCEAU.findall(str(terme).lower())
        if not morceaux:
            continue
        libelle = str(libelle)
        index.setdefault(" ".join(morceaux), libelle)
        if len(morceaux) > 1:
            n_max = max(n_max, len(morceaux))
            index.setdefault("".join(morceaux), libelle)
    return Lexique(index, n_max)


def _lexicon(ctx, ref, entrees_directes=None):
    """Indexed lexicon, cached: building it for every product would cost more
    than the matching itself."""
    cle = ("lexique", ctx.relation, ref, id(entrees_directes))

    def factory():
        entrees = dict(entrees_directes or {})
        depuis_regles = _resolve_rules(ctx.rules, ref, dict) or {}
        entrees.update({str(k).lower(): v for k, v in depuis_regles.items()})
        return compile_lexicon(entrees)

    return _cache_op(ctx, cle, factory)


def _find_terms(lex: Lexique, texte: str) -> List[Tuple[int, str, str]]:
    """[(position, label, matched text)] of the lexicon terms in the text.

    Greedy left-to-right matching, longest n-gram first: 'Dress Shoes' gives
    the shoe and consumes both words, so that 'dress' can no longer fire
    behind it. That is what replaces the 'longest term first' rule of the
    regex version.
    """
    trouves: List[Tuple[int, str, str]] = []
    if not lex.index:
        return trouves
    mots = [(m.start(), m.end(), m.group(0).lower())
            for m in _MORCEAU.finditer(texte)]
    i, n = 0, len(mots)
    while i < n:
        pris = 0
        for taille in range(min(lex.n_max, n - i), 0, -1):
            cle = " ".join(mots[j][2] for j in range(i, i + taille))
            libelle = lex.index.get(cle)
            if libelle is not None:
                debut, fin = mots[i][0], mots[i + taille - 1][1]
                trouves.append((debut, libelle, texte[debut:fin]))
                pris = taille
                break
        i += pris if pris else 1
    return trouves


@op("match_lexicon")
def _op_match_lexicon(vals, ctx):
    """Keeps from a value only the terms of a closed lexicon that it contains.

    rules_ref   lexicon {term: label}, terms in lowercase
    mode        all (default) | dominant | head | first
                'head' keeps only the RIGHTMOST term: in English the head of a
                compound noun is on the right, so 'T-Shirt Dress' is a dress
                and 'Dress Shoes' a shoe.
                'dominant' keeps the most REPEATED label in the value, and
                breaks ties on the right. Sales titles pile up keywords and
                the real type comes back several times: 'Workout Shorts Yoga
                Sport Fitness Short Pant' is a short, which 'head' classified
                as trousers because 'Pant' closed the march.
    max_terms   maximum number of labels emitted (default 3)
    """
    lex = _lexicon(ctx, ctx.params.get("rules_ref"), ctx.params.get("lexicon"))
    mode = str(ctx.params.get("mode", "all")).lower()
    maxi = int(ctx.params.get("max_terms", 3))
    out: List[str] = []
    for v in vals:
        trouves = _find_terms(lex, v)
        if not trouves:
            ctx.stats[f"{ctx.relation}:lexique_muet"] += 1
            continue
        if mode == "head":
            trouves = [max(trouves, key=lambda t: t[0])]
        elif mode == "first":
            trouves = [min(trouves, key=lambda t: t[0])]
        elif mode == "dominant":
            trouves = [(_dominant_position(trouves), _dominant(trouves), "")]
        ctx.stats[f"{ctx.relation}:lexique_touche"] += 1
        for _pos, libelle, _brut in trouves:
            if libelle not in out:
                out.append(libelle)
    return out[:maxi] if maxi else out


def _dominant(trouves):
    """Most repeated label, ties broken by the rightmost position."""
    compte: Counter = Counter(l for _p, l, _b in trouves)
    derniere = {l: p for p, l, _b in trouves}
    return max(compte, key=lambda l: (compte[l], derniere[l]))


def _dominant_position(trouves):
    gagnant = _dominant(trouves)
    return max(p for p, l, _b in trouves if l == gagnant)


@op("match_subtype")
def _op_match_subtype(vals, ctx):
    """Product sub-type, looked up IN the sub-lexicon of its type.

    The lexicon is nested: {type: {term: sub-type}}. Looking 'boot' up
    everywhere would produce absurdities: 'Boot Cut Jeans' is not a boot, nor
    is 'Bootleg Yoga Pant'. The product type is determined first, then only
    the sub-types that belong to it are looked up.

    The operation emits the sub-type at PRODUCT level and additionally records
    the taxonomy edge (type, relation, sub-type), written once at the end of
    the file, which links two products of the same department without going
    through their common type.

    type_rules_ref   lexicon of the types (the same as hasProductType)
    rules_ref        nested lexicon of the sub-types
    taxo_relation    name of the taxonomy relation (default hasSubtype)
    """
    lex_type = _lexicon(ctx, ctx.params.get("type_rules_ref"))
    imbrique = _resolve_rules(ctx.rules, ctx.params.get("rules_ref"), dict) or {}
    taxo_rel = str(ctx.params.get("taxo_relation", "hasSubtype"))
    maxi = int(ctx.params.get("max_terms", 2))

    out: List[str] = []
    for v in vals:
        types = _find_terms(lex_type, v)
        if not types:
            continue
        # same arbitration as 'dominant' in match_lexicon: the sub-type must
        # be looked up under the SAME type as the one that will be published,
        # otherwise the two relations contradict each other
        type_produit = _dominant(types)
        sous = imbrique.get(type_produit)
        if not sous:
            continue
        lex_sous = _cache_op(
            ctx, ("soustype", type_produit),
            lambda s=sous: compile_lexicon({str(k).lower(): x
                                             for k, x in s.items()}))
        for _pos, libelle, _brut in _find_terms(lex_sous, v):
            if libelle in out:
                continue
            out.append(libelle)
            ctx.stats[f"{ctx.relation}:sous_type"] += 1
            if ctx.pass_no == 2:
                ctx.taxo.add((type_produit, taxo_rel, libelle))
    return out[:maxi] if maxi else out


# Letter sizes. Single-letter forms are only accepted in UPPERCASE in the
# original title: a lowercase 's' is almost always the end of a word or a
# possessive ("Women's"), never a size.
_TAILLES_LETTRES = {
    "xxxs": "XXXS", "xxs": "XXS", "xs": "XS", "x-small": "XS", "extra small": "XS",
    "s": "S", "small": "S",
    "m": "M", "medium": "M", "med": "M",
    "l": "L", "large": "L",
    "xl": "XL", "x-large": "XL", "extra large": "XL", "1x": "XL",
    "xxl": "XXL", "xx-large": "XXL", "2x": "XXL", "2xl": "XXL",
    "xxxl": "XXXL", "xxx-large": "XXXL", "3x": "XXXL", "3xl": "XXXL",
    "3x-large": "XXXL",
    "xxxxl": "XXXXL", "4x": "XXXXL", "4xl": "XXXXL", "4x-large": "XXXXL",
    "5x": "XXXXXL", "5xl": "XXXXXL",
    "one size": "One Size", "onesize": "One Size", "free size": "One Size",
    "plus size": "Plus Size",
}

_TAILLE_MESUREE = re.compile(
    r"(?<![A-Za-z0-9.])(?P<n>\d{1,4}(?:\.\d{1,2})?)\s*"
    r"(?P<u>mm|cm|centimeters?|inch(?:es)?|in|\"|'')(?![A-Za-z])",
    re.IGNORECASE)

_LETTRE_SEULE = re.compile(r"^[a-z]$")


@op("extract_sizes")
def _op_extract_sizes(vals, ctx):
    """Sizes read from a title or a record, brought back to one spelling.

    A title often carries several ('S M L XL'): each gives a triple. Numeric
    sizes carry a unit (inch or millimetre, depending on the seller) and are
    brought back to a single one, failing which '10 inch' and '25.4 cm'
    make two entities for one measurement. A number WITHOUT a unit is
    refused: in a title it is just as often a pack size, a model or a shoe
    size.

    unit      output unit of the numeric sizes (default cm)
    max_terms maximum number of sizes emitted
    """
    cible = str(ctx.params.get("unit", "cm")).lower()
    facteur_cible = _VERS_CM.get(cible, 1.0)
    maxi = int(ctx.params.get("max_terms", 4))
    lex = _cache_op(ctx, ("tailles",),
                    lambda: compile_lexicon(_TAILLES_LETTRES))
    out: List[str] = []

    def add_edge(x):
        if x not in out:
            out.append(x)

    for v in vals:
        for _pos, libelle, brut in _find_terms(lex, v):
            # single letter: require the uppercase form in the original text.
            # Without this rule, the apostrophe of "Women's" produces size S
            # on a third of the catalogue.
            if len(brut) == 1 and _LETTRE_SEULE.match(brut):
                ctx.stats[f"{ctx.relation}:taille_minuscule_ignoree"] += 1
                continue
            add_edge(libelle)
        for m in _TAILLE_MESUREE.finditer(v):
            try:
                n = float(m.group("n"))
            except ValueError:
                continue
            if n <= 0:
                continue
            u = m.group("u").lower()
            u = {'"': "in", "''": "in", "inches": "inch",
                 "centimeter": "cm", "centimeters": "cm"}.get(u, u)
            facteur = _VERS_CM.get(u)
            if facteur is None:
                continue
            valeur = n * facteur / facteur_cible
            add_edge("%s %s" % (_format_number(valeur, 1), cible))
            ctx.stats[f"{ctx.relation}:taille_mesuree"] += 1
    return out[:maxi] if maxi else out


def build_learned_index(counter: Counter, min_count: int = 3,
                        max_len: int = 40, min_mots: int = 2) -> Dict[str, str]:
    """Lexicon LEARNED on the corpus: normalised value -> canonical form.

    Used for OPEN-vocabulary relations, where no hand-written lexicon can do:
    we do not know the list of artists, but the corpus already contains it
    for the products whose record is filled in.

    Two guards. A value must be attested several times, otherwise a typo would
    become a search pattern. And it must have at least two words: looking up a
    one-word name inside a title manufactures false matches wholesale --
    'Live', 'Best' and 'House' are artist names in this corpus as much as they
    are ordinary words.
    """
    idx: Dict[str, str] = {}
    for valeur, n in counter.items():
        if n < min_count or len(valeur) > max_len:
            continue
        mots = _MORCEAU.findall(valeur.lower())
        if len(mots) < min_mots:
            continue
        idx.setdefault(" ".join(mots), valeur)
    return idx


@op("match_learned")
def _op_match_learned(vals, ctx):
    """Looks in free text for the values learned on another relation.

    learn_from  relation whose vocabulary is reused (e.g. hasArtist)
    min_count   number of attestations required (default 3)
    max_len     maximum length of a learned value (default 40)
    max_terms   maximum number of values emitted (default 2)

    Two passes: the source relation must have been counted first.
    """
    if ctx.pass_no == 1:
        return []
    source = str(ctx.params.get("learn_from") or ctx.relation)
    entrees = ctx.valuemap.get(source + "::learned") or {}
    if not entrees:
        return []
    lex = _cache_op(ctx, ("appris", source),
                    lambda: compile_lexicon(entrees))
    maxi = int(ctx.params.get("max_terms", 2))
    out: List[str] = []
    for v in vals:
        for _pos, libelle, _brut in _find_terms(lex, v):
            if libelle not in out:
                out.append(libelle)
                ctx.stats[f"{ctx.relation}:appris_trouve"] += 1
    return out[:maxi] if maxi else out


@op("category_path")
def _op_category_path(vals, ctx):
    """Limits the depth of the category tree and/or keeps only the leaf."""
    depth = ctx.params.get("max_depth")
    leaf_only = bool(ctx.params.get("leaf_only", False))
    skip_first = bool(ctx.params.get("skip_root", False))
    vs = list(vals)
    if skip_first and len(vs) > 1:
        vs = vs[1:]
    if leaf_only and vs:
        return [vs[-1]]
    if depth:
        vs = vs[: int(depth)]
    return vs


# --- Learned canonicalisation (2 passes) -------------------------------------

@op("canon_by_freq")
def _op_canon_by_freq(vals, ctx):
    """Pass 1: counts the variants. Pass 2: replaces each variant by the most
    frequent surface form of its cluster.

    Cluster = same normalised key (case/accents/punctuation ignored). With
    'tight': true, spaces are ignored too ('JK Rowling' == 'J.K.Rowling').
    That is what merges Red/red/RED without a hand-written table.
    """
    if ctx.pass_no == 1:
        c = ctx.collector.setdefault(ctx.relation, Counter())
        for v in vals:
            c[v] += 1
        return vals
    vm = ctx.valuemap.get(ctx.relation, {})
    out = []
    for v in vals:
        nv = vm.get(v, v)
        if nv != v:
            ctx.stats[f"{ctx.relation}:canon"] += 1
        out.append(nv)
    return out


def build_valuemap(counter: Counter, tight: bool = False, min_count: int = 1) -> Dict[str, str]:
    """Chooses a canonical surface form per cluster of variants."""
    clusters: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    keyfn = tight_key if tight else match_key
    for val, cnt in counter.items():
        if cnt < min_count:
            continue
        clusters[keyfn(val)].append((val, cnt))
    vmap: Dict[str, str] = {}
    for _k, group in clusters.items():
        if len(group) == 1:
            continue
        best = max(group, key=lambda vc: (vc[1], _accent_score(vc[0]), -len(vc[0]), vc[0]))[0]
        for val, _c in group:
            if val != best:
                vmap[val] = best
    return vmap


@op("fold_by_prefix")
def _op_fold_by_prefix(vals, ctx):
    """Merges a long value into a shorter and more frequent value of which it
    is the extension: 'Bloomsbury Publishing PLC' -> 'Bloomsbury',
    'Penguin Books' -> 'Penguin'. Aligned on whole words. Two passes."""
    if ctx.pass_no == 1:
        c = ctx.collector.setdefault(ctx.relation, Counter())
        for v in vals:
            c[v] += 1
        return vals
    idx = ctx.valuemap.get(ctx.relation + "::prefix", {})
    if not idx:
        return vals
    out = []
    for v in vals:
        hit = idx.get(v)
        if hit and hit != v:
            ctx.stats[f"{ctx.relation}:folded_prefix"] += 1
            out.append(hit)
        else:
            out.append(v)
    return out


def build_prefix_index(counter: Counter, min_count: int = 3,
                       min_prefix_tokens: int = 1) -> Dict[str, str]:
    """long_value -> short_value, if the short one is a prefix (aligned on
    words) and is at least as frequent."""
    by_prefix: Dict[Tuple[str, ...], Tuple[str, int]] = {}
    items = [(v, c) for v, c in counter.items() if c >= min_count]
    for v, c in items:
        toks = tuple(match_key(v).split())
        if len(toks) < min_prefix_tokens or not toks:
            continue
        cur = by_prefix.get(toks)
        if cur is None or c > cur[1]:
            by_prefix[toks] = (v, c)
    idx: Dict[str, str] = {}
    for v, c in counter.items():
        toks = tuple(match_key(v).split())
        for cut in range(min_prefix_tokens, len(toks)):
            cand = by_prefix.get(toks[:cut])
            if cand and cand[1] >= max(c, min_count):
                idx[v] = cand[0]
                break
    return idx


@op("fold_partial_names")
def _op_fold_partial_names(vals, ctx):
    """Attaches a partial name ('Rowling') to the most frequent full name that
    carries that surname ('J.K. Rowling'), ONLY if the surname is unambiguous
    in the corpus. Requires two passes."""
    if ctx.pass_no == 1:
        c = ctx.collector.setdefault(ctx.relation, Counter())
        for v in vals:
            c[v] += 1
        return vals
    idx = ctx.valuemap.get(ctx.relation + "::lastname", {})
    if not idx:
        return vals
    out = []
    for v in vals:
        if " " not in v.strip():
            hit = idx.get(match_key(v))
            if hit:
                ctx.stats[f"{ctx.relation}:folded_partial"] += 1
                out.append(hit)
                continue
        out.append(v)
    return out


def build_lastname_index(counter: Counter, min_count: int = 2) -> Dict[str, str]:
    """surname -> full name, if and only if a single full name carries it."""
    by_last: Dict[str, Counter] = defaultdict(Counter)
    for val, cnt in counter.items():
        toks = val.split(" ")
        if len(toks) < 2:
            continue
        last = toks[-1]
        if last.rstrip(".").lower() in SUFFIX_TOKENS and len(toks) > 2:
            last = toks[-2]
        by_last[match_key(last)][val] += cnt
    idx: Dict[str, str] = {}
    for last, group in by_last.items():
        if len(group) == 1:
            name, cnt = next(iter(group.items()))
            if cnt >= min_count:
                idx[last] = name
    return idx


# Pairs where the singular and the plural designate TWO different things in a
# product catalogue. Without this list, 'Glasses' (eyewear) would be merged
# into 'Glass' (the material) and 'Shorts' (the garment) into 'Short'.
# The key is the SINGULAR form, in lowercase.
_PLURIELS_DISTINCTS = frozenset({
    "arm", "brief", "custom", "glass", "good", "new", "short",
    "spectacle", "tight", "work",
})


def singular_forms(valeur: str) -> List[str]:
    """Candidate singular forms of a supposedly plural value.

    Only the regular English inflections are handled. Irregular plurals
    (children/child, men/man) are not attempted: guessing them would require
    a lexicon, and doing without only produces an omission, never an abusive
    merge.
    """
    bas = valeur.lower()
    out: List[str] = []
    if bas.endswith("ies") and len(bas) > 5:
        out.append(bas[:-3] + "y")
    if bas.endswith("es") and len(bas) > 4:
        out.append(bas[:-2])
    if bas.endswith("s") and not bas.endswith("ss") and len(bas) > 3:
        out.append(bas[:-1])
    return out


def build_plural_index(counter: Counter, min_count: int = 1,
                       keep_distinct: Iterable[str] = ()) -> Dict[str, str]:
    """losing_form -> winning_form, for singular/plural couples.

    The merge only happens if BOTH forms are attested in the corpus: we never
    singularise nor pluralise an isolated value. That is what makes the
    operation safe on a taxonomic relation, where 'Thriller' and 'Thrillers'
    designate the same genre, without touching a value whose plural does not
    exist.

    The most frequent form wins, as in canon_by_freq; on a tie, the shortest
    then alphabetical order, so that the result does not depend on the order
    in which the file is read.
    """
    exceptions = {str(m).strip().lower() for m in keep_distinct}
    exceptions |= _PLURIELS_DISTINCTS
    par_forme: Dict[str, str] = {}
    for val in counter:
        par_forme.setdefault(val.lower(), val)
    idx: Dict[str, str] = {}
    for pluriel, n_pluriel in counter.items():
        if n_pluriel < min_count:
            continue
        for singulier in singular_forms(pluriel):
            if singulier in exceptions:
                continue
            forme_sg = par_forme.get(singulier)
            if forme_sg is None or forme_sg == pluriel:
                continue
            n_sg = counter[forme_sg]
            if n_sg < min_count:
                continue
            cle_pl = (n_pluriel, -len(pluriel), pluriel)
            cle_sg = (n_sg, -len(forme_sg), forme_sg)
            gagnant, perdant = ((pluriel, forme_sg) if cle_pl > cle_sg
                                else (forme_sg, pluriel))
            idx[perdant] = gagnant
            break
    # A loser may itself be the winner of another couple. The chain is
    # resolved once and for all so that pass 2 stays a plain lookup.
    for perdant in list(idx):
        vu = {perdant}
        cible = idx[perdant]
        while cible in idx and cible not in vu:
            vu.add(cible)
            cible = idx[cible]
        idx[perdant] = cible
    return idx


@op("fold_plural")
def _op_fold_plural(vals, ctx):
    """Merges 'Thrillers' into 'Thriller' when both forms coexist.

    Operation to be enabled explicitly, and only on taxonomic relations
    (category, genre): that is where singular and plural are
    interchangeable. Two passes."""
    if ctx.pass_no == 1:
        c = ctx.collector.setdefault(ctx.relation, Counter())
        for v in vals:
            c[v] += 1
        return vals
    idx = ctx.valuemap.get(ctx.relation + "::plural", {})
    if not idx:
        return vals
    out = []
    for v in vals:
        hit = idx.get(v)
        if hit and hit != v:
            ctx.stats[f"{ctx.relation}:folded_plural"] += 1
            out.append(hit)
        else:
            out.append(v)
    return out


@op("min_support")
def _op_min_support(vals, ctx):
    """Pass 2: drops the values that are too rare (noise / hapax) for the KG."""
    if ctx.pass_no == 1:
        c = ctx.collector.setdefault(ctx.relation + "::support", Counter())
        for v in vals:
            c[v] += 1
        return vals
    n = int(ctx.params.get("n", 2))
    support = ctx.valuemap.get(ctx.relation + "::support_counts", {})
    if not support:
        return vals
    out = []
    for v in vals:
        if int(support.get(v, 0)) >= n:
            out.append(v)
        else:
            ctx.stats[f"{ctx.relation}:below_support"] += 1
    return out


# =============================================================================
# 5. Configuration
# =============================================================================

def _load_yaml_strict(raw: str):
    """Loads YAML while REFUSING repeated keys inside a same block.

    PyYAML silently keeps the last one. That has been expensive: a relation
    defined twice in CDs_and_Vinyl, once by lexicon at the top of the block
    and once by record field below, let the second definition win.
    hasGenre capped at 0.3 % of the products instead of 12.9 %, and nothing
    reported it: neither reading the config, nor the build, nor the audit,
    since the relation did exist.
    """

    class Strict(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        vues = set()
        for cle_node, _valeur in node.value:
            cle = loader.construct_object(cle_node, deep=deep)
            if cle in vues:
                raise SystemExit(
                    "Config invalide : la cle '%s' est definie deux fois dans "
                    "le meme bloc (ligne %d). YAML garderait silencieusement "
                    "la derniere." % (cle, cle_node.start_mark.line + 1))
            vues.add(cle)
        return loader.construct_mapping(node, deep)

    Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
                           mapping)
    return yaml.load(raw, Loader=Strict)


def load_config(path: str) -> dict:
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        if yaml is None:
            raise SystemExit("PyYAML is required to read a YAML config (pip install pyyaml).")
        cfg = _load_yaml_strict(raw)
    else:
        cfg = json.loads(raw)
    if not isinstance(cfg, dict):
        raise SystemExit(f"Config invalide : {path}")
    cfg["_dir"] = str(p.parent.resolve())
    return cfg


def load_rules(cfg: dict) -> Dict[str, Any]:
    """Loads the rule files referenced in 'rules_files' -> {alias: content}."""
    rules: Dict[str, Any] = {}
    base = Path(cfg.get("_dir", "."))
    for alias, rel in (cfg.get("rules_files") or {}).items():
        fp = Path(rel)
        if not fp.is_absolute():
            fp = base / rel
        if not fp.exists():
            log(f"WARNING: rule file not found: {fp}")
            continue
        with open_any(str(fp)) as fh:
            rules[alias] = json.load(fh)
    return rules


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def resolve_category(cfg: dict, category: str) -> dict:
    """Resolves inheritance (`inherit`) and merges with the defaults."""
    cats = cfg.get("categories") or {}
    if category not in cats:
        avail = ", ".join(sorted(k for k in cats if not k.startswith("_")))
        raise SystemExit(f"Category '{category}' absent from the config. Available: {avail}")
    seen: Set[str] = set()
    chain: List[dict] = []
    cur = category
    while cur:
        if cur in seen:
            raise SystemExit(f"Boucle d'heritage sur '{cur}'")
        seen.add(cur)
        node = cats.get(cur)
        if node is None:
            raise SystemExit(f"Parent '{cur}' not found in the config")
        chain.append(node)
        cur = node.get("inherit")
    merged: dict = {}
    for node in reversed(chain):
        merged = _deep_merge(merged, node)
    merged.pop("inherit", None)
    return merged


# --- Source selection --------------------------------------------------------

def match_sources(flat: Dict[str, List[str]], patterns: Sequence[str]) -> List[Tuple[str, List[str]]]:
    """Resolves the source paths, with wildcard support ('details/*Actor*').
    The order of the patterns is authoritative (first one listed wins)."""
    import fnmatch

    hits: List[Tuple[str, List[str]]] = []
    used: Set[str] = set()
    # Amazon writes the same key in several ways inside one product
    # ('Number Of Items' and 'Number of Items', 'Batteries required' and
    # 'Batteries Required?'). The variants are grouped, not dropped.
    lowered: Dict[str, List[str]] = defaultdict(list)
    for k in flat:
        lowered[_source_key(k)].append(k)

    def _take(cles: List[str]) -> None:
        neuves = [k for k in cles if k not in used]
        if not neuves:
            return
        used.update(neuves)
        valeurs: List[str] = []
        for k in neuves:
            valeurs.extend(flat[k])
        hits.append((neuves[0], valeurs))

    for pat in patterns:
        if any(ch in pat for ch in "*?["):
            # pattern: the '?' is a wildcard, it is not trimmed
            pl = pat.lower().strip()
            for kl in list(lowered):
                if fnmatch.fnmatch(kl, pl):
                    _take(lowered[kl])
        else:
            pl = _source_key(pat)
            if pl in lowered:
                _take(lowered[pl])
    return hits


def _source_key(cle: str) -> str:
    """Comparison key for attribute paths: case and trailing '?' ignored.
    'Batteries Required?' and 'Batteries required' name the same attribute."""
    return cle.lower().rstrip("?").strip()


# --- Running a pipeline ------------------------------------------------------

DEFAULT_PRE_OPS = ["fix_encoding", "strip_html", "collapse_ws", "strip_chars"]


def run_pipeline(values: List[str], ops_spec: Sequence[Any], ctx_base: dict,
                 relation: str, pass_no: int, shared: dict) -> Tuple[List[str], Dict[str, List[str]]]:
    """Applies the sequence of operations to a list of raw values."""
    side: Dict[str, List[str]] = {}
    vals = list(values)
    for entry in ops_spec:
        if isinstance(entry, str):
            name, params = entry, {}
        elif isinstance(entry, dict):
            name = entry.get("op") or entry.get("name")
            params = {k: v for k, v in entry.items() if k not in ("op", "name")}
        else:
            continue
        fn = OPS.get(str(name))
        if fn is None:
            raise SystemExit(f"Unknown operation in the config: '{name}' (relation {relation})")
        ctx = OpContext(
            relation=relation,
            params=params,
            rules=shared["rules"],
            person=shared["person"],
            pass_no=pass_no,
            collector=shared["collector"],
            valuemap=shared["valuemap"],
            side=side,
            stats=shared["stats"],
            cache=shared.setdefault("_op_cache", {}),
            taxo=shared.setdefault("taxo", set()),
        )
        vals = fn(vals, ctx)
        vals = [v for v in (collapse_ws(x) for x in vals) if v]
        if not vals and not side:
            break
    # deduplication preserving the order
    seen: Set[str] = set()
    out: List[str] = []
    for v in vals:
        k = match_key(v)
        if k and k not in seen:
            seen.add(k)
            out.append(v)
    return out, side


# =============================================================================
# 6. Extraction of the image / text / subject modalities
# =============================================================================

def extract_subject(rec: dict, spec: dict) -> Optional[str]:
    field_names = spec.get("fields") or [spec.get("field", "title")]
    raw = None
    for f in field_names:
        v = rec.get(f)
        if isinstance(v, list):
            v = " ".join(str(x) for x in v if x)
        if isinstance(v, str) and v.strip():
            raw = v
            break
    if not raw:
        return None
    s = collapse_ws(strip_html(fix_mojibake(nfkc(raw))))
    s = s.strip(" -|·•,;")
    max_len = int(spec.get("max_len", 200))
    if len(s) > max_len:
        s = s[:max_len].rsplit(" ", 1)[0].rstrip(" ,;-")
    return s or None


def extract_text(rec: dict, spec: dict) -> Optional[str]:
    parts: List[str] = []
    for f in spec.get("fields", ["title", "description"]):
        v = rec.get(f)
        if v is None:
            continue
        if isinstance(v, list):
            v = " ".join(str(x) for x in v if isinstance(x, (str, int, float)))
        if not isinstance(v, str):
            continue
        v = collapse_ws(strip_html(fix_mojibake(nfkc(v))))
        if v:
            parts.append(v)
    if not parts:
        return None
    sep = spec.get("separator", " . ")
    text = collapse_ws(sep.join(parts))
    max_chars = int(spec.get("max_chars", 4000))
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0]
    min_chars = int(spec.get("min_chars", 1))
    return text if len(text) >= min_chars else None


# Generic images served by Amazon when the record has no visual of its own.
# Thousands of products share one placeholder URL, which would give them all
# the same visual vector.
DEFAULT_IMAGE_PLACEHOLDERS = [
    r"/images/G/\d+/digital/video/web/Default_Background_Art",
    r"/no[-_]?image[-_]?available",
    r"/images/G/\d+/x-locale/common/transparent-pixel",
    r"/placeholder\.(?:jpg|png|gif)",
]


def extract_images(rec: dict, spec: dict) -> List[Tuple[str, str]]:
    """Returns [(variant, url)]; the MAIN image is placed first."""
    imgs = rec.get("images")
    if not isinstance(imgs, list):
        return []
    prefer = spec.get("prefer", ["hi_res", "large", "thumb"])
    main_only = bool(spec.get("main_only", True))
    motifs = spec.get("exclude_patterns", DEFAULT_IMAGE_PLACEHOLDERS)
    rx = re.compile("|".join(motifs), re.IGNORECASE) if motifs else None

    out: List[Tuple[str, str]] = []
    ordered = sorted(
        [i for i in imgs if isinstance(i, dict)],
        key=lambda i: 0 if str(i.get("variant", "")).upper() == "MAIN" else 1,
    )
    for item in ordered:
        url = None
        for k in prefer:
            u = item.get(k)
            if not (isinstance(u, str) and u.startswith("http")):
                continue
            if rx is not None and rx.search(u):
                continue                 # generic image: we look for a better one
            url = u
            break
        if url:
            out.append((str(item.get("variant") or "IMG"), url))
        if main_only and out:
            break
    return out


# =============================================================================
# 7. Approximate counters (bounded memory)
# =============================================================================

class DistinctCounter:
    """Counts distinct values with a set of 64-bit hashes, bounded."""

    def __init__(self, cap: int = 4_000_000) -> None:
        self.cap = cap
        self.h: Set[int] = set()
        self.truncated = False

    def add(self, v: str) -> None:
        if len(self.h) >= self.cap:
            self.truncated = True
            return
        self.h.add(hash(v))

    def __len__(self) -> int:
        return len(self.h)

    def value(self) -> Dict[str, Any]:
        return {"distinct": len(self.h), "truncated": self.truncated}


def prune_counter(c: Counter, cap: int) -> int:
    """Prunes the hapax when a counter exceeds the allowed memory capacity."""
    if len(c) <= cap:
        return 0
    removed = 0
    for k in [k for k, v in c.items() if v <= 1]:
        del c[k]
        removed += 1
    return removed


# =============================================================================
# 8. BUILD: construction of the tri-modal dataset
# =============================================================================

@dataclass
class BuildPlan:
    category: str
    cat_cfg: dict
    defaults: dict
    relations: Dict[str, dict]
    needs_two_passes: bool


def make_plan(cfg: dict, category: str) -> BuildPlan:
    defaults = cfg.get("defaults") or {}
    cat_cfg = resolve_category(cfg, category)
    relations: Dict[str, dict] = {}
    # global relations (common to every category) then specific ones
    for rel, spec in (defaults.get("global_relations") or {}).items():
        relations[rel] = dict(spec)
    for rel, spec in (cat_cfg.get("relations") or {}).items():
        if spec is None or spec is False:
            relations.pop(rel, None)          # explicit deactivation
            continue
        relations[rel] = _deep_merge(relations.get(rel, {}), spec)
    def _learns(o) -> bool:
        nom = o if isinstance(o, str) else o.get("op")
        if nom in ("canon_by_freq", "min_support", "fold_partial_names",
                   "fold_by_prefix", "fold_plural"):
            return True
        # split_smart only learns in 'evidence' mode
        return nom == "split_smart" and (
            not isinstance(o, dict) or o.get("mode", "evidence") != "always")

    two = any(any(_learns(o) for o in (spec.get("ops") or []))
              for spec in relations.values())
    return BuildPlan(category, cat_cfg, defaults, relations, two)


def _effective_ops(spec: dict, defaults: dict) -> List[Any]:
    pre = spec.get("pre_ops", defaults.get("pre_ops", DEFAULT_PRE_OPS))
    return list(pre) + list(spec.get("ops") or [])


def _extract_relation_values(flat: Dict[str, List[str]], spec: dict) -> List[str]:
    hits = match_sources(flat, spec.get("sources") or [])
    if not hits:
        return []
    if spec.get("first_source_only", True):
        # priority: first source filled in (avoids duplicating Actors + Starring)
        return list(hits[0][1])
    vals: List[str] = []
    for _k, vs in hits:
        vals.extend(vs)
    return vals


def build_category(cfg: dict, category: str, inputs: Sequence[str], outdir: str,
                   limit: Optional[int] = None, single_pass: bool = False,
                   gzip_out: bool = False, homonyms: str = "exact",
                   counter_cap: int = 3_000_000) -> dict:
    plan = make_plan(cfg, category)
    defaults = plan.defaults
    rules = load_rules(cfg)
    person = PersonNormalizer.from_rules(_person_rules(cfg, plan, rules))

    shared: Dict[str, Any] = {
        "rules": rules,
        "person": person,
        "collector": {},
        "valuemap": {},
        "stats": Counter(),
    }

    id_field = defaults.get("id_field", "parent_asin")
    subject_spec = _deep_merge(defaults.get("subject", {}), plan.cat_cfg.get("subject", {}))
    text_spec = _deep_merge(defaults.get("text", {}), plan.cat_cfg.get("text", {}))
    image_spec = _deep_merge(defaults.get("image", {}), plan.cat_cfg.get("image", {}))

    raw_distinct: Dict[str, DistinctCounter] = defaultdict(DistinctCounter)
    final_distinct: Dict[str, DistinctCounter] = defaultdict(DistinctCounter)
    name_counts: Counter = Counter()

    # generic relation -> specific relations that cover it (see pass 2)
    subsomption: Dict[str, List[str]] = {
        rel: list(spec["subsumed_by"])
        for rel, spec in plan.relations.items() if spec.get("subsumed_by")
    }
    # fallback relation -> relation it completes when that one is empty
    repli: Dict[str, str] = {
        rel: str(spec["fallback_for"])
        for rel, spec in plan.relations.items() if spec.get("fallback_for")
    }

    two_passes = plan.needs_two_passes and not single_pass

    # ---------------- PASS 1: learning ---------------------------------------
    if two_passes:
        log(f"[{category}] pass 1/2: learning the value variants")
        n = 0
        for rec in iter_meta_records(inputs, limit):
            n += 1
            subject = extract_subject(rec, subject_spec)
            if subject:
                name_counts[subject] += 1
            flat = flatten_record(rec)
            for rel, spec in plan.relations.items():
                vals = _extract_relation_values(flat, spec)
                if not vals:
                    continue
                for v in vals:
                    raw_distinct[rel].add(v)
                run_pipeline(vals, _effective_ops(spec, defaults), {}, rel, 1, shared)
            if n % 200_000 == 0:
                log(f"  ... {n:,} products analysed")
                for rel, c in shared["collector"].items():
                    removed = prune_counter(c, counter_cap)
                    if removed:
                        log(f"      elagage {rel}: -{removed:,} hapax")
        log(f"[{category}] pass 1 done: {n:,} products")

        valuemap: Dict[str, Any] = {}
        # Relations whose learned vocabulary is reused by ANOTHER relation
        # (match_learned). This must be known before the loop: the relation
        # that learns and the one that consumes are two different relations.
        besoins_appris: Dict[str, Tuple[int, int]] = {}
        for _r, _spec in plan.relations.items():
            for o in _effective_ops(_spec, defaults):
                if isinstance(o, dict) and o.get("op") == "match_learned":
                    cible = str(o.get("learn_from") or _r)
                    besoins_appris[cible] = (int(o.get("min_count", 3)),
                                             int(o.get("max_len", 40)))
        for rel, spec in plan.relations.items():
            tight = False
            min_sup = 0
            for o in _effective_ops(spec, defaults):
                if isinstance(o, dict) and o.get("op") == "canon_by_freq":
                    tight = bool(o.get("tight", False))
                if isinstance(o, dict) and o.get("op") == "min_support":
                    min_sup = int(o.get("n", 2))
            fold_min = 0
            prefix_min = 0
            prefix_tokens = 1
            plural_min = 0
            plural_keep: List[str] = []
            for o in _effective_ops(spec, defaults):
                name_o = o if isinstance(o, str) else o.get("op")
                if name_o == "fold_partial_names":
                    fold_min = int(o.get("min_count", 2)) if isinstance(o, dict) else 2
                elif name_o == "fold_by_prefix":
                    prefix_min = int(o.get("min_count", 3)) if isinstance(o, dict) else 3
                    prefix_tokens = int(o.get("min_prefix_tokens", 1)) if isinstance(o, dict) else 1
                elif name_o == "fold_plural":
                    plural_min = int(o.get("min_count", 1)) if isinstance(o, dict) else 1
                    plural_min = max(1, plural_min)
                    if isinstance(o, dict):
                        garder_pl = _resolve_rules(
                            shared["rules"], o.get("keep_distinct_ref"), list) or []
                        plural_keep = list(garder_pl) + list(o.get("keep_distinct", []))
            # splitting decisions on ambiguous separators (split_smart)
            for o in _effective_ops(spec, defaults):
                if not (isinstance(o, dict) and o.get("op") == "split_smart"):
                    continue
                entiers = shared["collector"].get(rel + "::wholes")
                if not entiers:
                    continue
                garder = _resolve_rules(shared["rules"], o.get("keep_whole_ref"), list) or []
                garder = list(garder) + list(o.get("keep_whole", []))
                valuemap[rel + "::weaksplit"] = build_weak_split_index(
                    shared["collector"].get(rel + "::atoms") or Counter(),
                    entiers,
                    o.get("weak", _WEAK_SEPS),
                    int(o.get("min_evidence", 3)),
                    int(o.get("min_len", 3)),
                    garder,
                )

            c = shared["collector"].get(rel)
            if c:
                valuemap[rel] = build_valuemap(c, tight=tight)
                if rel in besoins_appris:
                    vm_r = valuemap.get(rel, {})
                    compte_r: Counter = Counter()
                    for val, cnt in c.items():
                        compte_r[vm_r.get(val, val)] += cnt
                    n_min, l_max = besoins_appris[rel]
                    valuemap[rel + "::learned"] = build_learned_index(
                        compte_r, n_min, l_max)
                if fold_min or prefix_min or plural_min:
                    vm = valuemap.get(rel, {})
                    canon_counts: Counter = Counter()
                    for val, cnt in c.items():
                        canon_counts[vm.get(val, val)] += cnt
                    if plural_min:
                        valuemap[rel + "::plural"] = build_plural_index(
                            canon_counts, plural_min, plural_keep)
                    if fold_min:
                        valuemap[rel + "::lastname"] = build_lastname_index(canon_counts, fold_min)
                    if prefix_min:
                        valuemap[rel + "::prefix"] = build_prefix_index(
                            canon_counts, prefix_min, prefix_tokens)
            if min_sup:
                cs = shared["collector"].get(rel + "::support")
                if cs:
                    vm = valuemap.get(rel, {})
                    agg: Counter = Counter()
                    for val, cnt in cs.items():
                        agg[vm.get(val, val)] += cnt
                    valuemap[rel + "::support_counts"] = dict(agg)
        shared["valuemap"] = valuemap
        shared["collector"] = {}
    else:
        shared["valuemap"] = {}

    # ---------------- PASS 2: writing ----------------------------------------
    os.makedirs(outdir, exist_ok=True)
    ext = ".gz" if gzip_out else ""
    opener = (lambda p: gzip.open(p, "wt", encoding="utf-8")) if gzip_out \
        else (lambda p: open(p, "w", encoding="utf-8"))

    paths = {
        "kg": os.path.join(outdir, f"{category}.kg{ext}"),
        "link": os.path.join(outdir, f"{category}.link{ext}"),
        "text": os.path.join(outdir, f"{category}.text{ext}"),
        "image": os.path.join(outdir, f"{category}.image{ext}"),
    }

    stats = shared["stats"]
    n_products = n_kept = n_triples = n_text = n_img = 0
    rel_triples: Counter = Counter()
    derived_relations: Set[str] = set()
    per_product_triples = 0

    log(f"[{category}] pass {'2/2' if two_passes else '1/1'}: writing the dataset")
    with opener(paths["kg"]) as f_kg, opener(paths["link"]) as f_link, \
         opener(paths["text"]) as f_text, opener(paths["image"]) as f_img:

        f_kg.write("# subject\trelation\tobject\n")
        f_link.write("# subject\tproduct_id\tn_homonyms\tproduct_name\n")
        f_text.write("# product_id\ttext\n")
        f_img.write("# product_id\tvariant\turl\n")

        for rec in iter_meta_records(inputs, limit):
            n_products += 1
            pid = str(rec.get(id_field) or rec.get("asin") or "").strip()
            subject = extract_subject(rec, subject_spec)
            if not pid or (not subject and defaults.get("drop_products_without_subject", True)):
                stats["products_dropped_no_subject"] += 1
                continue

            flat = flatten_record(rec)
            triples: List[Tuple[str, str]] = []
            for rel, spec in plan.relations.items():
                vals = _extract_relation_values(flat, spec)
                if not vals:
                    continue
                if not two_passes:
                    for v in vals:
                        raw_distinct[rel].add(v)
                out, side = run_pipeline(vals, _effective_ops(spec, defaults), {}, rel, 2, shared)
                for v in out:
                    triples.append((rel, v))
                for srel, svals in side.items():
                    derived_relations.add(srel)
                    for v in dict.fromkeys(svals):
                        triples.append((srel, collapse_ws(v)))

            # Fallback: published under the name of the relation it completes,
            # and only where that relation produced nothing for this product,
            # so that an approximate reading never contradicts a filled-in
            # record.
            if repli:
                presentes = {rel for rel, _v in triples}
                gardes = []
                for rel, val in triples:
                    cible = repli.get(rel)
                    if cible is None:
                        gardes.append((rel, val))
                    elif cible in presentes:
                        shared["stats"][f"{rel}:repli_inutile"] += 1
                    else:
                        gardes.append((cible, val))
                        shared["stats"][f"{cible}:repli"] += 1
                triples = gardes

            # Subsumption: a generic relation only keeps what the specific
            # relations have not already said. 'details/Contributor' lists
            # actors, director and producers alike.
            if subsomption:
                deja: Dict[str, Set[str]] = defaultdict(set)
                for rel, val in triples:
                    deja[rel].add(match_key(val))
                gardes = []
                for rel, val in triples:
                    couvrantes = subsomption.get(rel)
                    if couvrantes and any(match_key(val) in deja.get(sp, ())
                                          for sp in couvrantes):
                        shared["stats"][f"{rel}:subsume"] += 1
                        continue
                    gardes.append((rel, val))
                triples = gardes

            min_rel = int(plan.cat_cfg.get("min_relations", defaults.get("min_relations", 1)))
            if len(triples) < min_rel:
                stats["products_dropped_too_few_relations"] += 1
                continue

            n_kept += 1
            subj_out = tsv_safe(subject)
            # The product name does NOT identify a product, hence the
            # identifier in the subject:
            #   disambiguate: always -> always 'Name ##ASIN'
            #   disambiguate: true   -> only the shared names
            #   disambiguate: false  -> never (ambiguous subject)
            mode_desamb = subject_spec.get("disambiguate")
            if mode_desamb == "always" or (
                    mode_desamb and name_counts.get(subject, 0) > 1):
                subj_out = f"{subj_out} ##{pid}"

            seen_t: Set[Tuple[str, str]] = set()
            for rel, val in triples:
                key = (rel, match_key(val))
                if key in seen_t:
                    continue
                seen_t.add(key)
                f_kg.write(f"{subj_out}\t{rel}\t{tsv_safe(val)}\n")
                final_distinct[rel].add(val)
                rel_triples[rel] += 1
                n_triples += 1
            per_product_triples += len(seen_t)

            homo = name_counts.get(subject, 0) if (homonyms == "exact" and two_passes) else 0
            # The .link carries the subject AS IT APPEARS in the .kg, then the
            # PROPER name: without that last column, disambiguating would
            # deprive the user of the displayable label of the product.
            f_link.write(f"{subj_out}\t{pid}\t{homo if homo else ''}\t"
                         f"{tsv_safe(subject)}\n")

            txt = extract_text(rec, text_spec)
            if txt:
                f_text.write(f"{pid}\t{tsv_safe(txt)}\n")
                n_text += 1

            for variant, url in extract_images(rec, image_spec):
                f_img.write(f"{pid}\t{variant}\t{url}\n")
                n_img += 1

            if n_products % 200_000 == 0:
                log(f"  ... {n_products:,} products processed, {n_triples:,} triples")

        # TAXONOMY edges: their subject is a product type, not a product, so
        # they are written once at the end of the file, after a comment line.
        taxo = sorted(shared.get("taxo") or ())
        if taxo:
            f_kg.write("# taxonomie : subject = product type, not a product\n")
            for sujet, rel, objet in taxo:
                f_kg.write(f"{tsv_safe(sujet)}\t{rel}\t{tsv_safe(objet)}\n")
                rel_triples[rel] += 1
                n_triples += 1
            n_taxo = len(taxo)
            log(f"  taxonomy: {n_taxo:,} type -> subtype edges")
        else:
            n_taxo = 0

    # ---------------- report -------------------------------------------------
    if two_passes and shared["valuemap"]:
        vm_path = os.path.join(outdir, f"{category}.valuemap.json")
        with open(vm_path, "w", encoding="utf-8") as fh:
            json.dump(
                {k: v for k, v in shared["valuemap"].items() if not k.endswith(("::support_counts", "::lastname", "::prefix", "::learned"))},
                fh, ensure_ascii=False, indent=1, sort_keys=True,
            )
        paths["valuemap"] = vm_path

    report = {
        "category": category,
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "script_version": __version__,
        "inputs": list(inputs),
        "two_passes": two_passes,
        "products_read": n_products,
        "products_kept": n_kept,
        "triples": n_triples,
        # taxonomy edges included in 'triples' above, but counted separately
        # since their subject is not a product
        "taxonomy_edges": n_taxo,
        "avg_triples_per_product": round(per_product_triples / n_kept, 2) if n_kept else 0,
        "text_lines": n_text,
        "image_lines": n_img,
        "homonym_names": sum(1 for v in name_counts.values() if v > 1) if two_passes else None,
        "relations": {
            rel: {
                "triples": rel_triples.get(rel, 0),
                "distinct_raw": raw_distinct[rel].value()["distinct"] if rel in raw_distinct else 0,
                "distinct_normalized": final_distinct[rel].value()["distinct"] if rel in final_distinct else 0,
                "reduction_pct": _reduction(
                    raw_distinct[rel].value()["distinct"] if rel in raw_distinct else 0,
                    final_distinct[rel].value()["distinct"] if rel in final_distinct else 0,
                ),
                "sources": plan.relations.get(rel, {}).get("sources", ["<derivee>"]),
            }
            for rel in list(plan.relations) + sorted(derived_relations)
        },
        "op_stats": dict(sorted(stats.items())),
        "files": paths,
    }
    rp = os.path.join(outdir, f"{category}.report.json")
    with open(rp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    log(f"[{category}] done: {n_kept:,} products, {n_triples:,} triples -> {outdir}")
    return report


def _reduction(before: int, after: int) -> float:
    if not before:
        return 0.0
    return round(100.0 * (before - after) / before, 1)


def _person_rules(cfg: dict, plan: BuildPlan, rules: Dict[str, Any]) -> dict:
    """Assembles the 'people' rules applicable to the category."""
    ref = plan.cat_cfg.get("person_rules") or (cfg.get("defaults") or {}).get("person_rules")
    merged: Dict[str, Any] = {"person_mappings": {}, "person_invalid_values": [],
                              "person_invalid_patterns": [], "person_mc_mac_exceptions": []}
    refs = [ref] if isinstance(ref, str) else list(ref or [])
    for r in refs:
        node: Any = rules
        for part in str(r).split("."):
            node = node.get(part) if isinstance(node, dict) else None
            if node is None:
                break
        if not isinstance(node, dict):
            continue
        merged["person_mappings"].update(node.get("person_mappings", {}) or {})
        for k in ("person_invalid_values", "person_invalid_patterns", "person_mc_mac_exceptions"):
            merged[k].extend(node.get(k, []) or [])
    return merged


# =============================================================================
# 9. DISCOVER: attribute proposal + redundancy detection
# =============================================================================

_VOCAB_GENERIQUE = {"yes", "no", "true", "false", "oui", "non", "enabled",
                    "not enabled", "unknown", "none", "n a", "0", "1", "on", "off"}

DEFAULT_KEY_BLACKLIST = [
    "parent_asin", "asin", "price", "average_rating", "rating_number", "bought_together",
    "details/asin", "details/isbn*", "details/upc", "details/ean", "details/item model number",
    "details/best sellers rank*", "details/date first available", "details/customer reviews",
    "details/product dimensions", "details/package dimensions", "details/item weight",
    "details/shipping weight", "details/item dimensions*", "details/batteries",
    "details/manufacturer part*", "details/country of origin", "details/discontinued*",
    "features", "description", "title", "images*", "videos*",
    # 'author/avatar' is an image URL, well covered on Books and therefore
    # wrongly recommended by the purely statistical scoring.
    "author/avatar", "author/about", "*/avatar", "*url*", "*/image*",
    "subtitle",          # handled apart: carries the book format, not an attribute
]


def _is_blacklisted(key: str) -> bool:
    import fnmatch
    kl = key.lower()
    return any(fnmatch.fnmatch(kl, p) for p in DEFAULT_KEY_BLACKLIST)


def discover(inputs: Sequence[str], sample: int = 50_000, top_pairs: int = 60,
             jaccard_min: float = 0.30, max_examples: int = 8) -> dict:
    key_docs: Counter = Counter()
    key_values: Dict[str, Counter] = defaultdict(Counter)   # ATOMIC values
    key_raw: Dict[str, Counter] = defaultdict(Counter)      # raw values
    key_len: Dict[str, List[int]] = defaultdict(list)
    key_multi: Counter = Counter()
    n = 0
    for rec in iter_meta_records(inputs, sample):
        n += 1
        flat = flatten_record(rec)
        for k, vals in flat.items():
            key_docs[k] += 1
            for v in vals[:10]:
                v = collapse_ws(strip_html(fix_mojibake(nfkc(v))))
                if not v:
                    continue
                key_raw[k][v] += 1
                if len(key_len[k]) < 5000:
                    key_len[k].append(len(v))
                atoms = _atomize(v)
                if len(atoms) > 1:
                    key_multi[k] += 1
                for a in atoms:
                    key_values[k][a] += 1
    if not n:
        raise SystemExit("Aucun enregistrement lu.")

    attrs = []
    for k, docs in key_docs.most_common():
        vals = key_values[k]
        multi = key_multi[k] > 0.2 * docs
        distinct_norm = len({match_key(v) for v in vals})
        coverage = docs / n
        uniqueness = distinct_norm / max(docs, 1)
        avg_len = (sum(key_len[k]) / len(key_len[k])) if key_len[k] else 0
        reasons = []
        keep = True
        if _is_blacklisted(k):
            keep, r = False, "blacklist (identifier / free text / dedicated modality)"
            reasons.append(r)
        if coverage < 0.02:
            keep = False
            reasons.append(f"low coverage ({coverage:.1%})")
        if distinct_norm <= 1:
            keep = False
            reasons.append("constant value")
        if uniqueness > 0.60 and distinct_norm > 50 and not multi:
            keep = False
            reasons.append(f"quasi-identifier (uniqueness {uniqueness:.0%})")
        if avg_len > 80:
            keep = False
            reasons.append(f"free text (mean length {avg_len:.0f})")
        attrs.append({
            "key": k,
            "coverage": round(coverage, 4),
            "n_products": docs,
            "multivalued": multi,
            "distinct_raw": len(key_raw[k]),
            "distinct_normalized": distinct_norm,
            "uniqueness": round(uniqueness, 4),
            "avg_len": round(avg_len, 1),
            "recommended": keep,
            "reasons": reasons,
            "examples": [v for v, _c in key_raw[k].most_common(max_examples)],
            "atomic_examples": [v for v, _c in vals.most_common(max_examples)],
        })

    # --- redundancy between attributes ---------------------------------------
    cand = [a["key"] for a in attrs if a["recommended"]][:top_pairs]
    # vocabularies that prove no kinship between two attributes
    sets = {k: {match_key(v) for v in key_values[k]} for k in cand}
    redundant = []
    for i, a in enumerate(cand):
        for b in cand[i + 1:]:
            sa, sb = sets[a], sets[b]
            if not sa or not sb:
                continue
            # Two boolean fields both amount to {yes, no}: their Jaccard is 1.0
            # without any kinship, so a rich enough vocabulary is required.
            if min(len(sa), len(sb)) < 4 or (sa | sb) <= _VOCAB_GENERIQUE:
                continue
            inter = len(sa & sb)
            if not inter:
                continue
            jac = inter / len(sa | sb)
            cont = inter / min(len(sa), len(sb))          # inclusion of one set in the other
            name_sim = _name_similarity(a, b)
            if jac >= jaccard_min or cont >= 0.75 or name_sim >= 0.8:
                redundant.append({
                    "a": a, "b": b,
                    "jaccard": round(jac, 3),
                    "containment": round(cont, 3),
                    "name_similarity": round(name_sim, 3),
                    "shared_examples": sorted(sa & sb)[:5],
                    "verdict": "FUSION probable" if (jac >= 0.5 or cont >= 0.85) else "a check_category",
                })
    redundant.sort(key=lambda d: -max(d["jaccard"], d["containment"]))

    return {
        "sampled_products": n,
        "inputs": list(inputs),
        "attributes": attrs,
        "redundancy_candidates": redundant,
        "suggested_yaml": _suggest_yaml(attrs, redundant),
    }


_ATOM_SEPS = re.compile(r"\s*[,;|]\s*")


def _atomize(v: str) -> List[str]:
    """Splits a multi-valued value ('A, B, C') to compare SETS of values and
    not joined strings, so that details/Actors and details/Starring are seen
    to carry the same information."""
    if not _ATOM_SEPS.search(v):
        return [v]
    parts = [p for p in (x.strip() for x in _ATOM_SEPS.split(v)) if len(p) >= 2]
    return parts if len(parts) > 1 else [v]


def _name_similarity(a: str, b: str) -> float:
    ta = set(match_key(a.replace("/", " ")).split())
    tb = set(match_key(b.replace("/", " ")).split())
    ta -= {"details"}
    tb -= {"details"}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _rel_name(key: str) -> str:
    base = key.split("/")[-1]
    toks = [t for t in re.split(r"[^A-Za-z0-9]+", base) if t]
    if not toks:
        return "hasAttribute"
    return "has" + "".join(t[0].upper() + t[1:].lower() for t in toks)


def _suggest_yaml(attrs: List[dict], redundant: List[dict]) -> str:
    multi = {a["key"] for a in attrs if a.get("multivalued")}
    groups: Dict[str, List[str]] = {}
    merged: Set[str] = set()
    for r in redundant:
        if r["verdict"] != "FUSION probable":
            continue
        a, b = r["a"], r["b"]
        target = None
        for g, members in groups.items():
            if a in members or b in members:
                target = g
                break
        if target:
            groups[target] = sorted(set(groups[target]) | {a, b})
        else:
            groups[_rel_name(a)] = sorted({a, b})
        merged.update({a, b})
    for a in attrs:
        if a["recommended"] and a["key"] not in merged:
            groups.setdefault(_rel_name(a["key"]), [a["key"]])
    lines = ["    relations:"]
    for rel, srcs in groups.items():
        lines.append(f"      {rel}:")
        lines.append("        sources: [" + ", ".join(f'"{s}"' for s in srcs) + "]")
        lines.append("        ops:")
        if any(sc in multi for sc in srcs):
            lines.append('          - {op: split_multi, separators: [",", ";", "|"]}')
        lines.append("          - {op: drop_values, rules_ref: common.invalid_values}")
        lines.append("          - {op: canon_by_freq}")
        lines.append("          - {op: max_values, n: 5}")
    return "\n".join(lines)


# =============================================================================
# 10. CLI
# =============================================================================

def expand_inputs(patterns: Sequence[str]) -> List[str]:
    import glob as _glob
    out: List[str] = []
    for p in patterns:
        if any(ch in p for ch in "*?["):
            out.extend(sorted(_glob.glob(p)))
        elif os.path.isdir(p):
            for ext in ("*.jsonl", "*.jsonl.gz", "*.json.gz", "*.tsv", "*.csv"):
                out.extend(sorted(_glob.glob(os.path.join(p, ext))))
        else:
            out.append(p)
    missing = [p for p in out if not os.path.exists(p)]
    if missing:
        raise SystemExit("File(s) not found: " + ", ".join(missing))
    if not out:
        raise SystemExit("No input file resolved.")
    return out


def guess_category(path: str, known: Sequence[str]) -> Optional[str]:
    base = os.path.basename(path)
    for cat in sorted(known, key=len, reverse=True):
        if cat.lower() in base.lower():
            return cat
    # fallback on the McAuley naming convention: meta_<CAT>.jsonl[.gz|.zst]
    m = re.match(r"^meta_(.+?)\.jsonl(\.(gz|zst))?$", base, re.I)
    return m.group(1) if m else None


def cmd_build(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    known = [k for k in (cfg.get("categories") or {}) if not k.startswith("_")]
    reports = []
    if args.all:
        jobs: Dict[str, List[str]] = defaultdict(list)
        for p in expand_inputs(args.input):
            cat = args.category or guess_category(p, known)
            if not cat:
                log(f"WARNING: category not deduced for {p}, skipped.")
                continue
            jobs[cat].append(p)
        if not jobs:
            raise SystemExit("No category deduced from the file names.")
    else:
        if not args.category:
            raise SystemExit("--category is required (or use --all with standard file names).")
        jobs = {args.category: expand_inputs(args.input)}

    for cat, files in jobs.items():
        reports.append(build_category(
            cfg, cat, files, args.out,
            limit=args.limit, single_pass=args.single_pass,
            gzip_out=args.gzip, homonyms=args.homonyms,
            counter_cap=args.counter_cap,
        ))
    summary = os.path.join(args.out, "build_summary.json")
    os.makedirs(args.out, exist_ok=True)
    with open(summary, "w", encoding="utf-8") as fh:
        json.dump(reports, fh, ensure_ascii=False, indent=2)
    print(_format_summary(reports))
    return 0


def _format_summary(reports: List[dict]) -> str:
    lines = []
    for r in reports:
        lines.append("=" * 78)
        lines.append(f"CATEGORIE : {r['category']}")
        lines.append(
            f"  produits lus {r['products_read']:,} | retenus {r['products_kept']:,} "
            f"| triplets {r['triples']:,} | moy/produit {r['avg_triples_per_product']}"
        )
        lines.append(f"  {'RELATION':<26}{'TRIPLETS':>12}{'VAL. BRUTES':>14}{'VAL. NORM.':>13}{'REDUC.':>9}")
        lines.append("  " + "-" * 74)
        for rel, s in sorted(r["relations"].items(), key=lambda kv: -kv[1]["triples"]):
            if not s["triples"]:
                continue
            lines.append(
                f"  {rel:<26}{s['triples']:>12,}{s['distinct_raw']:>14,}"
                f"{s['distinct_normalized']:>13,}{s['reduction_pct']:>8}%"
            )
    return "\n".join(lines)


def cmd_discover(args: argparse.Namespace) -> int:
    files = expand_inputs(args.input)
    res = discover(files, sample=args.sample, jaccard_min=args.jaccard)
    os.makedirs(args.out, exist_ok=True)
    name = args.category or (guess_category(files[0], []) or "UNKNOWN")
    path = os.path.join(args.out, f"{name}.discovery.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=2)

    print(f"\nSample: {res['sampled_products']:,} products\n")
    print(f"{'ATTRIBUTE':<42}{'COV.':>8}{'DISTINCT':>10}{'UNIQ.':>8}  KEPT")
    print("-" * 82)
    for a in res["attributes"][:60]:
        flag = "YES" if a["recommended"] else "no"
        print(f"{a['key'][:41]:<42}{a['coverage']:>7.1%}{a['distinct_normalized']:>10,}"
              f"{a['uniqueness']:>7.0%}  {flag}"
              + ("" if a["recommended"] else f"   ({a['reasons'][0]})"))
    if res["redundancy_candidates"]:
        print("\nREDUNDANCIES DETECTED (relations that are candidates for merging)")
        print("-" * 82)
        for r in res["redundancy_candidates"][:25]:
            print(f"  {r['a']}  <->  {r['b']}")
            print(f"      jaccard={r['jaccard']} inclusion={r['containment']} "
                  f"name={r['name_similarity']}  => {r['verdict']}")
    print(f"\nConfig skeleton to paste under categories.{name}:\n")
    print(res["suggested_yaml"])
    print(f"\nFull detail: {path}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    rules = load_rules(cfg)
    cats = [k for k in (cfg.get("categories") or {}) if not k.startswith("_")]
    problems = 0
    for cat in (["" ] if False else cats):
        try:
            plan = make_plan(cfg, cat)
        except SystemExit as e:
            print(f"[{cat}] ERROR: {e}")
            problems += 1
            continue
        for rel, spec in plan.relations.items():
            if not spec.get("sources"):
                print(f"[{cat}/{rel}] ERROR: no source defined")
                problems += 1
            for o in _effective_ops(spec, plan.defaults):
                name = o if isinstance(o, str) else o.get("op")
                if name not in OPS:
                    print(f"[{cat}/{rel}] ERROR: unknown operation '{name}'")
                    problems += 1
                if isinstance(o, dict) and o.get("rules_ref"):
                    node: Any = rules
                    for part in str(o["rules_ref"]).split("."):
                        node = node.get(part) if isinstance(node, dict) else None
                        if node is None:
                            break
                    if node is None:
                        print(f"[{cat}/{rel}] ERROR: rules_ref not found '{o['rules_ref']}'")
                        problems += 1
        print(f"[{cat}] {len(plan.relations)} relations, "
              f"{'2 passes' if plan.needs_two_passes else '1 pass'}")
    # consistency of the people tables
    for alias, node in rules.items():
        if isinstance(node, dict) and node.get("person_mappings"):
            clusters = build_person_clusters(node["person_mappings"])
            heads = len(set(clusters.values()))
            cyc = sum(1 for k, v in node["person_mappings"].items()
                      if node["person_mappings"].get(v) is not None)
            print(f"[rules:{alias}] {len(node['person_mappings'])} mappings -> "
                  f"{heads} canonical forms, {len(clusters)} variants attached "
                  f"({cyc} chains/cycles resolved)")
    print("\nOK" if not problems else f"\n{problems} problem(s) detected")
    return 1 if problems else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    global LOG_LEVEL
    ap = argparse.ArgumentParser(
        prog="build_trimodal_kg.py",
        description="Builds tri-modal Amazon datasets (image / text / KG).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples
--------
  # 1. Explore an unknown category and propose the attributes
  python build_trimodal_kg.py discover -i meta_Automotive.jsonl.gz --sample 50000 -o out/

  # 2. Build the dataset of a category
  python build_trimodal_kg.py build -c configs/categories.yaml \\
      --category Books -i meta_Books.jsonl.gz -o out/

  # 3. Build every category at once (category deduced from the file name)
  python build_trimodal_kg.py build -c configs/categories.yaml --all -i "data/meta_*.jsonl.gz" -o out/

  # 4. Check the config
  python build_trimodal_kg.py validate -c configs/categories.yaml
""",
    )
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    common_ap = argparse.ArgumentParser(add_help=False)
    common_ap.add_argument("-q", "--quiet", action="store_true")

    b = sub.add_parser("build", parents=[common_ap], help="build the tri-modal dataset")
    b.add_argument("-c", "--config", required=True)
    b.add_argument("-i", "--input", nargs="+", required=True,
                   help="meta_*.jsonl(.gz) files, directory or glob pattern")
    b.add_argument("-o", "--out", default="out")
    b.add_argument("--category", help="name of the category in the config")
    b.add_argument("--all", action="store_true",
                   help="deduce the category from the name of each input file")
    b.add_argument("--limit", type=int, help="process only the first N products (test)")
    b.add_argument("--single-pass", action="store_true",
                   help="disables the learning of variants (faster, less precise)")
    b.add_argument("--gzip", action="store_true", help="compress the output files")
    b.add_argument("--homonyms", choices=["exact", "off"], default="exact")
    b.add_argument("--counter-cap", type=int, default=3_000_000,
                   help="max size of the variant counters before hapax pruning")
    b.set_defaults(func=cmd_build)

    d = sub.add_parser("discover", parents=[common_ap], help="propose the relevant attributes of a category")
    d.add_argument("-i", "--input", nargs="+", required=True)
    d.add_argument("-o", "--out", default="out")
    d.add_argument("--category")
    d.add_argument("--sample", type=int, default=50_000)
    d.add_argument("--jaccard", type=float, default=0.30)
    d.set_defaults(func=cmd_discover)

    v = sub.add_parser("validate", parents=[common_ap], help="check the config and the rules")
    v.add_argument("-c", "--config", required=True)
    v.set_defaults(func=cmd_validate)

    args = ap.parse_args(argv)
    if args.quiet:
        LOG_LEVEL = 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
