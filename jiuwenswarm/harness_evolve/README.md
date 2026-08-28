# harness_evolve — Penguin 式 Agent Harness 自演进闭环

在 JiuwenSwarm 中实现 Penguin Harness 四技能闭环:创建专用 agent(harness) →
构建 benchmark → 标准化管理 → 用 harness 执行评测获得诊断 → 基于结果自动迭代 harness。
**不影响现有 jiuwenswarm 功能**(本包独立,无任何现有模块反向依赖它)。

## 闭环全景(与 4 个 SKILL.md 技能一一对应)

```
┌────────────────┐  ┌─────────────────┐  ┌──────────────────┐
│ agent-creation │→│ benchmark-design │→│ agent-evaluation  │
│ 用户创建 harness│  │ 4 输入+pilot 校准 │  │ 单 case 隔离执行+打分 │
└────────────────┘  └─────────────────┘  └──────────────────┘
        ▲                                        │
        │            ┌───────────────────────────┘
        │            ▼
┌────────────────┐  ┌──────────────────────────────────┐
│ agent-optimization │  Reference=scoreboard 末条;严格大于才接受 │
│ 快照→proposal→编辑→复评→接受/回滚      │
└────────────────┘  └──────────────────────────────────┘
```

- **创建**:`agent-register`(AgentConfigService 定义 + sidecar 版本 v1 + 自动写入 config.yaml `react.subagents.<name>.enabled: true`,让主 agent 可直接调度它)
- **benchmark**:`benchmark-init` → `case-write`×N(statement 公开 / rubric 私有,打分项和=100)→ `validate`/`leak-check` → pilot 校准 → `benchmark-freeze` → `baseline-record` → `scoreboard-append`
- **评估**:`eval-run`(单 cell,`--json`)/ `eval-matrix`(全 case×runs,证据归档不入 scoreboard)/ `report`(诊断报告)
- **优化**:`optimize` 一键复合,或手工 `snapshot-create` → `agent-edit` → `eval-matrix` → 判定 → `scoreboard-append` / `agent-restore`

## CLI

```
jiuwenswarm-harness-evolve <命令> --data-dir <数据根目录> ...
```

退出码:0 成功 / 2 用户输入错误 / 3 执行失败。所有命令支持 `--json`。

### agent 管理
| 命令 | 说明 |
| --- | --- |
| `agent-register --name --description --prompt-file [--model --tools --skills --max-iterations --when-to-use --location]` | 新建 harness(重名/内置保护,惰性建 sidecar v1,**自动注册进 config 的 react.subagents 启用为主 agent 子代理,并自动热重载 agent 服务,免重启生效**) |
| `agent-list` / `agent-inspect --name` | 列出(含版本)/ 查看完整定义与 sidecar |
| `agent-version --name` | sidecar 版本 / next_version / baseline |
| `agent-export-package --name [--activate]` | 打包为 Harness 包注入主 agent(重启或 web 面板激活;同名覆盖重导出,包 id 稳定) |
| `agent-export --name --out` | 导出跨机迁移 bundle(定义+状态+快照+skills,zip) |
| `agent-import --file [--as]` | 从 bundle 导入(同名拒绝,--as 改名;自动启用子代理) |
| `agent-delete --name [--yes] [--with-benchmarks]` | 删除 agent 及关联产物(需 --yes 确认;内置拒绝) |

### benchmark
| 命令 | 说明 |
| --- | --- |
| `benchmark-init --benchmark --agent --title --description` | 建 benchmark(4 输入) |
| `case-write --benchmark --case CASE-<nnn>-<semantic> --statement <f> --rubric <f>` | 写 case(rubric 和必须 == 100,frozen 后拒绝) |
| `benchmark-validate` / `leak-check` | 结构校验 / statement↔rubric 泄露检查 |
| `benchmark-freeze [--baseline-version N]` | 冻结(之后只读) |
| `scoreboard-append --file <Evaluation.yaml>` | 追加已接受评估(frozen+版本单调+schema 校验) |
| `scoreboard-read [--version N]` | 读 scoreboard |
| `benchmark-export --out <zip>` / `benchmark-import --file <zip> [--as 新id]` | 迁移 |
| `benchmark-list` | 列出全部 benchmark(版本/冻结/被测 agent/父版本) |
| `benchmark-evolve --benchmark [--as 新id] [--title] [--description]` | **版本化复制**(源须已冻结):继承全部 case + 空 scoreboard,新版解冻可增删改;源与源 scoreboard 原样保留 |
| `case-delete --benchmark --case` | 删除 case(仅未冻结 benchmark) |

