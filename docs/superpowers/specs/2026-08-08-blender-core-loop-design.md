# Blender MCP Core Iteration Loop Design

Date: 2026-08-08  
Target: Windows and Blender 5.x  
Scope: the ten mandatory acceptance requirements in the supplied brief

## 1. Goal

Build a dependable local loop for AI-assisted Blender work:

```text
inspect scene -> execute bpy -> render or capture viewport -> inspect image -> adjust -> repeat
```

The first release deliberately focuses on seven core MCP tools. Optional asset
providers, batch multi-view rendering, import/export helpers, reference-image
management, mesh-specific queries, and script-file execution are outside this
batch.

## 2. Decisions and Constraints

- Blender and the MCP server run on the same Windows machine.
- Blender 5.x is the primary runtime target.
- CPython 3.11 is the validated deployment target for the MCP server. Package
  syntax and dependency metadata retain a 3.10 installation floor, but other
  interpreter lines are not considered Windows production-validated.
- Existing public tool signatures do not need to remain compatible.
- Blender's configured workspace is the authority for managed file operations.
- `BLENDER_MCP_WORKDIR` overrides the configured workspace. The default is
  `%USERPROFILE%\BlenderMCP`.
- Managed tools may only read or write inside that workspace.
- `execute_python` intentionally remains unrestricted and may use the complete
  Blender Python environment, including `bpy`, `bmesh`, `mathutils`, installed
  add-ons, and normal Python file access.
- No script, screenshot, render, prompt, or path is uploaded. Local logs contain
  tool names, request IDs, duration, and success state, but not script bodies or
  image data.
- Opening a blend file refuses to discard dirty state unless the caller passes
  `discard_unsaved=true`.
- Embedded scripts in opened blend files are disabled by default.
- A single `execute_python` request accepts up to 1 MiB of UTF-8 source, exceeding
  the 256 KiB acceptance requirement.
- Standard output and standard error are each capped at 2 MiB in the response;
  truncation is explicit so an accidental print loop cannot exceed the frame
  limit.
- The MCP server stays on the maintained 1.x SDK line, pinned to
  `mcp[cli]>=1.28.1,<2`, and uses direct `CallToolResult` values for mixed text
  and image content over stdio.
- The bridge host is fixed to `localhost`; it cannot send tool parameters to a
  caller-selected remote Blender endpoint.

## 3. Architecture

```text
MCP client
  -> MCP tool adapter
  -> serialized request connection
  -> length-prefixed JSON socket
  -> Blender add-on dispatcher
  -> Blender main-thread operation
  -> structured response or workspace image path
  -> MCP text/structured metadata plus ImageContent
```

### 3.1 MCP Tool Adapter

The Python package registers tools, validates public arguments, sends commands,
and converts Blender responses into MCP results. Image-producing tools validate
and read the local PNG after Blender completes, then return both metadata and
image content.

The server package is separated into focused modules:

- `protocol.py`: frame encoding/decoding and protocol models.
- `connection.py`: socket lifecycle, serialization, timeout, and reconnect.
- `image_results.py`: safe PNG loading and mixed MCP content construction.
- `server.py`: MCP registration and thin tool adapters.

### 3.2 Socket Protocol

Each message consists of a four-byte unsigned big-endian payload length followed
by UTF-8 JSON. The maximum frame size is 8 MiB. This removes reliance on partial
JSON parsing and safely handles fragmented or coalesced TCP packets.

Request envelope:

```json
{
  "protocol_version": 1,
  "request_id": "uuid",
  "type": "execute_python",
  "params": {}
}
```

Success envelope:

```json
{
  "protocol_version": 1,
  "request_id": "uuid",
  "success": true,
  "result": {}
}
```

Failure envelope:

```json
{
  "protocol_version": 1,
  "request_id": "uuid",
  "success": false,
  "error": {
    "type": "KeyError",
    "message": "...",
    "traceback": "...",
    "retryable": false
  }
}
```

Only one command is active per Blender connection. Request IDs are still
validated so stale or mismatched responses fail explicitly. Connection failures
invalidate the socket and permit one reconnect on the next request; mutating
commands are never automatically replayed.

The default command timeout is 600 seconds and is configurable through
`BLENDER_MCP_TIMEOUT_SECONDS`. A timeout does not claim that Blender cancelled
the underlying operation, because a synchronous Blender render cannot be safely
interrupted from the socket thread.

