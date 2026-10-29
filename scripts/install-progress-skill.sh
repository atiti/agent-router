#!/bin/sh
set -eu

# Never replace a user's existing skill, including a symlink or incomplete one.
source_skill=$1
skills_dir=$2
destination="$skills_dir/goal-checkpoint"
mkdir -p "$skills_dir"
if ! mkdir "$destination" 2>/dev/null; then
    if [ -e "$destination" ] || [ -L "$destination" ]; then
        printf 'Keeping existing goal-checkpoint skill at %s\n' "$destination"
        exit 0
    fi
    printf 'Cannot create goal-checkpoint skill at %s\n' "$destination" >&2
    exit 1
fi
if ! cp "$source_skill/SKILL.md" "$destination/SKILL.md"; then
    rmdir "$destination" 2>/dev/null || true
    exit 1
fi
printf 'Installed goal-checkpoint skill at %s\n' "$destination"
