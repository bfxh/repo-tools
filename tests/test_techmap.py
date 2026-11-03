"""techmap 的场景测试：CLI 出口码、引用核验、自动标签的确定性。

和 scripts/techmap.py 的 --self-test 分工：自测盯判定函数，这里盯**命令行契约**
（agent 只会照 --help 敲命令，退出码和输出形状就是它的接口）。
"""

import io
import json
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import techmap  # noqa: E402


def make_repo(root: pathlib.Path) -> dict:
    """造一个"看起来像 trit"的本地仓：源码 + 测试文件（测试名可被 grep 到）。"""
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "src" / "kernel.wgsl").write_text("var<workgroup> tile: array<u32, 16>;\n",
                                              encoding="utf-8")
    (root / "tests" / "kernel.rs").write_text(
        "#[test]\nfn kernel_matches_cpu() {}\n#[test]\nfn kernel_rejects_bad_shape()\n",
        encoding="utf-8")
    return {"version": 1, "roots": {"o/r": str(root)}, "entries": []}


def good_entry(**changes):
    entry = {
        "id": "kernel-tiled",
        "title": "分块内核",
        "tags": ["gpu", "tiling"],
        "kind": "technique",
        "status": "landed",
        "repo": "o/r",
        "summary": "把 B 装进共享内存被多行复用，行距做填充避开 bank 冲突。",
        "evidence": [{"type": "pr", "value": "#1"},
                     {"type": "measurement", "value": "端到端 1.72x"}],
        "paths": ["src/kernel.wgsl"],
        "tests": ["tests/kernel.rs::kernel_matches_cpu"],
    }
    entry.update(changes)
    return entry


