#!/usr/bin/env python3
"""openai/math 的本地参考索引：sync / status / search / show。

定位：**参考目录，不是依赖，也不是经过验证的结论**。
- 默认源：https://github.com/openai/math（Apache-2.0）
- 默认缓存：<repo>/.trit/mathref/index.json（只在你显式 sync 时写）
- 每条记录带 `verified: false`；history 里有撤回/修正，不要把目录当真理。

用法：
    python scripts/mathref.py sync [--repo .] [--source URL|PATH]
    python scripts/mathref.py status [--repo .] [--json]
    python scripts/mathref.py search "Hodge" [--limit 10]
    python scripts/mathref.py show 001
    python scripts/mathref.py --self-test
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys
import tempfile
import urllib.request
from typing import Any

DEFAULT_SOURCE = "https://raw.githubusercontent.com/openai/math/main"
REPO_URL = "https://github.com/openai/math"
CACHE_REL = pathlib.Path(".trit/mathref/index.json")
FAMILY_RE = re.compile(r"^\*\*(\d{3})\.\s+", re.MULTILINE)
PAPER_RE = re.compile(r"&emsp;\[([^\]]+)\]\(([^)]+)\)")
LEAN_RE = re.compile(r"\[Lean\]\(([^)]+)\)")
HTML_TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


def fetch(location: str, timeout: int = 30) -> str:
    if location.startswith("file://"):
        return pathlib.Path(urllib.request.url2pathname(location[7:])).read_text(encoding="utf-8")
    if location.startswith("http://") or location.startswith("https://"):
        request = urllib.request.Request(
            location, headers={"User-Agent": "repo-tools-mathref/1"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")
    return pathlib.Path(location).read_text(encoding="utf-8")


def clean(text: str) -> str:
    text = HTML_TAG_RE.sub("", text)
    text = text.replace("&emsp;", " ").replace("&nbsp;", " ")
    text = text.replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&")
    return SPACE_RE.sub(" ", text).strip()


def parse_contents(text: str) -> list[dict[str, Any]]:
    starts = list(FAMILY_RE.finditer(text))
    families: list[dict[str, Any]] = []
    for index, match in enumerate(starts):
        start = match.start()
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        block = text[start:end]
        head = re.match(r"^\*\*(\d{3})\.\s+(.+?)\*\*\s*(.*)", block, re.S)
        if not head:
            continue
        family_id = head.group(1)
        title = clean(head.group(2)).rstrip(".").strip()
        rest = head.group(3)
        summary = clean(rest.split("&emsp;", 1)[0])
        papers = [
            {"title": clean(paper_title), "path": path}
            for paper_title, path in PAPER_RE.findall(block)
        ]
        lean_match = LEAN_RE.search(block)
        families.append(
            {
                "id": family_id,
                "title": title,
                "summary": summary,
                "papers": papers,
                "lean": lean_match.group(1) if lean_match else None,
            }
        )
    return families


def parse_counts(readme: str, families: list[dict[str, Any]]) -> dict[str, int]:
    manuscripts = re.search(r"(\d+)\s+manuscripts", readme)
    result_families = re.search(r"(\d+)\s+result families", readme)
    return {
        "families": int(result_families.group(1)) if result_families else len(families),
        "manuscripts": int(manuscripts.group(1))
        if manuscripts
        else sum(len(family["papers"]) for family in families),
    }


def build_index(source: str, timeout: int = 30) -> dict[str, Any]:
    readme = fetch(source.rstrip("/") + "/README.md", timeout)
    contents = fetch(source.rstrip("/") + "/CONTENTS.md", timeout)
    history = fetch(source.rstrip("/") + "/history.md", timeout)
    families = parse_contents(contents)
    counts = parse_counts(readme, families)
    return {
        "source": REPO_URL,
        "source_raw": source,
        "fetched_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "verified": False,
        "license": "Apache-2.0",
        "counts": counts,
        "families": families,
        "readme_excerpt": readme[:2000],
        "history_excerpt": history[:4000],
    }


def save_index(cache: pathlib.Path, index: dict[str, Any]) -> None:
    cache.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache.with_suffix(cache.suffix + ".tmp")
    temporary.write_text(json.dumps(index, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    temporary.replace(cache)


def load_index(cache: pathlib.Path) -> dict[str, Any]:
    if not cache.is_file():
        raise FileNotFoundError(f"没有索引：{cache}（先跑 sync）")
    return json.loads(cache.read_text(encoding="utf-8"))


def search(index: dict[str, Any], query: str, limit: int) -> list[dict[str, Any]]:
    needle = query.strip().lower()
    if not needle:
        return []
    hits: list[tuple[int, dict[str, Any]]] = []
    for family in index["families"]:
        haystack = " ".join(
            [family["id"], family["title"], family["summary"]]
            + [paper["title"] for paper in family["papers"]]
        ).lower()
        if needle not in haystack:
            continue
        score = 0
        if needle in family["id"]:
            score += 100
        if needle in family["title"].lower():
            score += 50
        if needle in family["summary"].lower():
            score += 10
        score += sum(1 for paper in family["papers"] if needle in paper["title"].lower())
        hits.append((score, family))
    hits.sort(key=lambda item: (-item[0], item[1]["id"]))
    return [family for _, family in hits[:limit]]


def find_family(index: dict[str, Any], family_id: str) -> dict[str, Any] | None:
    needle = family_id.strip().lower()
    for family in index["families"]:
        if family["id"].lower() == needle:
            return family
    for family in index["families"]:
        if family["id"].startswith(needle):
            return family
    return None


def family_url(path: str) -> str:
    return f"{REPO_URL}/blob/main/{path}"


def print_family(family: dict[str, Any], full: bool = False) -> None:
    print(f"{family['id']}. {family['title']}")
    if family["summary"]:
        print(f"  {family['summary']}")
    if family.get("lean"):
        print(f"  Lean: {family_url(family['lean'])}")
    for paper in family["papers"]:
        print(f"  - {paper['title']}: {family_url(paper['path'])}")
    if full:
        print()


def status(index: dict[str, Any], cache: pathlib.Path) -> None:
    print(f"source:   {index['source']}")
    print(f"cache:    {cache}")
    print(f"fetched:  {index['fetched_at']}")
    print(f"license:  {index['license']}")
    print(f"verified: false（原始研究目录；history 里有撤回/修正）")
    print(
        f"counts:   {index['counts']['families']} result families / "
        f"{index['counts']['manuscripts']} manuscripts"
    )


def self_test() -> int:
    failures: list[str] = []

    def case(name: str, fn) -> None:
        try:
            fn()
        except AssertionError as exc:
            failures.append(f"{name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - self-test reports, never crashes
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    contents = """# Mathematics manuscript collection

