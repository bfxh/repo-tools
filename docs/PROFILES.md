# 项目默认值（profiles）

> 目标：让“用不用三进制 / 用不用自研 GPU 内核 / 用不用新的文档管理”变成**按项目显式选择**，
> 默认永远是旧的；成熟后也只逐项目翻默认，不搞全局切换。

工具：`scripts/profiles.py`（零依赖，Python ≥ 3.11）。

---

## 1. 三条硬约束

1. **不写全局**：只读 `<repo>/.trit/project.json`；`init` 也只写这个文件。
2. **默认不改变任何项目**：没有配置 = `legacy` = 上游构建 / NVIDIA 后端 / 旧文档管理。
3. **优先级固定**：CLI > 环境变量 > 仓库配置 > 内置默认。

> 这就是“我开发不影响其他项目，也不影响你本身项目”的落地方式：
> 任何项目不加 `.trit/project.json`，行为和今天完全一样。

---

## 2. 内置默认

| 字段 | 默认 | 允许值 | 说明 |
|---|---|---|---|
| `profile` | `legacy` | `legacy` / `hybrid` / `trit` | 三档迁移 |
| `maturity` | `stable`（legacy） | `experimental` / `beta` / `stable` | `trit` 默认 `experimental` |
| `build.default` | `upstream` | `upstream` / `trit` | 构建用哪套 |
| `build.features` | `[]` | 字符串数组 | 例如 `["gpu"]` |
| `gpu.backend` | `nvidia` | `nvidia` / `trit` / `auto` | `auto` 由 profile 决定 |
| `gpu.allow_nvidia` | `true`（legacy） | 布尔 | 允许三进制项目用 NVIDIA |
| `gpu.alarm` | `true` | 布尔 | 三进制 + NVIDIA 时报警 |
| `docs.manager` | `legacy` | `legacy` / `trit` | 文档由谁管理 |

三档 profile：

| profile | 构建 | GPU | 文档 | maturity | 用途 |
|---|---|---|---|---|---|
| `legacy` | upstream | nvidia | legacy | stable | 默认，所有旧项目 |
| `hybrid` | upstream | trit | trit | beta | 先在 GPU/文档试 |
| `trit` | trit | trit | trit | experimental | 全量试用 |

---

## 3. 仓库配置

```json
{
  "version": 1,
  "profile": "trit",
  "maturity": "beta",
  "build": {
    "default": "trit",
    "features": ["gpu"]
  },
  "gpu": {
    "backend": "trit",
    "allow_nvidia": false,
    "alarm": true
  },
  "docs": {
    "manager": "trit"
  }
}
```

- 只写 `{"version":1,"profile":"trit"}` 也可以，其余字段继承 profile 默认。
- 未知字段/非法值/`version != 1` 一律报错，不静默忽略。
- `init` 拒绝覆盖已有文件；要覆盖必须显式 `--force`。

---

## 4. 报警与成熟度

| 条件 | 默认行为 |
|---|---|
| `profile=trit` 且 `maturity != stable` | 警告“仍是 experimental” |
| 已启用三进制但 `gpu.backend=nvidia` | 警告；`allow_nvidia=true` 或 `alarm=false` 关闭 |
| `gpu.backend=trit` 但 `build.features` 没有 `gpu` | 警告：构建不会编进 GPU 内核 |
| `check --strict` | 任何警告都判红 |

> “默认报警，可显式关闭”对应你说的“默认报警 关闭”：
> 报警默认开；要静默就写 `allow_nvidia=true`（允许 NVIDIA）或 `alarm=false`（连报警也关）。

---

## 5. 命令

```bash
# 看当前项目生效配置（JSON）
python scripts/profiles.py show --repo .

# 看从哪来的（sources 字段）
python scripts/profiles.py show --repo . --format json

# 只输出 cargo features（给构建脚本用）
python scripts/profiles.py show --repo . --format cargo

# 检查；警告不判红
python scripts/profiles.py check --repo .

# CI 用：警告也判红
python scripts/profiles.py check --repo . --strict

# 新建项目配置（默认 legacy；拒绝覆盖）
python scripts/profiles.py init --repo .
python scripts/profiles.py init --repo . --profile trit
```

常见覆盖：

```bash
python scripts/profiles.py check --profile trit --gpu-backend nvidia
python scripts/profiles.py check --profile trit --gpu-backend nvidia --allow-nvidia
python scripts/profiles.py check --profile trit --gpu-backend nvidia --no-alarm
```

---

## 6. 迁移路线（逐项目）

```text
legacy（默认，不动）
  → hybrid（GPU/文档先试；构建仍 upstream）
  → trit（全量；maturity 从 experimental → beta → stable）
  → 把该项目默认改成 trit（只改该项目的 .trit/project.json）
```

- **不搞全局默认**：不在用户目录写配置，不改别的仓。
- 成熟的标准由项目自己定：至少 CI 全绿 + 关键路径有对照读数 + 可一键回退。
- 回退：删除 `.trit/project.json` 或把 `profile` 改回 `legacy`；不需要改代码。

---

## 7. 与构建/GPU/文档的接法

| 消费方 | 怎么用 |
|---|---|
| 构建脚本 | `FEATURES=$(python scripts/profiles.py show --repo . --format cargo)` |
| CI | `python scripts/profiles.py check --repo . --strict` |
| GPU | 读 `gpu.backend`；`trit` 时必须启用 `gpu` feature |
| 文档 | 读 `docs.manager`；`trit` 时才走新的文档管理 |
| Git 替代软件 | 把 `.trit/project.json` 当项目级策略输入，不写全局 |

> 这只是**解析器/策略层**；具体“用哪个内核/哪个构建后端”由各项目自己的构建脚本执行。
> 不在本工具里偷偷改任何项目的 `Cargo.toml` / CI。

---

## 8. 非目标

- 不做全局用户配置（避免“一改影响所有项目”）。
- 不自动改 `Cargo.toml` 的 `default` features。
- 不替代各项目的构建系统；只给出**可解释的生效值 + 来源**。
- 不在 `show/check` 时创建 `.trit` 目录或写文件。

---

## 9. 验收

```bash
python scripts/profiles.py --self-test
python scripts/profiles.py check --repo .
python scripts/profiles.py check --repo . --profile trit --gpu-backend nvidia
python scripts/profiles.py check --repo . --profile trit --gpu-backend nvidia --strict
```

- 自测必须覆盖：默认 legacy、repo/env/CLI 覆盖、报警开关、unknown、init 拒绝覆盖、auto。
- `--strict` 必须能把“三进制 + NVIDIA”判红。
- 任何命令都不得写 `--repo` 之外的路径。
