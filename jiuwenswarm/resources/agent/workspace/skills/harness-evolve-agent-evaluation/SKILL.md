---
name: harness-evolve-agent-evaluation
description: 用冻结(或 pilot)benchmark 对被测 harness 做单次隔离执行并打分,产出诊断。被测 agent 全程不见 rubric;错答=可打分(低分),启动/执行失败=不可打分;单次评估不写 scoreboard。
license: Apache-2.0
---

# Agent 评估

对被测 agent(harness)做**单 case 单次隔离执行 + 私有打分**,产出带诊断的结果。这是 benchmark 校准与 optimize 闭环的证据来源。

## 核心原则

1. **一次一 cell** — 单次执行只覆盖一个 case 的一个 run;矩阵评测见 `eval-matrix`(optimization 技能)。
2. **rubric 全程私有** — 被测 agent 的工作区只含 statement 拷贝与它的技能,**绝不包含 rubric/**(后端强制:rubric 只进打分员上下文)。对话中也不得向被测 agent 复述打分标准。
3. **错答 = 可打分** — 正常跑完但产物错/缺 → `status: ok`,照常打分(得低分)。**不可打分**仅限:启动/构造失败、执行异常(`launch_failed`)、评测期间被测 agent 定义被外部改动(`version_changed`)、两次打分输出均非法(`grading_failed`)。
4. **单次评估不写 scoreboard** — `eval-run`/`eval-matrix` 只归档证据;只有用户显式 `scoreboard-append` 才入册(frozen + 版本单调校验)。
5. **失败码语义** — `launch_failed`/`version_changed` 不是 0 分,是不可打分;修复后重跑,不掩盖。

## 操作流程

> **CLI 入口**:所有命令走 `jiuwenswarm-harness-evolve <命令>`;若提示命令不存在
> (entry point 未安装),等价改用 `python -m jiuwenswarm.harness_evolve.cli <命令>`。
> 本技能与 jiuwenswarm 自带的 auto-harness run 任务调度是两套机制,勿混用。

### 单 case 评估(校准/pilot 场景)

```
jiuwenswarm-harness-evolve eval-run \
  --agent <被测 agent> \
  --benchmark <benchmark id> \
  --case CASE-001 \
  [--model <模型名>] \
  [--json]          # 输出机器可读结果
```

输出含:`status / failure_code / score / items(逐项得分) / session_id / workspace / trace_file`。

- `status: ok` + score → 可打分完成;
- `status: failed` → 看 failure_code:`launch_failed`(启动/执行异常,查 trace)、`version_changed`(评测期间定义被改,先恢复)、`grading_failed`(打分输出两次非法,可重跑)。

### 读诊断

```
jiuwenswarm-harness-evolve eval-matrix --agent <被测 agent> --benchmark <id> \
  [--runs N] [--parallel P] [--tag <标签>]
jiuwenswarm-harness-evolve report --benchmark <id> --agent <被测 agent> [--target <目标分>]
```

- **`--parallel P` 并发执行**:每个 cell 一个独立子进程(openjiuwen 的工具/
  SysOperation 注册表是进程级全局单例,同进程并发会互相污染,故子进程隔离),
  结果聚合与顺序模式完全一致。**默认 P=2(并发)**;`--parallel 1` 回到顺序。
  注意:并发不省 token,只是花得快,且可能撞 API 限流——大矩阵建议 P=2~4。
- `report` 把最近一次矩阵证据渲染为 Markdown:顶层分 vs 目标差距、每 case 得分、逐项低分、session/trace/workspace 证据表。**诊断时先看 trace 与工作区产物**,再定结论。

### 复查与归档

- 失败 run 的证据(workspace、trace)保留在 `<data-dir>/benchmarks/<id>/evaluations/` 与 run 工作区;
- 需要可复现的会话信息:session_id 贯穿报告与证据,直接引用即可。

## 简化对照(与 Penguin agent-evaluation 的差异)

| Penguin | 本实现 |
| --- | --- |
| run_subagent 纯 YAML worker 协议 | `eval-run --json`(prompt 约定 + JSON 后解析) |
| 输出非纯 YAML → 重发同证据修复 | 打分 JSON 契约校验失败 → 同一证据重试一次 → 仍败则 grading_failed |
| provider/model_id/thinking_level 显式 | 单 `--model`(缺省默认模型) |
| cost 原始精度或 null | 无货币成本 API → 恒 null(不阻塞闭环) |
