import base64
import json
import tempfile
import unittest
from pathlib import Path

from blender_mcp.connection import BlenderCommandError
from blender_mcp.tool_results import error_result, image_result, text_result

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ToolResultTests(unittest.TestCase):
    def test_text_result_has_equivalent_visible_and_structured_metadata(self):
        result = text_result({"success": True, "ready": True})

        self.assertEqual(json.loads(result.content[0].text), result.structuredContent)
        self.assertTrue(result.structuredContent["ready"])
        self.assertFalse(result.isError)

    def test_image_result_contains_metadata_and_png_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            image = root / "renders" / "view.png"
            image.parent.mkdir()
            image.write_bytes(PNG)

            result = image_result(
                {"success": True, "image_path": str(image)}, workdir=str(root)
            )

        self.assertEqual(json.loads(result.content[0].text), result.structuredContent)
        self.assertEqual(result.content[1].mimeType, "image/png")
        self.assertEqual(base64.b64decode(result.content[1].data), PNG)
        self.assertFalse(result.isError)

    def test_image_outside_workdir_is_rejected(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            path = Path(outside) / "bad.png"
            path.write_bytes(PNG)

            with self.assertRaisesRegex(ValueError, "outside"):
                image_result({"image_path": str(path)}, workdir=root)

    def test_non_png_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = root / "renders" / "fake.png"
            path.parent.mkdir()
            path.write_bytes(b"not a png")

            with self.assertRaisesRegex(ValueError, "PNG"):
                image_result({"image_path": str(path)}, workdir=str(root))

    def test_missing_image_is_reported_as_not_found_validation_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            missing = root / "renders" / "missing.png"

            with self.assertRaisesRegex(ValueError, "does not exist") as caught:
                image_result(
                    {"image_path": str(missing)}, workdir=str(root)
                )

        self.assertEqual(caught.exception.category, "NOT_FOUND")

    def test_remote_error_preserves_diagnostics_and_marks_mcp_error(self):
        error = BlenderCommandError(
            "BLENDER_ERROR",
            "KeyError",
            "missing",
            "trace",
            False,
            "request-1",
            {"stdout": "before"},
        )

        result = error_result(error)

        self.assertTrue(result.isError)
        self.assertEqual(result.structuredContent["error"]["type"], "KeyError")
        self.assertEqual(result.structuredContent["error"]["traceback"], "trace")
        self.assertEqual(
            result.structuredContent["error"]["details"]["stdout"], "before"
        )

    def test_connection_error_has_stable_category_without_traceback(self):
        result = error_result(ConnectionError("offline"))

        self.assertTrue(result.isError)
        self.assertEqual(
            result.structuredContent["error"]["category"], "CONNECTION_ERROR"
        )
        self.assertIsNone(result.structuredContent["error"]["traceback"])


if __name__ == "__main__":
    unittest.main()
