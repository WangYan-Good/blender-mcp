import tempfile
import unittest
from pathlib import Path

from tests.addon_loader import load_addon


class MemorySocket:
    def __init__(self):
        self.data = bytearray()

    def sendall(self, data):
        self.data.extend(data)

    def recv(self, size):
        chunk = bytes(self.data[:size])
        del self.data[:size]
        return chunk


class AddonContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.addon = load_addon()

    def test_targets_blender_5(self):
        self.assertEqual(self.addon.bl_info["blender"], (5, 0, 0))

    def test_blender_properties_are_evaluated_not_string_annotations(self):
        annotation = self.addon.BLENDERMCP_AddonPreferences.__annotations__["workdir"]
        self.assertNotIsInstance(annotation, str)

    def test_framed_message_round_trip_preserves_unicode(self):
        transport = MemorySocket()
        message = {
            "protocol_version": 1,
            "request_id": "frame-test",
            "type": "get_status",
            "params": {"label": "人物"},
        }

        self.addon.send_frame(transport, message)

        self.assertEqual(self.addon.receive_frame(transport), message)

    def test_framed_messages_reject_non_finite_numbers(self):
        transport = MemorySocket()

        with self.assertRaises(self.addon.ProtocolError):
            self.addon.send_frame(transport, {"value": float("inf")})

    def test_environment_workdir_wins_over_preference(self):
        result = self.addon.resolve_workdir(
            preference="C:/from-preference",
            environ={"BLENDER_MCP_WORKDIR": "C:/from-environment"},
            home=Path("C:/Users/example"),
        )

        self.assertEqual(result, Path("C:/from-environment"))

    def test_default_workdir_uses_home(self):
        result = self.addon.resolve_workdir(
            preference="", environ={}, home=Path("C:/Users/example")
        )

        self.assertEqual(result, Path("C:/Users/example/BlenderMCP"))

    def test_initialize_workdir_creates_required_subdirectories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.addon.initialize_workdir(Path(tmp) / "BlenderMCP")

            self.assertEqual(
                sorted(path.name for path in root.iterdir()),
                ["assets", "exports", "input", "projects", "renders", "scripts"],
            )

    def test_relative_output_can_create_nested_parent_inside_workdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.addon.initialize_workdir(Path(tmp) / "BlenderMCP")

            result = self.addon.resolve_managed_path(
                "renders/round-1/front.png",
                workdir=root,
                suffix=".png",
                must_exist=False,
            )

            self.assertEqual(result, root / "renders" / "round-1" / "front.png")
            self.assertFalse(result.parent.exists())

    def test_parent_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.addon.initialize_workdir(Path(tmp) / "BlenderMCP")

            with self.assertRaises(self.addon.CommandError) as caught:
                self.addon.resolve_managed_path(
                    "../outside.blend",
                    workdir=root,
                    suffix=".blend",
                    must_exist=False,
                )

            self.assertEqual(caught.exception.category, "PATH_OUTSIDE_WORKDIR")

    def test_existing_symlink_cannot_escape_workdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = self.addon.initialize_workdir(base / "BlenderMCP")
            outside = base / "outside.blend"
            outside.write_bytes(b"outside")
            link = root / "projects" / "linked.blend"
            try:
                link.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlinks are unavailable: {exc}")

            with self.assertRaises(self.addon.CommandError) as caught:
                self.addon.resolve_managed_path(
                    "projects/linked.blend",
                    workdir=root,
                    suffix=".blend",
                    must_exist=False,
                )

            self.assertEqual(caught.exception.category, "PATH_OUTSIDE_WORKDIR")

    def test_wrong_extension_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.addon.initialize_workdir(Path(tmp) / "BlenderMCP")

            with self.assertRaises(self.addon.CommandError) as caught:
                self.addon.resolve_managed_path(
                    "renders/view.jpg",
                    workdir=root,
                    suffix=".png",
                    must_exist=False,
                )

            self.assertEqual(caught.exception.category, "VALIDATION_ERROR")

    def test_addon_has_no_optional_network_providers(self):
        source = Path(self.addon.__file__).read_text(encoding="utf-8").lower()

        for forbidden in (
            "polyhaven",
            "sketchfab",
            "hyper3d",
            "hunyuan",
            "requests.get",
            "requests.post",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
