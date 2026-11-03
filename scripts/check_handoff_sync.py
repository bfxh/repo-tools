#!/usr/bin/env python3
"""收工闸门：交接账本（LEDGER）与摘要（NOW）必须互相对得上。

为什么要有这个：`NOW.md` 是下一个会话开工唯一会读的东西。它一旦写飞——超了字节上限、
点了一个 LEDGER 里根本不存在的工作编号、或者把「最新 CHG」写成了一条旧的——下一个人就会
按一份不存在的账干活。这类错**不会让任何东西报错**，所以必须有个尺子替人盯着。

口径（分两档，噪音判成拦截，工具第二天就被绕过）：
- **拦**：文件读不到、NOW 超上限、摘要点名的 A 编号在 LEDGER 里找不到、摘要写的「最新 CHG」
  不是 CHANGES 里真最新的那条。这些是「证明不了」或「已经写飞」，不能放行。
- **警**：LEDGER 里 DONE 的项还在摘要里单列（该沉进 CLOSED 了）、LEDGER 里 open 的项摘要完全没提。
  这是成本问题，不是假账。

用法：`python scripts/check_handoff_sync.py --dir D:/KF/_handoff [--now-limit 3000]`
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

A_ID = re.compile(r"A\d+")
CHG_ID = re.compile(r"CHG-\d{4}-\d{2}-\d{2}-[A-Z]+")


def read_dir(root: pathlib.Path) -> tuple[dict[str, str], list[str]]:
    """读三件套。任何一个读不到都是「证明不了」，不是「没问题」。"""
    texts: dict[str, str] = {}
    problems: list[str] = []
    for name in ("LEDGER.md", "NOW.md", "CHANGES.md"):
        path = root / name
        try:
            texts[name] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"拦：{name} 读不出来（{type(exc).__name__}: {exc}）——读不到不等于没问题")
    return texts, problems


def ledger_states(ledger: str) -> tuple[set[str], set[str]]:
    """返回 LEDGER 里 (open 的工作编号, done 的工作编号)。"""
    open_ids: set[str] = set()
    done_ids: set[str] = set()
    for line in ledger.splitlines():
        stripped = line.strip()
        if stripped.startswith("- [ ]"):
            open_ids.update(A_ID.findall(stripped))
        elif stripped.startswith("- [x]"):
            done_ids.update(A_ID.findall(stripped))
    return open_ids, done_ids


def latest_chg(changes: str) -> str:
    """CHANGES 里最新的一条：按文件顺序取第一个出现的编号（新条目写在最上面）。"""
    for match in CHG_ID.finditer(changes):
        return match.group(0)
    return ""


def check_sync(texts: dict[str, str], now_limit: int) -> tuple[list[str], list[str]]:
    """核心判据。返回 (拦, 警)。"""
    reds: list[str] = []
    warns: list[str] = []
    now = texts.get("NOW.md", "")
    ledger = texts.get("LEDGER.md", "")
    changes = texts.get("CHANGES.md", "")
    size = len(now.encode("utf-8"))
    if size > now_limit:
        reds.append(f"拦：NOW.md {size} B 超了上限 {now_limit} B（先沉后剪：证据已在 LEDGER 的行才能删）")
    if not ledger.strip():
        reds.append("拦：LEDGER.md 是空的——摘要没有可核对的对象")
        return reds, warns
    ledger_ids = set(A_ID.findall(ledger))
    cited = set(A_ID.findall(now))
    ghosts = sorted(cited - ledger_ids, key=lambda item: (len(item), item))
    if ghosts:
        reds.append("拦：摘要点名了 LEDGER 里不存在的工作编号：" + " ".join(ghosts))
    pointer = CHG_ID.search(now)
    newest = latest_chg(changes)
    if changes.strip():
        if not pointer:
            reds.append("拦：摘要没写「最新 CHG-…」，下一个人无法确认自己读到的是不是新的")
        elif newest and pointer.group(0) != newest:
            reds.append(f"拦：摘要说最新是 {pointer.group(0)}，CHANGES 里实际最新是 {newest}")
    done_ids = done_ids_in(ledger)
    # 只看 CLOSED 之前那一段：CLOSED 段里的 `- DONE A<n>:` 正是它们该待的地方
    head = now.split("CLOSED")[0]
    stragglers = sorted({item for line in head.splitlines()
                         if line.strip().startswith("- DONE")
                         for item in A_ID.findall(line) if item in done_ids},
                        key=lambda item: (len(item), item))
    if stragglers:
        warns.append("警：LEDGER 已 DONE 但摘要还单列着（该沉进 CLOSED 段）：" + " ".join(stragglers))
    forgotten = sorted(set(open_in(ledger)) - cited, key=lambda item: (len(item), item))
    if forgotten:
        warns.append(f"警：LEDGER 里 open 的编号有 {len(forgotten)} 条摘要没提（前 8 条："
                     + " ".join(forgotten[:8]) + "）；摘要是开工唯一入口，漏了等于没人接")
    return reds, warns


def done_ids_in(ledger: str) -> set[str]:
    return ledger_states(ledger)[1]


def open_in(ledger: str) -> set[str]:
    return ledger_states(ledger)[0]


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="check_handoff_sync.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default="", help="交接账本目录（LEDGER/NOW/CHANGES 所在处）")
    parser.add_argument("--now-limit", type=int, default=3000, help="NOW.md 的字节上限")
    parser.add_argument("--self-test", action="store_true", help="跑内置自测（用临时夹具，不碰真账本）")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.dir:
        print("拦：没给 --dir——不知道要核对哪本账，就不许报「同步」")
        return 2
    root = pathlib.Path(args.dir)
    texts, problems = read_dir(root)
    if problems:
        for line in problems:
            print(line)
        return 1
    reds, warns = check_sync(texts, args.now_limit)
    for line in reds + warns:
        print(line)
    if reds:
        print(f"FAIL 拦 {len(reds)} 条 ⇒ 账本与摘要不一致")
        return 1
    print(f"OK ledger and digest in sync（NOW {len(texts['NOW.md'].encode('utf-8'))} B"
          f"｜警 {len(warns)} 条）")
    return 0


def self_test() -> int:
    import tempfile

    failures: list[str] = []
    total = [0]

    def check(name: str, actual, expected) -> None:
        total[0] += 1
        if actual != expected:
            failures.append(name)
            print(f"FAIL {name}：期望 {expected!r}，实际 {actual!r}")

    ledger_ok = ("- [ ] **A9 · 等人拍板**\n"
                 "  · 细节\n"
                 "- [x] **A8 · 已经做完**\n  · 细节\n")
    now_ok = ("# NOW\n\n"
              "Changes: CHANGES.md 最新 CHG-2026-11-03-B\n\n"
              "- A9 等人拍板\n- A8 做完\n")
    changes_ok = "# 变更记录\n\n## CHG-2026-11-03-B · 最新\n\n正文\n"

    reds, warns = check_sync({"LEDGER.md": ledger_ok, "NOW.md": now_ok, "CHANGES.md": changes_ok}, 3000)
    check("一切对得上时不判红", (reds, warns), ([], []))

    reds, _ = check_sync({"LEDGER.md": ledger_ok,
                          "NOW.md": now_ok + "\n- A77 凭空捏一条不存在\n",
                          "CHANGES.md": changes_ok}, 3000)
    check("摘要点名 LEDGER 里没有的编号 ⇒ 拦", reds, ["拦：摘要点名了 LEDGER 里不存在的工作编号：A77"])

    reds, _ = check_sync({"LEDGER.md": ledger_ok, "NOW.md": now_ok, "CHANGES.md": changes_ok},
                         len(now_ok.encode("utf-8")) - 1)
    check("NOW 超字节上限 ⇒ 拦", any("超了上限" in line for line in reds), True)
    reds, _ = check_sync({"LEDGER.md": "", "NOW.md": now_ok, "CHANGES.md": changes_ok}, 3000)
    check("LEDGER 空 ⇒ 拦（摘要没有可核对的对象，也不许转头去比 CHG）",
          any("LEDGER.md 是空的" in line for line in reds), True)

    reds, _ = check_sync({"LEDGER.md": ledger_ok,
                          "NOW.md": "# NOW\n\n- A9 x\n",
                          "CHANGES.md": changes_ok}, 3000)
    check("摘要没写最新 CHG ⇒ 拦", any("没写「最新 CHG" in line for line in reds), True)

    stale = ("# NOW\n\nChanges: CHANGES.md 最新 CHG-2026-11-03-A\n\n- A9 x\n")
    reds, _ = check_sync({"LEDGER.md": ledger_ok, "NOW.md": stale, "CHANGES.md": changes_ok}, 3000)
    check("摘要的「最新 CHG」不是真最新 ⇒ 拦",
          any("实际最新是 CHG-2026-11-03-B" in line for line in reds), True)

    reds, warns = check_sync({"LEDGER.md": ledger_ok,
                              "NOW.md": "# NOW\n\nChanges: 最新 CHG-2026-11-03-B\n\n",
                              "CHANGES.md": changes_ok}, 3000)
    check("LEDGER 空摘要 ⇒ 拦（读得出文件但没内容也一样证明不了）",
          (bool(reds), warns), (False, ["警：LEDGER 里 open 的编号有 1 条摘要没提（前 8 条：A9）；摘要是开工唯一入口，漏了等于没人接"]))

    done_line = now_ok + "- DONE A8: 还挂着一行\n"
    _reds, warns = check_sync({"LEDGER.md": ledger_ok, "NOW.md": done_line,
                               "CHANGES.md": changes_ok}, 3000)
    check("已 DONE 还在摘要单列 ⇒ 只警不拦", [line for line in warns if "沉进 CLOSED" in line],
          ["警：LEDGER 已 DONE 但摘要还单列着（该沉进 CLOSED 段）：A8"])
    closed_now = chr(10).join(["# NOW", "", "Changes: 最新 CHG-2026-11-03-B", "", "- A9 x", "", "CLOSED (artifacts in ledger):", "- DONE A8: 已沉底", ""])
    _reds, warns = check_sync({"LEDGER.md": ledger_ok, "NOW.md": closed_now,
                               "CHANGES.md": changes_ok}, 3000)
    check("CLOSED 段里的 - DONE 不再报警（那正是它们该待的地方）",
          [line for line in warns if "沉进 CLOSED" in line], [])

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "LEDGER.md").write_text(ledger_ok, encoding="utf-8")
        (root / "NOW.md").write_text(now_ok, encoding="utf-8")
        check("缺 CHANGES.md ⇒ 读不出来就判红（不当成没有变更）",
              bool(read_dir(root)[1]), True)
        (root / "CHANGES.md").write_text(changes_ok, encoding="utf-8")
        check("三件套齐了就读得出来", read_dir(root)[1], [])
        check("main：账本对得上 ⇒ rc=0", main(["--dir", str(root)]), 0)
        check("main：NOW 超上限 ⇒ rc=1（不是印一句警告就过去）",
              main(["--dir", str(root), "--now-limit", "10"]), 1)
        (root / "NOW.md").write_text(now_ok + "- A77 捏一个不存在的" + chr(10),
                           encoding="utf-8")
        check("main：摘要点错编号 ⇒ rc=1", main(["--dir", str(root)]), 1)
        check("main：没给 --dir ⇒ rc=2（不知道核对哪本账就不许报「同步」）", main([]), 2)
        (root / "NOW.md").write_text(now_ok, encoding="utf-8")
        (root / "CHANGES.md").unlink()
        check("main：少一件（CHANGES 没了）⇒ rc=1（读不到就不许报「同步」）",
              main(["--dir", str(root)]), 1)
        (root / "CHANGES.md").write_text(changes_ok, encoding="utf-8")
        (root / "LEDGER.md").write_bytes(b"\xff\xfe not utf8")
        check("LEDGER 不是 utf-8 ⇒ 判红", len(read_dir(root)[1]), 1)

    check("CHANGES 取第一条当最新（新条目写在最上面）",
          latest_chg("# t\n\n## CHG-2026-11-03-C · 新\n## CHG-2026-11-03-A · 旧\n"), "CHG-2026-11-03-C")
    check("CHANGES 空 ⇒ 最新是空串（不去比）", latest_chg("# 变更记录\n"), "")
    check("open/done 分类", ledger_states(ledger_ok), ({"A9"}, {"A8"}))

    if failures:
        for line in failures:
            print(line)
        print(f"self-test: {total[0] - len(failures)}/{total[0]} passed")
        return 1
    print(f"self-test: {total[0]}/{total[0]} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