### 3.3 Blender Add-on Dispatcher

The socket listener stays on a background thread. It decodes complete frames and
schedules every Blender API operation through `bpy.app.timers` on Blender's main
thread. The worker sends the structured response after the scheduled operation
finishes.

The add-on remains easy to install on Windows. Internally, transport, workspace
policy, operations, and UI/preferences have separate classes or sections with
narrow interfaces. Optional provider handlers may remain in the repository, but
they are outside this release's compatibility and test guarantees.

## 4. Workspace Policy

On initialization, the add-on creates:

```text
BlenderMCP/
  input/
  assets/
  projects/
  renders/
  exports/
  scripts/
```

Managed paths follow these rules:

1. Relative paths resolve beneath the workspace.
2. Absolute paths are accepted only when their canonical destination is beneath
   the workspace.
3. Existing inputs resolve symlinks/junctions before containment is checked.
4. New outputs resolve their nearest existing parent before containment is
   checked.
5. Comparisons use Windows case-insensitive path semantics.
6. Blend tools require a `.blend` suffix; image tools in this batch write PNG.
7. Parent directories are created only after containment validation.

The MCP process treats the work directory reported by Blender as authoritative
and revalidates image paths before reading them.

## 5. MCP Tools

The MCP server is named `blender`; the logical fully qualified names below use
that namespace.

### 5.1 `blender.get_status`

No arguments.

Returns:

- online/ready state;
- Blender, add-on, and protocol versions;
- current blend-file path or `null`;
- dirty state;
- active scene name;
- authoritative workspace path;
- supported render engines and image formats;
- maximum code and frame sizes.

### 5.2 `blender.execute_python`

Arguments:

- `code: string`, required, 1 MiB maximum when UTF-8 encoded.

Execution namespace includes `bpy`, `bmesh`, and `mathutils`. Standard output and
standard error are captured independently. If the script assigns a JSON-safe
value to `result`, that value is returned; other values use a bounded `repr`.

Success result:

```json
{
  "success": true,
  "stdout": "...",
  "stdout_truncated": false,
  "stderr": "...",
  "stderr_truncated": false,
  "result": null,
  "duration_ms": 12
}
```

On failure, partial stdout and stderr are preserved alongside `error.type`,
`error.message`, and the complete Blender-side traceback. The MCP result is
marked as an error while retaining this structured diagnostic content.

### 5.3 `blender.get_scene_info`

Arguments default to:

```json
{
  "include_objects": true,
  "include_materials": true,
  "include_collections": true,
  "include_cameras": true,
  "max_objects": 200
}
```

The result includes scene name, frame, render engine, active object, selected
objects, active camera, object/material/collection counts, and requested lists.
Object summaries contain name, type, collection names, visibility, transform,
and mesh vertex/edge/polygon counts where applicable. The response reports
`truncated=true` and total count when `max_objects` is reached.

### 5.4 `blender.render`

Arguments:

- `camera: string | null`: named camera or current scene camera;
- `width: int = 1024`, `height: int = 1024`, each 64 through 4096;
- `engine: string | null`: current engine when omitted, otherwise validated
  against Blender's runtime enum;
- `output_path: string | null`: defaults to a unique PNG below `renders/`.

The operation temporarily applies camera, engine, resolution, percentage, PNG
format, and output path; it restores previous scene settings in `finally`.
Successful output includes canonical path, dimensions, camera, engine, byte size,
and duration. The MCP response contains a JSON text block and an `image/png`
content block. The PNG remains in the workspace.

### 5.5 `blender.viewport_screenshot`

Arguments:

- `view`: `CURRENT`, `FRONT`, `BACK`, `LEFT`, `RIGHT`, `TOP`, `BOTTOM`, or
  `CAMERA`;
- `mode`: `WIREFRAME`, `SOLID`, `MATERIAL`, or `RENDERED`;
- `width: int = 1024`, `height: int = 1024`, each 64 through 4096;
- optional workspace `output_path`, otherwise a unique PNG under `renders/`.

The implementation prefers Blender GPU off-screen drawing so the Blender window
does not need foreground focus. It temporarily changes the selected 3D viewport
orientation and shading, restores them in `finally`, and falls back to Blender's
viewport screenshot operator only when a valid UI viewport exists. It returns
metadata and MCP image content like `render`.

