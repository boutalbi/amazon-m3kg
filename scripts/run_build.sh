#!/usr/bin/env bash
# =============================================================================
# Builds every category in series: build, audit, image check.
#
# Before rebuilding a category, its previous report is copied into reports_v1/
# (a few kB, to compare) and its large files are deleted, so that two series
# never occupy the disk at once. A category already built (marker in
# out_final/.v2/) is skipped: the script can be re-run at will.
# =============================================================================
PY="${PYTHON:-python}"
SRC="${M3KG_DATA:-data}"
cd "$(dirname "$0")/.."
mkdir -p out_final logs_v2 reports_v1 out_final/.v2

CATS="Toys_and_Games
      Books
      Clothing_Shoes_and_Jewelry
      Movies_and_TV
      Digital_Music
      All_Beauty
      Baby_Products
      CDs_and_Vinyl
      Amazon_Fashion
      Arts_Crafts_and_Sewing
      Beauty_and_Personal_Care
      Cell_Phones_and_Accessories
      Electronics
      Sports_and_Outdoors
      Automotive"

for cat in $CATS; do
  fichier="$SRC/meta_${cat}.jsonl.gz"
  if [ ! -f "$fichier" ]; then
    echo "$cat: file missing, skipped"
    continue
  fi
  if [ -f "out_final/.v2/${cat}" ]; then
    echo "$cat: already in v2, skipped"
    continue
  fi

  # 1. archive the v1 report, then free the space
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
    echo "$cat: BUILD FAILED"
    tail -6 "logs_v2/${cat}.build.log"
    continue
  fi
  "$PY" audit_kg.py -o out_final/ --category "$cat" \
      > "logs_v2/${cat}.audit.txt" 2>&1
  "$PY" check_images.py -o out_final/ --category "$cat" \
      > "logs_v2/${cat}.images.txt" 2>&1
  touch "out_final/.v2/${cat}"
  echo "$cat done in $(( ($(date +%s)-debut)/60 )) min -- $(grep -m1 'score global' "logs_v2/${cat}.audit.txt")"
done
echo DONE
