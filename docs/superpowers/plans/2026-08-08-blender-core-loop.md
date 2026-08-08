# Blender MCP Core Iteration Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver seven dependable MCP tools that close the Blender inspect, execute, render, view, revise, and persist loop on Windows with Blender 5.x.

**Architecture:** Replace the unframed socket exchange with serialized, length-prefixed JSON request/response envelopes. Keep Blender API work on Blender's main thread, enforce a Blender-owned workspace for managed file tools, and return structured MCP errors plus mixed text/image results from thin server adapters.

**Tech Stack:** Python 3.10+, Blender 5.x Python API, TCP sockets, MCP Python SDK `mcp[cli]>=1.28.1,<2`, standard-library `unittest`, and Blender GPU off-screen rendering.

## Global Constraints

- Primary runtime target: Windows and Blender 5.x.
- Blender and the MCP server run on the same Windows machine.
- Expose exactly these core tools: `get_status`, `execute_python`, `get_scene_info`, `render`, `viewport_screenshot`, `save`, and `open`.
- Do not preserve compatibility aliases for old core tool signatures.
- `BLENDER_MCP_WORKDIR` overrides the add-on preference; default to `%USERPROFILE%\BlenderMCP`.
- Restrict save, open, render, and screenshot paths to the managed workspace; do not restrict `execute_python`.
- Accept at most 1 MiB of UTF-8 Python source and at most 8 MiB per socket frame.
- Cap captured stdout and stderr at 2 MiB each and report truncation explicitly.
- Never upload prompts, code, stdout/stderr, paths, screenshots, or render output.
- Refuse to open a blend file with dirty state unless `discard_unsaved=true`; disable embedded scripts when opening.
- Do not claim real Blender verification from this Linux workspace; distinguish automated checks from the Windows Blender 5.x smoke test.

## File Map

- Create `src/blender_mcp/protocol.py`: frame codec, envelopes, response validation, and protocol exceptions.
- Create `src/blender_mcp/connection.py`: serialized socket lifecycle and Blender command errors.
- Create `src/blender_mcp/tool_results.py`: safe PNG loading and MCP `CallToolResult` builders.
- Replace `src/blender_mcp/server.py`: only the seven core MCP registrations and lifecycle.
- Replace `addon.py`: compact Blender 5.x add-on containing framed transport, workspace policy, seven handlers, and UI preferences.
- Delete `src/blender_mcp/telemetry.py` and `src/blender_mcp/telemetry_decorator.py`: no remote telemetry remains.
- Modify `src/blender_mcp/__init__.py`: export the new connection class without importing legacy telemetry code.
- Modify `pyproject.toml` and `uv.lock`: require maintained MCP 1.x and remove direct `httpx` dependency.
- Create `tests/__init__.py`, `tests/test_protocol.py`, `tests/test_connection.py`, `tests/test_tool_results.py`, `tests/test_server_tools.py`, and `tests/test_addon_contract.py`.
- Replace `README.md`: Windows/Blender 5.x installation, workspace, seven tools, security model, and troubleshooting.
- Create `docs/windows-blender-5-smoke-test.md`: manual end-to-end acceptance procedure.
- Modify `TERMS_AND_CONDITIONS.md`: remove claims that this build collects or licenses telemetry data.

## Execution Order

Execute tasks by number: `1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 8`. The detailed
Task 1 section begins after the Task 6 section in this document; numerical order,
not page order, is authoritative because Tasks 4–6 consume Task 1–3 interfaces.

---

### Task 4: MCP Core Adapter, Structured Errors, and Privacy Removal

**Files:**
- Create: `src/blender_mcp/tool_results.py`
- Replace: `src/blender_mcp/server.py`
- Delete: `src/blender_mcp/telemetry.py`
- Delete: `src/blender_mcp/telemetry_decorator.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/test_tool_results.py`
- Create: `tests/test_server_tools.py`

**Interfaces:**
- Consumes: `BlenderConnection.send_command` and `BlenderCommandError` from Task 1.
- Produces: `text_result(metadata: Mapping[str, Any], *, is_error: bool = False) -> CallToolResult`.
- Produces: `image_result(metadata: Mapping[str, Any], *, workdir: str) -> CallToolResult`.
- Produces: `error_result(error: Exception) -> CallToolResult`.
- Produces: MCP tools `get_status`, `execute_python`, and `get_scene_info` that always return `CallToolResult`.

- [ ] **Step 1: Write failing result-builder tests**

```python
# tests/test_tool_results.py
import base64
import json
import tempfile
import unittest
from pathlib import Path

from blender_mcp.connection import BlenderCommandError
from blender_mcp.tool_results import error_result, image_result, text_result

PNG = b"\x89PNG\r\n\x1a\n" + b"test-payload"

class ToolResultTests(unittest.TestCase):
    def test_text_result_has_visible_and_structured_metadata(self):
        result = text_result({"success": True, "ready": True})
        self.assertEqual(json.loads(result.content[0].text)["ready"], True)
        self.assertEqual(result.structuredContent["ready"], True)
        self.assertFalse(result.isError)

    def test_image_result_contains_text_and_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            image = root / "renders" / "view.png"
            image.parent.mkdir()
            image.write_bytes(PNG)
            result = image_result({"success": True, "image_path": str(image)}, workdir=str(root))
            self.assertEqual(result.content[1].mimeType, "image/png")
            self.assertEqual(base64.b64decode(result.content[1].data), PNG)

    def test_image_outside_workdir_is_rejected(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            path = Path(outside) / "bad.png"
            path.write_bytes(PNG)
            with self.assertRaises(ValueError):
                image_result({"image_path": str(path)}, workdir=root)

    def test_remote_error_is_marked_as_mcp_error(self):
        error = BlenderCommandError("BLENDER_ERROR", "KeyError", "missing", "trace", False, "req", {"stdout": "before"})
        result = error_result(error)
        self.assertTrue(result.isError)
        self.assertEqual(result.structuredContent["error"]["type"], "KeyError")
```