**721 manuscripts covering 373 result families.**

## Manuscript map

**001. Test family one.** Proves a test statement. ([Lean](lean/docs/001.md))

&emsp;[Paper one](preprints/one/paper.pdf)

We prove paper one.

**002. Test family two.** Proves another statement.

&emsp;[Paper two](preprints/two/paper.pdf)

We prove paper two.
"""
    readme = "# Readme\n\n721 manuscripts covering 373 result families.\n"
    with tempfile.TemporaryDirectory() as temp_dir:
        root = pathlib.Path(temp_dir)
        source = root / "source"
        source.mkdir()
        (source / "README.md").write_text(readme, encoding="utf-8")
        (source / "CONTENTS.md").write_text(contents, encoding="utf-8")
        (source / "history.md").write_text("# History\n", encoding="utf-8")
        cache = root / "cache/index.json"

        def parse_case() -> None:
            families = parse_contents(contents)
            assert len(families) == 2, len(families)
            assert families[0]["id"] == "001"
            assert families[0]["lean"] == "lean/docs/001.md"
            assert len(families[0]["papers"]) == 1
            assert families[1]["papers"][0]["path"] == "preprints/two/paper.pdf"

        def sync_case() -> None:
            index = build_index(source.as_uri())
            assert index["counts"] == {"families": 373, "manuscripts": 721}, index["counts"]
            assert index["verified"] is False
            save_index(cache, index)
            loaded = load_index(cache)
            assert loaded["families"][0]["title"] == "Test family one"

        def search_case() -> None:
            index = load_index(cache)
            hits = search(index, "family one", 10)
            assert len(hits) == 1 and hits[0]["id"] == "001"
            assert search(index, "no-such-thing", 10) == []

        def show_case() -> None:
            index = load_index(cache)
            family = find_family(index, "002")
            assert family is not None and family["title"] == "Test family two"
            assert find_family(index, "999") is None

        def missing_cache_case() -> None:
            try:
                load_index(root / "missing.json")
            except FileNotFoundError:
                return
            raise AssertionError("missing cache must raise FileNotFoundError")

        case("parse contents", parse_case)
        case("sync and load", sync_case)
        case("search", search_case)
        case("show", show_case)
        case("missing cache", missing_cache_case)

    if failures:
        for failure in failures:
            print(f"FAIL self-test {failure}")
        print(f"self-test FAIL（{len(failures)} 项）")
        return 1
    print("PASS self-test：5 个用例（解析/同步/搜索/show/缺缓存）")
    return 0


def cache_path(repo: pathlib.Path, explicit: str | None) -> pathlib.Path:
    return pathlib.Path(explicit) if explicit else repo / CACHE_REL


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    sub = parser.add_subparsers(dest="command")

    for name in ("sync", "status", "search", "show"):
        command = sub.add_parser(name)
        command.add_argument("--repo", default=".")
        command.add_argument("--cache")
        if name == "sync":
            command.add_argument("--source", default=DEFAULT_SOURCE)
            command.add_argument("--timeout", type=int, default=30)
        if name == "search":
            command.add_argument("query")
            command.add_argument("--limit", type=int, default=10)
        if name == "show":
            command.add_argument("family")
        if name in ("status", "search", "show"):
            command.add_argument("--json", action="store_true")

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
    cache = cache_path(repo, args.cache)

    try:
        if args.command == "sync":
            index = build_index(args.source, args.timeout)
            save_index(cache, index)
            print(
                f"已同步 {index['counts']['families']} families / "
                f"{index['counts']['manuscripts']} manuscripts -> {cache}"
            )
            print("verified=false：这是原始研究目录，不是已验证结论。")
            return 0

        index = load_index(cache)
        if args.command == "status":
            if args.json:
                print(json.dumps({**index, "cache": str(cache)}, ensure_ascii=False, indent=2))
            else:
                status(index, cache)
            return 0
        if args.command == "search":
            hits = search(index, args.query, args.limit)
            if args.json:
                print(json.dumps(hits, ensure_ascii=False, indent=2))
            else:
                for family in hits:
                    print_family(family)
            return 0
        if args.command == "show":
            family = find_family(index, args.family)
            if family is None:
                print(f"FAIL 找不到 family {args.family}", file=sys.stderr)
                return 1
            if args.json:
                print(json.dumps(family, ensure_ascii=False, indent=2))
            else:
                print_family(family, full=True)
            return 0
    except (OSError, ValueError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
