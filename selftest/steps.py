"""The individual test/calibration steps.

Each takes a BoardConfig and returns a dict of config fields it learned (or {}).
Steps that spin the wheels are marked `moves=True` and gated by the runner.

Design rule: never assert this car's values. A step either checks something
universal (a chip responds, a formula holds) or asks the operator what they saw
and derives the per-unit value from the answer.
"""

from __future__ import annotations

import time
from dataclasses import replace

from ..config import BoardConfig
from ..drivers import ADC, Camera, LineArray, Leds, Motors, PCA9685, Servos, Ultrasonic
from ..drivers.pca9685 import bus_clock_hz
from .runner import ask, confirm, fail, note, ok, pause, say, step, warn, wrote

WHEELS = ("left_front", "left_rear", "right_front", "right_rear")
WHEEL_LABEL = {
    "left_front": "LEFT FRONT", "left_rear": "LEFT REAR",
    "right_front": "RIGHT FRONT", "right_rear": "RIGHT REAR",
}
PROBE_DUTY = 1800      # enough to turn a raised wheel unambiguously
PROBE_MS = 900


def _pca(cfg: BoardConfig) -> PCA9685:
    p = PCA9685(cfg.pca9685_address, busnum=cfg.i2c_busnum)
    p.set_pwm_freq(cfg.pwm_freq_hz)
    return p


# --- 1. bus ------------------------------------------------------------------

@step("bus", "I2C bus, chip presence, clock speed")
def bus_step(cfg: BoardConfig) -> dict:
    import smbus2

    say("Checking the I2C bus and the two chips on it.\n")
    bus = smbus2.SMBus(cfg.i2c_busnum)
    found = []
    for addr in (cfg.pca9685_address, cfg.adc_address):
        try:
            bus.read_byte(addr)
            found.append(addr)
            ok(f"device responds at 0x{addr:02X}")
        except OSError:
            fail(f"nothing at 0x{addr:02X}")
    bus.close()

    if cfg.pca9685_address not in found:
        fail("No PWM driver — motors and servos cannot work.")
        say("  Check the HAT is seated and I2C is enabled (raspi-config).")
        return {}

    p = _pca(cfg)
    mode1 = p._read(p.MODE1)
    if mode1 & p.AI:
        ok(f"MODE1 = 0x{mode1:02X}, auto-increment on")
    else:
        fail(f"MODE1 = 0x{mode1:02X}, auto-increment OFF — writes will be 4x slower")

    actual = bus_clock_hz(cfg.i2c_busnum)
    if actual is None:
        warn("could not read the bus clock from the device tree")
    elif actual == cfg.i2c_expected_hz:
        ok(f"bus clock {actual} Hz, matches config")
    else:
        warn(f"bus clock is {actual} Hz but config expects {cfg.i2c_expected_hz}")
        say("  Not a fault — but update i2c_expected_hz, or see install.sh --i2c-fast.")

    t0 = time.perf_counter()
    for _ in range(20):
        p.set_all_duties([0] * 8)
    per = (time.perf_counter() - t0) / 20 * 1000
    ok(f"full 8-channel update takes {per:.2f} ms ({1000 / per:.0f} Hz ceiling)")
    p.close()
    return {}


# --- 2. battery --------------------------------------------------------------

@step("battery", "battery voltage and PCB revision scaling",
      writes=("pcb_version",))
