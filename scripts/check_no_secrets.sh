#!/usr/bin/env bash
# Secret / forbidden-path guard for the PUBLIC worker repo.
# Fails the build (exit 1) if any credential file, private-only module, client
# data path, or high-signal secret value appears in the tracked tree.
#
# Run locally before pushing, and in CI on every push/PR (see phase2-scrape.yml
# 'guard' job). Scans git-tracked files only.
set -uo pipefail

cd "$(git rev-parse --show-toplevel)"
fail=0

note() { echo "  [BLOCK] $1"; fail=1; }

# 1) Forbidden paths — these must NEVER exist in the public repo.
echo "== forbidden-path check =="
FORBIDDEN_GLOBS=(
  'cloud/sql_state.py'
  'cloud/api.py'
  'cloud/storage.py'
  'cloud/phase2_worker.py'
  'cloud/plan_phase2.py'
  'cloud/seed_grid.py'
  'cloud/sql'
  'dashboard'
  'inputs'
  'outputs'
)
tracked="$(git ls-files)"
while IFS= read -r f; do
  for pat in "${FORBIDDEN_GLOBS[@]}"; do
    case "$f" in
      "$pat"|"$pat"/*) note "forbidden path tracked: $f" ;;
    esac
  done
  case "$f" in
    *.env|*.env.*|.env|.env.*)            note "env file tracked: $f" ;;
    *api_keys*|*api_key_usage*)           note "api-keys file tracked: $f" ;;
    *gdrive_token*|*service_account*.json|*credentials*) note "credential file tracked: $f" ;;
    *.pem|*.pfx|*.p12|*.key)              note "key/cert file tracked: $f" ;;
  esac
done <<< "$tracked"

# 2) High-signal secret VALUES in file contents.
echo "== secret-content check =="
declare -A PATTERNS=(
  ["Azure SQL host"]='ewwayxoov5|[A-Za-z0-9_-]+\.database\.windows\.net'
  ["private key block"]='BEGIN ([A-Z]+ )?PRIVATE KEY'
  ["storage AccountKey"]='AccountKey='
  ["Google API key"]='AIza[0-9A-Za-z_-]{35}'
  ["Cloudflare tunnel host"]='[a-z0-9-]+\.trycloudflare\.com'
  ["bearer token literal"]='WORKER_TOKEN[[:space:]]*[:=][[:space:]]*["'\''a-f0-9]{16}'
  ["aws secret"]='aws_secret_access_key'
)
for name in "${!PATTERNS[@]}"; do
  if git grep -nIiE "${PATTERNS[$name]}" -- . ':(exclude)scripts/check_no_secrets.sh' >/dev/null 2>&1; then
    note "$name pattern found:"
    git grep -nIiE "${PATTERNS[$name]}" -- . ':(exclude)scripts/check_no_secrets.sh' | head -5 | sed 's/^/      /'
  fi
done

if [ "$fail" -ne 0 ]; then
  echo ""
  echo "FAILED: forbidden paths or secrets detected. Do NOT push."
  exit 1
fi
echo ""
echo "OK: no forbidden paths or secrets in tracked tree."
