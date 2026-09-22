# -*- coding: utf-8 -*-
"""Results table: mean +- standard deviation over the seeds, per
(dataset, model), from results/*.json; also picks up the single-seed
Amazon-KG runs already done in the reference logs (test result lines) when
the dataset has not been re-run here.

    .venv/Scripts/python.exe collect_results.py [--md resultats.md]
"""
import argparse
import glob
import json
import os
import re
import statistics

RACINE = os.path.dirname(os.path.abspath(__file__))
LOGS_TENSORREC = r"C:\Users\Bouta\PycharmProjects\TensorRec\log"
METRIQUES = ["recall@20", "ndcg@20", "mrr@20", "hit@20", "precision@20"]
_TEST = re.compile(r"test result: OrderedDict\(\[(.*)\]\)")
_KV = re.compile(r"\('([a-z]+@\d+)', ([0-9.]+)\)")


def from_logs():
    """{(model, dataset): [dict]} of the test results found in the reference logs."""
    out = {}
    for f in glob.glob(os.path.join(LOGS_TENSORREC, "*", "*Amazon-KG-5core*.log")):
        algo = os.path.basename(os.path.dirname(f))
        m = re.search(r"Amazon-KG-5core-(Books|Movies_and_TV)", os.path.basename(f))
        if not m:
            continue
        dataset = "Amazon-KG-5core-" + m.group(1)
        txt = open(f, encoding="utf-8", errors="replace").read()
        t = _TEST.findall(txt)
        if not t:
            continue
        res = {k: float(v) for k, v in _KV.findall(t[-1])}
        if "recall@20" not in res:
            continue
        out.setdefault((algo, dataset), []).append(res)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", default=os.path.join(RACINE, "resultats.md"))
    args = ap.parse_args()
    runs = {}
    for f in glob.glob(os.path.join(RACINE, "results", "*.json")):
        r = json.load(open(f, encoding="utf-8"))
        runs.setdefault((r["algo"], r["dataset"]), []).append(r["test_result"])
    anciens = from_logs()
    for cle, v in anciens.items():
        runs.setdefault(cle, v)          # only if not re-run here

    lignes = ["# Face-a-face Amazon-M3KG / Amazon-KG v2.0 (memes produits, memes interactions)\n",
              "Moyenne +- ecart-type sur les graines ; (n) = nombre de graines. "
              "Les lignes Amazon-KG a une graine viennent des journaux TensorRec.\n"]
    for cat in ("Books", "Movies_and_TV"):
        lignes.append("\n## %s\n" % cat.replace("_and_", " & "))
        lignes.append("| algo | graphe | n | " + " | ".join(METRIQUES) + " |")
        lignes.append("|---|---|---:|" + "---:|" * len(METRIQUES))
        for algo in ["BPR", "CFKG", "CKE", "KGCN", "KGNNLS", "KTUP", "MKR", "RippleNet"]:
            for label, ds in (("M3KG", "M3KG-AKGsubset-" + cat), ("Amazon-KG", "Amazon-KG-5core-" + cat)):
                rs = runs.get((algo, ds))
                if not rs:
                    continue
                cells = []
                for m in METRIQUES:
                    vals = [r[m] for r in rs if m in r]
                    if not vals:
                        cells.append("")
                    elif len(vals) == 1:
                        cells.append("%.4f" % vals[0])
                    else:
                        cells.append("%.4f ± %.4f" % (statistics.mean(vals), statistics.stdev(vals)))
                lignes.append("| %s | %s | %d | %s |" % (algo, label, len(rs), " | ".join(cells)))
    open(args.md, "w", encoding="utf-8").write("\n".join(lignes) + "\n")
    print("\n".join(lignes))
    return 0


if __name__ == "__main__":
    main()
