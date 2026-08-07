"""Core Blender MCP server exposing local inspection and execution tools."""

from __future__ import annotations

import logging
import os
import sys
import warnings
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic_settings.exceptions import IncompleteFieldDefinitionWarning

from .connection import BlenderConnection
from .tool_results import (
    ImageResultError,
    error_result,
    image_result,
    text_result,
    validation_error,
)

logger = logging.getLogger("BlenderMCPServer")

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9876
DEFAULT_TIMEOUT_SECONDS = 600.0
MAX_CODE_BYTES = 1024 * 1024

_blender_connection: Any | None = None


def get_blender_connection() -> Any:
    """Lazily create the local Blender connection."""
    global _blender_connection
    if _blender_connection is None:
        host = os.getenv("BLENDER_HOST", DEFAULT_HOST)
        port = int(os.getenv("BLENDER_PORT", str(DEFAULT_PORT)))
        timeout_seconds = float(
            os.getenv("BLENDER_MCP_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
        )
        if not 1 <= port <= 65535:
            raise ValueError("BLENDER_PORT must be between 1 and 65535")
        if timeout_seconds <= 0:
            raise ValueError("BLENDER_MCP_TIMEOUT_SECONDS must be positive")
        _blender_connection = BlenderConnection(
            host=host, port=port, timeout_seconds=timeout_seconds
        )
    return _blender_connection


def _run_text(command_type: str, params: dict[str, Any] | None = None) -> CallToolResult:
    try:
        metadata = get_blender_connection().send_command(command_type, params)
        return text_result(metadata)
    except Exception as exc:
        return error_result(exc)


def _run_image(command_type: str, params: dict[str, Any]) -> CallToolResult:
    try:
        metadata = get_blender_connection().send_command(command_type, params)
        workdir = metadata.get("workdir")
        if not isinstance(workdir, str) or not workdir:
            raise ImageResultError(
                "VALIDATION_ERROR", "Blender image response is missing workdir"
            )
        return image_result(metadata, workdir=workdir)
    except Exception as exc:
        return error_result(exc)


@asynccontextmanager
async def server_lifespan(_server):
    logger.info("Blender MCP server starting")
    try:
        yield {}
    finally:
        global _blender_connection
        if _blender_connection is not None:
            _blender_connection.disconnect()
            _blender_connection = None
        logger.info("Blender MCP server stopped")


with warnings.catch_warnings():
    # MCP 1.29 emits this false-positive while resolving its generic lifespan
    # annotation on Python 3.13. It does not affect stdio lifecycle behavior.
    warnings.filterwarnings(
        "ignore",
        category=IncompleteFieldDefinitionWarning,
        message="Field 'lifespan' has an incomplete definition.*",
    )
    mcp = FastMCP(
        "BlenderMCP",
        instructions=(
            "Inspect the scene, execute Blender Python, and visually verify changes. "
            "execute_python is unrestricted; managed file tools stay in Blender's workspace."
        ),
        lifespan=server_lifespan,
    )


@mcp.tool(structured_output=False)
def get_status() -> CallToolResult:
    """Check Blender readiness, versions, current file, workspace, and limits."""
    return _run_text("get_status")


@mcp.tool(structured_output=False)
def execute_python(code: str) -> CallToolResult:
    """Execute unrestricted Python inside Blender and return full diagnostics."""
    if not isinstance(code, str):
        return validation_error("code must be a string")
    code_size = len(code.encode("utf-8"))
    if code_size > MAX_CODE_BYTES:
        return validation_error(
            f"code is {code_size} UTF-8 bytes; maximum is {MAX_CODE_BYTES}"
        )
    return _run_text("execute_python", {"code": code})


@mcp.tool(structured_output=False)
def get_scene_info(
    include_objects: bool = True,
    include_materials: bool = True,
    include_collections: bool = True,
    include_cameras: bool = True,
    max_objects: int = 200,
) -> CallToolResult:
    """Get a deterministic, bounded summary of the active Blender scene."""
    return _run_text(
        "get_scene_info",
        {
            "include_objects": include_objects,
            "include_materials": include_materials,
            "include_collections": include_collections,
            "include_cameras": include_cameras,
            "max_objects": max_objects,
        },
    )


@mcp.tool(structured_output=False)
def save(
    path: str,
    overwrite: bool = False,
    compress: bool = True,
) -> CallToolResult:
    """Save a .blend file inside Blender's managed workspace."""
    if not isinstance(path, str) or not path.strip():
        return validation_error("path must be a non-empty string")
    if type(overwrite) is not bool:
        return validation_error("overwrite must be boolean")
    if type(compress) is not bool:
        return validation_error("compress must be boolean")
    return _run_text(
        "save",
        {"path": path, "overwrite": overwrite, "compress": compress},
    )


@mcp.tool(structured_output=False)
def open(path: str, discard_unsaved: bool = False) -> CallToolResult:
    """Open a managed .blend file, preserving dirty work unless explicitly discarded."""
    if not isinstance(path, str) or not path.strip():
        return validation_error("path must be a non-empty string")
    if type(discard_unsaved) is not bool:
        return validation_error("discard_unsaved must be boolean")
    return _run_text(
        "open",
        {"path": path, "discard_unsaved": discard_unsaved},
    )


@mcp.tool(structured_output=False)
def render(
    camera: str | None = None,
    width: int = 1024,
    height: int = 1024,
    engine: str | None = None,
    output_path: str | None = None,
) -> CallToolResult:
    """Render a camera view and return metadata plus inline PNG content."""
    for name, value in (("width", width), ("height", height)):
        if type(value) is not int or not 64 <= value <= 4096:
            return validation_error(
                f"{name} must be an integer from 64 to 4096"
            )
    for name, value in (
        ("camera", camera),
        ("engine", engine),
        ("output_path", output_path),
    ):
        if value is not None and (
            not isinstance(value, str) or not value.strip()
        ):
            return validation_error(
                f"{name} must be a non-empty string or null"
            )
    return _run_image(
        "render",
        {
            "camera": camera,
            "width": width,
            "height": height,
            "engine": engine,
            "output_path": output_path,
        },
    )


def main() -> None:
    """Run the MCP server over stdio."""
    try:
        interactive = sys.stdin.isatty()
    except (AttributeError, OSError):
        interactive = False
    if interactive:
        logger.info(
            "BlenderMCP is a stdio MCP server and should be launched by an MCP client."
        )
    mcp.run()


if __name__ == "__main__":
    main()