- [ ] **Step 2: Write failing server registration tests using an injected fake connection**

```python
# tests/test_server_tools.py
import unittest

import blender_mcp.server as server

class FakeConnection:
    def __init__(self): self.calls = []
    def send_command(self, name, params=None):
        self.calls.append((name, params or {}))
        return {"success": True, "command": name}
    def disconnect(self): pass

class ServerToolTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeConnection()
        server.set_connection_for_testing(self.fake)

    def tearDown(self):
        server.set_connection_for_testing(None)

    def test_execute_python_forwards_code_without_logging_prompt_argument(self):
        result = server.execute_python("result = 4")
        self.assertEqual(self.fake.calls, [("execute_python", {"code": "result = 4"})])
        self.assertFalse(result.isError)

    def test_scene_flags_are_forwarded(self):
        server.get_scene_info(False, True, False, True, 25)
        self.assertEqual(self.fake.calls[0][1]["max_objects"], 25)
```

- [ ] **Step 3: Run focused tests and verify imports fail**

Run: `PYTHONPATH=src python3 -m unittest tests.test_tool_results tests.test_server_tools -v`

Expected: FAIL because `tool_results.py` and the new server functions do not exist.

- [ ] **Step 4: Upgrade the supported MCP SDK and refresh the lock**

Change runtime dependencies to:

```toml
dependencies = [
    "mcp[cli]>=1.28.1,<2",
]
```

Remove the direct `httpx` dependency and set the project version to `2.0.0`
because the public tools and Blender runtime floor are intentionally breaking.
Use a temporary uv executable if the host lacks uv, then regenerate rather than
hand-editing the lock:

```bash
python3 -m venv /tmp/blender-mcp-uv
/tmp/blender-mcp-uv/bin/pip install uv
/tmp/blender-mcp-uv/bin/uv lock
```

Expected: `uv.lock` resolves `mcp` at 1.28.1 or a newer 1.x release and retains the project package.

- [ ] **Step 5: Implement direct MCP result builders**

Use `CallToolResult`, `TextContent`, and `ImageContent` from `mcp.types`. Serialize metadata with `ensure_ascii=False`, set `structuredContent=dict(metadata)`, and set `isError` in the v1.28.1 model. `image_result` must resolve both workdir and image, use `os.path.commonpath` with `normcase`, require a regular file no larger than 32 MiB, validate the eight-byte PNG signature, then base64-encode it.

`error_result` maps `BlenderCommandError` fields verbatim. Other exceptions become category `CONNECTION_ERROR` for `ConnectionError`/`TimeoutError` and `INTERNAL_ERROR` otherwise; unexpected errors expose the exception type and message but no local Python traceback.

- [ ] **Step 6: Replace the MCP server with three initial core registrations**

Create one lazily initialized global connection using `BLENDER_HOST`, `BLENDER_PORT`, and `BLENDER_MCP_TIMEOUT_SECONDS`. `set_connection_for_testing` replaces it deterministically. The lifespan only logs local startup/shutdown and disconnects on shutdown; it performs no network request and does not require Blender to be online at MCP startup.

```python
@mcp.tool(structured_output=False)
def get_status() -> CallToolResult:
    return _run_text("get_status")

@mcp.tool(structured_output=False)
def execute_python(code: str) -> CallToolResult:
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        return validation_error("code exceeds 1048576 UTF-8 bytes")
    return _run_text("execute_python", {"code": code})

@mcp.tool(structured_output=False)
def get_scene_info(
    include_objects: bool = True,
    include_materials: bool = True,
    include_collections: bool = True,
    include_cameras: bool = True,
    max_objects: int = 200,
) -> CallToolResult:
    return _run_text("get_scene_info", locals())
```

Remove prompts and all optional provider tools from `server.py`. Tool docstrings must describe the unrestricted nature of `execute_python` and the bounded scene response.

- [ ] **Step 7: Delete telemetry implementation and prove it is unreachable**

Delete both telemetry source files. Add assertions to `tests/test_server_tools.py` that `server.py` contains none of `telemetry`, `upload_screenshot`, `user_prompt`, or `httpx`, and that the registered tool names are exactly the three names implemented at this stage.

- [ ] **Step 8: Install the project in an isolated environment and run focused tests**

```bash
python3 -m venv .venv
./.venv/bin/pip install -e .
./.venv/bin/python -m unittest tests.test_protocol tests.test_connection tests.test_tool_results tests.test_server_tools -v
```

Expected: PASS. Inspect one `CallToolResult` in the test to confirm text uses `content[0]`, structured metadata is present, and `isError` is false on success.

- [ ] **Step 9: Commit Task 4**

```bash
git add pyproject.toml uv.lock src/blender_mcp tests/test_tool_results.py tests/test_server_tools.py
git commit -m "feat: expose private structured Blender core tools"
```

---

### Task 5: Safe Blend Save and Open Lifecycle

**Files:**
- Modify: `addon.py`
- Modify: `src/blender_mcp/server.py`
- Modify: `tests/test_addon_contract.py`
- Modify: `tests/test_server_tools.py`

**Interfaces:**
- Consumes: `resolve_managed_path`, framed dispatcher, and MCP `_run_text` adapter.
- Produces: add-on `save(path: str, overwrite: bool = False, compress: bool = True) -> dict`.
- Produces: add-on `open_file(path: str, discard_unsaved: bool = False) -> dict`, dispatched as command `open`.
- Produces: MCP tools `save(path, overwrite=False, compress=True)` and `open(path, discard_unsaved=False)`.

- [ ] **Step 1: Add failing add-on save/open contract tests**

```python
def test_save_and_open_handlers_are_registered(self):
    self.assertIn('"save": self.save', self.source)
    self.assertIn('"open": self.open_file', self.source)

def test_open_protects_dirty_state_and_embedded_scripts(self):
    self.assertIn("bpy.data.is_dirty", self.source)
    self.assertIn('"DIRTY_FILE"', self.source)
    self.assertIn("discard_unsaved", self.source)
    self.assertIn("use_scripts=False", self.source)
    self.assertIn("load_ui=False", self.source)

def test_blend_extension_and_overwrite_are_enforced(self):
    self.assertIn('suffix=".blend"', self.source)
    self.assertIn("overwrite", self.source)
```