def run_cli(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = techmap.main(argv)
    return code, buffer.getvalue()


class TechmapCliScenarios(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.data = make_repo(self.root / "repo")
        self.map_path = self.root / "TECHMAP.json"

    def write(self, entries: list[dict]) -> None:
        self.data["entries"] = entries
        self.map_path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")

    def cli(self, *argv: str) -> tuple[int, str]:
        return run_cli(["--map", str(self.map_path), *argv])

    # —— 加载 ——

    def test_missing_map_is_red_not_empty_green(self) -> None:
        code, _out = self.cli("check")
        self.assertEqual(1, code, "索引读不到不能当成绿")

    def test_broken_json_reports_a_reason(self) -> None:
        self.map_path.write_text("{ nope", encoding="utf-8")
        code, _out = self.cli("check")
        self.assertEqual(1, code)

    def test_version_mismatch_is_red(self) -> None:
        self.write([good_entry()])
        self.data["version"] = 7
        self.map_path.write_text(json.dumps(self.data), encoding="utf-8")
        code, out = self.cli("check")
        self.assertEqual(1, code)
        self.assertIn("version", out)

    # —— check ——

    def test_clean_index_is_green(self) -> None:
        self.write([good_entry()])
        code, out = self.cli("check")
        self.assertEqual(0, code, out)
        self.assertIn("绿", out)

    def test_deleted_test_name_turns_index_red(self) -> None:
        self.write([good_entry(tests=["tests/kernel.rs::test_nobody_deleted_me"])])
        code, out = self.cli("check")
        self.assertEqual(1, code, "测试被改名而索引没跟上，必须响")
        self.assertIn("找不到", out)

    def test_unverified_root_only_warns(self) -> None:
        self.write([good_entry()])
        self.data["roots"] = {"o/r": str(self.root / "not-here")}
        self.map_path.write_text(json.dumps(self.data), encoding="utf-8")
        code, out = self.cli("check")
        self.assertEqual(0, code, "root 不在不该判红（换台机器就满屏假红）")
        self.assertIn("没被核验", out)

    def test_entry_without_evidence_is_red(self) -> None:
        entry = good_entry()
        del entry["evidence"]
        self.write([entry])
        code, out = self.cli("check")
        self.assertEqual(1, code)
        self.assertIn("证据", out)

    def test_vague_measurement_is_red(self) -> None:
        self.write([good_entry(evidence=[{"type": "measurement", "value": "快了不少"}])])
        code, out = self.cli("check")
        self.assertEqual(1, code)
        self.assertIn("measurement", out)

    # —— 按 git ref 核验（分支无关） ——

    def _git_repo(self) -> pathlib.Path:
        import subprocess

        repo = self.root / "gitrepo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "in_ref.wgsl").write_text("alpha_symbol\n", encoding="utf-8")
        for argv in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "seed"]):
            subprocess.run(["git", *argv], cwd=repo, check=True, capture_output=True)
        return repo

    def test_ref_verification_survives_a_different_checked_out_branch(self) -> None:
        import shutil

        repo = self._git_repo()
        entry = good_entry(paths=["src/in_ref.wgsl"], symbols=["src/in_ref.wgsl::alpha_symbol"],
                           tests=[])
        self.write([entry])
        self.data["roots"] = {"o/r": {"path": str(repo), "ref": "HEAD"}}
        self.map_path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        # 检出被别的分支拿走：磁盘上没有这个文件，但索引该按 ref 判定"技术在"
        shutil.rmtree(repo / "src")
        code, out = self.cli("check")
        self.assertEqual(0, code, out)

    def test_ref_verification_still_catches_a_removed_file(self) -> None:
        repo = self._git_repo()
        self.write([good_entry(paths=["src/never_existed.wgsl"], tests=[], symbols=[])])
        self.data["roots"] = {"o/r": {"path": str(repo), "ref": "HEAD"}}
        self.map_path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        code, out = self.cli("check")
        self.assertEqual(1, code, "ref 里没有就是没有，不能因为磁盘上有个同名目录就放过")
        self.assertIn("路径不存在", out)

    # —— 查询 ——

    def test_search_prefers_tag_hit(self) -> None:
        self.write([good_entry(),
                    good_entry(id="bank-padding", title="bank 填充", tags=["gpu", "bank-conflict"],
                               summary="行距 8 改 9 个 vec4。")])
        code, out = self.cli("search", "bank conflict")
        self.assertEqual(0, code, out)
        self.assertLess(out.index("bank-padding"), out.index("kernel-tiled"))

    def test_search_no_hit_exits_one(self) -> None:
        self.write([good_entry()])
        code, _out = self.cli("search", "zzz")
        self.assertEqual(1, code, "查不到要能被脚本分辨")

    def test_query_filters_by_kind_and_status(self) -> None:
        self.write([good_entry(), good_entry(id="an-incident", title="事故", kind="incident")])
        code, out = self.cli("query", "--kind", "incident")
        self.assertEqual(0, code)
        self.assertIn("an-incident", out)
        self.assertNotIn("kernel-tiled  [", out)

    def test_query_requires_all_tags(self) -> None:
        self.write([good_entry(tags=["gpu", "tiling"]), good_entry(id="solo", tags=["gpu"])])
        _code, out = self.cli("query", "--tag", "gpu", "--tag", "tiling")
        self.assertIn("kernel-tiled", out)
        self.assertNotIn("solo", out)
        _code, out = self.cli("query", "--tag", "gpu", "--any-tag")
        self.assertIn("solo", out)

    def test_where_asks_which_technologies_touch_a_file(self) -> None:
        self.write([good_entry(), good_entry(id="other", title="别的", tags=["ci"],
                                             paths=["src/kernel.wgsl"])])
        code, out = self.cli("where", "src/kernel.wgsl")
        self.assertEqual(0, code)
        self.assertIn("kernel-tiled", out)
        self.assertIn("other", out)

    def test_show_and_tests_lookup(self) -> None:
        self.write([good_entry()])
        code, out = self.cli("show", "kernel-tiled")
        self.assertEqual(0, code)
        self.assertIn("分块内核", json.loads(out)["title"])
        code, out = self.cli("tests", "kernel-tiled")
        self.assertEqual(0, code)
        self.assertEqual("tests/kernel.rs::kernel_matches_cpu", out.strip())
        code, _out = self.cli("show", "ghost")
        self.assertEqual(1, code, "不存在的 id 不能退 0")

    def test_tags_lists_vocabulary_with_counts(self) -> None:
        self.write([good_entry(), good_entry(id="solo", tags=["gpu"])])
        code, out = self.cli("tags")
        self.assertEqual(0, code)
        self.assertRegex(out, r"(?m)^\s*2\s+gpu")

    # —— add / suggest ——

    def test_add_refuses_bad_entry_and_writes_nothing(self) -> None:
        self.write([good_entry()])
        before = self.map_path.read_text(encoding="utf-8")
        bad = self.root / "bad.json"
        bad.write_text(json.dumps(good_entry(id="New TAG", evidence=[])), encoding="utf-8")
        code, _out = self.cli("add", "--file", str(bad))
        self.assertEqual(1, code)
        self.assertEqual(before, self.map_path.read_text(encoding="utf-8"), "拒收时不许留下半个文件")

    def test_add_accepts_verified_entry(self) -> None:
        self.write([])
        new = self.root / "new.json"
        new.write_text(json.dumps(good_entry(id="second-entry", title="第二条",
                                             tags=["gpu"],
                                             summary="又来一条能核验的记录。",
                                             tests=["tests/kernel.rs::kernel_rejects_bad_shape"])),
                       encoding="utf-8")
        code, _out = self.cli("add", "--file", str(new))
        self.assertEqual(0, code)
        self.assertEqual(1, len(json.loads(self.map_path.read_text(encoding="utf-8"))["entries"]))
        self.assertEqual(0, self.cli("check")[0])

    def test_suggest_is_deterministic_and_rule_driven(self) -> None:
        paths = ["src/kernel.wgsl", "tests/kernel.rs"]
        first = techmap.suggest_tags(paths, ["perf(gpu): 分块"])
        second = techmap.suggest_tags(paths, ["perf(gpu): 分块"])
        self.assertEqual(first, second, "自动标签必须可复算，否则不能当门")
        self.assertIn("gpu", first)
        self.assertIn("perf", first)
        self.assertEqual({}, techmap.suggest_tags([], []))

    def test_self_test_passes_and_reports_counts(self) -> None:
        code, out = run_cli(["--self-test"])
        self.assertEqual(0, code, out)
        tally = out.strip().splitlines()[-1]
        passed, _, rest = tally.rsplit("self-test: ", 1)[-1].partition("/")
        self.assertEqual(passed, rest.split(" ")[0], "自测不许留失败项")
        self.assertGreaterEqual(int(passed), 40, "自测条数掉下 40 就是在被删")


if __name__ == "__main__":
    unittest.main()
