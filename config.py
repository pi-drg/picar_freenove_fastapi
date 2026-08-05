"""Board configuration for the Freenove 4WD Smart Car.

All hardware-specific constants live here, ported from Freenove's stock driver
code (Code/Server/*.py in the FNK0043B kit) so the rest of the package reads as
clean, board-agnostic logic. These values are wiring facts about the PCB — do
not "simplify" them without checking against real hardware.

Attribution: pin assignments, PCA9685 channel maps, servo calibration, and the
ADS7830 command/scaling are derived from Freenove's example code (public,
per-kit). We reimplement the drivers; we carry over the constants.
"""

from dataclasses import dataclass, field


@dataclass
class BoardConfig:
    # --- PCA9685 (I2C PWM driver for motors + servos) ------------------------
    pca9685_address: int = 0x40
    pwm_freq_hz: int = 50

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
