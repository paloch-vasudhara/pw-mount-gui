"""Command protocol: strict schemas for client -> gateway `cmd` messages (T3.1a).

Wire format (existing authenticated WebSocket):
  client  {"type":"cmd","id":"<opaque, 1-64 chars>","cmd":"<name>","args":{...}}
  gateway {"type":"ack","id":..., "cmd":..., "ok":bool, "reason":str|null, "control":{...}}

Anything that does not validate is refused with a reason (G7); nothing malformed reaches the
command worker or PWI4.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

MAX_MESSAGE_BYTES = 2048  # a valid command is < 200 bytes
MAX_ID_LEN = 64

Num = Annotated[float, Field(allow_inf_nan=False)]


class _Args(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class NoArgs(_Args):
    pass


class GotoAltAz(_Args):
    alt: Annotated[Num, Field(ge=0.0, le=90.0)]  # horizon floor; Lim EL is T3.1b
    az: Annotated[Num, Field(ge=0.0, lt=360.0)]


class GotoRaDecJ2000(_Args):
    ra_h: Annotated[Num, Field(ge=0.0, lt=24.0)]
    dec_deg: Annotated[Num, Field(ge=-90.0, le=90.0)]


class Tracking(_Args):
    on: bool


class Disconnect(_Args):
    # Second send of a blocked disconnect: the user confirmed "disconnect without homing".
    skip_home: bool = False


JOG_SPEEDS = (0.5, 1.0, 2.0, 5.0, 10.0)  # deg/s of the mount axis (Q9)
STEP_SIZES_ARCMIN = (1, 5, 10, 30, 60)


class Jog(_Args):
    """Start / keep-alive / switch of a jog (T3.4c): the client re-sends it every 100 ms."""

    axis: Annotated[int, Field(ge=0, le=1)]
    dir: Annotated[int, Field(ge=-1, le=1)]
    speed: Num

    @field_validator("dir")
    @classmethod
    def _dir(cls, v: int) -> int:
        if v == 0:
            raise ValueError("dir")
        return v

    @field_validator("speed")
    @classmethod
    def _speed(cls, v: float) -> float:
        for s in JOG_SPEEDS:
            if abs(v - s) < 1e-9:
                return s
        raise ValueError("speed")


class Step(_Args):
    axis: Annotated[int, Field(ge=0, le=1)]
    dir: Annotated[int, Field(ge=-1, le=1)]
    size_arcmin: Annotated[int, Field(ge=1, le=60)]

    @field_validator("dir")
    @classmethod
    def _dir(cls, v: int) -> int:
        if v == 0:
            raise ValueError("dir")
        return v

    @field_validator("size_arcmin")
    @classmethod
    def _size(cls, v: int) -> int:
        if v not in STEP_SIZES_ARCMIN:
            raise ValueError("size_arcmin")
        return v


class TrackObject(_Args):
    norad: Annotated[int, Field(gt=0)]  # NORAD catalogue id; the gateway fetches the TLE (Q13)


ARG_MODELS: dict[str, type[_Args]] = {
    "take_control": NoArgs,
    "release_control": NoArgs,
    "stop": NoArgs,
    "goto_altaz": GotoAltAz,
    "goto_radec_j2000": GotoRaDecJ2000,
    "tracking": Tracking,
    "connect": NoArgs,
    "disconnect": Disconnect,
    "home": NoArgs,
    "homed_override": NoArgs,  # T3.3: proceed without homing (only while not homed)
    "jog": Jog,
    "jog_stop": NoArgs,
    "step": Step,
    "wrap_turn": NoArgs,  # T3.4e: 360 deg reposition through the other axis-0 representation
    "wrap_turn_cancel": NoArgs,  # T3.4e: "stay": clears the azimuth-limit prompt
    "track_object": TrackObject,  # T4.1: check only; the real command arrives with T4.2
}
# Commands that move or start the mount (need the lock, ready gateway, a connected mount).
MOTION_COMMANDS = frozenset(
    {"goto_altaz", "goto_radec_j2000", "tracking", "jog", "step", "wrap_turn", "track_object"}
)
# Long-running sequences (T3.2): acked when started, progress is in the snapshot's `sequence`.
SEQUENCE_COMMANDS = frozenset({"connect", "disconnect", "home"})
CmdName = Literal[
    "take_control",
    "release_control",
    "stop",
    "goto_altaz",
    "goto_radec_j2000",
    "tracking",
    "connect",
    "disconnect",
    "home",
    "homed_override",
    "jog",
    "jog_stop",
    "step",
    "wrap_turn",
    "wrap_turn_cancel",
    "track_object",
]


class Envelope(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    type: Literal["cmd"]
    id: Annotated[str, Field(min_length=1, max_length=MAX_ID_LEN)]
    cmd: str
    args: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class Command:
    id: str
    name: str
    args: dict[str, Any]


class CommandError(Exception):
    """Refusal with a user-facing reason; `cmd_id` is set when the id could be read."""

    def __init__(self, reason: str, cmd_id: str | None = None, cmd: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.cmd_id = cmd_id
        self.cmd = cmd


def _salvage_id(obj: Any) -> str | None:
    cid = obj.get("id") if isinstance(obj, dict) else None
    return cid if isinstance(cid, str) and 0 < len(cid) <= MAX_ID_LEN else None


def parse_command(raw: str | bytes) -> Command:
    """Validate one inbound text frame. Raises CommandError (never anything else)."""
    if len(raw) > MAX_MESSAGE_BYTES:
        raise CommandError("Command too large")
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        raise CommandError("Invalid command: not JSON") from None
    cid = _salvage_id(obj)
    if not isinstance(obj, dict):
        raise CommandError("Invalid command: not an object")
    try:
        env = Envelope.model_validate(obj)
    except ValidationError:
        raise CommandError("Invalid command: bad envelope", cid) from None
    return validate_command(env.id, env.cmd, env.args)


def validate_command(cmd_id: str, name: str, raw_args: Any) -> Command:
    """Validate a command name + args with the strict per-command schema (shared by the
    WebSocket parser and the REST dry-run check). Raises CommandError."""
    model = ARG_MODELS.get(name)
    if model is None:
        raise CommandError("Unknown command", cmd_id)
    try:
        args = model.model_validate(raw_args).model_dump()
    except ValidationError as exc:
        fields = ", ".join(sorted({str(e["loc"][0]) if e["loc"] else "args" for e in exc.errors()}))
        raise CommandError(f"Invalid arguments ({fields})", cmd_id, name) from None
    return Command(cmd_id, name, args)
