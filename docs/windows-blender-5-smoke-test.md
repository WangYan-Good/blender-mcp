# Windows + Blender 5.x 手工验收

本流程验证首批十项硬需求。它会清理当前场景并测试丢弃未保存修改，因此只应在新建的临时 Blender 会话中执行。请先完成 README 的本地源码安装，并记录 Blender 精确版本、Windows 版本、GPU 与执行日期。

## 0. 准备

1. 新建空白 Blender 文件。
2. 在插件偏好中把 Workspace 设为一个全新的目录，例如
   `C:\Temp\BlenderMCP-smoke-20260808`。
3. 确认 3D View 处于普通单视图布局，不是 Quad View；保持 Blender 窗口可见。
4. 在 BlenderMCP 侧栏重启 socket server。
5. 使用 CPython 3.11 启动 README 中指向当前 checkout 或本地 wheel 的 MCP server。
6. 调用 `tools/list`，确认工具名恰好是：
   `execute_python`、`get_scene_info`、`get_status`、`open`、`render`、`save`、`viewport_screenshot`。

下文使用标准 MCP JSON-RPC 请求。实际客户端可以代为生成 `jsonrpc`、`id` 和 `method`；`params.name` 与 `params.arguments` 必须保持一致。

## 1. 在线状态与工作目录

```json
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_status","arguments":{}}}
```

通过条件：调用成功，结构化结果包含：

- `online=true`、`ready=true`；
- `blender_version` 以 `5.` 开头；
- `protocol_version=1`；
- `workdir` 是步骤 0 设置的规范绝对路径；
- `max_code_bytes=1048576`、`max_frame_bytes=8388608`。

