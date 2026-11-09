#!/usr/bin/env bash
# Regenerates the sketches' figure images from the papers' arXiv e-prints. The images are not committed: arXiv's
# default license does not allow republishing a paper's figures. Needs curl, tar and pdftocairo (poppler-utils).
set -euo pipefail
cd "$(dirname "$0")"

# prefix arXiv-id figure-names (each is figures/<name>.pdf in the e-print)
papers=(
  "icl 2611.05344v1 fig1_circuit fig2_slot fig3_law fig4_gate fig9_waterfall fig12_reader_rows fig23_scale"
  "scarce 2610.21029v2 channel_util_s1 methodology_2 methodology_by_step pact_vs_bid performance_s1"
)

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
for entry in "${papers[@]}"; do
  read -r prefix id names <<<"$entry"
  mkdir -p "$work/$prefix"
  curl -fsSL "https://arxiv.org/e-print/$id" -o "$work/$prefix.tar"
  tar -xf "$work/$prefix.tar" -C "$work/$prefix"
  for name in $names; do
    pdftocairo -png -r 160 -singlefile "$work/$prefix/figures/$name.pdf" "$prefix-$name"
  done
done
