#!/usr/bin/env bash
set -euo pipefail

action_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
expected='This action is for testing purposes only and does not perform any meaningful action. touch=3'
actual="$(cd / && "$BASH" "$action_dir/main.sh")"

if [[ "$actual" != "$expected" ]]; then
  printf 'Expected: %s\nActual: %s\n' "$expected" "$actual" >&2
  exit 1
fi

printf 'Dummy action test passed.\n'
