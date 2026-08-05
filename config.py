"""Board configuration for the Freenove 4WD Smart Car.

All hardware-specific constants live here, ported from Freenove's stock driver
code (Code/Server/*.py in the FNK0043B kit) so the rest of the package reads as
clean, board-agnostic logic. These values are wiring facts about the PCB — do
not "simplify" them without checking against real hardware.

Attribution: pin assignments, PCA9685 channel maps, servo calibration, and the
ADS7830 command/scaling are derived from Freenove's example code (public,
per-kit). We reimplement the drivers; we carry over the constants.
"""

import json
import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)

# Per-unit calibration overlay. The defaults below describe ONE calibrated car;
# a differently-assembled kit will have a different motor_channels map, servo
# trim, LED colour order and so on. `python -m picar_freenove_fastapi.selftest`
# measures those on your car and writes them here, leaving this file untouched.
UNIT_CONFIG_ENV = "PICAR_UNIT_CONFIG"
DEFAULT_UNIT_CONFIG = Path("/etc/yakrobot/unit.json")

# Fields whose JSON form is a list-of-lists or list but must be tuples in Python.
_TUPLE_VALUED = {"motor_channels", "servo_angle_limits"}
_TUPLE_FIELDS = {"camera_stream_size"}


def unit_config_path() -> Path:
    """Where the per-unit overlay lives. $PICAR_UNIT_CONFIG wins if set."""
    override = os.getenv(UNIT_CONFIG_ENV)
    return Path(override) if override else DEFAULT_UNIT_CONFIG


