# AGENTS.md

Operating guide for coding agents working on this repo. Read this before
touching hardware. `README.md` explains what the package *is*; this file is how
to deploy it, what is already known about the hardware, and what will bite you.

Substitute your own values for `<pi-host>` and `<pi-user>` throughout.

---

## 1. Safety rules — non-negotiable

This package drives motors on a physical robot. A wrong command sends a real car
across a real floor.

1. **Confirm the wheels are elevated before ANY drive command** — `/drive`,
   `/mecanum`, or anything calling `Motors.set`. Ask the operator; do not infer
   it from earlier in the conversation, and do not assume it carries over from a
   previous test.
2. **Cap `duration_ms` at ~1000 during bring-up.** The server auto-stops after
   `duration_ms`, but a typo'd `8000` is eight seconds of unattended motion.
   `demos/demo.sh` enforces a 5000 ms cap; ad-hoc `curl` does not.
3. **Kill switch:** `sudo systemctl stop yakrobot-freenove`. Know it before you
   start, and tell the operator it exists before a floor test.
4. **Servo buzzing means stop immediately** — it is a stalled servo drawing
   current against its end stop. Re-centre to 90 and re-check the angle clamp.
5. **Benchmarks and probes must not drive.** To time I²C, write to PCA9685
   channels 10–15 (nothing is wired to them) or write duty `0` (coast). Never
   benchmark with a non-zero duty on channels 0–7.

If you are unsure whether something moves the car, it moves the car.

---

## 2. Hardware ground truth

> **Scope: these are the reference car's values, not universal facts.** A kit
> assembled differently — motors in different ports, LED strip fitted the other
> way round, different PCB revision — will legitimately differ. The packaged
> defaults are one calibrated car, and a reasonable starting guess for a kit
> built the same way.
>
> **On a car you have not calibrated, run the selftest rather than trusting this
> section:** `python -m picar_freenove_fastapi.selftest --all` (see §2b). On a
> car that has been calibrated against these values, trust this over any external
> document, including a deployment brief.

Each of these was established on real hardware and contradicts either Freenove's
published values or the naive reading of the code. Re-deriving them costs a
hardware session; getting them wrong can damage a servo.

### Motor channel map is globally inverted

`config.py:motor_channels` is **swapped from Freenove's published values in
every pair**:

```
left_front  (0, 1)     left_rear  (3, 2)
right_front (6, 7)     right_rear (4, 5)          # (forward_ch, reverse_ch)
```

Freenove's published pairs drove all four wheels *backward* on `forward` and
forward on `back` — a clean global inversion, confirmed by testing both
directions with the wheels raised. Do not "restore" these to match Freenove's
example code or any older revision of the README.

### Servos

- **Travel is clamped to 30–150°** on both axes (`config.py:servo_angle_limits`).
  Unclamped, `pan 180` computes a 389 µs pulse — below the 500 µs spec floor and
  a route to a stalled servo.
- **Higher `pan` = RIGHT, lower = LEFT.** Easy to label backwards.
- **Tilt travels from the horizon UPWARD.** The camera is not meant to point
  down. If a spec asks for "tilt 120 = down", the spec is wrong for this car —
  raise it with the operator rather than inverting the axis.

### Mecanum sign convention

The kit ships both wheel sets. `config.wheel_type` (`"mecanum"` | `"ordinary"`)
records which is fitted, and it is enforced: with `"ordinary"`, a non-zero `vy`
raises ValueError in `Motors.mecanum` and returns **HTTP 400** from `/mecanum`,
because plain wheels cannot translate sideways. `vx`, `omega` and `/drive` work
on both.

**Check `GET /info` (`wheels`, `holonomic`) before promising a strafe** — do not
assume mecanum. Set it with the `wheels` selftest step.

The convention matches ROS REP-103 exactly, which is why a ROS 2 bridge needs no
sign juggling:

| Axis | Positive means |
|---|---|
| `vx` | forward |
| `vy` | strafe **left** |
| `omega` | spin **counter-clockwise** in place |

### Duty floors

Per-unit — motors, battery health and surface all move these. The `duty`
selftest step measures them for a given car.

