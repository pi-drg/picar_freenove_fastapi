# demos

Shell demos that drive the car through the HTTP API. Nothing here is imported by
the package — these are operator tools, safe to edit live on the Pi.

## demo.sh

A sequence is pairs of `MOVE MS`:

```bash
./demo.sh left 500 right 1000 left 500 cw 2000
./demo.sh --leds chase --duty 1500 forward 800 cw 1000
./demo.sh --dry-run left 500 cw 2000        # print, don't move
```

| Move | Effect | | Move | Effect |
|---|---|---|---|---|
| `forward` | drive forward | | `back` | drive backward |
| `left` | strafe left | | `right` | strafe right |
| `ccw` | spin counter-clockwise | | `cw` | spin clockwise |
| `pause` | hold still | | | |

Options: `--duty N` (default 1200), `--gap MS` between moves (700),
`--leds EFFECT` for the whole run, `--leds-cw` to flip LED direction,
`--host H:P`, `--dry-run`, `--help`.

Run it on the Pi (`127.0.0.1:8080` by default) or from your laptop:

```bash
ROBOT_HOST=robot.local:8080 ./demo.sh forward 500
```

## Ready-made

- **`box.sh`** — strafes a square *without changing heading*. A differential-drive
  car physically cannot do this; it's the clearest demonstration of what the
  mecanum wheels buy you. `SIDE_MS=1200 ./box.sh` for a bigger square.
- **`shimmy.sh`** — left/right/left then a clockwise spin, LEDs chasing. The
  lateral moves cancel, so leftover sideways drift is open-loop error.

## Safety

- Every move is bounded server-side by the auto-stop timer in `robot.py`. If this
  script is killed mid-run, the current move still ends on schedule.
- An `EXIT` trap stops the motors and clears the LEDs on Ctrl-C or any error.
- The whole sequence is validated before the first move, so a typo in the last
  move fails before anything has moved.
- Durations are capped at 5000ms per move as a typo guard.
- Kill switch: `sudo systemctl stop yakrobot-freenove`.

Strafing needs more duty than driving — it fights the wheel rollers. 1200 works on
a hard floor; the ~700 stall floor measured with wheels raised is not a useful
guide for lateral moves.
