"""Robot-side HTTP server for the Freenove 4WD car.

This is OUR server — it depends only on this package's drivers, not on any
Freenove module. It exposes the endpoint surface the gateway's Freenove4wdAdapter
dials (/info, /drive, /mecanum, /look, /scan, /distance, /line, /battery,
/snapshot). Run it on the car's Pi:

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
import base64
import os

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from .config import BoardConfig
from .robot import Robot

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

    `reverse` flips travel direction for `chase` and `rainbow`. On this car
    ascending pixel index runs clockwise, so reverse=true is counter-clockwise.
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
    return {"robot": "freenove-4wd", "hardware": "ok", "battery_v": volts}


@app.post("/drive", dependencies=[Depends(auth)])
async def drive(req: DriveReq):
    r = get_robot()
    return await asyncio.to_thread(r.drive, req.direction, req.duty, req.duration_ms)


@app.post("/mecanum", dependencies=[Depends(auth)])
async def mecanum(req: MecanumReq):
    r = get_robot()
    return await asyncio.to_thread(r.mecanum, req.vx, req.vy, req.omega, req.duration_ms)


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
    r = get_robot()
    jpeg = await asyncio.to_thread(r.snapshot_jpeg)
    b64 = base64.b64encode(jpeg).decode("ascii")
    return {"image": f"data:image/jpeg;base64,{b64}", "bytes": len(jpeg)}
