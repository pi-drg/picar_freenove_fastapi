"""Robot-side HTTP server for the Freenove 4WD car.

This is OUR server — it depends only on this package's drivers, not on any
Freenove module. It exposes the endpoint surface the gateway's Freenove4wdAdapter
dials (/info, /drive, /mecanum, /look, /scan, /distance, /line, /battery,
/snapshot), plus two WebSocket endpoints for realtime teleop — /ws/control and
/ws/video (protocol: AGENTS.md §9). Run it on the car's Pi:

    ROBOT_TOKEN=$(cat /etc/yakrobot/token) uvicorn picar_freenove_fastapi.server:app \
        --host 127.0.0.1 --port 8080

Bind to loopback and let the gateway (same Pi, or over its authenticated tunnel)
be the only client. If ROBOT_TOKEN is set, every request must carry
`Authorization: Bearer <token>` — a minimal on-robot guard for the LAN case,
independent of the gateway's own MCP auth. The gateway is still the real trust
boundary; this is defence in depth, not a replacement.

All hardware calls run in a thread pool so blocking I2C/GPIO/servo waits never
stall the async event loop.
"""

import asyncio
import logging
import os

from fastapi import (
    Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect,
)
from fastapi.responses import Response
from pydantic import BaseModel

from .config import BoardConfig
from .robot import Robot


def _configure_logging() -> None:
    """Send this package's MOTOR/SERVO lines to stdout, and so to the journal.

    Done here rather than with basicConfig: uvicorn owns the root logger's
    configuration, and adding a root handler double-prints its output. An app
    logger with no handler of its own is silently dropped at INFO, because
    logging's last-resort handler only passes WARNING and above — which is why
    these lines need an explicit handler to exist at all.
    """
    log = logging.getLogger("picar_freenove_fastapi")
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False


_configure_logging()
_log = logging.getLogger(__name__)

app = FastAPI(title="YakRobot — Freenove 4WD")

_robot: Robot | None = None
_ROBOT_TOKEN = os.getenv("ROBOT_TOKEN")  # optional shared bearer token


def get_robot() -> Robot:
    global _robot
    if _robot is None:
        _robot = Robot(BoardConfig.load())   # defaults + this car's calibration
    return _robot


async def auth(authorization: str | None = Header(default=None)) -> None:
    if _ROBOT_TOKEN is None:
        return  # auth disabled (loopback-only deployments)
    if authorization != f"Bearer {_ROBOT_TOKEN}":
        raise HTTPException(status_code=401, detail="bad or missing bearer token")


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _robot is not None:
        await asyncio.to_thread(_robot.close)


class DriveReq(BaseModel):
    direction: str
    duty: int = 1200
    duration_ms: int = 800


class MecanumReq(BaseModel):
    vx: int = 0
    vy: int = 0
    omega: int = 0
    duration_ms: int = 800


class LookReq(BaseModel):
    pan: int = 90
    tilt: int = 90


class LedReq(BaseModel):
    """`effect` is one of solid | off | blink | rainbow | breathing | chase.

    r/g/b apply to `solid` directly; for `breathing` and `chase` they override the
    cycling rainbow colour when non-zero. `index` targets a single LED (solid
    only); omitted means the whole strip. `duration_ms` bounds an animation, after
    which the strip goes dark — omit it to run until the next /led call.

    `reverse` flips travel direction for `chase` and `rainbow`. Which way that is
    depends on how the strip is fitted (see config.led_index_clockwise); by
    default ascending pixel index runs clockwise, so reverse=true is
    counter-clockwise.
    """
    effect: str = "solid"
    r: int = 0
    g: int = 0
    b: int = 0
    brightness: int | None = None
    duration_ms: int | None = None
    wait_ms: int | None = None
    index: int | None = None
    reverse: bool = False


@app.get("/info", dependencies=[Depends(auth)])
async def info():
    r = get_robot()
    volts = await asyncio.to_thread(r.battery)
    # `wheels` and `holonomic` are ADDITIONS to the adapter contract, not changes
    # — existing clients read the three keys below and ignore the rest. They let
    # a client discover whether strafing is possible before commanding it.
    return {
        "robot": "freenove-4wd",
        "hardware": "ok",
        "battery_v": volts,
        "wheels": r.cfg.wheel_type,
        "holonomic": r.cfg.holonomic,
    }