- Wheels raised: ~500 stalls, ~700 turns.
- On the floor, strafing fights the rollers and needs **~1200** — the raised-wheel
  floor is not a useful guide for lateral moves.
- `box.sh` traces a rectangle rather than a square for this reason: `vy` covers
  less ground than `vx` at equal duty and duration.

### LEDs

Eight WS2812s on **SPI0 MOSI (GPIO10) — SPI, not I²C**, so lighting never
contends with motor writes. Strip orientation is per-unit: the default assumes
ascending pixel index travels **clockwise**, so `reverse: true` gives
counter-clockwise. `led_index_clockwise` records it; the `leds` selftest step
determines it.

---

## 2b. Calibrating a car you have not seen before

Do not hand-edit `config.py` to match a new car. Run the interactive selftest —
it derives the values by driving the hardware and asking the operator what
happened, and writes them to a per-unit overlay at `/etc/yakrobot/unit.json`
that `BoardConfig.load()` applies over the packaged defaults.

```bash
sudo systemctl stop yakrobot-freenove          # it owns the bus and the pins
cd ~/source && python -m picar_freenove_fastapi.selftest --all
sudo systemctl start yakrobot-freenove
```

Order matters: `bus` and `battery` first, because a flat battery makes every
later step lie, then `motors` before `mecanum`.

The `motors` step is the important one. It assumes nothing about the wiring —
it energises one PCA9685 channel at a time and asks which wheel moved and which
way, then validates the derived map and asks for a confirming forward run before
saving. This is what replaces "trust the map in `config.py`" on an unknown car.

