---
name: harness-evolve-agent-optimization
description: 基于冻结 benchmark 的矩阵评估结果与诊断自动迭代被测 harness。Reference=scoreboard 末条;接受=每 cell 有效且顶层均分严格大于 Reference;编辑前必快照,被拒即回滚;被拒候选不入 scoreboard。
license: Apache-2.0
---

# Agent 优化

基于冻结 benchmark 的矩阵评估结果与诊断,自动迭代被测 agent(harness)的定义(prompt 正文 + frontmatter 白名单字段)。

## 固定输入(缺一不可,先向用户确认)

1. **Test Agent** — 被测 agent 名
2. **冻结 Benchmark** — 必须已 frozen 且 scoreboard 已有至少一条已接受基线
3. **目标分** — 达此均分即停止(0-100)
4. **runs** — 每 case 的 run 数(默认 1)
5. **轮数上限** — 最多迭代几轮(默认 3)

## 核心原则

1. **Reference = scoreboard 末条** — 其完整 Evaluation(含逐 case 得分)从 scoreboard 读回,用户不复述。**运行时一致性**:末条版本必须等于被测 agent 当前 sidecar 版本,不一致 → 立即停止(先 `agent-restore` 或重新评估)。
2. **接受判据(无统计检验)** — 候选必须 **每 cell 有效**(无 launch/version/grading 失败)且顶层均分**严格大于** Reference;等于或更低 → 拒绝。假设是否被证据支持与接受决定**分离记录**(每轮报告同时给出二者)。
3. **版本只增,被拒不复用** — 编辑后版本 +1;被拒候选的版本号被烧毁,回滚不回退 `next_version`。
4. **编辑前必有快照(后端强制)** — `agent-edit` 前 `snapshots/v<N>.tar.gz` 必须存在;快照存在即拒覆盖。**只编辑 prompt/description/model/tools/skills/max_iterations/when_to_use 白名单字段**,不改其它 agent 的定义。
5. **回滚 = 恢复 + 校验** — 被拒后 `agent-restore` 还原 md 字节并校验 sha256,sidecar 版本回退到 Reference。
6. **被拒候选不入 scoreboard** — 但证据(得分、session_id、trace、workspace)保留在 evaluations/ 归档,供诊断。
7. **停止条件** — 达目标分、轮数上限、或运行时不一致(矩阵作废)。

## 操作流程

> **CLI 入口**:所有命令走 `jiuwenswarm-harness-evolve <命令>`;若提示命令不存在
> (entry point 未安装),等价改用 `python -m jiuwenswarm.harness_evolve.cli <命令>`。
> 本技能与 jiuwenswarm 自带的 auto-harness run 任务调度是两套机制,勿混用。

### 推荐:一键复合

```
jiuwenswarm-harness-evolve optimize \
  --agent <被测 agent> \
  --benchmark <benchmark id> \
  --target <目标分> \
  [--rounds 3] [--runs 1] [--parallel P] [--model <模型名>]
```

内部每轮:快照(复用不覆盖)→ proposal LLM 生成候选 prompt → `agent-edit` → `eval-matrix` → 判定(严格大于 → `scoreboard-append`,否则 `agent-restore`)。每轮输出接受/回滚、得分对比、候选版本与变更点。**达 target 或轮数上限自动停**。每轮矩阵默认 `--parallel 2` 子进程并发(`--parallel 1` 回顺序;并发不省 token,可能撞 API 限流)。

### 或手工逐步(诊断更细)

1. **确认基线**
   ```
   jiuwenswarm-harness-evolve scoreboard-read --benchmark <id>
   jiuwenswarm-harness-evolve agent-version --name <被测 agent>
   ```
   末条版本必须 == 当前 agent 版本,否则先 `agent-restore --version <末条版本>`。

2. **诊断**:`eval-matrix` + `report`(见 agent-evaluation 技能),定位低分 case 与 trace 中的行为缺陷。

3. **快照 → 编辑**
   ```
   jiuwenswarm-harness-evolve snapshot-create --name <被测 agent> [--version <N>]
   jiuwenswarm-harness-evolve agent-edit --name <被测 agent> \
     --expected-version <N> --prompt-file <新 prompt 临时文件>
   ```

   编辑(与 restore)后 CLI 自动热重载 agent 服务,新定义立即对主 agent 生效,
   无需重启。

4. **复评与判定**
   ```
   jiuwenswarm-harness-evolve eval-matrix --agent <被测 agent> --benchmark <id> \
     [--runs N] [--parallel P]
   ```
   均分严格大于 Reference 且每 cell 有效 → `scoreboard-append --file <新证据>`;否则 `agent-restore --version <N>` 回滚,记录证据。

5. **收尾**:达目标分即停;向用户汇报每轮 Baseline + 候选(分数/run 数/版本/变更/决定/session_ids)。

6. **同步已导出的 harness 包(如有)**:迭代修改的是**定义**;若该 agent 之前
   导出过 harness 包,包是导出时刻的**快照**,不会自动更新——需重新执行
   `agent-export-package --name <被测 agent>` 同名覆盖导出(包 id 稳定),
   让叠加进主 agent 的 prompt 跟上新版本;未导出过则跳过。

## 禁止事项

- **禁止读 rubric/ 目录**或向被测 agent 复述打分标准(leak);
- **禁止修改已冻结的 benchmark**(frozen 后 `case-write` 已被后端拒绝);
- **禁止直接编辑 *.md 定义文件** — 一律走 `agent-edit`(否则 sha256 漂移拒绝);
- **禁止覆盖快照**、禁止手动改 scoreboard/state.json。

## 简化对照(与 Penguin agent-optimization 的差异)

| Penguin | 本实现 |
| --- | --- |
| Reference=当前最佳 State+完整 Evaluation | scoreboard 末条 + 运行时版本一致性校验 |
| admissible + 每 cell 有效 + 严格大于 | 同(无统计检验) |
| 快照含 vault 排除等细节 | 快照=md 字节 + manifest(原子 tar,禁覆盖) |
| 每轮报告基线+全部候选 | optimize 输出逐轮报告(接受/回滚/得分/版本/变更) |
| 运行时不一致→矩阵作废停止 | 同(ScoreboardError 停止) |
