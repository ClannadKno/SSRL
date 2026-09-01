# -*- coding: utf-8 -*-
import os

content = """# 教师端实验控制页面问题分析报告

> 生成日期：2026-07-06  
> 范围：`routes/pages.py`（`teacher_session_control` 路由）、`routes/api.py`（task API）、`routes/teacher_api.py`（session API）、`services/teacher_session_service.py`、`static/teacher/session-control.js`、`templates/teacher/session_control.html`

---

## 问题 1：新建任务表单中包含不必要的选项

### 现状

在 `session_control.html` 的"任务管理"面板中，任务创建表单包含两个复选框：

- **`taskActive`**（L109）：`<input id="taskActive" type="checkbox" checked> 创建后设为启用`
- **`agentInterventionEnabled`**（L110）：`<input id="agentInterventionEnabled" type="checkbox" checked> 启用智能体干预`

### 问题

这两个字段对于任务本身而言不是核心属性：

1. **"创建后设为启用"（taskActive）** — 任务的新建/启用状态应该在任务列表中通过单独的"启用/停用"按钮来控制，而不是在创建时就决定。目前任务列表卡片已经提供了 `toggleTaskActive` 功能。
2. **"启用智能体干预"（agentInterventionEnabled）** — 智能体干预是课次级别（session）的配置（见问题 3），不应该附加在任务上。任务的 agent_intervention_enabled 字段与课次的策略智能体开关是重复概念。

### 影响范围

| 文件 | 位置 | 说明 |
|---|---|---|
| `templates/teacher/session_control.html` | L109-L110 | 两个 checkbox 的 HTML 定义 |
| `static/teacher/session-control.js` | `fillTaskForm()` / `fillTaskFormv2()` | 读取/写入这两个 checkbox 值 |
| `static/teacher/session-control.js` | `collectTaskPayload()` | 将 checkbox 值打包到任务 payload 中发送给 API |

### 建议

移除这两个 checkbox 及其相关 JavaScript 处理逻辑。

---

## 问题 2：页面布局不符合需求

### 现状

当前的布局结构：

- 顶部：当前状态栏（全宽）
- 第二行：创建新课次（全宽）
- 第三行：左侧课次列表 + 右侧任务管理（含当前课次号、任务表单等）
- 第四行：任务列表（全宽）

### 问题

"任务管理"面板混合了以下功能：
- 当前课次号的设置（`currentSessionNo` + `saveCurrentSession()`）
- 当前任务摘要（`currentTaskSummary`）
- 完整的新建/编辑任务表单

而"任务列表"作为一个独立区域被放在页面底部，与"课次列表"分开。

用户希望的结构：
- 第一行：当前状态栏（全宽）
- 第二行：新建课次 | 新建任务（左右并列）
- 第三行：课次列表 | 任务列表（左右并列）

即：
1. **"任务管理"面板替换为"新建任务"** — 只包含创建新任务的表单，不含"当前课次号"和"当前任务摘要"
2. **"任务列表"和"课次列表"并列** — 两个列表卡片左右排列
3. **"新建任务"和"新建课次"并列** — 两个创建卡片也左右排列

### 影响范围

| 文件 | 位置 | 说明 |
|---|---|---|
| `templates/teacher/session_control.html` | 全部 | 整体 HTML 结构需要重写 |
| `static/teacher/session-control.js` | 部分函数 | 需要调整目标元素 ID |

### 注意点

- 当前课次号的设置功能需要保留，建议移到"课次列表"区域
- 当前任务摘要可以在"课次列表"中展示，或者在展开详情中查看
- `session_control.html` 和 `task_admin.html` 之间存在冗余代码

---

## 问题 3：智能体启用配置的编辑位置不合理

### 现状

目前有两种途径可以设置智能体的启用状态：

**途径 A — 创建新课次时（已有）**  
在"创建新课次"卡片底部有两个复选框：
- `<input id="createStrategyAgentEnabled" type="checkbox"> 启用策略智能体`
- `<input id="createEmotionAgentEnabled" type="checkbox"> 启用情绪智能体`

对应的 `createSession()` 函数已经正确读取这两个值并传给后端。

**途径 B — 课次列表展开后内联编辑（不应存在）**  
在课次列表展开详情中，有"编辑"链接调用 `editAgentConfig()` 函数，在课次详情区内联显示两个 checkbox 并允许保存修改。

### 问题

用户要求智能体的启用状态 **只在创建新课次时选择**，创建后不应再允许修改。当前的 B 途径（课次列表中内联编辑）允许教师在课次创建后随意修改，这违反了实验控制的要求。

### 影响范围

| 文件 | 位置 | 说明 |
|---|---|---|
| `static/teacher/session-control.js` | `_buildExpandedContent()` | 课次详情中显示了"编辑"链接 |
| `static/teacher/session-control.js` | `editAgentConfig()` / `saveAgentConfig()` / `cancelAgentEdit()` | 三个内联编辑函数需要移除 |
| `routes/teacher_api.py` | `api_teacher_session_update_agent_config()` | 对应的后端 API 端点 |
| `services/teacher_session_service.py` | `update_session_agent_config()` | 对应的服务层函数 |

### 建议

1. **保留** 创建新课次时的智能体复选框（已正确实现）
2. **删除** 课次列表中的内联编辑功能（`editAgentConfig` / `saveAgentConfig` / `cancelAgentEdit`）
3. **可选** 删除后端 `update_session_agent_config` API 端点以防止通过 API 直接绕过
4. 课次详情中应仅显示当前的智能体配置状态（只读），不提供编辑入口

---

## 问题 4：课次删除功能无法正常工作

### 根因

`deleteSession()` 函数 **未定义**，导致点击删除按钮后没有任何反应。

### 详情

在 `session-control.js` 的 `_buildExpandedContent()` 函数中，为 draft 状态的课次生成了删除按钮，调用 `deleteSession(s.id)`。

但通过对所有 JS 文件的全文搜索，确认 **不存在名为 `deleteSession` 的函数定义**。

只找到一处引用：
- `static\\teacher\\session-control.js:136` → 仅引用（调用），无定义

而其他 session 管理函数都有完整实现：
| 函数 | 状态 | 说明 |
|---|---|---|
| `createSession()` | ✅ 已定义 | 创建课次 |
| `startSession(sessionId)` | ✅ 已定义 | 开始课次 |
| `endSession(sessionId)` | ✅ 已定义 | 结束课次 |
| `archiveSession(sessionId)` | ✅ 已定义 | 归档课次 |
| **`deleteSession(sessionId)`** | ❌ **缺失** | **删除课次 — 函数不存在** |

### 后端状况

后端已具备完整的删除能力：
- `DELETE /api/teacher/session/<session_id>` 路由已定义（`teacher_api.py`）
- `delete_session()` 服务层已实现（`teacher_session_service.py`）
- 仅允许删除 draft 状态的课次，写入 audit_log

问题纯粹在前端：按钮渲染出来了，但点击后调用的函数不存在，浏览器会报 `Uncaught ReferenceError: deleteSession is not defined`，导致没有任何反应。

### 建议

在 `session-control.js` 中补充 `deleteSession(sessionId)` 函数，参考 `startSession()` / `endSession()` 的实现风格。

---

## 总结

| 编号 | 问题 | 严重程度 | 修复方式 |
|---|---|---|---|
| 1 | 任务表单包含不必要的"创建后设为启用"和"启用智能体干预" | 低 | 删除对应 checkbox 和处理逻辑 |
| 2 | 页面布局不符合预期 | 高 | 重写 HTML 布局结构 |
| 3 | 智能体启用配置不应在课次列表中内联编辑 | 中 | 删除内联编辑功能 |
| 4 | 删除课次按钮无响应（`deleteSession` 函数缺失） | **严重** | 补充 `deleteSession()` 函数实现 |

## 涉及文件速览

| 文件 | 问题 1 | 问题 2 | 问题 3 | 问题 4 |
|---|---|---|---|---|
| `templates/teacher/session_control.html` | ✅ L109-L110 | ✅ 全部重排 | — | — |
| `static/teacher/session-control.js` | ✅ `fillTaskForm` / `collectTaskPayload` | ✅ `renderTaskCards` / `loadTaskAdmin` | ✅ `editAgentConfig` / `saveAgentConfig` / `cancelAgentEdit` | ✅ 补充 `deleteSession()` |
| `routes/teacher_api.py` | — | — | ✅ 可选删除 agent-config 端点 | — |
| `services/teacher_session_service.py` | — | — | ✅ 可选删除 | — |
"""

output_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'docs', '实验控制页面问题分析报告.md')
with open(output_path, 'w', encoding='utf-8') as f:
    f.write(content)
print('Report saved to: ' + output_path)
