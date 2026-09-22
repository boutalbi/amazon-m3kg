#!/usr/bin/env bash
# Post-processing of wave 3, tolerant to files arriving one by one:
#   - per category, as soon as its report exists: tri-modal filter + N-Triples;
#   - as soon as its reviews are there: RecBole export (source deleted once
#     the row counts are verified);
#   - loops every 10 min until the 10 categories are exported, then archives
#     (one zip per category -> global archive) and deletes the raw files.
cd "$(dirname "$0")/.."
PY="${PYTHON:-python}"
AVIS="${M3KG_DATA:-data}"
CATS="Subscription_Boxes Health_and_Personal_Care Software Video_Games Musical_Instruments Pet_Supplies Office_Products Patio_Lawn_and_Garden Health_and_Household Tools_and_Home_Improvement"
mkdir -p out_final/reports
while true; do
  restants=0
  for c in $CATS; do
    [ -f "out_final/recbole/$c/$c.recbole.json" ] && continue
    if [ ! -f "out_final/$c.report.json" ] || [ ! -f "out_final/.v2/$c" ]; then restants=$((restants+1)); continue; fi
    if ! grep -q '"finalised"' "out_final/$c.report.json"; then
      echo "[$(date +%H:%M)] == $c: tri-modal filter + N-Triples"
      "$PY" finalize_outputs.py -o out_final --category "$c" 2>&1 | grep -E "tri-modal|WARNING"
      "$PY" export_ntriples.py -o out_final --category "$c" 2>&1 | grep -v Warning
      cp "out_final/$c.report.json" out_final/reports/
    fi
    if [ -f "$AVIS/$c.jsonl.gz" ] && [ ! -f "$AVIS/$c.jsonl.gz.crdownload" ]; then
      echo "[$(date +%H:%M)] == $c: RecBole export"
      "$PY" export_recbole.py -o out_final --category "$c" --reviews "$AVIS/$c.jsonl.gz" --delete-source 2>&1 | grep -E "-core in|graph:|source deleted|WARNING"
      cp "out_final/recbole/$c/$c.recbole.json" out_final/reports/ 2>/dev/null
    else
      restants=$((restants+1))
    fi
  done
  [ "$restants" -eq 0 ] && break
  sleep 600
done
echo "[$(date +%H:%M)] 10 categories exported; archiving"
"$PY" archive_categories.py --step categories && "$PY" archive_categories.py --step global
echo "[$(date +%H:%M)] DONE post_wave3"
