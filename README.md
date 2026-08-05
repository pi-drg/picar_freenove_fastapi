# picar_freenove_fastapi — self-contained robot-side control

Our own control stack for the Freenove 4WD Smart Car (FNK0043B), running on the
car's Raspberry Pi. **Depends on nothing from Freenove's repo** — only standard
Pi tooling (`smbus2`, `gpiozero`, `picamera2`) plus this package. Board-specific
constants are ported from Freenove's public example code and consolidated in
`config.py` with attribution; everything else is reimplemented clean.

This replaces both Freenove's stock port-5000 TCP server (unauthenticated,
plaintext `CMD_X#...`) **and** the earlier shim that wrapped Freenove's classes.
For a workshop this matters: attendees flash one package, not "clone Freenove +
layer our shim on top of their import paths."

## Why own the drivers instead of wrapping Freenove's

The only thing worth keeping from Freenove's drivers is the *board magic* — the
wiring facts you can't guess and don't want to re-derive on real hardware:

- Motor half-bridge channel map and the upper/lower forward-channel asymmetry,
  plus active-brake on duty 0. Note the map in `config.py` is **globally inverted
  from Freenove's published values** (LF=0/1, LR=3/2, RF=6/7, RR=4/5) — the
  published pairs drove all four wheels backward on `forward`, verified on
  hardware in both directions with the wheels raised.
- Servo calibration: pan (ch8) inverted `2500 - (angle+trim)/0.09`, tilt (ch9)
  `500 + (angle+trim)/0.09`, 1500us centre.
- ADS7830 per-channel command byte and 3.3V/5.2V PCB-revision scaling.
- Ultrasonic pins (trig 27 / echo 22) and IR line pins (14/15/23).

These are ~40 lines total, now isolated in `config.py`. The rest — I2C timing,
GPIO, camera — is standard and reimplemented.

## Layout

```
picar_freenove_fastapi/
  config.py            # BoardConfig — ALL board constants, one place
  drivers/
    pca9685.py         # PWM driver (register + pulse math)
    motor.py           # named-wheel API, drive/mecanum mixing, active brake
    servo.py           # pan/tilt calibration + travel-limit clamp
    adc.py             # ADS7830 battery/photoresistor
    sensors.py         # ultrasonic + IR line (gpiozero)
    camera.py          # picamera2 MJPEG + single-frame grab
    leds.py            # WS2812 strip over SPI + animation thread
  robot.py             # Robot facade: one lock, auto-stop timer, lazy camera/LEDs
  server.py            # our FastAPI server (optional bearer-token auth)
  requirements.txt
```

The package is imported directly (`picar_freenove_fastapi.server:app`), so the
systemd unit's `WorkingDirectory` is its **parent** directory, not the package.

## Run (on the Pi)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r picar_freenove_fastapi/requirements.txt
# picamera2 / lgpio via apt if not already present (see requirements.txt)

# optional on-robot bearer token (defence in depth on the LAN)
export ROBOT_TOKEN=$(openssl rand -hex 16)

uvicorn picar_freenove_fastapi.server:app --host 127.0.0.1 --port 8080
```

Bind to loopback; the gateway (same Pi, or over its authenticated tunnel) is the
only client. The gateway's MCP auth + reservation is still the real trust
boundary — `ROBOT_TOKEN` is a minimal extra guard, not a replacement.

## Gateway wiring

The endpoint surface is byte-for-byte what the `freenove4wd` gateway plugin's
`Freenove4wdAdapter` already dials (`/info`, `/drive`, `/mecanum`, `/look`,
`/scan`, `/distance`, `/line`, `/battery`, `/snapshot`). No gateway-side change:
just point it at the robot —
`export FREENOVE_URL=http://<pi-host>:8080`.

`GET`/`POST /led` is an **addition** on top of that contract, not a change to it.
The gateway does not know about it and is unaffected.

## LEDs

Eight WS2812s on SPI0 MOSI (GPIO10) — SPI, not I2C, so lighting never contends
with motor or sensor writes. Needs `dtparam=spi=on` and the user in the `spi`
group; `deploy/install.sh` handles both.

```bash
curl -X POST localhost:8080/led -H 'content-type: application/json' \
     -d '{"effect":"solid","r":255,"g":0,"b":0}'          # whole strip red
curl -X POST localhost:8080/led -H 'content-type: application/json' \
     -d '{"effect":"solid","index":3,"r":0,"g":0,"b":255}' # one pixel blue
curl -X POST localhost:8080/led -H 'content-type: application/json' \
     -d '{"effect":"rainbow","duration_ms":5000}'          # animate, then dark
curl -X POST localhost:8080/led -d '{"effect":"off"}' -H 'content-type: application/json'
```

Effects: `solid`, `off`, `blink`, `rainbow`, `breathing`, `chase` — the last four
ported from Freenove's `led.py`, which drove them from a caller's `while` loop.
Here each runs on a daemon thread instead. `duration_ms` bounds a run and leaves
the strip dark; omit it and the effect runs until the next `/led` call. Brightness
defaults to Freenove's cap of 55/255 — these LEDs are bright.