@app.post("/drive", dependencies=[Depends(auth)])
async def drive(req: DriveReq):
    r = get_robot()
    return await asyncio.to_thread(r.drive, req.direction, req.duty, req.duration_ms)


@app.post("/mecanum", dependencies=[Depends(auth)])
async def mecanum(req: MecanumReq):
    r = get_robot()
    try:
        return await asyncio.to_thread(
            r.mecanum, req.vx, req.vy, req.omega, req.duration_ms)
    except ValueError as exc:  # vy commanded on a car with ordinary wheels
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/look", dependencies=[Depends(auth)])
async def look(req: LookReq):
    r = get_robot()
    result = await asyncio.to_thread(r.look, req.pan, req.tilt)
    return {"status": "ok", **result}


@app.get("/distance", dependencies=[Depends(auth)])
async def distance():
    r = get_robot()
    cm = await asyncio.to_thread(r.distance)
    return {"cm": cm}


@app.get("/scan", dependencies=[Depends(auth)])
async def scan():
    r = get_robot()
    return await asyncio.to_thread(r.scan)


@app.get("/line", dependencies=[Depends(auth)])
async def line():
    r = get_robot()
    return await asyncio.to_thread(r.line_state)


@app.get("/battery", dependencies=[Depends(auth)])
async def battery():
    r = get_robot()
    volts = await asyncio.to_thread(r.battery)
    return {"volts": volts}


@app.post("/led", dependencies=[Depends(auth)])
async def led(req: LedReq):
    r = get_robot()
    try:
        return await asyncio.to_thread(
            r.led, req.effect, req.r, req.g, req.b,
            req.brightness, req.duration_ms, req.wait_ms, req.index, req.reverse,
        )
    except ValueError as exc:  # unknown effect or out-of-range index
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/led", dependencies=[Depends(auth)])
async def led_state():
    r = get_robot()
    return await asyncio.to_thread(r.led_state)


@app.get("/snapshot", dependencies=[Depends(auth)])
async def snapshot():
    """One JPEG frame, as raw bytes — not JSON, not base64.

    `curl -o out.jpg .../snapshot`, or open the URL directly in a browser: a
    plain HTTP client needs nothing beyond a GET. Callers that need the frame
    embedded in a structured result (the gateway's MCP tool) base64-encode it
    themselves at that layer, where it is actually needed.
    """
    r = get_robot()
    jpeg = await asyncio.to_thread(r.snapshot_jpeg)
    return Response(content=jpeg, media_type="image/jpeg")


# --- realtime control (WebSocket) --------------------------------------------
#
# Two sockets, deliberately separate: a stalled video stream must never be able
# to delay a stop command. Over a transatlantic link the video socket *will*
# stall during congestion while the tiny control frames still get through.
#
# The control protocol is absolute state, not events: the client sends the
# velocity it wants, repeatedly, and every message is idempotent. A lost,
# duplicated or reordered frame therefore costs nothing, which is what lets
# this survive jitter with no reliability layer bolted on top.

_controller: WebSocket | None = None


async def ws_auth(ws: WebSocket) -> bool:
    """Bearer-token check for WebSockets. True when the socket may proceed.

    Browsers cannot set headers on a WebSocket handshake, so the token arrives
    as `?token=...` instead. Semantics otherwise mirror `auth` exactly,
    including the gotcha: ROBOT_TOKEN unset disables the check, but
    ROBOT_TOKEN= (empty) does NOT — os.getenv returns "", not None, and an
    empty token then has to be presented explicitly.
    """
    if _ROBOT_TOKEN is None:
        return True
    if ws.query_params.get("token") == _ROBOT_TOKEN:
        return True
    await ws.close(code=1008, reason="bad or missing token")
    return False


def _clamp_duty(value, cap: int) -> int:
    try:
        return max(-cap, min(cap, int(value)))
    except (TypeError, ValueError):
        return 0


async def _telemetry_loop(ws: WebSocket, r: Robot) -> None:
    """Push battery + forward range while the socket is open.

    Distance goes over the same I2C lock as the drive writes, which serialises
    them — at one reading per few seconds that is far below the bus budget.
    """
    while True:
        await asyncio.sleep(r.cfg.control_telemetry_s)
        volts = await asyncio.to_thread(r.battery)
        cm = await asyncio.to_thread(r.distance)
        await ws.send_json({"type": "telemetry", "battery_v": volts, "distance_cm": cm})


