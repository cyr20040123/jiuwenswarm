---
name: harness-evolve-agent-creation
description: 从需求创建可被评测与迭代的专用 agent(harness)。需求不清先追问;新建禁止覆盖已存在 agent;定义一律写入 AgentConfigService,绝不手改 *.md。
license: Apache-2.0
---

# Agent Harness 创建

把用户需求转化为一个**专用 agent 定义**(简称 harness),供后续 benchmark 评测与迭代闭环使用。

## 核心原则

1. **需求不明先追问** — 用户只给一句"建一个 agent"时,必须追问:目标能力、输入输出形态、使用场景、约束(工具/技能/模型偏好)。缺需求不开始。
2. **新建禁止覆盖** — 目标名已存在(见 `agent-list`)时拒绝新建,提示换名或修改已存在 agent。名字必须匹配 `[A-Za-z0-9_-]{3,50}`。
3. **定义即全部** — harness = 一个自定义 agent 定义:prompt 正文(即 AGENTS.md 等价物)+ frontmatter(description/model/tools/skills/max_iterations/when_to_use)。**禁止直接编辑 *.md 文件**——创建走 `agent-register`,后续迭代走 `agent-edit`。
4. **模型简化** — 不指定 provider/model_id 组合,只给模型名(缺省用 jiuwenswarm 默认模型)。
5. **技能按名挂载** — 需要技能时给 frontmatter `skills` 名字列表(系统从技能目录安装,不手写)。

## 操作流程

> **CLI 入口**:所有命令走 `jiuwenswarm-harness-evolve <命令>`;若提示命令不存在
> (entry point 未安装),等价改用 `python -m jiuwenswarm.harness_evolve.cli <命令>`。
> 本技能与 jiuwenswarm 自带的 auto-harness run 任务调度是两套机制,勿混用。

### 1. 澄清需求

向用户确认:①目标能力 ②输入→输出 ③可用工具 ④需要哪些已有技能 ⑤模型偏好。全部明确后才继续。

### 2. 检查重名

```
jiuwenswarm-harness-evolve agent-list
```

已有同名 agent → 停下,与用户协商换名或改需求,不覆盖。

### 3. 写 prompt 正文到临时文件

把 prompt 正文(角色、职责、工作方式、输出约定)写入**工作区临时文件**(如 `<workspace>/harness-prompts/<name>.md`)。prompt 应包含足够的行为约束,以便后续评测可区分好坏。

### 4. 注册

```
jiuwenswarm-harness-evolve agent-register \
  --name <name> \
  --description "<一句话描述>" \
  --prompt-file <workspace>/harness-prompts/<name>.md \
  [--model <model名>] \
  [--tools "Tool1,Tool2"] \
  [--skills "skill-a,skill-b"] \
  [--max-iterations 15] \
  [--when-to-use "<何时使用的说明>"]
```

成功输出 `version=1`(sidecar 状态已初始化),并自动写入用户 config.yaml
`react.subagents.<name>.enabled: true`——这是主 agent 能调度该 harness 的
前提(jiuwenswarm 的 `_load_custom_subagents` 只加载显式启用的自定义 agent)。
同时 CLI 会**自动热重载运行中的 agent 服务**(无需重启即可调度;服务未运行
时会提示"重启 agent 服务后生效")。
若输出提示"config.yaml 不存在",说明尚未初始化用户配置,需手动在
`~/.jiuwenswarm/config/config.yaml` 的 `react.subagents` 下添加
`<name>: {enabled: true}`。失败(重名/非法名/内置保护)→ 按错误信息修正,不绕过。

### 5. 验收

```
jiuwenswarm-harness-evolve agent-inspect --name <name>
```

确认 prompt 正文、工具、技能、版本号都符合预期。告诉用户 agent 已就绪,下一步建议 `harness-evolve-benchmark-design` 构建 benchmark。

### 6. 可选:导出为 Harness 包注入主 agent

> **两种消费形态说明**:① **subagent 调度**(默认,已打通)——主 agent 对话中派
> 出独立子 agent 执行任务,定义持久、**创建/更新后自动热重载生效**(无需重启);
> **这也是评测闭环评估的对象**
> (评测 = 把定义当独立 agent 完整跑 case);② **harness 包**(本步骤)——把 prompt
> 叠加进主 agent 自身(临时,重启回 Native),是导出时刻的**快照**,不参与评测
> 闭环;迭代(agent-edit)后需重新导出同步。

```
jiuwenswarm-harness-evolve agent-export-package --name <name> [--activate]
```

打包后主 agent 的 system prompt 直接叠加该 harness 的 prompt(prompt_sections
语义是叠加不是替换);技能拷入包内、工具**不打包**(主 agent 自带,声明会导致
热加载绑定冲突)、model 仅记 metadata。**生效只有一种方式**:到 web 端
Harness Package 管理面板激活(热加载);`--activate` 只是预标记,**重启 agent
服务会清空激活回到 Native,需重新激活**。同名覆盖重导出(id 稳定),导出前
自动用官方 loader 自校验、失败保留旧包。用户没要求打包时不做这一步。
跨机器迁移(导出 bundle 复用/导入/删除)走 `harness-evolve-agent-management`
技能。

## 简化对照(与 Penguin agent-creation 的差异)

| Penguin | 本实现 |
| --- | --- |
| 写 agent_state/{system_config.yaml, AGENTS.md, skills/} | `agent-register` 写 AgentConfigService 定义 + sidecar state |
| provider/model_id/thinking_level 显式三元组 | 单 `--model`(缺省默认模型) |
| skills 目录物理安装 | frontmatter `skills` 名字列表,评测时自动安装 |
| 新建禁覆盖 id 字符集 | AgentConfigService 同名保护 + 名字正则 |
