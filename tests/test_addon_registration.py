import types
import unittest
from unittest.mock import patch

from tests.addon_loader import load_addon


class FakeTimers:
    def __init__(self):
        self.registered = {}

    def register(self, function, first_interval=0.0, persistent=False):
        self.registered[function] = {
            "first_interval": first_interval,
            "persistent": persistent,
        }

    def is_registered(self, function):
        return function in self.registered

    def unregister(self, function):
        del self.registered[function]


class FakeServer:
    instances = []

    def __init__(self, host="localhost", port=9876):
        self.port = port
        self.running = False
        self.start_calls = 0
        FakeServer.instances.append(self)

    def start(self):
        self.start_calls += 1
        self.running = True

    def stop(self):
        self.running = False


class FailingServer(FakeServer):
    def start(self):
        raise RuntimeError("port in use")


def make_scene(auto_start=True):
    return types.SimpleNamespace(
        blendermcp_port=9876,
        blendermcp_auto_start_server=auto_start,
        blendermcp_server_running=False,
    )


def make_fake_bpy():
    return types.SimpleNamespace(
        types=types.SimpleNamespace(Scene=type("Scene", (), {})),
        utils=types.SimpleNamespace(
            register_class=lambda _cls: None,
            unregister_class=lambda _cls: None,
        ),
        app=types.SimpleNamespace(timers=FakeTimers(), background=False),
        # Matches registration from saved preferences: no scene exists yet.
        context=types.SimpleNamespace(),
    )


class AddonRegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.addon = load_addon()

    def setUp(self):
        FakeServer.instances = []
        self.bpy = make_fake_bpy()
        patchers = (
            patch.object(self.addon, "bpy", self.bpy),
            patch.object(self.addon, "BlenderMCPServer", FakeServer),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_register_without_scene_defers_auto_start_to_persistent_timer(self):
        self.addon.register()

        self.assertEqual(FakeServer.instances, [])
        self.assertEqual(
            self.bpy.app.timers.registered[self.addon._auto_start_server],
            {"first_interval": 0.0, "persistent": True},
        )

    def test_deferred_auto_start_starts_server_once_scene_exists(self):
        self.addon.register()
        scene = make_scene()
        self.bpy.context.scene = scene

        self.assertIsNone(self.addon._auto_start_server())

        (server,) = FakeServer.instances
        self.assertIs(self.bpy.types.blendermcp_server, server)
        self.assertEqual(server.port, 9876)
        self.assertTrue(server.running)
        self.assertTrue(scene.blendermcp_server_running)

    def test_deferred_auto_start_respects_disabled_preference(self):
        self.bpy.context.scene = make_scene(auto_start=False)

        self.assertIsNone(self.addon._auto_start_server())

        self.assertEqual(FakeServer.instances, [])
        self.assertFalse(hasattr(self.bpy.types, "blendermcp_server"))

    def test_deferred_auto_start_does_not_restart_running_server(self):
        running = FakeServer()
        running.running = True
        self.bpy.types.blendermcp_server = running
        self.bpy.context.scene = make_scene()

        self.addon._auto_start_server()

        self.assertEqual(FakeServer.instances, [running])
        self.assertEqual(running.start_calls, 0)

    def test_deferred_auto_start_reports_failure_without_raising(self):
        self.bpy.context.scene = scene = make_scene()

        with patch.object(self.addon, "BlenderMCPServer", FailingServer), patch(
            "builtins.print"
        ) as printed:
            self.assertIsNone(self.addon._auto_start_server())

        printed.assert_called_once_with("Blender MCP auto-start failed: port in use")
        self.assertFalse(scene.blendermcp_server_running)

    def test_unregister_cancels_pending_auto_start(self):
        self.addon.register()

        self.addon.unregister()

        self.assertFalse(
            self.bpy.app.timers.is_registered(self.addon._auto_start_server)
        )
        self.assertEqual(FakeServer.instances, [])


if __name__ == "__main__":
    unittest.main()
