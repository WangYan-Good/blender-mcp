"""Blender 5.x add-on for the local Blender MCP core bridge."""

import json
import io
import os
import socket
import struct
import threading
import time
import traceback
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import bmesh
import bpy
import mathutils
from bpy.props import BoolProperty, IntProperty, StringProperty

bl_info = {
    "name": "Blender MCP Core",
    "author": "BlenderMCP contributors",
    "version": (2, 0, 0),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > BlenderMCP",
    "description": (
        "Local MCP bridge for Blender inspection, Python, rendering, and persistence"
    ),
    "category": "Interface",
}

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_REQUEST_ID_BYTES = 128
MAX_CODE_BYTES = 1024 * 1024
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
MAX_RESULT_JSON_BYTES = 1024 * 1024
MAX_RESPONSE_STREAM_JSON_BYTES = 512 * 1024
MAX_RESPONSE_ERROR_JSON_BYTES = 1024 * 1024
MAX_SCENE_ITEMS = 2000
HEADER = struct.Struct("!I")
WORKDIR_ENV = "BLENDER_MCP_WORKDIR"
WORKDIR_SUBDIRS = ("input", "assets", "projects", "renders", "exports", "scripts")
VIEWPORT_VIEWS = frozenset(
    {"CURRENT", "FRONT", "BACK", "LEFT", "RIGHT", "TOP", "BOTTOM", "CAMERA"}
)
VIEWPORT_MODES = frozenset({"WIREFRAME", "SOLID", "MATERIAL", "RENDERED"})
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ProtocolError(RuntimeError):
    """A local socket peer sent an invalid frame or request."""


class FrameTooLargeError(ProtocolError):
    """A local socket message exceeded the configured maximum."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def safe_request_id(value: Any) -> str | None:
    if (
        isinstance(value, str)
        and value
        and len(value.encode("utf-8")) <= MAX_REQUEST_ID_BYTES
    ):
        return value
    return None


class CommandError(RuntimeError):
    """An expected command failure with stable diagnostics."""

    def __init__(
        self,
        category: str,
        message: str,
        *,
        error_type: str | None = None,
        traceback_text: str | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.error_type = error_type or type(self).__name__
        self.traceback_text = traceback_text
        self.retryable = retryable
        self.details = dict(details or {})

    def as_payload(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "type": self.error_type,
            "message": self.message,
            "traceback": self.traceback_text,
            "retryable": self.retryable,
            "details": self.details,
        }


def receive_exact(sock: socket.socket, size: int) -> bytes:
    """Receive exactly size bytes or fail without returning a partial frame."""
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ProtocolError(
                "connection closed before a complete frame was received"
            )
        data.extend(chunk)
    return bytes(data)


def send_frame(sock: socket.socket, message: dict[str, Any]) -> None:
    """Send one length-prefixed UTF-8 JSON object."""
    try:
        payload = json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"message is not JSON serializable: {exc}") from exc
    if len(payload) > MAX_FRAME_BYTES:
        raise FrameTooLargeError(
            f"frame is {len(payload)} bytes; maximum is {MAX_FRAME_BYTES}"
        )
    sock.sendall(HEADER.pack(len(payload)) + payload)


def receive_frame(sock: socket.socket) -> dict[str, Any]:
    """Receive one length-prefixed UTF-8 JSON object."""
    declared_size = HEADER.unpack(receive_exact(sock, HEADER.size))[0]
    if declared_size > MAX_FRAME_BYTES:
        raise FrameTooLargeError(
            f"declared frame is {declared_size} bytes; maximum is {MAX_FRAME_BYTES}"
        )
    try:
        message = json.loads(
            receive_exact(sock, declared_size).decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProtocolError(f"invalid frame payload: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("frame payload must be a JSON object")
    return message


def validate_request(request: dict[str, Any]) -> None:
    """Validate fields needed to route one Blender command."""
    if request.get("protocol_version") != PROTOCOL_VERSION:
        raise CommandError(
            "PROTOCOL_ERROR",
            "unsupported protocol version",
            error_type="ProtocolError",
        )
    if safe_request_id(request.get("request_id")) is None:
        raise CommandError(
            "PROTOCOL_ERROR",
            f"request_id must be 1 to {MAX_REQUEST_ID_BYTES} UTF-8 bytes",
            error_type="ProtocolError",
        )
    if not isinstance(request.get("type"), str) or not request["type"]:
        raise CommandError(
            "PROTOCOL_ERROR",
            "type must be a non-empty string",
            error_type="ProtocolError",
        )
    if not isinstance(request.get("params"), dict):
        raise CommandError(
            "PROTOCOL_ERROR",
            "params must be an object",
            error_type="ProtocolError",
        )


def truncate_utf8(text: str, max_bytes: int = MAX_CAPTURE_BYTES) -> tuple[str, bool]:
    """Truncate UTF-8 text without returning a partial code point."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def truncate_json_text(text: str, max_bytes: int) -> tuple[str, bool]:
    """Bound a string by its encoded JSON size, including escape expansion."""
    if len(_json_bytes(text)) <= max_bytes:
        return text, False
    low, high = 0, len(text)
    while low < high:
        midpoint = (low + high + 1) // 2
        if len(_json_bytes(text[:midpoint])) <= max_bytes:
            low = midpoint
        else:
            high = midpoint - 1
    return text[:low], True


