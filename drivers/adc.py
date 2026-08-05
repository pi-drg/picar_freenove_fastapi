"""ADS7830 8-channel ADC driver (I2C) — battery voltage and photoresistors.

The channel command byte and the double-read "stable byte" trick are ported
from Freenove's ADC class. Voltage scaling depends on the PCB revision (set in
BoardConfig.pcb_version).
"""

try:
    import smbus2 as smbus
except ImportError:  # pragma: no cover
    import smbus

from ..config import BoardConfig


class ADC:
    def __init__(self, cfg: BoardConfig, busnum: int = 1):
        self.cfg = cfg
        self.bus = smbus.SMBus(busnum)

    def _read_stable_byte(self) -> int:
        while True:
            a = self.bus.read_byte(self.cfg.adc_address)
            b = self.bus.read_byte(self.cfg.adc_address)
            if a == b:
                return a

    def read_channel_voltage(self, channel: int) -> float:
        cmd = self.cfg.adc_command | ((((channel << 2) | (channel >> 1)) & 0x07) << 4)
        self.bus.write_byte(self.cfg.adc_address, cmd)
        raw = self._read_stable_byte()
        return round(raw / 255.0 * self.cfg.adc_voltage_coefficient, 2)

    def battery_voltage(self) -> float:
        """Pack voltage in volts (channel voltage × PCB divider multiplier)."""
        v = self.read_channel_voltage(self.cfg.adc_battery_channel)
        return round(v * self.cfg.adc_battery_multiplier, 2)

    def close(self) -> None:
        self.bus.close()
