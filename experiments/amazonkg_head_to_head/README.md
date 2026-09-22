# Head-to-head: Amazon-M3KG vs Amazon-KG v2.0

Same items, same interactions, same models, same seeds; only the graph changes.

- `datasets/M3KG-AKGsubset-{Books,Movies_and_TV}/` : our graph rebuilt on the exact item sets
  of Amazon-KG v2.0 (`.kg`, `.link`) and their `.inter` copied as is. Amazon-KG's own `.kg`/`.link`
  are in their repository: https://github.com/WangYuhan-0520/Amazon-KG-v2.0-dataset
  (place them in `datasets/Amazon-KG-5core-{Books,Movies_and_TV}/`).
- `configs/<MODEL>_<DATASET>.yaml` : RecBole configurations, identical across graphs.
  CFKG's early-stopping patience is 30 on Books and 50 on Movies & TV (its validation metric is
  flat for the first 30-60 epochs); everything else is the default of the paper.
- `run_all.py` : one JSON per (model, dataset, seed) in `results/`; finished runs are skipped.
- `collect_results.py` : mean +- std over seeds -> `final_results.md`.

KGNNLS coincides with KGCN on binarised interactions (its label-smoothness term is constant)
and is therefore not reported separately; its three Books runs are kept in `results/`.