def battery_step(cfg: BoardConfig) -> dict:
    say("The ADC scaling differs between PCB revisions, so a wrong pcb_version")
    say("gives a plausible-looking but wrong voltage.\n")

    readings = {}
    for rev in (1, 2):
        adc = ADC(replace(cfg, pcb_version=rev), busnum=cfg.i2c_busnum)
        readings[rev] = adc.battery_voltage()
        adc.close()
        say(f"  as PCB v{rev}: {readings[rev]:.2f} V")

    say("\nThese cars run 2x 18650 Li-ion: roughly 8.4 V full, 7.4 V nominal,")
    say("6.4 V empty. Measure across the battery terminals if you are unsure.\n")

    idx = ask("Which reading matches the real battery voltage?",
              [f"PCB v1 — {readings[1]:.2f} V",
               f"PCB v2 — {readings[2]:.2f} V",
               "Neither / cannot tell"])
    if idx == 2:
        warn("left pcb_version unchanged")
        return {}

    rev = idx + 1
    volts = readings[rev]
    if volts < 6.5:
        warn(f"{volts:.2f} V is low — charge before driving, motors brown out first")
    elif volts > 8.6:
        warn(f"{volts:.2f} V is above a 2S full charge — check pcb_version again")
    else:
        ok(f"{volts:.2f} V, healthy")
    wrote("pcb_version", rev)
    return {"pcb_version": rev}


# --- 3. servos ---------------------------------------------------------------

@step("servos", "pan/tilt direction, travel and centring",
      writes=("servo_trim_deg", "servo_pan_inverted"))
def servos_step(cfg: BoardConfig) -> dict:
    p = _pca(cfg)
    servos = Servos(p, cfg)
    updates: dict = {}

    say("Centring both servos. Watch the camera head.\n")
    servos.set_angle("pan", 90)
    servos.set_angle("tilt", 90)
    time.sleep(0.6)

    if not confirm("Is the camera pointing straight ahead and level?"):
        say("\nNudging pan in 5-degree steps. Say when it is centred.")
        trim = cfg.servo_trim_deg
        while True:
            idx = ask(f"Current trim {trim}. Which way should it go?",
                      ["left", "right", "it is centred now", "give up, leave as is"])
            if idx >= 2:
                break
            trim += -5 if idx == 0 else 5
            trim = max(-45, min(45, trim))
            Servos(p, replace(cfg, servo_trim_deg=trim)).set_angle("pan", 90)
            time.sleep(0.4)
        if idx == 2:
            updates["servo_trim_deg"] = trim
            wrote("servo_trim_deg", trim)

    lo, hi = cfg.servo_angle_limits["pan"]
    say(f"\nMoving pan to {hi} (its clamped limit).")
    servos.set_angle("pan", hi)
    time.sleep(0.8)
    idx = ask("Which way did the camera turn?",
              ["to the RIGHT", "to the LEFT", "it did not move", "it buzzed / strained"])
    if idx == 3:
        fail("Buzzing means the servo is straining against a stop — stopping.")
        servos.set_angle("pan", 90)
        say("  Re-seat the servo horn at centre and narrow servo_angle_limits.")
        p.close()
        return updates
    if idx == 2:
        fail("No movement — check the servo lead on channel "
             f"{cfg.servo_channel_map['pan']}.")
    elif idx == 1:
        inverted = not cfg.servo_pan_inverted
        warn("Pan is mirrored relative to the packaged default.")
        updates["servo_pan_inverted"] = inverted
        wrote("servo_pan_inverted", inverted)
    else:
        ok("pan direction matches the default")

    servos.set_angle("pan", 90)
    time.sleep(0.5)

    say(f"\nMoving tilt to {hi}.")
    servos.set_angle("tilt", hi)
    time.sleep(0.8)
    idx = ask("Which way did the camera tilt?",
              ["UPWARD (toward the ceiling)", "DOWNWARD (toward the floor)",
               "it did not move", "it buzzed / strained"])
    if idx == 0:
        ok("tilt travels from the horizon upward, as expected")
    elif idx == 1:
        warn("Tilt is inverted on this car. The endpoint contract assumes "
             "higher = up; note it, or re-seat the horn 180 degrees.")
    elif idx == 3:
        fail("Buzzing — re-seat the tilt horn at centre.")

    servos.set_angle("tilt", 90)
    servos.set_angle("pan", 90)
    ok("returned to centre")
    p.close()
    return updates


# --- 4. leds -----------------------------------------------------------------

@step("leds", "strip colour order and index direction",
      writes=("led_color_order", "led_index_clockwise"))
