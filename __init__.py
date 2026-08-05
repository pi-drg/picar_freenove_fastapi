"""picar_freenove_fastapi — self-contained robot-side control for the Freenove 4WD car."""

from .config import BoardConfig
from .robot import Robot

__all__ = ["BoardConfig", "Robot"]