- [ ] **Step 2: Add failing MCP forwarding tests**

```python
def test_save_forwards_explicit_overwrite(self):
    server.save("projects/person.blend", overwrite=True, compress=False)
    self.assertEqual(self.fake.calls[0], ("save", {"path": "projects/person.blend", "overwrite": True, "compress": False}))

def test_open_defaults_to_preserving_dirty_state(self):
    server.open("projects/person.blend")
    self.assertEqual(self.fake.calls[0], ("open", {"path": "projects/person.blend", "discard_unsaved": False}))
```

- [ ] **Step 3: Run focused tests and verify they fail**

Run: `./.venv/bin/python -m unittest tests.test_addon_contract tests.test_server_tools -v`

Expected: FAIL because the handlers and registrations are absent.

- [ ] **Step 4: Implement save with containment and overwrite protection**

Resolve the target with `.blend`, `must_exist=False`; reject an existing target unless `overwrite` is true; create the already-validated parent; call:

```python
result = bpy.ops.wm.save_as_mainfile(
    filepath=str(target),
    check_existing=False,
    compress=bool(compress),
)
if "FINISHED" not in result or not target.is_file():
    raise CommandError("BLENDER_ERROR", "Blender did not create the requested blend file")
return {"success": True, "path": str(target), "size_bytes": target.stat().st_size, "is_dirty": bool(bpy.data.is_dirty)}
```

- [ ] **Step 5: Implement open with dirty-state and script protections**

Resolve an existing `.blend`; test `bpy.data.is_dirty` before calling Blender; raise `CommandError("DIRTY_FILE", ...)` unless discard is explicit. Call `bpy.ops.wm.open_mainfile(filepath=str(target), load_ui=False, use_scripts=False)`, require `FINISHED`, then return current file, active scene, object count, and dirty state. Do not auto-save and do not retry.

- [ ] **Step 6: Register thin MCP adapters and update exact tool-name assertions**

Both adapters validate only simple Python types and defer canonical path validation to Blender. The registered-name test now expects exactly `get_status`, `execute_python`, `get_scene_info`, `save`, and `open`.

- [ ] **Step 7: Run focused tests and syntax verification**

Run: `./.venv/bin/python -m unittest tests.test_addon_contract tests.test_server_tools -v`

Run: `python3 -m py_compile addon.py`

Expected: PASS.

- [ ] **Step 8: Commit Task 5**

```bash
git add addon.py src/blender_mcp/server.py tests/test_addon_contract.py tests/test_server_tools.py
git commit -m "feat: add protected Blender save and open tools"
```

---

### Task 6: Camera Render and MCP Image Return

**Files:**
- Modify: `addon.py`
- Modify: `src/blender_mcp/server.py`
- Modify: `src/blender_mcp/tool_results.py`
- Modify: `tests/test_addon_contract.py`
- Modify: `tests/test_server_tools.py`
- Modify: `tests/test_tool_results.py`

**Interfaces:**
- Consumes: managed-path policy and `image_result`.
- Produces: add-on `render(camera=None, width=1024, height=1024, engine=None, output_path=None) -> dict`.
- Produces: MCP `render(...) -> CallToolResult` with JSON `TextContent`, structured metadata, and PNG `ImageContent`.

- [ ] **Step 1: Add failing render contract tests**

```python
def test_render_handler_is_registered_and_restores_settings(self):
    self.assertIn('"render": self.render', self.source)
    for token in ("bpy.ops.render.render(write_still=True)", "finally:", "resolution_x", "resolution_y", "resolution_percentage", "image_settings.file_format", "scene.camera", "render.engine"):
        self.assertIn(token, self.source)

def test_render_output_is_workspace_png(self):
    self.assertIn('suffix=".png"', self.source)
    self.assertIn('"image_path"', self.source)
    self.assertIn('"workdir"', self.source)
```

```python
def test_render_returns_image_result(self):
    self.fake.response = {"success": True, "image_path": self.png_path, "workdir": self.workdir, "width": 512, "height": 512}
    result = server.render(width=512, height=512)
    self.assertEqual(result.content[1].mimeType, "image/png")
    self.assertEqual(self.fake.calls[0][0], "render")
```

- [ ] **Step 2: Run focused tests and verify missing render behavior fails**

Run: `./.venv/bin/python -m unittest tests.test_addon_contract tests.test_tool_results tests.test_server_tools -v`

Expected: FAIL on missing render handler/tool.

- [ ] **Step 3: Implement render input validation and unique output paths**

Validate dimensions as non-boolean integers in `64..4096`. Resolve the named camera and require type `CAMERA`; otherwise use the scene camera and fail with `NOT_FOUND` when absent. Read valid engine identifiers from Blender RNA and reject unsupported values. When output is omitted, generate `renders/render-YYYYMMDD-HHMMSS-<8 hex>.png`; otherwise resolve a managed `.png` output and create its validated parent.

- [ ] **Step 4: Implement synchronous render with complete restoration**

Snapshot camera, engine, filepath, resolution x/y/percentage, file format, and color mode. Set the requested values, call `bpy.ops.render.render(write_still=True)`, validate `FINISHED` and a non-empty PNG file, and return image path, workdir, width, height, camera, engine, size, and duration. Restore every snapshot value in a `finally` block even when rendering fails.

- [ ] **Step 5: Register the MCP render tool and map success to mixed content**

The adapter forwards only explicit fields and calls `_run_image("render", params)`. `_run_image` requires `image_path` and `workdir` in Blender's result, passes them to `image_result`, and maps connection/command/file validation failures through `error_result`. Update the exact registered-name test to include `render`.

- [ ] **Step 6: Run focused tests and syntax verification**

Run: `./.venv/bin/python -m unittest tests.test_addon_contract tests.test_tool_results tests.test_server_tools -v`