Bursts run at duty 900 for 1.5 s, preceded by a lead-in and countdown (~4.8 s,
because the operator's eyes are on the keyboard when they press Enter), and every
question offers a replay. If the operator says they could not tell, **replay
rather than letting them guess** — a wrong answer here is silent and poisons
everything downstream. `--probe-duty`, `--probe-ms` and `--lead-ms` tune it.

`config.py` stays the packaged default; per-car values live in the overlay,
outside the repo. See `selftest/README.md`.

---

## 3. Deploying to a Pi

Target: any Raspberry Pi running Raspberry Pi OS / Debian Bookworm or Trixie,
with the Freenove 4WD HAT fitted. Nothing here assumes a particular Pi model —
the package is I/O-bound, not CPU-bound. On the smallest boards allow extra time
for first start (uvicorn can take ~5 s to bind).

### 3.1 Copy the package

The package directory **is** the importable module, so its name must stay
`picar_freenove_fastapi` (hyphens are illegal in Python module names) and its
**parent** is what goes on the import path.

```bash
rsync -a --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
      ./picar_freenove_fastapi <pi-user>@<pi-host>:~/source/
```

### 3.2 Install

```bash
ssh <pi-user>@<pi-host> '~/source/picar_freenove_fastapi/deploy/install.sh'
# add --i2c-fast only if you need a high-rate control loop (see README)
```

The installer is idempotent. It installs apt deps, enables I²C and SPI, adds the
user to the `i2c` and `spi` groups, builds the venv, and import-checks the
package.

**The venv must be built with `--system-site-packages`.** `picamera2`, `lgpio`
and `spidev` come from apt and are invisible inside an isolated venv; the server
cannot start without them.

### 3.3 Install the service

```bash
sudo cp ~/source/picar_freenove_fastapi/deploy/yakrobot-freenove.service \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now yakrobot-freenove
```

Check `WorkingDirectory` and the `ExecStart` venv path match where you actually
put the package. `WorkingDirectory` is the **parent** of the package directory.

### 3.4 Auth

`server.py` requires `Authorization: Bearer <token>` on every request when
`ROBOT_TOKEN` is set in `/etc/yakrobot/env`, and skips the check entirely when
it is unset.

> **Gotcha:** `ROBOT_TOKEN=` (empty) does **not** disable auth. `os.getenv`
> returns `""`, not `None`, so the check stays on and demands the literal string
> `"Bearer "`. To disable it, the line must be **absent** from the file.

Binding `0.0.0.0` without a token exposes an unauthenticated motor endpoint to
the whole network. If an operator asks for that, say so once, then respect the
decision — but never make it silently.

### 3.5 Public URL — WITHDRAWN from the Pi, now on the gateway host

> **Do not run a tunnel on this Pi.** Set up and verified working on
> 2026-08-16, then withdrawn the same day: the public edge moved to the
> **gateway host**, which is on the same LAN and has CPU to spare that a
> Pi Zero 2 W does not. `yakrobot-gateway` already implements both providers
> in `src/core/tunnel.py`.
>
> Current state on this Pi: systemd unit **removed**; the `cloudflared` binary,
> `/etc/cloudflared/config.yml` and the named tunnel `picar-finland-01`
> (`f8f493f1-607b-42be-8f88-a3fa1fe37925`) are **left in place but dormant**.
> The DNS record `picar-finland-01.yakrobot.com` still points at that dormant
> tunnel and returns 1033/530 until repointed or deleted.
>
> The rest of this section is kept only as a working reference for how it was
> done, should a Pi-hosted tunnel ever be wanted again.

`cloudflared` runs on the Pi and dials **out** to Cloudflare. No inbound port
forward, no dynamic DNS, no router access — which is why this beats
Caddy-plus-port-forward for a car sitting on someone else's network.

```bash
cloudflared tunnel login                                    # interactive, once
cloudflared tunnel create picar-finland-01
cloudflared tunnel route dns picar-finland-01 <hostname>    # creates the CNAME
sudo install -m644 deploy/cloudflared-config.yml.example /etc/cloudflared/config.yml
#   ...then substitute <TUNNEL_UUID> and <PUBLIC_HOSTNAME>
sudo cp deploy/cloudflared.service /etc/systemd/system/ && sudo systemctl daemon-reload
cloudflared --config /etc/cloudflared/config.yml tunnel ingress validate
```

**The unit is installed disabled, on purpose. Start it for a session, stop it
after:**

```bash
sudo systemctl start cloudflared     # public URL goes live
sudo systemctl stop  cloudflared     # URL returns 502; car is unreachable
```

`systemctl enable` is the wrong move while `ROBOT_TOKEN` is unset (§3.4): the
server does no auth, so a running tunnel is a **publicly drivable motor
endpoint** for anyone holding the URL. On-demand start is the mitigation that
costs nothing. Before enabling at boot, put a real control in front of it —
Cloudflare Access on the hostname, or set `ROBOT_TOKEN`.

Notes that will bite you:

- **The origin stays `0.0.0.0:8080`.** Do not "tighten" it to loopback to suit
  the tunnel: `yakrobot-gateway` dials this port from another host and would
  break. `127.0.0.1` in the ingress rule reaches the same listener anyway.
- **A fresh DNS route needs a moment.** Immediately after
  `tunnel route dns`, the edge returns **1033 / HTTP 530** even with the tunnel
  connected and all prechecks passing. It is propagation, not misconfiguration
  — retest before debugging.
- **`--no-autoupdate` is deliberate.** An auto-update restarts the tunnel and
  drops every live WebSocket; mid-drive that kills the control socket and the
  car coasts until the server deadman fires. Update between sessions.
- **The tunnel is not a kill switch.** Stopping it drops the control socket, so
  the car halts via §-deadman — but `sudo systemctl stop yakrobot-freenove`
  (§1.3) remains the real one.

Health check: `sudo journalctl -u cloudflared -n 40`. A working start logs four
`Registered tunnel connection` lines; on this unit they land on `hel01`/`arn07`.

---

## 4. Verifying a deployment

```bash
# service healthy
ssh <pi-user>@<pi-host> 'systemctl is-active yakrobot-freenove && \
                         systemctl is-enabled yakrobot-freenove'

# non-motor smoke test — safe with the car on the floor
curl -s <pi-host>:8080/info
curl -s <pi-host>:8080/battery
curl -s -X POST <pi-host>:8080/look -H 'content-type: application/json' \
     -d '{"pan":90,"tilt":90}'

# journal must be free of tracebacks and 500s
ssh <pi-user>@<pi-host> "sudo journalctl -u yakrobot-freenove -b --no-pager \
     | grep -iE 'error|traceback|exception'"
```

### 4.1 Actuation logging — measure motion, do not eyeball it

Every actuation writes one line to the journal, so what the car did can be read
off a timeline instead of watched. This matters more than it sounds: a raised
wheel at duty 900 spins far too fast to judge direction by eye, and a 700 ms
deadman is not eyeball-measurable at all.

```bash
ssh <pi-user>@<pi-host> "sudo journalctl -u yakrobot-freenove \
     --since '2 minutes ago' -o short-precise --no-pager \
     | grep -E 'MOTOR|SERVO|CAMERA|LED'"
```

```
10:34:30.224633 MOTOR vx=0 vy=900 omega=0 lf=-900 lr=900 rf=900 rr=-900 deadman=700ms
10:34:30.927835 MOTOR stop by=deadman lf=0 lr=0 rf=0 rr=0
10:35:12.430039 SERVO pan=120 tilt=150 (requested pan=120 tilt=200)
```

- **`-o short-precise` is not optional.** Default second-granularity timestamps
  cannot resolve a sub-second deadman.
- **`stop by=deadman` vs `by=command`** separates the auto-stop timer firing
  from a caller asking. Subtract the two timestamps and you have *measured* the
  deadman — 703 ms against a 700 ms setting, on 2026-08-16.
- **SERVO logs the clamped angle and the requested one**, so a clamp that bites
  is visible rather than mysterious.
- **What is deliberately not logged:** sensing (battery, distance, line) and
  individual video frames. Telemetry polls sensors on a timer and `/ws/video`
  runs at ~10 fps; logging either buries the lines above. `/ws/video` logs
  `CAMERA stream open` / `closed` only, and `CAMERA snapshot` covers deliberate
  stills via `/snapshot`.
- An explicit stop does not cancel the pending auto-stop timer, so a redundant
  `stop by=deadman` often follows a `by=command` a few hundred ms later. It is
  harmless — a new drive command re-arms and cancels the old timer — but do not
  read it as the deadman misfiring.

To confirm the Pi matches your working tree:

```bash
find . -type f \( -name '*.py' -o -name '*.sh' -o -name '*.md' \
                  -o -name '*.txt' -o -name '*.service' \) \
     -not -path './.venv/*' -not -path '*/__pycache__/*' -not -path './.git/*' \
     | sort | xargs sha256sum
```

Run the same command on the Pi and diff. Editing files on both sides
independently is the most common way this project drifts.

**Restarting matters.** Matching checksums do not mean matching *behaviour* —
the running process holds whatever it imported at last start. After syncing
code, `sudo systemctl restart yakrobot-freenove`.

---

## 5. Testing without breaking things

**Verify register logic with a fake bus, not the car.** The highest-risk changes
touch the channel mapping, and a wrong map is invisible until wheels turn. Swap
in a recording stub for `SMBus`, run old and new code paths over a wide range of
inputs, and diff the resulting register state. That caught the whole positional
mapping risk in the block-write change across 507 cases with zero hardware time.

**Strafe to test wheel identity.** Forward-only motion looks correct even when
two wheels are swapped. `vy` alone must give LF back / LR forward / RF forward /
RR back — the diagonal signature. This is the only cheap test for that error
class.

**Test both directions.** The globally-inverted motor map was found by testing
`back` after `forward` looked wrong. One direction is not a test.

**Ask what the operator actually saw.** "Did it spin in place or drift?" beats
inferring success from an HTTP 200 — the server returns `ok` for a command that
moved nothing.

---

## 6. Gotchas

**`pkill -f <pattern>` over SSH kills your own shell.** The pattern string
appears in the remote shell's own argv, so it matches itself and the connection
dies with exit 255. Check port state instead: `ss -ltn | grep :8080`.

**The venv is path-bound.** Scripts hardcode absolute shebangs, so renaming or
moving the package directory breaks it. Rebuild the venv after any rename.

**Ad-hoc scripts need `PYTHONPATH`.** A script in `/tmp` will not find the
package; use `PYTHONPATH=/home/<pi-user>/source`.

**lgpio drops `.lgd-nfy*` FIFOs in the working directory.** With
`WorkingDirectory=/home/<pi-user>/source` they land there, and rsync will happily
carry them back to your machine. They are ignored via `.gitignore`.

**`/drive` takes `"back"`, not `"backward"`.** An unknown direction raises
`KeyError` → HTTP 500.

**`/snapshot` returns a raw JPEG body** — not JSON, not base64 (an older note
here said otherwise; corrected 2026-08-16 against the running server). Callers
needing it inside a structured result base64 it at their own layer. For
streaming, use `/ws/video` rather than repeated snapshots.

---

## 7. Out of scope

Do not change these without explicit instruction:

- **The HTTP endpoint contract.** An external gateway adapter dials these exact
  paths and shapes. `/led` was an addition; nothing was altered. So were
  `/ws/control` and `/ws/video` (2026-08-16) — realtime teleop, documented in
  §9. Additions are fine; changing an existing path or shape is not.
- **The gateway repo or its plugin.**
- **Board constants in `config.py`** — unless you are correcting them against
  real hardware, and then update the comment with the date and what you observed.

---

## 8. Conventions

- Board constants live in `config.py` and nowhere else.
- Blocking hardware calls run via `asyncio.to_thread`; never block the event loop.
- All I²C hardware shares one lock in `robot.py`. LEDs are on SPI with their own
  lock and deliberately do not take the I²C one.
- Comments explain *why*, especially where a value contradicts an upstream
  source. Every corrected constant carries the date and the observation.
- `demos/` is operator tooling, imported by nothing, safe to edit live on the Pi.

---

## 9. Realtime teleop WebSockets

Added 2026-08-16 as contract *additions* (§7). **This section is the protocol
reference** — it is what a client implementer should read. Auth mirrors §3.4 but
takes the token as `?token=<ROBOT_TOKEN>`, because browsers cannot set headers
on a WS handshake.

| Socket | Direction | Carries |
|---|---|---|
| `/ws/control` | both | JSON text frames |
| `/ws/video` | down | binary JPEG frames, latest-frame |

Client → server on `/ws/control`:

```jsonc
{"type": "drive", "vx": 1200, "vy": 0, "omega": 0}   // absolute state
{"type": "look",  "pan": 96, "tilt": 84}             // absolute angles
{"type": "stop"}
{"type": "ping",  "t": 1723280000000}                // client clock, echoed back
```

Server → client:

```jsonc
{"type": "hello", "wheels": "mecanum", "holonomic": true, "battery_v": 7.9,
 "deadman_ms": 700, "max_duty": 1400, "controller": true}
{"type": "look", "pan": 96, "tilt": 84}                        // CLAMPED echo
{"type": "telemetry", "battery_v": 7.8, "distance_cm": 87.2}   // every control_telemetry_s
{"type": "pong", "t": 1723280000000}
{"type": "error", "detail": "busy"}
```

`hello` carries this car's deadman and duty cap so a client tunes its heartbeat
and speed limit to the server's reality instead of hardcoding them. Tunables are
`control_deadman_ms`, `control_max_duty` and `control_telemetry_s` in
`config.py`, overridable per unit in `/etc/yakrobot/unit.json`.

Things that will bite you:

- **`uvicorn` alone cannot accept a WebSocket.** It needs a protocol
  implementation, and bare `uvicorn` does not pull one in — every handshake is
  rejected until `websockets` is installed. It is in `requirements.txt`; if
  upgrades ever drop it, the symptom is a failed handshake, not an error you
  can see in this file.
- **`duration_ms` on the WS drive path IS the deadman.** Each `drive` re-arms
  `Robot._arm_auto_stop` with `control_deadman_ms`, so silence stops the car.
  There is no second watchdog — do not add one, and do not raise
  `control_deadman_ms` without understanding that it is how long a car with a
  dead link keeps rolling.
- **Control is absolute state, not deltas.** Every message carries the full
  `(vx, vy, omega)`. Keep it that way: it is what makes a lost or reordered
  frame harmless, and a delta protocol over a jittery transatlantic link
  desynchronises silently.
- **One driver at a time.** A second `/ws/control` connection is accepted but
  view-only, and told `{"type":"error","detail":"busy"}`. The slot frees on
  disconnect, which also calls `robot.stop()`.
- **Test without a car.** `yakrobot-gateway` ships a `fakerobot_picar` plugin
  whose simulator serves this exact protocol: `yakrobot sim --robot
  fakerobot_picar`. Use it before booking a hardware session.