@dataclass
class BoardConfig:
    # --- PCA9685 (I2C PWM driver for motors + servos) ------------------------
    pca9685_address: int = 0x40
    pwm_freq_hz: int = 50
    i2c_busnum: int = 1

    # Expected I2C bus clock. This is DECLARATIVE — the bus speed is a
    # device-tree parameter fixed at boot and cannot be set from Python. Changing
    # this field alone does nothing; it only tells the driver what to expect so a
    # mismatch is logged instead of silently costing you throughput.
    #
    # To actually switch to fast mode: `deploy/install.sh --i2c-fast` (or add
    # `dtparam=i2c_arm_baudrate=400000` to /boot/firmware/config.txt), reboot,
    # then set this to 400_000.
    #
    # Measured on picar-finland-01 (Pi Zero 2 W), full 8-channel motor update:
    #     100 kHz, per-register writes  11.3 ms   <- pre-2026-08-05 behaviour
    #     100 kHz, block write           3.4 ms   <- current, see PCA9685.set_pwm
    #     400 kHz, block write          ~0.95 ms  (projected)
    # Only worth changing if you need a high-rate control loop; at demo rates
    # (one command per ~700 ms) the difference is invisible. Fast mode also
    # raises bus-integrity risk on this HAT's long traces — if reads start
    # failing intermittently after enabling it, that is the cause.
    i2c_expected_hz: int = 100_000

    # Motor half-bridge channel pairs, per wheel. Each wheel is two PCA9685
    # channels; `fwd` is the channel driven with duty for FORWARD motion, `rev`
    # the other. Ported verbatim from Freenove Ordinary_Car — the upper/lower
    # asymmetry is real (upper wheels forward on the high channel, lower on the
    # low channel).                        (fwd_channel, rev_channel)
    # Corrected on hardware 2026-08-04 (picar-finland-01): the ported map drove
    # all four wheels backward on `forward`, and forward on `back` — a clean
    # global inversion, verified by testing both directions with wheels raised.
    # Every pair is swapped from Freenove's published values.
    motor_channels: dict = field(default_factory=lambda: {
        "left_front":  (0, 1),   # LU: ch0=fwd, ch1=rev
        "left_rear":   (3, 2),   # LL: ch3=fwd, ch2=rev
        "right_front": (6, 7),   # RU: ch6=fwd, ch7=rev
        "right_rear":  (4, 5),   # RL: ch4=fwd, ch5=rev
    })

    # Advisory only — nothing reads this. Measured 2026-08-04 on picar-finland-01
    # with the wheels RAISED: duty 500 stalled, 700 turned them. On the ground,
    # under the car's own weight, the real floor will be higher than this.
    duty_floor_unloaded: int = 700

    # --- Servos (also on the PCA9685, channels 8-15) -------------------------
    servo_channel_map: dict = field(default_factory=lambda: {
        "pan": 8,    # Freenove servo '0' — camera horizontal
        "tilt": 9,   # Freenove servo '1' — camera vertical
    })
    servo_initial_pulse_us: int = 1500
    servo_deg_per_us: float = 0.09    # 180° over a 500-2500us span
    servo_trim_deg: int = 10          # Freenove `error` offset
    servo_pan_inverted: bool = True   # pan (ch8) uses 2500 - x; others 500 + x

    # Travel limits per servo, degrees (min, max). set_servo_pulse_us applies no
    # microsecond clamp, so this angle clamp is the ONLY guard against driving a
    # servo into its mechanical stop. With trim=10 the inverted pan formula gives
    # 2500 - (170+10)/0.09 = 500us at angle 170, so anything above that leaves the
    # servo's spec band; 150 keeps a margin (722us). Set on hardware 2026-08-04.
    servo_angle_limits: dict = field(default_factory=lambda: {
        "pan": (30, 150),
        "tilt": (30, 150),
    })

    # --- Ultrasonic (gpiozero DistanceSensor, BCM pins) ----------------------
    ultrasonic_trigger_pin: int = 27
    ultrasonic_echo_pin: int = 22
    ultrasonic_max_distance_m: float = 3.0

    # --- IR line sensors (gpiozero LineSensor, BCM pins) ---------------------
    ir_pins: dict = field(default_factory=lambda: {
        "left": 14, "middle": 15, "right": 23,
    })

    # --- ADC (ADS7830, I2C) --------------------------------------------------
    adc_address: int = 0x48
    adc_command: int = 0x84
    # PCB v1 → 3.3V ref, battery x3; PCB v2 → 5.2V ref, battery x2.
    # Set to match YOUR board (printed on the PCB / Freenove params.json).
    pcb_version: int = 2
    adc_battery_channel: int = 2

    @property
    def adc_voltage_coefficient(self) -> float:
        return 3.3 if self.pcb_version == 1 else 5.2

    @property
    def adc_battery_multiplier(self) -> int:
        return 3 if self.pcb_version == 1 else 2

    # --- WS2812 LED strip (SPI, NOT I2C) -------------------------------------
    # Ported from Freenove spi_ledpixel.py + params.json. This car reports
    # Connect_Version 2, which selects the SPI driver with GRB byte order.
    # Requires `dtparam=spi=on` and the user in the `spi` group.
    led_count: int = 8
    led_color_order: str = "GRB"
    led_brightness: int = 55      # Freenove's MAX_BRIGHTNESS — these are bright
    led_spi_bus: int = 0          # SPI0 MOSI = GPIO10 is the WS2812 data line
    led_spi_device: int = 0
    led_spi_hz: int = 6_400_000   # int(8 / 1.25e-6) — one SPI byte per WS2812 bit

    # --- Camera --------------------------------------------------------------
    camera_stream_size: tuple = (640, 480)

    # --- LED strip orientation (per-unit) ------------------------------------
    # True when ascending pixel index travels clockwise viewed from above. The
    # `reverse` flag on /led is interpreted against this, so a strip fitted the
    # other way round still animates in the requested direction.
    led_index_clockwise: bool = True

    # --- IR line sensors (per-unit) ------------------------------------------
    # True when a sensor reads 1 over a DARK line. Freenove's boards ship both
    # polarities; the selftest determines which you have.
    line_active_high_on_dark: bool = True

    # --- Per-unit overlay ----------------------------------------------------
    @classmethod
    def load(cls, path: Path | str | None = None) -> "BoardConfig":
        """Defaults with this car's calibration applied on top.

        Missing file is not an error — you get the defaults, which are one
        calibrated unit's values and a reasonable starting guess for a kit
        assembled the same way. Run the selftest to make them yours.
        """
        cfg = cls()
        p = Path(path) if path is not None else unit_config_path()
        if not p.exists():
            return cfg
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("ignoring unreadable unit config %s: %s", p, exc)
            return cfg

        known = {f.name for f in fields(cls)}
        for key, value in data.items():
            if key.startswith("_"):          # _comment, _calibrated_at, ...
                continue
            if key not in known:
                log.warning("unit config %s: unknown field %r, ignored", p, key)
                continue
            if key in _TUPLE_VALUED and isinstance(value, dict):
                value = {k: tuple(v) for k, v in value.items()}
            elif key in _TUPLE_FIELDS and isinstance(value, list):
                value = tuple(value)
            setattr(cfg, key, value)
        log.info("applied unit config from %s (%d fields)", p, len(data))
        return cfg

    def unit_overrides(self) -> dict:
        """Fields differing from the defaults — what the selftest has written."""
        base = type(self)()
        return {f.name: getattr(self, f.name) for f in fields(self)
                if getattr(self, f.name) != getattr(base, f.name)}
