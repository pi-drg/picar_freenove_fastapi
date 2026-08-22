"""Robot facade — composes the drivers behind one thread-safe object.

Owns a single lock (all hardware shares one I2C bus + PCA9685, so concurrent
writes must serialise) and the auto-stop timer that enforces open-loop motion
safety: every drive command schedules a hard stop, so a dropped connection or a
crashed caller cannot leave the motors running.

Camera driver is created lazily (picamera2 init is slow and not every task needs
it). Sensors and PWM are initialised eagerly at construction.
"""

import logging
import threading

from .config import BoardConfig
from .drivers import PCA9685, Motors, Servos, ADC, Ultrasonic, LineArray, Camera, Leds

logger = logging.getLogger(__name__)


class Robot:
    def __init__(self, cfg: BoardConfig | None = None):
        self.cfg = cfg or BoardConfig()
        self._lock = threading.RLock()
        self._stop_timer: threading.Timer | None = None

        self.pca = PCA9685(self.cfg.pca9685_address,
                           busnum=self.cfg.i2c_busnum,
                           expected_hz=self.cfg.i2c_expected_hz)
        self.pca.set_pwm_freq(self.cfg.pwm_freq_hz)
        self.motors = Motors(self.pca, self.cfg)
        self.servos = Servos(self.pca, self.cfg)
        self.adc = ADC(self.cfg)
        self.ultrasonic = Ultrasonic(self.cfg)
        self.lines = LineArray(self.cfg)
        self._camera: Camera | None = None
        self._leds: Leds | None = None

    # --- camera (lazy) -------------------------------------------------------
    @property
    def camera(self) -> Camera:
        if self._camera is None:
            self._camera = Camera(self.cfg)
        return self._camera

    # --- LEDs (lazy) ---------------------------------------------------------
    # Lazy like the camera, so a Pi with SPI disabled still serves every other
    # endpoint instead of failing at construction.
    @property
    def leds(self) -> Leds:
        if self._leds is None:
            self._leds = Leds(self.cfg)
        return self._leds

    # --- motion (open-loop, always auto-stopped) -----------------------------
    def _arm_auto_stop(self, duration_ms: int) -> None:
        if self._stop_timer is not None:
            self._stop_timer.cancel()
        self._stop_timer = threading.Timer(duration_ms / 1000.0, self.stop)
        self._stop_timer.daemon = True
        self._stop_timer.start()

    def drive(self, direction: str, duty: int = 1200, duration_ms: int = 800) -> dict:
        with self._lock:
            mix = self.motors.drive(direction, duty)
        if direction != "stop":
            self._arm_auto_stop(duration_ms)
        logger.info("MOTOR drive=%s duty=%d lf=%d lr=%d rf=%d rr=%d deadman=%dms",
                    direction, duty, *mix, duration_ms)
        return {"direction": direction, "duties": mix}

    def mecanum(self, vx: int, vy: int, omega: int, duration_ms: int = 800) -> dict:
        with self._lock:
            mix = self.motors.mecanum(vx, vy, omega)
        self._arm_auto_stop(duration_ms)
        logger.info("MOTOR vx=%d vy=%d omega=%d lf=%d lr=%d rf=%d rr=%d deadman=%dms",
                    vx, vy, omega, *mix, duration_ms)
        return {"duties": mix}

    def stop(self) -> dict:
        with self._lock:
            self.motors.stop()
        # `by` distinguishes a commanded stop from the auto-stop timer firing,
        # which is the only way to see the deadman working in the journal.
        by = "deadman" if threading.current_thread() is self._stop_timer else "command"
        logger.info("MOTOR stop by=%s lf=0 lr=0 rf=0 rr=0", by)
        return {"direction": "stop"}

    # --- camera / servos -----------------------------------------------------
    def look(self, pan: int = 90, tilt: int = 90) -> dict:
        with self._lock:
            result = self.servos.look(pan, tilt)
        # Log the CLAMPED angles the servos actually took, not what was asked.
        logger.info("SERVO pan=%s tilt=%s (requested pan=%d tilt=%d)",
                    result.get("pan"), result.get("tilt"), pan, tilt)
        return result

    def snapshot_jpeg(self) -> bytes:
        # Camera has its own internal sync; no need to hold the I2C lock.
        jpeg = self.camera.grab_jpeg()
        # Only deliberate stills reach here. /ws/video pulls frames straight
        # from the camera, so a 10 fps stream cannot flood the journal.
        logger.info("CAMERA snapshot bytes=%d", len(jpeg))
        return jpeg

    # --- LEDs ----------------------------------------------------------------
    # The strip is on SPI with its own lock, so none of these take the I2C lock —
    # lighting can animate while the car drives.
    def led(self, effect: str = "solid", r: int = 0, g: int = 0, b: int = 0,
            brightness: int | None = None, duration_ms: int | None = None,
            wait_ms: int | None = None, index: int | None = None,
            reverse: bool = False) -> dict:
        leds = self.leds
        if brightness is not None:
            leds.set_brightness(brightness)
        if effect == "off":
            leds.off()
        elif effect == "solid":
            leds.stop_effect()
            leds.effect = "solid"
            if index is None:
                leds.set_all(r, g, b)
            else:
                leds.set_pixel(index, r, g, b)
        else:
            color = (r, g, b) if (r or g or b) else None
            leds.start_effect(effect, duration_ms=duration_ms, wait_ms=wait_ms,
                              color=color, reverse=reverse)
        state = leds.state()
        logger.info("LED effect=%s rgb=(%d,%d,%d) index=%s brightness=%s reverse=%s",
                    effect, r, g, b, index, brightness, reverse)
        return state

    def led_state(self) -> dict:
        return self.leds.state()

    # --- sensing -------------------------------------------------------------
    # Deliberately unlogged. /ws/control telemetry polls distance and battery
    # on a timer, so logging reads would bury the MOTOR and SERVO lines the
    # journal is actually read for. Query the endpoints when you want a value.
    def distance(self):
        with self._lock:
            return self.ultrasonic.distance_cm()

    def scan(self, angles=range(30, 151, 30), settle_s: float = 0.15) -> dict:
        """Sweep the pan servo, sample ultrasonic at each angle, recentre."""
        import time
        samples = {}
        for a in angles:
            with self._lock:
                self.servos.set_angle("pan", a)
            time.sleep(settle_s)
            with self._lock:
                samples[str(a)] = self.ultrasonic.distance_cm()
        with self._lock:
            self.servos.set_angle("pan", 90)
        # INFO because a scan moves the pan servo — it is actuation, not just
        # a reading, and it leaves the camera recentred.
        logger.info("SERVO scan angles=%s samples=%s recentred=90",
                    list(angles), samples)
        return {"samples": samples, "unit": "cm"}

    def line_state(self) -> dict:
        with self._lock:
            return self.lines.read()

    def battery(self) -> float:
        with self._lock:
            return self.adc.battery_voltage()

    # --- lifecycle -----------------------------------------------------------
    def close(self) -> None:
        logger.info("ROBOT closing — stopping motors and releasing hardware")
        try:
            self.stop()
        finally:
            if self._stop_timer:
                self._stop_timer.cancel()
            self.ultrasonic.close()
            self.lines.close()
            self.adc.close()
            if self._camera:
                self._camera.close()
            if self._leds:
                self._leds.close()
            self.pca.close()
