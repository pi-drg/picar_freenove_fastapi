"""Entry point: `python -m picar_freenove_fastapi.selftest`."""

import sys

from . import steps  # noqa: F401 - importing registers every @step
from .runner import main

if __name__ == "__main__":
    sys.exit(main())