def fit_response_to_frame(response: dict[str, Any]) -> dict[str, Any]:
    """Preserve diagnostics while guaranteeing the response can be framed."""
    try:
        if len(_json_bytes(response)) <= MAX_FRAME_BYTES:
            return response
    except (TypeError, ValueError, OverflowError):
        return {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": safe_request_id(response.get("request_id")),
            "success": False,
            "error": {
                "category": "PROTOCOL_ERROR",
                "type": "ResponseNotSerializable",
                "message": "Blender response is not strict JSON",
                "traceback": None,
                "retryable": False,
                "details": {},
            },
        }

    if response.get("success") and isinstance(response.get("result"), dict):
        result = response["result"]
        for field in ("stdout", "stderr"):
            value = result.get(field)
            if isinstance(value, str):
                bounded, truncated = truncate_json_text(
                    value, MAX_RESPONSE_STREAM_JSON_BYTES
                )
                result[field] = bounded
                if truncated:
                    result[f"{field}_truncated"] = True
    elif isinstance(response.get("error"), dict):
        error = response["error"]
        details = error.get("details")
        if not isinstance(details, dict):
            details = {}
            error["details"] = details
        for field in ("message", "traceback"):
            value = error.get(field)
            if isinstance(value, str):
                bounded, truncated = truncate_json_text(
                    value, MAX_RESPONSE_ERROR_JSON_BYTES
                )
                error[field] = bounded
                if truncated:
                    details[f"{field}_truncated"] = True
        for field in ("stdout", "stderr"):
            value = details.get(field)
            if isinstance(value, str):
                bounded, truncated = truncate_json_text(
                    value, MAX_RESPONSE_STREAM_JSON_BYTES
                )
                details[field] = bounded
                if truncated:
                    details[f"{field}_truncated"] = True

    if len(_json_bytes(response)) <= MAX_FRAME_BYTES:
        return response
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": safe_request_id(response.get("request_id")),
        "success": False,
        "error": {
            "category": "PROTOCOL_ERROR",
            "type": "ResponseTooLarge",
            "message": "Blender response exceeded the transport frame limit",
            "traceback": None,
            "retryable": False,
            "details": {"max_frame_bytes": MAX_FRAME_BYTES},
        },
    }


class CappedTextBuffer(io.TextIOBase):
    """Text writer that bounds captured output while reporting truncation."""

    def __init__(self, max_bytes: int = MAX_CAPTURE_BYTES) -> None:
        super().__init__()
        self.max_bytes = max_bytes
        self._data = bytearray()
        self.truncated = False

    def writable(self) -> bool:
        return True

    def write(self, value: str) -> int:
        if not isinstance(value, str):
            raise TypeError("captured output must be text")
        encoded = value.encode("utf-8", errors="replace")
        remaining = self.max_bytes - len(self._data)
        if remaining > 0:
            self._data.extend(encoded[:remaining])
        if len(encoded) > remaining:
            self.truncated = True
        return len(value)

    def getvalue(self) -> str:
        return bytes(self._data).decode("utf-8", errors="ignore")


def resolve_workdir(
    preference: str = "",
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
    home: Path | None = None,
) -> Path:
    """Resolve the configured workspace without creating it."""
    environment = os.environ if environ is None else environ
    configured = environment.get(WORKDIR_ENV, "").strip() or preference.strip()
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    return (Path.home() if home is None else Path(home)) / "BlenderMCP"


