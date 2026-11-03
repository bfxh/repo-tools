#!/usr/bin/env python3
"""技术标签索引：每个新功能 / 每项单独技术 / 每道门 / 每次事故一条记录，可按标签、路径、符号反查。

为什么要这个（用户的原话形状）：「每个新功能、每个单独的技术，都可以查询……因为它会有标签的，
单独一个技术标签，就不需要我重新去找，不需要 AI 去麻烦找」。翻 GitHub / grep 找"那个 bank conflict
的填充是几"这种问题，每次都要重搜一遍；标签索引把它变成一次查询。

关键设计是**引用必须可核验**，否则它三周后就是一堆假话：

- `paths` / `symbols` / `tests` / `docs` 都在 `roots` 指向的本地仓里核验；测试写成
  `文件::测试名`，**测试名在文件里找不到就判红**（删了测试没人改索引 ⇒ 门响）；
- `evidence` 至少一条：`pr`（#123）、`commit`（7–40 位十六进制）、`measurement`（**必须带数字和单位**，
  不许写"快了很多"）、`doc`、`log`；
- `status=retracted` 必须写 `replaced_by`；`related` / `supersedes` 不许指向不存在的 id；
- root 没配 ⇒ 只能警告"这条没法核验"，不当绿也不判红（跨机器时常见）。

用法：
    python3 scripts/techmap.py search "bank conflict"
    python3 scripts/techmap.py query --tag gpu --kind technique --status landed
    python3 scripts/techmap.py show wf32-layer-chain
    python3 scripts/techmap.py where crates/trit-gpu/src/matmul_wf32_plan.rs
    python3 scripts/techmap.py tests wf32-layer-chain        # 直接喂给 cargo test 的名单
    python3 scripts/techmap.py check [--json]                 # 门禁：引用烂了/证据空了就红
    python3 scripts/techmap.py tags                            # 标签词表与频次
    python3 scripts/techmap.py suggest --repo bfxh/trit --range main..HEAD   # 只提议，不写文件
    python3 scripts/techmap.py add --file entry.json                         # 校验通过才入库
    python3 scripts/techmap.py --self-test

默认索引：`D:\\KF\\_handoff\\TECHMAP.json`（`--map` 或 `TRIT_TECHMAP` 覆盖）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys
from collections import Counter
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_MAP = pathlib.Path(r"D:\KF\_handoff\TECHMAP.json")
VERSION = 1
KINDS = ("feature", "technique", "gate", "incident", "decision")
STATUSES = ("landed", "experimental", "retracted")
EVIDENCE_TYPES = ("pr", "commit", "measurement", "doc", "log")
ID_OK = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
TAG_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,31}$")
SHA_OK = re.compile(r"^[0-9a-f]{7,40}$")
PR_OK = re.compile(r"^(#\d+|https?://\S+)$")
UNITS = ("ms", "ns", "ps", "s", "%", "x", "GB/s", "Gop/s", "MB", "MiB", "GiB", "kB",
         "次", "条", "×", "个", "行", "篇", "页", "字节", "偏差", "存活", "命中率", "倍", "拦", "警")
REQUIRED_KEYS = {"id", "title", "tags", "kind", "status", "repo", "summary", "evidence"}
OPTIONAL_KEYS = {"paths", "symbols", "tests", "docs", "related", "supersedes",
                 "replaced_by", "why", "added", "updated"}

# —— 自动标签的确定性规则（无 LLM；同输入必同输出，才能被测） ——
PATH_RULES: list[tuple[str, str]] = [
    (r"\.wgsl$", "wgsl"),
    (r"matmul_wf32", "wf32"),
    (r"crates/trit-gpu/", "gpu"),
    (r"crates/trit-ops/", "cpu-kernels"),
    (r"crates/trit-orch/", "coordination"),
    (r"(claim|receipt|handoff)", "coordination"),
    (r"scripts/mutation|scripts/mutants", "mutation-gate"),
    (r"scripts/ci_|\.github/workflows/", "ci"),
    (r"(test_surface|module_budget|hazard_gate|ci_integrity|check_vendored)", "gate"),
    (r"(^|/)tests/", "tests"),
    (r"\.md$|(^|/)docs/", "docs"),
    (r"profiles\.py|vendored\.json", "defaults"),
    (r"(bench|examples)/", "benchmark"),
]
SCOPE_OK = re.compile(r"^\s*[a-z]+\(([a-z0-9._-]+)\)", re.IGNORECASE)
COMMIT_RULES: list[tuple[str, str]] = [
    (r"^\s*perf(\(|:)", "perf"),
    (r"^\s*fix(\(|:)", "bugfix"),
    (r"^\s*feat(\(|:)", "feature"),
    (r"^\s*ci(\(|:)", "ci"),
    (r"^\s*test(\(|:)", "tests"),
    (r"^\s*docs(\(|:)", "docs"),
    (r"bank|conflict|tiling|tile", "gpu"),
    (r"sha256|逐字", "drift-guard"),
]


def git_output(repo_root: str, args: list[str]) -> str:
    result = subprocess.run(["git", "-C", repo_root, *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    return result.stdout if result.returncode == 0 else ""


def normalize_tag(raw: str) -> str:
    return re.sub(r"[\s_]+", "-", str(raw).strip().lower())


def suggest_tags(paths: list[str], messages: list[str]) -> dict[str, list[str]]:
    """按规则给候选标签 + 每个标签的理由串。只提议，不写索引。"""
    out: dict[str, list[str]] = {}
    for path in paths:
        for pattern, tag in PATH_RULES:
            if re.search(pattern, path.replace("\\", "/")):
                out.setdefault(normalize_tag(tag), []).append(f"path {path} ~ {pattern}")
    for message in messages:
        scope = SCOPE_OK.match(message)
        if scope:
            tag = normalize_tag(scope.group(1))
            out.setdefault(tag, []).append(f"commit scope {message!r}")
        for pattern, tag in COMMIT_RULES:
            if re.search(pattern, message, flags=re.IGNORECASE):
                out.setdefault(normalize_tag(tag), []).append(f"commit ~ {pattern}")
    return dict(sorted(out.items(), key=lambda kv: (-len(kv[1]), kv[0])))


def entry_digest(entry: dict[str, Any]) -> str:
    payload = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _strings(entry: dict[str, Any], key: str) -> list[str]:
    value = entry.get(key, [])
    return value if isinstance(value, list) and all(isinstance(x, str) for x in value) else []


def validate_entry(entry: dict[str, Any], known_ids: set[str]) -> list[str]:
    """单条记录的结构校验（不含"文件在不在"，那需要 root）。"""
    errors: list[str] = []
    if not isinstance(entry, dict):
        return ["记录必须是对象"]
    missing = REQUIRED_KEYS - set(entry)
    if missing:
        errors.append(f"{entry.get('id', '?')} 缺字段：{', '.join(sorted(missing))}")
    unknown = set(entry) - REQUIRED_KEYS - OPTIONAL_KEYS
    if unknown:
        errors.append(f"{entry.get('id', '?')} 有未知字段：{', '.join(sorted(unknown))}")
    identifier = str(entry.get("id", ""))
    if not ID_OK.match(identifier):
        errors.append(f"id 不合法：{identifier!r}（要小写字母数字与 . _ -，长度 2–64）")
    tags = entry.get("tags")
    if not isinstance(tags, list) or not tags:
        errors.append(f"{identifier or '?'} tags 必须是非空数组")
        tags = []
    for tag in tags:
        if not TAG_OK.match(str(tag)):
            errors.append(f"{identifier} 标签不合法：{tag!r}")
        elif normalize_tag(tag) != tag:
            errors.append(f"{identifier} 标签没归一：{tag!r} 应为 {normalize_tag(tag)!r}")
    if len(set(tags)) != len(tags):
        errors.append(f"{identifier} 标签有重复")
    if entry.get("kind") not in KINDS:
        errors.append(f"{identifier} kind 非法：{entry.get('kind')!r}（允许 {', '.join(KINDS)}）")
    if entry.get("status") not in STATUSES:
        errors.append(f"{identifier} status 非法：{entry.get('status')!r}")
    if entry.get("status") == "retracted" and not entry.get("replaced_by"):
        errors.append(f"{identifier} 撤回了却没写 replaced_by（撤回要能追到去处）")
    if not str(entry.get("title", "")).strip():
        errors.append(f"{identifier} title 不能为空")
    if _is_thin(str(entry.get("summary", ""))):
        errors.append(f"{identifier} summary 太短，起不到「不用重新找」的作用")
    evidence = entry.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append(f"{identifier} 一条证据都没有（那它就是传闻，不是记录）")
        evidence = []
    for item in evidence:
        if not isinstance(item, dict) or "type" not in item or "value" not in item:
            errors.append(f"{identifier} 证据必须带 type/value")
            continue
        kind, value = item["type"], str(item["value"])
        if kind not in EVIDENCE_TYPES:
            errors.append(f"{identifier} 证据类型非法：{kind}")
        elif kind == "pr" and not PR_OK.match(value):
            errors.append(f"{identifier} pr 证据要写 #123 或 URL：{value!r}")
        elif kind == "commit" and not SHA_OK.match(value):
            errors.append(f"{identifier} commit 证据要是 7–40 位十六进制：{value!r}")
        elif kind == "measurement" and not _measurement_ok(value):
            errors.append(f"{identifier} measurement 证据必须带数字和单位：{value!r}")
    for key in ("related", "supersedes", "replaced_by"):
        refs = _strings(entry, key) if key != "replaced_by" else ([entry[key]] if entry.get(key) else [])
        for ref in refs:
            if ref not in known_ids:
                errors.append(f"{identifier} 的 {key} 指向不存在的 id：{ref}")
    return errors


def _is_thin(text: str) -> bool:
    """中文一句话的信息量不在"字符数 12"上：6 个汉字已经是完整一句。

    所以判据按语言分开：>=6 个汉字，或者 >=2 个像样的拉丁词，二选一即算不薄。
    """
    stripped = text.strip()
    cjk = len(re.findall(r"[一-鿿]", stripped))
    latin_words = len(re.findall(r"[A-Za-z0-9]{3,}", stripped))
    return cjk < 6 and latin_words < 2


def _measurement_ok(value: str) -> bool:
    if not re.search(r"\d", value):
        return False
    return any(unit in value for unit in UNITS)


class RepoSource:
    """按 git ref 核验引用；git 不可用时退回文件系统。

    为什么不直接看磁盘：索引核验的是"这条技术在不在"，而本地 worktree 随时可能
    停在别的分支上（这台机器上 28 个 worktree 各站一支分支）。只认 HEAD 检出
    会让索引在无关分支上集体判红，那种红没有意义。
    """

    def __init__(self, path: str, ref: str = "origin/main") -> None:
        self.root = pathlib.Path(path)
        self.ref = ref.strip()

    def _git(self, args: list[str]) -> str:
        if not self.ref:
            return ""
        return git_output(str(self.root), args)

    def _in_ref(self, rel: str) -> bool | None:
        """None = git 这条路走不通（没仓库/没这个 ref），得退回文件系统。"""
        if not self.ref or not self.root.is_dir():
            return None
        out = self._git(["ls-tree", "--name-only", self.ref, "--", rel])
        if out.strip():
            return True
        probe = self._git(["rev-parse", "--verify", self.ref])
        return False if probe.strip() else None

    def exists(self, rel: str) -> bool:
        known = self._in_ref(rel)
        if known is not None:
            return known
        return (self.root / rel.split("#", 1)[0]).exists()

    def text(self, rel: str) -> str:
        target = rel.split("#", 1)[0]
        if self.ref and self.root.is_dir():
            out = self._git(["show", f"{self.ref}:{target}"])
            if out:
                return out
        path = self.root / target
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
        return ""


def check_references(entry: dict[str, Any], source: RepoSource) -> tuple[list[str], list[str]]:
    """在已知 root 下核验 paths/tests/docs；返回 (判红, 警告)。"""
    red: list[str] = []
    warn: list[str] = []
    identifier = str(entry.get("id", "?"))
    for path in _strings(entry, "paths") + _strings(entry, "docs"):
        target = path.split("#", 1)[0]
        if not target:
            continue
        if not source.exists(target):
            red.append(f"{identifier} 引用的路径不存在：{target}")
    for ref in _strings(entry, "tests"):
        file_part, sep, test_name = ref.partition("::")
        if not file_part or not sep or not test_name:
            red.append(f"{identifier} 测试引用必须写成 文件::测试名（只给文件钉不住任何判据）：{ref!r}")
            continue
        target = file_part.split("#", 1)[0]
        if not source.exists(target):
            red.append(f"{identifier} 的测试文件不存在：{target}")
            continue
        if test_name and test_name not in source.text(target):
            red.append(f"{identifier} 的测试名在文件里找不到（被删/改名了？）：{test_name}")
    for symbol in _strings(entry, "symbols"):
        _file, _, name = symbol.partition("::")
        if not _file:
            red.append(f"{identifier} 符号要写成 文件::符号名：{symbol!r}")
            continue
        if not source.exists(_file):
            red.append(f"{identifier} 符号所在文件不存在：{_file}")
            continue
        head = name.split("(")[0].split(":")[-1]
        if head and head not in source.text(_file):
            red.append(f"{identifier} 的符号在文件里找不到：{symbol}")
    return red, warn


def make_source(config: Any) -> RepoSource:
    """roots 里的一项可以是路径字符串，也可以是 {"path": ..., "ref": ...}。"""
    if isinstance(config, dict):
        return RepoSource(str(config.get("path", "")), str(config.get("ref", "origin/main")))
    return RepoSource(str(config))


def audit(data: dict[str, Any], roots: dict[str, str] | None = None
          ) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """整本索引的对账：结构 + 引用。roots 可覆盖索引里的配置（测试用）。"""
    red: list[str] = []
    warn: list[str] = []
    if not isinstance(data, dict) or data.get("version") != VERSION:
        return [f"索引 version 必须是 {VERSION}，得到 {data.get('version') if isinstance(data, dict) else type(data).__name__!r}"], [], []
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        return ["索引里没有条目（空索引等于没有门）"], [], []
    root_map = dict(data.get("roots") or {})
    if roots:
        root_map.update(roots)
    ids = [str(entry.get("id", "")) for entry in entries if isinstance(entry, dict)]
    known = {i for i in ids if i}
    if len(known) != len(ids):
        red.append(f"id 有重复或为空：{[i for i, c in Counter(ids).items() if c > 1]}")
    rows: list[dict[str, Any]] = []
    for entry in entries:
        errors = validate_entry(entry, known)
        identifier = str(entry.get("id", "?"))
        repo = str(entry.get("repo", ""))
        raw_root = root_map.get(repo)
        if not errors:
            if raw_root is None:
                warn.append(f"{identifier} 的仓 {repo!r} 没配 root ⇒ 引用没被核验")
            else:
                source = make_source(raw_root)
                if not source.root.is_dir():
                    warn.append(f"{identifier} 的 root 不存在：{source.root} ⇒ 引用没被核验")
                else:
                    ref_red, _ref_warn = check_references(entry, source)
                    errors += ref_red
        red += errors
        rows.append({"id": identifier, "errors": errors,
                     "verified": bool(raw_root) and make_source(raw_root).root.is_dir()})
    return red, warn, rows


def search(entries: list[dict[str, Any]], text: str, limit: int = 10) -> list[tuple[int, dict[str, Any]]]:
    """打分的关键词搜索：标签命中 > 标题 > 符号/路径 > 正文。"""
    tokens = [t for t in re.split(r"[^\w+-]+", text.lower()) if t]
    if not tokens:
        return []
    scored: list[tuple[int, dict[str, Any]]] = []
    for entry in entries:
        raw_tags = [str(t).lower() for t in entry.get("tags", [])]
        # "bank-conflict" 这种复合标签，搜 bank 或 conflict 都该命中
        tags = set(raw_tags) | {part for tag in raw_tags for part in tag.split("-")}
        tags = list(tags)
        title = str(entry.get("title", "")).lower()
        body = " ".join([str(entry.get("summary", "")), str(entry.get("why", "")),
                         " ".join(_strings(entry, "paths")), " ".join(_strings(entry, "symbols")),
                         " ".join(_strings(entry, "docs"))]).lower()
        score = 0
        for token in tokens:
            if token in tags:
                score += 5
            if token in title:
                score += 4
            if token in body:
                score += 2
        if score:
            scored.append((score, entry))
    scored.sort(key=lambda item: (-item[0], str(item[1].get("id"))))
    return scored[:limit]


def filter_entries(entries: list[dict[str, Any]], *, tags: list[str], any_tag: bool = False,
                   kind: str = "", status: str = "", repo: str = "") -> list[dict[str, Any]]:
    want = [normalize_tag(t) for t in tags]
    out = []
    for entry in entries:
        have = {normalize_tag(str(t)) for t in entry.get("tags", [])}
        if want:
            matched = bool(have & set(want)) if any_tag else set(want) <= have
            if not matched:
                continue
        if kind and entry.get("kind") != kind:
            continue
        if status and entry.get("status") != status:
            continue
        if repo and repo not in str(entry.get("repo", "")):
            continue
        out.append(entry)
    return out


def where_entries(entries: list[dict[str, Any]], needle: str) -> list[dict[str, Any]]:
    """反查：这个文件/符号/标签牵动哪些技术条目。"""
    needle_norm = needle.replace("\\", "/").lower()
    out = []
    for entry in entries:
        fields = (_strings(entry, "paths") + _strings(entry, "tests") + _strings(entry, "symbols")
                  + _strings(entry, "docs") + [str(t).lower() for t in entry.get("tags", [])])
        if any(needle_norm in str(field).replace("\\", "/").lower() for field in fields):
            out.append(entry)
    return out


def load_map(path: pathlib.Path) -> tuple[dict[str, Any], str]:
    if not path.is_file():
        return {}, f"索引文件读不到：{path}"
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except ValueError as exc:
        return {}, f"索引不是合法 JSON：{exc}"


def render(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("（没有命中条目）")
        return
    for entry in rows:
        tags = ",".join(entry.get("tags", []))
        print(f"  {entry.get('id')}  [{entry.get('status')}/{entry.get('kind')}]  {entry.get('title')}")
        print(f"      tags: {tags}")
        if entry.get("paths"):
            print(f"      paths: {', '.join(entry['paths'][:4])}")
        if entry.get("tests"):
            print(f"      tests: {', '.join(entry['tests'][:3])}")


def cmd_check(data: dict[str, Any], as_json: bool) -> int:
    red, warn, rows = audit(data)
    if as_json:
        print(json.dumps({"tool": "techmap", "entries": len(rows), "red": red,
                          "warn": warn, "green": not red}, ensure_ascii=False, indent=2))
    else:
        print(f"条目 {len(rows)} 个｜拦 {len(red)} 条｜警 {len(warn)} 条")
        for line in red:
            print(f"  [拦] {line}")
        for line in warn:
            print(f"  [警] {line}")
        print("结论：" + ("绿（每条都有证据，引用都核验过）" if not red else "红（索引在说谎或已经烂掉）"))
    return 1 if red else 0


def cmd_suggest(argv: list[str], data: dict[str, Any]) -> int:
    def value(flag: str, default: str = "") -> str:
        if flag in argv and argv.index(flag) + 1 < len(argv):
            return argv[argv.index(flag) + 1]
        return default

    repo = value("--repo")
    root = (data.get("roots") or {}).get(repo) or value("--root")
    if not root or not pathlib.Path(root).is_dir():
        print(f"FAIL 找不到 {repo!r} 的本地 root（--root 或索引 roots 里配）", file=sys.stderr)
        return 2
    rng = value("--range", "HEAD~1..HEAD")
    files = [line for line in git_output(root, ["diff", "--name-only", rng]).splitlines() if line]
    messages = [line for line in git_output(root, ["log", "--format=%s", rng]).splitlines() if line]
    proposals = suggest_tags(files, messages)
    print(json.dumps({"repo": repo, "range": rng, "files": len(files),
                      "suggest": proposals}, ensure_ascii=False, indent=2))
    return 0


def cmd_add(path: pathlib.Path, entry_file: str) -> int:
    data, error = load_map(path)
    if error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1
    try:
        entry = json.loads(pathlib.Path(entry_file).read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"FAIL 新条目不是合法 JSON：{exc}", file=sys.stderr)
        return 1
    entries = data.get("entries", [])
    known = {str(e.get("id", "")) for e in entries} | {str(entry.get("id", ""))}
    errors = validate_entry(entry, known)
    red, _warn, _rows = audit({"version": VERSION, "roots": data.get("roots") or {},
                               "entries": entries + [entry]})
    if errors or red:
        for line in errors + red:
            print(f"FAIL {line}")
        return 1
    entries.append(entry)
    data["entries"] = entries
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已入库：{entry['id']}（digest {entry_digest(entry)}）")
    return 0


def _fixture_entry(**changes: Any) -> dict[str, Any]:
    entry = {
        "id": "wf32-tiled-kernel",
        "title": "WF32 16×16 共享内存 tile 内核",
        "tags": ["gpu", "wgsl", "wf32", "tiling"],
        "kind": "technique",
        "status": "landed",
        "repo": "bfxh/trit",
        "summary": "B 进 workgroup 共享内存被 16 行复用，行距 9 个 vec4 避开 bank 冲突。",
        "evidence": [{"type": "pr", "value": "#246"},
                     {"type": "measurement", "value": "conv1 端到端 1.72x"}],
        "paths": ["crates/trit-gpu/src/matmul_wf32_tiled.wgsl"],
        "symbols": ["crates/trit-gpu/src/matmul_wf32_tiled.wgsl::btile"],
        "tests": ["crates/trit-gpu/tests/wf32_plan.rs::plan_matches_cpu_on_all_kernels"],
    }
    entry.update(changes)
    return entry


def self_test() -> int:
    """每条判红都要有真的会红的反证；绿例必须真跑。"""
    import tempfile

    checks: list[tuple[str, bool]] = []

    def check(label: str, got, want) -> None:
        checks.append((label, got == want))

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp) / "trit"
        (root / "crates/trit-gpu/src").mkdir(parents=True)
        (root / "crates/trit-gpu/tests").mkdir(parents=True)
        (root / "crates/trit-gpu/src/matmul_wf32_tiled.wgsl").write_text(
            "var<workgroup> btile: array<vec4<f32>, 144>;\n", encoding="utf-8")
        (root / "crates/trit-gpu/tests/wf32_plan.rs").write_text(
            "#[test]\nfn plan_matches_cpu_on_all_kernels() {}\n#[test]\nfn chain_keeps_work()\n",
            encoding="utf-8")
        roots = {"bfxh/trit": str(root)}
        good = _fixture_entry()
        red, warn, _rows = audit({"version": VERSION, "roots": roots, "entries": [good]}, roots)
        check("结构完整 + 引用可核验 => 绿", (red, warn), ([], []))
        check("没配 root => 只警告不判红",
              audit({"version": VERSION, "roots": {}, "entries": [good]}, {})[1] != [], True)
        check("root 指向不存在的目录 => 警告",
              audit({"version": VERSION, "roots": roots, "entries": [good]},
                    {"bfxh/trit": str(root / "nope")})[1] != [], True)

        def only(entry: dict[str, Any]) -> list[str]:
            return audit({"version": VERSION, "roots": roots, "entries": [entry]}, roots)[0]

        no_ev = dict(good)
        del no_ev["evidence"]
        check("缺 evidence => 红", any("一条证据都没有" in e for e in only(no_ev)), True)
        empty_ev = _fixture_entry(evidence=[])
        check("空证据数组 => 红", any("一条证据都没有" in e for e in only(empty_ev)), True)
        vague = _fixture_entry(evidence=[{"type": "measurement", "value": "快了很多"}])
        check("measurement 没数字没单位 => 红", only(vague) != [], True)
        check("measurement 带读数 => 绿",
              [e for e in only(_fixture_entry(evidence=[{"type": "measurement", "value": "1.72x"}]))
               if "measurement" in e], [])
        check("measurement 用本仓门禁口径（拦/警）也算读数",
              only(_fixture_entry(evidence=[{"type": "measurement", "value": "现网首跑：拦 1 / 警 7"}])) == [],
              True)
        check("measurement 只写单位不写数字 => 红",
              only(_fixture_entry(evidence=[{"type": "measurement", "value": "很多 ms"}])) != [], True)
        check("pr 证据写 PR246 => 红",
              only(_fixture_entry(evidence=[{"type": "pr", "value": "PR246"}])) != [], True)
        check("commit 证据写分支名 => 红",
              only(_fixture_entry(evidence=[{"type": "commit", "value": "main"}])) != [], True)
        check("id 大写 => 红", only(_fixture_entry(id="WF32_Tiled")) != [], True)
        check("标签没归一（大写）=> 红并给出照抄值",
              any("GPU" in e and "没归一" in e
                  for e in only(_fixture_entry(tags=["gpu", "GPU"]))), True)
        check("标签里有非法字符 => 红",
              any("不合法" in e for e in only(_fixture_entry(tags=["gpu", "gpu@x"]))), True)
        check("标签重复 => 红", any("重复" in e for e in only(_fixture_entry(tags=["gpu", "gpu"]))), True)
        check("标签为空 => 红", only(_fixture_entry(tags=[])) != [], True)
        check("kind 非法 => 红", only(_fixture_entry(kind="magic")) != [], True)
        check("status 非法 => 红", only(_fixture_entry(status="done")) != [], True)
        check("撤回却没写去处 => 红",
              any("replaced_by" in e for e in only(_fixture_entry(status="retracted"))), True)
        moved = _fixture_entry(id="old-kernel", status="retracted", replaced_by="wf32-tiled-kernel")
        check("撤回写上去处 => 绿（和被指向的那条一起进索引才成立）",
              audit({"version": VERSION, "roots": roots, "entries": [good, moved]}, roots)[0], [])
        check("summary 太短 => 红", only(_fixture_entry(summary="tile")) != [], True)
        check("未知字段 => 红", only(_fixture_entry(mystery=1)) != [], True)
        check("中文短句不算薄（6 个汉字即成句）",
              [e for e in only(_fixture_entry(summary="行距改成九个。")) if "太短" in e], [])
        check("拉丁短句两个词即算不薄",
              [e for e in only(_fixture_entry(summary="tile reuse padded") ) if "太短" in e], [])
        check("一个英文词 => 仍判薄", only(_fixture_entry(summary="tilesize")) != [], True)
        check("related 指向不存在的 id => 红",
              any("related" in e for e in only(_fixture_entry(related=["ghost-id"]))), True)
        check("paths 指到不存在的文件 => 红",
              any("路径不存在" in e
                  for e in only(_fixture_entry(paths=["crates/trit-gpu/src/deleted.rs"]))), True)
        check("tests 文件不存在 => 红",
              any("测试文件不存在" in e
                  for e in only(_fixture_entry(tests=["crates/trit-gpu/tests/gone.rs::x"]))), True)
        renamed = _fixture_entry(tests=[
            "crates/trit-gpu/tests/wf32_plan.rs::plan_that_was_removed"])
        check("测试被删/改名 => 红（这条是索引不老化的关键）",
              any("找不到" in e for e in only(renamed)), True)
        check("tests 引用不带 :: => 红",
              any("钉不住任何判据" in e
                  for e in only(_fixture_entry(tests=["crates/trit-gpu/tests/wf32_plan.rs"]))), True)
        check("符号不在文件里 => 红（消息点名符号）",
              any("符号在文件里找不到" in e
                  for e in only(_fixture_entry(symbols=[
                      "crates/trit-gpu/src/matmul_wf32_tiled.wgsl::gone_symbol"]))), True)
        check("符号所在文件没了 => 红（消息要说文件不存在，不能只说找不到）",
              any("符号所在文件不存在" in e
                  for e in only(_fixture_entry(symbols=[
                      "crates/trit-gpu/src/deleted.wgsl::btile"]))), True)

        dup = _fixture_entry(id="other-entry")
        check("id 重复 => 红",
              any("重复" in e for e in audit({"version": VERSION, "roots": roots,
                                             "entries": [good, dict(good)]}, roots)[0]), True)
        check("空索引 => 红（没条目等于没门）",
              audit({"version": VERSION, "roots": roots, "entries": []}, roots)[0] != [], True)
        check("version 不对 => 红",
              audit({"version": 2, "roots": roots, "entries": [good]}, roots)[0] != [], True)
        check("坏 JSON => 报错串", load_map(root / "nope.json")[1] != "", True)

        entries = [good,
                   _fixture_entry(id="wf32-layer-chain", title="WF32 层间链式（激活不出显存）",
                                  tags=["gpu", "wf32", "batching"],
                                  summary="上一层 out 在显存里重排成下一层 b，只首层上传、只末层回读。",
                                  paths=["crates/trit-gpu/src/matmul_wf32_tiled.wgsl"],
                                  tests=["crates/trit-gpu/tests/wf32_plan.rs::chain_keeps_work"],
                                  related=["wf32-tiled-kernel"]),
                   _fixture_entry(id="bank-padding", title="共享内存 bank 填充",
                                  tags=["gpu", "wgsl", "bank-conflict"],
                                  summary="行距从 8 个 vec4 改成 9 个，避开 16 列同 bank。",
                                  paths=["crates/trit-gpu/src/matmul_wf32_tiled.wgsl"])]
        scored = search(entries, "bank conflict")
        check("搜索按标签优先排序", [e["id"] for _s, e in scored][0], "bank-padding")
        # 只在正文里出现过同一个词、但没有该标签的条目，必须排在"打了标签"的条目后面
        body_only = _fixture_entry(id="body-only", title="分块的另一半", tags=["gpu"],
                                   summary="这里顺带提到了 bank 与 conflict 两个词。",
                                   tests=[])
        tagged = _fixture_entry(id="tagged-only", title="填充", tags=["bank-conflict"],
                                summary="只在标签上挂了这个名字。",
                                paths=["crates/trit-gpu/src/matmul_wf32_tiled.wgsl"], tests=[])
        ranked = [entry["id"] for _score, entry in search([body_only, tagged], "bank conflict")]
        check("标签命中优先于正文命中", ranked, ["tagged-only", "body-only"])
        check("搜索无命中返回空", search(entries, "zzz-nothing"), [])
        check("多标签是 AND", sorted(e["id"] for e in filter_entries(entries, tags=["gpu", "wf32"])),
              ["wf32-layer-chain", "wf32-tiled-kernel"])
        check("--any-tag 是 OR", len(filter_entries(entries, tags=["tiling"], any_tag=True)), 1)
        check("按 status 过滤", len(filter_entries(entries, tags=[], status="landed")), 3)
        check("按 kind 过滤", len(filter_entries(entries, tags=[], kind="gate")), 0)
        check("反查文件命中多条", len(where_entries(entries, "matmul_wf32_tiled.wgsl")), 3)
        check("反查测试名", [e["id"] for e in where_entries(entries, "chain_keeps_work")],
              ["wf32-layer-chain"])
        check("反查标签", len(where_entries(entries, "bank-conflict")), 1)
        check("反查不存在的串 => 空", where_entries(entries, "nothing-here"), [])

        first = suggest_tags(["crates/trit-gpu/src/matmul_wf32_tiled.wgsl",
                              "crates/trit-gpu/tests/wf32_plan.rs"], ["perf(gpu): tiled"])
        again = suggest_tags(["crates/trit-gpu/src/matmul_wf32_tiled.wgsl",
                              "crates/trit-gpu/tests/wf32_plan.rs"], ["perf(gpu): tiled"])
        check("自动标签是确定性的（同输入同输出）", first, again)
        check("自动标签抓到 wgsl", sorted(first.get("wgsl", [])) != [], True)
        check("自动标签抓到 wf32", "wf32" in first, True)
        check("自动标签抓到 commit 前缀 perf", first.get("perf", []) != [], True)
        check("自动标签抓到 tests", "tests" in first, True)
        check("commit 的 scope 也进标签",
              suggest_tags([], ["perf(gpu): 分块"]).get("gpu", []) != [], True)
        check("空输入不发明标签", suggest_tags([], []), {})
        check("路径反斜杠也能归一",
              "gpu" in suggest_tags(["crates\\trit-gpu\\src\\x.rs"], []), True)

        check("digest 稳定", entry_digest(good), entry_digest(dict(good)))
        check("内容变了 digest 变", entry_digest(good) == entry_digest(_fixture_entry(title="别的")), False)

    failed = [label for label, ok in checks if not ok]
    for label, ok in checks:
        print(f"  {'FAILED' if not ok else 'ok    '} {label}")
    print(f"self-test: {len(checks) - len(failed)}/{len(checks)} passed")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--map", default=str(DEFAULT_MAP))
    parser.add_argument("--self-test", action="store_true")
    sub = parser.add_subparsers(dest="command")
    for name in ("search", "query", "where"):
        p = sub.add_parser(name)
        p.add_argument("target", nargs="?", default="")
        p.add_argument("--tag", action="append", default=[])
        p.add_argument("--any-tag", action="store_true")
        p.add_argument("--kind", default="")
        p.add_argument("--status", default="")
        p.add_argument("--repo", default="")
        p.add_argument("--limit", type=int, default=20)
        p.add_argument("--json", action="store_true")
    show = sub.add_parser("show")
    show.add_argument("id")
    show.add_argument("--json", action="store_true")
    tests = sub.add_parser("tests")
    tests.add_argument("id")
    checks = sub.add_parser("check")
    checks.add_argument("--json", action="store_true")
    sub.add_parser("tags")
    sug = sub.add_parser("suggest")
    sug.add_argument("--repo", default="")
    sug.add_argument("--root", default="")
    sug.add_argument("--range", default="HEAD~1..HEAD")
    add = sub.add_parser("add")
    add.add_argument("--file", required=True)
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    path = pathlib.Path(args.map)
    data, error = load_map(path)
    if error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1
    entries = data.get("entries", [])

    if args.command == "check":
        return cmd_check(data, args.json)
    if args.command == "suggest":
        return cmd_suggest(sys.argv[2:], data)
    if args.command == "add":
        return cmd_add(path, args.file)
    if args.command == "tags":
        tally: Counter[str] = Counter()
        for entry in entries:
            for tag in entry.get("tags", []):
                tally[normalize_tag(str(tag))] += 1
        for tag, count in tally.most_common():
            print(f"{count:>4}  {tag}")
        return 0
    if args.command in ("search", "query", "where"):
        if args.command == "search":
            rows = [entry for _score, entry in search(entries, args.target or "", args.limit)]
        elif args.command == "where":
            rows = where_entries(entries, args.target or "")[: args.limit]
        else:
            rows = filter_entries(entries, tags=args.tag, any_tag=args.any_tag, kind=args.kind,
                                  status=args.status, repo=args.repo)[: args.limit]
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            print(f"命中 {len(rows)} 条：")
            render(rows)
        return 0 if rows else 1
    if args.command == "show":
        for entry in entries:
            if entry.get("id") == args.id:
                print(json.dumps(entry, ensure_ascii=False, indent=2))
                return 0
        print(f"FAIL 没有这个 id：{args.id}", file=sys.stderr)
        return 1
    if args.command == "tests":
        for entry in entries:
            if entry.get("id") == args.id:
                for ref in _strings(entry, "tests"):
                    print(ref)
                return 0
        print(f"FAIL 没有这个 id：{args.id}", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
