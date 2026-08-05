"""PCA9685 16-channel PWM driver (I2C).

Reimplemented from the datasheet; register constants and the prescale/pulse
math match Freenove's stock driver so timing is identical on the FNK0043B board.
"""

import logging
import math
import time

try:
    import smbus2 as smbus
except ImportError:  # pragma: no cover - Pi has one or the other
    import smbus

log = logging.getLogger(__name__)


def bus_clock_hz(busnum: int = 1) -> int | None:
    """Actual I2C bus clock, from the device tree. None if unreadable.

    The bus speed is fixed at boot by `dtparam=i2c_arm_baudrate` and cannot be
    changed from userspace, so this is the only way to know what we really got.
    Readable without root, and derived from the bus number so there is no
    peripheral-address translation to get wrong.
    """
    path = f"/sys/bus/i2c/devices/i2c-{busnum}/of_node/clock-frequency"
    try:
        with open(path, "rb") as fh:
            return int.from_bytes(fh.read(4), "big")   # device-tree cells are big-endian
    except (OSError, ValueError):
        return None


class PCA9685:
    MODE1 = 0x00
    PRESCALE = 0xFE
    LED0_ON_L = 0x06
    AI = 0x20          # MODE1 bit 5: auto-increment the register pointer

    def __init__(self, address: int = 0x40, busnum: int = 1,
                 expected_hz: int | None = None):
        self.address = address
        self.busnum = busnum
        self.bus = smbus.SMBus(busnum)
        # AI on: lets set_pwm write a channel's four registers in ONE transaction
        # instead of four. Measured 2x on a full motor update; see config.py.
        # set_pwm_freq preserves this bit (it reads MODE1 before modifying it).
        self._write(self.MODE1, self.AI)

        if expected_hz is not None:
            actual = bus_clock_hz(busnum)
            if actual is not None and actual != expected_hz:
                log.warning(
                    "I2C bus %d runs at %d Hz but config expects %d Hz. "
                    "Bus speed is set by dtparam=i2c_arm_baudrate at boot, not "
                    "from Python — see config.i2c_expected_hz.",
                    busnum, actual, expected_hz,
                )

    def _write(self, reg: int, value: int) -> None:
        self.bus.write_byte_data(self.address, reg, value)

    def _read(self, reg: int) -> int:
        return self.bus.read_byte_data(self.address, reg)

    def set_pwm_freq(self, freq_hz: float) -> None:
        """Set PWM frequency. 25MHz internal osc, 12-bit counter."""
        prescale = math.floor(25_000_000.0 / 4096.0 / float(freq_hz) - 1.0 + 0.5)
        oldmode = self._read(self.MODE1)
        self._write(self.MODE1, (oldmode & 0x7F) | 0x10)  # sleep to set prescale
        self._write(self.PRESCALE, int(prescale))
        self._write(self.MODE1, oldmode)
        time.sleep(0.005)
        self._write(self.MODE1, oldmode | 0x80)  # restart

    def set_pwm(self, channel: int, on: int, off: int) -> None:
        """Write a channel's ON/OFF pair.

        One I2C transaction, not four: the channel's registers are consecutive
        (base..base+3) and MODE1's AI bit advances the register pointer for us,
        so the address/register/framing overhead is paid once instead of four
        times. Requires AI to be set — see __init__.
        """
        base = self.LED0_ON_L + 4 * channel
        self.bus.write_i2c_block_data(
            self.address, base, [on & 0xFF, on >> 8, off & 0xFF, off >> 8]
        )

    def set_all_duties(self, duties: list[int]) -> None:
        """Write channels 0..len(duties)-1 in ONE transaction.

        The batched form of set_duty. Channel registers are consecutive, so with
        AI set the whole run goes out as a single I2C transaction instead of one
        per channel. `duties[i]` is the 12-bit duty for channel i, on-time from 0.

        POSITIONAL: index *is* channel number. Callers holding a name->channel
        map must project through it — see Motors.set.

        8 channels = 32 payload bytes, exactly the SMBus block limit, so this
        cannot be extended over the servo channels at 8 and 9.
        """
        payload: list[int] = []
        for d in duties:
            d = max(0, min(4095, int(d)))
            payload += [0, 0, d & 0xFF, d >> 8]
        if len(payload) > 32:
            raise ValueError(
                f"{len(duties)} channels = {len(payload)} bytes exceeds the "
                "32-byte SMBus block limit; write them in two calls"
            )
        self.bus.write_i2c_block_data(self.address, self.LED0_ON_L, payload)

    def set_duty(self, channel: int, duty: int) -> None:
        """Set a raw 12-bit duty (0-4095) on a channel (on-time from 0)."""
        self.set_pwm(channel, 0, max(0, min(4095, duty)))

    def set_servo_pulse_us(self, channel: int, pulse_us: float) -> None:
        """Drive a servo channel with a pulse width in microseconds (@50Hz)."""
        off = int(pulse_us * 4096 / 20000)  # 20ms period at 50Hz
        self.set_pwm(channel, 0, off)

    def close(self) -> None:
        self.bus.close()