Run: `python3 -m py_compile addon.py`

Expected: PASS.

- [ ] **Step 7: Commit Task 6**

```bash
git add addon.py src/blender_mcp/server.py src/blender_mcp/tool_results.py tests/test_addon_contract.py tests/test_server_tools.py tests/test_tool_results.py
git commit -m "feat: return Blender camera renders through MCP"
```

---

### Task 1: Framed Protocol and Serialized Connection

**Files:**
- Create: `src/blender_mcp/protocol.py`
- Create: `src/blender_mcp/connection.py`
- Create: `tests/__init__.py`
- Create: `tests/test_protocol.py`
- Create: `tests/test_connection.py`
- Modify: `src/blender_mcp/__init__.py`

**Interfaces:**
- Produces: `encode_frame(message: Mapping[str, Any], max_frame_bytes: int = MAX_FRAME_BYTES) -> bytes`
- Produces: `receive_frame(sock: SocketLike, max_frame_bytes: int = MAX_FRAME_BYTES) -> dict[str, Any]`
- Produces: `make_request(command_type: str, params: Mapping[str, Any] | None = None, request_id: str | None = None) -> dict[str, Any]`
- Produces: `validate_response(response: Mapping[str, Any], expected_request_id: str) -> dict[str, Any]`
- Produces: `BlenderConnection.send_command(command_type: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]`
- Produces: `BlenderCommandError(category, error_type, message, traceback_text, retryable, request_id, details)`

- [ ] **Step 1: Write protocol tests that fail before the module exists**

```python
# tests/test_protocol.py
import json
import struct
import unittest

from blender_mcp.protocol import (
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    FrameTooLargeError,
    ProtocolError,
    encode_frame,
    make_request,
    receive_frame,
    validate_response,
)


class ChunkSocket:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def recv(self, size):
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        self.chunks.insert(0, chunk[size:]) if len(chunk) > size else None
        return chunk[:size]


class ProtocolTests(unittest.TestCase):
    def test_fragmented_frame_round_trip(self):
        message = {"protocol_version": 1, "request_id": "abc", "success": True, "result": {"x": 1}}
        frame = encode_frame(message)
        sock = ChunkSocket([frame[:1], frame[1:4], frame[4:9], frame[9:]])
        self.assertEqual(receive_frame(sock), message)

    def test_rejects_oversized_header_before_payload_read(self):
        sock = ChunkSocket([struct.pack("!I", MAX_FRAME_BYTES + 1)])
        with self.assertRaises(FrameTooLargeError):
            receive_frame(sock)

    def test_rejects_invalid_json(self):
        payload = b"not-json"
        with self.assertRaises(ProtocolError):
            receive_frame(ChunkSocket([struct.pack("!I", len(payload)), payload]))

    def test_request_has_uuid_and_protocol_version(self):
        request = make_request("get_status", {}, request_id="known")
        self.assertEqual(request, {"protocol_version": PROTOCOL_VERSION, "request_id": "known", "type": "get_status", "params": {}})

    def test_response_request_id_must_match(self):
        with self.assertRaises(ProtocolError):
            validate_response({"protocol_version": 1, "request_id": "wrong", "success": True, "result": {}}, "expected")
```

- [ ] **Step 2: Write connection tests for serialization, remote errors, and no replay**

```python
# tests/test_connection.py
import unittest
from unittest.mock import Mock

from blender_mcp.connection import BlenderCommandError, BlenderConnection
from blender_mcp.protocol import encode_frame


class ScriptedSocket:
    def __init__(self, response_frame):
        self.remaining = response_frame
        self.sent = []
        self.closed = False

    def settimeout(self, timeout): self.timeout = timeout
    def connect(self, address): self.address = address
    def sendall(self, data): self.sent.append(data)
    def recv(self, size):
        chunk, self.remaining = self.remaining[:size], self.remaining[size:]
        return chunk
    def close(self): self.closed = True


class ConnectionTests(unittest.TestCase):
    def test_success_returns_result(self):
        response = encode_frame({"protocol_version": 1, "request_id": "fixed", "success": True, "result": {"ready": True}})
        sock = ScriptedSocket(response)
        connection = BlenderConnection("localhost", 9876, socket_factory=lambda *args: sock, request_id_factory=lambda: "fixed")
        self.assertEqual(connection.send_command("get_status"), {"ready": True})
        self.assertEqual(len(sock.sent), 1)

    def test_remote_error_preserves_diagnostics(self):
        response = encode_frame({"protocol_version": 1, "request_id": "fixed", "success": False, "error": {"category": "BLENDER_ERROR", "type": "KeyError", "message": "missing", "traceback": "trace", "retryable": False, "details": {"stdout": "before"}}})
        sock = ScriptedSocket(response)
        connection = BlenderConnection("localhost", 9876, socket_factory=lambda *args: sock, request_id_factory=lambda: "fixed")
        with self.assertRaises(BlenderCommandError) as caught:
            connection.send_command("execute_python", {"code": "bad"})
        self.assertEqual(caught.exception.error_type, "KeyError")
        self.assertEqual(caught.exception.details["stdout"], "before")

    def test_broken_receive_does_not_replay_mutation(self):
        sock = ScriptedSocket(b"")
        connection = BlenderConnection("localhost", 9876, socket_factory=lambda *args: sock, request_id_factory=lambda: "fixed")
        with self.assertRaises(Exception):
            connection.send_command("save", {"path": "projects/a.blend"})
        self.assertEqual(len(sock.sent), 1)
```

- [ ] **Step 3: Run the focused tests and verify the expected import failures**

Run: `PYTHONPATH=src python3 -m unittest tests.test_protocol tests.test_connection -v`

Expected: FAIL with `ModuleNotFoundError` for `blender_mcp.protocol` or `blender_mcp.connection`.

- [ ] **Step 4: Implement the frame codec and envelope validation**

