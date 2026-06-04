---
name: subagent-module-workflow
description: 当 Codex 需要按多模块实施计划推进功能开发，并且每个模块都要经过全新的编码、验证、评审三个 subagent 闸门时使用；适用于需要文件化交接、反复修复循环、真实流程验证、API/数据库/对象存储/前端页面/Playwright 验收的开发任务。
---

# Subagent 模块化开发流程

## 总览

使用本 skill 执行按模块拆分的实现计划。每个模块都必须经过三个彼此独立的角色：

1. 编码 subagent：实现当前模块，并写入 `implementation.md`。
2. 验证 subagent：验证刚完成的实现，并写入 `verification.md`。
3. 评审 subagent：阅读实现、验证证据和 diff，独立评审并写入 `review.md`。

主 agent 只根据文件化交接结果做决策：如果评审通过，进入下一个模块；如果仍有阻塞问题，重新开启同一模块的编码、验证、评审循环。不要让 subagent 依赖聊天历史互相传话，所有交接都必须落到文件里。

## 准备工作

开始实现前：

- 尽量使用隔离 worktree；如果只能在当前工作区执行，要在交接文件中明确记录。
- 将用户确认过的计划拆成带编号的模块。
- 每个模块创建独立运行目录：

```text
docs/subagent-runs/<feature-name>/module-N/
```

每个模块目录必须包含这些交接文件：

```text
implementation.md
verification.md
review.md
```

如果当前环境没有可用的 subagent 工具，先用 `tool_search` 搜索 `subagent`、`multi-agent`、`task agent` 等关键词。若仍没有 subagent 能力，不要假装已经启动 subagent；在主 agent 中按同样角色顺序执行，并在交接文件里明确记录这个限制。

## 主 Agent 状态机

每个模块按下面的循环执行：

```text
编码 -> 验证 -> 评审 -> 决策
```

决策规则：

- 如果 `review.md` 写明 `Decision: PROCEED`，标记当前模块完成，销毁或丢弃本轮 subagent 上下文，进入下一个模块。
- 如果 `review.md` 写明 `Decision: FIX_REQUIRED`，为同一模块启动新的编码 subagent，并把评审发现作为必须修复项输入。
- 如果 `review.md` 写明 `Decision: NEEDS_CONTEXT`，只补充缺失上下文，然后重新运行被阻塞的角色。
- 如果同一角色因为同一原因连续三次 `BLOCKED`，且主 agent 无法自行解决，停止并向用户说明阻塞点。

当前模块存在 Critical 或 Important 级别评审问题时，绝不能进入下一个模块。

## 所有 Subagent 的共同规则

每个 subagent 都必须：

- 只处理当前模块。
- 只修改与当前模块直接相关的逻辑，不能为了满足当前模块的功能随意修改其它模块的现有逻辑，若有必要调整已有逻辑，需要请求用户许可。
- 只阅读传入的模块规格和必要交接文件。
- 不依赖之前 subagent 的记忆。
- 所有交接文件使用中文，除非仓库制品本身要求英文。
- 记录实际运行过的命令、退出码和简洁证据。
- 只使用以下状态：
  - `DONE`
  - `DONE_WITH_CONCERNS`
  - `NEEDS_CONTEXT`
  - `BLOCKED`
- 没有新的验证证据时，不能宣称成功。
- 不能编造工具输出、截图、日志、测试结果或浏览器观察。

## 编码 Subagent

### 职责

只实现当前模块。在可行时，先补测试或验证用例，再写生产代码。

### 输入内容

主 agent 必须向编码 subagent 提供：

- 当前模块编号和标题。
- 当前模块的需求。
- 允许修改的文件或子系统边界。
- 要产出的接口、类型、响应结构或迁移要求。
- 如果处于修复循环，提供上一轮 review 的必须修复项。
- `implementation.md` 的写入路径。

### 硬性规则

编码 subagent 必须：

- 只修改当前模块所需文件。
- 避免无关重构。
- 避免对其它模块的现有逻辑进行改动式调整。
- 让公开接口与模块规格保持一致。
- 为新增行为补充测试。
- 记录所有改动文件。
- 记录实际运行过的命令。

编码 subagent 不得：

- 实现后续模块。
- 没有运行相关命令却标记验证通过。
- 在模块要求真实验证时，用 mock 代替真实流程。
- 在模块规格会影响 API 或数据模型但仍不明确时继续硬写。

### `implementation.md` 模板

```markdown
# 模块 N 实现记录

Status: DONE | DONE_WITH_CONCERNS | NEEDS_CONTEXT | BLOCKED

## 实现范围
说明本模块完成了什么。

## 改动文件
- `path/to/file`: 改动原因

## 公开接口
列出新增或变更的 API、函数、类型、schema、路由或响应结构。

## 新增测试
- `path/to/test`: 覆盖的场景

## 运行命令
- `command`
  - Exit code:
  - Evidence:

## 遗留顾虑
写 `None`，或描述具体顾虑。
```

