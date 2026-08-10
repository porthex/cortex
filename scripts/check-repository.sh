#!/usr/bin/env bash
set -euo pipefail

root="$(git rev-parse --show-toplevel)"
cd "$root"

failures=0
fail() {
  printf 'ERROR: %s\n' "$1" >&2
  failures=$((failures + 1))
}

required=(
  README.md
  CONTRIBUTING.md
  SECURITY.md
  LICENSES.md
  THIRD_PARTY_NOTICES.md
  docs/architecture.md
  docs/configuration.md
  config/cortex.example.yaml
)

for path in "${required[@]}"; do
  [[ -f "$path" ]] || fail "missing required file: $path"
done

# A Cortex license must be an explicit owner decision. Prevent an accidental
# generic license file from implying a choice that has not been made.
if [[ -e LICENSE || -e LICENSE.md || -e COPYING ]]; then
  fail "unexpected project license; confirm ownership and licensing intent first"
fi

python3 - <<'PY' || fail "package metadata declares a Cortex license while LICENSES.md says it is undecided"
from pathlib import Path
import re

pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
project_section = pyproject.split("[project]", 1)[1].split("\n[", 1)[0]
licensing_status = Path("LICENSES.md").read_text(encoding="utf-8")
undecided = "Cortex does not currently include a project license." in licensing_status
if undecided and re.search(r"(?m)^\s*license\s*=", project_section):
    raise SystemExit(1)
PY

# Catch common high-confidence credential forms. This complements, rather than
# replaces, gitleaks in CI.
secret_pattern='(gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----|sk-[A-Za-z0-9]{20,})'
while IFS= read -r -d '' path; do
  [[ -f "$path" ]] || continue
  if grep -Eiq '(^|/)(\.env($|\.)|secrets?/|credentials?\.json$|.*\.(db|sqlite3?|dump|sql|pem|key|p12|pfx)$|.*\.sqlite-|.*-(wal|shm|journal)$|backups?/|memories?/)' <<< "$path"; then
    fail "candidate path resembles a secret, private memory, database, or backup artifact: $path"
  fi
  if grep -nEI "$secret_pattern" -- "$path"; then
    fail "possible credential in candidate file: $path"
  fi
  if grep -nE '/(home|Users)/[^ /]+' -- "$path"; then
    fail "machine-specific user path in candidate file: $path"
  fi
done < <(git ls-files -z --cached --others --exclude-standard)

python3 - <<'PY' || fail "YAML parsing failed"
from pathlib import Path
import yaml

for pattern in ("*.yml", "*.yaml"):
    for path in Path(".").rglob(pattern):
        if ".git" not in path.parts:
            with path.open(encoding="utf-8") as stream:
                yaml.safe_load(stream)
print("YAML parse checks passed.")
PY

if [[ "$failures" -ne 0 ]]; then
  printf '%s repository hygiene check(s) failed.\n' "$failures" >&2
  exit 1
fi

printf 'Repository hygiene checks passed.\n'
