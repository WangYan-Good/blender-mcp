"""Load addon.py with small Blender stubs for host-side behavior tests."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
from unittest.mock import patch


def _property(*_args, **_kwargs):
    return None


def load_addon():
    root = pathlib.Path(__file__).parents[1]
    bpy = types.ModuleType("bpy")
    bpy.types = types.SimpleNamespace(
        AddonPreferences=object,
        Operator=object,
        Panel=object,
    )
    bpy.context = types.SimpleNamespace()
    bpy.app = types.SimpleNamespace()
    bpy.utils = types.SimpleNamespace()

    props = types.ModuleType("bpy.props")
    for name in (
        "BoolProperty",
        "EnumProperty",
        "FloatProperty",
        "IntProperty",
        "StringProperty",
    ):
        setattr(props, name, _property)
        setattr(bpy, "props", props)

    requests = types.ModuleType("requests")
    requests.utils = types.SimpleNamespace(default_headers=lambda: {})
    bmesh = types.ModuleType("bmesh")
    mathutils = types.ModuleType("mathutils")

    spec = importlib.util.spec_from_file_location(
        "blender_mcp_addon_under_test", root / "addon.py"
    )
    module = importlib.util.module_from_spec(spec)
    stubs = {
        "bpy": bpy,
        "bpy.props": props,
        "requests": requests,
        "bmesh": bmesh,
        "mathutils": mathutils,
    }
    with patch.dict(sys.modules, stubs):
        assert spec.loader is not None
        spec.loader.exec_module(module)
    return module
