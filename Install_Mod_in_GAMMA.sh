#!/usr/bin/env bash
set -euo pipefail
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MODS_DIR="${1:-}"
if [[ -z "$MODS_DIR" ]]; then
    for candidate in \
        "$HOME/Games/GAMMA/mods" \
        "$HOME/STALKER/GAMMA/mods" \
        "$SRC_DIR/../GAMMA/mods" \
        "$SRC_DIR/../../GAMMA/mods"; do
        if [[ -d "$candidate" ]]; then
            MODS_DIR="$candidate"
            break
        fi
    done
fi

if [[ -z "$MODS_DIR" || ! -d "$MODS_DIR" ]]; then
    echo "Could not find GAMMA mods folder automatically."
    echo "Usage: $0 /path/to/GAMMA/mods"
    exit 1
fi

TARGET_DIR="$MODS_DIR/xrRazom_VOIP_Telemetry"
mkdir -p "$TARGET_DIR"
cp -a "$SRC_DIR/gamedata" "$TARGET_DIR/"
cp -a "$SRC_DIR/meta.ini" "$TARGET_DIR/"
echo "Done! Mod installed to: $TARGET_DIR"
echo "Open Mod Organizer 2, press F5, and enable 'xrRazom_VOIP_Telemetry' at the bottom (below xrRazom Co-op)."
