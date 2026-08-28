---
name: harness-evolve-benchmark-design
description: 为被测 harness 设计多 case benchmark,经 pilot 校准后冻结为正式基线。4 个固定输入;statement 公开、rubric 私有且打分项总和必须恰好 100;leak-check;只接受达标基线。
license: Apache-2.0
---

# Benchmark 设计

为被测 agent(harness)设计多 case benchmark,经 pilot 校准后冻结为正式基线,供后续评估与迭代使用。

## 固定输入(缺一不可,先向用户确认)

1. **Test Agent** — 被测 agent 名(`agent-list` 确认存在)
2. **目标能力** — 要评测的具体能力范围
3. **期望 baseline 分** — 校准的达标线(0-100)
4. **pilot 迭代预算** — 最多修订几轮后必须冻结

## 核心原则

1. **case 设计** — 每个 case 一输入一输出,有明确对错/优劣空间;多 case 覆盖目标能力的子维度。
2. **statement 公开,rubric 私有** — case 目录结构:
   ```
   benchmarks/<id>/CASE-<nnn>-<semantic>/
   ├── statement/README.md   # 公开:被测 agent 能看到的任务描述
   └── rubric/README.md      # 私有:含 "## Scoring" 表,打分项总和必须恰好 100
   ```
   rubric 内容**绝不写入 statement**,也不在对话中向被测 agent 泄露(后端保证不入被测工作区)。
3. **打分表格式** — `## Scoring` 段落后是 `| item | points | description |` 表;每项 points 为整数,总和必须 == 100,否则 `case-write`/`validate` 拒绝。
4. **pilot 校准** — 冻结前先逐 case 用 `eval-run` 试跑;不达标(低于期望 baseline 分)就修订 case(或被测 agent),在迭代预算内重跑。**只保留一份"最低分完整有效修订"临时副本**用于对比。
5. **冻结策略** — 预算内先出现达标的有效 pilot 修订 → 冻结它;否则冻结**最低分**修订。未选中的修订**不入 scoreboard**。
6. **leak check** — 冻结前必须跑 `leak-check` 确认 statement 与 rubric 无互相泄露。
7. **时序** — scoreboard 的 time 由后端生成(UTC),禁止手工编造;写后由后端重 parse 校验。
8. **旧 benchmark 永远只读** — 已冻结的 benchmark 及其 scoreboard **绝不修改/覆盖/删除**
   (历史分数是优化判据的生命线)。任何"补充/修改需求"——不管当初描述不严谨
   还是新增目标——一律走**版本化复制**(见操作流程第 7 步):`benchmark-evolve`
   复制出新版,在新版上增删改、重新 pilot → freeze → baseline。**不提供
   "改旧尺子"的通道,也不要手动改 frozen benchmark 的文件。**

## 操作流程

> **CLI 入口**:所有命令走 `jiuwenswarm-harness-evolve <命令>`;若提示命令不存在
> (entry point 未安装),等价改用 `python -m jiuwenswarm.harness_evolve.cli <命令>`。
> 本技能与 jiuwenswarm 自带的 auto-harness run 任务调度是两套机制,勿混用。

### 0. 检查是否可基于已有 benchmark(每次设计前必做)

```
jiuwenswarm-harness-evolve benchmark-list
```

- 用户明确说"基于已有 benchmark 修改/重建" → 定位目标 benchmark,直接走
  **第 7 步版本化复制**流程(跳过第 1 步 init)。
- 用户没说、但需求与某个已有 benchmark **十分接近**(同 test_agent 且能力域
  重叠:标题/描述语义相似)→ **先询问用户**:"现有 benchmark `<id>`
  (v<n>, <title>)与你的需求很接近,是否基于它建新版?";确认 → 第 7 步;
  否认 → 按全新 benchmark 走第 1 步。
- 无相似 benchmark → 按全新 benchmark 走第 1 步。

### 1. 收集 4 输入 → `benchmark-init`

