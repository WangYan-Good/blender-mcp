"""Construction of structured text and image MCP tool results."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, ImageContent, TextContent

from .connection import BlenderCommandError
from .protocol import ProtocolError

MAX_IMAGE_BYTES = 32 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ImageResultError(ValueError):
    """A Blender image result cannot be safely returned to the MCP client."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def _json_text(metadata: Mapping[str, Any]) -> str:
    return json.dumps(dict(metadata), ensure_ascii=False, indent=2)


def text_result(
    metadata: Mapping[str, Any], *, is_error: bool = False
) -> CallToolResult:
    """Return semantically equivalent visible and structured JSON."""
    structured = dict(metadata)
    return CallToolResult(
        content=[TextContent(type="text", text=_json_text(structured))],
        structuredContent=structured,
        isError=is_error,
    )


def _is_within(root: Path, candidate: Path) -> bool:
    normalized_root = os.path.normcase(str(root))
    normalized_candidate = os.path.normcase(str(candidate))
    try:
        return os.path.commonpath(
            (normalized_root, normalized_candidate)
        ) == normalized_root
    except ValueError:
        return False


def image_result(
    metadata: Mapping[str, Any], *, workdir: str
) -> CallToolResult:
    """Read one managed PNG and include it alongside structured metadata."""
    try:
        root = Path(workdir).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise ImageResultError(
            "NOT_FOUND", "Blender MCP workspace does not exist"
        ) from exc
    raw_image_path = metadata.get("image_path")
    if not isinstance(raw_image_path, str) or not raw_image_path:
        raise ImageResultError(
            "VALIDATION_ERROR", "Blender response is missing image_path"
        )
    try:
        image_path = Path(raw_image_path).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise ImageResultError("NOT_FOUND", "image file does not exist") from exc
    if not _is_within(root, image_path):
        raise ImageResultError(
            "PATH_OUTSIDE_WORKDIR", "image path is outside the Blender MCP workspace"
        )
    if not image_path.is_file():
        raise ImageResultError("NOT_FOUND", "image path is not a regular file")
    size = image_path.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise ImageResultError(
            "VALIDATION_ERROR",
            f"image is {size} bytes; maximum is {MAX_IMAGE_BYTES}",
        )
    image_bytes = image_path.read_bytes()
    if not image_bytes.startswith(PNG_SIGNATURE):
        raise ImageResultError("VALIDATION_ERROR", "image is not a valid PNG file")

    structured = dict(metadata)
    structured["image_path"] = str(image_path)
    return CallToolResult(
        content=[
            TextContent(type="text", text=_json_text(structured)),
            ImageContent(
                type="image",
                data=base64.b64encode(image_bytes).decode("ascii"),
                mimeType="image/png",
            ),
        ],
        structuredContent=structured,
        isError=False,
    )


def validation_error(message: str) -> CallToolResult:
    return text_result(
        {
            "success": False,
            "error": {
                "category": "VALIDATION_ERROR",
                "type": "ValidationError",
                "message": message,
                "traceback": None,
                "retryable": False,
                "details": {},
            },
        },
        is_error=True,
    )


def error_result(error: Exception) -> CallToolResult:
    """Convert local and remote failures into stable MCP error content."""
    if isinstance(error, BlenderCommandError):
        category = error.category
        error_type = error.error_type
        message = error.message
        traceback_text = error.traceback_text
        retryable = error.retryable
        request_id: str | None = error.request_id
        details = error.details
    elif isinstance(error, ImageResultError):
        category = error.category
        error_type = type(error).__name__
        message = str(error)
        traceback_text = None
        retryable = False
        request_id = None
        details = {}
    elif isinstance(error, ProtocolError):
        category = "PROTOCOL_ERROR"
        error_type = type(error).__name__
        message = str(error)
        traceback_text = None
        retryable = False
        request_id = None
        details = {}
    elif isinstance(error, (ConnectionError, TimeoutError, OSError)):
        category = "CONNECTION_ERROR"
        error_type = type(error).__name__
        message = str(error)
        traceback_text = None
        retryable = True
        request_id = None
        details = {}
    else:
        category = "INTERNAL_ERROR"
        error_type = type(error).__name__
        message = str(error)
        traceback_text = None
        retryable = False
        request_id = None
        details = {}

    metadata: dict[str, Any] = {
        "success": False,
        "error": {
            "category": category,
            "type": error_type,
            "message": message,
            "traceback": traceback_text,
            "retryable": retryable,
            "details": details,
        },
    }
    if request_id:
        metadata["request_id"] = request_id
    return text_result(metadata, is_error=True)
