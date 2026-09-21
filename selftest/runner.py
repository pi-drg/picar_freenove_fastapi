"""Step registry, prompts, safety gates and the CLI."""

from __future__ import annotations

import argparse
import getpass
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..config import BoardConfig, unit_config_path

SERVICE = "yakrobot-freenove"

# Motion probe: slow enough to READ the direction, not just see movement. The
# unloaded stall floor is around 700, so ~900 turns a raised wheel lazily. Both
# are overridable with --probe-duty / --probe-ms.
PROBE = {"duty": 900, "ms": 1500, "lead_ms": 1800}


# --- terminal helpers --------------------------------------------------------

class C:
    B = "\033[1m"; DIM = "\033[2m"; R = "\033[31m"; G = "\033[32m"
    Y = "\033[33m"; C_ = "\033[36m"; X = "\033[0m"

    @classmethod
    def off(cls) -> None:
        for k in ("B", "DIM", "R", "G", "Y", "C_", "X"):
            setattr(cls, k, "")


def title(text: str) -> None:
    print(f"\n{C.B}{C.C_}== {text} =={C.X}")


def say(text: str = "") -> None:
    print(text)


def note(text: str) -> None:
    print(f"{C.DIM}{text}{C.X}")


def ok(text: str) -> None:
    print(f"{C.G}  OK{C.X}  {text}")


def warn(text: str) -> None:
    print(f"{C.Y} WARN{C.X}  {text}")


def fail(text: str) -> None:
    print(f"{C.R} FAIL{C.X}  {text}")


def wrote(key: str, value) -> None:
    print(f"{C.C_} SAVE{C.X}  {key} = {value!r}")


def ask(prompt: str, options: list[str]) -> int:
    """Numbered menu. Returns the chosen index. Ctrl-C aborts the step."""
    say()
    for i, opt in enumerate(options, 1):
        say(f"  {C.B}{i}{C.X}) {opt}")
    while True:
        raw = input(f"{C.B}{prompt} [1-{len(options)}]: {C.X}").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        say(f"  {C.R}enter a number between 1 and {len(options)}{C.X}")


def ask_replay(prompt: str, options: list[str], replay) -> int:
    """Like ask(), plus a 'show me again' entry that re-runs `replay`.

    Nobody catches a 1.5 s wheel spin every time, and guessing corrupts the
    calibration silently. Repeating is free; a wrong answer is not.
    """
    again = "show me that again"
    while True:
        idx = ask(prompt, [*options, again])
        if idx < len(options):
            return idx
        replay()


def countdown(text: str = "watch the wheels", secs: int = 3) -> None:
    """Give the operator time to look up before something moves.

    The lead-in matters: the operator has just pressed Enter, so their eyes are
    on the keyboard, not the car. Counting down from the instant of the keypress
    means they miss the start of the motion — which is exactly the part that
    tells them the direction.
    """
    print(f"{C.DIM}  look up — {text}{C.X}", end="", flush=True)
    time.sleep(PROBE["lead_ms"] / 1000)
    for n in range(secs, 0, -1):
        print(f" {n}", end="", flush=True)
        time.sleep(1)
    print(f" {C.B}now{C.X}", flush=True)


def confirm(prompt: str) -> bool:
    while True:
        raw = input(f"{C.B}{prompt} [y/n]: {C.X}").strip().lower()
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False


def pause(prompt: str = "press Enter to continue") -> None:
    input(f"{C.DIM}{prompt}{C.X}")


# --- step registry -----------------------------------------------------------

@dataclass
class Step:
    name: str
    summary: str
    run: Callable
    moves: bool = False          # drives the motors — needs wheels raised
    on_floor: bool = False       # must run with the car on the ground
    writes: tuple = ()           # config fields it can calibrate


STEPS: dict[str, Step] = {}


def step(name: str, summary: str, *, moves: bool = False,
         on_floor: bool = False, writes: tuple = ()) -> Callable:
    def deco(fn: Callable) -> Callable:
        STEPS[name] = Step(name, summary, fn, moves, on_floor, writes)
        return fn
    return deco


# --- unit config persistence -------------------------------------------------