```python
# src/blender_mcp/protocol.py
import json
import struct
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 8 * 1024 * 1024
HEADER = struct.Struct("!I")

class SocketLike(Protocol):
    def recv(self, size: int) -> bytes: ...

class ProtocolError(RuntimeError): pass
class FrameTooLargeError(ProtocolError): pass

def _receive_exact(sock: SocketLike, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise ProtocolError("connection closed before a complete frame was received")
        chunks.extend(chunk)
    return bytes(chunks)

def encode_frame(message: Mapping[str, Any], max_frame_bytes: int = MAX_FRAME_BYTES) -> bytes:
    payload = json.dumps(dict(message), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > max_frame_bytes:
        raise FrameTooLargeError(f"frame is {len(payload)} bytes; maximum is {max_frame_bytes}")
    return HEADER.pack(len(payload)) + payload

def receive_frame(sock: SocketLike, max_frame_bytes: int = MAX_FRAME_BYTES) -> dict[str, Any]:
    length = HEADER.unpack(_receive_exact(sock, HEADER.size))[0]
    if length > max_frame_bytes:
        raise FrameTooLargeError(f"declared frame is {length} bytes; maximum is {max_frame_bytes}")
    try:
        value = json.loads(_receive_exact(sock, length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"invalid frame payload: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError("frame payload must be a JSON object")
    return value

def make_request(command_type, params=None, request_id=None):
    return {"protocol_version": PROTOCOL_VERSION, "request_id": request_id or str(uuid.uuid4()), "type": command_type, "params": dict(params or {})}

def validate_response(response, expected_request_id):
    if response.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol version")
    if response.get("request_id") != expected_request_id:
        raise ProtocolError("response request_id does not match request")
    if not isinstance(response.get("success"), bool):
        raise ProtocolError("response success must be boolean")
    return dict(response)
```

- [ ] **Step 5: Implement the connection without automatic command replay**

Implement `BlenderConnection` as a dataclass with `host`, `port`, `timeout_seconds`, injectable `socket_factory`, injectable `request_id_factory`, an optional socket, and a `threading.Lock`. `send_command` must hold the lock across `sendall(encode_frame(request))` and `receive_frame`, validate the request ID, return `result`, translate a failure envelope into `BlenderCommandError`, and close/invalidate the socket on transport or protocol failure. It must log only command type, request ID, payload byte count, duration, and outcome.

```python
def send_command(self, command_type, params=None):
    with self._lock:
        request_id = self.request_id_factory()
        request = make_request(command_type, params, request_id)
        try:
            sock = self._ensure_connected()
            sock.sendall(encode_frame(request))
            response = validate_response(receive_frame(sock), request_id)
            if not response["success"]:
                raise BlenderCommandError.from_envelope(response)
            return response.get("result", {})
        except BlenderCommandError:
            raise
        except Exception:
            self.disconnect()
            raise
```

- [ ] **Step 6: Export the new connection API and run focused tests**

Modify `src/blender_mcp/__init__.py` to export `BlenderConnection` from `.connection`. Do not import `server` at package import time.

Run: `PYTHONPATH=src python3 -m unittest tests.test_protocol tests.test_connection -v`

Expected: all tests PASS.

- [ ] **Step 7: Commit Task 1**

```bash
git add src/blender_mcp/protocol.py src/blender_mcp/connection.py src/blender_mcp/__init__.py tests/__init__.py tests/test_protocol.py tests/test_connection.py
git commit -m "feat: add framed Blender command transport"
```

---

### Task 2: Blender Add-on Foundation and Workspace Policy

**Files:**
- Replace: `addon.py`
- Create: `tests/test_addon_contract.py`

**Interfaces:**
- Consumes: protocol constants `PROTOCOL_VERSION=1`, `MAX_FRAME_BYTES=8388608`, and four-byte `!I` framing; the add-on embeds the small codec because Blender installs `addon.py` independently.
- Produces: `resolve_managed_path(raw_path: str, *, workdir: Path, suffix: str | None, must_exist: bool) -> Path`
- Produces: `BlenderMCPServer.start()`, `stop()`, `_client_loop(client)`, and `_execute_request(request)`.
- Produces: add-on preference `workdir: StringProperty` and scene preference `blendermcp_port`.

- [ ] **Step 1: Write static contract and pure-helper tests before replacing the add-on**

```python
# tests/test_addon_contract.py
import ast
import pathlib
import unittest

ADDON = pathlib.Path(__file__).parents[1] / "addon.py"

class AddonContractTests(unittest.TestCase):
    def setUp(self):
        self.source = ADDON.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_addon_targets_blender_5(self):
        self.assertIn('"blender": (5, 0, 0)', self.source)

    def test_addon_uses_length_prefixed_frames(self):
        self.assertIn('struct.Struct("!I")', self.source)
        self.assertIn("MAX_FRAME_BYTES = 8 * 1024 * 1024", self.source)
        self.assertNotIn("json.loads(buffer.decode", self.source)

    def test_workspace_contract_is_present(self):
        for token in ("BLENDER_MCP_WORKDIR", "resolve_managed_path", "input", "assets", "projects", "renders", "exports", "scripts"):
            self.assertIn(token, self.source)

    def test_no_remote_telemetry_or_provider_imports(self):
        self.assertNotIn("telemetry", self.source.lower())
        self.assertNotIn("import requests", self.source)
```

- [ ] **Step 2: Run the add-on contract tests and verify they fail against the legacy file**

Run: `python3 -m unittest tests.test_addon_contract -v`

Expected: FAIL because the legacy add-on targets Blender 3.0, uses unframed JSON, and contains telemetry/provider code.

- [ ] **Step 3: Replace the legacy add-on with the core Blender 5.x shell**

The replacement must contain only standard-library imports plus Blender-bundled `bpy`, `bmesh`, `mathutils`, `gpu`, and `numpy`. Define constants for protocol version, frame/code/output limits, required subdirectories, and add-on version. Keep the socket listener on a daemon thread and dispatch Blender work with `bpy.app.timers.register`.

