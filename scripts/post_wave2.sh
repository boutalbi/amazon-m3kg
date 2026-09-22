#!/usr/bin/env bash
# Post-processing of wave 2: tri-modal filter, N-Triples, RecBole export
# (the review source is deleted once the row counts are verified), per category.
cd "$(dirname "$0")/.."
PY="${PYTHON:-python}"
AVIS="${M3KG_DATA:-data}"
for c in Gift_Cards Magazine_Subscriptions Appliances Handmade_Products Industrial_and_Scientific Grocery_and_Gourmet_Food Kindle_Store Home_and_Kitchen; do
  echo "[$(date +%H:%M)] == $c"
  "$PY" finalize_outputs.py -o out_final --category "$c" 2>&1 | grep -E "tri-modal|already finalised|WARNING"
  "$PY" export_ntriples.py -o out_final --category "$c" 2>&1 | grep -v Warning
  if [ -f "$AVIS/$c.jsonl.gz" ]; then
    "$PY" export_recbole.py -o out_final --category "$c" --reviews "$AVIS/$c.jsonl.gz" --delete-source 2>&1 | grep -E "core|graph:|products released|source deleted|WARNING"
  else
    echo "  reviews missing for $c"
  fi
  cp "out_final/$c.report.json" out_final/reports/ 2>/dev/null
  cp "out_final/recbole/$c/$c.recbole.json" out_final/reports/ 2>/dev/null
done
echo "[$(date +%H:%M)] DONE post_wave2"
