"""Blender integration through the Model Context Protocol."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("blender-mcp")
except PackageNotFoundError:
    # Package is not installed (e.g. running from a source checkout)
    __version__ = "unknown"

# Importing the package must not start or configure the MCP server.
from .connection import BlenderCommandError, BlenderConnection

__all__ = ["BlenderCommandError", "BlenderConnection", "__version__"]
