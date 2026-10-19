#!/usr/bin/env bash
set -euo pipefail
bridge_source=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
bridge_install="$HOME/.local/share/codex-telegram-bridge"
umask 077
mkdir -p -- "$bridge_install"
if [[ ! -d "$bridge_install/venv" ]]; then
    uv venv "$bridge_install/venv"
fi
UV_PROJECT_ENVIRONMENT="$bridge_install/venv" uv sync --project "$bridge_source" --frozen --no-dev --no-editable --reinstall-package codex-telegram-bridge
mkdir -p -- "$HOME/.local/bin"
for bridge_command in codex-bridge codex-tg; do
    if [[ ! -e "$HOME/.local/bin/$bridge_command" && ! -L "$HOME/.local/bin/$bridge_command" ]]; then
        ln -s -- "$bridge_install/venv/bin/$bridge_command" "$HOME/.local/bin/$bridge_command"
    fi
done
exec "$bridge_install/venv/bin/codex-bridge" setup
