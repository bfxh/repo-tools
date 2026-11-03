"""故障注入（chaos）：把工具**自身**的失败当作输入，验证它不会静默失败。

覆盖的"混乱场景"：
  - 回退写到一半失败（磁盘满 / 权限）⇒ 必须留下 pending，并能重跑收敛；
  - 对象库被删 / 清单被篡改 ⇒ 必须报错，而不是"看起来成功"；
  - 并发 save + restore ⇒ 最终状态要能对上某个快照；
  - autoflow 里再套 autoflow ⇒ 外层回退到外层快照；
  - 命令把快照库删掉 ⇒ autoflow 不能假装成功；
  - gh 返回垃圾 JSON ⇒ 命令返回非零而不是崩栈；
  - CI 检查永远 pending / 先 pending 后通过 ⇒ 只在真绿时合并；
  - 同一秒内连续建快照 ⇒ id 不撞、顺序稳定。
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

autoflow = importlib.import_module("autoflow")
gitflow = importlib.import_module("gitflow")
snapshot = importlib.import_module("snapshot")


class ChaosRoot(unittest.TestCase):
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

    def verify(self, snap_id: str) -> int:
        return snapshot.verify(self.root, snapshot.resolve(self.root, snap_id))


class InterruptedRestore(ChaosRoot):
    def test_restore_fails_midway_then_resumes(self) -> None:
        self.write("a.txt", b"orig-a")
        self.write("b.txt", b"orig-b")
        snap_id = snapshot.save(self.root, "chaos")
        self.write("a.txt", b"changed-a")
        self.write("b.txt", b"changed-b")

        original = snapshot.sha256_file
        pending = self.root / ".trit" / "pending.json"

        def flaky(path):
            # 只在"已经开始写回"之后才炸，模拟磁盘满：此时 pending 必须已经在
            if pending.exists() and str(path).endswith("b.txt"):
                raise OSError("模拟磁盘满")
            return original(path)

        snapshot.sha256_file = flaky
        try:
            with self.assertRaises(OSError):
                snapshot.restore(self.root, snapshot.resolve(self.root, snap_id), prune=False, dry_run=False)
        finally:
            snapshot.sha256_file = original

        self.assertTrue(pending.exists(), "中断后必须留下 pending 标记")
        # 再跑一次同一条命令必须能收敛（幂等）
        snapshot.restore(self.root, snapshot.resolve(self.root, snap_id), prune=False, dry_run=False)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"orig-a")
        self.assertEqual((self.root / "b.txt").read_bytes(), b"orig-b")
        self.assertFalse((self.root / ".trit" / "pending.json").exists())
        self.assertEqual(self.verify(snap_id), 0)

    def test_missing_blob_is_fatal(self) -> None:
        self.write("a.txt", b"orig")
        snap_id = snapshot.save(self.root, "chaos")
        manifest = snapshot.resolve(self.root, snap_id)
        blob = snapshot.blob_path(self.root, manifest["files"]["a.txt"]["sha"])
        blob.unlink()
        self.write("a.txt", b"changed")
        with self.assertRaises(SystemExit):
            snapshot.restore(self.root, snapshot.resolve(self.root, snap_id), prune=False, dry_run=False)

    def test_tampered_manifest_is_caught(self) -> None:
        self.write("a.txt", b"orig")
        snap_id = snapshot.save(self.root, "chaos")
        path = self.root / ".trit" / "snapshots" / f"{snap_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["files"]["a.txt"]["sha"] = "0" * 64
        path.write_text(json.dumps(data), encoding="utf-8")
        self.write("a.txt", b"changed")
        with self.assertRaises(SystemExit):
            snapshot.restore(self.root, snapshot.resolve(self.root, snap_id), prune=False, dry_run=False)


class ConcurrentUse(ChaosRoot):
    def test_concurrent_save_and_restore_stay_consistent(self) -> None:
        for index in range(30):
            self.write(f"f{index}.txt", f"v{index}".encode())
        first = snapshot.save(self.root, "before")
        errors: list[BaseException] = []

        def saver() -> None:
            try:
                snapshot.save(self.root, "during")
            except BaseException as exc:  # noqa: BLE001 - 收集给主线程断言
                errors.append(exc)

        def restorer() -> None:
            try:
                snapshot.restore(self.root, snapshot.resolve(self.root, first), prune=False, dry_run=False)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=saver), threading.Thread(target=restorer)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.verify(first), 0)

    def test_ids_unique_within_same_second(self) -> None:
        ids = set()
        for _ in range(30):
            ids.add(snapshot.save(self.root, "burst"))
        self.assertEqual(len(ids), 30)
        self.assertEqual(len(snapshot.list_snapshots(self.root)), 30)


class NestedAutoflow(ChaosRoot):
    def test_nested_autoflow_restores_outer_state(self) -> None:
        self.write("a.txt", b"orig")
        inner = self.root / "inner.py"
        inner.write_text(
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('af', r'{autoflow.__file__}')\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "sys.modules['af'] = module\n"
            "spec.loader.exec_module(module)\n"
            "code = module.run(r'%s', [sys.executable, '-c', \"open('inner.txt','wb').write(b'1'); raise SystemExit(2)\"], '', False)\n"
            "open('leftover.txt', 'wb').write(b'outer')\n"
            "sys.exit(code)\n" % self.root,
            encoding="utf-8",
        )
        code = autoflow.run(self.root, [sys.executable, str(inner)], "", False)
        self.assertEqual(code, 2)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"orig")
        self.assertFalse((self.root / "inner.txt").exists(), "内层新建的文件必须被清掉")
        self.assertFalse((self.root / "leftover.txt").exists(), "外层失败必须把外层改动也回退")

    def test_command_deleting_snapshot_store_is_not_silent_success(self) -> None:
        self.write("a.txt", b"orig")
        code = autoflow.run(
            self.root,
            [sys.executable, "-c", "import shutil;shutil.rmtree('.trit', ignore_errors=True);raise SystemExit(3)"],
            "",
            False,
        )
        self.assertNotEqual(code, 0, "快照库被删时必须返回非零（不能假装回退成功）")
        # 快照库没了就没有可回退的东西：断言"响亮失败"而不是 pending
        self.assertEqual((self.root / "a.txt").read_bytes(), b"orig")


ROWS_PENDING = '[{"name": "CI", "bucket": "pending"}]'
ROWS_PASS = '[{"name": "CI", "bucket": "pass"}]'

class GhMisbehaves(ChaosRoot):
    def _fake(self, mapping):
        def fake_gh(args, cwd=None):
            for key, value in mapping.items():
                if args[: len(key)] == list(key):
                    return types.SimpleNamespace(**value)
            return types.SimpleNamespace(returncode=0, stdout="")

        return fake_gh

    def test_garbage_json_does_not_crash(self) -> None:
        original = gitflow.gh
        gitflow.gh = self._fake({("pr", "list"): {"returncode": 0, "stdout": "not json at all"}})
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertNotEqual(gitflow.stack("o/r", remote_state=True), 0)
                self.assertNotEqual(gitflow.protect("o/r", "main"), 0)
        finally:
            gitflow.gh = original

    def test_merge_waits_for_pending_then_merges(self) -> None:
        state = {"checks": 0, "merged": 0}

        def fake_gh(args, cwd=None):
            if args[:2] == ["pr", "checks"]:
                if "--watch" in args:
                    return types.SimpleNamespace(returncode=0, stdout=ROWS_PASS)
                state["checks"] += 1
                if state["checks"] < 3:
                    return types.SimpleNamespace(returncode=8, stdout=ROWS_PENDING)
                return types.SimpleNamespace(returncode=0, stdout=ROWS_PASS)
            if args[:2] == ["pr", "merge"]:
                state["merged"] += 1
                return types.SimpleNamespace(returncode=0, stdout="")
            return types.SimpleNamespace(returncode=0, stdout="")

        original = gitflow.gh
        gitflow.gh = fake_gh
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = gitflow.watch_and_merge("feat/x", "o/r", attempts=5, pause=0)
        finally:
            gitflow.gh = original
        self.assertEqual(code, 0)
        self.assertEqual(state["merged"], 1)

    def test_merge_gives_up_when_never_green(self) -> None:
        def fake_gh(args, cwd=None):
            if args[:2] == ["pr", "checks"]:
                return types.SimpleNamespace(returncode=8, stdout=ROWS_PENDING)
            return types.SimpleNamespace(returncode=0, stdout="")

        original = gitflow.gh
        gitflow.gh = fake_gh
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = gitflow.watch_and_merge("feat/x", "o/r", attempts=2, pause=0)
        finally:
            gitflow.gh = original
        self.assertNotEqual(code, 0, "永远 pending 时必须放弃，不能报成功")


if __name__ == "__main__":
    unittest.main(verbosity=2)
