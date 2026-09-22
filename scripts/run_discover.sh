#!/usr/bin/env bash
# Runs the 'discover' pass over every downloaded metadata file.
PY="${PYTHON:-python}"
SRC="${M3KG_DATA:-data}"
cd "$(dirname "$0")/.."
mkdir -p out_discover
for f in "$SRC"/meta_*.jsonl.gz; do
  cat=$(basename "$f" .jsonl.gz); cat=${cat#meta_}
  echo "=========== $cat ==========="
  "$PY" build_trimodal_kg.py discover -i "$f" --sample 50000 -o out_discover/ --category "$cat" \
    > "out_discover/${cat}.discover.txt" 2>&1 && echo "  ok" || echo "  FAILED"
done
echo "DONE"
