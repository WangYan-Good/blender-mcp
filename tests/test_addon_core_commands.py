import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.addon_loader import load_addon


class SizedCollection(list):
    pass


def make_mesh_object(name, vertices, edges, polygons):
    return types.SimpleNamespace(
        name=name,
        type="MESH",
        users_collection=[types.SimpleNamespace(name="Characters")],
        hide_render=False,
        visible_get=lambda: True,
        location=(1.0, 2.0, 3.0),
        rotation_euler=(0.1, 0.2, 0.3),
        scale=(1.0, 1.0, 1.0),
        data=types.SimpleNamespace(
            vertices=SizedCollection(range(vertices)),
            edges=SizedCollection(range(edges)),
            polygons=SizedCollection(range(polygons)),
        ),
    )


def make_fake_bpy(objects=None):
    objects = list(objects or [])
    camera = types.SimpleNamespace(
        name="Camera",
        type="CAMERA",
        users_collection=[types.SimpleNamespace(name="Scene Collection")],
        hide_render=False,
        visible_get=lambda: True,
        location=(0.0, -5.0, 2.0),
        rotation_euler=(1.2, 0.0, 0.0),
        scale=(1.0, 1.0, 1.0),
        data=types.SimpleNamespace(type="PERSP", lens=50.0),
    )
    engine_items = [
        types.SimpleNamespace(identifier="BLENDER_EEVEE_NEXT"),
        types.SimpleNamespace(identifier="BLENDER_WORKBENCH"),
        types.SimpleNamespace(identifier="CYCLES"),
    ]
    render = types.SimpleNamespace(
        engine="BLENDER_EEVEE_NEXT",
        bl_rna=types.SimpleNamespace(
            properties={"engine": types.SimpleNamespace(enum_items=engine_items)}
        ),
    )
    scene = types.SimpleNamespace(
        name="Portrait",
        frame_current=12,
        render=render,
        objects=objects + [camera],
        camera=camera,
    )
    active = objects[0] if objects else camera
    return types.SimpleNamespace(
        app=types.SimpleNamespace(version_string="5.0.1", version=(5, 0, 1)),
        data=types.SimpleNamespace(
            filepath="C:/BlenderMCP/projects/person.blend",
            is_dirty=True,
            materials=[types.SimpleNamespace(name="Skin", use_nodes=True)],
            collections=[
                types.SimpleNamespace(name="Characters", objects=objects),
                types.SimpleNamespace(name="Scene Collection", objects=[camera]),
            ],
            cameras=[camera.data],
        ),
        context=types.SimpleNamespace(
            scene=scene,
            selected_objects=[active],
            view_layer=types.SimpleNamespace(
                objects=types.SimpleNamespace(active=active)
            ),
        ),
    )


class AddonCoreCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.addon = load_addon()

    def setUp(self):
        self.server = self.addon.BlenderMCPServer()

    def test_status_reports_versions_dirty_state_workspace_and_engines(self):
        fake_bpy = make_fake_bpy()
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            self.addon, "bpy", fake_bpy
        ), patch.object(
            self.addon, "get_configured_workdir", return_value=Path(tmp).resolve()
        ):
            result = self.server.get_status()

        self.assertEqual(result["blender_version"], "5.0.1")
        self.assertEqual(result["addon_version"], "2.0.0")
        self.assertTrue(result["is_dirty"])
        self.assertEqual(result["scene"], "Portrait")
        self.assertEqual(result["workdir"], str(Path(tmp).resolve()))
        self.assertEqual(
            result["render_engines"],
            ["BLENDER_EEVEE_NEXT", "BLENDER_WORKBENCH", "CYCLES"],
        )
        self.assertEqual(result["max_code_bytes"], 1024 * 1024)

    def test_execute_python_captures_both_streams_and_json_result(self):
        fake_bpy = make_fake_bpy()
        code = (
            "import sys\n"
            "print('stdout-ok')\n"
            "print('stderr-ok', file=sys.stderr)\n"
            "result = {'version': bpy.app.version_string}"
        )
        with patch.object(self.addon, "bpy", fake_bpy):
            result = self.server.execute_python(code)

        self.assertEqual(result["stdout"], "stdout-ok\n")
        self.assertEqual(result["stderr"], "stderr-ok\n")
        self.assertEqual(result["result"], {"version": "5.0.1"})
        self.assertFalse(result["stdout_truncated"])
        self.assertFalse(result["stderr_truncated"])

    def test_execute_python_preserves_partial_output_and_traceback(self):
        fake_bpy = make_fake_bpy()
        with patch.object(self.addon, "bpy", fake_bpy):
            with self.assertRaises(self.addon.CommandError) as caught:
                self.server.execute_python(
                    "print('before')\nbpy.data.objects['__missing__']"
                )

        error = caught.exception
        self.assertEqual(error.category, "BLENDER_ERROR")
        self.assertEqual(error.error_type, "AttributeError")
        self.assertEqual(error.details["stdout"], "before\n")
        self.assertIn("Traceback (most recent call last)", error.traceback_text)
        self.assertNotIn("print('before')", error.message)

    def test_execute_python_rejects_more_than_one_mibibyte(self):
        code = "#" * (1024 * 1024 + 1)

        with self.assertRaises(self.addon.CommandError) as caught:
            self.server.execute_python(code)

        self.assertEqual(caught.exception.category, "VALIDATION_ERROR")
        self.assertNotIn(code[:100], caught.exception.message)

    def test_utf8_capture_truncation_never_returns_partial_character(self):
        text, truncated = self.addon.truncate_utf8("人A", max_bytes=3)

        self.assertEqual(text, "人")
        self.assertTrue(truncated)

    def test_scene_info_returns_deterministic_mesh_summary(self):
        head = make_mesh_object("Head", vertices=8, edges=12, polygons=6)
        body = make_mesh_object("Body", vertices=16, edges=24, polygons=12)
        fake_bpy = make_fake_bpy([head, body])
        with patch.object(self.addon, "bpy", fake_bpy):
            result = self.server.get_scene_info(max_objects=2)

        self.assertEqual(result["scene"], "Portrait")
        self.assertEqual(result["active_object"], "Head")
        self.assertEqual(result["selected_objects"], ["Head"])
        self.assertEqual(result["camera"], "Camera")
        self.assertEqual(result["object_count"], 3)
        self.assertTrue(result["truncated"])
        self.assertEqual([item["name"] for item in result["objects"]], ["Body", "Camera"])
        self.assertEqual(result["objects"][0]["mesh"], {"vertices": 16, "edges": 24, "polygons": 12})
        self.assertEqual(result["materials"], [{"name": "Skin", "use_nodes": True}])

    def test_scene_info_can_omit_large_lists(self):
        fake_bpy = make_fake_bpy([make_mesh_object("Head", 8, 12, 6)])
        with patch.object(self.addon, "bpy", fake_bpy):
            result = self.server.get_scene_info(
                include_objects=False,
                include_materials=False,
                include_collections=False,
                include_cameras=False,
            )

        self.assertNotIn("objects", result)
        self.assertNotIn("materials", result)
        self.assertNotIn("collections", result)
        self.assertNotIn("cameras", result)

    def test_dispatch_exposes_the_three_core_commands(self):
        fake_bpy = make_fake_bpy()
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            self.addon, "bpy", fake_bpy
        ), patch.object(
            self.addon, "get_configured_workdir", return_value=Path(tmp).resolve()
        ):
            response = self.server._execute_request(
                {
                    "protocol_version": 1,
                    "request_id": "status-request",
                    "type": "get_status",
                    "params": {},
                }
            )

        self.assertTrue(response["success"])
        self.assertEqual(response["request_id"], "status-request")
        self.assertTrue(response["result"]["ready"])


if __name__ == "__main__":
    unittest.main()
