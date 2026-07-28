#!/bin/sh
set -eu

runtime_root=/app/runtime
startup_trace_dir="${runtime_root}/startup-check/traces"
interactive_trace_dir="${runtime_root}/interactive/traces"
interactive_cache_dir="${runtime_root}/interactive/cache"

mkdir -p "${startup_trace_dir}" "${interactive_trace_dir}" "${interactive_cache_dir}"

python -c "from pathlib import Path; from orchestrator.demo import main; raise SystemExit(main(Path('${startup_trace_dir}')))"
date -u +"%Y-%m-%dT%H:%M:%SZ" > "${runtime_root}/startup-check/.ok"

exec python -m orchestrator.interactive_session \
  --host 0.0.0.0 \
  --port 8765 \
  --trace-dir "${interactive_trace_dir}" \
  --cache-dir "${interactive_cache_dir}"
