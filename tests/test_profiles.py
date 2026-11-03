from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

profiles = __import__("profiles")


class ProfileRoot(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self._tmp.name) / "repo"
        self.repo.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_config(self, payload: dict) -> None:
        path = self.repo / ".trit/project.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")


class Defaults(ProfileRoot):
    def test_default_is_legacy_and_writes_nothing(self) -> None:
        resolution = profiles.resolve(self.repo, env={})
        self.assertEqual(resolution.profile, "legacy")
        self.assertEqual(resolution.effective["build"]["default"], "upstream")
        self.assertEqual(resolution.effective["gpu"]["backend"], "nvidia")
        self.assertEqual(resolution.errors, [])
        self.assertEqual(resolution.warnings, [])
        self.assertEqual(resolution.effective["math"]["mode"], "reference")
        self.assertEqual(
            resolution.effective["math"]["reference"], "https://github.com/openai/math"
        )
        self.assertFalse(resolution.effective["math"]["verified"])
        self.assertFalse((self.repo / ".trit").exists(), "show/check must not create .trit")

    def test_repo_profile_trit(self) -> None:
        self.write_config({"version": 1, "profile": "trit"})
        resolution = profiles.resolve(self.repo, env={})
        self.assertEqual(resolution.profile, "trit")
        self.assertEqual(resolution.effective["gpu"]["backend"], "trit")
        self.assertIn("gpu", resolution.effective["build"]["features"])
        self.assertTrue(any("experimental" in warning for warning in resolution.warnings))

    def test_env_overrides_repo(self) -> None:
        self.write_config({"version": 1, "profile": "trit"})
        resolution = profiles.resolve(self.repo, env={"TRIT_GPU_BACKEND": "nvidia"})
        self.assertEqual(resolution.effective["gpu"]["backend"], "nvidia")
        self.assertTrue(any("nvidia" in warning for warning in resolution.warnings))

    def test_cli_overrides_env(self) -> None:
        resolution = profiles.resolve(
            self.repo,
            env={"TRIT_GPU_BACKEND": "nvidia"},
            cli={"profile": "trit", "gpu_backend": "trit"},
        )
        self.assertEqual(resolution.effective["gpu"]["backend"], "trit")


class Alarms(ProfileRoot):
    def test_allow_nvidia_silences(self) -> None:
        self.write_config({"version": 1, "profile": "trit"})
        resolution = profiles.resolve(
            self.repo,
            env={"TRIT_GPU_BACKEND": "nvidia", "TRIT_ALLOW_NVIDIA": "true"},
        )
        self.assertFalse(any("nvidia" in warning for warning in resolution.warnings))

    def test_alarm_off_silences(self) -> None:
        self.write_config({"version": 1, "profile": "trit"})
        resolution = profiles.resolve(
            self.repo,
            env={"TRIT_GPU_BACKEND": "nvidia", "TRIT_ALARM": "false"},
        )
        self.assertFalse(any("nvidia" in warning for warning in resolution.warnings))

    def test_strict_turns_warning_into_failure(self) -> None:
        self.write_config({"version": 1, "profile": "trit"})
        resolution = profiles.resolve(
            self.repo,
            env={"TRIT_GPU_BACKEND": "nvidia"},
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = profiles.check(resolution, strict=True)
        self.assertEqual(code, 1)
        self.assertIn("strict", out.getvalue())

    def test_auto_resolves_by_profile(self) -> None:
        self.write_config(
            {"version": 1, "profile": "trit", "gpu": {"backend": "auto"}}
        )
        resolution = profiles.resolve(self.repo, env={})
        self.assertEqual(resolution.effective["gpu"]["backend"], "trit")
        self.assertEqual(resolution.sources["gpu.backend"], "auto(profile)")

    def test_math_dependency_requires_verification(self) -> None:
        self.write_config(
            {"version": 1, "math": {"mode": "dependency", "verified": False}}
        )
        resolution = profiles.resolve(self.repo, env={})
        self.assertTrue(
            any("verified=false" in warning for warning in resolution.warnings),
            resolution.warnings,
        )


class Validation(ProfileRoot):
    def test_unknown_profile_fails(self) -> None:
        self.write_config({"version": 1, "profile": "bogus"})
        resolution = profiles.resolve(self.repo, env={})
        self.assertTrue(resolution.errors)

    def test_unknown_key_fails(self) -> None:
        self.write_config({"version": 1, "profile": "legacy", "bogus": 1})
        resolution = profiles.resolve(self.repo, env={})
        self.assertTrue(any("未知字段" in error for error in resolution.errors))

    def test_gpu_trit_requires_feature(self) -> None:
        self.write_config(
            {
                "version": 1,
                "profile": "legacy",
                "build": {"features": []},
                "gpu": {"backend": "trit"},
            }
        )
        resolution = profiles.resolve(self.repo, env={})
        self.assertTrue(any("GPU 内核" in warning for warning in resolution.warnings))


class Init(ProfileRoot):
    def test_init_and_refuse_overwrite(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(profiles.init(self.repo, "legacy", force=False), 0)
            self.assertEqual(profiles.init(self.repo, "trit", force=False), 1)
            self.assertEqual(profiles.init(self.repo, "trit", force=True), 0)
        payload = json.loads((self.repo / ".trit/project.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["profile"], "trit")

    def test_init_does_not_touch_other_paths(self) -> None:
        other = pathlib.Path(self._tmp.name) / "other"
        other.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            profiles.init(self.repo, "legacy", force=False)
        self.assertFalse((other / ".trit").exists())
        self.assertFalse((self.repo / "Cargo.toml").exists())


class Cli(ProfileRoot):
    def test_show_cargo_format(self) -> None:
        self.write_config({"version": 1, "profile": "trit"})
        resolution = profiles.resolve(self.repo, env={})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = profiles.show(resolution, "cargo")
        self.assertEqual(code, 0)
        self.assertIn("gpu", out.getvalue())


if __name__ == "__main__":
    unittest.main()
