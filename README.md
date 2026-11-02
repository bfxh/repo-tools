# repo-tools

四个小工具：**不依赖 git 的字节级回退**、**失败自动回退的命令包装**、**一句话提交 GitHub**、**依赖脱离体检 + 离线自足**。
每个都带 `--self-test`（正例 + 逐项反证），CI 里逐个跑。

```bash
python3 scripts/snapshot.py save            # 存快照（内容寻址，不碰 git）
python3 scripts/snapshot.py verify          # 逐字节比
python3 scripts/snapshot.py restore --prune # 退回（默认不删你新写的文件）

python3 scripts/autoflow.py run -- <命令…>   # 跑命令；失败自动回退到快照

python3 scripts/gitflow.py publish --title "…" --files a b c
                                            # 点名文件 → 密钥扫描 → 提交 → 推 → PR → 等 CI → 绿即合
python3 scripts/gitflow.py diagnose --pr 210
                                            # 失败检查逐条给出：检查名 + 运行地址 + 日志尾部

python3 scripts/deps.py plan                # 某个 crate 能不能整块摘出去、缺哪几块
python3 scripts/deps.py detach <crate> --to <dir> --verify
python3 scripts/vendor.py save / restore    # 离线自足开关
```

## 设计约束

- **不依赖 git**：`snapshot` / `autoflow` 在任何目录、任何 git 状态下都能存能退；git 只做附注。
- **字节级**：按 SHA-256 内容寻址；回退后逐文件校验，换行/编码不会被顺手改掉。
- **默认不破坏**：`restore` 默认不删快照之后新写的文件（要删显式 `--prune`）；`gitflow` 不做 `git add -A`。
- **提交前拒绝**：`gitflow` 扫描 `sk-*`、GitHub PAT、Slack token、AWS key、私钥头，以及 `.env` 一类按文件名拒收。
- **可审计**：每个工具失败时打印**具体路径/行/命令**，不吞错。

## 要求

- Python ≥ 3.11（`deps.py` 用 `tomllib`）；
- `gitflow` 需要 `git` + `gh` 并已登录；其余工具只用标准库；
- `deps` / `vendor` 只对 Cargo 工作区有意义。

## 自测

```bash
for f in snapshot autoflow gitflow vendor deps; do python3 scripts/$f.py --self-test; done
```

## 状态

这四个文件同时**内置**在 [bfxh/trit](https://github.com/bfxh/trit) 的 `scripts/` 下（那边是变异门登记表的宿主）。
本仓是它们的公开上游；两处内容应保持一致，改动时两边同步。
