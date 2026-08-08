# Blender MCP Core 2.0

Blender MCP Core provides a focused local iteration loop for Blender:

```text
inspect scene -> execute bpy -> render or capture viewport -> inspect image -> adjust -> repeat
```

This branch intentionally exposes seven core MCP tools. It targets Windows and
Blender 5.x and does not preserve the legacy provider or compatibility APIs.

## Requirements

- Windows 10 or 11
- Blender 5.x running with its graphical interface
- CPython 3.11 for the MCP server
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

The package metadata retains a Python 3.10 installation floor, but this release's
Windows production configuration and real-runtime acceptance target CPython
3.11. Other interpreter lines have not completed the Windows stdio + Blender
smoke test.

Install uv from PowerShell, then restart any desktop MCP client so it receives
the updated `PATH`:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
where.exe uv
```

## Install the Blender add-on

1. In Blender 5, open **Edit > Preferences > Add-ons**.
2. Choose **Install from Disk** and select this repository's `addon.py`.
3. Enable **Blender MCP Core**.
4. In the add-on preferences, set **Workspace** to a dedicated directory such
   as `D:\BlenderMCP`.
5. In a 3D View, press `N`, open **BlenderMCP**, and confirm the server is
   listening on `localhost:9876`. Auto-start is enabled by default; the panel
   also provides explicit Start and Stop buttons.

The workspace is selected in this order:

1. `BLENDER_MCP_WORKDIR` in the Blender process;
2. the add-on's Workspace preference;
3. `%USERPROFILE%\BlenderMCP`.

To use the environment variable, set it before starting Blender and then fully
restart Blender:

```powershell
[Environment]::SetEnvironmentVariable(
  "BLENDER_MCP_WORKDIR",
  "D:\BlenderMCP",
  "User"
)
```

The add-on creates `input`, `assets`, `projects`, `renders`, `exports`, and
`scripts` beneath the workspace.

## Run this checkout as an MCP server

Use the current checkout during development. A bare `uvx blender-mcp` may resolve
an older published release and should not be used to test this source tree.
Replace both paths below with the output of `where.exe uv` and your clone path:

```json
{
  "mcpServers": {
    "blender": {
      "command": "C:\\Users\\YOU\\.local\\bin\\uv.exe",
      "args": [
        "--directory",
        "C:\\Code\\blender-mcp",
        "run",
        "--python",
        "3.11",
        "blender-mcp"
      ],
      "env": {
        "BLENDER_PORT": "9876",
        "BLENDER_MCP_TIMEOUT_SECONDS": "600"
      }
    }
  }
}
```

Alternatively, build a wheel and run that exact artifact:

```powershell
cd C:\Code\blender-mcp
uv build
uvx --python 3.11 --from .\dist\blender_mcp-2.0.0-py3-none-any.whl blender-mcp
```

For a wheel-based desktop configuration, use the absolute `uvx.exe` path and
pass `--python`, `3.11`, `--from`, the absolute wheel path, then `blender-mcp`.

## Core tools

| Tool | Main arguments | Result |
|---|---|---|
| `get_status` | none | Online/readiness state, versions, current file, workspace, engines, and limits |
| `execute_python` | `code` | Separate stdout/stderr, JSON result or bounded repr, duration, and full normal-size traceback on failure |
| `get_scene_info` | include flags, `max_objects` | Deterministic scene, object, transform, mesh, material, collection, and camera summary |
| `save` | `path`, `overwrite=false`, `compress=true` | Canonical `.blend` path, size, and dirty state |
| `open` | `path`, `discard_unsaved=false` | Newly opened file and scene summary |
| `render` | camera, dimensions, engine, output path | Metadata plus inline `image/png` content |
| `viewport_screenshot` | view, mode, dimensions, output path | Metadata plus inline `image/png` content |

`viewport_screenshot.view` accepts `CURRENT`, `FRONT`, `BACK`, `LEFT`, `RIGHT`,
`TOP`, `BOTTOM`, or `CAMERA`. Its mode accepts `WIREFRAME`, `SOLID`, `MATERIAL`,
or `RENDERED`. Image dimensions must be from 64 through 4096.

A typical agent loop is:

1. Call `get_status` and `get_scene_info`.
2. Use `execute_python` to make one bounded scene change and set a `result`
   variable with useful structured metadata.
3. Call `render` or `viewport_screenshot` and inspect the returned image block.
4. Adjust with another `execute_python` call and repeat.
5. Call `save` to a path such as `projects/person.blend`.

## File and execution safety

Managed file tools (`save`, `open`, `render`, and `viewport_screenshot`) only
accept canonical paths inside the configured workspace. Parent traversal,
cross-drive paths, escaping links, wrong extensions, and missing inputs are
rejected. `save` will not replace an existing file unless `overwrite=true`.
`open` will not discard dirty work unless `discard_unsaved=true`, and it loads
files with saved UI state and embedded scripts disabled.

`execute_python` is deliberately unrestricted. It runs with Blender's user
permissions and can read/write files, access the network, start processes, or
modify any Blender data. Only send trusted code.

Workspace containment prevents ordinary traversal and already-present escaping
links; it is not an operating-system sandbox against a malicious same-user
process racing Windows reparse points. Run Blender and the MCP client only with
trusted local software.

The Blender socket listens on localhost without authentication. Any process on
the same Windows account or machine that can reach port 9876 may request
unrestricted Python execution. Do not expose or forward this port to a network.
The MCP bridge always connects to `localhost`; this core release does not accept
a remote-host override.

This core build performs no telemetry and no remote prompt, scene, code, or image
upload. Local logs contain command type, request ID, frame size, and timing, but
not Python source. Your MCP client or AI provider may process tool content under
its own terms.

## Limits and defaults

- Python source: 1 MiB, measured as UTF-8 bytes
- Framed Blender protocol message: 8 MiB
- Captured stdout and stderr: 2 MiB each before transport-budget truncation
- JSON-safe Python result: 1 MiB; larger results become a bounded repr
- Scene data-block list: 2,000 items per non-object section
- Returned PNG file: 32 MiB
- Blender command timeout: 600 seconds, configurable with
  `BLENDER_MCP_TIMEOUT_SECONDS`

Image return in this release is same-machine only: the MCP process reads the PNG
path produced by Blender, so both processes must share the same filesystem.

## Troubleshooting

- **Blender is offline / connection refused:** start Blender, enable the add-on,
  and confirm the sidebar says `localhost:9876`. Match `BLENDER_PORT` if changed.
- **Protocol version mismatch:** update both `addon.py` and the MCP server from
  the same checkout, then restart Blender and the desktop client.
- **No 3D viewport is available:** use Blender's GUI, keep a normal non-quad 3D
  View open, and avoid running the add-on in background mode.
- **Viewport GPU failure:** the tool attempts an exact-size screen-area fallback.
  Restore the Blender window, update GPU drivers, and avoid disconnected RDP
  sessions if both methods fail.
- **Render timeout:** reduce samples/resolution or raise
  `BLENDER_MCP_TIMEOUT_SECONDS`; timed-out mutations are never automatically
  replayed.
- **Invalid output path:** use a `.blend` or `.png` path beneath the workspace,
  for example `projects/person.blend` or `renders/front.png`.
- **Desktop client cannot find uv:** use the absolute path from `where.exe uv`
  and fully quit/restart the client after configuration changes.

## Verification status

The automated suite verifies the MCP tool contract with an in-memory MCP client;
the smoke test's `tools/list` step
verifies the real stdio launch path on the target desktop. Actual GPU drawing,
`bpy.ops.render`, save/open, and viewport behavior must also pass the
[Windows + Blender 5.x smoke test](docs/windows-blender-5-smoke-test.md) before
production use. That real-runtime smoke test is not considered passed until its
observed results are recorded on a Windows machine with Blender 5.x.

## License

The source is available under the [MIT License](LICENSE). Blender MCP is an
independent project and is not affiliated with the Blender Foundation.
