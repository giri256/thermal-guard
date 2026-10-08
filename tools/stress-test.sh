#!/bin/bash
# The test used for every result in the README: glmark2 scene by scene (10 s each) with the
# flight recorder running as root. If the machine freezes, the fsync'd logs survive the reboot.
#   bash tools/stress-test.sh [out_dir]       default out_dir: ./stress-<UTC time>
# Needs glmark2 (glmark2-wayland on Wayland) and sudo. Run it from your desktop session.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$PWD/stress-$(date -u +%Y-%m-%dT%H%M%SZ)}"
mkdir -p "$OUT"
T="$OUT/test.log"
mark() { echo "$(date -u +%T.%3N) $*" >> "$T"; sync "$T"; echo "$*"; }

GLMARK=glmark2; [ -n "${WAYLAND_DISPLAY:-}" ] && command -v glmark2-wayland >/dev/null && GLMARK=glmark2-wayland
command -v "$GLMARK" >/dev/null || { echo "install glmark2 first"; exit 1; }

mark "kernel $(uname -r) | $GLMARK"
sudo -v || exit 1
sudo python3 -I "$HERE/flight-recorder.py" "$OUT" & REC=$!
trap 'sudo kill $REC 2>/dev/null; wait $REC 2>/dev/null' EXIT
sleep 2

for scene in build:use-vbo=true texture shading:shading=phong refract terrain jellyfish terrain terrain; do
    mark "SCENE START $scene"
    stdbuf -oL "$GLMARK" --fullscreen -b "$scene:duration=10" 2>&1 | while IFS= read -r l; do
        echo "$(date -u +%T.%3N) $l" >> "$OUT/glmark2.log"; sync "$OUT/glmark2.log"; done
    rc=${PIPESTATUS[0]}; mark "SCENE END $scene rc=$rc"
    [ "$rc" = 0 ] || { mark "ABORT: glmark2 failed, see glmark2.log"; exit 1; }
done
mark "ALL SCENES SURVIVED"
peak=$(grep -o 'core[0-9]*=[0-9]*' "$OUT/telemetry.log" | cut -d= -f2 | sort -n | tail -1)
mark "peak core temperature: ${peak:-?} °C"
echo "logs: $OUT"