## 2. 任意 bpy、stdout、stderr 与结构化结果

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"execute_python","arguments":{"code":"import sys\nprint(\"stdout-ok\")\nprint(\"stderr-ok\", file=sys.stderr)\nresult = {\"blender\": bpy.app.version_string, \"objects\": len(bpy.data.objects)}"}}}
```

通过条件：`stdout` 为 `stdout-ok\n`，`stderr` 为 `stderr-ok\n`，`result.blender` 以 `5.` 开头，`result_serialized_as` 为 `json`。

## 3. 完整错误诊断

```json
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"execute_python","arguments":{"code":"print(\"before-error\")\nbpy.data.objects[\"__missing__\"]"}}}
```

通过条件：MCP 工具结果标记为错误，`error.category=BLENDER_ERROR`，`error.type=KeyError`，`error.traceback` 含 `Traceback (most recent call last)`，且 `error.details.stdout` 含 `before-error`。

## 4. 大于 256 KiB 的脚本

在支持脚本调用 MCP 的客户端中生成参数，不要把大字符串打印到终端或日志：

```python
code = "#" * (300 * 1024) + "\nresult = {'bytes': 300 * 1024, 'executed': True}"
assert len(code.encode("utf-8")) > 256 * 1024
response = call_tool("execute_python", {"code": code})
```

等价的 MCP 调用是：

```json
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"execute_python","arguments":{"code":"<由上面代码生成的 300 KiB 源字符串>"}}}
```

通过条件：`result.executed=true`，`result.bytes=307200`。随后可把长度改为精确 1 MiB 验证成功；1 MiB + 1 UTF-8 字节必须返回 `VALIDATION_ERROR`。

## 5. 创建固定场景并读取

```json
{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"execute_python","arguments":{"code":"import bpy\nfor obj in list(bpy.data.objects):\n    bpy.data.objects.remove(obj, do_unlink=True)\nbpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))\ncube = bpy.context.object\ncube.name = 'MCP_Cube'\nbpy.ops.object.camera_add(location=(6, -6, 4))\ncamera = bpy.context.object\ncamera.name = 'MCP_Camera'\ncamera.data.lens = 50\ncamera.rotation_euler = (cube.location - camera.location).to_track_quat('-Z', 'Y').to_euler()\nbpy.context.scene.camera = camera\nbpy.ops.object.light_add(type='AREA', location=(4, -2, 6))\nlight = bpy.context.object\nlight.name = 'MCP_Key'\nlight.data.energy = 1000\nlight.data.shape = 'DISK'\nlight.data.size = 5\nresult = {'cube': cube.name, 'camera': camera.name, 'light': light.name}"}}}
```

```json
{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"get_scene_info","arguments":{"include_objects":true,"include_materials":true,"include_collections":true,"include_cameras":true,"max_objects":200}}}
```

通过条件：场景结果至少包含 `MCP_Cube`、`MCP_Camera`、`MCP_Key`；`camera=MCP_Camera`；Cube 的 mesh 统计非零；对象列表含变换信息。

## 6. 保存、覆盖保护与脏文件保护

```json
{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"save","arguments":{"path":"projects/person.blend","overwrite":false,"compress":true}}}
```

通过条件：`projects\person.blend` 位于 smoke workspace，文件存在且 `size_bytes>0`。原样再调用一次必须返回 `VALIDATION_ERROR`；改为 `overwrite=true` 才能覆盖。

制造未保存修改：

```json
{"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"execute_python","arguments":{"code":"bpy.context.scene['dirty_probe'] = 'unsaved'\nresult = {'is_dirty': bpy.data.is_dirty}"}}}
```

确认 `result.is_dirty=true`，然后调用：

```json
{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"open","arguments":{"path":"projects/person.blend","discard_unsaved":false}}}
```

通过条件：返回 `DIRTY_FILE`，当前修改未被悄悄丢弃。随后明确批准丢弃：

```json
{"jsonrpc":"2.0","id":10,"method":"tools/call","params":{"name":"open","arguments":{"path":"projects/person.blend","discard_unsaved":true}}}
```

通过条件：成功，`current_file` 指向 `person.blend`，`scene` 非空，`is_dirty=false`。打开操作必须使用当前版本代码中的 `load_ui=False` 与 `use_scripts=False`。

## 7. 指定 Camera 渲染及设置恢复

先记录设置：

```json
{"jsonrpc":"2.0","id":11,"method":"tools/call","params":{"name":"execute_python","arguments":{"code":"s = bpy.context.scene\nr = s.render\nresult = {'camera': s.camera.name if s.camera else None, 'engine': r.engine, 'filepath': r.filepath, 'x': r.resolution_x, 'y': r.resolution_y, 'pct': r.resolution_percentage, 'format': r.image_settings.file_format, 'color': r.image_settings.color_mode}"}}}
```

保存该结果，然后渲染：

```json
{"jsonrpc":"2.0","id":12,"method":"tools/call","params":{"name":"render","arguments":{"camera":"MCP_Camera","width":640,"height":480,"engine":null,"output_path":"renders/camera.png"}}}
```

通过条件：`camera=MCP_Camera`、`width=640`、`height=480`、`size_bytes>0`，`image_path` 位于 workspace。重复 id=11 的检查，所有字段必须与渲染前完全一致。

## 8. Viewport 截图及状态恢复

```json
{"jsonrpc":"2.0","id":13,"method":"tools/call","params":{"name":"viewport_screenshot","arguments":{"view":"FRONT","mode":"SOLID","width":800,"height":600,"output_path":"renders/front-solid.png"}}}
```

```json
{"jsonrpc":"2.0","id":14,"method":"tools/call","params":{"name":"viewport_screenshot","arguments":{"view":"CURRENT","mode":"MATERIAL","width":800,"height":600,"output_path":"renders/current-material.png"}}}
```

通过条件：两次结果均为请求的 `view`、`mode` 与精确尺寸，`method` 为 `gpu_offscreen` 或 `screen_fallback`，文件非空。调用后人工确认原视图方向、透视、camera offset/zoom 与 shading 已恢复。

## 9. 图片真正进入 MCP 内容

检查 id=12、13、14 的原始 MCP 工具结果，而不只是结构化 metadata。每次必须同时包含：

```json
{"type":"text","text":"<JSON metadata>"}
```

以及：

```json
{"type":"image","mimeType":"image/png","data":"<非空 base64>"}
```

把 `data` 解码后必须以十六进制 `89 50 4E 47 0D 0A 1A 0A` 开头，并能被图片查看器打开。人工检查 Cube 未被非正方形 800×600 截图明显拉伸，FRONT 与 CURRENT 方向不同。

## 10. Windows 工作目录边界

```json
{"jsonrpc":"2.0","id":15,"method":"tools/call","params":{"name":"save","arguments":{"path":"..\\outside.blend","overwrite":true,"compress":true}}}
```

通过条件：返回 `PATH_OUTSIDE_WORKDIR`，workspace 外未创建文件。再分别验证：

- workspace 内混合大小写绝对路径仍规范化到同一根目录；
- 另一个盘符（例如 workspace 在 `D:`，参数为 `C:\outside.blend`）被拒绝；
- 指向 workspace 外的 symlink/junction 被拒绝；
- `.jpg` 渲染路径与非 `.blend` 保存路径返回 `VALIDATION_ERROR`。

## 验收记录

记录每项 Pass/Fail、失败响应、Blender System Info、GPU backend，以及截图文件。只有十项全部通过，才把本机 Windows + Blender 5.x 真机烟测标记为通过；Linux/Python mock 自动测试不能替代此记录。
