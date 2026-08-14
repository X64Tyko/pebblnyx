#!/usr/bin/env bash
# Installs pebblnyx's git hooks. `.git/hooks/` is never tracked by git itself -- it lives
# outside the tree git manages, per clone -- so the hook has to live under version control
# somewhere ELSE (tools/hooks/) and be installed into place explicitly, once per clone.
# This is that install step. CLion's own commit flow shells out to the same git binary,
# so a hook installed here runs whether you commit from the IDE or the terminal.
#
#   tools/install_git_hooks.sh
#
# Symlinked rather than copied: editing tools/hooks/pre-commit takes effect immediately,
# with nothing to re-run after a pull that changes it.

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

for hook in tools/hooks/*; do
	name=$(basename "$hook")
	target=".git/hooks/$name"
	ln -sf "../../$hook" "$target"
	chmod +x "$hook"
	echo "installed $target -> $hook"
done