def leds_step(cfg: BoardConfig) -> dict:
    leds = Leds(cfg)
    updates: dict = {}
    say("Lighting pixel 0 in what should be RED.\n")
    leds.off()
    leds.set_pixel(0, 255, 0, 0)
    leds.show()

    idx = ask("What colour is lit?", ["red", "green", "blue", "nothing lit"])
    if idx == 3:
        fail("No light. Check dtparam=spi=on, the spi group, and the strip lead.")
        leds.off()
        leds.close()
        return {}
    if idx != 0:
        # Our byte order puts the requested red into the wrong slot; the colour
        # seen tells us which permutation this strip actually uses.
        seen = ("red", "green", "blue")[idx]
        order = {"green": "RGB", "blue": "BRG"}[seen]
        warn(f"red appeared as {seen} — colour order is not {cfg.led_color_order}")
        updates["led_color_order"] = order
        wrote("led_color_order", order)
        leds.close()
        leds = Leds(replace(cfg, led_color_order=order))
        leds.set_pixel(0, 255, 0, 0)
        leds.show()
        if not confirm("Is pixel 0 red now?"):
            warn("still wrong — set led_color_order by hand from the strip datasheet")
    else:
        ok(f"colour order {cfg.led_color_order} is correct")

    say("\nNow lighting pixels 0, 1 and 2 to find which way the strip runs.")
    leds.off()
    for i in range(3):
        leds.set_pixel(i, 0, 80, 0)
    leds.show()
    say("Look down at the car from above, front facing away from you.\n")
    idx = ask("The three lit LEDs run in which direction from the first?",
              ["clockwise", "counter-clockwise", "cannot tell"])
    if idx < 2:
        cw = idx == 0
        updates["led_index_clockwise"] = cw
        wrote("led_index_clockwise", cw)
        if cw != cfg.led_index_clockwise:
            note("  /led `reverse` will be interpreted against this.")
    leds.off()
    leds.close()
    ok("strip cleared")
    return updates


# --- 5. motors ---------------------------------------------------------------

@step("motors", "identify each wheel and build the channel map",
      moves=True, writes=("motor_channels",))
