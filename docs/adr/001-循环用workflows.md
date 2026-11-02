# ADR-001 用 LlamaIndex Workflows 显式三步循环，而不是 FunctionAgent

## 背景

浏览器 Agent 每一轮要做四件事：读取页面状态 → 模型选动作 → 执行动作 → 记录轨迹。企业级要求每步可追溯、可暂停、可恢复。

## 候选方案

1. **FunctionAgent（框架内循环）**：把浏览器动作包成工具，循环由框架运行
2. **Workflows 显式三步**：自己定义 observe / decide / act 三个 step，事件驱动
3. **自研 while 循环**：不引编排框架

## 决定

选 2：LlamaIndex Workflows 显式循环。

## 理由

- 每步都是自己的代码：trace 粒度、上下文裁剪、暂停/恢复、审批插入点全部可控
- 学习目标：真正用一次 LlamaIndex 的 Workflow（事件、状态、Context）
- 与 P3 的审计落表、P5 的审批停等在结构上天然兼容

## 后果

- 需要自己管循环退出条件（max_steps、重复动作检测、超时）
- LlamaIndex Workflows 的社区示例比 LangGraph 少，踩坑要靠自己记录
- 如果 P5 实测发现 Workflows 表达不了审批停等，局部回退：在 workflow 内手动暂停，或换 LangGraph——届时另开一条 ADR
