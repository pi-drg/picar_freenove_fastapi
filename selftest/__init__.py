"""Interactive hardware self-test and calibration for one car.

Each step exercises one subsystem, asks you what you saw, and writes what it
learned to the per-unit config overlay (see config.BoardConfig.load). Run the
steps one at a time during assembly, or `--all` end to end on a new car.

    python -m picar_freenove_fastapi.selftest            # list steps
    python -m picar_freenove_fastapi.selftest bus        # run one
    python -m picar_freenove_fastapi.selftest --all

The package defaults describe ONE calibrated car. Your kit may have the motors
in different ports, the LED strip fitted the other way round, or a different PCB
revision — that is what this tool is for.
"""

from .runner import STEPS, main

__all__ = ["STEPS", "main"]
