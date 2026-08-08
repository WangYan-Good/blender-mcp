import base64
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import blender_mcp.server as server
from blender_mcp.connection import BlenderCommandError

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakeConnection:
    def __init__(self, response=None, error=None):
        self.response = response or {"success": True}
        self.error = error
        self.calls = []
        self.disconnected = False

    def send_command(self, name, params=None):
        self.calls.append((name, dict(params or {})))
        if self.error:
            raise self.error
        return dict(self.response)

    def disconnect(self):
        self.disconnected = True


class ServerToolTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeConnection()
        self.connection_patch = patch.object(
            server, "_blender_connection", self.fake
        )
        self.connection_patch.start()

    def tearDown(self):
        self.connection_patch.stop()

    def test_execute_python_forwards_code_and_returns_structured_result(self):
        self.fake.response = {
            "success": True,
            "stdout": "ok\n",
            "stderr": "",
            "result": 4,
        }

        result = server.execute_python("result = 2 + 2")

        self.assertEqual(
            self.fake.calls, [("execute_python", {"code": "result = 2 + 2"})]
        )
        self.assertEqual(result.structuredContent["result"], 4)
        self.assertFalse(result.isError)

    def test_execute_python_rejects_oversize_before_contacting_blender(self):
        result = server.execute_python("#" * (1024 * 1024 + 1))

        self.assertTrue(result.isError)
        self.assertEqual(
            result.structuredContent["error"]["category"], "VALIDATION_ERROR"
        )
        self.assertEqual(self.fake.calls, [])

    def test_scene_flags_and_limit_are_forwarded(self):
        server.get_scene_info(False, True, False, True, 25)

        self.assertEqual(
            self.fake.calls,
            [
                (
                    "get_scene_info",
                    {
                        "include_objects": False,
                        "include_materials": True,
                        "include_collections": False,
                        "include_cameras": True,
                        "max_objects": 25,
                    },
                )
            ],
        )

    def test_remote_error_becomes_mcp_error_instead_of_prose_success(self):
        self.fake.error = BlenderCommandError(
            "BLENDER_ERROR",
            "KeyError",
            "missing",
            "trace",
            False,
            "request-2",
            {},
        )

        result = server.execute_python("raise KeyError('missing')")

        self.assertTrue(result.isError)
        self.assertEqual(result.structuredContent["error"]["type"], "KeyError")

    def test_status_connection_failure_is_an_error_result(self):
        self.fake.error = ConnectionError("Blender is offline")

        result = server.get_status()

        self.assertTrue(result.isError)
        self.assertEqual(
            result.structuredContent["error"]["category"], "CONNECTION_ERROR"
        )

    def test_image_command_uses_blender_reported_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = root / "renders" / "image.png"
            path.parent.mkdir()
            path.write_bytes(PNG)
            self.fake.response = {
                "success": True,
                "image_path": str(path),
                "workdir": str(root),
            }

            result = server._run_image("render", {})

        self.assertEqual(result.content[1].mimeType, "image/png")

    def test_image_response_without_workspace_is_validation_error(self):
        self.fake.response = {"success": True, "image_path": "renders/image.png"}

        result = server._run_image("render", {})

        self.assertTrue(result.isError)
        self.assertEqual(
            result.structuredContent["error"]["category"], "VALIDATION_ERROR"
        )

    def test_save_forwards_explicit_overwrite_and_compression(self):
        result = server.save(
            "projects/person.blend", overwrite=True, compress=False
        )

        self.assertFalse(result.isError)
        self.assertEqual(
            self.fake.calls,
            [
                (
                    "save",
                    {
                        "path": "projects/person.blend",
                        "overwrite": True,
                        "compress": False,
                    },
                )
            ],
        )

    def test_save_rejects_non_boolean_options_before_contacting_blender(self):
        result = server.save("projects/person.blend", overwrite=1)

        self.assertTrue(result.isError)
        self.assertEqual(
            result.structuredContent["error"]["category"], "VALIDATION_ERROR"
        )
        self.assertEqual(self.fake.calls, [])

    def test_open_defaults_to_preserving_dirty_state(self):
        result = server.open("projects/person.blend")

        self.assertFalse(result.isError)
        self.assertEqual(
            self.fake.calls,
            [
                (
                    "open",
                    {
                        "path": "projects/person.blend",
                        "discard_unsaved": False,
                    },
                )
            ],
        )

    def test_open_rejects_empty_path_before_contacting_blender(self):
        result = server.open("")

        self.assertTrue(result.isError)
        self.assertEqual(
            result.structuredContent["error"]["category"], "VALIDATION_ERROR"
        )
        self.assertEqual(self.fake.calls, [])

    def test_render_returns_png_content_and_forwards_parameters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "renders" / "portrait.png"
            target.parent.mkdir()
            target.write_bytes(PNG)
            self.fake.response = {
                "success": True,
                "image_path": str(target),
                "workdir": str(root),
                "width": 512,
                "height": 384,
            }

            result = server.render(
                camera="Camera",
                width=512,
                height=384,
                engine="CYCLES",
                output_path="renders/portrait.png",
            )

        self.assertFalse(result.isError)
        self.assertEqual(result.content[1].mimeType, "image/png")
        self.assertEqual(
            self.fake.calls,
            [
                (
                    "render",
                    {
                        "camera": "Camera",
                        "width": 512,
                        "height": 384,
                        "engine": "CYCLES",
                        "output_path": "renders/portrait.png",
                    },
                )
            ],
        )

    def test_render_rejects_invalid_dimensions_before_contacting_blender(self):
        result = server.render(width=True)

        self.assertTrue(result.isError)
        self.assertEqual(
            result.structuredContent["error"]["category"], "VALIDATION_ERROR"
        )
        self.assertEqual(self.fake.calls, [])

    def test_viewport_tool_returns_png_and_forwards_parameters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "renders" / "front.png"
            target.parent.mkdir()
            target.write_bytes(PNG)
            self.fake.response = {
                "success": True,
                "image_path": str(target),
                "workdir": str(root),
                "view": "FRONT",
                "mode": "SOLID",
            }

            result = server.viewport_screenshot(
                view="FRONT", mode="SOLID", width=800, height=600
            )

        self.assertFalse(result.isError)
        self.assertEqual(result.content[1].mimeType, "image/png")
        self.assertEqual(
            self.fake.calls,
            [
                (
                    "viewport_screenshot",
                    {
                        "view": "FRONT",
                        "mode": "SOLID",
                        "width": 800,
                        "height": 600,
                        "output_path": None,
                    },
                )
            ],
        )

    def test_viewport_rejects_unknown_mode_before_contacting_blender(self):
        result = server.viewport_screenshot(mode="TEXTURED")

        self.assertTrue(result.isError)
        self.assertEqual(
            result.structuredContent["error"]["category"], "VALIDATION_ERROR"
        )
        self.assertEqual(self.fake.calls, [])

    def test_all_seven_core_tools_are_registered(self):
        names = sorted(tool.name for tool in server.mcp._tool_manager.list_tools())

        self.assertEqual(
            names,
            [
                "execute_python",
                "get_scene_info",
                "get_status",
                "open",
                "render",
                "save",
                "viewport_screenshot",
            ],
        )

    def test_import_is_silent_on_supported_python_hosts(self):
        completed = subprocess.run(
            [sys.executable, "-W", "default", "-c", "import blender_mcp.server"],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, "")


if __name__ == "__main__":
    unittest.main()
