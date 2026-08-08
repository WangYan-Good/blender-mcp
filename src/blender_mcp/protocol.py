"""Length-prefixed JSON protocol shared by the MCP-side connection."""

from __future__ import annotations

import json
import struct
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_REQUEST_ID_BYTES = 128
HEADER = struct.Struct("!I")


class SocketLike(Protocol):
    """The receive operation required by the frame decoder."""

    def recv(self, size: int) -> bytes: ...


class ProtocolError(RuntimeError):
    """The peer sent an invalid or incomplete protocol message."""


class FrameTooLargeError(ProtocolError):
    """A frame exceeded the configured transport limit."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _receive_exact(sock: SocketLike, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ProtocolError(
                "connection closed before a complete frame was received"
            )
        data.extend(chunk)
    return bytes(data)


def encode_frame(
    message: Mapping[str, Any], max_frame_bytes: int = MAX_FRAME_BYTES
) -> bytes:
    """Encode one JSON object with a four-byte network-order length prefix."""
    try:
        payload = json.dumps(
            dict(message),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"message is not JSON serializable: {exc}") from exc
    if len(payload) > max_frame_bytes:
        raise FrameTooLargeError(
            f"frame is {len(payload)} bytes; maximum is {max_frame_bytes}"
        )
    return HEADER.pack(len(payload)) + payload


def receive_frame(
    sock: SocketLike, max_frame_bytes: int = MAX_FRAME_BYTES
) -> dict[str, Any]:
    """Read and decode exactly one length-prefixed JSON object."""
    declared_size = HEADER.unpack(_receive_exact(sock, HEADER.size))[0]
    if declared_size > max_frame_bytes:
        raise FrameTooLargeError(
            f"declared frame is {declared_size} bytes; maximum is {max_frame_bytes}"
        )
    try:
        message = json.loads(
            _receive_exact(sock, declared_size).decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProtocolError(f"invalid frame payload: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("frame payload must be a JSON object")
    return message


def make_request(
    command_type: str,
    params: Mapping[str, Any] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build a versioned request envelope."""
    resolved_request_id = str(uuid.uuid4()) if request_id is None else request_id
    if (
        not isinstance(resolved_request_id, str)
        or not resolved_request_id
        or len(resolved_request_id.encode("utf-8")) > MAX_REQUEST_ID_BYTES
    ):
        raise ProtocolError(
            f"request_id must be 1 to {MAX_REQUEST_ID_BYTES} UTF-8 bytes"
        )
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": resolved_request_id,
        "type": command_type,
        "params": dict(params or {}),
    }


def validate_response(
    response: Mapping[str, Any], expected_request_id: str
) -> dict[str, Any]:
    """Validate response identity and the fields required by the connection."""
    if response.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol version in response")
    if response.get("request_id") != expected_request_id:
        raise ProtocolError("response request_id does not match request")
    if not isinstance(response.get("success"), bool):
        raise ProtocolError("response success must be boolean")
    if response["success"]:
        if not isinstance(response.get("result"), dict):
            raise ProtocolError("successful response result must be an object")
        return dict(response)

    error = response.get("error")
    if not isinstance(error, dict):
        raise ProtocolError("failed response is missing error details")
    for field in ("category", "type", "message"):
        if not isinstance(error.get(field), str) or not error[field]:
            raise ProtocolError(f"error {field} must be a non-empty string")
    if error.get("traceback") is not None and not isinstance(
        error["traceback"], str
    ):
        raise ProtocolError("error traceback must be a string or null")
    if type(error.get("retryable")) is not bool:
        raise ProtocolError("error retryable must be boolean")
    if not isinstance(error.get("details"), dict):
        raise ProtocolError("error details must be an object")
    return dict(response)
