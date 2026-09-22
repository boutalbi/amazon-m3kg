#!/usr/bin/env bash
# =============================================================================
# One BATCH of the build: same work as run_build.sh, but on the list of
# categories passed as arguments.
#
#   bash run_build_batch.sh batch2 Automotive Sports_and_Outdoors Electronics
#
# The build is single-threaded and uses about 400 MB, so several batches run
# in parallel on a multi-core machine.
#
# SAFETY. The batch lists must be DISJOINT: the marker out_final/.v2/<CAT> is
# only written when a category finishes, so it does not protect two batches
# that would start the same one at the same time. Each category writes its own
# files only; there is no other sharing.
#
# The first argument is the BATCH NAME: it names the log, so that two batches
# do not write to the same file.
# =============================================================================
PY="${PYTHON:-python}"
SRC="${M3KG_DATA:-data}"
cd "$(dirname "$0")/.."
mkdir -p out_final logs_v2 reports_v1 out_final/.v2

LOT="$1"; shift
if [ -z "$LOT" ] || [ $# -eq 0 ]; then
  echo "usage: bash run_build_batch.sh <batch_name> <Category> [Category...]" >&2
  exit 2
fi

for cat in "$@"; do
  fichier="$SRC/meta_${cat}.jsonl.gz"
  if [ ! -f "$fichier" ]; then
    echo "[$LOT] $cat: file missing, skipped"
    continue
  fi
  if [ -f "out_final/.v2/${cat}" ]; then
    echo "[$LOT] $cat: already in v2, skipped"
    continue
  fi

  if [ -f "out_final/${cat}.report.json" ]; then
    cp "out_final/${cat}.report.json" "reports_v1/${cat}.report.json"
  fi
  rm -f "out_final/${cat}.kg" "out_final/${cat}.text" \
        "out_final/${cat}.image" "out_final/${cat}.link" \
        "out_final/${cat}.valuemap.json" "out_final/${cat}.report.json"

  debut=$(date +%s)
  "$PY" build_trimodal_kg.py build -c configs/categories.yaml --category "$cat" \
      -i "$fichier" -o out_final/ > "logs_v2/${cat}.build.log" 2>&1
  if [ $? -ne 0 ]; then
    echo "[$LOT] $cat: BUILD FAILED"
    tail -6 "logs_v2/${cat}.build.log"
    continue
  fi
  "$PY" audit_kg.py -o out_final/ --category "$cat" \
      > "logs_v2/${cat}.audit.txt" 2>&1
  "$PY" check_images.py -o out_final/ --category "$cat" \
      > "logs_v2/${cat}.images.txt" 2>&1
  touch "out_final/.v2/${cat}"
  echo "[$LOT] $cat done in $(( ($(date +%s)-debut)/60 )) min -- $(grep -m1 'score global' "logs_v2/${cat}.audit.txt")"
done
echo "[$LOT] DONE"
