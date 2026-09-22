#!/usr/bin/env bash
# Downloads the Amazon Reviews 2023 metadata files (McAuley-Lab) into data/
# Usage: bash download_meta.sh All_Beauty Digital_Music Books
# With no argument: downloads the four small test categories.
set -u
BASE="https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023/resolve/main/raw/meta_categories"
DEST="$(cd "$(dirname "$0")" && pwd)/data"
mkdir -p "$DEST"
CATS=("$@")
if [ ${#CATS[@]} -eq 0 ]; then
  CATS=(All_Beauty Digital_Music Amazon_Fashion Baby_Products)
fi
for c in "${CATS[@]}"; do
  f="$DEST/meta_${c}.jsonl.gz"
  echo "--- $c -> $f"
  curl -L -C - --fail --progress-bar -o "$f" "$BASE/meta_${c}.jsonl.gz" \
    || echo "!! failed on $c"
done
ls -lh "$DEST"
