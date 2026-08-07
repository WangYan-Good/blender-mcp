"""Serialized TCP connection from the MCP server to the Blender add-on."""

from __future__ import annotations

import logging
import socket
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .protocol import encode_frame, make_request, receive_frame, validate_response

logger = logging.getLogger("BlenderMCPConnection")


class BlenderCommandError(RuntimeError):
    """A structured command failure returned by Blender."""

    def __init__(
        self,
        category: str,
        error_type: str,
        message: str,
        traceback_text: str | None,
        retryable: bool,
        request_id: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.error_type = error_type
        self.message = message
        self.traceback_text = traceback_text
        self.retryable = retryable
        self.request_id = request_id
        self.details = dict(details or {})

    @classmethod
    def from_envelope(cls, response: Mapping[str, Any]) -> "BlenderCommandError":
        error = response["error"]
        return cls(
            category=str(error.get("category", "BLENDER_ERROR")),
            error_type=str(error.get("type", "RuntimeError")),
            message=str(error.get("message", "Unknown Blender error")),
            traceback_text=error.get("traceback"),
            retryable=bool(error.get("retryable", False)),
            request_id=str(response.get("request_id", "")),
            details=error.get("details"),
        )


@dataclass
class BlenderConnection:
    """One serialized request/response stream to a Blender add-on."""

    host: str
    port: int
    timeout_seconds: float = 600.0
    socket_factory: Callable[..., Any] = field(
        default=socket.socket, repr=False
    )
    request_id_factory: Callable[[], str] = field(
        default=lambda: str(uuid.uuid4()), repr=False
    )
    sock: Any | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def _ensure_connected(self):
        if self.sock is not None:
            return self.sock
        new_socket = self.socket_factory(socket.AF_INET, socket.SOCK_STREAM)
        try:
            new_socket.settimeout(self.timeout_seconds)
            new_socket.connect((self.host, self.port))
        except Exception:
            new_socket.close()
            raise
        self.sock = new_socket
        logger.info("Connected to Blender at %s:%s", self.host, self.port)
        return new_socket

    def connect(self) -> bool:
        """Connect eagerly, returning false when Blender is unavailable."""
        try:
            self._ensure_connected()
            return True
        except Exception as exc:
            logger.info("Could not connect to Blender: %s", type(exc).__name__)
            return False

    def disconnect(self) -> None:
        """Close and invalidate the current stream."""
        current, self.sock = self.sock, None
        if current is not None:
            try:
                current.close()
            except OSError:
                pass

    def send_command(
        self, command_type: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send one command without replaying it after an uncertain failure."""
        with self._lock:
            request_id = self.request_id_factory()
            request = make_request(command_type, params, request_id=request_id)
            frame = encode_frame(request)
            started = time.monotonic()
            try:
                current = self._ensure_connected()
                current.sendall(frame)
                response = validate_response(receive_frame(current), request_id)
                if not response["success"]:
                    raise BlenderCommandError.from_envelope(response)
                logger.info(
                    "Blender command succeeded type=%s request_id=%s bytes=%s duration_ms=%.1f",
                    command_type,
                    request_id,
                    len(frame),
                    (time.monotonic() - started) * 1000,
                )
                result = response["result"]
                if not isinstance(result, dict):
                    raise RuntimeError("Blender command result must be an object")
                return result
            except BlenderCommandError:
                logger.info(
                    "Blender command failed type=%s request_id=%s duration_ms=%.1f",
                    command_type,
                    request_id,
                    (time.monotonic() - started) * 1000,
                )
                raise
            except Exception:
                self.disconnect()
                logger.info(
                    "Blender transport failed type=%s request_id=%s duration_ms=%.1f",
                    command_type,
                    request_id,
                    (time.monotonic() - started) * 1000,
                )
                raise