## 验证 Subagent

### 职责

验证编码 subagent 的改动确实可用。优先真实执行，而不是只看代码。对于明确要求真实流程验证的模块，禁止使用 mock 作为验收依据。

### 输入内容

主 agent 必须向验证 subagent 提供：

- 当前模块需求。
- `implementation.md`。
- 当前 git diff 或改动文件列表。
- 必须运行的验证命令。
- `verification.md` 的写入路径。

### 验证强度

按模块要求选择最强验证方式：

- 早期数据模型、字段持久化、接口契约模块，可以使用单元测试或 API 测试。
- 从涉及真实文件内容、原文预览、前端行为或浏览器流程的模块开始，按规格使用真实服务和 Playwright。
- 如果真实服务不可用，报告 `BLOCKED` 或 `PASSED_WITH_WARNINGS`；不要悄悄降级成 mock。

### 真实流程要求

当模块要求真实验证时，必须尽量使用：

- 真实 API 请求。
- 当前环境配置的真实数据库和对象存储。
- 实际前端页面。
- Playwright 或浏览器自动化验证路由、点击、滚动、高亮和 Network 请求。
- 可复核证据，例如请求 URL、响应状态、截图路径、trace 路径、DOM 断言或数据库记录。

### `verification.md` 模板

```markdown
# 模块 N 验证记录

Status: PASSED | PASSED_WITH_WARNINGS | FAILED | BLOCKED

## 输入
- implementation: `path/to/implementation.md`
- diff 范围或改动文件:

## 运行命令
- `command`
  - Exit code:
  - Evidence:

## 真实流程检查
仅在模块禁止 mock 时必须填写。
- API 请求:
- 数据库/对象存储参与:
- 浏览器/Playwright 检查:
- Network 观察:
- 截图/trace 证据:

## 结果
- 通过:
- 失败:

## 建议
PROCEED | FIX_REQUIRED | NEEDS_CONTEXT
```

## 评审 Subagent

### 职责

独立评审实现和验证证据。评审 subagent 不改代码，只写结论和必须修复项。

### 输入内容

主 agent 必须向评审 subagent 提供：

- 当前模块需求。
- `implementation.md`。
- `verification.md`。
- 当前 git diff。
- `review.md` 的写入路径。

### 评审优先级

按以下顺序评审：

1. 规格符合度：当前模块是否只做了要求的事，并且完整满足要求。
2. 验证可信度：验证证据是否足以证明功能可用。
3. 安全与权限：鉴权、身份链路、数据隔离、破坏性操作是否安全。
4. 兼容性：迁移、现有 API、前端路由、测试和用户流程是否被破坏。
5. 代码质量：可维护性、重复、错误处理、是否符合本仓库模式。

评审文档切块、原文预览、chunk 定位等流程时，以下问题必须视为阻塞：

- 丢失或绕过 `workspace_id + file_id` 身份链路。
- 通过文件名定位文档。
- 返回了其他 workspace 或其他 file 的 chunk。
- 模块要求真实验证时却使用 mock 作为通过依据。
- 没有浏览器或 API 证据却声称预览、高亮、滚动定位可用。

### 严重级别

- `Critical`：数据泄漏、权限绕过、迁移破坏、核心流程运行时崩溃。
- `Important`：缺少必需行为、验证不可信、路由或 API 契约损坏、有意义的回归。
- `Minor`：不阻塞模块的清理或打磨建议。

### `review.md` 模板

```markdown
# 模块 N 评审记录

Status: APPROVED | APPROVED_WITH_NOTES | CHANGES_REQUESTED | BLOCKED

## 摘要
一句话说明结论。

## 发现
- Severity: Critical | Important | Minor
  - File/Area:
  - Issue:
  - Required Fix:

## 验证评估
说明验证证据是否充分，以及原因。

## 规格符合度
- 已满足:
- 缺失:
- 越界:

## 决策
PROCEED | FIX_REQUIRED | NEEDS_CONTEXT

## 备注
可选的非阻塞说明。
```

## 单模块完成标准

只有同时满足以下条件，当前模块才算完成：

- `implementation.md` 存在，且状态不是阻塞状态。
- `verification.md` 存在，且建议为 `PROCEED`。
- `review.md` 存在，且写明 `Decision: PROCEED` 或 `决策: PROCEED`。
- 主 agent 已读取三个文件，并确认没有剩余阻塞问题。

## 清理并进入下一模块

模块通过后：

- 保留交接文件，作为持久进度记录。
- 不复用上一轮 subagent 的上下文。
- 为下一个模块重新启动编码、验证、评审 subagent。
- 输入给新模块的内容只包含该模块需求，以及前序模块已经批准的接口决策。
