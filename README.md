# repo-tools

四个小工具：**不依赖 git 的字节级回退**、**失败自动回退的命令包装**、**一句话提交 GitHub**、**依赖脱离体检 + 离线自足**。
外加一个**项目默认值解析器**（默认 legacy）和一个 **openai/math 参考索引**（默认 `verified=false`）。
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

python3 scripts/profiles.py show --repo .   # 看项目生效默认（默认 legacy）
python3 scripts/profiles.py check --repo . --strict
                                            # 三进制 + NVIDIA 默认报警；strict 判红
python3 scripts/profiles.py init --repo . --profile trit
                                            # 只写该项目 .trit/project.json

python3 scripts/mathref.py sync --repo .    # 按需索引 openai/math
python3 scripts/mathref.py search "Hodge"   # 查 family / Lean / 论文
python3 scripts/mathref.py status --repo .  # 计数 + verified=false
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

## 默认值治理（profiles）

- **默认不改变任何项目**：没有 `.trit/project.json` = `legacy` = 上游构建 / NVIDIA / 旧文档。
- **只按项目 opt-in**：`legacy → hybrid → trit`；不写全局用户配置。
- **报警可关**：`profile=trit` 却用 `gpu.backend=nvidia` 默认警告；
  `gpu.allow_nvidia=true` 或 `gpu.alarm=false` 关闭。
- **成熟度闸门**：`profile=trit` 默认 `experimental`；`check --strict` 下警告判红。

详细字段、优先级、迁移路线见 [docs/PROFILES.md](docs/PROFILES.md)。

## 数学参考（openai/math）

- 默认参考：https://github.com/openai/math（Apache-2.0，721 手稿 / 373 families）。
- **原始研究目录，不是已验证结论**；每个 profile 默认 `mode=reference`、`verified=false`。
- `mathref.py sync/search/show/status` 按需建本地索引；不 vendoring、不写全局、不参与构建。
- `mode=dependency` 且 `verified=false` 时 `profiles.py check` 报警。

详见 [docs/MATHREF.md](docs/MATHREF.md)。

## 自测

```bash
for f in snapshot autoflow gitflow vendor deps profiles mathref; do python3 scripts/$f.py --self-test; done
```

## 状态

这四个文件同时**内置**在 [bfxh/trit](https://github.com/bfxh/trit) 的 `scripts/` 下（那边是变异门登记表的宿主）。
本仓是它们的公开上游；两处内容应保持一致，改动时两边同步。