def load_unit(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        warn(f"could not read {path}: {exc} — starting fresh")
        return {}


def save_unit(path: Path, updates: dict) -> None:
    """Merge updates into the overlay. Never rewrites unrelated keys."""
    if not updates:
        return
    data = load_unit(path)
    data.update(updates)
    data["_calibrated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data["_comment"] = ("Per-unit calibration written by "
                        "`python -m picar_freenove_fastapi.selftest`. "
                        "Overrides picar_freenove_fastapi/config.py defaults.")
    body = json.dumps(data, indent=2, sort_keys=True) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    except PermissionError:
        tmp = Path("/tmp/unit.json")
        tmp.write_text(body)
        user = getpass.getuser()
        warn(f"no permission to write {path}")
        say("  Wrote it to a temporary file instead. Install it — the -o/-g keep")
        say("  it writable, so later steps save without this detour:")
        say(f"    {C.B}sudo install -D -m644 -o {user} -g {user} {tmp} {path}{C.X}")
        return
    say(f"\n{C.G}saved {path}{C.X}")


# --- safety ------------------------------------------------------------------

def service_active() -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", SERVICE],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def require_service_stopped() -> bool:
    """The server holds the I2C bus and the GPIO pins; two owners will clash."""
    if not service_active():
        return True
    fail(f"{SERVICE} is running and owns the hardware.")
    say(f"  Stop it first:  {C.B}sudo systemctl stop {SERVICE}{C.X}")
    say(f"  Afterwards:     {C.B}sudo systemctl start {SERVICE}{C.X}")
    return False


def safety_gate(st: Step) -> bool:
    if not (st.moves or st.on_floor):
        return True
    say()
    if st.on_floor:
        warn("This step DRIVES THE CAR ACROSS THE FLOOR.")
        say("  Clear about a metre in every direction and stay clear of it.")
        return confirm("Is the car on the floor with space around it?")
    warn("This step SPINS THE WHEELS.")
    say("  The car must be on a stand or block with all four wheels off the ground.")
    say(f"  Kill switch at any time: {C.B}Ctrl-C{C.X}")
    return confirm("Are all four wheels off the ground?")


# --- CLI ---------------------------------------------------------------------

def print_orientation_note() -> None:
    """Steps ask about forward/reverse, left/right and clockwise/counter-clockwise
    without re-explaining the frame each time. Say it once, up front, instead.
    """
    say(f"\n{C.B}Orientation, before you start:{C.X}")
    say("  FRONT of the car is the end with the camera / pan-tilt head. Every")
    say("  direction question below (forward/reverse, left/right, clockwise) is")
    say("  judged looking down at the car from above with that end pointing")
    say("  away from you. Face the camera away from you now, so it stays")
    say("  consistent for the rest of the run.")


def list_steps(path: Path) -> None:
    unit = load_unit(path)
    say(f"\n{C.B}Hardware self-test and calibration{C.X}")
    say(f"unit config: {path}{'' if path.exists() else '  (not created yet)'}")
    if unit:
        n = len([k for k in unit if not k.startswith('_')])
        say(f"{C.DIM}calibrated {unit.get('_calibrated_at', '?')} — {n} fields{C.X}")
    say(f"\n  {'STEP':<12}  {'SUMMARY':<48}MOVES")
    for st in STEPS.values():
        flag = (f"{C.R}floor{C.X}" if st.on_floor
                else f"{C.Y}wheels up{C.X}" if st.moves else "")
        say(f"  {C.B}{st.name:<12}{C.X}  {st.summary:<48}{flag}")
    say(f"\nrun one:  {C.B}python -m picar_freenove_fastapi.selftest <step>{C.X}")
    say(f"run all:  {C.B}python -m picar_freenove_fastapi.selftest --all{C.X}")
    say(f"\n{C.DIM}Order matters: bus and battery first, then motors before "
        f"mecanum.{C.X}")


def run_step(st: Step, cfg: BoardConfig, path: Path) -> bool:
    title(f"{st.name} — {st.summary}")
    if not safety_gate(st):
        warn("skipped")
        return False
    try:
        updates = st.run(cfg) or {}
    except KeyboardInterrupt:
        say()
        warn("interrupted — stopping hardware")
        _panic_stop(cfg)
        raise
    except Exception as exc:                       # noqa: BLE001 - operator tool
        fail(f"{type(exc).__name__}: {exc}")
        _panic_stop(cfg)
        return False
    save_unit(path, updates)
    return True


def _panic_stop(cfg: BoardConfig) -> None:
    """Best-effort: cut the motors whatever went wrong."""
    try:
        from ..drivers import PCA9685
        PCA9685(cfg.pca9685_address, busnum=cfg.i2c_busnum).set_all_duties([0] * 8)
    except Exception:                              # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m picar_freenove_fastapi.selftest",
        description="Interactive hardware self-test and calibration.")
    ap.add_argument("steps", nargs="*", help="steps to run (default: list them)")
    ap.add_argument("--all", action="store_true", help="run every step in order")
    ap.add_argument("--show", action="store_true", help="print the unit config")
    ap.add_argument("--config", metavar="PATH", help="unit config path override")
    ap.add_argument("--probe-duty", type=int, metavar="N",
                    help=f"motor duty used by probing steps "
                         f"(default {PROBE['duty']}; raise it if a wheel stalls)")
    ap.add_argument("--probe-ms", type=int, metavar="MS",
                    help=f"how long each probe burst runs "
                         f"(default {PROBE['ms']})")
    ap.add_argument("--lead-ms", type=int, metavar="MS",
                    help=f"pause after your keypress before the countdown "
                         f"starts (default {PROBE['lead_ms']})")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args(argv)

    if args.no_color or not sys.stdout.isatty():
        C.off()

    if args.probe_duty:
        PROBE["duty"] = max(300, min(4095, args.probe_duty))
    if args.probe_ms:
        PROBE["ms"] = max(200, min(5000, args.probe_ms))
    if args.lead_ms is not None:
        PROBE["lead_ms"] = max(0, min(10000, args.lead_ms))

    path = Path(args.config) if args.config else unit_config_path()

    if args.show:
        unit = load_unit(path)
        say(json.dumps(unit, indent=2, sort_keys=True) if unit
            else f"no unit config at {path} — using packaged defaults")
        return 0

    if not args.steps and not args.all:
        list_steps(path)
        return 0

    names = list(STEPS) if args.all else args.steps
    unknown = [n for n in names if n not in STEPS]
    if unknown:
        fail(f"unknown step(s): {', '.join(unknown)}")
        say(f"known: {', '.join(STEPS)}")
        return 2

    if not require_service_stopped():
        return 1

    print_orientation_note()

    cfg = BoardConfig.load(path)
    done = 0
    try:
        for name in names:
            done += bool(run_step(STEPS[name], cfg, path))
            cfg = BoardConfig.load(path)     # pick up what this step just wrote
    except KeyboardInterrupt:
        say()
        return 130

    say(f"\n{C.B}{done}/{len(names)} step(s) completed.{C.X}")
    if service_active() is False:
        note(f"Remember: sudo systemctl start {SERVICE}")
    return 0
