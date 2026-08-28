---
name: harness-evolve-agent-management
description: 管理自定义 agent 的迁移与生命周期:导出跨机 bundle(agent-export)、导入复用(agent-import,同名拒绝,--as 改名)、删除(agent-delete,无 --yes 只打印清单,用户确认后才执行)。内置 agent 一律不动;导入绝不覆盖;bundle 是快照,迭代后需重新导出。
license: Apache-2.0
---

# Agent 管理(导入 / 导出 / 删除)

管理自定义 agent 的生命周期:查看、导出跨机迁移 bundle、导入复用、删除。
与 `agent-export-package`(Harness 包注入主 agent)不同,本技能操作的是**完整
agent**(定义 + 演进状态 + 历史快照 + 技能),用于跨电脑迁移。

## 核心原则

1. **删除先看清单** — `agent-delete` 不带 `--yes` 只会打印将删清单并**拒绝执行**
   (退出码 2)。必须先把清单原样展示给用户、得到明确同意后才加 `--yes`。
   **禁止直接 --yes**。
2. **导入绝不覆盖** — 目标名已存在(见 `agent-list`)→ 拒绝并提示
   `--as <新名>` 改名导入;技能同名冲突由后端自动跳过(绝不覆盖目标机已有技能)。
3. **内置 agent 一律不动** — general-purpose 等内置 agent 导出/删除都会被后端
   拒绝;也不要拿内置名当 `--as` 目标。
4. **bundle 是快照** — bundle 固化导出时刻的定义与版本;之后迭代过的 agent 需
   **重新导出**才能把新版本带到目标机(同名覆盖即可)。
5. **只走 CLI** — 所有操作走下方命令,不直接改 `*.md` / `state.json` / 目录。

## 操作流程

> **CLI 入口**:所有命令走 `jiuwenswarm-harness-evolve <命令>`;若提示命令不存在
> (entry point 未安装),等价改用 `python -m jiuwenswarm.harness_evolve.cli <命令>`。
> 本技能与 jiuwenswarm 自带的 auto-harness run 任务调度是两套机制,勿混用。

### 1. 查看现状(任何操作前必做)

```
jiuwenswarm-harness-evolve agent-list
jiuwenswarm-harness-evolve agent-inspect --name <name>
```

确认:agent 是否存在、version、skills、是否内置(source)。操作对象不存在或为
内置 → 停下,不执行。

### 2. 导出(本机 → bundle,非破坏性)

```
jiuwenswarm-harness-evolve agent-export --name <agent> --out <目录>/<agent>-bundle
```

- 产物 `<agent>-bundle.zip`:定义 md(字节原样)+ state.json(版本状态)+ 全部
  历史快照 + frontmatter skills + manifest.json(全文件 sha256 校验清单)。
- 输出若提示"技能缺失(已跳过)",要明确告诉用户哪些技能没打进包(目标机需
  自行安装)。
- 无需确认;导出不影响本机任何数据。

### 3. 导入(bundle → 目标机,同名拒绝)

```
# 1. 先查目标名是否空闲(必做):
jiuwenswarm-harness-evolve agent-list
# 2. 空闲 → 直接导入:
jiuwenswarm-harness-evolve agent-import --file <bundle.zip>
# 3. 已存在 → 与用户确认后改名导入:
jiuwenswarm-harness-evolve agent-import --file <bundle.zip> --as <新名>
```

- 导入自动完成:定义落盘、版本/快照原样恢复(改名时快照身份一并重写,历史
  可回滚)、skills 拷入(已存在跳过)、**自动启用为子代理**(写入 config 的
  react.subagents),并**自动热重载 agent 服务**(无需重启即可调度)。
- 校验失败(损坏/篡改)会整体拒绝、目标机零残留。
- 汇报:新名字、版本、快照数、被跳过的技能(如有)、子代理是否启用。

### 4. 删除(危险操作,两步确认)

```
# 第一步:打印清单(不执行)
jiuwenswarm-harness-evolve agent-delete --name <agent>

# 第二步:用户看过清单并明确同意后才执行
jiuwenswarm-harness-evolve agent-delete --name <agent> --yes
```

- 清单包含:定义文件、sidecar(状态/快照/工作区)、harness 包、config 条目、
  引用它的 benchmark 列表。
- benchmark 默认**保留**并告警;只有用户明确要求连带删除时才加
  `--with-benchmarks`。
- 删除后 CLI 自动热重载 agent 服务,主 agent 立即不再调度该子代理(无需重启)。
- 用户说"删了吧"但没看过清单 → 先跑第一步给清单,再确认一次。

## 命令速查

| 操作 | 命令 | 危险级别 |
| --- | --- | --- |
| 列出 | `agent-list` | 无 |
| 查看 | `agent-inspect --name <n>` | 无 |
| 导出 | `agent-export --name <n> --out <p>` | 无(只读) |
| 导入 | `agent-import --file <zip> [--as <n>]` | 低(目标机落盘,不覆盖) |
| 删除 | `agent-delete --name <n> [--yes] [--with-benchmarks]` | **高(需 --yes)** |

## 与其它技能的衔接

- 创建与迭代走 `harness-evolve-agent-creation` / `harness-evolve-agent-optimization`;
- 迁移到目标机后即可当子代理用(已自动启用);若还要叠加进主 agent,再走
  `agent-export-package`(见 creation 技能第 6 步)。