### 评估
| 命令 | 说明 |
| --- | --- |
| `eval-run --agent --benchmark --case [--model --no-trace --json]` | 单 cell 执行+打分(不要求 frozen,pilot 可用) |
| `eval-matrix --agent --benchmark [--runs N --parallel P --tag]` | 全矩阵聚合为 Evaluation(不入 scoreboard);默认 P=2 子进程并发,`--parallel 1` 回顺序 |
| `report --benchmark --agent [--tag --target]` | 渲染最近矩阵证据为诊断报告 |

### 优化
| 命令 | 说明 |
| --- | --- |
| `snapshot-create --name [--version N]` | 编辑前快照(存在即拒,禁覆盖) |
| `agent-edit --name --expected-version N [--prompt-file --description --model --tools --skills --max-iterations --when-to-use --force]` | 白名单字段编辑(前置快照+漂移检测+版本+1) |
| `agent-restore --name --version N` | 回滚(文件恢复+sha256 校验+版本回退;next_version 不回退) |
| `baseline-record --name [--version N]` | 记录冻结时基线版本 |
| `optimize --agent --benchmark --target <分> [--rounds 3 --runs 1 --parallel P --model]` | 一键复合闭环(需 scoreboard 已有已接受基线;每轮矩阵默认 P=2 子进程并发,`--parallel 1` 回顺序) |

## 打包注入主 agent(Harness 包)

创建/迭代好的 agent 除了作为**子代理**被主 agent 调度,还可打包成 **Harness 包**:
web 端"Harness Package 管理"面板管理的即是这套机制。包的 prompt 会**叠加**到主
agent 的 system prompt(不是替换主 agent),让主 agent 直接获得该 agent 的角色与
技能能力。

```bash
jiuwenswarm-harness-evolve agent-export-package --name <agent> [--activate]
```

- 输出包位于 `~/.jiuwenswarm/auto-harness/runtime_extensions/<hash8>/<name>/`,
  包 id 形如 `pkg_<hash8>_<name>_<时间戳>`;**同名覆盖重导出**:内容更新、包 id 不变、
  激活状态保留。
- 打包内容 = prompt 正文(prompt_sections,双语同内容)+ frontmatter skills(拷入
  包内)+ metadata 溯源(agent_version / md 指纹 / model / declared_tools)。
  **工具一律不打包**:文件工具与 WebSearch/WebFetch 主 agent 默认自带,而热加载的
  绑定语义是"新增工具",同名言硬冲突(openjiuwen extension_binder 整体抛错)——把
  它们写进包会让激活直接失败。
- **生效方式(实测确认)**:只有 web 端 Harness Package 管理面板的热加载是生效途径
  (`--activate` 只是预标记);agent 服务重启时 `reset_harness_packages_state` 会把
  激活状态清回 Native Agent,包文件保留、需重新到面板激活。
- **与评测闭环的关系**:评测评估的是**定义本身**(subagent 形态,把定义当独立
  agent 完整跑 case);harness 包是导出时刻的**快照**,不参与评测闭环,迭代
  (agent-edit)后需重新 `agent-export-package` 同名覆盖同步。
- 每次导出前用 openjiuwen 官方 loader 自校验 manifest,失败会报错且**旧包保留**。

## 热重载(变更后免重启生效)

以下命令成功后会**自动通知运行中的 agent 服务热重载** subagent 定义,无需重启:

`agent-register` / `agent-edit` / `agent-restore` / `agent-delete` / `agent-import`

- 实现:CLI 直连 agent server(`AGENT_SERVER_HOST`/`AGENT_SERVER_PORT`,默认
  `ws://127.0.0.1:18092`,由 `~/.jiuwenswarm/config/.env` 提供)发
  `agent.reload_config` 消息(params 空 = 全量重读磁盘定义;指纹相同跳过 =
  幂等),与 web 面板 agent 管理的热生效是同一机制。
