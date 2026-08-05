#!/usr/bin/env bash
# box.sh — strafe a square without ever changing heading.
#
# The point of this one: a differential-drive car cannot do it. Each side is a
# pure translation, so the car crabs around the square still facing the same way
# the whole time. Good first demo for showing what mecanum wheels buy you.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SIDE_MS="${SIDE_MS:-800}"
exec "$HERE/demo.sh" --leds chase "$@" \
    forward "$SIDE_MS" \
    right   "$SIDE_MS" \
    back    "$SIDE_MS" \
    left    "$SIDE_MS"
