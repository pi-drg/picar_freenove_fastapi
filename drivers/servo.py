"""Pan/tilt servo driver over the PCA9685.

Angle→pulse calibration is ported from Freenove's Servo class:
  pan  (inverted): pulse_us = 2500 - (angle + trim) / deg_per_us
  tilt (normal):   pulse_us =  500 + (angle + trim) / deg_per_us
with deg_per_us = 0.09 (a 500-2500us span mapping to ~0-180°) and a small trim
offset. Keeping these exact avoids the servos hitting mechanical stops or
sitting off-centre.
"""

from ..config import BoardConfig
from .pca9685 import PCA9685


class Servos:
    def __init__(self, pca: PCA9685, cfg: BoardConfig):
        self.pca = pca
        self.cfg = cfg
        # Centre both servos on init (Freenove drives 1500us to all channels).
        for ch in cfg.servo_channel_map.values():
            self.pca.set_servo_pulse_us(ch, cfg.servo_initial_pulse_us)

    def set_angle(self, name: str, angle: int) -> int:
        if name not in self.cfg.servo_channel_map:
            raise ValueError(f"Unknown servo '{name}'. Known: {list(self.cfg.servo_channel_map)}")
        # Clamp to this servo's travel limits, not the full 0-180 span — the
        # pulse math has no microsecond guard, so out-of-range angles would drive
        # against a mechanical stop. Returned value is the clamped one, so the
        # caller sees where the servo actually went.
        lo, hi = self.cfg.servo_angle_limits.get(name, (0, 180))
        angle = int(max(lo, min(hi, angle)))
        trim = self.cfg.servo_trim_deg
        span = (angle + trim) / self.cfg.servo_deg_per_us
        inverted = self.cfg.servo_pan_inverted and name == "pan"
        pulse_us = 2500 - span if inverted else 500 + span
        self.pca.set_servo_pulse_us(self.cfg.servo_channel_map[name], pulse_us)
        return angle

    def look(self, pan: int = 90, tilt: int = 90) -> dict:
        return {"pan": self.set_angle("pan", pan), "tilt": self.set_angle("tilt", tilt)}
