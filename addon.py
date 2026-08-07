"""Blender 5.x add-on for the local Blender MCP core bridge."""

import json
import io
import os
import socket
import struct
import threading
import time
import traceback
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
MAX_CODE_BYTES = 1024 * 1024
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
HEADER = struct.Struct("!I")
WORKDIR_ENV = "BLENDER_MCP_WORKDIR"
WORKDIR_SUBDIRS = ("input", "assets", "projects", "renders", "exports", "scripts")


class ProtocolError(RuntimeError):
    """A local socket peer sent an invalid frame or request."""


class FrameTooLargeError(ProtocolError):
    """A local socket message exceeded the configured maximum."""


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
            message, ensure_ascii=False, separators=(",", ":")
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
        message = json.loads(receive_exact(sock, declared_size).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
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
    if not isinstance(request.get("request_id"), str) or not request["request_id"]:
        raise CommandError(
            "PROTOCOL_ERROR",
            "request_id must be a non-empty string",
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

                completed = threading.Event()
                response_holder: dict[str, dict[str, Any]] = {}

                def execute_on_main_thread():
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
            try:
                client.close()
            except OSError:
                pass

    def _execute_request(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("request_id")
        try:
            validate_request(request)
            result = self._dispatch(request["type"], request["params"])
            return {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
                "success": True,
                "result": result,
            }
        except CommandError as exc:
            return {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
                "success": False,
                "error": exc.as_payload(),
            }
        except Exception as exc:
            return {
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

    def _dispatch(self, command_type: str, params: dict[str, Any]) -> dict[str, Any]:
        handlers: dict[str, Any] = {
            "get_status": self.get_status,
            "execute_python": self.execute_python,
            "get_scene_info": self.get_scene_info,
            "save": self.save,
            "open": self.open_file,
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
        }

    @staticmethod
    def _json_result(value: Any) -> tuple[Any, str]:
        try:
            json.dumps(value, ensure_ascii=False)
            return value, "json"
        except (TypeError, ValueError, OverflowError):
            try:
                rendered = repr(value)
            except Exception as exc:
                rendered = f"<repr failed: {type(exc).__name__}>"
            return truncate_utf8(rendered, max_bytes=16 * 1024)[0], "repr"

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

        result, serialization = self._json_result(namespace.get("result"))
        return {
            "success": True,
            "stdout": stdout.getvalue(),
            "stdout_truncated": stdout.truncated,
            "stderr": stderr.getvalue(),
            "stderr_truncated": stderr.truncated,
            "result": result,
            "result_serialized_as": serialization,
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
            "camera_count": sum(obj.type == "CAMERA" for obj in objects),
            "truncated": bool(include_objects and len(objects) > max_objects),
        }
        if include_objects:
            result["objects"] = [
                self._object_summary(obj) for obj in objects[:max_objects]
            ]
        if include_materials:
            result["materials"] = [
                {"name": material.name, "use_nodes": bool(material.use_nodes)}
                for material in sorted(bpy.data.materials, key=lambda item: item.name)
            ]
        if include_collections:
            result["collections"] = [
                {"name": collection.name, "object_count": len(collection.objects)}
                for collection in sorted(
                    bpy.data.collections, key=lambda item: item.name
                )
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
            ]
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

    scene = getattr(bpy.context, "scene", None)
    if scene is not None and scene.blendermcp_auto_start_server:
        server = BlenderMCPServer(port=scene.blendermcp_port)
        bpy.types.blendermcp_server = server
        try:
            server.start()
            scene.blendermcp_server_running = server.running
        except Exception as exc:
            print(f"Blender MCP auto-start failed: {exc}")


def unregister() -> None:
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
