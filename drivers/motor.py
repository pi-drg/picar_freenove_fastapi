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
        # set() writes channels 0..n-1 as one contiguous block, so the channel
        # map must cover exactly that range with no gaps. A gap would silently
        # write 0 (coast) to an unmapped channel instead of leaving it alone.
        chans = sorted(c for pair in cfg.motor_channels.values() for c in pair)
        if chans != list(range(len(chans))):
            raise ValueError(
                f"motor_channels must cover channels 0..{len(chans) - 1} with no "
                f"gaps or duplicates for block writes; got {chans}"
            )
        self._nch = len(chans)

    @staticmethod
    def _wheel_duties(fwd_ch: int, rev_ch: int, duty: int) -> tuple:
        """(channel, duty) pairs for one wheel. Single source of the H-bridge
        polarity rule — both set() and _set_wheel go through it."""
        duty = _clamp(duty)
        if duty > 0:
            return ((rev_ch, 0), (fwd_ch, duty))
        if duty < 0:
            return ((fwd_ch, 0), (rev_ch, -duty))
        return ((fwd_ch, 4095), (rev_ch, 4095))   # active brake

    def _set_wheel(self, wheel: str, duty: int) -> None:
        """Drive one wheel. Diagnostics only — set() is the fast path."""
        fwd_ch, rev_ch = self.cfg.motor_channels[wheel]
        for ch, val in self._wheel_duties(fwd_ch, rev_ch, duty):
            self.pca.set_duty(ch, val)

    def set(self, lf: int, lr: int, rf: int, rr: int) -> None:
        """Set all four wheels by signed duty, in a single I2C transaction.

        Projects the named-wheel map onto positional channel slots — index is
        channel number — then hands the whole block to the PCA9685 at once.
        """
        duties = [0] * self._nch
        for wheel, duty in zip(self.ORDER, (lf, lr, rf, rr)):
            fwd_ch, rev_ch = self.cfg.motor_channels[wheel]
            for ch, val in self._wheel_duties(fwd_ch, rev_ch, duty):
                duties[ch] = val
        self.pca.set_all_duties(duties)

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

        Sign convention, from the driver's seat facing forward:
            vx    > 0  drive forward
            vy    > 0  strafe LEFT
            omega > 0  spin counter-clockwise, in place

        Matches ROS REP-103, so a ROS 2 bridge needs no sign juggling.

        ORDINARY WHEELS: lateral motion is physically impossible, so a non-zero
        `vy` raises ValueError rather than scrubbing the tyres and pretending.
        `vx` and `omega` still work — the mixing degenerates to differential
        drive, which is exactly right for plain wheels. Set cfg.wheel_type.

        Verified on mecanum hardware with wheels raised: vy alone gives LF back /
        LR forward / RF forward / RR back — the correct diagonal signature, which
        confirms wheel identity in motor_channels. Forward-only motion cannot
        catch that class of error, since every wheel turns forward either way.

        Strafing fights the rollers, so it needs more duty than driving: 1200
        works on a hard floor, well above the ~700 unloaded stall floor.
        """
        if vy and not self.cfg.holonomic:
            raise ValueError(
                f"vy={vy} requires mecanum wheels, but wheel_type is "
                f"{self.cfg.wheel_type!r} — this car cannot move sideways. "
                "Use vx/omega, or /drive, for ordinary wheels."
            )
        lf = _clamp(vx - vy - omega)
        lr = _clamp(vx + vy - omega)
        rf = _clamp(vx + vy + omega)
        rr = _clamp(vx - vy + omega)
        self.set(lf, lr, rf, rr)
        return (lf, lr, rf, rr)
