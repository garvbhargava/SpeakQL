#!/usr/bin/env bash
# Train both models, unattended, in order.
#
#   docker compose --profile train up -d trainer
#   docker compose logs -f trainer
#
# In a container rather than a terminal because this takes hours on CPU and
# nothing about it should depend on a terminal staying open. Every stage
# resumes from its own checkpoint, so a restart costs minutes, not the run.
#
# Stage marker files in ml/results let a restarted container skip what is
# already finished instead of starting the whole chain again.

set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here/.."

results="ml/results"
mkdir -p "$results"

say() { printf '\033[36m==>\033[0m %s %s\n' "$(date -u +%H:%M:%S)" "$1"; }

stage() {
  local name="$1"; shift
  if [[ -f "$results/$name.done" ]]; then
    say "$name already finished, skipping"
    return 0
  fi
  say "$name starting"
  if "$@" >>"$results/$name.log" 2>&1; then
    date -u +%FT%TZ > "$results/$name.done"
    say "$name finished"
  else
    say "$name FAILED -- see $results/$name.log"
    tail -5 "$results/$name.log" || true
    return 1
  fi
}

# Each stage depends on the one before it, so a failure stops the chain: the
# alternative is three confusing failures for one cause.
stage stage1 python -u -m ml.generator_train --stage 1 --epochs "${STAGE1_EPOCHS:-1}" --resume || exit 1
stage stage2 python -u -m ml.generator_train --stage 2 --epochs "${STAGE2_EPOCHS:-6}" --resume || exit 1
stage retriever python -u -m ml.retriever_train --epochs "${RETRIEVER_EPOCHS:-1}" || exit 1

say "all stages done"
date -u +%FT%TZ > "$results/training.done"