def initialize_workdir(workdir: Path) -> Path:
    """Create and return the canonical Blender MCP workspace."""
    root = Path(workdir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise CommandError(
            "VALIDATION_ERROR", "configured Blender MCP workspace is not a directory"
        )
    for name in WORKDIR_SUBDIRS:
        (root / name).mkdir(exist_ok=True)
    return root


def _is_within(root: Path, candidate: Path) -> bool:
    normalized_root = os.path.normcase(str(root))
    normalized_candidate = os.path.normcase(str(candidate))
    try:
        return os.path.commonpath(
            (normalized_root, normalized_candidate)
        ) == normalized_root
    except ValueError:
        return False


def resolve_managed_path(
    raw_path: str,
    *,
    workdir: Path,
    suffix: str | None = None,
    must_exist: bool = False,
) -> Path:
    """Resolve a path and ensure its canonical destination stays in workdir."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise CommandError("VALIDATION_ERROR", "path must be a non-empty string")

    root = Path(workdir).expanduser().resolve(strict=True)
    candidate = Path(os.path.expandvars(raw_path)).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    if suffix and candidate.suffix.lower() != suffix.lower():
        raise CommandError(
            "VALIDATION_ERROR", f"path must end with {suffix.lower()}"
        )

    try:
        # ``strict=False`` still resolves every existing symlink, including a
        # final output path, while permitting parents that will be created.
        # This prevents a workspace-local link from redirecting a managed
        # write outside the workspace.
        resolved = candidate.resolve(strict=must_exist)
    except FileNotFoundError as exc:
        raise CommandError("NOT_FOUND", f"path does not exist: {raw_path}") from exc

    if not _is_within(root, resolved):
        raise CommandError(
            "PATH_OUTSIDE_WORKDIR", "path is outside the Blender MCP workspace"
        )
    return resolved


def validate_image_dimensions(width: int, height: int) -> None:
    """Validate bounded image dimensions without accepting booleans as ints."""
    for name, value in (("width", width), ("height", height)):
        if type(value) is not int or not 64 <= value <= 4096:
            raise CommandError(
                "VALIDATION_ERROR",
                f"{name} must be an integer from 64 to 4096",
            )


def resolve_png_output(
    output_path: str | None,
    *,
    workdir: Path,
    prefix: str,
) -> Path:
    """Resolve a caller path or create a unique managed PNG destination."""
    if output_path is None:
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        output_path = f"renders/{prefix}-{timestamp}-{uuid.uuid4().hex[:8]}.png"
    elif not isinstance(output_path, str) or not output_path.strip():
        raise CommandError(
            "VALIDATION_ERROR", "output_path must be a non-empty string or null"
        )
    target = resolve_managed_path(
        output_path,
        workdir=workdir,
        suffix=".png",
        must_exist=False,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def validate_png_output(target: Path) -> int:
    """Require a non-empty PNG and return its byte size."""
    if not target.is_file() or target.stat().st_size == 0:
        raise CommandError(
            "BLENDER_ERROR", "Blender did not create the requested PNG"
        )
    try:
        with target.open("rb") as image_file:
            signature = image_file.read(len(PNG_SIGNATURE))
    except OSError as exc:
        raise CommandError(
            "BLENDER_ERROR", "Blender image output could not be read"
        ) from exc
    if signature != PNG_SIGNATURE:
        raise CommandError("BLENDER_ERROR", "Blender image output is not a PNG")
    return target.stat().st_size


def get_addon_preferences(context=None):
    """Return this add-on's preferences when Blender has registered them."""
    context = bpy.context if context is None else context
    preferences = getattr(context, "preferences", None)
    if preferences is None:
        return None
    addon = preferences.addons.get(__name__)
    return addon.preferences if addon else None


def get_configured_workdir(context=None) -> Path:
    preferences = get_addon_preferences(context)
    preference = getattr(preferences, "workdir", "") if preferences else ""
    return initialize_workdir(resolve_workdir(preference))


class BlenderMCPServer:
    """Local framed socket server that schedules Blender work on the main thread."""

    def __init__(self, host: str = "localhost", port: int = 9876) -> None:
        self.host = host
        self.port = port
        self.running = False
        self.socket: socket.socket | None = None
        self.server_thread: threading.Thread | None = None
        self._command_lock = threading.Lock()
        self._clients: set[socket.socket] = set()
        self._clients_lock = threading.Lock()

    def start(self) -> None:
        if self.running:
            return
        if bpy.app.background:
            raise RuntimeError("Blender MCP requires Blender's graphical interface")
        get_configured_workdir()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self.host, self.port))
            listener.listen(1)
            listener.settimeout(1.0)
        except Exception:
            listener.close()
            raise
        self.socket = listener
        self.running = True
        self.server_thread = threading.Thread(
            target=self._server_loop,
            name="BlenderMCPServer",
            daemon=True,
        )
        self.server_thread.start()
        print(f"Blender MCP listening on {self.host}:{self.port}")

    def stop(self) -> None:
        self.running = False
        listener, self.socket = self.socket, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        with self._clients_lock:
            clients = tuple(self._clients)
            self._clients.clear()
        for client in clients:
            try:
                client.close()
            except OSError:
                pass
        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(timeout=1.5)
        self.server_thread = None
        print("Blender MCP stopped")

    def _server_loop(self) -> None:
        while self.running and self.socket is not None:
            try:
                client, _address = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._clients_lock:
                if not self.running:
                    client.close()
                    break
                self._clients.add(client)
            threading.Thread(
                target=self._client_loop,
                args=(client,),
                name="BlenderMCPClient",
                daemon=True,
            ).start()

    def _client_loop(self, client: socket.socket) -> None:
        try:
            client.settimeout(None)
            while self.running:
                try:
                    request = receive_frame(client)
                except ProtocolError as exc:
                    print(f"Blender MCP protocol error: {exc}")
                    break
                if not self.running:
                    break

                completed = threading.Event()
                response_holder: dict[str, dict[str, Any]] = {}

                def execute_on_main_thread():
                    if not self.running:
                        completed.set()
                        return None
                    with self._command_lock:
                        response_holder["response"] = self._execute_request(request)
                    completed.set()
                    return None

                bpy.app.timers.register(execute_on_main_thread, first_interval=0.0)
                while self.running and not completed.wait(0.2):
                    pass
                if not completed.is_set():
                    break
                send_frame(client, response_holder["response"])
        except (ConnectionError, OSError, ProtocolError):
            pass
        finally:
            with self._clients_lock:
                self._clients.discard(client)
            try:
                client.close()
            except OSError:
                pass

    def _execute_request(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = safe_request_id(request.get("request_id"))
        try:
            validate_request(request)
            result = self._dispatch(request["type"], request["params"])
            response = {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
                "success": True,
                "result": result,
            }
        except CommandError as exc:
            response = {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
                "success": False,
                "error": exc.as_payload(),
            }
        except Exception as exc:
            response = {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
                "success": False,
                "error": {
                    "category": "BLENDER_ERROR",
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                    "retryable": False,
                    "details": {},
                },
            }
        return fit_response_to_frame(response)

    def _dispatch(self, command_type: str, params: dict[str, Any]) -> dict[str, Any]:
        handlers: dict[str, Any] = {
            "get_status": self.get_status,
            "execute_python": self.execute_python,
            "get_scene_info": self.get_scene_info,
            "save": self.save,
            "open": self.open_file,
            "render": self.render,
            "viewport_screenshot": self.viewport_screenshot,
        }
        handler = handlers.get(command_type)
        if handler is None:
            raise CommandError(
                "VALIDATION_ERROR",
                f"unknown command type: {command_type}",
                error_type="UnknownCommand",
            )
        return handler(**params)

    @staticmethod
    def _render_engine_identifiers(scene) -> list[str]:
        try:
            items = scene.render.bl_rna.properties["engine"].enum_items
            return sorted(str(item.identifier) for item in items)
        except (AttributeError, KeyError, TypeError):
            return [str(scene.render.engine)]

    def get_status(self) -> dict[str, Any]:
        """Report Blender readiness and the add-on's runtime constraints."""
        scene = bpy.context.scene
        workdir = get_configured_workdir()
        return {
            "success": True,
            "online": True,
            "ready": True,
            "blender_version": str(bpy.app.version_string),
            "addon_version": ".".join(str(part) for part in bl_info["version"]),
            "protocol_version": PROTOCOL_VERSION,
            "current_file": str(bpy.data.filepath) if bpy.data.filepath else None,
            "is_dirty": bool(bpy.data.is_dirty),
            "scene": scene.name,
            "workdir": str(workdir),
            "render_engines": self._render_engine_identifiers(scene),
            "image_formats": ["PNG"],
            "max_code_bytes": MAX_CODE_BYTES,
            "max_frame_bytes": MAX_FRAME_BYTES,
            "max_capture_bytes": MAX_CAPTURE_BYTES,
            "max_result_json_bytes": MAX_RESULT_JSON_BYTES,
            "max_scene_items": MAX_SCENE_ITEMS,
        }

    @staticmethod
    def _json_result(value: Any) -> tuple[Any, str, bool]:
        try:
            encoded = _json_bytes(value)
            if len(encoded) <= MAX_RESULT_JSON_BYTES:
                return value, "json", False
            oversized = True
        except (TypeError, ValueError, OverflowError):
            oversized = False
        try:
            rendered = repr(value)
        except Exception as exc:
            rendered = f"<repr failed: {type(exc).__name__}>"
        rendered, truncated = truncate_json_text(rendered, 16 * 1024)
        return rendered, "repr", bool(oversized or truncated)

    def execute_python(self, code: str) -> dict[str, Any]:
        """Execute unrestricted Python in Blender with bounded diagnostics."""
        if not isinstance(code, str):
            raise CommandError("VALIDATION_ERROR", "code must be a string")
        code_size = len(code.encode("utf-8"))
        if code_size > MAX_CODE_BYTES:
            raise CommandError(
                "VALIDATION_ERROR",
                f"code is {code_size} UTF-8 bytes; maximum is {MAX_CODE_BYTES}",
            )

        stdout = CappedTextBuffer()
        stderr = CappedTextBuffer()
        namespace = {
            "__name__": "__main__",
            "bpy": bpy,
            "bmesh": bmesh,
            "mathutils": mathutils,
        }
        started = time.monotonic()
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                compiled = compile(code, "<blender-mcp>", "exec")
                exec(compiled, namespace)
        except BaseException as exc:
            raise CommandError(
                "BLENDER_ERROR",
                str(exc),
                error_type=type(exc).__name__,
                traceback_text=traceback.format_exc(),
                details={
                    "stdout": stdout.getvalue(),
                    "stdout_truncated": stdout.truncated,
                    "stderr": stderr.getvalue(),
                    "stderr_truncated": stderr.truncated,
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                },
            ) from exc

        result, serialization, result_truncated = self._json_result(
            namespace.get("result")
        )
        return {
            "success": True,
            "stdout": stdout.getvalue(),
            "stdout_truncated": stdout.truncated,
            "stderr": stderr.getvalue(),
            "stderr_truncated": stderr.truncated,
            "result": result,
            "result_serialized_as": serialization,
            "result_truncated": result_truncated,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
        }

    @staticmethod
    def _float_triplet(values) -> list[float]:
        return [float(values[0]), float(values[1]), float(values[2])]

    def _object_summary(self, obj) -> dict[str, Any]:
        summary = {
            "name": obj.name,
            "type": obj.type,
            "collections": sorted(collection.name for collection in obj.users_collection),
            "visible": bool(obj.visible_get()),
            "hide_render": bool(obj.hide_render),
            "location": self._float_triplet(obj.location),
            "rotation": self._float_triplet(obj.rotation_euler),
            "scale": self._float_triplet(obj.scale),
        }
        if obj.type == "MESH" and obj.data is not None:
            summary["mesh"] = {
                "vertices": len(obj.data.vertices),
                "edges": len(obj.data.edges),
                "polygons": len(obj.data.polygons),
            }
        return summary

    def get_scene_info(
        self,
        include_objects: bool = True,
        include_materials: bool = True,
        include_collections: bool = True,
        include_cameras: bool = True,
        max_objects: int = 200,
    ) -> dict[str, Any]:
        """Return a deterministic, bounded summary of the active scene."""
        include_values = {
            "include_objects": include_objects,
            "include_materials": include_materials,
            "include_collections": include_collections,
            "include_cameras": include_cameras,
        }
        for name, value in include_values.items():
            if type(value) is not bool:
                raise CommandError("VALIDATION_ERROR", f"{name} must be boolean")
        if type(max_objects) is not int or not 1 <= max_objects <= 2000:
            raise CommandError(
                "VALIDATION_ERROR", "max_objects must be an integer from 1 to 2000"
            )

        scene = bpy.context.scene
        active = bpy.context.view_layer.objects.active
        objects = sorted(scene.objects, key=lambda item: item.name)
        truncated_sections: list[str] = []
        if include_objects and len(objects) > max_objects:
            truncated_sections.append("objects")
        if include_materials and len(bpy.data.materials) > MAX_SCENE_ITEMS:
            truncated_sections.append("materials")
        if include_collections and len(bpy.data.collections) > MAX_SCENE_ITEMS:
            truncated_sections.append("collections")
        camera_count = sum(obj.type == "CAMERA" for obj in objects)
        if include_cameras and camera_count > MAX_SCENE_ITEMS:
            truncated_sections.append("cameras")

        result: dict[str, Any] = {
            "success": True,
            "scene": scene.name,
            "frame": int(scene.frame_current),
            "render_engine": str(scene.render.engine),
            "active_object": active.name if active else None,
            "selected_objects": sorted(obj.name for obj in bpy.context.selected_objects),
            "camera": scene.camera.name if scene.camera else None,
            "object_count": len(objects),
            "material_count": len(bpy.data.materials),
            "collection_count": len(bpy.data.collections),
            "camera_count": camera_count,
            "truncated": bool(truncated_sections),
            "truncated_sections": truncated_sections,
        }
        if include_objects:
            result["objects"] = [
                self._object_summary(obj) for obj in objects[:max_objects]
            ]
        if include_materials:
            result["materials"] = [
                {"name": material.name, "use_nodes": bool(material.use_nodes)}
                for material in sorted(
                    bpy.data.materials, key=lambda item: item.name
                )[:MAX_SCENE_ITEMS]
            ]
        if include_collections:
            result["collections"] = [
                {"name": collection.name, "object_count": len(collection.objects)}
                for collection in sorted(
                    bpy.data.collections, key=lambda item: item.name
                )[:MAX_SCENE_ITEMS]
            ]
        if include_cameras:
            result["cameras"] = [
                {
                    "name": obj.name,
                    "projection": str(obj.data.type),
                    "lens": float(obj.data.lens),
                }
                for obj in objects
                if obj.type == "CAMERA"
            ][:MAX_SCENE_ITEMS]
        return result

    def save(
        self,
        path: str,
        overwrite: bool = False,
        compress: bool = True,
    ) -> dict[str, Any]:
        """Save a blend file inside the managed workspace."""
        if type(overwrite) is not bool:
            raise CommandError("VALIDATION_ERROR", "overwrite must be boolean")
        if type(compress) is not bool:
            raise CommandError("VALIDATION_ERROR", "compress must be boolean")

        target = resolve_managed_path(
            path,
            workdir=get_configured_workdir(),
            suffix=".blend",
            must_exist=False,
        )
        if target.exists() and not overwrite:
            raise CommandError(
                "VALIDATION_ERROR",
                "blend file already exists; pass overwrite=true to replace it",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        operation = bpy.ops.wm.save_as_mainfile(
            filepath=str(target),
            check_existing=False,
            compress=compress,
        )
        if "FINISHED" not in operation or not target.is_file():
            raise CommandError(
                "BLENDER_ERROR", "Blender did not create the requested blend file"
            )
        return {
            "success": True,
            "path": str(target),
            "size_bytes": target.stat().st_size,
            "is_dirty": bool(bpy.data.is_dirty),
        }

    def open_file(
        self,
        path: str,
        discard_unsaved: bool = False,
    ) -> dict[str, Any]:
        """Open a managed blend file without loading UI state or scripts."""
        if type(discard_unsaved) is not bool:
            raise CommandError(
                "VALIDATION_ERROR", "discard_unsaved must be boolean"
            )

        target = resolve_managed_path(
            path,
            workdir=get_configured_workdir(),
            suffix=".blend",
            must_exist=True,
        )
        if not target.is_file():
            raise CommandError("NOT_FOUND", f"blend file does not exist: {path}")
        if bpy.data.is_dirty and not discard_unsaved:
            raise CommandError(
                "DIRTY_FILE",
                "current blend file has unsaved changes; pass discard_unsaved=true to open",
            )

        operation = bpy.ops.wm.open_mainfile(
            filepath=str(target),
            load_ui=False,
            use_scripts=False,
        )
        if "FINISHED" not in operation:
            raise CommandError("BLENDER_ERROR", "Blender did not open the blend file")
        scene = bpy.context.scene
        return {
            "success": True,
            "path": str(target),
            "current_file": str(bpy.data.filepath) if bpy.data.filepath else None,
            "scene": scene.name,
            "object_count": len(scene.objects),
            "is_dirty": bool(bpy.data.is_dirty),
        }

    def render(
        self,
        camera: str | None = None,
        width: int = 1024,
        height: int = 1024,
        engine: str | None = None,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        """Render the active scene to a managed PNG and restore all settings."""
        validate_image_dimensions(width, height)
        if camera is not None and (
            not isinstance(camera, str) or not camera.strip()
        ):
            raise CommandError(
                "VALIDATION_ERROR", "camera must be a non-empty string or null"
            )
        if engine is not None and (
            not isinstance(engine, str) or not engine.strip()
        ):
            raise CommandError(
                "VALIDATION_ERROR", "engine must be a non-empty string or null"
            )

        scene = bpy.context.scene
        settings = scene.render
        render_camera = (
            scene.camera if camera is None else bpy.data.objects.get(camera)
        )
        if render_camera is None:
            raise CommandError("NOT_FOUND", "requested render camera was not found")
        if render_camera.type != "CAMERA":
            raise CommandError(
                "VALIDATION_ERROR", "requested object is not a camera"
            )

        valid_engines = self._render_engine_identifiers(scene)
        selected_engine = str(settings.engine) if engine is None else engine
        if selected_engine not in valid_engines:
            raise CommandError(
                "VALIDATION_ERROR",
                f"unsupported render engine: {selected_engine}",
                details={"supported_engines": valid_engines},
            )

        workdir = get_configured_workdir()
        target = resolve_png_output(
            output_path,
            workdir=workdir,
            prefix="render",
        )
        image_settings = settings.image_settings
        snapshot = {
            "camera": scene.camera,
            "engine": settings.engine,
            "filepath": settings.filepath,
            "resolution_x": settings.resolution_x,
            "resolution_y": settings.resolution_y,
            "resolution_percentage": settings.resolution_percentage,
            "file_format": image_settings.file_format,
            "color_mode": image_settings.color_mode,
        }
        started = time.monotonic()
        try:
            scene.camera = render_camera
            settings.engine = selected_engine
            settings.filepath = str(target)
            settings.resolution_x = width
            settings.resolution_y = height
            settings.resolution_percentage = 100
            image_settings.file_format = "PNG"
            image_settings.color_mode = "RGBA"

            operation = bpy.ops.render.render(write_still=True)
            if "FINISHED" not in operation:
                raise CommandError("BLENDER_ERROR", "Blender render was cancelled")
            size_bytes = validate_png_output(target)

            result = {
                "success": True,
                "image_path": str(target),
                "workdir": str(workdir),
                "width": width,
                "height": height,
                "camera": render_camera.name,
                "engine": selected_engine,
                "size_bytes": size_bytes,
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
            }
        finally:
            scene.camera = snapshot["camera"]
            settings.engine = snapshot["engine"]
            settings.filepath = snapshot["filepath"]
            settings.resolution_x = snapshot["resolution_x"]
            settings.resolution_y = snapshot["resolution_y"]
            settings.resolution_percentage = snapshot["resolution_percentage"]
            image_settings.file_format = snapshot["file_format"]
            image_settings.color_mode = snapshot["color_mode"]
        return result

    @staticmethod
    def _find_viewport_context():
        """Find one non-quad VIEW_3D window region suitable for operators."""
        manager = getattr(bpy.context, "window_manager", None)
        windows = list(getattr(manager, "windows", ()))
        current_window = getattr(bpy.context, "window", None)
        if current_window in windows:
            windows.remove(current_window)
            windows.insert(0, current_window)
        for window in windows:
            screen = getattr(window, "screen", None)
            for area in getattr(screen, "areas", ()):
                if getattr(area, "type", None) != "VIEW_3D":
                    continue
                space = getattr(getattr(area, "spaces", None), "active", None)
                if space is None or getattr(space, "type", None) != "VIEW_3D":
                    continue
                if getattr(space, "region_quadviews", None):
                    continue
                region = next(
                    (
                        item
                        for item in area.regions
                        if getattr(item, "type", None) == "WINDOW"
                    ),
                    None,
                )
                region_3d = getattr(space, "region_3d", None)
                if region is not None and region_3d is not None:
                    return window, screen, area, region, space, region_3d
        raise CommandError(
            "BLENDER_ERROR",
            "No 3D viewport is available; run Blender with its GUI",
        )

    @staticmethod
    def _snapshot_viewport_state(space, region_3d) -> dict[str, Any]:
        return {
            "view_distance": region_3d.view_distance,
            "view_location": region_3d.view_location.copy(),
            "view_rotation": region_3d.view_rotation.copy(),
            "view_perspective": region_3d.view_perspective,
            "view_camera_zoom": region_3d.view_camera_zoom,
            "view_camera_offset": region_3d.view_camera_offset.copy(),
            # This derived matrix is copied for diagnostics/draw consistency,
            # but restored by updating the writable view properties below.
            "view_matrix": region_3d.view_matrix.copy(),
            "shading": space.shading.type,
        }

    @staticmethod
    def _restore_viewport_state(space, region_3d, snapshot) -> None:
        space.shading.type = snapshot["shading"]
        region_3d.view_distance = snapshot["view_distance"]
        region_3d.view_location = snapshot["view_location"]
        region_3d.view_rotation = snapshot["view_rotation"]
        region_3d.view_perspective = snapshot["view_perspective"]
        region_3d.view_camera_zoom = snapshot["view_camera_zoom"]
        region_3d.view_camera_offset = snapshot["view_camera_offset"]
        region_3d.update()

    @staticmethod
    def _adapt_projection_aspect(
        projection_matrix,
        *,
        source_width: int,
        source_height: int,
        target_width: int,
        target_height: int,
    ):
        """Preserve vertical FOV when off-screen and region aspects differ."""
        projection = projection_matrix.copy()
        source_aspect = source_width / max(source_height, 1)
        target_aspect = target_width / target_height
        projection[0][0] *= source_aspect / target_aspect
        return projection

    def _capture_viewport_gpu(
        self,
        *,
        target: Path,
        width: int,
        height: int,
        scene,
        view_layer,
        space,
        region,
        region_3d,
        view_matrix,
        projection_matrix,
    ) -> None:
        """Draw one exact-size viewport image through Blender's GPU API."""
        import gpu
        import numpy as np

        offscreen = None
        image = None
        try:
            offscreen = gpu.types.GPUOffScreen(width, height, format="RGBA8")
            offscreen.draw_view3d(
                scene,
                view_layer,
                space,
                region,
                view_matrix,
                projection_matrix,
                do_color_management=True,
                draw_background=True,
            )
            pixel_buffer = offscreen.texture_color.read()
            pixels = np.frombuffer(
                pixel_buffer,
                dtype=np.uint8,
                count=width * height * 4,
            ).astype(np.float32)
            pixels *= 1.0 / 255.0

            image = bpy.data.images.new(
                f"BlenderMCP-{uuid.uuid4().hex}",
                width=width,
                height=height,
                alpha=True,
                float_buffer=False,
            )
            image.pixels.foreach_set(pixels)
            image.file_format = "PNG"
            image.filepath_raw = str(target)
            image.update()
            image.save()
            validate_png_output(target)
        finally:
            if image is not None:
                bpy.data.images.remove(image)
            if offscreen is not None:
                offscreen.free()

    def _capture_viewport_fallback(
        self,
        *,
        target: Path,
        width: int,
        height: int,
        **_context,
    ) -> None:
        """Capture a viewport area and resize it to the requested dimensions."""
        operation = bpy.ops.screen.screenshot_area(
            filepath=str(target),
            check_existing=False,
        )
        if "FINISHED" not in operation:
            raise RuntimeError("Blender viewport screenshot was cancelled")

        image = None
        try:
            image = bpy.data.images.load(str(target), check_existing=False)
            if tuple(image.size) != (width, height):
                image.scale(width, height)
            image.file_format = "PNG"
            image.filepath_raw = str(target)
            image.save()
            validate_png_output(target)
        finally:
            if image is not None:
                bpy.data.images.remove(image)

    def viewport_screenshot(
        self,
        view: str = "CURRENT",
        mode: str = "MATERIAL",
        width: int = 1024,
        height: int = 1024,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        """Capture an oriented and shaded 3D viewport to a managed PNG."""
        if not isinstance(view, str) or view not in VIEWPORT_VIEWS:
            raise CommandError(
                "VALIDATION_ERROR",
                f"view must be one of: {', '.join(sorted(VIEWPORT_VIEWS))}",
            )
        if not isinstance(mode, str) or mode not in VIEWPORT_MODES:
            raise CommandError(
                "VALIDATION_ERROR",
                f"mode must be one of: {', '.join(sorted(VIEWPORT_MODES))}",
            )
        validate_image_dimensions(width, height)
        window, screen, area, region, space, region_3d = (
            self._find_viewport_context()
        )
        if view == "CAMERA" and getattr(window.scene, "camera", None) is None:
            raise CommandError("NOT_FOUND", "active scene has no camera")
        workdir = get_configured_workdir()
        target = resolve_png_output(
            output_path,
            workdir=workdir,
            prefix="viewport",
        )
        snapshot = self._snapshot_viewport_state(space, region_3d)
        view_preferences = getattr(
            getattr(bpy.context, "preferences", None), "view", None
        )
        smooth_view = getattr(view_preferences, "smooth_view", None)
        started = time.monotonic()
        method = "gpu_offscreen"

        try:
            if smooth_view is not None:
                view_preferences.smooth_view = 0
            with bpy.context.temp_override(
                window=window,
                screen=screen,
                area=area,
                region=region,
            ):
                scene = bpy.context.scene
                view_layer = bpy.context.view_layer
                if view == "CAMERA":
                    if scene.camera is None:
                        raise CommandError(
                            "NOT_FOUND", "active scene has no camera"
                        )
                    if region_3d.view_perspective != "CAMERA":
                        operation = bpy.ops.view3d.view_camera()
                        if "FINISHED" not in operation:
                            raise CommandError(
                                "BLENDER_ERROR", "could not enter camera view"
                            )
                elif view != "CURRENT":
                    operation = bpy.ops.view3d.view_axis(
                        type=view,
                        align_active=False,
                        relative=False,
                    )
                    if "FINISHED" not in operation:
                        raise CommandError(
                            "BLENDER_ERROR", f"could not apply {view} view"
                        )

                space.shading.type = mode
                view_layer.update()
                region_3d.update()
                if view == "CAMERA":
                    view_matrix = scene.camera.matrix_world.inverted()
                    projection_matrix = scene.camera.calc_matrix_camera(
                        bpy.context.evaluated_depsgraph_get(),
                        x=width,
                        y=height,
                    )
                else:
                    view_matrix = region_3d.view_matrix.copy()
                    projection_matrix = self._adapt_projection_aspect(
                        region_3d.window_matrix,
                        source_width=region.width,
                        source_height=region.height,
                        target_width=width,
                        target_height=height,
                    )

                capture_context = {
                    "target": target,
                    "width": width,
                    "height": height,
                    "scene": scene,
                    "view_layer": view_layer,
                    "space": space,
                    "region": region,
                    "region_3d": region_3d,
                    "view_matrix": view_matrix,
                    "projection_matrix": projection_matrix,
                }
                try:
                    self._capture_viewport_gpu(**capture_context)
                except Exception as gpu_error:
                    method = "screen_fallback"
                    try:
                        self._capture_viewport_fallback(**capture_context)
                    except Exception as fallback_error:
                        raise CommandError(
                            "BLENDER_ERROR",
                            "GPU and screen viewport capture both failed",
                            details={
                                "gpu_error": (
                                    f"{type(gpu_error).__name__}: {gpu_error}"
                                ),
                                "fallback_error": (
                                    f"{type(fallback_error).__name__}: {fallback_error}"
                                ),
                            },
                        ) from fallback_error

                size_bytes = validate_png_output(target)
                result = {
                    "success": True,
                    "image_path": str(target),
                    "workdir": str(workdir),
                    "view": view,
                    "mode": mode,
                    "width": width,
                    "height": height,
                    "method": method,
                    "size_bytes": size_bytes,
                    "duration_ms": round(
                        (time.monotonic() - started) * 1000, 3
                    ),
                }
        finally:
            self._restore_viewport_state(space, region_3d, snapshot)
            if smooth_view is not None:
                view_preferences.smooth_view = smooth_view
        return result


class BLENDERMCP_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    workdir: StringProperty(
        name="Workspace",
        subtype="DIR_PATH",
        description=(
            "Managed root for blend files and images; BLENDER_MCP_WORKDIR overrides it"
        ),
        default="",
    )

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "workdir")
        layout.label(text="Managed file tools stay inside this workspace.")
        layout.label(text="execute_python remains unrestricted.", icon="ERROR")


class BLENDERMCP_PT_Panel(bpy.types.Panel):
    bl_label = "Blender MCP"
    bl_idname = "BLENDERMCP_PT_core"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BlenderMCP"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.prop(scene, "blendermcp_port")
        try:
            layout.label(text=f"Workspace: {get_configured_workdir(context)}")
        except Exception as exc:
            layout.label(text=f"Workspace error: {exc}", icon="ERROR")
        if scene.blendermcp_server_running:
            layout.operator("blendermcp.stop_server", text="Stop MCP Server")
            layout.label(text=f"Listening on localhost:{scene.blendermcp_port}")
        else:
            layout.operator("blendermcp.start_server", text="Start MCP Server")


class BLENDERMCP_OT_StartServer(bpy.types.Operator):
    bl_idname = "blendermcp.start_server"
    bl_label = "Start Blender MCP Server"

    def execute(self, context):
        try:
            server = getattr(bpy.types, "blendermcp_server", None)
            if server is None:
                server = BlenderMCPServer(port=context.scene.blendermcp_port)
                bpy.types.blendermcp_server = server
            server.start()
            context.scene.blendermcp_server_running = server.running
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class BLENDERMCP_OT_StopServer(bpy.types.Operator):
    bl_idname = "blendermcp.stop_server"
    bl_label = "Stop Blender MCP Server"

    def execute(self, context):
        server = getattr(bpy.types, "blendermcp_server", None)
        if server is not None:
            server.stop()
            del bpy.types.blendermcp_server
        context.scene.blendermcp_server_running = False
        return {"FINISHED"}


CLASSES = (
    BLENDERMCP_AddonPreferences,
    BLENDERMCP_PT_Panel,
    BLENDERMCP_OT_StartServer,
    BLENDERMCP_OT_StopServer,
)


def _auto_start_server() -> None:
    """Start the socket server once Blender has a scene.

    Add-ons enabled from saved preferences register before any scene exists,
    so ``register`` defers auto-start to this one-shot timer.
    """
    scene = getattr(bpy.context, "scene", None)
    if scene is None or not scene.blendermcp_auto_start_server:
        return None
    server = getattr(bpy.types, "blendermcp_server", None)
    if server is not None and server.running:
        return None
    if server is None:
        server = BlenderMCPServer(port=scene.blendermcp_port)
        bpy.types.blendermcp_server = server
    try:
        server.start()
        scene.blendermcp_server_running = server.running
    except Exception as exc:
        print(f"Blender MCP auto-start failed: {exc}")
    return None


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.blendermcp_port = IntProperty(
        name="Port", default=9876, min=1024, max=65535
    )
    bpy.types.Scene.blendermcp_server_running = BoolProperty(default=False)
    bpy.types.Scene.blendermcp_auto_start_server = BoolProperty(
        name="Auto-start Server", default=True
    )

    # Persistent so a .blend opened at launch does not drop the pending start.
    bpy.app.timers.register(_auto_start_server, first_interval=0.0, persistent=True)


def unregister() -> None:
    if bpy.app.timers.is_registered(_auto_start_server):
        bpy.app.timers.unregister(_auto_start_server)
    server = getattr(bpy.types, "blendermcp_server", None)
    if server is not None:
        server.stop()
        del bpy.types.blendermcp_server

    for name in (
        "blendermcp_port",
        "blendermcp_server_running",
        "blendermcp_auto_start_server",
    ):
        if hasattr(bpy.types.Scene, name):
            delattr(bpy.types.Scene, name)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
