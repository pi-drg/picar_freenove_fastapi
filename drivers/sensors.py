"""Range and line sensors via gpiozero.

Thin wrappers — gpiozero already abstracts the GPIO timing correctly, so there's
no reason to reimplement it. Only the pin assignments are board-specific
(ported from Freenove).
"""

import warnings

from gpiozero import DistanceSensor, LineSensor

from ..config import BoardConfig


class Ultrasonic:
    def __init__(self, cfg: BoardConfig):
        warnings.filterwarnings("ignore")  # silence gpiozero PWM-fallback / no-echo noise
        self.sensor = DistanceSensor(
            echo=cfg.ultrasonic_echo_pin,
            trigger=cfg.ultrasonic_trigger_pin,
            max_distance=cfg.ultrasonic_max_distance_m,
        )

    def distance_cm(self):
        try:
            return round(float(self.sensor.distance) * 100, 1)
        except Exception:
            return None

    def close(self):
        self.sensor.close()


class LineArray:
    def __init__(self, cfg: BoardConfig):
        self.sensors = {name: LineSensor(pin) for name, pin in cfg.ir_pins.items()}

    def read(self) -> dict:
        return {name: bool(s.value) for name, s in self.sensors.items()}

    def close(self):
        for s in self.sensors.values():
            s.close()
