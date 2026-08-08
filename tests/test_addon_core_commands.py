import base64
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from tests.addon_loader import load_addon

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class SizedCollection(list):
    pass


class ObjectCollection(SizedCollection):
    def get(self, name):
        return next((item for item in self if item.name == name), None)

    def __getitem__(self, key):
        if isinstance(key, str):
            item = self.get(key)
            if item is None:
                raise KeyError(key)
            return item
        return super().__getitem__(key)


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
        filepath="C:/previous/output.png",
        resolution_x=1920,
        resolution_y=1080,
        resolution_percentage=50,
        image_settings=types.SimpleNamespace(
            file_format="JPEG",
            color_mode="RGB",
        ),
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
            objects=ObjectCollection(objects + [camera]),
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


def install_fake_wm(fake_bpy):
    calls = {"save": [], "open": []}

    def save_as_mainfile(**kwargs):
        calls["save"].append(dict(kwargs))
        path = Path(kwargs["filepath"])
        path.write_bytes(b"BLENDER")
        fake_bpy.data.filepath = str(path)
        fake_bpy.data.is_dirty = False
        return {"FINISHED"}

    def open_mainfile(**kwargs):
        calls["open"].append(dict(kwargs))
        fake_bpy.data.filepath = kwargs["filepath"]
        fake_bpy.data.is_dirty = False
        fake_bpy.context.scene.name = "OpenedScene"
        return {"FINISHED"}

    fake_bpy.ops = types.SimpleNamespace(
        wm=types.SimpleNamespace(
            save_as_mainfile=save_as_mainfile,
            open_mainfile=open_mainfile,
        )
    )
    return calls


def install_fake_render(fake_bpy, *, status=None, write_file=True):
    calls = []
    status = {"FINISHED"} if status is None else status

    def render(**kwargs):
        settings = fake_bpy.context.scene.render
        calls.append(
            {
                "kwargs": dict(kwargs),
                "camera": fake_bpy.context.scene.camera.name,
                "engine": settings.engine,
                "filepath": settings.filepath,
                "width": settings.resolution_x,
                "height": settings.resolution_y,
                "percentage": settings.resolution_percentage,
                "file_format": settings.image_settings.file_format,
                "color_mode": settings.image_settings.color_mode,
            }
        )
        if write_file:
            Path(settings.filepath).write_bytes(PNG)
        return status

    fake_bpy.ops = types.SimpleNamespace(
        render=types.SimpleNamespace(render=render)
    )
    return calls


