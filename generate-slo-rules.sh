#!/usr/bin/env bash
# Generate Prometheus SLO rules from slo/*.yaml with Sloth, validate them, install them as
# rules/slo-<app>.yml in the monitoring stack (a directory mount, picked up on reload), reload.
set -euo pipefail
cd "$(dirname "$0")"
RULES_DIR=~/monitoring/prometheus/rules
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD/slo:/in:ro" -v "$tmp:/out" \
  ghcr.io/slok/sloth:v0.16.0 generate -i /in -o /out >"$tmp/sloth.log" 2>&1 || { cat "$tmp/sloth.log" >&2; exit 1; }
chmod 755 "$tmp"
files=(); for f in "$tmp"/*.yaml; do files+=("/r/$(basename "$f")"); done
docker run --rm --entrypoint promtool -v "$tmp:/r:ro" prom/prometheus:v3.15.1 check rules "${files[@]}" >/dev/null
# Drop rule files for SLO specs that no longer exist, then install the fresh set.
for f in "$RULES_DIR"/slo-*.yml; do
  [ -e "$f" ] || continue
  name=$(basename "$f" .yml); name=${name#slo-}
  [ -e "slo/$name.yaml" ] || rm -v "$f"
done
for f in "$tmp"/*.yaml; do
  cp "$f" "$RULES_DIR/slo-$(basename "$f" .yaml).yml"
done
curl -fsS -X POST http://100.73.222.43:9090/-/reload
echo "installed $(ls "$tmp" | wc -l) SLO rule files into $RULES_DIR"
