"""WS2812 LED strip driver over SPI.

Eight addressable WS2812s sit on SPI0 MOSI (GPIO10). WS2812 has no clock line —
the timing is encoded into the SPI bitstream itself: every colour bit becomes one
SPI byte, 0xF8 (long high pulse) for a '1' and 0x80 (short high pulse) for a '0',
clocked at 6.4MHz so each byte spans ~1.25us. That is the whole trick.

Board facts ported from Freenove's spi_ledpixel.py and params.json: the packaged
defaults assume Connect_Version 2, which selects the SPI path with GRB ordering
and a brightness cap of 55. A board wired otherwise needs a different
led_color_order — the `leds` selftest step determines it. The bit patterns, clock rate and colour-order table are theirs; the
encoding is reimplemented in pure Python because 8 LEDs is 192 bytes and numpy
buys nothing at that size.

The strip is on SPI, not the I2C bus, so this driver keeps its own lock and does
NOT contend with the Robot's I2C lock — LED animation can run while the car
drives without serialising against motor writes.
"""

import threading
import time

import spidev

from ..config import BoardConfig

# One SPI byte per colour bit, MSB first.
_BIT_HIGH = 0xF8   # 0b11111000 — long pulse  = '1'
_BIT_LOW = 0x80    # 0b10000000 — short pulse = '0'

# Freenove's led_type / led_type_offset tables, packed two bits per channel.
_ORDER_OFFSETS = {
    "RGB": 0x06, "RBG": 0x09, "GRB": 0x12,
    "GBR": 0x21, "BRG": 0x18, "BGR": 0x24,
}

EFFECTS = ("solid", "off", "blink", "rainbow", "breathing", "chase")

# Per-effect default frame interval, from led.py's wait_ms defaults.
_DEFAULT_WAIT_MS = {"blink": 300, "rainbow": 20, "breathing": 10, "chase": 50}


def wheel(pos: int) -> tuple:
    """Position 0-255 around the colour wheel to an (r, g, b) triple."""
    pos &= 255
    if pos < 85:
        return (pos * 3, 255 - pos * 3, 0)
    if pos < 170:
        pos -= 85
        return (255 - pos * 3, 0, pos * 3)
    pos -= 170
    return (0, pos * 3, 255 - pos * 3)


