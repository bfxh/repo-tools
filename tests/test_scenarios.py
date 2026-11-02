"""场景测试：不变量 + 边界 + 组合矩阵，跑在两个平台上都要一致。

原则（吃过亏的地方）：
  - 空输入、只有空文件、二进制、CRLF、Unicode 名字都要能逐字节往返；
  - 同一操作跑两遍结果必须相同（确定性）；
  - 失败路径必须真的把状态还原，而不是"看起来成功了"；
  - 每个工具的自测输出两次跑要一致（只允许快照 id 这种时间戳差异）。
"""

from __future__ import annotations

import importlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

snapshot = importlib.import_module("snapshot")
autoflow = importlib.import_module("autoflow")
gitflow = importlib.import_module("gitflow")
vendor = importlib.import_module("vendor")
deps = importlib.import_module("deps")


def run_tool(name: str) -> str:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / f"{name}.py"), "--self-test"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout


def normalized_snapshot_id(text: str) -> str:
    text = re.sub(r"\d{8}T\d{6}Z-[0-9a-f]{4}", "<id>", text)
    return re.sub(r"tmp[A-Za-z0-9_]+", "<tmp>", text)


class TempRoot(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, rel: str, data: bytes) -> pathlib.Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


class SnapshotScenarios(TempRoot):
    def snap(self, label: str = "") -> str:
        return snapshot.save(self.root, label)

    def restore(self, snap_id: str, prune: bool = False, dry: bool = False) -> int:
        return snapshot.restore(self.root, snapshot.resolve(self.root, snap_id), prune=prune, dry_run=dry)

    def test_empty_root_roundtrip(self) -> None:
        snap_id = self.snap()
        self.assertEqual(snapshot.verify(self.root, snapshot.resolve(self.root, snap_id)), 0)
        self.write("later.txt", b"x")
        self.assertEqual(snapshot.verify(self.root, snapshot.resolve(self.root, snap_id)), 1)
        self.restore(snap_id, prune=True)
        self.assertFalse((self.root / "later.txt").exists())
        self.assertEqual(snapshot.verify(self.root, snapshot.resolve(self.root, snap_id)), 0)

    def test_empty_and_binary_files_roundtrip(self) -> None:
        payloads = {"empty.bin": b"", "zeros.bin": b"\x00" * 4096, "blob.bin": bytes(range(256)) * 4}
        for name, data in payloads.items():
            self.write(name, data)
        snap_id = self.snap()
        for name, data in payloads.items():
            self.write(name, b"tampered")
        self.restore(snap_id)
        for name, data in payloads.items():
            self.assertEqual((self.root / name).read_bytes(), data, name)

    def test_duplicate_content_is_deduplicated(self) -> None:
        self.write("a.txt", b"same")
        self.write("b.txt", b"same")
        self.snap()
        objects = [p for p in (self.root / ".trit" / "objects").rglob("*") if p.is_file()]
        self.assertEqual(len(objects), 1)

    def test_unicode_and_space_names(self) -> None:
        names = ["中文 文件.txt", "dir with space/in ner.txt", "emoji-🙂.md"]
        for name in names:
            self.write(name, b"payload")
        snap_id = self.snap()
        for name in names:
            (self.root / name).unlink()
        self.restore(snap_id)
        for name in names:
            self.assertEqual((self.root / name).read_bytes(), b"payload")

    def test_deep_nesting_and_many_files(self) -> None:
        deep = "/".join(["d"] * 20) + "/leaf.txt"
        self.write(deep, b"deep")
        for index in range(200):
            self.write(f"bulk/{index // 25}/f{index}.txt", f"{index}".encode())
        snap_id = self.snap()
        (self.root / "bulk").rename(self.root / "bulk-moved")
        self.restore(snap_id, prune=True)
        self.assertEqual((self.root / deep).read_bytes(), b"deep")
        for index in (0, 99, 199):
            self.assertEqual((self.root / f"bulk/{index // 25}/f{index}.txt").read_bytes(), str(index).encode())
        self.assertFalse((self.root / "bulk-moved").exists())

    def test_excluded_directories_never_enter_snapshot(self) -> None:
        for name in ("target/x.bin", "node_modules/m.js", ".trit/keep", "__pycache__/p.pyc"):
            self.write(name, b"junk")
        self.write("src/keep.rs", b"code")
        self.snap()
        manifest = json.loads(next((self.root / ".trit" / "snapshots").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(list(manifest["files"]), ["src/keep.rs"])

    def test_restore_matrix_prune_and_dry_run(self) -> None:
        # 四种组合逐一验证：dry-run 不许改；prune 决定新增文件去留
        for prune in (False, True):
            for dry in (False, True):
                with self.subTest(prune=prune, dry=dry):
                    shutil.rmtree(self.root / ".trit", ignore_errors=True)
                    shutil.rmtree(self.root / "src", ignore_errors=True)
                    self.write("src/a.txt", b"orig")
                    snap_id = self.snap()
                    self.write("src/a.txt", b"changed")
                    self.write("src/new.txt", b"new")
                    self.restore(snap_id, prune=prune, dry=dry)
                    if dry:
                        self.assertEqual((self.root / "src/a.txt").read_bytes(), b"changed")
                        self.assertTrue((self.root / "src/new.txt").exists())
                    else:
                        self.assertEqual((self.root / "src/a.txt").read_bytes(), b"orig")
                        self.assertEqual((self.root / "src/new.txt").exists(), not prune)

    def test_restore_is_idempotent(self) -> None:
        self.write("a.txt", b"orig")
        snap_id = self.snap()
        self.write("a.txt", b"changed")
        self.write("b.txt", b"new")
        self.restore(snap_id, prune=True)
        first = {p.name: p.read_bytes() for p in self.root.rglob("*") if p.is_file() and ".trit" not in p.parts}
        self.restore(snap_id, prune=True)
        second = {p.name: p.read_bytes() for p in self.root.rglob("*") if p.is_file() and ".trit" not in p.parts}
        self.assertEqual(first, second)

    def test_two_snapshots_are_independent(self) -> None:
        self.write("a.txt", b"v1")
        first = self.snap("v1")
        self.write("a.txt", b"v2")
        second = self.snap("v2")
        self.restore(first)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"v1")
        self.assertEqual(snapshot.verify(self.root, snapshot.resolve(self.root, second)), 1)

    def test_renamed_file_is_missing_plus_extra(self) -> None:
        self.write("old.txt", b"x")
        snap_id = self.snap()
        (self.root / "old.txt").rename(self.root / "new.txt")
        plan = snapshot.diff(self.root, snapshot.resolve(self.root, snap_id))
        self.assertEqual(plan["missing"], ["old.txt"])
        self.assertEqual(plan["extra"], ["new.txt"])

    def test_tampered_object_is_rejected(self) -> None:
        self.write("a.txt", b"orig")
        snap_id = self.snap()
        manifest = snapshot.resolve(self.root, snap_id)
        blob = snapshot.blob_path(self.root, manifest["files"]["a.txt"]["sha"])
        blob.write_bytes(b"tampered")
        self.write("a.txt", b"changed")
        with self.assertRaises(SystemExit):
            self.restore(snap_id)

    def test_symlink_is_skipped(self) -> None:
        self.write("real.txt", b"x")
        try:
            os.symlink(self.root / "real.txt", self.root / "link.txt")
        except (OSError, NotImplementedError):
            self.skipTest("本平台不允许普通用户建符号链接")
        snap_id = self.snap()
        manifest = snapshot.resolve(self.root, snap_id)
        self.assertNotIn("link.txt", manifest["files"])


    def test_invalid_snapshot_id_is_rejected(self) -> None:
        self.write("a.txt", b"x")
        self.snap()
        with self.assertRaises(SystemExit) as caught:
            snapshot.resolve(self.root, "../evil")
        self.assertIn("不合法", str(caught.exception))

    @unittest.skipIf(os.name == "nt", "Windows 不支持 POSIX 可执行位")
    def test_executable_bit_roundtrip(self) -> None:
        script = self.write("run.sh", b"#!/bin/sh\n")
        os.chmod(script, 0o755)
        snap_id = self.snap()
        os.chmod(script, 0o644)
        self.restore(snap_id)
        self.assertEqual(script.stat().st_mode & 0o777, 0o755)


    def test_gc_keeps_recent_and_restorable(self) -> None:
        ids = []
        for index in range(4):
            self.write("g.txt", str(index).encode())
            ids.append(self.snap(f"g{index}"))
        before = sorted(p.name for p in (self.root / ".trit" / "objects").rglob("*") if p.is_file())
        snapshot.gc(self.root, keep=2)
        after = sorted(p.name for p in (self.root / ".trit" / "objects").rglob("*") if p.is_file())
        self.assertEqual([s["id"] for s in snapshot.list_snapshots(self.root)], ids[-2:])
        self.assertLess(len(after), len(before))
        self.write("g.txt", b"tampered")
        self.restore(ids[-1], prune=True)
        self.assertEqual((self.root / "g.txt").read_bytes(), b"3")


class AutoflowScenarios(TempRoot):
    def run_tool(self, argv: list[str], keep: bool = False) -> int:
        return autoflow.run(self.root, argv, "", keep)

    def test_success_keeps_changes(self) -> None:
        self.write("a.txt", b"orig")
        code = self.run_tool([sys.executable, "-c", "open('a.txt','wb').write(b'new')"])
        self.assertEqual(code, 0)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"new")

    def test_failure_without_changes_preserves_state(self) -> None:
        self.write("a.txt", b"orig")
        code = self.run_tool([sys.executable, "-c", "raise SystemExit(3)"])
        self.assertEqual(code, 3)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"orig")

    def test_failure_removes_created_tree(self) -> None:
        self.write("a.txt", b"orig")
        code = self.run_tool(
            [sys.executable, "-c", "import os;os.makedirs('x/y',exist_ok=True);open('x/y/z','wb').write(b'1');raise SystemExit(1)"]
        )
        self.assertEqual(code, 1)
        self.assertFalse((self.root / "x").exists())

    def test_failure_restores_deleted_file(self) -> None:
        self.write("a.txt", b"orig")
        code = self.run_tool([sys.executable, "-c", "import os;os.remove('a.txt');raise SystemExit(4)"])
        self.assertEqual(code, 4)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"orig")

    def test_keep_on_fail_keeps_scene(self) -> None:
        self.write("a.txt", b"orig")
        code = self.run_tool([sys.executable, "-c", "open('a.txt','wb').write(b'kept');raise SystemExit(2)"], keep=True)
        self.assertEqual(code, 2)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"kept")

    def test_missing_command_is_a_failure(self) -> None:
        code = self.run_tool(["definitely-not-a-real-command-xyz"])
        self.assertNotEqual(code, 0)

    def test_repeat_is_deterministic(self) -> None:
        self.write("a.txt", b"orig")
        argv = [sys.executable, "-c", "open('a.txt','wb').write(b'x');open('n.txt','wb').write(b'1');raise SystemExit(5)"]
        self.assertEqual(self.run_tool(argv), 5)
        first = (self.root / "a.txt").read_bytes()
        self.assertEqual(self.run_tool(argv), 5)
        self.assertEqual((self.root / "a.txt").read_bytes(), first)
        self.assertFalse((self.root / "n.txt").exists())