@app.websocket("/ws/control")
async def ws_control(ws: WebSocket) -> None:
    """Realtime driving. Protocol reference: AGENTS.md §9."""
    global _controller

    if not await ws_auth(ws):
        return
    await ws.accept()

    r = get_robot()
    cfg = r.cfg

    # One driver at a time — non-negotiable when drivers may be on different
    # continents. A second connection is accepted but view-only: it still gets
    # telemetry, which is exactly what a spotter next to the car wants.
    is_controller = _controller is None
    if is_controller:
        _controller = ws
    else:
        await ws.send_json({"type": "error", "detail": "busy"})

    volts = await asyncio.to_thread(r.battery)
    await ws.send_json({
        "type": "hello",
        "wheels": cfg.wheel_type,
        "holonomic": cfg.holonomic,
        "battery_v": volts,
        "deadman_ms": cfg.control_deadman_ms,
        "max_duty": cfg.control_max_duty,
        "controller": is_controller,
    })

    telemetry = asyncio.create_task(_telemetry_loop(ws, r))
    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")

            if kind == "ping":
                await ws.send_json({"type": "pong", "t": msg.get("t")})
                continue

            if not is_controller:
                if kind in ("drive", "look", "stop"):
                    await ws.send_json({"type": "error", "detail": "busy"})
                continue

            if kind == "drive":
                cap = cfg.control_max_duty
                try:
                    # duration_ms IS the deadman: the existing auto-stop timer
                    # is re-armed by every heartbeat, so silence stops the car
                    # with no separate watchdog to get wrong.
                    await asyncio.to_thread(
                        r.mecanum,
                        _clamp_duty(msg.get("vx"), cap),
                        _clamp_duty(msg.get("vy"), cap),
                        _clamp_duty(msg.get("omega"), cap),
                        cfg.control_deadman_ms,
                    )
                except ValueError as exc:      # vy on an ordinary-wheel car
                    await ws.send_json({"type": "error", "detail": str(exc)})

            elif kind == "stop":
                await asyncio.to_thread(r.stop)

            elif kind == "look":
                result = await asyncio.to_thread(
                    r.look, int(msg.get("pan", 90)), int(msg.get("tilt", 90)))
                # Echo the CLAMPED angles: the UI must track where the servo
                # actually went, not where it was asked to go.
                await ws.send_json({"type": "look", **result})

            else:
                await ws.send_json(
                    {"type": "error", "detail": f"unknown message type {kind!r}"})

    except (WebSocketDisconnect, RuntimeError, ValueError, TypeError, KeyError):
        # ValueError/TypeError/KeyError cover malformed JSON and bad payloads —
        # a client that sends nonsense loses its socket, and the deadman (plus
        # the explicit stop below) means the car does not keep driving.
        pass
    finally:
        telemetry.cancel()
        if is_controller:
            await asyncio.to_thread(r.stop)
            _controller = None


@app.websocket("/ws/video")
async def ws_video(ws: WebSocket) -> None:
    """Latest-frame JPEG stream.

    `grab_jpeg` blocks until the *next* encoded frame, so every client always
    receives the newest one and a slow link simply samples fewer frames. There
    is no queue, so latency cannot compound the way it does with a buffered
    stream — the frame you see is the freshest one that fit down the pipe.
    """
    if not await ws_auth(ws):
        return
    await ws.accept()

    r = get_robot()
    # Two lines per stream, never one per frame: at ~10 fps, per-frame logging
    # would bury every MOTOR and SERVO line in the journal.
    _log.info("CAMERA stream open")

    min_gap = 1.0 / r.cfg.camera_stream_fps if r.cfg.camera_stream_fps else 0.0
    loop = asyncio.get_event_loop()
    last_sent = 0.0
    try:
        while True:
            frame = await asyncio.to_thread(r.camera.grab_jpeg)
            # Drop frames that arrive inside the cap rather than sleeping on
            # them. Sleeping would send a stale frame late; dropping means the
            # next one sent is always the newest the camera has produced.
            now = loop.time()
            if now - last_sent < min_gap:
                continue
            last_sent = now
            await ws.send_bytes(frame)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except TimeoutError:
        # Camera stalled rather than the client leaving — say so instead of
        # dropping silently, so the operator knows which half is broken.
        _log.warning("CAMERA stream stalled — no frame within timeout")
        await ws.close(code=1011, reason="no camera frame")
    finally:
        _log.info("CAMERA stream closed")