```
jiuwenswarm-harness-evolve benchmark-init \
  --benchmark <id([a-z0-9-]{3,64})> \
  --agent <被测 agent 名> \
  --title "<标题>" \
  --description "<描述>"
```

### 2. 逐 case 写 statement/rubric(写到临时文件再引用)

```
jiuwenswarm-harness-evolve case-write \
  --benchmark <id> --case CASE-001-summarize \
  --statement <workspace>/cases/CASE-001/statement.md \
  --rubric <workspace>/cases/CASE-001/rubric.md
```

每个 case 一个 `CASE-<nnn>-<semantic>` 名(semantic 用短横线小写)。

### 3. 校验 + leak check

```
jiuwenswarm-harness-evolve benchmark-validate --benchmark <id>
jiuwenswarm-harness-evolve leak-check --benchmark <id>
```

校验失败 → 修订后重写;leak 报告逐条处理,确认无泄露才继续。

### 4. pilot 校准(每 case 一 run,冻结前不限)

```
jiuwenswarm-harness-evolve eval-run --agent <被测 agent> --benchmark <id> --case CASE-001
```

记录每 case 得分与失败码。未达期望分 → 分析原因:case 设计问题修 case,agent 行为问题修 agent(走 agent-edit,见 optimization 技能),在预算内重跑。

### 5. 冻结 + 记录基线 + 入 scoreboard

```
# 先跑一次全矩阵作为基线证据(默认 --parallel 2 子进程并发)
jiuwenswarm-harness-evolve eval-matrix --agent <被测 agent> --benchmark <id> \
  [--runs N] [--parallel P] --tag baseline
jiuwenswarm-harness-evolve benchmark-freeze --benchmark <id>
jiuwenswarm-harness-evolve baseline-record --name <被测 agent> [--version <N>]
jiuwenswarm-harness-evolve scoreboard-append --benchmark <id> --file <evaluations/xxx.yaml>
```

`scoreboard-append` 的输入来自 `eval-matrix` 的产物(证据归档)。后端校验:frozen 门槛、版本严格大于末条、schema 完整性;失败即拒绝,不强制写入。

### 6. 验收

```
jiuwenswarm-harness-evolve scoreboard-read --benchmark <id>
```

向用户汇报:冻结了哪个修订、基线分、每个 case 的得分。**未选中的 pilot 修订不回写 scoreboard**。

### 7. 版本化复制:基于已有 benchmark 建新版(需求补充/修改的唯一通道)

```
jiuwenswarm-harness-evolve benchmark-evolve --benchmark <已冻结 id> [--as <新id>]
```

- 前置:源 benchmark **必须已冻结**(未冻结会报错——尺子未定稿时直接在原
  benchmark 上改即可);默认新 id 为 `<原id>-v<N>`,可用 `--as` 自定义;
  `--title`/`--description` 可覆盖继承值。
- 新版 = 继承全部 case + **空 scoreboard**(新周期新基线);config 记录
  `version = 源版本+1`、`parent = <原id>`;源 benchmark 与源 scoreboard
  **一个字节不动**。
- 新版未冻结 → 增删改:`case-write` 写新/覆盖旧 case、`case-delete --benchmark
  <新id> --case <id>` 删除过时 case → 按第 3-6 步重新 validate / leak-check /
  pilot / freeze / baseline。

## 简化对照(与 Penguin benchmark-design 的差异)

| Penguin | 本实现 |
| --- | --- |
| benchmark_config.toml 三字段 | + `test_agent` / `version` / `parent` 字段(版本化演化) |
| run_subagent 纯 YAML 协议 | `eval-run --json` 逐 cell(prompt 约定 + JSON 后解析) |
| 校准临时副本 + 自动冻结 | 人工判定 + `benchmark-freeze` 显式落盘 |
| 协议失败重发 | JSON 契约校验失败用同一证据重试一次,不重跑 |