**Direction.** On this car the strip is wired so ascending pixel index travels
**clockwise**. `chase` and `rainbow` take `reverse` to flip that:

```bash
curl -X POST localhost:8080/led -H 'content-type: application/json' \
     -d '{"effect":"chase","reverse":true,"wait_ms":80}'   # counter-clockwise
```

Animations run on their own thread against SPI, while motors and sensors are on
I2C behind a separate lock — verified on hardware that lighting keeps animating
smoothly during driving, with no stutter.

## This car: mecanum wheels

Mecanum wheels are fitted, so `/mecanum` is fully holonomic. Sign convention, all
confirmed on the floor, from the driver's seat facing forward:

| Axis | Positive means |
|---|---|
| `vx` | drive forward |
| `vy` | strafe **left** |
| `omega` | spin **counter-clockwise**, in place |

```bash
# strafe left (no rotation)
curl -X POST localhost:8080/mecanum -H 'content-type: application/json' \
     -d '{"vx":0,"vy":1200,"omega":0,"duration_ms":800}'
# spin on the spot
curl -X POST localhost:8080/mecanum -H 'content-type: application/json' \
     -d '{"vx":0,"vy":0,"omega":1200,"duration_ms":800}'
```

`/drive` `"left"`/`"right"` also spin in place — they are differential presets, not
strafes, and produce duties identical to `omega`.

Verified 2026-08-04. On raised wheels, `vy` alone gives LF back / LR forward /
RF forward / RR back — the correct diagonal signature, which also confirms wheel
identity in `motor_channels`. Forward-only motion cannot test that: forward looks
correct even if two wheels are swapped. On the floor, spin held its centre cleanly
and strafe translated sideways in both directions.

Strafing fights the rollers and needs more duty than driving — 1200 works on a
hard floor, well above the ~700 unloaded stall floor.

## I²C throughput

Every motor update writes eight PCA9685 channels. The chip's registers are
consecutive, so MODE1's auto-increment bit (`0x20`) is enabled and writes are
batched: `set_pwm` sends one channel's four registers in a single transaction,
and `Motors.set` sends **all eight channels — the whole motor state — in one**.
The address, register and framing overhead is paid once instead of 32 times.

Measured on picar-finland-01 (Pi Zero 2 W, 100 kHz bus), full 8-channel update:

| | Transactions | Per update | Ceiling |
|---|---|---|---|
| Per-register writes (pre-2026-08-05) | 32 | 11.17 ms | 90 Hz |
| Per-channel block (`set_pwm`) | 8 | 5.14 ms | 195 Hz |
| Whole-block (`set_all_duties`) | **1** | **3.23 ms** | **310 Hz** |
| Whole-block @ 400 kHz | 1 | ~0.95 ms (projected) | ~1050 Hz |

`Motors.set` projects the named-wheel map onto positional channel slots, so
`motor_channels` must cover channels `0..n-1` with no gaps — `Motors.__init__`
enforces that, because a gap would silently coast an unmapped channel rather
than leave it alone. The batched path is verified register-identical to the
old per-wheel path across 507 input combinations.

At demo rates — one command per ~700 ms — none of this is visible. It matters
only if you drive the car from a high-rate control loop (ROS 2 `cmd_vel`,
`ros2_control`), where 11 ms against a 10 ms budget misses every deadline.

Bus speed is a device-tree parameter fixed at boot and **cannot be set from
Python**. `config.i2c_expected_hz` is declarative: the driver reads the real
clock from `/sys/bus/i2c/devices/i2c-N/of_node/clock-frequency` and logs a
warning on mismatch. To actually change it, `deploy/install.sh --i2c-fast`
(adds `dtparam=i2c_arm_baudrate=400000`), reboot, then update the config field.

Fast mode is opt-in on purpose: this HAT has long bus traces shared with the
ADS7830, so if `/battery` starts returning intermittent errors after enabling
it, that is the cause — revert the dtparam.

## Open-loop safety

No wheel encoders on the stock kit → every `/drive` and `/mecanum` schedules a
hard stop after `duration_ms` (default 800). A dropped connection or crashed
caller cannot leave the motors running. This is also why full Unified Autonomy
Stack integration stops at behavioural mission tools (no odometry → no map).
Add encoders or a Pi-I²C IMU to lift that ceiling.

## Verified (off-hardware, with faked I²C/GPIO/camera)

- Package imports; `Robot` constructs.
- Forward drive maps to the correct per-wheel forward channels (ch1/2/5/7 = duty,
  partners = 0); stop applies active brake (both channels 4095) — matches
  Freenove exactly.
- Servo pulse widths match Freenove's formula within <1us across 0/90/180° for
  both pan (inverted) and tilt.
- All 8 ADS7830 command bytes match; battery scaling correct (raw 200 → 8.16V @
  PCB v2).
- Mecanum mixing correct for strafe and spin.
- Every FastAPI endpoint returns the adapter-expected shape; bearer-token auth
  rejects missing/bad tokens and accepts the right one.

Hardware-in-the-loop calibration (servo centre trim, `duty` floor before stall,
`pcb_version`) still needs a real car — those are the values in `config.py` to
confirm on first bring-up.
