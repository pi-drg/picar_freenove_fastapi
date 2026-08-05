#!/usr/bin/env bash
# shimmy.sh — the sequence worked out during bring-up: strafe left/right/left,
# then spin clockwise on the spot, with the LEDs chasing counter-clockwise.
#
# The lateral moves cancel (0.5s + 0.5s left against 1.0s right), so any sideways
# drift you see at the end is open-loop error, not an imbalanced command.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$HERE/demo.sh" --leds chase "$@" \
    left 500 \
    right 1000 \
    left 500 \
    cw 2000
