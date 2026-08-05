"""Four-wheel brushed-motor driver over the PCA9685.

Each wheel is an H-bridge pair of PCA9685 channels. Signed duty (-4095..4095):
positive → forward, negative → reverse, zero → active brake (both channels high,
matching Freenove's stock behaviour — the wheel resists motion rather than
coasting).
"""

from ..config import BoardConfig
from .pca9685 import PCA9685


def _clamp(duty: int) -> int:
    return max(-4095, min(4095, int(duty)))


class Motors:
    # Named wheels in a fixed order → the 4-tuple used by drive mixing.
    ORDER = ("left_front", "left_rear", "right_front", "right_rear")

    def __init__(self, pca: PCA9685, cfg: BoardConfig):
        self.pca = pca
        self.cfg = cfg

    def _set_wheel(self, wheel: str, duty: int) -> None:
        fwd_ch, rev_ch = self.cfg.motor_channels[wheel]
        duty = _clamp(duty)
        if duty > 0:
            self.pca.set_duty(rev_ch, 0)
            self.pca.set_duty(fwd_ch, duty)
        elif duty < 0:
            self.pca.set_duty(fwd_ch, 0)
            self.pca.set_duty(rev_ch, -duty)
        else:  # active brake
            self.pca.set_duty(fwd_ch, 4095)
            self.pca.set_duty(rev_ch, 4095)

    def set(self, lf: int, lr: int, rf: int, rr: int) -> None:
        """Set all four wheels by signed duty."""
        self._set_wheel("left_front", lf)
        self._set_wheel("left_rear", lr)
        self._set_wheel("right_front", rf)
        self._set_wheel("right_rear", rr)

    def stop(self) -> None:
        self.set(0, 0, 0, 0)

    # --- Convenience mixing --------------------------------------------------
    def drive(self, direction: str, duty: int) -> tuple:
        """Differential-drive presets.

        Note "left"/"right" spin the car in place; they are not strafes. On this
        car (mecanum wheels fitted) sideways motion comes from mecanum(vy=...).
        """
        mix = {
            "forward": (duty, duty, duty, duty),
            "back": (-duty, -duty, -duty, -duty),
            "left": (-duty, -duty, duty, duty),    # spin in place
            "right": (duty, duty, -duty, -duty),
            "stop": (0, 0, 0, 0),
        }[direction]
        self.set(*mix)
        return mix

    def mecanum(self, vx: int, vy: int, omega: int) -> tuple:
        """Holonomic mixing for the mecanum build (X-roller layout).

        This car HAS mecanum wheels fitted, so all three axes are real. (The
        upstream README claimed vy was inert — true only for the ordinary-wheel
        kit, not for this build.)

        Sign convention, all confirmed on the floor 2026-08-04, from the driver's
        seat facing forward:
            vx    > 0  drive forward
            vy    > 0  strafe LEFT
            omega > 0  spin counter-clockwise, in place

        Also verified with wheels raised: vy alone gives LF back / LR forward /
        RF forward / RR back — the correct diagonal signature, which confirms
        wheel identity in motor_channels. Forward-only motion cannot catch that
        class of error, since every wheel turns forward either way.

        Strafing fights the rollers, so it needs more duty than driving: 1200
        works on a hard floor, well above the ~700 unloaded stall floor.
        """
        lf = _clamp(vx - vy - omega)
        lr = _clamp(vx + vy - omega)
        rf = _clamp(vx + vy + omega)
        rr = _clamp(vx - vy + omega)
        self.set(lf, lr, rf, rr)
        return (lf, lr, rf, rr)