def motors_step(cfg: BoardConfig) -> dict:
    say("This drives ONE PCA9685 channel at a time and asks what moved.")
    say("It assumes nothing about your wiring — the map is built from answers.\n")
    say(f"Each burst is {PROBE_MS} ms at duty {PROBE_DUTY}.")
    note("Forward = the wheel's top surface moves toward the FRONT of the car.")
    pause()

    p = _pca(cfg)
    n_channels = 8
    found: dict[int, tuple[str, str]] = {}
    options = [f"{WHEEL_LABEL[w]} — {d}" for w in WHEELS for d in ("forward", "reverse")]
    options.append("nothing moved")

    try:
        for ch in range(n_channels):
            say(f"\n{'-' * 52}\nChannel {ch} of {n_channels - 1}")
            duties = [0] * n_channels
            duties[ch] = PROBE_DUTY
            p.set_all_duties(duties)
            time.sleep(PROBE_MS / 1000)
            p.set_all_duties([0] * n_channels)

            idx = ask("Which wheel turned, and which way?", options)
            if idx == len(options) - 1:
                warn(f"channel {ch} did nothing — noted")
                continue
            wheel = WHEELS[idx // 2]
            direction = "forward" if idx % 2 == 0 else "reverse"
            found[ch] = (wheel, direction)
            ok(f"channel {ch} -> {WHEEL_LABEL[wheel]} {direction}")
    finally:
        p.set_all_duties([0] * n_channels)

    # Build the map, validating as we go.
    channel_map: dict[str, list] = {w: [None, None] for w in WHEELS}
    problems = []
    for ch, (wheel, direction) in found.items():
        slot = 0 if direction == "forward" else 1
        if channel_map[wheel][slot] is not None:
            problems.append(f"{WHEEL_LABEL[wheel]} {direction} claimed by "
                            f"channels {channel_map[wheel][slot]} and {ch}")
        channel_map[wheel][slot] = ch

    for wheel, pair in channel_map.items():
        if None in pair:
            problems.append(f"{WHEEL_LABEL[wheel]} is missing a "
                            f"{'forward' if pair[0] is None else 'reverse'} channel")

    say(f"\n{'-' * 52}")
    if problems:
        fail("The answers do not form a complete map:")
        for pr in problems:
            say(f"    - {pr}")
        say("\n  Nothing saved. Re-run this step; if a wheel never moved, check")
        say("  its motor lead, and remember duty must clear the stall floor.")
        p.close()
        return {}

    result = {w: tuple(pair) for w, pair in channel_map.items()}
    say("Derived channel map:\n")
    for wheel, (f, r) in result.items():
        say(f"    {WHEEL_LABEL[wheel]:<12}  forward=ch{f}  reverse=ch{r}")

    if result == cfg.motor_channels:
        ok("identical to the current config")
    else:
        warn("differs from the current config — this car is wired differently")

    if not confirm("\nSave this map?"):
        warn("not saved")
        p.close()
        return {}

    say("\nVerifying: all four wheels forward for 1 second.")
    motors = Motors(p, replace(cfg, motor_channels=result))
    motors.set(PROBE_DUTY, PROBE_DUTY, PROBE_DUTY, PROBE_DUTY)
    time.sleep(1.0)
    motors.stop()
    good = confirm("Did ALL FOUR wheels turn as if driving forward?")
    p.close()
    if not good:
        fail("Map rejected — not saved. Re-run and check the direction answers.")
        return {}
    ok("map verified")
    wrote("motor_channels", result)
    return {"motor_channels": result}


# --- 6. duty -----------------------------------------------------------------

@step("duty", "find the duty where the wheels start turning",
      moves=True, writes=("duty_floor_unloaded",))
def duty_step(cfg: BoardConfig) -> dict:
    say("Ramping all four wheels from a stall. Say when they start turning.\n")
    p = _pca(cfg)
    motors = Motors(p, cfg)
    floor = None
    try:
        for duty in range(300, 2100, 100):
            say(f"  duty {duty} ...")
            motors.set(duty, duty, duty, duty)
            time.sleep(0.8)
            motors.stop()
            if confirm(f"    Did all four wheels turn at {duty}?"):
                floor = duty
                break
    finally:
        motors.stop()
        p.close()

    if floor is None:
        fail("Never turned by 2000 — check the battery voltage and the motor leads.")
        return {}
    ok(f"unloaded stall floor is about {floor}")
    note("  On the floor, and especially strafing, you will need well above this.")
    wrote("duty_floor_unloaded", floor)
    return {"duty_floor_unloaded": floor}


# --- 7. mecanum --------------------------------------------------------------

@step("mecanum", "confirm strafing and roller orientation",
      on_floor=True)
def mecanum_step(cfg: BoardConfig) -> dict:
    say("Strafing tests something a forward test cannot: that each wheel is in")
    say("the right corner AND that the rollers form an X seen from above.")
    say("A wheel on the wrong corner makes 'strafe' come out as rotation.\n")
    duty = max(1200, cfg.duty_floor_unloaded + 500)
    say(f"Using duty {duty} for 800 ms per move.")
    pause()

    p = _pca(cfg)
    motors = Motors(p, cfg)
    results = []
    try:
        for label, (vx, vy, omega), expect in (
            ("strafe LEFT",  (0, duty, 0),  "moved LEFT, still facing the same way"),
            ("strafe RIGHT", (0, -duty, 0), "moved RIGHT, still facing the same way"),
            ("spin CCW",     (0, 0, duty),  "turned counter-clockwise on the spot"),
        ):
            say(f"\n  {label} ...")
            motors.mecanum(vx, vy, omega)
            time.sleep(0.8)
            motors.stop()
            time.sleep(0.5)
            idx = ask(f"What did the car do?",
                      [expect, "the opposite direction", "rotated instead of sliding",
                       "barely moved", "something else"])
            results.append((label, idx))
    finally:
        motors.stop()
        p.close()

    say()
    for label, idx in results:
        if idx == 0:
            ok(f"{label}: correct")
        elif idx == 1:
            fail(f"{label}: inverted — re-run the `motors` step, a pair is swapped")
        elif idx == 2:
            fail(f"{label}: rotated instead of translating")
            say("      Either two wheels are in each other's corners, or a mecanum")
            say("      wheel is mounted on the wrong side. Rollers must form an X")
            say("      when you look down at the car.")
        elif idx == 3:
            warn(f"{label}: weak — strafing needs more duty than driving; "
                 "try a harder surface or a charged battery")
        else:
            warn(f"{label}: unexpected — re-run `motors`")

    if all(idx == 0 for _, idx in results):
        ok("\nHolonomic motion confirmed on this car.")
    return {}


# --- 8. ultrasonic -----------------------------------------------------------

@step("ultrasonic", "distance sensor responds to an obstacle")
def ultrasonic_step(cfg: BoardConfig) -> dict:
    us = Ultrasonic(cfg)
    try:
        say("Reading with nothing in front of the car.\n")
        base = [us.distance_cm() for _ in range(5)]
        say(f"  readings: {[f'{d:.0f}' if d else 'none' for d in base]} cm")
        say("\nNow hold your hand about 20 cm in front of the sensor.")
        pause()
        near = [us.distance_cm() for _ in range(5)]
        say(f"  readings: {[f'{d:.0f}' if d else 'none' for d in near]} cm")

        b = [d for d in base if d]
        n = [d for d in near if d]
        if not n:
            fail("No reading with an obstacle present — check the trig/echo pins "
                 f"({cfg.ultrasonic_trigger_pin}/{cfg.ultrasonic_echo_pin}).")
        elif b and min(n) < min(b) - 5:
            ok(f"responds: {min(b):.0f} cm clear -> {min(n):.0f} cm with your hand")
        else:
            warn("readings barely changed — aim your hand squarely at the sensor "
                 "and re-run")
    finally:
        us.close()
    return {}


# --- 9. line -----------------------------------------------------------------

@step("line", "IR line sensor polarity over light and dark",
      writes=("line_active_high_on_dark",))
def line_step(cfg: BoardConfig) -> dict:
    line = LineArray(cfg)
    try:
        say("Place the car over a plain LIGHT surface.")
        pause()
        light = line.read()
        say(f"  light surface: {light}")

        say("\nNow place the car so the middle sensor sits over a DARK line.")
        pause()
        dark = line.read()
        say(f"  dark line:     {dark}")

        if light == dark:
            fail("Identical readings — the sensors are not distinguishing the line.")
            say("  Adjust the potentiometers on the IR board until the LEDs change,")
            say("  and check the ride height (they want ~1-2 cm).")
            return {}

        changed = [k for k in light if light[k] != dark[k]]
        ok(f"sensors responding: {', '.join(changed)}")
        active_high = bool(dark.get(changed[0]))
        wrote("line_active_high_on_dark", active_high)
        note(f"  a sensor reads {int(active_high)} over a dark line on this car")
        return {"line_active_high_on_dark": active_high}
    finally:
        line.close()


# --- 10. camera --------------------------------------------------------------

@step("camera", "capture a frame and check it is a valid image")
def camera_step(cfg: BoardConfig) -> dict:
    out = "/tmp/selftest-snapshot.jpg"
    cam = Camera(cfg)
    try:
        cam.start()
        time.sleep(1.0)                 # let auto-exposure settle
        jpeg = cam.grab_jpeg()
    finally:
        cam.close()

    if not jpeg.startswith(b"\xff\xd8"):
        fail("Frame is not a JPEG — the camera stack is misconfigured.")
        return {}
    with open(out, "wb") as fh:
        fh.write(jpeg)
    ok(f"captured {len(jpeg):,} bytes, {cfg.camera_stream_size[0]}x"
       f"{cfg.camera_stream_size[1]}")
    say(f"\n  Saved to {out}. Copy it off and look at it:")
    say(f"    scp <pi-user>@<pi-host>:{out} .")
    say("\n  Check it is in focus, the right way up, and not all black.")
    return {}
