"""PCA9685 16-channel PWM driver (I2C).

Reimplemented from the datasheet; register constants and the prescale/pulse
math match Freenove's stock driver so timing is identical on the FNK0043B board.
"""

import math
import time

try:
    import smbus2 as smbus
except ImportError:  # pragma: no cover - Pi has one or the other
    import smbus


class PCA9685:
    MODE1 = 0x00
    PRESCALE = 0xFE
    LED0_ON_L = 0x06

    def __init__(self, address: int = 0x40, busnum: int = 1):
        self.address = address
        self.bus = smbus.SMBus(busnum)
        self._write(self.MODE1, 0x00)

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
        base = self.LED0_ON_L + 4 * channel
        self.bus.write_byte_data(self.address, base, on & 0xFF)
        self.bus.write_byte_data(self.address, base + 1, on >> 8)
        self.bus.write_byte_data(self.address, base + 2, off & 0xFF)
        self.bus.write_byte_data(self.address, base + 3, off >> 8)

    def set_duty(self, channel: int, duty: int) -> None:
        """Set a raw 12-bit duty (0-4095) on a channel (on-time from 0)."""
        self.set_pwm(channel, 0, max(0, min(4095, duty)))

    def set_servo_pulse_us(self, channel: int, pulse_us: float) -> None:
        """Drive a servo channel with a pulse width in microseconds (@50Hz)."""
        off = int(pulse_us * 4096 / 20000)  # 20ms period at 50Hz
        self.set_pwm(channel, 0, off)

    def close(self) -> None:
        self.bus.close()
