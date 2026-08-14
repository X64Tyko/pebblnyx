#!/usr/bin/env bash
# Runs both halves of pebblnyx's lint setup: clang-format (style, everywhere) and
# clang-tidy (static analysis, the portable core -- see .clang-tidy's header comment for
# exactly why that scope stops where it does). Non-destructive by default -- pass `fix`
# to have clang-format apply its changes in place instead of just checking for them;
# clang-tidy findings are never auto-applied, only reported.
#
#   tools/lint.sh          # check
#   tools/lint.sh fix      # reformat in place, then check clang-tidy
#
# Exits non-zero if either check would have anything to say, so this is CI-shaped even
# though nothing wires it into CI yet.

set -euo pipefail
cd "$(dirname "$0")/.."

MODE="${1:-check}"
if [[ "$MODE" != "check" && "$MODE" != "fix" ]]; then
  echo "usage: $0 [check|fix]" >&2
  exit 2
fi

status=0

echo "--- clang-format ---"
mapfile -d '' FILES < <(git ls-files -z '*.c' '*.h')
IGNORE_PATTERNS=$(sed -e 's#\*\*/##' -e 's#/\*\*##' -e '/^#/d' -e '/^$/d' .clang-format-ignore)
FILTERED=()
for f in "${FILES[@]}"; do
  skip=0
  while IFS= read -r pat; do
    [[ -n "$pat" && "$f" == *"$pat"* ]] && skip=1 && break
  done <<< "$IGNORE_PATTERNS"
  [[ $skip -eq 0 ]] && FILTERED+=("$f")
done

if [[ "$MODE" == "fix" ]]; then
  clang-format -i --style=file -- "${FILTERED[@]}"
  echo "reformatted in place"
else
  bad=0
  for f in "${FILTERED[@]}"; do
    if ! diff -q <(clang-format --style=file -- "$f") "$f" >/dev/null; then
      echo "needs formatting: $f"
      bad=1
    fi
  done
  if [[ $bad -ne 0 ]]; then
    echo "clang-format: FAIL -- run 'tools/lint.sh fix'"
    status=1
  else
    echo "clang-format: OK ($((${#FILTERED[@]})) files)"
  fi
fi

echo
echo "--- clang-tidy ---"
python3 tools/gen_compile_commands.py
mapfile -t TIDY_FILES < <(python3 -c "
import json
for e in json.load(open('tests/compile_commands.json')):
    print(e['file'])
")
# clang-tidy exits 0 even when it has findings to report -- only a crash or a
# --warnings-as-errors hit is non-zero -- so pass/fail is decided by whether anything
# landed on stdout, the same way the format check above works. `--quiet`'s own
# `[N/25] Processing file...` progress goes to stderr regardless of --quiet, so that
# is let through live (for visibility) rather than folded into the pass/fail check --
# an earlier version merged the two streams and failed on every run, progress lines
# included, whether or not clang-tidy had found anything.
tidy_out=$(cd tests && clang-tidy -p . --quiet "${TIDY_FILES[@]}") || true
if [[ -n "$tidy_out" ]]; then
  echo "$tidy_out"
  echo "clang-tidy: FAIL"
  status=1
else
  echo "clang-tidy: OK (${#TIDY_FILES[@]} files -- see .clang-tidy's header for scope)"
fi

exit $status
