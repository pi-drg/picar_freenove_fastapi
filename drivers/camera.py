"""Camera via picamera2 (MJPEG streaming + single-frame grab).

picamera2 is the correct, maintained camera stack on Raspberry Pi OS, so this is
a thin wrapper rather than a reimplementation. Provides a lazily-started MJPEG
stream and a single-frame JPEG grab for VLM/perception hand-off.
"""

import io
from threading import Condition

from picamera2 import Picamera2
from picamera2.encoders import JpegEncoder
from picamera2.outputs import FileOutput

from ..config import BoardConfig


class _FrameSink(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.cond = Condition()

    def write(self, buf: bytes) -> int:
        with self.cond:
            self.frame = buf
            self.cond.notify_all()
        return len(buf)


class Camera:
    def __init__(self, cfg: BoardConfig):
        self.cam = Picamera2()
        self.cfg = cfg
        self.sink = _FrameSink()
        self.streaming = False
        self._config = self.cam.create_video_configuration(
            main={"size": cfg.camera_stream_size}
        )

    def start(self) -> None:
        if not self.streaming:
            if self.cam.started:
                self.cam.stop()
            self.cam.configure(self._config)
            self.cam.start_recording(JpegEncoder(), FileOutput(self.sink))
            self.streaming = True

    def stop(self) -> None:
        if self.streaming:
            self.cam.stop_recording()
            self.streaming = False

    def grab_jpeg(self, timeout: float = 2.0) -> bytes:
        """Return one JPEG frame (bytes). Starts the stream if needed."""
        self.start()
        with self.sink.cond:
            if not self.sink.cond.wait(timeout):
                raise TimeoutError("No camera frame within timeout")
            return self.sink.frame

    def close(self) -> None:
        self.stop()
        self.cam.close()
