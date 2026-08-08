import base64
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from mcp.shared.memory import create_connected_server_and_client_session

import blender_mcp.server as server

EXPECTED_TOOLS = [
    "execute_python",
    "get_scene_info",
    "get_status",
    "open",
    "render",
    "save",
    "viewport_screenshot",
]
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class OfflineConnection:
    def send_command(self, _name, _params=None):
        raise ConnectionError("Blender is offline for this test")

    def disconnect(self):
        pass


class ImageConnection:
    def __init__(self, workdir: Path, image_path: Path):
        self.workdir = workdir
        self.image_path = image_path

    def send_command(self, name, _params=None):
        if name != "render":
            raise AssertionError(f"unexpected command: {name}")
        return {
            "success": True,
            "workdir": str(self.workdir),
            "image_path": str(self.image_path),
            "width": 64,
            "height": 64,
        }

    def disconnect(self):
        pass


class MCPSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_discovers_seven_tools_and_offline_status_is_structured(self):
        server._blender_connection = OfflineConnection()
        async with create_connected_server_and_client_session(
            server.mcp,
            read_timeout_seconds=timedelta(seconds=5),
        ) as session:
            tools = await session.list_tools()
            status = await session.call_tool("get_status", {})

        self.assertEqual(sorted(tool.name for tool in tools.tools), EXPECTED_TOOLS)
        self.assertTrue(status.isError)
        self.assertEqual(
            status.structuredContent["error"]["category"], "CONNECTION_ERROR"
        )

    async def test_session_preserves_render_image_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp).resolve()
            image_path = workdir / "renders" / "view.png"
            image_path.parent.mkdir()
            image_path.write_bytes(PNG)
            server._blender_connection = ImageConnection(workdir, image_path)

            async with create_connected_server_and_client_session(
                server.mcp,
                read_timeout_seconds=timedelta(seconds=5),
            ) as session:
                result = await session.call_tool(
                    "render", {"width": 64, "height": 64}
                )

        images = [block for block in result.content if block.type == "image"]
        self.assertFalse(result.isError)
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].mimeType, "image/png")
        self.assertEqual(base64.b64decode(images[0].data), PNG)


if __name__ == "__main__":
    unittest.main()