- 连不上服务时**最多重试 5 次、间隔 3 秒**,仍失败提示"重启 agent 服务后生效"
  (失败不阻断主命令);环境变量 `JIUWENSWARM_HARNESS_EVOLVE_NO_HOT_RELOAD=1`
  可关闭自动通知。

## 跨机迁移(agent-export / agent-import / agent-delete)

迭代好的 agent 可导出为 **bundle**(zip)搬到另一台电脑复用:

```bash
jiuwenswarm-harness-evolve agent-export --name <agent> --out <路径>   # 本机导出
jiuwenswarm-harness-evolve agent-import --file <bundle.zip>          # 目标机导入
jiuwenswarm-harness-evolve agent-delete --name <agent> --yes         # 删除(需确认)
```

- **bundle 内容**:定义 md(字节原样)+ sidecar(state.json 版本状态 + 全部历史
  快照)+ frontmatter skills;manifest.json 记录全文件 sha256,导入时三层校验
  (结构/完整性/语义),任一失败零残留。
- **导入**:同名已存在 → 拒绝,`--as <新名>` 改名导入(快照自动重打修正身份,
  版本历史完整可回滚);state.json 的 md_path/md_sha256 自动重写为新机路径;
  skills 目标已存在 → 跳过不覆盖;自动写入 config 启用为子代理(与
  agent-register 同款)。
- **删除**:不带 `--yes` 只打印将删清单并拒绝(退出码 2);`--yes` 按序清理
  config 条目 → harness 包(元数据 rescan 自愈)→ sidecar(状态/快照/工作区)→
  定义文件最后删;引用它的 benchmark 默认保留+告警,`--with-benchmarks` 一并
  删除;内置 agent 一律拒绝。
- **与 `agent-export-package` 的区别**:后者打包 **Harness 包**注入主 agent
  (叠加 prompt,web 面板热加载);本组命令导出**完整 agent**(定义+演进状态),
  用于跨机迁移复用。

## 数据布局(默认 `~/.jiuwenswarm/harness-evolve/`,尊重 JIUWENSWARM_DATA_DIR)

```
harness-evolve/
├── agents/<name>/
│   ├── state.json                # sidecar:version/next_version/md_path/md_sha256/baseline_version
│   ├── snapshots/v<N>.tar.gz     # 编辑前快照(原子,禁覆盖)
│   └── workspaces/<bid>/<case>/run<nn>-<uuid8>/   # 每次评测唯一工作区(statement+skills,rubric 绝不入内)
└── benchmarks/<id>/
    ├── benchmark_config.toml     # title/description/test_agent/runs=1/frozen
    ├── scoreboard.yaml           # evaluations: [];只追加已接受;frozen 门槛+版本严格大于末条
    ├── CASE-<nnn>-<semantic>/{statement,rubric}/README.md
    └── evaluations/*.yaml        # 每 cell 证据归档(被拒候选也留,不入 scoreboard)
```

## 关键语义(与 Penguin 对齐)

- **rubric**:打分项和必须恰好 100;`## Scoring` 表 `| item | points | description |`;rubric 只进打分员上下文,**绝不进被测 agent 工作区**。
- **打分**:prompt 约定 + JSON 后解析;校验 = item ⊆ rubric、score ∈ [0, points]、total == items 之和(所得总分,可低于 100);坏输出用同一证据重试一次,仍败 → `grading_failed`。
- **失败分类**:构造/启动异常 = `launch_failed`(不可打分);正常跑完但产物差 = `ok`(可打分,低分);评测期间被测定义被改 = `version_changed`;cost 恒 null(openjiuwen 0.1.16 无货币成本 API)。
- **优化判据**:接受 = 每 cell 有效 且 顶层均分**严格大于** scoreboard 末条(Reference),无统计检验;被拒候选不入 scoreboard、版本不复用;运行时版本不一致 → 立即停止。
- **benchmark 演化**:已冻结的 benchmark 及其 scoreboard **永远只读**;需求补充/修改
  (表述不严谨或新增目标)一律 `benchmark-evolve` 版本化复制出新版(继承 cases +
  空 scoreboard,config 记 version/parent),在新版上 case-write/case-delete 增删改
  后重新 pilot → freeze → baseline——旧分数永远可比,判据链不被污染。

