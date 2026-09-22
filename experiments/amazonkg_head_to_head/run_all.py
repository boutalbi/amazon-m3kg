# -*- coding: utf-8 -*-
"""Head-to-head Amazon-M3KG / Amazon-KG v2.0: same products, same
interactions, only the graph changes. One run = (model, dataset, seed); its
test result is written to results/<model>_<dataset>_<seed>.json as soon as
the run ends, and a run already done is skipped: the script can be restarted
without redoing any work.

    .venv/Scripts/python.exe run_all.py                    # everything still missing
    .venv/Scripts/python.exe run_all.py --datasets M3KG-AKGsubset-Books
    .venv/Scripts/python.exe run_all.py --algos CKE KGCN --seeds 42

The configs are those of the reference environment (same hyperparameters),
only data_path and dataset change; the seed is passed here.
"""
import argparse
import json
import os
import sys
import time
import traceback

os.environ["PYTHONIOENCODING"] = "utf-8"
# CPU threads per worker: evaluation (full ranking) is CPU-bound and three
# workers with 6 threads each fight over 6 cores.
_n = os.environ.get("M3KG_THREADS")
if _n:
    for _v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[_v] = _n
RACINE = os.path.dirname(os.path.abspath(__file__))
os.chdir(RACINE)

ALGOS = ["CFKG", "CKE", "KGCN", "RippleNet"]   # KGNNLS = KGCN on binarised ratings; MKR and KTUP dropped
DATASETS = ["M3KG-AKGsubset-Books", "M3KG-AKGsubset-Movies_and_TV"]
SEEDS = [42, 2024, 7]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--algos", nargs="+", default=ALGOS)
    ap.add_argument("--datasets", nargs="+", default=DATASETS)
    ap.add_argument("--seeds", nargs="+", type=int, default=SEEDS)
    ap.add_argument("--gpu", type=int, default=0, help="-1 = CPU")
    args = ap.parse_args()
    import torch
    if _n:
        torch.set_num_threads(int(_n))
    from recbole.quick_start import run_recbole

    os.makedirs("results", exist_ok=True)
    todo = [(d, a, s) for d in args.datasets for a in args.algos for s in args.seeds]
    for i, (dataset, algo, seed) in enumerate(todo, 1):
        sortie = os.path.join("results", "%s_%s_%d.json" % (algo, dataset, seed))
        if os.path.exists(sortie):
            print("[%d/%d] %s %s seed %d: already done" % (i, len(todo), algo, dataset, seed), flush=True)
            continue
        print("[%d/%d] %s %s seed %d : %s" % (i, len(todo), algo, dataset, seed, time.strftime("%H:%M:%S")), flush=True)
        t0 = time.time()
        try:
            r = run_recbole(model=algo, dataset=dataset,
                            config_file_list=[os.path.join("configs", "%s_%s.yaml" % (algo, dataset))],
                            config_dict={"gpu_id": max(args.gpu, 0), "use_gpu": args.gpu >= 0,
                                         "seed": seed, "reproducibility": True,
                                         "checkpoint_dir": "saved_%s" % ("cpu" if args.gpu < 0 else "gpu%d" % args.gpu),
                                         "show_progress": False})
        except Exception:
            traceback.print_exc()
            with open(os.path.join("results", "%s_%s_%d.ECHEC.txt" % (algo, dataset, seed)), "w") as fh:
                fh.write(traceback.format_exc())
            continue
        res = {"algo": algo, "dataset": dataset, "seed": seed,
               "best_valid_score": r.get("best_valid_score"),
               "best_valid_result": dict(r.get("best_valid_result") or {}),
               "test_result": dict(r.get("test_result") or {}),
               "duration_s": round(time.time() - t0), "date": time.strftime("%Y-%m-%d %H:%M"),
               "device": "cpu" if args.gpu < 0 else "cuda:%d" % args.gpu}
        with open(sortie, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=1)
        print("    -> %s en %.0f min" % (
            ", ".join("%s=%.4f" % (k, v) for k, v in res["test_result"].items()), (time.time() - t0) / 60), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