```python
bl_info = {
    "name": "Blender MCP Core",
    "author": "BlenderMCP contributors",
    "version": (2, 0, 0),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > BlenderMCP",
    "description": "Local MCP bridge for Blender inspection, Python, rendering, and persistence",
    "category": "Interface",
}

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_CODE_BYTES = 1024 * 1024
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
HEADER = struct.Struct("!I")
WORKDIR_ENV = "BLENDER_MCP_WORKDIR"
WORKDIR_SUBDIRS = ("input", "assets", "projects", "renders", "exports", "scripts")
```

`_client_loop` must read one complete frame, validate the request envelope, schedule a closure on the main thread, and only then send a success or error envelope carrying the original request ID. `sendall` must be protected so only one scheduled callback writes a response at a time.

- [ ] **Step 4: Implement canonical workspace resolution before any directory creation**

```python
def resolve_managed_path(raw_path, *, workdir, suffix=None, must_exist=False):
    root = Path(workdir).expanduser().resolve()
    candidate = Path(raw_path).expanduser()
    candidate = candidate if candidate.is_absolute() else root / candidate
    if suffix and candidate.suffix.lower() != suffix.lower():
        raise CommandError("VALIDATION_ERROR", f"path must end with {suffix}")
    if must_exist:
        resolved = candidate.resolve(strict=True)
    else:
        existing_parent = candidate.parent
        missing_parts = []
        while not existing_parent.exists():
            missing_parts.append(existing_parent.name)
            if existing_parent.parent == existing_parent:
                raise CommandError("VALIDATION_ERROR", "output path has no existing ancestor")
            existing_parent = existing_parent.parent
        resolved_parent = existing_parent.resolve(strict=True)
        for part in reversed(missing_parts):
            resolved_parent /= part
        resolved = resolved_parent / candidate.name
    if os.path.commonpath((os.path.normcase(str(root)), os.path.normcase(str(resolved)))) != os.path.normcase(str(root)):
        raise CommandError("PATH_OUTSIDE_WORKDIR", "path is outside the Blender MCP workspace")
    return resolved
```

Resolve the workspace in this order: non-empty `BLENDER_MCP_WORKDIR`, non-empty add-on `workdir` preference, then `Path.home() / "BlenderMCP"`. Create the root and six required subdirectories only after the root itself resolves successfully.

- [ ] **Step 5: Add minimal UI and lifecycle registration**

The Preferences panel exposes `workdir` and explains that managed file tools stay inside it while `execute_python` is unrestricted. The 3D View sidebar exposes the port, resolved workspace, server state, and Start/Stop buttons. Auto-start remains enabled by default. Remove telemetry consent, API credentials, provider toggles, feedback links, and terms-opening operators.

- [ ] **Step 6: Run parsing and contract verification**

Run: `python3 -m unittest tests.test_addon_contract -v`

Run: `python3 -m py_compile addon.py`

Expected: all tests PASS and compilation exits 0 without importing Blender.

- [ ] **Step 7: Commit Task 2**

```bash
git add addon.py tests/test_addon_contract.py
git commit -m "refactor: create Blender 5 core addon shell"
```

---

### Task 3: Status, Python Execution, and Scene Inspection Handlers

**Files:**
- Modify: `addon.py`
- Modify: `tests/test_addon_contract.py`

**Interfaces:**
- Consumes: add-on dispatcher and workspace resolver from Task 2.
- Produces: command handlers `get_status() -> dict`, `execute_python(code: str) -> dict`, and `get_scene_info(include_objects=True, include_materials=True, include_collections=True, include_cameras=True, max_objects=200) -> dict`.
- Produces: dispatch map entries named exactly `get_status`, `execute_python`, and `get_scene_info`.

- [ ] **Step 1: Add failing source-contract tests for all three handlers**

```python
def test_core_read_and_execute_handlers_are_registered(self):
    for token in ('"get_status": self.get_status', '"execute_python": self.execute_python', '"get_scene_info": self.get_scene_info'):
        self.assertIn(token, self.source)

def test_python_diagnostics_contract(self):
    for token in ("redirect_stdout", "redirect_stderr", "traceback.format_exc", "stdout_truncated", "stderr_truncated", "MAX_CODE_BYTES"):
        self.assertIn(token, self.source)

def test_scene_info_is_bounded(self):
    self.assertIn("max_objects=200", self.source)
    self.assertIn('"truncated"', self.source)
```

- [ ] **Step 2: Run the focused test and verify missing handlers fail**

Run: `python3 -m unittest tests.test_addon_contract -v`

Expected: FAIL on handler registration and diagnostic tokens.

- [ ] **Step 3: Implement status with runtime capabilities**

`get_status` returns `ready`, `blender_version`, `addon_version`, `protocol_version`, `current_file`, `is_dirty`, `scene`, `workdir`, `render_engines`, `image_formats=["PNG"]`, `max_code_bytes`, and `max_frame_bytes`. Read render-engine identifiers from `bpy.context.scene.render.bl_rna.properties["engine"].enum_items` rather than hard-coding EEVEE names.

- [ ] **Step 4: Implement unrestricted Python execution with bounded capture**

Compile with filename `"<blender-mcp>"`, execute with a namespace containing `__name__`, `bpy`, `bmesh`, and `mathutils`, and capture both streams. Use a UTF-8 byte truncation helper that returns `(text, truncated)` without producing invalid UTF-8. Convert `result` by attempting `json.dumps`; if serialization fails, return `repr(result)[:16384]` and `result_serialized_as="repr"`.

On an exception, raise `CommandError` with category `BLENDER_ERROR`, Python exception type, full `traceback.format_exc()`, and details containing partial stdout/stderr plus their truncation flags. Do not include the source text.

- [ ] **Step 5: Implement bounded scene summaries**

Validate the four include flags as booleans and `max_objects` in `1..2000`. Include scene metadata, active/selected object names, active camera, total counts, and requested lists. Each object summary includes collections, visible/render-hidden flags, location/rotation/scale as float triples, and mesh counts for mesh objects. Sort lists by Blender data-block name for deterministic output and set `truncated` when total objects exceeds `max_objects`.