## 真模型端到端手动验证步骤(成本控制)

前提:config 有 `models.defaults`(优先用本地模型;用外部 API 前先确认成本)。全程
`--max-iterations 3`、2 个 case、`--runs 1`,控制在个位数次模型调用。

1. **创建 harness**
   ```bash
   printf '你是代码评审 agent。阅读 README 后按要求输出评审结论。' > /tmp/reviewer.md
   jiuwenswarm-harness-evolve agent-register --name review-agent \
     --description "code reviewer" --prompt-file /tmp/reviewer.md --max-iterations 3
   jiuwenswarm-harness-evolve agent-inspect --name review-agent
   ```

2. **构建 2 case benchmark**
   ```bash
   jiuwenswarm-harness-evolve benchmark-init --benchmark review-bench \
     --agent review-agent --title "Review" --description "review quality"
   # case 1:statement 描述"审查这段代码的边界条件",rubric 表(correctness 60/format 40)
   jiuwenswarm-harness-evolve case-write --benchmark review-bench \
     --case CASE-001-boundary --statement /tmp/c1-statement.md --rubric /tmp/c1-rubric.md
   jiuwenswarm-harness-evolve case-write --benchmark review-bench \
     --case CASE-002-clarity  --statement /tmp/c2-statement.md --rubric /tmp/c2-rubric.md
   jiuwenswarm-harness-evolve benchmark-validate --benchmark review-bench
   jiuwenswarm-harness-evolve leak-check --benchmark review-bench
   ```

3. **pilot 校准(每 case 一 run)**
   ```bash
   jiuwenswarm-harness-evolve eval-run --agent review-agent --benchmark review-bench --case CASE-001-boundary --json
   jiuwenswarm-harness-evolve eval-run --agent review-agent --benchmark review-bench --case CASE-002-clarity  --json
   ```
   检查:workspace 产物路径、`~/.jiuwenswarm/.agent/traces/dump-agent-<session_id>.txt`、score 与 items。

4. **冻结 + 基线入册**
   ```bash
   jiuwenswarm-harness-evolve eval-matrix --agent review-agent --benchmark review-bench --tag baseline
   jiuwenswarm-harness-evolve benchmark-freeze --benchmark review-bench
   jiuwenswarm-harness-evolve baseline-record --name review-agent
   jiuwenswarm-harness-evolve scoreboard-append --benchmark review-bench --file <evaluations/baseline.yaml>
   ```

5. **优化一轮(观察接受或回滚)**
   ```bash
   jiuwenswarm-harness-evolve optimize --agent review-agent --benchmark review-bench \
     --target 95 --rounds 1 --runs 1
   ```
   接受:`agent-version` 应 +1,`scoreboard-read` 出现新条目;拒绝:`agent-version` 回到基线,
   `evaluations/optimize-r1.yaml` 保留被拒证据。人工核对 agent md 内容未被直接改动过
   (全程应走 `agent-edit`)。

6. **回归**:跑 `tests/unit_tests/harness_evolve/` 全绿;确认 `agent_config_service.py`、
   `interface_deep.py`、`code_agent_rail.py` 无 diff。

## 技能安装说明

5 个技能位于 `jiuwenswarm/resources/agent/workspace/skills/harness-evolve-*/`(仓库内):
4 个闭环技能(创建/benchmark/评测/优化)+ 1 个管理技能
(`harness-evolve-agent-management`:agent 导入/导出/删除,删除须 --yes 两步确认)。
已安装的 jiuwenswarm 环境需手动复制到用户技能目录才能被主 agent 加载:

```bash
cp -r jiuwenswarm/resources/agent/workspace/skills/harness-evolve-* ~/.jiuwenswarm/agent/workspace/skills/
```

## 测试

```bash
pytest tests/unit_tests/harness_evolve/ -o addopts="" -p no:cacheprovider
```

(本环境 pytest.ini 的 addopts 需显式清空;仓库配置依赖 pytest-asyncio,该插件缺失时
`server/cli` 目录下个别 async 测试文件会报收集错误,与 harness_evolve 无关。)