def install_fake_viewport(fake_bpy, *, available=True):
    calls = {"axis": [], "camera": 0, "updates": 0, "overrides": []}
    region_3d = types.SimpleNamespace(
        view_distance=8.0,
        view_location=[1.0, 2.0, 3.0],
        view_rotation=[1.0, 0.0, 0.0, 0.0],
        view_perspective="PERSP",
        view_camera_zoom=1.25,
        view_camera_offset=[0.1, -0.2],
        view_matrix=[[1.0]],
        window_matrix=[[2.0]],
    )

    def update_region():
        calls["updates"] += 1

    region_3d.update = update_region
    shading = types.SimpleNamespace(type="MATERIAL")
    space = types.SimpleNamespace(
        type="VIEW_3D", region_3d=region_3d, shading=shading
    )
    region = types.SimpleNamespace(type="WINDOW", width=900, height=700)
    area = types.SimpleNamespace(
        type="VIEW_3D",
        regions=[region],
        spaces=types.SimpleNamespace(active=space),
    )
    screen = types.SimpleNamespace(areas=[area] if available else [])
    window = types.SimpleNamespace(
        screen=screen,
        scene=fake_bpy.context.scene,
        view_layer=fake_bpy.context.view_layer,
    )
    fake_bpy.context.window = window
    fake_bpy.context.window_manager = types.SimpleNamespace(windows=[window])
    fake_bpy.context.preferences = types.SimpleNamespace(
        view=types.SimpleNamespace(smooth_view=200)
    )

    @contextmanager
    def temp_override(**kwargs):
        calls["overrides"].append(dict(kwargs))
        yield

    fake_bpy.context.temp_override = temp_override

    def update_view_layer():
        calls["updates"] += 1

    fake_bpy.context.view_layer.update = update_view_layer

    def view_axis(**kwargs):
        calls["axis"].append(dict(kwargs))
        region_3d.view_rotation = [0.0, 1.0, 0.0, 0.0]
        region_3d.view_perspective = "ORTHO"
        return {"FINISHED"}

    def view_camera():
        calls["camera"] += 1
        region_3d.view_perspective = "CAMERA"
        return {"FINISHED"}

    fake_bpy.ops = types.SimpleNamespace(
        view3d=types.SimpleNamespace(
            view_axis=view_axis,
            view_camera=view_camera,
        )
    )
    return calls, region_3d, shading


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
        self.assertEqual(error.error_type, "KeyError")
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

    def test_save_creates_validated_parent_and_blend_file(self):
        fake_bpy = make_fake_bpy()
        calls = install_fake_wm(fake_bpy)
        with tempfile.TemporaryDirectory() as tmp:
            root = self.addon.initialize_workdir(Path(tmp) / "BlenderMCP")
            with patch.object(self.addon, "bpy", fake_bpy), patch.object(
                self.addon, "get_configured_workdir", return_value=root
            ):
                result = self.server.save(
                    "projects/round-1/person.blend", compress=False
                )

            target = root / "projects" / "round-1" / "person.blend"
            self.assertTrue(target.is_file())
            self.assertEqual(result["path"], str(target))
            self.assertEqual(result["size_bytes"], len(b"BLENDER"))
            self.assertFalse(result["is_dirty"])
            self.assertEqual(
                calls["save"],
                [
                    {
                        "filepath": str(target),
                        "check_existing": False,
                        "compress": False,
                    }
                ],
            )

    def test_save_refuses_existing_file_without_overwrite(self):
        fake_bpy = make_fake_bpy()
        calls = install_fake_wm(fake_bpy)
        with tempfile.TemporaryDirectory() as tmp:
            root = self.addon.initialize_workdir(Path(tmp) / "BlenderMCP")
            target = root / "projects" / "person.blend"
            target.write_bytes(b"existing")
            with patch.object(self.addon, "bpy", fake_bpy), patch.object(
                self.addon, "get_configured_workdir", return_value=root
            ):
                with self.assertRaises(self.addon.CommandError) as caught:
                    self.server.save("projects/person.blend")

        self.assertEqual(caught.exception.category, "VALIDATION_ERROR")
        self.assertEqual(calls["save"], [])

    def test_open_refuses_to_discard_dirty_file_by_default(self):
        fake_bpy = make_fake_bpy()
        calls = install_fake_wm(fake_bpy)
        with tempfile.TemporaryDirectory() as tmp:
            root = self.addon.initialize_workdir(Path(tmp) / "BlenderMCP")
            target = root / "projects" / "person.blend"
            target.write_bytes(b"BLENDER")
            with patch.object(self.addon, "bpy", fake_bpy), patch.object(
                self.addon, "get_configured_workdir", return_value=root
            ):
                with self.assertRaises(self.addon.CommandError) as caught:
                    self.server.open_file("projects/person.blend")

        self.assertEqual(caught.exception.category, "DIRTY_FILE")
        self.assertEqual(calls["open"], [])

    def test_open_with_explicit_discard_disables_ui_and_scripts(self):
        fake_bpy = make_fake_bpy([make_mesh_object("Head", 8, 12, 6)])
        calls = install_fake_wm(fake_bpy)
        with tempfile.TemporaryDirectory() as tmp:
            root = self.addon.initialize_workdir(Path(tmp) / "BlenderMCP")
            target = root / "projects" / "person.blend"
            target.write_bytes(b"BLENDER")
            with patch.object(self.addon, "bpy", fake_bpy), patch.object(
                self.addon, "get_configured_workdir", return_value=root
            ):
                result = self.server.open_file(
                    "projects/person.blend", discard_unsaved=True
                )

        self.assertEqual(
            calls["open"],
            [
                {
                    "filepath": str(target),
                    "load_ui": False,
                    "use_scripts": False,
                }
            ],
        )
        self.assertEqual(result["scene"], "OpenedScene")
        self.assertFalse(result["is_dirty"])
        self.assertEqual(result["object_count"], 2)

    def test_render_writes_png_and_restores_all_scene_settings(self):
        fake_bpy = make_fake_bpy()
        calls = install_fake_render(fake_bpy)
        scene = fake_bpy.context.scene
        original = (
            scene.camera,
            scene.render.engine,
            scene.render.filepath,
            scene.render.resolution_x,
            scene.render.resolution_y,
            scene.render.resolution_percentage,
            scene.render.image_settings.file_format,
            scene.render.image_settings.color_mode,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = self.addon.initialize_workdir(Path(tmp) / "BlenderMCP")
            with patch.object(self.addon, "bpy", fake_bpy), patch.object(
                self.addon, "get_configured_workdir", return_value=root
            ):
                result = self.server.render(
                    camera="Camera",
                    width=640,
                    height=480,
                    engine="CYCLES",
                    output_path="renders/portrait.png",
                )

            target = root / "renders" / "portrait.png"
            self.assertEqual(target.read_bytes(), PNG)
            self.assertEqual(result["image_path"], str(target))
            self.assertEqual(result["workdir"], str(root))
            self.assertEqual(result["size_bytes"], len(PNG))
            self.assertEqual(result["camera"], "Camera")
            self.assertEqual(result["engine"], "CYCLES")
            self.assertEqual(
                calls,
                [
                    {
                        "kwargs": {"write_still": True},
                        "camera": "Camera",
                        "engine": "CYCLES",
                        "filepath": str(target),
                        "width": 640,
                        "height": 480,
                        "percentage": 100,
                        "file_format": "PNG",
                        "color_mode": "RGBA",
                    }
                ],
            )

        restored = (
            scene.camera,
            scene.render.engine,
            scene.render.filepath,
            scene.render.resolution_x,
            scene.render.resolution_y,
            scene.render.resolution_percentage,
            scene.render.image_settings.file_format,
            scene.render.image_settings.color_mode,
        )
        self.assertEqual(restored, original)

    def test_render_failure_still_restores_settings(self):
        fake_bpy = make_fake_bpy()
        install_fake_render(fake_bpy, status={"CANCELLED"}, write_file=False)
        scene = fake_bpy.context.scene
        original = (
            scene.render.engine,
            scene.render.filepath,
            scene.render.resolution_x,
            scene.render.resolution_y,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = self.addon.initialize_workdir(Path(tmp) / "BlenderMCP")
            with patch.object(self.addon, "bpy", fake_bpy), patch.object(
                self.addon, "get_configured_workdir", return_value=root
            ):
                with self.assertRaises(self.addon.CommandError) as caught:
                    self.server.render(width=256, height=256, engine="CYCLES")

        self.assertEqual(caught.exception.category, "BLENDER_ERROR")
        self.assertEqual(
            (
                scene.render.engine,
                scene.render.filepath,
                scene.render.resolution_x,
                scene.render.resolution_y,
            ),
            original,
        )

    def test_render_rejects_invalid_dimensions_camera_and_engine(self):
        fake_bpy = make_fake_bpy()
        install_fake_render(fake_bpy)
        with tempfile.TemporaryDirectory() as tmp:
            root = self.addon.initialize_workdir(Path(tmp) / "BlenderMCP")
            with patch.object(self.addon, "bpy", fake_bpy), patch.object(
                self.addon, "get_configured_workdir", return_value=root
            ):
                invalid_calls = (
                    {"width": True},
                    {"height": 32},
                    {"camera": "Missing"},
                    {"engine": "NOT_AN_ENGINE"},
                )
                for params in invalid_calls:
                    with self.subTest(params=params):
                        with self.assertRaises(self.addon.CommandError) as caught:
                            self.server.render(**params)
                        self.assertIn(
                            caught.exception.category,
                            {"VALIDATION_ERROR", "NOT_FOUND"},
                        )

    def test_viewport_front_solid_uses_gpu_and_restores_view_state(self):
        fake_bpy = make_fake_bpy()
        calls, region_3d, shading = install_fake_viewport(fake_bpy)
        original = (
            region_3d.view_distance,
            list(region_3d.view_location),
            list(region_3d.view_rotation),
            region_3d.view_perspective,
            region_3d.view_camera_zoom,
            list(region_3d.view_camera_offset),
            shading.type,
        )
        observed = []

        def capture_gpu(**kwargs):
            observed.append(
                (
                    kwargs["space"].shading.type,
                    kwargs["region_3d"].view_perspective,
                    kwargs["width"],
                    kwargs["height"],
                )
            )
            kwargs["target"].write_bytes(PNG)

        with tempfile.TemporaryDirectory() as tmp:
            root = self.addon.initialize_workdir(Path(tmp) / "BlenderMCP")
            with patch.object(self.addon, "bpy", fake_bpy), patch.object(
                self.addon, "get_configured_workdir", return_value=root
            ), patch.object(
                self.server, "_capture_viewport_gpu", side_effect=capture_gpu
            ):
                result = self.server.viewport_screenshot(
                    view="FRONT",
                    mode="SOLID",
                    width=800,
                    height=600,
                    output_path="renders/front.png",
                )

        self.assertEqual(
            calls["axis"],
            [{"type": "FRONT", "align_active": False, "relative": False}],
        )
        self.assertEqual(observed, [("SOLID", "ORTHO", 800, 600)])
        self.assertEqual(result["method"], "gpu_offscreen")
        self.assertEqual(result["view"], "FRONT")
        self.assertEqual(result["mode"], "SOLID")
        self.assertEqual(
            (
                region_3d.view_distance,
                list(region_3d.view_location),
                list(region_3d.view_rotation),
                region_3d.view_perspective,
                region_3d.view_camera_zoom,
                list(region_3d.view_camera_offset),
                shading.type,
            ),
            original,
        )

    def test_viewport_gpu_failure_uses_exact_size_fallback(self):
        fake_bpy = make_fake_bpy()
        _calls, _region_3d, _shading = install_fake_viewport(fake_bpy)
        fallback_calls = []

        def capture_fallback(**kwargs):
            fallback_calls.append((kwargs["width"], kwargs["height"]))
            kwargs["target"].write_bytes(PNG)

        with tempfile.TemporaryDirectory() as tmp:
            root = self.addon.initialize_workdir(Path(tmp) / "BlenderMCP")
            with patch.object(self.addon, "bpy", fake_bpy), patch.object(
                self.addon, "get_configured_workdir", return_value=root
            ), patch.object(
                self.server,
                "_capture_viewport_gpu",
                side_effect=RuntimeError("GPU unavailable"),
            ), patch.object(
                self.server,
                "_capture_viewport_fallback",
                side_effect=capture_fallback,
            ):
                result = self.server.viewport_screenshot(width=320, height=240)

        self.assertEqual(fallback_calls, [(320, 240)])
        self.assertEqual(result["method"], "screen_fallback")

    def test_viewport_gpu_helper_frees_offscreen_and_image(self):
        fake_bpy = make_fake_bpy()
        events = []

        class FakePixels:
            def foreach_set(self, values):
                events.append(("pixels", len(values), float(values[-1])))

        class FakeImage:
            def __init__(self, target):
                self.pixels = FakePixels()
                self.file_format = None
                self.filepath_raw = str(target)

            def update(self):
                events.append(("image_update",))

            def save(self):
                Path(self.filepath_raw).write_bytes(PNG)

        class FakeImages:
            def __init__(self, target):
                self.target = target

            def new(self, name, **kwargs):
                events.append(("image_new", name, kwargs))
                return FakeImage(self.target)

            def remove(self, _image):
                events.append(("image_remove",))

        class FakeOffscreen:
            def __init__(self, width, height, **kwargs):
                events.append(("offscreen_new", width, height, kwargs))
                self.texture_color = types.SimpleNamespace(
                    read=lambda: bytes([0, 64, 128, 255] * (width * height))
                )

            def draw_view3d(self, *args, **kwargs):
                events.append(("draw", len(args), kwargs))

            def free(self):
                events.append(("free",))

        fake_gpu = types.SimpleNamespace(
            types=types.SimpleNamespace(GPUOffScreen=FakeOffscreen)
        )

        class FakeArray(list):
            def astype(self, _dtype):
                return self

            def __imul__(self, factor):
                for index, value in enumerate(self):
                    self[index] = value * factor
                return self

        fake_numpy = types.SimpleNamespace(
            uint8=object(),
            float32=object(),
            frombuffer=lambda data, dtype, count: FakeArray(list(data)[:count]),
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "viewport.png"
            fake_bpy.data.images = FakeImages(target)
            with patch.object(self.addon, "bpy", fake_bpy), patch.dict(
                sys.modules, {"gpu": fake_gpu, "numpy": fake_numpy}
            ):
                self.server._capture_viewport_gpu(
                    target=target,
                    width=2,
                    height=2,
                    scene=object(),
                    view_layer=object(),
                    space=object(),
                    region=object(),
                    region_3d=object(),
                    view_matrix=object(),
                    projection_matrix=object(),
                )

        self.assertIn(("pixels", 16, 1.0), events)
        self.assertIn(("image_remove",), events)
        self.assertEqual(events[-1], ("free",))

    def test_viewport_screen_fallback_resizes_and_overwrites_without_dialog(self):
        fake_bpy = make_fake_bpy()
        events = []

        class FakeImage:
            size = (900, 700)
            file_format = None
            filepath_raw = ""

            def scale(self, width, height):
                events.append(("scale", width, height))
                self.size = (width, height)

            def save(self):
                Path(self.filepath_raw).write_bytes(PNG)

        image = FakeImage()

        def screenshot_area(**kwargs):
            events.append(("screenshot", dict(kwargs)))
            Path(kwargs["filepath"]).write_bytes(PNG)
            return {"FINISHED"}

        fake_bpy.ops = types.SimpleNamespace(
            screen=types.SimpleNamespace(screenshot_area=screenshot_area)
        )
        fake_bpy.data.images = types.SimpleNamespace(
            load=lambda *args, **kwargs: image,
            remove=lambda _image: events.append(("image_remove",)),
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "fallback.png"
            with patch.object(self.addon, "bpy", fake_bpy):
                self.server._capture_viewport_fallback(
                    target=target,
                    width=320,
                    height=240,
                )

        self.assertEqual(
            events[0],
            (
                "screenshot",
                {"filepath": str(target), "check_existing": False},
            ),
        )
        self.assertIn(("scale", 320, 240), events)
        self.assertEqual(events[-1], ("image_remove",))

    def test_viewport_requires_gui_area_and_camera_when_requested(self):
        fake_bpy = make_fake_bpy()
        install_fake_viewport(fake_bpy, available=False)
        with patch.object(self.addon, "bpy", fake_bpy):
            with self.assertRaises(self.addon.CommandError) as no_area:
                self.server.viewport_screenshot()
        self.assertEqual(no_area.exception.category, "BLENDER_ERROR")

        fake_bpy = make_fake_bpy()
        install_fake_viewport(fake_bpy)
        fake_bpy.context.scene.camera = None
        with patch.object(self.addon, "bpy", fake_bpy):
            with self.assertRaises(self.addon.CommandError) as no_camera:
                self.server.viewport_screenshot(view="CAMERA")
        self.assertEqual(no_camera.exception.category, "NOT_FOUND")

    def test_viewport_camera_does_not_toggle_when_already_in_camera_view(self):
        fake_bpy = make_fake_bpy()
        calls, region_3d, _shading = install_fake_viewport(fake_bpy)
        region_3d.view_perspective = "CAMERA"
        camera = fake_bpy.context.scene.camera
        camera.matrix_world = types.SimpleNamespace(inverted=lambda: [[1.0]])
        camera.calc_matrix_camera = lambda *args, **kwargs: [[2.0]]
        fake_bpy.context.evaluated_depsgraph_get = lambda: object()

        def capture_gpu(**kwargs):
            kwargs["target"].write_bytes(PNG)

        with tempfile.TemporaryDirectory() as tmp:
            root = self.addon.initialize_workdir(Path(tmp) / "BlenderMCP")
            with patch.object(self.addon, "bpy", fake_bpy), patch.object(
                self.addon, "get_configured_workdir", return_value=root
            ), patch.object(
                self.server, "_capture_viewport_gpu", side_effect=capture_gpu
            ):
                result = self.server.viewport_screenshot(view="CAMERA")

        self.assertEqual(calls["camera"], 0)
        self.assertEqual(result["view"], "CAMERA")

    def test_viewport_reports_both_capture_failures(self):
        fake_bpy = make_fake_bpy()
        _calls, region_3d, shading = install_fake_viewport(fake_bpy)
        original = (region_3d.view_perspective, shading.type)
        with tempfile.TemporaryDirectory() as tmp:
            root = self.addon.initialize_workdir(Path(tmp) / "BlenderMCP")
            with patch.object(self.addon, "bpy", fake_bpy), patch.object(
                self.addon, "get_configured_workdir", return_value=root
            ), patch.object(
                self.server,
                "_capture_viewport_gpu",
                side_effect=RuntimeError("GPU unavailable"),
            ), patch.object(
                self.server,
                "_capture_viewport_fallback",
                side_effect=RuntimeError("screen unavailable"),
            ):
                with self.assertRaises(self.addon.CommandError) as caught:
                    self.server.viewport_screenshot(view="TOP", mode="WIREFRAME")

        self.assertEqual(caught.exception.category, "BLENDER_ERROR")
        self.assertIn("GPU unavailable", caught.exception.details["gpu_error"])
        self.assertIn(
            "screen unavailable", caught.exception.details["fallback_error"]
        )
        self.assertEqual((region_3d.view_perspective, shading.type), original)


if __name__ == "__main__":
    unittest.main()