- [ ] **Step 6: Run add-on contract and syntax checks**

Run: `python3 -m unittest tests.test_addon_contract -v`

Run: `python3 -m py_compile addon.py`

Expected: PASS.

- [ ] **Step 7: Commit Task 3**

```bash
git add addon.py tests/test_addon_contract.py
git commit -m "feat: add Blender status execution and scene handlers"
```

---

### Task 7: Viewport Orientation, Shading, and Screenshot Return

**Files:**
- Modify: `addon.py`
- Modify: `src/blender_mcp/server.py`
- Modify: `tests/test_addon_contract.py`
- Modify: `tests/test_server_tools.py`

**Interfaces:**
- Consumes: managed PNG paths, add-on main-thread dispatch, and MCP `_run_image`.
- Produces: add-on `viewport_screenshot(view="CURRENT", mode="MATERIAL", width=1024, height=1024, output_path=None) -> dict`.
- Produces: MCP `viewport_screenshot(...) -> CallToolResult` with JSON metadata and PNG content.

- [ ] **Step 1: Add failing viewport contracts**

```python
def test_viewport_handler_supports_required_views_and_modes(self):
    self.assertIn('"viewport_screenshot": self.viewport_screenshot', self.source)
    for view in ("CURRENT", "FRONT", "BACK", "LEFT", "RIGHT", "TOP", "BOTTOM", "CAMERA"):
        self.assertIn(f'"{view}"', self.source)
    for mode in ("WIREFRAME", "SOLID", "MATERIAL", "RENDERED"):
        self.assertIn(f'"{mode}"', self.source)

def test_viewport_prefers_gpu_offscreen_and_restores_state(self):
    for token in ("gpu.types.GPUOffScreen", "draw_view3d", "texture_color.read", "view_matrix.copy", "space.shading.type", "finally:"):
        self.assertIn(token, self.source)
```

```python
def test_viewport_tool_forwards_and_returns_png(self):
    self.fake.response = {"success": True, "image_path": self.png_path, "workdir": self.workdir, "view": "FRONT", "mode": "SOLID"}
    result = server.viewport_screenshot(view="FRONT", mode="SOLID", width=800, height=600)
    self.assertEqual(result.content[1].mimeType, "image/png")
    self.assertEqual(self.fake.calls[0], ("viewport_screenshot", {"view": "FRONT", "mode": "SOLID", "width": 800, "height": 600, "output_path": None}))
```

- [ ] **Step 2: Run focused tests and verify the viewport tool is absent**

Run: `./.venv/bin/python -m unittest tests.test_addon_contract tests.test_server_tools -v`

Expected: FAIL on viewport handler and tool assertions.

- [ ] **Step 3: Locate a valid Blender 3D viewport and snapshot its state**

Search every `bpy.context.window_manager.windows`, then each window screen area, for the first `VIEW_3D` area with a `WINDOW` region. Return `(window, screen, area, region, space, region_3d)`. If none exists, raise `CommandError("BLENDER_ERROR", "No 3D viewport is available; run Blender with its GUI")`.

Snapshot `view_matrix.copy()` for drawing/diagnostics, plus the writable state `view_distance`, `view_location.copy()`, `view_rotation.copy()`, `view_perspective`, `view_camera_offset.copy()`, `view_camera_zoom`, and `space.shading.type`. Restore the writable state in `finally` and call `region_3d.update()`; do not assign the derived view matrix directly. Temporarily disable smooth-view animation and restore that preference too.

- [ ] **Step 4: Apply requested view and shading through a context override**

Validate enum strings before mutation. Under `bpy.context.temp_override(window=window, screen=screen, area=area, region=region, space_data=space)`:

- leave orientation unchanged for `CURRENT`;
- call `bpy.ops.view3d.view_camera()` for `CAMERA` after verifying a scene camera;
- call `bpy.ops.view3d.view_axis(type=view, align_active=False, relative=False)` for the six axis views;
- set `space.shading.type = mode` and update the view layer.

Do not alter object transforms, selection, active camera, or scene render settings.

- [ ] **Step 5: Draw and save an exact-size off-screen PNG**

Create `gpu.types.GPUOffScreen(width, height, format="RGBA8")`, call `draw_view3d` with scene, view layer, space, region, and copied updated view/projection matrices, then read `texture_color`. Convert the UBYTE buffer to normalized float pixels using Blender's bundled NumPy, save through a temporary Blender image data-block in PNG format, and remove the image data-block. Always free the GPU off-screen object in `finally`.

If GPU drawing fails, capture the error text and attempt `bpy.ops.screen.screenshot_area(filepath=..., check_existing=False)` only inside the valid viewport override. Rescale that area capture to the requested dimensions before saving. If fallback also fails, raise a `BLENDER_ERROR` that reports both failures. Return `method="gpu_offscreen"` or `method="screen_fallback"` with path, workdir, requested view/mode, dimensions, size, and duration.

- [ ] **Step 6: Register the seventh MCP tool and freeze the public tool list**

Forward all five arguments, including `output_path=None`, through `_run_image`. Update the registration test so the exact sorted names are:

```python
[
    "execute_python",
    "get_scene_info",
    "get_status",
    "open",
    "render",
    "save",
    "viewport_screenshot",
]
```

- [ ] **Step 7: Run focused and aggregate automated tests**

Run: `./.venv/bin/python -m unittest discover -s tests -v`

Run: `python3 -m py_compile addon.py src/blender_mcp/*.py`

Expected: PASS.

- [ ] **Step 8: Commit Task 7**

```bash
git add addon.py src/blender_mcp/server.py tests/test_addon_contract.py tests/test_server_tools.py
git commit -m "feat: return oriented Blender viewport screenshots"
```

---

### Task 8: Documentation, Privacy Audit, and Windows Blender Acceptance Guide

**Files:**
- Replace: `README.md`
- Create: `docs/windows-blender-5-smoke-test.md`
- Modify: `TERMS_AND_CONDITIONS.md`
- Delete: `test_set_texture_version_guard.py`
- Modify: `tests/test_addon_contract.py`
- Modify: `tests/test_server_tools.py`

