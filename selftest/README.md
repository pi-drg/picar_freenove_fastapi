# selftest — hardware check and per-unit calibration

Ten steps you run one at a time. Each exercises one subsystem, asks what you
saw, and writes what it learned to a per-unit config overlay.

**The defaults in `config.py` describe one calibrated car.** Yours may have the
motors in different ports, the LED strip fitted the other way round, or a
different PCB revision. This tool measures your car instead of assuming ours.

```bash
sudo systemctl stop yakrobot-freenove          # it owns the hardware

cd ~/source
python -m picar_freenove_fastapi.selftest              # list the steps
python -m picar_freenove_fastapi.selftest bus          # run one
python -m picar_freenove_fastapi.selftest --all        # run them in order
python -m picar_freenove_fastapi.selftest --show       # print current calibration

sudo systemctl start yakrobot-freenove
```

The server holds the I²C bus and the GPIO pins, so the runner refuses to start
while it is running rather than fighting it for the hardware.

## The steps

| Step | Checks | Calibrates | Car must be |
|---|---|---|---|
| `bus` | I²C, PCA9685, ADC, clock, write speed | — | anywhere |
| `battery` | voltage plausible | `pcb_version` | anywhere |
| `wheels` | which wheel set is fitted | `wheel_type` | anywhere |
| `servos` | pan/tilt direction and travel | `servo_trim_deg`, `servo_pan_inverted` | anywhere |
| `leds` | strip lights, colours correct | `led_color_order`, `led_index_clockwise` | anywhere |
| `motors` | every wheel turns both ways | **`motor_channels`** | **wheels raised** |
| `duty` | wheels clear the stall floor | `duty_floor_unloaded` | **on the floor** |
| `mecanum` | strafing translates, not rotates | — | **on the floor** |
| `ultrasonic` | distance responds to an obstacle | — | anywhere |
| `line` | IR sensors see a dark line | `line_active_high_on_dark` | anywhere |
| `camera` | frame captured and valid | — | anywhere |

Run them in listed order on a new car: `bus` and `battery` first (a flat battery
makes every later step lie), `wheels` before `mecanum` (it decides whether that
step applies at all), and `motors` before `mecanum`.

## How `motors` works

It assumes **nothing** about your wiring. It drives one PCA9685 channel at a
time and asks which wheel moved and which way. Eight questions later it has the
complete map, checks it for gaps and duplicates, then drives all four wheels
forward and asks you to confirm before saving anything.

Every burst runs at duty 900 for 1.5 s — deliberately slow, because you need to
read the *direction*, not just notice movement. You get ~4.8 s of warning first
(a pause to look up from the keyboard, then a 3-2-1), and **every question has a
"show me that again" option**, so a missed burst costs a keypress rather than a
guess. Guessing corrupts the calibration silently, which is much worse.

```bash
# a stiff drivetrain may stall at 900 — give it more
python -m picar_freenove_fastapi.selftest --probe-duty 1200 motors
python -m picar_freenove_fastapi.selftest --probe-ms 2500 motors
python -m picar_freenove_fastapi.selftest --lead-ms 3000 motors   # slower lead-in
```

If a wheel never moves, its lead is loose or the duty is below the stall floor.
If two channels claim the same wheel and direction, nothing is saved and you
re-run — a partial map is worse than none.

## Ordinary vs mecanum wheels

The `wheels` step asks which set you fitted and writes `wheel_type`. With
`"ordinary"`, strafing is disabled everywhere: `Motors.mecanum` raises on a
non-zero `vy`, `/mecanum` returns HTTP 400, and `demos/demo.sh` refuses a
sequence containing `left` or `right`. `vx`, `omega` and `/drive` are unaffected,
and the `mecanum` step skips itself with a note.

Run this before `mecanum`, or you will be asked to test a manoeuvre the car
cannot perform.

## Why `mecanum` matters

Forward motion looks correct even when two wheels are in each other's corners.
Strafing does not: it comes out as rotation. `mecanum` is the only step that
detects a wheel on the wrong corner, or a mecanum wheel mounted on the wrong
side — the rollers must form an **X** when you look down at the car.

## Where calibration is stored

`/etc/yakrobot/unit.json`, overridable with `$PICAR_UNIT_CONFIG` or `--config`.
`BoardConfig.load()` applies it over the packaged defaults, so the file only
contains what differs on your car:

```json
{
  "motor_channels": {
    "left_front": [1, 0], "left_rear": [2, 3],
    "right_front": [7, 6], "right_rear": [5, 4]
  },
  "pcb_version": 1,
  "duty_floor_unloaded": 900,
  "_calibrated_at": "2026-08-05T18:04:11+00:00"
}
```

It is outside the repo on purpose: it belongs to the car, not the code, and it
survives re-cloning. Steps merge into it rather than rewriting it, so running
one step never discards another's work. Delete the file to return to defaults.

## Safety

Steps that move the car are gated. `motors` asks you to confirm all four
wheels are off the ground; `duty` and `mecanum` ask you to confirm the floor
is clear, since both drive the car across it. Ctrl-C at any point cuts the
motors, and so does an unexpected error.

Nothing here bypasses the auto-stop timer in `robot.py` — these steps drive the
motors directly and stop them explicitly, so an interrupted step leaves the car
stopped, not coasting.
