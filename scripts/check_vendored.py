#!/usr/bin/env python3
"""镜像文件对账：从上游仓 copy 过来的脚本，内容必须逐字一致。

用法：
  python3 scripts/check_vendored.py                 # 对账，漂了就红
  python3 scripts/check_vendored.py --json
  python3 scripts/check_vendored.py --self-test

为什么要这个（2026-11-03 实测的形状）：`agent_claims.py` 在 `bfxh/trit` 和这里**各有一份**。
上游那份后来加了「同仓但路径不相交 ⇒ 只警不拦」和「跨仓同一份代码的两个落点」两条口径，
这里的副本停在旧版——两边的 `--self-test` **各自都是绿的**，判定却互相矛盾：
上游绿的是「不拦」，这里绿的是「同仓不同路径也拦」。闸门长两份、各自跑各自的 CI，
就没有任何人能发现规则已经分叉。这份记录 + 这个对账就是那个缺失的人。

口径：
- **拦**：`scripts/vendored.json` 读不到/JSON 坏、记录里的文件不存在、sha256 对不上。
  sha 对不上只有两种可能：有人只改了副本（正是事故形状），或上游又前进了没同步过来——
  两种都得有人看一眼，不能装绿。
- 不判上游漂移：这里没有上游的读权限，能证伪的只有「本地副本与当初同步的那份不一致」。
  真要跟上上游，重新 copy 后更新 `scripts/vendored.json` 的 `sha256` 与 `synced_from_commit`。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parents[1]
RECORD = ROOT / "scripts" / "vendored.json"


def sha256_of(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(record_path: pathlib.Path, root: pathlib.Path) -> tuple[list[str], list[str], list[dict]]:
    """返回 (拦, 警告, 明细)。绿 = 拦为空。"""
    if not record_path.is_file():
        return [f"镜像记录读不到：{record_path}（有副本却没账 ⇒ 判红）"], [], []
    try:
        data = json.loads(record_path.read_text(encoding="utf-8"))
        files = data["files"]
    except (ValueError, KeyError, TypeError) as exc:
        return [f"镜像记录格式不对：{type(exc).__name__}: {exc}"], [], []

    red: list[str] = []
    rows: list[dict] = []
    for entry in files:
        if not isinstance(entry, dict) or "path" not in entry:
            red.append(f"镜像记录里有条目不是带 path 的对象：{entry!r}")
            continue
        if "sha256" not in entry:
            # 缺 sha 的账什么都证明不了，不能当成「没有对账要求」而放行
            red.append(f"{entry['path']} 的记录没有 sha256（对不了账 ⇒ 判红）")
            rows.append({"path": entry["path"], "state": "unrecorded"})
            continue
        path = root / entry["path"]
        if not path.is_file():
            red.append(f"记录里的镜像文件不存在：{entry['path']}")
            rows.append({"path": entry["path"], "state": "missing"})
            continue
        actual = sha256_of(path)
        if actual != entry["sha256"]:
            red.append(
                f"{entry['path']} 与上游同步点不一致（记录 {entry['sha256'][:12]}…，"
                f"实际 {actual[:12]}…）——要么重新从 {entry.get('source', '上游')} 同步，"
                "要么把这次的改动推回上游再更新账"
            )
            rows.append({"path": entry["path"], "state": "drift"})
        else:
            rows.append({"path": entry["path"], "state": "match"})
    return red, [], rows


def self_test() -> int:
    import tempfile

    checks: list[tuple[str, bool]] = []

    def check(label: str, got, want) -> None:
        checks.append((label, got == want))

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "scripts").mkdir()
        body = b"def gate():\n    return 1\n"
        (root / "scripts" / "mirror.py").write_bytes(body)
        digest = hashlib.sha256(body).hexdigest()
        record = root / "scripts" / "vendored.json"

        def write_record(payload) -> None:
            record.write_text(json.dumps({"files": payload}), encoding="utf-8")

        write_record([{"path": "scripts/mirror.py", "source": "o/up", "sha256": digest}])
        check("逐字一致 => 绿", audit(record, root)[0], [])

        (root / "scripts" / "mirror.py").write_bytes(body + "# 只改了副本\n".encode())
        red, _, rows = audit(record, root)
        check("副本被本地改过 => 拦一条", (len(red), rows[0]["state"]), (1, "drift"))

        (root / "scripts" / "mirror.py").unlink()
        check("镜像文件没了 => 拦", len(audit(record, root)[0]), 1)

        record.unlink()
        check("没有账 => 拦（不当成没有副本）", len(audit(record, root)[0]), 1)

        record.write_text("{ not json", encoding="utf-8")
        check("账坏了 => 拦", len(audit(record, root)[0]), 1)

        (root / "scripts" / "mirror.py").write_bytes(body)
        write_record([{"path": "scripts/mirror.py"}])
        red, _, rows = audit(record, root)
        check("账里缺 sha256 => 拦（对不了账不许放行）", (len(red), rows[0]["state"]),
              (1, "unrecorded"))

    failed = [label for label, ok in checks if not ok]
    for label, ok in checks:
        print(f"  {'FAILED' if not ok else 'ok    '} {label}")
    print(f"self-test: {len(checks) - len(failed)}/{len(checks)} passed")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--record", default=str(RECORD))
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    root = pathlib.Path(args.root)
    red, warn, rows = audit(pathlib.Path(args.record), root)
    if args.json:
        print(json.dumps({"tool": "check-vendored", "red": red, "warn": warn,
                          "files": rows, "green": not red}, ensure_ascii=False))
    else:
        print(f"镜像 {len(rows)} 个｜拦 {len(red)} 条｜警 {len(warn)} 条")
        for row in rows:
            print(f"  [{row['state']}] {row['path']}")
        for line in red:
            print(f"  [拦] {line}")
        for line in warn:
            print(f"  [警] {line}")
        print("结论：" + ("绿（每个副本都和同步那一刻逐字一致）" if not red
                        else "红（副本漂了：要么重新同步，要么把改动推回上游）"))
    return 1 if red else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
