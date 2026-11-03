# 数学参考：openai/math

> 默认参考：**https://github.com/openai/math**（Apache-2.0）。
> 定位：**原始研究目录，不是经过验证的结论，也不是构建依赖。**
> 工具：`scripts/mathref.py`（零依赖，按需同步到 `<repo>/.trit/mathref/`）。

---

## 1. 这是什么

OpenAI 公开的数学手稿集合（2026-10）：

- **721 manuscripts / 373 result families**；
- 约 42% 的 top-line result 有 Lean 形式化；
- 包含正文 PDF、Lean、Comparator challenge、reasoning summaries；
- README 明确说：**这是 raw research data**，部分结果可能有问题；history 里有撤回与修正。

所以默认 `verified: false`；它适合做“看看别人做到哪、有没有相关形式化”的参考，
不适合直接当“正确答案”或构建依赖。

---

## 2. 默认怎么接

`profiles.py` 的每个 profile 默认都带：

```json
"math": {
  "reference": "https://github.com/openai/math",
  "mode": "reference",
  "verified": false
}
```

- `mode=reference`：只读参考，不进入构建。
- `mode=dependency`：才允许构建/生成消费；`verified=false` 时 `check` 报警。
- `mode=off`：关闭。
- 所有项目默认都是 `reference`，不需要改任何现有项目。

查看：

```bash
python scripts/profiles.py show --repo . --format json
# effective.math.reference / mode / verified
```

---

## 3. 本地索引：sync / search / show

不 vendoring 5.4 万文件；只在显式 `sync` 时写一个本地 JSON 索引。

```bash
# 从默认源同步（README/CONTENTS/history）到 <repo>/.trit/mathref/index.json
python scripts/mathref.py sync --repo .

# 状态：源、时间、计数、license、verified=false
python scripts/mathref.py status --repo .

# 搜索（family id / title / summary / paper title）
python scripts/mathref.py search "Hodge" --repo . --limit 5

# 看某个 family（按 id 或前缀）
python scripts/mathref.py show 001 --repo .
```

输出：

- `status --json`：完整索引摘要；
- `search --json`：family 数组；
- `show --json`：单个 family（含 papers/lean 链接）。

链接默认拼到 `https://github.com/openai/math/blob/main/...`。

---

## 4. 缓存与边界

- 默认缓存：`<repo>/.trit/mathref/index.json`；`--cache` 可改。
- `sync` 才写文件；`status/search/show` 只读。
- 不写用户目录、不写别的项目、不改 `Cargo.toml`。
- 网络失败会 `FAIL ...` 并退出 1；不会静默给空索引。
- 索引里保留 `verified: false` 和 `history_excerpt`，提醒有撤回/修正。
- 解析的是 `CONTENTS.md` 的 HTML 表格；如果上游改格式，`sync` 的计数/条目可能变少——先用 `status` 和 `--self-test` 对账。

---

## 5. 自测

```bash
python scripts/mathref.py --self-test
```

覆盖：解析 2 个 family、同步/加载、搜索、show、缺缓存报错。

真实同步对账（可选）：

```bash
python scripts/mathref.py sync --repo /tmp/mathref-demo
python scripts/mathref.py status --repo /tmp/mathref-demo
# 应为 373 result families / 721 manuscripts
```

---

## 6. 给库/智能体的建议

- **默认先查目录**：做数学相关任务时，先 `search` 有没有相关 family / Lean / 反例。
- **把它当线索，不当结论**：引用时标 `verified=false`；需要结论时自己复算或读 Lean。
- **不要复制进仓库**：保持引用 + 本地索引；避免 5.4 万文件的维护负担。
- **不要默认当依赖**：`mode=dependency` 必须显式设置，且 `verified=false` 会报警。
