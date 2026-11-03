#!/usr/bin/env python3
"""按项目解析构建默认值：默认 legacy，三进制/自研 GPU 只能显式 opt-in。

设计约束：
- **不写全局**：只读 `<repo>/.trit/project.json`；`init` 也只写该文件。
- **默认不改变任何项目**：没有配置 = legacy = 上游构建 / NVIDIA 后端 / 旧文档管理。
- **优先级**：CLI > 环境变量 > 仓库配置 > 内置默认。
- **报警可关**：profile=trit 却用 NVIDIA 后端时默认警告；
  `gpu.allow_nvidia=true` 或 `gpu.alarm=false` 关闭。
- **成熟度闸门**：profile=trit 默认 `experimental`；`--strict` 下实验档直接判红。

用法：
    python scripts/profiles.py show [--repo PATH] [--format json|env|cargo]
    python scripts/profiles.py check [--repo PATH] [--strict]
    python scripts/profiles.py init [--repo PATH] [--profile legacy|hybrid|trit] [--force]
    python scripts/profiles.py --self-test
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import pathlib
import sys
from dataclasses import dataclass, field
from typing import Any

CONFIG_REL = pathlib.Path(".trit/project.json")
PROFILES = ("legacy", "hybrid", "trit")
MATURITY = ("experimental", "beta", "stable")
BUILD_DEFAULTS = ("upstream", "trit")
GPU_BACKENDS = ("nvidia", "trit", "auto")
DOCS_MANAGERS = ("legacy", "trit")
VCS_BACKENDS = ("git", "trit")
VCS_HOSTINGS = ("github", "gitlab", "local")
# 后端真实状态：写进判据文案，避免"设成 trit 就以为脱离了 git"
VCS_IMPLEMENTED = ("git",)  # 不在这里的就是"未实现"
MATH_MODES = ("reference", "dependency", "off")
MATH_REFERENCE = "https://github.com/openai/math"

PROFILE_DEFAULTS: dict[str, dict[str, Any]] = {
    "legacy": {
        "maturity": "stable",
        "build": {"default": "upstream", "features": []},
        "gpu": {"backend": "nvidia", "allow_nvidia": True, "alarm": True},
        "docs": {"manager": "legacy"},
        "vcs": {"backend": "git", "hosting": "github"},
        "math": {"reference": MATH_REFERENCE, "mode": "reference", "verified": False},
    },
    "hybrid": {
        "maturity": "beta",
        "build": {"default": "upstream", "features": ["gpu"]},
        "gpu": {"backend": "trit", "allow_nvidia": True, "alarm": True},
        "docs": {"manager": "trit"},
        "vcs": {"backend": "git", "hosting": "github"},
        "math": {"reference": MATH_REFERENCE, "mode": "reference", "verified": False},
    },
    "trit": {
        "maturity": "experimental",
        "build": {"default": "trit", "features": ["gpu"]},
        "gpu": {"backend": "trit", "allow_nvidia": False, "alarm": True},
        "docs": {"manager": "trit"},
        "vcs": {"backend": "git", "hosting": "github"},
        "math": {"reference": MATH_REFERENCE, "mode": "reference", "verified": False},
    },
}

TOP_KEYS = {"version", "profile", "maturity", "build", "gpu", "docs", "vcs", "math"}
NESTED_KEYS = {
    "build": {"default", "features"},
    "gpu": {"backend", "allow_nvidia", "alarm"},
    "docs": {"manager"},
    "vcs": {"backend", "hosting"},
    "math": {"reference", "mode", "verified"},
}
ENV_KEYS = {
    "TRIT_PROFILE": ("profile",),
    "TRIT_MATURITY": ("maturity",),
    "TRIT_BUILD_DEFAULT": ("build", "default"),
    "TRIT_BUILD_FEATURES": ("build", "features"),
    "TRIT_GPU_BACKEND": ("gpu", "backend"),
    "TRIT_ALLOW_NVIDIA": ("gpu", "allow_nvidia"),
    "TRIT_ALARM": ("gpu", "alarm"),
    "TRIT_VCS_BACKEND": ("vcs", "backend"),
    "TRIT_VCS_HOSTING": ("vcs", "hosting"),
    "TRIT_MATH_REFERENCE": ("math", "reference"),
    "TRIT_MATH_MODE": ("math", "mode"),
    "TRIT_MATH_VERIFIED": ("math", "verified"),
}


@dataclass
class Resolution:
    repo: pathlib.Path
    config_file: pathlib.Path | None
    profile: str
    effective: dict[str, Any]
    sources: dict[str, str]
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "repo": str(self.repo),
            "config_file": str(self.config_file) if self.config_file else None,
            "profile": self.profile,
            "effective": self.effective,
            "sources": self.sources,
            "warnings": self.warnings,
            "errors": self.errors,
        }


def parse_bool(raw: str, key: str) -> bool:
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{key} 不是布尔值：{raw!r}")


def load_repo_config(repo: pathlib.Path) -> tuple[dict[str, Any], pathlib.Path | None, list[str]]:
    path = repo / CONFIG_REL
    if not path.is_file():
        return {}, None, []
    errors: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {}, path, [f"{path} 解析失败：{exc}"]
    if not isinstance(data, dict):
        return {}, path, [f"{path} 顶层必须是对象"]
    if data.get("version") != 1:
        errors.append(f"{path} version 必须是 1，得到 {data.get('version')!r}")
    for key in data:
        if key not in TOP_KEYS:
            errors.append(f"{path} 未知字段：{key}")
    for section, allowed in NESTED_KEYS.items():
        value = data.get(section)
        if value is None:
            continue
        if not isinstance(value, dict):
            errors.append(f"{path} {section} 必须是对象")
            continue
        for key in value:
            if key not in allowed:
                errors.append(f"{path} {section}.{key} 未知字段")
    return data, path, errors


def set_field(
    effective: dict[str, Any],
    sources: dict[str, str],
    path: tuple[str, ...],
    value: Any,
    source: str,
) -> None:
    target = effective
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    sources[".".join(path)] = source


def get_nested(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    target: Any = data
    for key in path:
        if not isinstance(target, dict) or key not in target:
            return None
        target = target[key]
    return target


def validate(effective: dict[str, Any], sources: dict[str, str]) -> list[str]:
    errors: list[str] = []
    checks = [
        (("maturity",), MATURITY),
        (("build", "default"), BUILD_DEFAULTS),
        (("gpu", "backend"), GPU_BACKENDS),
        (("docs", "manager"), DOCS_MANAGERS),
        (("vcs", "backend"), VCS_BACKENDS),
        (("vcs", "hosting"), VCS_HOSTINGS),
        (("math", "mode"), MATH_MODES),
    ]
    for path, allowed in checks:
        value = get_nested(effective, path)
        if value not in allowed:
            errors.append(f"{'.'.join(path)} 非法：{value!r}（允许 {', '.join(allowed)}）")
        else:
            sources.setdefault(".".join(path), "profile")
    features = effective["build"]["features"]
    if not isinstance(features, list) or not all(isinstance(item, str) and item for item in features):
        errors.append("build.features 必须是非空字符串数组")
    for path in (("gpu", "allow_nvidia"), ("gpu", "alarm"), ("math", "verified")):
        if not isinstance(get_nested(effective, path), bool):
            errors.append(f"{'.'.join(path)} 必须是布尔值")
    if effective["math"]["mode"] != "off" and not str(effective["math"]["reference"]).strip():
        errors.append("math.reference 不能为空（除非 math.mode=off）")
    return errors


def resolve(
    repo: pathlib.Path,
    *,
    cli: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> Resolution:
    cli = cli or {}
    env = env if env is not None else dict(os.environ)
    repo_data, config_file, errors = load_repo_config(repo)

    profile = "legacy"
    profile_source = "default"
    for value, source in (
        (repo_data.get("profile"), "repo"),
        (env.get("TRIT_PROFILE"), "env"),
        (cli.get("profile"), "cli"),
    ):
        if value is not None:
            profile = str(value)
            profile_source = source
    if profile not in PROFILES:
        errors.append(f"profile 非法：{profile!r}（允许 {', '.join(PROFILES)}）")
        profile = "legacy"
        profile_source = "fallback"

    effective = copy.deepcopy(PROFILE_DEFAULTS[profile])
    sources: dict[str, str] = {
        "profile": profile_source,
        "maturity": "profile",
        "build.default": "profile",
        "build.features": "profile",
        "gpu.backend": "profile",
        "gpu.allow_nvidia": "profile",
        "gpu.alarm": "profile",
        "docs.manager": "profile",
        "vcs.backend": "profile",
        "vcs.hosting": "profile",
        "math.reference": "profile",
        "math.mode": "profile",
        "math.verified": "profile",
    }

    # 仓库配置
    for path in (
        ("maturity",),
        ("build", "default"),
        ("build", "features"),
        ("gpu", "backend"),
        ("gpu", "allow_nvidia"),
        ("gpu", "alarm"),
        ("docs", "manager"),
        ("vcs", "backend"),
        ("vcs", "hosting"),
        ("math", "reference"),
        ("math", "mode"),
        ("math", "verified"),
    ):
        value = get_nested(repo_data, path)
        if value is not None:
            set_field(effective, sources, path, value, "repo")

    # 环境变量
    for key, path in ENV_KEYS.items():
        raw = env.get(key)
        if raw is None:
            continue
        try:
            if path == ("build", "features"):
                value = [item.strip() for item in raw.split(",") if item.strip()]
            elif path in (("gpu", "allow_nvidia"), ("gpu", "alarm"), ("math", "verified")):
                value = parse_bool(raw, key)
            else:
                value = raw
        except ValueError as exc:
            errors.append(str(exc))
            continue
        set_field(effective, sources, path, value, "env")

    # CLI 覆盖
    for key, path in (
        ("profile", ("profile",)),
        ("maturity", ("maturity",)),
        ("build_default", ("build", "default")),
        ("build_features", ("build", "features")),
        ("gpu_backend", ("gpu", "backend")),
        ("allow_nvidia", ("gpu", "allow_nvidia")),
        ("alarm", ("gpu", "alarm")),
        ("docs_manager", ("docs", "manager")),
        ("vcs_backend", ("vcs", "backend")),
        ("vcs_hosting", ("vcs", "hosting")),
        ("math_reference", ("math", "reference")),
        ("math_mode", ("math", "mode")),
        ("math_verified", ("math", "verified")),
    ):
        value = cli.get(key)
        if value is None:
            continue
        if path == ("profile",):
            if value not in PROFILES:
                errors.append(f"profile 非法：{value!r}")
                continue
            profile = str(value)
            sources["profile"] = "cli"
            effective = copy.deepcopy(PROFILE_DEFAULTS[profile])
            for reset in (
                ("maturity",),
                ("build", "default"),
                ("build", "features"),
                ("gpu", "backend"),
                ("gpu", "allow_nvidia"),
                ("gpu", "alarm"),
                ("docs", "manager"),
                ("vcs", "backend"),
                ("vcs", "hosting"),
                ("math", "reference"),
                ("math", "mode"),
                ("math", "verified"),
            ):
                sources[".".join(reset)] = "profile"
            # 重新叠加仓库/环境（CLI profile 只替换 profile 本身）
            for source_name, data in (("repo", repo_data),):
                for path2 in (
                    ("maturity",),
                    ("build", "default"),
                    ("build", "features"),
                    ("gpu", "backend"),
                    ("gpu", "allow_nvidia"),
                    ("gpu", "alarm"),
                    ("docs", "manager"),
                    ("vcs", "backend"),
                    ("vcs", "hosting"),
                    ("math", "reference"),
                    ("math", "mode"),
                    ("math", "verified"),
                ):
                    value2 = get_nested(data, path2)
                    if value2 is not None:
                        set_field(effective, sources, path2, value2, source_name)
            for key2, path2 in ENV_KEYS.items():
                raw2 = env.get(key2)
                if raw2 is None:
                    continue
                try:
                    if path2 == ("build", "features"):
                        value2 = [item.strip() for item in raw2.split(",") if item.strip()]
                    elif path2 in (("gpu", "allow_nvidia"), ("gpu", "alarm"), ("math", "verified")):
                        value2 = parse_bool(raw2, key2)
                    else:
                        value2 = raw2
                except ValueError as exc:
                    errors.append(str(exc))
                    continue
                set_field(effective, sources, path2, value2, "env")
            continue
        set_field(effective, sources, path, value, "cli")

    errors.extend(validate(effective, sources))

    # auto 由 profile 决定，但保留“这是自动”的来源
    if effective["gpu"]["backend"] == "auto":
        effective["gpu"]["backend"] = "trit" if profile in ("hybrid", "trit") else "nvidia"
        sources["gpu.backend"] = "auto(profile)"

    warnings: list[str] = []
    using_trit = (
        effective["build"]["default"] == "trit"
        or effective["gpu"]["backend"] == "trit"
        or effective["docs"]["manager"] == "trit"
    )
    # 传输/托管面：现在只有 git。要说清"哪些面已经换了、哪些面没换"，
    # 而不是让 git 这件事隐形地当默认。
    if effective["vcs"]["backend"] == "trit":
        errors.append(
            "vcs.backend=trit 未实现（已实现的只有 "
            + "/".join(VCS_IMPLEMENTED)
            + "）：没有变更集存储、没有 diff/合并、没有脱离 GitHub 的 CI 宿主；"
            "snapshot.py 只按字节回滚本地文件，不是 git 的替代"
        )
    if profile == "trit" and effective["maturity"] != "stable":
        warnings.append(
            f"profile=trit 仍是 {effective['maturity']}；成熟前请逐项目 opt-in，不要全局默认"
        )
    if (
        using_trit
        and effective["gpu"]["backend"] == "nvidia"
        and effective["gpu"]["alarm"]
        and not effective["gpu"]["allow_nvidia"]
    ):
        warnings.append(
            "已启用三进制但 gpu.backend=nvidia；默认报警。"
            "设 gpu.allow_nvidia=true 或 gpu.alarm=false 关闭"
        )
    if effective["gpu"]["backend"] == "trit" and "gpu" not in effective["build"]["features"]:
        warnings.append("gpu.backend=trit 但 build.features 没有 gpu；构建不会编进 GPU 内核")
    if (
        using_trit
        and effective["vcs"]["backend"] == "git"
    ):
        warnings.append(
            f"协调面已换 trit（认领/回执/交接包在 trit-orch），"
            f"传输与托管面仍是 git/{effective['vcs']['hosting']}——这是现状不是遗漏；"
            "vcs.backend=trit 会判红，直到真出现替代实现"
        )
    if effective["math"]["mode"] == "dependency" and not effective["math"]["verified"]:
        warnings.append(
            "openai/math 标为 dependency 但 verified=false；先改 reference 或完成验证"
        )

    return Resolution(
        repo=repo,
        config_file=config_file,
        profile=profile,
        effective=effective,
        sources=sources,
        warnings=warnings,
        errors=errors,
    )


def show(resolution: Resolution, fmt: str) -> int:
    if resolution.errors:
        for error in resolution.errors:
            print(f"FAIL {error}")
        return 1
    if fmt == "json":
        print(json.dumps(resolution.as_json(), ensure_ascii=False, indent=2))
    elif fmt == "env":
        print(f"TRIT_PROFILE={resolution.profile}")
        print(f"TRIT_MATURITY={resolution.effective['maturity']}")
        print(f"TRIT_BUILD_DEFAULT={resolution.effective['build']['default']}")
        print(f"TRIT_VCS_BACKEND={resolution.effective['vcs']['backend']}")
        print(f"TRIT_VCS_HOSTING={resolution.effective['vcs']['hosting']}")
        print(f"TRIT_BUILD_FEATURES={','.join(resolution.effective['build']['features'])}")
        print(f"TRIT_GPU_BACKEND={resolution.effective['gpu']['backend']}")
        print(f"TRIT_ALLOW_NVIDIA={str(resolution.effective['gpu']['allow_nvidia']).lower()}")
        print(f"TRIT_ALARM={str(resolution.effective['gpu']['alarm']).lower()}")
        print(f"TRIT_DOCS_MANAGER={resolution.effective['docs']['manager']}")
        print(f"TRIT_MATH_REFERENCE={resolution.effective['math']['reference']}")
        print(f"TRIT_MATH_MODE={resolution.effective['math']['mode']}")
        print(f"TRIT_MATH_VERIFIED={str(resolution.effective['math']['verified']).lower()}")
    elif fmt == "cargo":
        print(",".join(resolution.effective["build"]["features"]))
    else:
        print(f"FAIL 未知格式：{fmt}")
        return 2
    for warning in resolution.warnings:
        print(f"WARN {warning}", file=sys.stderr)
    return 0


def check(resolution: Resolution, strict: bool) -> int:
    for error in resolution.errors:
        print(f"FAIL {error}")
    for warning in resolution.warnings:
        print(f"WARN {warning}")
    if resolution.errors:
        print(f"结果：FAIL（{len(resolution.errors)} 项错误）")
        return 1
    if strict and resolution.warnings:
        print(f"结果：FAIL（strict，{len(resolution.warnings)} 项警告）")
        return 1
    print(f"结果：PASS（profile={resolution.profile}，{len(resolution.warnings)} 项警告）")
    return 0


def init(repo: pathlib.Path, profile: str, force: bool) -> int:
    if profile not in PROFILES:
        print(f"FAIL profile 非法：{profile}", file=sys.stderr)
        return 2
    path = repo / CONFIG_REL
    if path.exists() and not force:
        print(f"FAIL 已存在：{path}（要覆盖请显式 --force）", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "profile": profile}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(path)
    return 0


def overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    features = None
    if args.build_features is not None:
        features = [item.strip() for item in args.build_features.split(",") if item.strip()]
    return {
        "profile": args.profile,
        "maturity": args.maturity,
        "build_default": args.build_default,
        "build_features": features,
        "gpu_backend": args.gpu_backend,
        "allow_nvidia": args.allow_nvidia,
        "alarm": args.alarm,
        "docs_manager": args.docs_manager,
        "vcs_backend": args.vcs_backend,
        "vcs_hosting": args.vcs_hosting,
        "math_reference": args.math_reference,
        "math_mode": args.math_mode,
        "math_verified": args.math_verified,
    }


def add_overrides(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", choices=PROFILES)
    parser.add_argument("--maturity", choices=MATURITY)
    parser.add_argument("--build-default", choices=BUILD_DEFAULTS)
    parser.add_argument("--build-features", help="逗号分隔，例如 gpu")
    parser.add_argument("--gpu-backend", choices=GPU_BACKENDS)
    parser.add_argument(
        "--allow-nvidia",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="允许三进制项目继续用 NVIDIA 后端（关闭默认报警）",
    )
    parser.add_argument(
        "--alarm",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="三进制 + NVIDIA 时是否报警",
    )
    parser.add_argument("--docs-manager", choices=DOCS_MANAGERS)
    parser.add_argument("--vcs-backend", choices=VCS_BACKENDS)
    parser.add_argument("--vcs-hosting", choices=VCS_HOSTINGS)
    parser.add_argument("--math-reference", help="默认 https://github.com/openai/math")
    parser.add_argument("--math-mode", choices=MATH_MODES)
    parser.add_argument(
        "--math-verified",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="openai/math 是否已人工验证（默认 false）",
    )


def self_test() -> int:
    import tempfile

    failures: list[str] = []

    def case(name: str, fn) -> None:
        try:
            fn()
        except AssertionError as exc:
            failures.append(f"{name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - self-test must report, not crash
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    with tempfile.TemporaryDirectory() as temp:
        root = pathlib.Path(temp)
        repo = root / "repo"
        repo.mkdir()

        def default_legacy() -> None:
            res = resolve(repo, env={})
            assert res.profile == "legacy", res.profile
            assert res.effective["build"]["default"] == "upstream"
            assert res.effective["gpu"]["backend"] == "nvidia"
            assert not res.errors
            assert not res.warnings

        def math_default_reference() -> None:
            res = resolve(repo, env={})
            assert res.effective["math"]["mode"] == "reference"
            assert res.effective["math"]["reference"] == "https://github.com/openai/math"
            assert res.effective["math"]["verified"] is False

        def math_dependency_warns() -> None:
            res = resolve(repo, env={}, cli={"math_mode": "dependency"})
            assert any("verified=false" in warning for warning in res.warnings), res.warnings

        def repo_trit() -> None:
            (repo / ".trit").mkdir()
            (repo / ".trit/project.json").write_text(
                json.dumps({"version": 1, "profile": "trit"}), encoding="utf-8"
            )
            res = resolve(repo, env={})
            assert res.profile == "trit"
            assert res.effective["gpu"]["backend"] == "trit"
            assert "gpu" in res.effective["build"]["features"]
            assert any("experimental" in w for w in res.warnings), res.warnings

        def env_overrides_repo() -> None:
            res = resolve(repo, env={"TRIT_GPU_BACKEND": "nvidia"})
            assert res.effective["gpu"]["backend"] == "nvidia"
            assert any("nvidia" in w for w in res.warnings), res.warnings

        def allow_nvidia_silences() -> None:
            res = resolve(repo, env={"TRIT_GPU_BACKEND": "nvidia", "TRIT_ALLOW_NVIDIA": "true"})
            assert not any("nvidia" in w for w in res.warnings), res.warnings

        def alarm_off_silences() -> None:
            res = resolve(repo, env={"TRIT_GPU_BACKEND": "nvidia", "TRIT_ALARM": "false"})
            assert not any("nvidia" in w for w in res.warnings), res.warnings

        def cli_overrides_env() -> None:
            res = resolve(
                repo,
                env={"TRIT_GPU_BACKEND": "nvidia"},
                cli={"gpu_backend": "trit", "allow_nvidia": False},
            )
            assert res.effective["gpu"]["backend"] == "trit"

        def unknown_profile() -> None:
            (repo / ".trit/project.json").write_text(
                json.dumps({"version": 1, "profile": "bogus"}), encoding="utf-8"
            )
            res = resolve(repo, env={})
            assert res.errors, "unknown profile must fail"

        def unknown_key() -> None:
            (repo / ".trit/project.json").write_text(
                json.dumps({"version": 1, "profile": "legacy", "bogus": 1}), encoding="utf-8"
            )
            res = resolve(repo, env={})
            assert any("未知字段" in e for e in res.errors), res.errors

        def init_and_refuse() -> None:
            import contextlib
            import io

            target = root / "init-repo"
            target.mkdir()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                assert init(target, "legacy", force=False) == 0
                assert (target / CONFIG_REL).is_file()
                assert init(target, "trit", force=False) == 1
                assert init(target, "trit", force=True) == 0
            assert json.loads((target / CONFIG_REL).read_text(encoding="utf-8"))["profile"] == "trit"

        def auto_resolution() -> None:
            (root / "auto-repo").mkdir()
            (root / "auto-repo/.trit").mkdir()
            (root / "auto-repo/.trit/project.json").write_text(
                json.dumps({"version": 1, "profile": "trit", "gpu": {"backend": "auto"}}),
                encoding="utf-8",
            )
            res = resolve(root / "auto-repo", env={})
            assert res.effective["gpu"]["backend"] == "trit"
            assert res.sources["gpu.backend"] == "auto(profile)"

        def vcs_is_explicitly_git() -> None:
            res = resolve(root / "vcs-repo-default", env={})
            assert res.effective["vcs"] == {"backend": "git", "hosting": "github"}, res.effective["vcs"]
            assert not res.errors, res.errors
            assert res.sources["vcs.backend"] == "profile", res.sources["vcs.backend"]

        def vcs_trit_profile_still_uses_git() -> None:
            # profile=trit 也不得悄悄把传输面说成已替代：默认仍是 git，且必须出声
            res = resolve(root / "vcs-repo-trit", env={}, cli={"profile": "trit"})
            assert res.effective["vcs"]["backend"] == "git", res.effective["vcs"]
            assert any("传输与托管面仍是 git" in w for w in res.warnings), res.warnings

        def vcs_backend_trit_is_an_error_not_a_warning() -> None:
            res = resolve(root / "vcs-repo-trit", env={}, cli={"vcs_backend": "trit"})
            assert any("未实现" in e for e in res.errors), (res.errors, res.warnings)
            # 判红不许降级成警告：降级等于"设了就以为脱离了 git"
            assert not any("未实现" in w for w in res.warnings), res.warnings

        def vcs_repo_and_env_override() -> None:
            # 优先级链必须真的覆盖到 vcs 这一段（少了某一处元组，这里就退不回仓库配置）
            over = root / "vcs-repo-over"
            (over / ".trit").mkdir(parents=True)
            (over / ".trit/project.json").write_text(
                json.dumps({"version": 1, "profile": "legacy", "vcs": {"hosting": "gitlab"}}),
                encoding="utf-8",
            )
            res = resolve(over, env={})
            assert res.effective["vcs"]["hosting"] == "gitlab", res.effective["vcs"]
            assert res.sources["vcs.hosting"] == "repo", res.sources["vcs.hosting"]
            res2 = resolve(over, env={"TRIT_VCS_HOSTING": "local"})
            assert res2.effective["vcs"]["hosting"] == "local", res2.effective["vcs"]
            assert res2.sources["vcs.hosting"] == "env", res2.sources["vcs.hosting"]

        def vcs_hosting_unknown_fails() -> None:
            res = resolve(root / "vcs-repo-default", env={"TRIT_VCS_HOSTING": "svn"})
            assert any("vcs.hosting 非法" in e for e in res.errors), res.errors

        (root / "vcs-repo-default").mkdir()
        (root / "vcs-repo-trit").mkdir()

        case("vcs default is explicit git", vcs_is_explicitly_git)
        case("profile=trit keeps git transport", vcs_trit_profile_still_uses_git)
        case("vcs.backend=trit fails closed", vcs_backend_trit_is_an_error_not_a_warning)
        case("vcs.hosting unknown fails", vcs_hosting_unknown_fails)
        case("vcs repo/env override", vcs_repo_and_env_override)
        case("default legacy", default_legacy)
        case("math default reference", math_default_reference)
        case("math dependency warns", math_dependency_warns)
        case("repo trit", repo_trit)
        case("env overrides repo", env_overrides_repo)
        case("allow_nvidia silences", allow_nvidia_silences)
        case("alarm off silences", alarm_off_silences)
        case("cli overrides env", cli_overrides_env)
        case("unknown profile fails", unknown_profile)
        case("unknown key fails", unknown_key)
        case("init refuses overwrite", init_and_refuse)
        case("auto resolves by profile", auto_resolution)

    if failures:
        for failure in failures:
            print(f"FAIL self-test {failure}")
        print(f"self-test FAIL（{len(failures)} 项）")
        return 1
    print("PASS self-test：17 个用例（默认/覆盖/报警/profile/初始化/auto/数学参考/vcs 传输面）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    sub = parser.add_subparsers(dest="command")

    show_parser = sub.add_parser("show", help="打印有效配置")
    show_parser.add_argument("--repo", default=".")
    show_parser.add_argument("--format", choices=("json", "env", "cargo"), default="json")
    add_overrides(show_parser)

    check_parser = sub.add_parser("check", help="检查配置；strict 下警告也判红")
    check_parser.add_argument("--repo", default=".")
    check_parser.add_argument("--strict", action="store_true")
    add_overrides(check_parser)

    init_parser = sub.add_parser("init", help="写 <repo>/.trit/project.json")
    init_parser.add_argument("--repo", default=".")
    init_parser.add_argument("--profile", choices=PROFILES, default="legacy")
    init_parser.add_argument("--force", action="store_true")

    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not args.command:
        parser.print_help()
        return 2

    repo = pathlib.Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"FAIL --repo 不是目录：{repo}", file=sys.stderr)
        return 2

    if args.command == "init":
        return init(repo, args.profile, args.force)

    resolution = resolve(repo, cli=overrides_from_args(args), env=dict(os.environ))
    if args.command == "show":
        return show(resolution, args.format)
    return check(resolution, args.strict)


if __name__ == "__main__":
    sys.exit(main())