class GitflowScenarios(TempRoot):
    def test_secret_pattern_sweep(self) -> None:
        samples = [
            ('K = "sk-' + "a" * 20 + '"', True),
            ('K = "sk-short"', False),
            ("gh" + "p_" + "a" * 36, True),
            ("AK" + "IA" + "A" * 16, True),
            ("xox" + "b-" + "1" * 12, True),
            ("-----BEGIN " + "RSA PRIVATE KEY-----", True),
            ("ordinary prose about secrets", False),
            ("sk-" + "a" * 15, False),
        ]
        for text, expected in samples:
            with self.subTest(text=text[:24]):
                self.write("f.txt", text.encode())
                hits = gitflow.secret_scan(self.root, ["f.txt"])
                self.assertEqual(bool(hits), expected, hits)

    def test_sensitive_file_names(self) -> None:
        for name in (".env", ".env.production", "credentials.json", "id_rsa"):
            with self.subTest(name=name):
                self.write(name, b"clean text")
                self.assertTrue(gitflow.secret_scan(self.root, [name]))

    def test_path_escape_sweep(self) -> None:
        cases = {"ok.txt": False, "../up.txt": True, "/absolute.txt": True, "a/../b.txt": True}
        for name, expected in cases.items():
            with self.subTest(name=name):
                problems = gitflow.base_ok(self.root, [name])
                self.assertEqual(any("跳出仓库" in p for p in problems), expected)

    def test_repo_from_url_sweep(self) -> None:
        cases = {
            "git@github.com:bfxh/trit.git": "bfxh/trit",
            "git@github.com:bfxh/trit": "bfxh/trit",
            "https://github.com/bfxh/trit": "bfxh/trit",
            "https://github.com/bfxh/trit.git": "bfxh/trit",
            "https://github.com/bfxh/trit/": "bfxh/trit",
            "https://gitlab.com/bfxh/trit": "",
            "file:///tmp/repo": "",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(gitflow.repo_from_url(url), expected)

    def test_pick_base_sweep(self) -> None:
        self.assertEqual(gitflow.pick_base([]), "main")
        self.assertEqual(gitflow.pick_base(["main"]), "main")
        self.assertEqual(gitflow.pick_base(["master"]), "master")
        self.assertEqual(gitflow.pick_base(["dev", "master"]), "master")
        self.assertEqual(gitflow.pick_base(["dev", "main", "master"]), "main")

    def test_failed_checks_sweep(self) -> None:
        rollup = [
            {"name": "a", "conclusion": "SUCCESS"},
            {"name": "b", "conclusion": "SKIPPED"},
            {"name": "c", "conclusion": "NEUTRAL"},
            {"name": "d", "conclusion": None},
            {"name": "e", "conclusion": "FAILURE"},
            {"name": "f", "conclusion": "CANCELLED"},
            {"name": "g", "conclusion": "TIMED_OUT"},
        ]
        self.assertEqual([c["name"] for c in gitflow.failed_checks(rollup)], ["e", "f", "g"])

    def test_plan_shape(self) -> None:
        args = {
            "title": "t", "body_file": "", "files": ["b", "a"], "branch": "feat/x",
            "base": "main", "no_merge": False, "repo": "o/r", "dry_run": True, "remote": "origin",
        }
        commands = gitflow.plan(args)
        self.assertEqual(commands[0], ["git", "add", "--", "b", "a"])
        self.assertFalse(any("-A" in command for command in commands))
        self.assertTrue(any(command[:3] == ["gh", "pr", "checks"] for command in commands))
        self.assertTrue(any(command[:3] == ["gh", "pr", "merge"] for command in commands))
        args["no_merge"] = True
        self.assertFalse(any(c[:3] == ["gh", "pr", "merge"] for c in gitflow.plan(args)))

    def test_publish_refuses_unsafe_inputs(self) -> None:
        self.write("ok.txt", b"hi")
        self.write(".env", b"X=1")
        base = {"title": "t", "body_file": "", "branch": "feat/x", "base": "main",
                "no_merge": True, "repo": "o/r", "dry_run": False, "remote": "origin"}
        for files in ([], [".env"], ["missing.txt"], ["../up.txt"]):
            with self.subTest(files=files):
                args = dict(base, files=files)
                original = gitflow.ROOT
                gitflow.ROOT = self.root
                try:
                    self.assertEqual(gitflow.publish(args), 1)
                finally:
                    gitflow.ROOT = original

    def _git(self, *args: str, cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=cwd or self.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )

    def test_publish_end_to_end_against_local_remote(self) -> None:
        remote = self.root / "remote.git"
        work = self.root / "work"
        work.mkdir()
        self.assertEqual(self._git("init", "--bare", "-b", "main", str(remote)).returncode, 0)
        self.assertEqual(self._git("init", cwd=work).returncode, 0)
        self._git("config", "user.email", "t@example.com", cwd=work)
        self._git("config", "user.name", "t", cwd=work)
        (work / "a.txt").write_bytes(b"hello")
        self.assertEqual(self._git("add", "a.txt", cwd=work).returncode, 0)
        self.assertEqual(self._git("commit", "-m", "init", cwd=work).returncode, 0)
        self.assertEqual(self._git("remote", "add", "origin", str(remote), cwd=work).returncode, 0)
        (work / "a.txt").write_bytes(b"hello world")

        calls: list[list[str]] = []

        def fake_gh(args: list[str], cwd=None):
            calls.append(args)
            return types.SimpleNamespace(returncode=0, stdout="")

        original_root, original_gh = gitflow.ROOT, gitflow.gh
        gitflow.ROOT, gitflow.gh = work, fake_gh
        try:
            code = gitflow.publish({
                "title": "场景测试提交", "body_file": "", "files": ["a.txt"], "branch": "feat/scenario",
                "base": "main", "no_merge": False, "repo": "o/r", "dry_run": False, "remote": "origin",
            })
        finally:
            gitflow.ROOT, gitflow.gh = original_root, original_gh

        self.assertEqual(code, 0)
        self.assertTrue(any("feat/scenario" in line for line in self._git("branch", "--list", cwd=work).stdout.splitlines()))
        self.assertEqual(self._git("log", "-1", "--pretty=%s", cwd=work).stdout.strip(), "场景测试提交")
        self.assertTrue(any(c[:2] == ["pr", "create"] for c in calls), calls)
        self.assertTrue(any(c[:2] == ["pr", "checks"] for c in calls), calls)
        self.assertTrue(any(c[:2] == ["pr", "merge"] for c in calls), calls)


    def test_branch_and_repo_validation(self) -> None:
        for bad in ("-x", "feat/../x", "feat x", "a..b", "x.lock", "a//b", ""):
            with self.subTest(bad=bad):
                self.assertTrue(gitflow.branch_problems(bad))
        for good in ("feat/x-1", "fix_a.b"):
            with self.subTest(good=good):
                self.assertEqual(gitflow.branch_problems(good), [])
        self.assertTrue(gitflow.repo_problems("trit"))
        self.assertEqual(gitflow.repo_problems("bfxh/trit"), [])

    def _fresh_repo(self) -> tuple[pathlib.Path, pathlib.Path]:
        remote = self.root / "remote.git"
        work = self.root / "work2"
        work.mkdir()
        self._git("init", "--bare", "-b", "main", str(remote))
        self._git("init", cwd=work)
        self._git("config", "user.email", "t@example.com", cwd=work)
        self._git("config", "user.name", "t", cwd=work)
        (work / "a.txt").write_bytes(b"a")
        (work / "extra.txt").write_bytes(b"extra")
        self._git("add", "a.txt", "extra.txt", cwd=work)
        self._git("commit", "-m", "init", cwd=work)
        self._git("remote", "add", "origin", str(remote), cwd=work)
        return work, remote

    def test_publish_refuses_dirty_index(self) -> None:
        work, _ = self._fresh_repo()
        (work / "a.txt").write_bytes(b"changed")
        (work / "extra.txt").write_bytes(b"changed too")
        self._git("add", "extra.txt", cwd=work)  # 索引里有列表外的文件
        original_root, original_gh = gitflow.ROOT, gitflow.gh
        gitflow.ROOT, gitflow.gh = work, lambda args, cwd=None: types.SimpleNamespace(returncode=0, stdout="")
        try:
            code = gitflow.publish({
                "title": "t", "body_file": "", "files": ["a.txt"], "branch": "feat/dirty",
                "base": "main", "no_merge": True, "repo": "o/r", "dry_run": False, "remote": "origin",
            })
        finally:
            gitflow.ROOT, gitflow.gh = original_root, original_gh
        self.assertEqual(code, 1)
        self.assertEqual(self._git("log", "-1", "--pretty=%s", cwd=work).stdout.strip(), "init")

    def test_publish_reuses_existing_pr(self) -> None:
        work, _ = self._fresh_repo()
        (work / "a.txt").write_bytes(b"changed")
        calls: list[list[str]] = []

        def fake_gh(args: list[str], cwd=None):
            calls.append(args)
            if args[:3] == ["pr", "list", "--repo"]:
                return types.SimpleNamespace(returncode=0, stdout='[{"number": 7}]')
            return types.SimpleNamespace(returncode=0, stdout="")

        original_root, original_gh = gitflow.ROOT, gitflow.gh
        gitflow.ROOT, gitflow.gh = work, fake_gh
        try:
            code = gitflow.publish({
                "title": "t", "body_file": "", "files": ["a.txt"], "branch": "feat/reuse",
                "base": "main", "no_merge": False, "repo": "o/r", "dry_run": False, "remote": "origin",
            })
        finally:
            gitflow.ROOT, gitflow.gh = original_root, original_gh
        self.assertEqual(code, 0)
        self.assertFalse(any(c[:2] == ["pr", "create"] for c in calls), calls)
        self.assertTrue(any(c[:2] == ["pr", "merge"] for c in calls), calls)


    def _branch_repo(self) -> pathlib.Path:
        """一个带 origin/main 的工作仓：main 上有一次提交。"""
        remote = self.root / "branch-remote.git"
        work = self.root / "branch-work"
        work.mkdir()
        self._git("init", "--bare", "-b", "main", str(remote))
        self._git("init", "-b", "main", cwd=work)
        self._git("config", "user.email", "t@example.com", cwd=work)
        self._git("config", "user.name", "t", cwd=work)
        (work / "f.txt").write_bytes(b"base\n")
        self._git("add", "f.txt", cwd=work)
        self._git("commit", "-m", "init", cwd=work)
        self._git("remote", "add", "origin", str(remote), cwd=work)
        self._git("push", "-u", "origin", "main", cwd=work)
        return work

    def _with_root(self, work: pathlib.Path):
        class Ctx:
            def __enter__(self):
                self.saved = gitflow.ROOT
                gitflow.ROOT = work

            def __exit__(self, *exc):
                gitflow.ROOT = self.saved
                return False

        return Ctx()

    def test_branch_start_creates_from_base(self) -> None:
        work = self._branch_repo()
        with self._with_root(work):
            self.assertEqual(gitflow.branch_start("feat/new", "main", "origin", dry_run=False), 0)
        names = self._git("branch", "--format=%(refname:short)", cwd=work).stdout.split()
        self.assertIn("feat/new", names)
        head = self._git("rev-parse", "HEAD", cwd=work).stdout.strip()
        base = self._git("rev-parse", "origin/main", cwd=work).stdout.strip()
        self.assertEqual(head, base)

    def test_branch_start_refuses_bad_or_protected_names(self) -> None:
        work = self._branch_repo()
        with self._with_root(work):
            self.assertEqual(gitflow.branch_start("-x", "main", "origin", dry_run=True), 2)
            self.assertEqual(gitflow.branch_start("main", "main", "origin", dry_run=True), 2)
            self.assertEqual(gitflow.branch_start("nested/../x", "main", "origin", dry_run=True), 2)

    def test_branch_sync_rebases_onto_new_base(self) -> None:
        work = self._branch_repo()
        self._git("checkout", "-b", "feat/a", cwd=work)
        (work / "mine.txt").write_bytes(b"mine")
        self._git("add", "mine.txt", cwd=work)
        self._git("commit", "-m", "mine", cwd=work)
        # 让 origin/main 前进（另一个克隆推上去，避免切分支）
        other = self.root / "other"
        self._git("clone", "-b", "main", str(self.root / "branch-remote.git"), str(other))
        self._git("config", "user.email", "t@example.com", cwd=other)
        self._git("config", "user.name", "t", cwd=other)
        (other / "theirs.txt").write_bytes(b"theirs")
        self._git("add", "theirs.txt", cwd=other)
        self._git("commit", "-m", "theirs", cwd=other)
        self.assertEqual(self._git("push", "origin", "main", cwd=other).returncode, 0)
        with self._with_root(work):
            self.assertEqual(gitflow.branch_sync("main", "origin", dry_run=False), 0)
        counts = self._git("rev-list", "--left-right", "--count", "origin/main...HEAD", cwd=work).stdout.split()
        self.assertEqual(counts, ["0", "1"])

    def test_branch_sync_aborts_on_conflict(self) -> None:
        work = self._branch_repo()
        self._git("checkout", "-b", "feat/conflict", cwd=work)
        (work / "f.txt").write_bytes(b"mine\n")
        self._git("add", "f.txt", cwd=work)
        self._git("commit", "-m", "mine", cwd=work)
        other = self.root / "other2"
        self._git("clone", "-b", "main", str(self.root / "branch-remote.git"), str(other))
        self._git("config", "user.email", "t@example.com", cwd=other)
        self._git("config", "user.name", "t", cwd=other)
        (other / "f.txt").write_bytes(b"theirs\n")
        self._git("add", "f.txt", cwd=other)
        self._git("commit", "-m", "theirs", cwd=other)
        self.assertEqual(self._git("push", "origin", "main", cwd=other).returncode, 0)
        before = self._git("rev-parse", "HEAD", cwd=work).stdout.strip()
        with self._with_root(work):
            self.assertEqual(gitflow.branch_sync("main", "origin", dry_run=False), 1)
        after = self._git("rev-parse", "HEAD", cwd=work).stdout.strip()
        self.assertEqual(before, after, "冲突后分支必须回到原样")
        self.assertFalse((work / ".git" / "rebase-merge").exists())

    def test_branch_sync_refuses_dirty_tree(self) -> None:
        work = self._branch_repo()
        self._git("checkout", "-b", "feat/dirty", cwd=work)
        (work / "f.txt").write_bytes(b"uncommitted\n")
        with self._with_root(work):
            self.assertEqual(gitflow.branch_sync("main", "origin", dry_run=False), 2)

    def test_branch_prune_dry_run_and_apply(self) -> None:
        work = self._branch_repo()
        self._git("checkout", "-b", "merged-one", cwd=work)
        (work / "m.txt").write_bytes(b"m")
        self._git("add", "m.txt", cwd=work)
        self._git("commit", "-m", "m", cwd=work)
        self._git("checkout", "main", cwd=work)
        self._git("merge", "--no-ff", "-m", "merge", "merged-one", cwd=work)
        self._git("checkout", "-b", "open-one", cwd=work)
        (work / "o.txt").write_bytes(b"o")
        self._git("add", "o.txt", cwd=work)
        self._git("commit", "-m", "o", cwd=work)
        self._git("checkout", "main", cwd=work)
        with self._with_root(work):
            self.assertEqual(gitflow.branch_prune("main", apply_changes=False), 0)
        names = self._git("branch", "--format=%(refname:short)", cwd=work).stdout.split()
        self.assertIn("merged-one", names)
        self.assertIn("open-one", names)
        with self._with_root(work):
            self.assertEqual(gitflow.branch_prune("main", apply_changes=True), 0)
        names = self._git("branch", "--format=%(refname:short)", cwd=work).stdout.split()
        self.assertNotIn("merged-one", names)
        self.assertIn("open-one", names)


class VendorScenarios(TempRoot):
    def test_splice_and_strip_table(self) -> None:
        block = vendor.block_for("vendor")
        cases = [
            ("", block),
            ("[net]\nretry = 2\n", "[net]\nretry = 2\n\n" + block),
            ("[net]\nretry = 2", "[net]\nretry = 2\n\n" + block),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(vendor.splice(text, block), expected)
                roundtrip = text if (not text or text.endswith("\n")) else text + "\n"
                self.assertEqual(vendor.strip_block(vendor.splice(text, block)), roundtrip)
        spliced = vendor.splice("[net]\nretry = 2\n", block)
        self.assertEqual(vendor.splice(spliced, block), spliced)

    def test_backup_only_once_and_restore(self) -> None:
        (self.root / vendor.CONFIG).parent.mkdir(parents=True)
        (self.root / vendor.CONFIG).write_bytes(b"[net]\nretry = 1\n")
        original = (vendor.ROOT, vendor.BACKUP, vendor.CONFIG)
        vendor.ROOT = self.root
        try:
            vendor.backup_config(self.root)
            (self.root / vendor.CONFIG).write_bytes(b"[net]\nretry = 9\n")
            vendor.backup_config(self.root)
            self.assertEqual((self.root / vendor.BACKUP).read_bytes(), b"[net]\nretry = 1\n")
            self.assertEqual(vendor.restore(self.root, verify_build=False), 0)
            self.assertEqual((self.root / vendor.CONFIG).read_bytes(), b"[net]\nretry = 1\n")
            self.assertFalse((self.root / vendor.BACKUP).exists())
        finally:
            vendor.ROOT, vendor.BACKUP, vendor.CONFIG = original

    def test_status_on_empty_root(self) -> None:
        original = vendor.ROOT
        vendor.ROOT = self.root
        try:
            self.assertEqual(vendor.status(self.root), 0)
        finally:
            vendor.ROOT = original


    def test_restore_preserves_foreign_vendor(self) -> None:
        (self.root / "vendor").mkdir()
        (self.root / "vendor" / "keep.txt").write_bytes(b"mine")
        original = vendor.ROOT
        vendor.ROOT = self.root
        try:
            self.assertEqual(vendor.restore(self.root, verify_build=False), 0)
            self.assertTrue((self.root / "vendor" / "keep.txt").exists())
            self.assertEqual(vendor.restore(self.root, verify_build=False, force=True), 0)
            self.assertFalse((self.root / "vendor").exists())
        finally:
            vendor.ROOT = original


    def test_save_refuses_foreign_directory(self) -> None:
        (self.root / "vendor").mkdir()
        (self.root / "vendor" / "mine.txt").write_bytes(b"mine")
        original = vendor.ROOT
        vendor.ROOT = self.root
        try:
            self.assertEqual(vendor.save(self.root, "vendor"), 2)
            self.assertTrue((self.root / "vendor" / "mine.txt").exists())
        finally:
            vendor.ROOT = original


class DepsScenarios(TempRoot):
    def make_workspace(self, members: list[str]) -> None:
        (self.root / "Cargo.toml").write_text(
            "[workspace]\n"
            f"members = [{', '.join(json.dumps(m) for m in members)}]\n"
            "\n[workspace.package]\n"
            'version = "1.2.3"\n'
            "\n[workspace.dependencies]\n"
            'beta = { path = "crates/beta", version = "1.2.3" }\n'
            'serde = "1"\n',
            encoding="utf-8",
        )

    def make_crate(self, name: str, manifest: str, files: dict[str, bytes] | None = None) -> None:
        crate = self.root / "crates" / name
        (crate / "src").mkdir(parents=True, exist_ok=True)
        (crate / "Cargo.toml").write_text(manifest, encoding="utf-8")
        (crate / "src" / "lib.rs").write_text("pub fn f() {}\n", encoding="utf-8")
        for rel, data in (files or {}).items():
            (crate / rel).parent.mkdir(parents=True, exist_ok=True)
            (crate / rel).write_bytes(data)

    def test_empty_workspace(self) -> None:
        self.make_workspace([])
        plans, _ = deps.build(self.root)
        self.assertEqual(plans, [])

    def test_dotted_and_inline_dependencies(self) -> None:
        self.make_workspace(["crates/alpha", "crates/beta"])
        self.make_crate("beta", '[package]\nname = "beta"\nversion = "1.2.3"\npublish = false\n')
        self.make_crate(
            "alpha",
            "[package]\nname = \"alpha\"\nversion.workspace = true\n"
            "\n[dependencies]\nbeta.workspace = true\nserde = { workspace = true }\n",
        )
        plans, _ = deps.build(self.root)
        alpha = next(p for p in plans if p["crate"] == "alpha")
        self.assertEqual(alpha["internal_deps"], ["beta"])
        kinds = [b["kind"] for b in alpha["blockers"]]
        self.assertIn("继承字段", kinds)
        self.assertTrue(any("不可发布" in b["detail"] for b in alpha["blockers"] if b["kind"] == "内部依赖"))

    def test_reference_classification(self) -> None:
        self.make_workspace(["crates/alpha"])
        self.make_crate(
            "alpha",
            '[package]\nname = "alpha"\nversion = "1.2.3"\n',
            {
                "src/inside.rs": b'const X: &str = include_str!("../data/x.txt");\n',
                "data/x.txt": b"x",
                "src/outside.rs": b'const Y: &str = include_str!("../../../shared.txt");\n',
                "src/missing.rs": b'const Z: &str = include_str!("../data/absent.txt");\n',
            },
        )
        (self.root / "shared.txt").write_bytes(b"shared")
        plans, _ = deps.build(self.root)
        detail = " ".join(b["detail"] for b in plans[0]["blockers"] if b["kind"] == "越界引用")
        self.assertIn("shared.txt", detail)
        self.assertIn("缺失", detail)
        self.assertNotIn("data/x.txt", detail)

    def test_detach_closure_and_source_integrity(self) -> None:
        self.make_workspace(["crates/alpha", "crates/beta", "crates/gamma"])
        self.make_crate("beta", '[package]\nname = "beta"\nversion = "1.2.3"\n')
        self.make_crate("gamma", '[package]\nname = "gamma"\nversion = "1.2.3"\n')
        self.make_crate(
            "alpha",
            "[package]\nname = \"alpha\"\nversion.workspace = true\n"
            "\n[dependencies]\nbeta = { workspace = true }\n"
            "\n[dev-dependencies]\ngamma = { path = \"../gamma\" }\n",
            {"src/extra/a.rs": b"// a\n", "target/junk.bin": b"junk", ".trit/x": b"x"},
        )
        before = {p.relative_to(self.root).as_posix(): p.read_bytes()
                  for p in self.root.rglob("*") if p.is_file()}
        out = self.root.parent / (self.root.name + "-detach")
        try:
            self.assertEqual(deps.detach(self.root, "alpha", out, force=False), 0)
            for name in ("alpha", "beta", "gamma"):
                self.assertTrue((out / "crates" / name / "Cargo.toml").is_file(), name)
            self.assertFalse((out / "crates" / "alpha" / "target").exists())
            self.assertFalse((out / "crates" / "alpha" / ".trit").exists())
            manifest = (out / "crates" / "alpha" / "Cargo.toml").read_text(encoding="utf-8")
            self.assertNotIn(".workspace = true", manifest)
            self.assertIn('path = "../beta"', manifest)
            after = {p.relative_to(self.root).as_posix(): p.read_bytes()
                     for p in self.root.rglob("*") if p.is_file()}
            self.assertEqual(before, after, "源仓必须一个字节都不动")
            # 目标非空且没 --force 要拒绝
            self.assertEqual(deps.detach(self.root, "alpha", out, force=False), 2)
            self.assertEqual(deps.detach(self.root, "alpha", out, force=True), 0)
        finally:
            shutil.rmtree(out, ignore_errors=True)


    def test_detach_refuses_destination_inside_source(self) -> None:
        self.make_workspace(["crates/alpha"])
        self.make_crate("alpha", '[package]\nname = "alpha"\nversion = "1.2.3"\n')
        inside = self.root / "inside"
        self.assertEqual(deps.detach(self.root, "alpha", inside, force=False), 2)
        self.assertFalse(inside.exists())


    def test_detach_refuses_dangling_reference(self) -> None:
        self.make_workspace(["crates/alpha", "crates/beta"])
        self.make_crate("beta", '[package]\nname = "beta"\nversion = "1.2.3"\n')
        self.make_crate(
            "alpha",
            '[package]\nname = "alpha"\nversion = "1.2.3"\n\n[dependencies]\nbeta = { path = "../beta" }\n',
        )
        # beta 的源码引用到 crate 外的文件：摘出去会丢
        (self.root / "crates" / "beta" / "src" / "out.rs").write_bytes(
            b'const X: &str = include_str!("../../../shared.txt");\n'
        )
        (self.root / "shared.txt").write_bytes(b"s")
        out = self.root.parent / (self.root.name + "-detach-dangling")
        try:
            self.assertEqual(deps.detach(self.root, "alpha", out, force=False), 2)
            self.assertFalse(out.exists())
        finally:
            shutil.rmtree(out, ignore_errors=True)


class DeterminismScenarios(unittest.TestCase):
    def test_self_test_output_is_stable_across_runs(self) -> None:
        for name in ("snapshot", "autoflow", "gitflow", "vendor", "deps"):
            with self.subTest(tool=name):
                first = normalized_snapshot_id(run_tool(name))
                second = normalized_snapshot_id(run_tool(name))
                self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main(verbosity=2)
