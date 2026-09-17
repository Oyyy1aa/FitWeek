#!/usr/bin/env sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
output=${1:-"$root/fitweek-source.zip"}
cd "$root"
case "$output" in "$root"/*) ;; *) echo "Output path must be inside workspace." >&2; exit 2;; esac
command -v zip >/dev/null 2>&1 || { echo "zip is required." >&2; exit 127; }
rm -f -- "$output"
find app tests frontend migrations scripts docs -type f \
  ! -path '*/node_modules/*' ! -path '*/dist/*' ! -path '*/.venv*/*' \
  ! -path '*/__pycache__/*' ! -name '*.log' ! -name '*.ics' -print \
  | zip -q "$output" -@
zip -q "$output" README.md pyproject.toml alembic.ini .env.example docker-compose.yml Dockerfile
echo "Created $output"
