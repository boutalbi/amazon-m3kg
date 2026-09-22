#!/usr/bin/env bash
# =============================================================================
# RecBole export of every category whose reviews have been downloaded, then
# DELETION of the original review file (too large: the .inter replaces it,
# filtered to the tri-modal products).
#
# Waits for the end of the finalisation (DONE line in logs_v2/finaliser.log)
# because the .link is rewritten by the tri-modal filter: an export started
# earlier would keep products without an image or without text. Use --now to
# export whatever is already finalised.
#
# Order per category:
#   1. Books, Movies: intersection export with Amazon-KG v2.0, which needs the
#      reviews -- hence BEFORE the deletion;
#   2. main export with --delete-source (deletes the original once the row
#      counts are verified).
#
# Reviews are looked up in ~/Downloads/<CAT>.jsonl.gz. A category without its
# file is skipped and listed at the end; re-run the script when the file is
# there. An export already done (<CAT>.recbole.json present) is kept unless
# --refaire is given; its source, if still present, is then deleted.
#
#   bash run_recbole_export.sh            waits, then exports
#   bash run_recbole_export.sh --now      exports with whatever is present
# =============================================================================
set -u
PY="${PYTHON:-python}"
AVIS="${M3KG_DATA:-data}"
AKG="${M3KG_AMAZONKG:-data/amazonkg}"
cd "$(dirname "$0")/.."
horodate() { date "+%H:%M:%S"; }
REFAIRE=0
for a in "$@"; do [ "$a" = "--refaire" ] && REFAIRE=1; done

if [ "${1:-}" != "--now" ]; then
  echo "[$(horodate)] waiting for the finalisation to end..."
  while ! grep -qE "DONE|TERMINE" logs_v2/finaliser.log 2>/dev/null; do sleep 120; done
fi

finalise() { grep -q '"finalised"' "out_final/$1.report.json" 2>/dev/null; }

manquants=""
for r in out_final/*.report.json; do
  c=$(basename "$r" .report.json)
  f="$AVIS/$c.jsonl.gz"
  if [ ! -f "$f" ]; then
    [ -f "out_final/recbole/$c/$c.recbole.json" ] || manquants="$manquants $c"
    continue
  fi
  if ! finalise "$c"; then
    echo "[$(horodate)] $c: not finalised yet (tri-modal filter), skipped"; manquants="$manquants $c"; continue
  fi
  # 1. head-to-head with Amazon-KG, before any deletion
  d=""
  [ "$c" = "Books" ] && d="Books"
  [ "$c" = "Movies_and_TV" ] && d="Movies"
  if [ -n "$d" ] && [ -d "$AKG/$d" ] && { [ ! -f "out_final/recbole/${c}_amazonkg/$c.recbole.json" ] || [ "$REFAIRE" = 1 ]; }; then
    echo "[$(horodate)] $c, Amazon-KG intersection"
    "$PY" export_recbole.py -o out_final --category "$c" --reviews "$f" --intersection "$AKG/$d" 2>&1 | grep -E "core|graph:|intersection"
  fi
  # 2. main export, then verified deletion of the source
  if [ -f "out_final/recbole/$c/$c.recbole.json" ] && [ "$REFAIRE" = 0 ]; then
    echo "[$(horodate)] $c: already exported, deleting the source"
    rm -f "$f"
    continue
  fi
  echo "[$(horodate)] $c"
  "$PY" export_recbole.py -o out_final --category "$c" --reviews "$f" --delete-source 2>&1 | grep -E "core|graph:|products released|source deleted|WARNING"
done

[ -n "$manquants" ] && echo "reviews missing or category not finalised:$manquants"
echo "[$(horodate)] DONE recbole"