**Interfaces:**
- Consumes: all seven tools and their exact contracts.
- Produces: reproducible install, configuration, safety, and acceptance instructions for Windows + Blender 5.x.

- [ ] **Step 1: Add final privacy and obsolete-feature regression checks**

```python
# add to tests/test_server_tools.py
def test_core_server_has_no_remote_telemetry(self):
    source = pathlib.Path(server.__file__).read_text(encoding="utf-8")
    for forbidden in ("telemetry", "supabase", "upload_screenshot", "prompt_text", "user_prompt"):
        self.assertNotIn(forbidden, source.lower())

# add to tests/test_addon_contract.py
def test_addon_has_no_optional_network_providers(self):
    for forbidden in ("polyhaven", "sketchfab", "hyper3d", "hunyuan", "requests.get", "requests.post"):
        self.assertNotIn(forbidden, self.source.lower())
```

- [ ] **Step 2: Run the privacy tests and verify documentation still contradicts the new build**

Run: `./.venv/bin/python -m unittest discover -s tests -v`

Run: `rg -n "telemetry|Poly Haven|Sketchfab|Hyper3D|Hunyuan|Blender 3\.0" README.md TERMS_AND_CONDITIONS.md`

Expected before documentation edits: tests PASS, while `rg` finds stale public documentation and terms.

- [ ] **Step 3: Replace README with the focused core product guide**

Document:

- prerequisites: Windows, Blender 5.x, Python 3.10+, and uv;
- installing `addon.py` from Blender's Add-ons preferences;
- setting the workdir preference or `BLENDER_MCP_WORKDIR`;
- the same `uvx --python 3.11 blender-mcp` MCP client configuration pattern;
- starting/stopping the add-on socket server;
- the seven tools with argument and return examples;
- the inspect/execute/render/view loop;
- managed-path restrictions versus unrestricted `execute_python`;
- dirty-file and overwrite protections;
- no telemetry or remote upload;
- 1 MiB source, 8 MiB frame, 2 MiB stream, 32 MiB PNG, and 600-second defaults;
- same-machine-only image return in this release;
- troubleshooting for offline Blender, mismatched protocol, unavailable viewport, render timeout, and invalid output paths;
- a link to the Windows Blender 5.x smoke-test guide.

Do not advertise removed providers, prompts, compatibility aliases, or Blender 3.x support.

- [ ] **Step 4: Write an executable manual acceptance sequence**

`docs/windows-blender-5-smoke-test.md` must include exact MCP calls and expected fields for all ten acceptance checks. Use `projects/person.blend` and `renders/*.png` beneath a temporary test workspace. Include this diagnostic script:

```python
import sys
print("stdout-ok")
print("stderr-ok", file=sys.stderr)
result = {"blender": bpy.app.version_string, "objects": len(bpy.data.objects)}
```

Include generation of a source string larger than 256 KiB without printing it, a deliberate `bpy.data.objects["__missing__"]` failure, creation of a camera/light/cube fixture, dirty-state rejection, explicit discard, render-setting restoration checks, FRONT/SOLID and CURRENT/MATERIAL screenshots, MCP client confirmation that both image calls contain `image/png`, and `..\outside.blend` rejection.

- [ ] **Step 5: Align terms and remove the obsolete legacy test**

Remove telemetry collection, data-license, deletion-request, and consent language from `TERMS_AND_CONDITIONS.md`. Replace the data section with the explicit statement that core tools perform no telemetry or upload, while arbitrary scripts run through `execute_python` can access the network or filesystem under the user's own authority. Preserve the MIT license and general third-party service disclaimer only where still applicable.

Delete `test_set_texture_version_guard.py` because the rewritten core add-on no longer exposes `set_texture`; its continued presence would test a removed, out-of-scope provider feature.

- [ ] **Step 6: Run the full automated and privacy verification suite**

```bash
./.venv/bin/python -m unittest discover -s tests -v
./.venv/bin/python -m compileall -q src addon.py
./.venv/bin/python -m pip check
git diff --check
```

Run: `rg -n "telemetry|supabase|upload_screenshot|prompt_text|user_prompt" src addon.py`

Expected: `rg` returns no matches. Confirm separately that README and terms state
that this build has no telemetry; deliberate negative privacy wording is excluded
from the zero-match command.

- [ ] **Step 7: Verify packaging and MCP tool discovery**

Build and install in a clean temporary virtual environment, then start an in-process MCP client session over stdio and assert the server advertises exactly seven tools. The discovery test does not require Blender to be online; calling `get_status` while offline must return an MCP error result with category `CONNECTION_ERROR`, not crash server startup.

Run:

```bash
/tmp/blender-mcp-uv/bin/uv build
python3 -m venv /tmp/blender-mcp-wheel-test
/tmp/blender-mcp-wheel-test/bin/pip install dist/blender_mcp-2.0.0-py3-none-any.whl
/tmp/blender-mcp-wheel-test/bin/python -c "import blender_mcp.server as server; print(server.mcp.name)"
```

Expected: wheel builds and installs. The automated MCP discovery unittest passes with the exact seven names.

- [ ] **Step 8: Record the real-runtime verification boundary**

Add a final README status note: automated tests are runnable on any supported Python host; the GPU, `bpy.ops.render`, save/open, and viewport behavior must pass `docs/windows-blender-5-smoke-test.md` on Windows with Blender 5.x before production use. Do not mark that smoke test as passed unless its observed results are supplied.

- [ ] **Step 9: Commit Task 8**

```bash
git add README.md TERMS_AND_CONDITIONS.md docs/windows-blender-5-smoke-test.md tests src addon.py pyproject.toml uv.lock
git add -u test_set_texture_version_guard.py
git commit -m "docs: add Blender 5 core setup and acceptance guide"
```

---

After each task, inspect `git status --short`, review the staged diff, run the task's focused tests, and commit only that task's files. Stop a batch on the first failing verification and diagnose it before continuing.
