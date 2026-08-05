#!/usr/bin/env bash
# demo.sh — drive the car through a sequence of moves from the command line.
#
#   ./demo.sh left 500 right 1000 left 500 cw 2000
#   ./demo.sh --leds chase --duty 1500 forward 800 cw 1000
#
# Every move is a NAME followed by a DURATION in milliseconds. The server's
# auto-stop timer bounds each move independently, so the worst case if this
# script dies mid-run is that the current move finishes and everything halts.
# The EXIT trap also stops the motors and clears the LEDs on Ctrl-C.
set -euo pipefail

HOST="${ROBOT_HOST:-127.0.0.1:8080}"
DUTY="${DUTY:-1200}"
GAP_MS="${GAP_MS:-700}"        # settle time inserted between moves
MAX_MS=5000                    # refuse absurd durations (typo guard)
LEDS=""
LED_REVERSE="true"             # reverse=true is counter-clockwise on this car
DRY_RUN=0

usage() {
    cat <<'EOF'
Usage: demo.sh [options] MOVE MS [MOVE MS ...]

Moves:
  forward MS    drive forward            back MS   drive backward
  left MS       strafe left              right MS  strafe right
  ccw MS        spin counter-clockwise   cw MS     spin clockwise
  pause MS      hold still

Options:
  --duty N       motor duty 0-4095 (default 1200; strafing wants >=1200)
  --gap MS       settle time between moves (default 700)
  --leds EFFECT  run an LED effect for the whole sequence
                 (chase|rainbow|breathing|blink), counter-clockwise
  --leds-cw      make the LED effect travel clockwise instead
  --host H:P     robot host:port (default 127.0.0.1:8080, or $ROBOT_HOST)
  --dry-run      print what would be sent without moving anything
  -h, --help     this text

Examples:
  ./demo.sh left 500 right 1000 left 500 cw 2000
  ./demo.sh --leds chase --duty 1500 forward 800 cw 1000
  ROBOT_HOST=robot.local:8080 ./demo.sh --dry-run forward 500
EOF
}

post() {  # post <path> <json>
    if [ "$DRY_RUN" = "1" ]; then
        printf '    would POST /%s %s\n' "$1" "$2"
        return 0
    fi
    curl -s --max-time 15 -X POST "http://$HOST/$1" \
         -H 'content-type: application/json' -d "$2"
    printf '\n'
}

stop_all() {
    [ "$DRY_RUN" = "1" ] && return 0
    curl -s --max-time 10 -X POST "http://$HOST/drive" \
         -H 'content-type: application/json' \
         -d '{"direction":"stop","duty":0,"duration_ms":0}' >/dev/null 2>&1 || true
    if [ -n "$LEDS" ]; then
        curl -s --max-time 10 -X POST "http://$HOST/led" \
             -H 'content-type: application/json' -d '{"effect":"off"}' >/dev/null 2>&1 || true
    fi
}
# Ctrl-C or any error must leave the car stopped, not coasting.
trap 'echo; echo "-- halting --"; stop_all' EXIT

while [ $# -gt 0 ]; do
    case "$1" in
        --duty)      DUTY="$2"; shift 2 ;;
        --gap)       GAP_MS="$2"; shift 2 ;;
        --leds)      LEDS="$2"; shift 2 ;;
        --leds-cw)   LED_REVERSE="false"; shift ;;
        --host)      HOST="$2"; shift 2 ;;
        --dry-run)   DRY_RUN=1; shift ;;
        -h|--help)   usage; trap - EXIT; exit 0 ;;
        --*)         echo "unknown option: $1" >&2; trap - EXIT; exit 2 ;;
        *)           break ;;
    esac
done

[ $# -eq 0 ] && { usage; trap - EXIT; exit 2; }
[ $(( $# % 2 )) -ne 0 ] && { echo "each move needs a duration: MOVE MS" >&2; trap - EXIT; exit 2; }

# Validate the whole sequence BEFORE moving anything — a typo in move 4 should
# not be discovered after moves 1-3 have already run.
for ((i = 1; i <= $#; i += 2)); do
    move="${!i}"; j=$((i + 1)); ms="${!j}"
    case "$move" in
        forward|back|left|right|cw|ccw|pause) ;;
        *) echo "unknown move '$move'" >&2; trap - EXIT; exit 2 ;;
    esac
    case "$ms" in
        ''|*[!0-9]*) echo "duration for '$move' must be a whole number of ms, got '$ms'" >&2
                     trap - EXIT; exit 2 ;;
    esac
    if [ "$ms" -gt "$MAX_MS" ]; then
        echo "duration ${ms}ms for '$move' exceeds the ${MAX_MS}ms safety cap" >&2
        trap - EXIT; exit 2
    fi
done

# Strafing needs mecanum wheels. Ask the robot rather than assuming, and fail
# before the first move instead of scrubbing the tyres halfway through.
WHEELS="mecanum"
if [ "$DRY_RUN" != "1" ]; then
    INFO="$(curl -s --max-time 5 "http://$HOST/info" 2>/dev/null || true)"
    case "$INFO" in
        *'"wheels":"ordinary"'*) WHEELS="ordinary" ;;
        *'"wheels":"mecanum"'*)  WHEELS="mecanum" ;;
        '') echo "cannot reach the robot at $HOST" >&2; trap - EXIT; exit 1 ;;
    esac
fi
if [ "$WHEELS" = "ordinary" ]; then
    for ((i = 1; i <= $#; i += 2)); do
        case "${!i}" in
            left|right)
                echo "'${!i}' strafes sideways, which needs mecanum wheels — this" >&2
                echo "car has ordinary wheels fitted. Use cw/ccw to turn." >&2
                trap - EXIT; exit 2 ;;
        esac
    done
fi

echo "robot=$HOST duty=$DUTY gap=${GAP_MS}ms wheels=$WHEELS${LEDS:+ leds=$LEDS}"

if [ -n "$LEDS" ]; then
    post led "{\"effect\":\"$LEDS\",\"wait_ms\":80,\"reverse\":$LED_REVERSE}" >/dev/null
    echo "LEDs: $LEDS"
fi

for ((i = 1; i <= $#; i += 2)); do
    move="${!i}"; j=$((i + 1)); ms="${!j}"
    printf '  %-8s %5sms  ' "$move" "$ms"
    case "$move" in
        forward) post mecanum "{\"vx\":$DUTY,\"vy\":0,\"omega\":0,\"duration_ms\":$ms}" ;;
        back)    post mecanum "{\"vx\":-$DUTY,\"vy\":0,\"omega\":0,\"duration_ms\":$ms}" ;;
        left)    post mecanum "{\"vx\":0,\"vy\":$DUTY,\"omega\":0,\"duration_ms\":$ms}" ;;
        right)   post mecanum "{\"vx\":0,\"vy\":-$DUTY,\"omega\":0,\"duration_ms\":$ms}" ;;
        ccw)     post mecanum "{\"vx\":0,\"vy\":0,\"omega\":$DUTY,\"duration_ms\":$ms}" ;;
        cw)      post mecanum "{\"vx\":0,\"vy\":0,\"omega\":-$DUTY,\"duration_ms\":$ms}" ;;
        pause)   printf 'holding\n' ;;
    esac
    # Wait out the move itself, then the settle gap.
    sleep "$(awk "BEGIN{print ($ms + $GAP_MS) / 1000}")"
done

echo "sequence complete"
