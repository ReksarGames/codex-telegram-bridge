#!/usr/bin/env bash
set -euo pipefail
umask 077
bridge_data="$HOME/.local/share/codex-telegram-bridge"
bridge_build=$(mktemp -d)
trap 'rm -rf -- "$bridge_build"' EXIT
mkdir -p -- "$bridge_data/bin"
chmod 700 -- "$bridge_data" "$bridge_data/bin"
if [[ -e "$bridge_data/bin/whisper-cli" || -L "$bridge_data/bin/whisper-cli" ]]; then
    echo 'whisper-cli уже существует; не перезаписываю'
    exit 0
fi
curl --fail --location --proto '=https' --tlsv1.2 \
    https://github.com/ggml-org/whisper.cpp/archive/refs/tags/v1.9.5.tar.gz \
    --output "$bridge_build/source.tar.gz"
tar -xzf "$bridge_build/source.tar.gz" -C "$bridge_build"
cmake -S "$bridge_build/whisper.cpp-1.9.5" -B "$bridge_build/build" \
    -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
    -DGGML_CUDA=OFF -DGGML_VULKAN=OFF -DWHISPER_BUILD_TESTS=OFF
cmake --build "$bridge_build/build" --target whisper-cli -j 4
( set -o noclobber; cat "$bridge_build/build/bin/whisper-cli" > "$bridge_data/bin/whisper-cli" )
chmod 755 -- "$bridge_data/bin/whisper-cli"
echo 'CPU whisper-cli установлен локально; модель настраивается через codex-bridge setup'