class Leds:
    def __init__(self, cfg: BoardConfig):
        self.cfg = cfg
        self.count = cfg.led_count
        self.brightness = max(0, min(255, cfg.led_brightness))
        self.effect = "off"

        offsets = _ORDER_OFFSETS.get(cfg.led_color_order, _ORDER_OFFSETS["GRB"])
        self._r_off = (offsets >> 4) & 0x03
        self._g_off = (offsets >> 2) & 0x03
        self._b_off = offsets & 0x03

        self._pixels = [(0, 0, 0)] * self.count
        self._lock = threading.RLock()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None

        self.spi = spidev.SpiDev()
        self.spi.open(cfg.led_spi_bus, cfg.led_spi_device)
        self.spi.mode = 0
        self.off()

    # --- wire encoding -------------------------------------------------------
    def _encode(self) -> list:
        buf = []
        for r, g, b in self._pixels:
            triple = [0, 0, 0]
            triple[self._r_off] = round(r * self.brightness / 255)
            triple[self._g_off] = round(g * self.brightness / 255)
            triple[self._b_off] = round(b * self.brightness / 255)
            for value in triple:
                for bit in range(7, -1, -1):
                    buf.append(_BIT_HIGH if (value >> bit) & 1 else _BIT_LOW)
        return buf

    def show(self) -> None:
        with self._lock:
            self.spi.xfer(self._encode(), self.cfg.led_spi_hz)

    # --- direct control ------------------------------------------------------
    def set_all(self, r: int, g: int, b: int) -> None:
        with self._lock:
            self._pixels = [(r, g, b)] * self.count
        self.show()

    def set_pixel(self, index: int, r: int, g: int, b: int) -> None:
        if not 0 <= index < self.count:
            raise ValueError(f"LED index {index} out of range 0-{self.count - 1}")
        with self._lock:
            self._pixels[index] = (r, g, b)
        self.show()

    def set_brightness(self, brightness: int) -> None:
        with self._lock:
            self.brightness = max(0, min(255, brightness))
        self.show()

    def off(self) -> None:
        self.stop_effect()
        with self._lock:
            self.effect = "off"
        self.set_all(0, 0, 0)

    # --- animations ----------------------------------------------------------
    def stop_effect(self) -> None:
        """Halt any running animation thread and wait for it to actually exit."""
        thread = self._thread
        if thread is not None and thread.is_alive():
            self._stop_evt.set()
            thread.join(timeout=2.0)
        self._thread = None
        self._stop_evt.clear()

    def start_effect(self, name: str, duration_ms: int | None = None,
                     wait_ms: int | None = None, color: tuple | None = None,
                     reverse: bool = False) -> dict:
        """Start an animation on a daemon thread.

        `reverse` flips travel direction for the two directional effects, `chase`
        and `rainbow`. Which way that is depends on how the strip is fitted; the
        default assumes ascending pixel index runs CLOCKWISE, so reverse=True
        gives counter-clockwise. See cfg.led_index_clockwise.
        """
        if name not in EFFECTS:
            raise ValueError(f"Unknown effect '{name}'. Known: {list(EFFECTS)}")
        self.stop_effect()
        wait_ms = wait_ms or _DEFAULT_WAIT_MS.get(name, 50)
        with self._lock:
            self.effect = name
        self._thread = threading.Thread(
            target=self._effect_loop,
            args=(name, duration_ms, wait_ms, color, reverse),
            daemon=True,
        )
        self._thread.start()
        return self.state()

    def _effect_loop(self, name: str, duration_ms, wait_ms: int, color,
                     reverse: bool = False) -> None:
        deadline = time.time() + duration_ms / 1000.0 if duration_ms else None
        blink_seq = [(255, 0, 0), (0, 0, 0), (0, 255, 0), (0, 0, 0), (0, 0, 255), (0, 0, 0)]
        step = 0        # generic frame counter
        chase_i = 0     # which pixel the chase is lit on
        breath = 0      # 0-199 triangle ramp for breathing
        hue = 0

        while not self._stop_evt.is_set():
            if deadline is not None and time.time() >= deadline:
                break

            if name == "blink":
                self._pixels = [blink_seq[step % len(blink_seq)]] * self.count
            elif name == "rainbow":
                drift = -step if reverse else step
                self._pixels = [
                    wheel(int(i * 256 / self.count) + drift) for i in range(self.count)
                ]
            elif name == "breathing":
                base = color or wheel(hue)
                # Triangle wave: ramp up over 0-99, back down over 100-199.
                level = breath if breath <= 100 else 200 - breath
                self._pixels = [tuple(int(c * level / 100) for c in base)] * self.count
                breath += 1
                if breath >= 200:
                    breath = 0
                    hue = (hue + 32) % 256
            elif name == "chase":
                self._pixels = [(0, 0, 0)] * self.count
                self._pixels[chase_i] = color or wheel(step * 5)
                chase_i = (chase_i - 1 if reverse else chase_i + 1) % self.count

            self.show()
            step += 1
            if self._stop_evt.wait(wait_ms / 1000.0):
                return  # explicitly stopped — leave the strip as the caller wants it

        # Ran to its natural end: leave the strip dark rather than frozen mid-frame.
        with self._lock:
            self.effect = "off"
            self._pixels = [(0, 0, 0)] * self.count
        self.show()

    # --- state / lifecycle ---------------------------------------------------
    def state(self) -> dict:
        with self._lock:
            return {
                "effect": self.effect,
                "brightness": self.brightness,
                "count": self.count,
                "pixels": [list(p) for p in self._pixels],
            }

    def close(self) -> None:
        try:
            self.off()
        finally:
            self.spi.close()