### 5.6 `blender.save`

Arguments:

- `path: string`, required and ending in `.blend`;
- `overwrite: bool = false`;
- `compress: bool = true`.

The path must be inside the workspace. Existing files are rejected unless
`overwrite=true`. A successful result includes canonical path, byte size, and
dirty state after saving.

### 5.7 `blender.open`

Arguments:

- `path: string`, required and ending in `.blend`;
- `discard_unsaved: bool = false`.

The path must exist inside the workspace. If `bpy.data.is_dirty` is true, the
operation fails unless `discard_unsaved=true`. The file is loaded without its
saved UI and without automatically executing embedded scripts. Success returns
the new file, scene, object count, and dirty state.

## 6. Error Semantics

Errors are never reduced to `success=false` or a formatted prose string.
Blender-side errors always preserve:

- stable category (`VALIDATION_ERROR`, `PATH_OUTSIDE_WORKDIR`, `NOT_FOUND`,
  `DIRTY_FILE`, `BLENDER_ERROR`, `TIMEOUT`, `CONNECTION_ERROR`, or
  `PROTOCOL_ERROR`);
- Python exception type where one exists;
- human-readable message;
- full Blender traceback for execution failures;
- request ID;
- retryability.

The MCP adapter maps failure envelopes to error tool results. It does not expose
script content in logs or error metadata. Expected validation failures omit a
traceback; unexpected Blender and Python failures include one.

## 7. Privacy and Logging

Remote telemetry and screenshot upload paths are removed from the core tool
flow. No network request is made by a core tool. Local logs use one event per
request with request ID, command type, duration, payload byte count, result state,
and error category. They omit prompt text, Python source, stdout/stderr, file
paths, and image bytes.

Optional third-party asset providers may use the network only when their separate
tools are explicitly invoked; they are not part of this first-batch acceptance.

## 8. Testing and Acceptance

### 8.1 Automated tests without Blender

- fragmented and coalesced length-prefixed frames;
- oversized, invalid UTF-8, invalid JSON, and wrong-version frames;
- request ID matching and connection invalidation;
- no automatic replay of mutating commands;
- 256 KiB and 1 MiB source payload boundaries;
- workspace containment, traversal, case, junction/symlink, extension, and
  overwrite checks;
- public argument validation and error mapping;
- mocked scene, save/open, render, and screenshot adapter responses;
- mixed MCP text/image content and PNG validation;
- source parsing and registration checks for the Blender add-on;
- privacy regression checks proving core tools cannot call telemetry upload.

### 8.2 Windows + Blender 5.x acceptance

A documented smoke test verifies:

1. `get_status` reports the running Blender version and workspace.
2. A script larger than 256 KiB executes, captures stdout/stderr, and returns a
   value.
3. A failing script returns exception type, message, and traceback.
4. Scene and object summaries match a known fixture scene.
5. Saving creates a blend file inside `projects/` and overwrite protection works.
6. Opening refuses dirty-state loss, then succeeds with explicit discard.
7. Camera rendering produces a valid visible PNG and restores scene settings.
8. Current and orthographic viewport captures produce visible PNG content.
9. Both image tools deliver MCP `ImageContent`, not only a filesystem path.
10. Managed path traversal and outside-workspace absolute paths are rejected.

Because Blender is not installed in the development workspace, completion claims
distinguish automated verification from the required Windows Blender smoke test.

## 9. Delivery Batches

To keep each change reviewable and avoid long-running transport failures:

1. Protocol framing, structured envelopes, connection behavior, and tests.
2. Workspace policy plus status, Python execution, and scene inspection.
3. Save/open lifecycle tools and dirty-state protection.
4. Render and viewport image-return tools.
5. Windows Blender smoke-test guide, installation updates, full regression run,
   and privacy audit.

Each batch starts with failing tests, implements only its stated scope, and runs
focused verification before the next batch.

## 10. Non-goals for This Release

- batch multi-view rendering;
- import/export wrappers;
- reference-image helpers;
- mesh/shape-key/armature-specific query tools;
- execution of `.py` files by path;
- remote Blender hosts or cross-machine image transfer;
- cancellation of an already-running synchronous Blender operation;
- compatibility aliases for previous MCP tool names.
