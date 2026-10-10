#!/usr/bin/env python3
"""Test the X-Combo share / import / authoring wrappers.

These functions are thin on purpose: they build the path and body and hand off
to `get_authenticated_data`, which owns mTLS, request signing and token
refresh. So the thing worth testing is exactly that — the path, the method, and
the JSON body we send — without any network or vehicle involvement.

The helper is replaced with a spy, so nothing here can reach XPENG.

Run from the repo root:

    python3 tests/test_scene_api.py
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
import types
import unittest
from pathlib import Path


def _stub_cryptography() -> None:
    """Provide a placeholder `cryptography` if it isn't installed.

    xpeng_api imports cryptography at module scope, but the scene wrappers only
    build a path and a JSON body -- they never call into it. A permissive stub
    keeps this test runnable without pulling a crypto dependency onto the host.
    """

    try:
        import cryptography  # noqa: F401
        return
    except ImportError:
        pass

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    class _Any(types.ModuleType):
        def __getattr__(self, name):  # noqa: D105
            child = _Any(f"{self.__name__}.{name}")
            setattr(self, name, child)
            return child

    def fake_import(name, *args, **kwargs):
        if name == "cryptography" or name.startswith("cryptography."):
            return _Any(name)
        return real_import(name, *args, **kwargs)

    if isinstance(__builtins__, dict):
        __builtins__["__import__"] = fake_import
    else:
        __builtins__.__import__ = fake_import

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _component  # noqa: E402

API_PATH = _component.module_path("xpeng_api.py")


def load_api():
    """Load xpeng_api.py standalone (it has no Home Assistant imports).

    Its directory goes on sys.path first: xpeng_api imports its sibling
    xpeng_errors, and a module loaded by path has no package to be relative to.
    Without this the sibling import has nowhere to resolve from.
    """

    api_dir = str(API_PATH.parent)
    if api_dir not in sys.path:
        # Append, never insert: this directory contains select.py, image.py,
        # const.py and friends, which would shadow the standard library for
        # anything imported afterwards. It only needs to be importable, not to
        # take precedence.
        sys.path.append(api_dir)
    spec = importlib.util.spec_from_file_location("xpeng_api_under_test", API_PATH)
    module = importlib.util.module_from_spec(spec)
    # Register before exec: some libraries resolve the module by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_stub_cryptography()
api = load_api()


# Some X-Combo helpers exist only in the live/private tree, not (yet) in the
# shipping copy. Rather than let those classes die with an AttributeError -
# which reads as "the test is broken" rather than "this copy lacks the feature"
# - skip them and say why. Run with XPENG_COMPONENT=live to exercise them.
def _requires(*names):
    """Skip a test class when the selected copy lacks an API function."""

    def deco(cls):
        missing = [n for n in names if not hasattr(api, n)]
        if missing:
            return unittest.skip(
                f"not implemented in {_component.component_dir().name}: "
                f"{', '.join(missing)}"
            )(cls)
        return cls

    return deco


class Spy:
    """Stands in for get_authenticated_data and records the call."""

    def __init__(self, return_value=None):
        self.calls = []
        self.return_value = return_value if return_value is not None else {"ok": True}

    def __call__(self, config, tenant, tokens, path, method="GET", body="", **kw):
        self.calls.append(
            {"path": path, "method": method, "body": body, "kwargs": kw}
        )
        return self.return_value

    @property
    def last(self):
        return self.calls[-1]

    def body_json(self):
        return json.loads(self.last["body"]) if self.last["body"] else None


class SceneApiTestBase(unittest.TestCase):
    def setUp(self):
        self._real = api.get_authenticated_data
        self.spy = Spy()
        api.get_authenticated_data = self.spy

    def tearDown(self):
        api.get_authenticated_data = self._real

    def call(self, fn, *args, **kwargs):
        return fn(None, {"_key_pool": {}}, {"accessToken": "t"}, *args, **kwargs)


@_requires("share_xcombo_scene")
class TestShare(SceneApiTestBase):
    def test_path_and_method(self):
        self.call(api.share_xcombo_scene, "scene-1")
        self.assertEqual(self.spy.last["path"], "/personal-tailor/v3/scene/share")
        self.assertEqual(self.spy.last["method"], "POST")

    def test_body_carries_scene_id(self):
        self.call(api.share_xcombo_scene, "scene-1")
        self.assertEqual(self.spy.body_json(), {"sceneId": "scene-1"})

    def test_vin_included_when_given(self):
        self.call(api.share_xcombo_scene, "scene-1", vin="VIN123")
        self.assertEqual(self.spy.body_json(), {"sceneId": "scene-1", "vin": "VIN123"})

    def test_vin_omitted_when_blank(self):
        self.call(api.share_xcombo_scene, "scene-1", vin="")
        self.assertNotIn("vin", self.spy.body_json())

    def test_returns_helper_result(self):
        self.spy.return_value = {"sceneCode": "12345678"}
        out = self.call(api.share_xcombo_scene, "scene-1")
        self.assertEqual(out, {"sceneCode": "12345678"})


@_requires("inherit_xcombo_scene")
class TestInherit(SceneApiTestBase):
    def test_path_and_method(self):
        self.call(api.inherit_xcombo_scene, "12345678")
        self.assertEqual(self.spy.last["path"], "/personal-tailor/v3/scene/inheritScene")
        self.assertEqual(self.spy.last["method"], "POST")

    def test_body_carries_scene_code(self):
        self.call(api.inherit_xcombo_scene, "12345678")
        self.assertEqual(self.spy.body_json(), {"sceneCode": "12345678"})

    def test_leading_zero_code_is_preserved(self):
        # Real codes such as 04430024 exist; an int would lose the zero.
        self.call(api.inherit_xcombo_scene, "04430024")
        self.assertEqual(self.spy.body_json()["sceneCode"], "04430024")

    def test_integer_code_is_stringified(self):
        self.call(api.inherit_xcombo_scene, 12345678)
        body = self.spy.body_json()["sceneCode"]
        self.assertIsInstance(body, str)
        self.assertEqual(body, "12345678")

    def test_leading_zero_int_is_not_recoverable_but_does_not_crash(self):
        # Passing 4430024 cannot yield "04430024"; the type is at least stable.
        self.call(api.inherit_xcombo_scene, 4430024)
        self.assertEqual(self.spy.body_json()["sceneCode"], "4430024")


@_requires("get_xcombo_share_info")
class TestDetail(SceneApiTestBase):
    def test_share_info_path(self):
        self.call(api.get_xcombo_share_info, "scene-9")
        self.assertEqual(self.spy.last["path"], "/personal-tailor/v3/scene/detail")
        self.assertEqual(self.spy.body_json(), {"sceneId": "scene-9"})


@_requires("save_xcombo_scene")
class TestSave(SceneApiTestBase):
    def test_path_and_method(self):
        self.call(api.save_xcombo_scene, {"sceneName": "test"})
        self.assertEqual(self.spy.last["path"], "/personal-tailor/v3/scene/update")
        self.assertEqual(self.spy.last["method"], "POST")

    def test_payload_passed_through_verbatim(self):
        payload = {"sceneName": "x", "conditions": [{"type": "time", "value": "08:00"}]}
        self.call(api.save_xcombo_scene, payload)
        self.assertEqual(self.spy.body_json(), payload)

    def test_does_not_mutate_caller_dict(self):
        payload = {"sceneName": "x"}
        self.call(api.save_xcombo_scene, payload)
        self.assertEqual(payload, {"sceneName": "x"})

    def test_empty_payload_is_still_valid_json(self):
        self.call(api.save_xcombo_scene, {})
        self.assertEqual(self.spy.body_json(), {})


@_requires(
    "share_xcombo_scene",
    "inherit_xcombo_scene",
    "get_xcombo_share_info",
    "save_xcombo_scene",
)
class TestSignatures(unittest.TestCase):
    """The HA call convention is positional config/tenant/tokens; keep it.

    The assertions live in a method rather than the class body so the class can
    be skipped cleanly on a copy that does not implement these helpers; a body
    that raises AttributeError at import time cannot be skipped.
    """

    def test_ha_call_convention_is_positional(self):
        for fn in (
            api.share_xcombo_scene,
            api.inherit_xcombo_scene,
            api.get_xcombo_share_info,
            api.save_xcombo_scene,
        ):
            params = list(inspect.signature(fn).parameters)
            self.assertTrue(
                params[:4] == ["config", "tenant", "tokens", "scene_id"]
                or params[:3] == ["config", "tenant", "tokens"],
                f"{fn.__name__} has unexpected parameters: {params}",
            )

    def test_save_takes_a_scene_dict(self):
        params = list(inspect.signature(api.save_xcombo_scene).parameters)
        self.assertEqual(params[:4], ["config", "tenant", "tokens", "scene"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
