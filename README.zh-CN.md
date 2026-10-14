# BridgeAgent

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)](pyproject.toml)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows%2011-0078D4.svg)](#局限)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#)
[![Author](https://img.shields.io/badge/author-Erikdai-181717.svg?logo=github)](https://github.com/Erikdai)

[English](README.md) | 中文

**让 Web Agent 能操作 Windows。**

BridgeAgent 是一个浏览器优先的 Windows AI agent。网页通过 DOM 读取，原生窗口通过 Windows UI Automation 读取，两者之间的切换由程序规则判定，不由模型判定。任务可以从网页开始，进入资源管理器、计算器、对话框或任何原生应用，再回到网页。

状态：alpha。Windows 11，Python 3.11 以上。用 DeepSeek（OpenAI 兼容接口）实测；Anthropic 后端已接入但未测试。

## 它做什么

- **两个世界一棵树。** 页面元素是 `b` 引用（Playwright），原生控件是 `d` 引用（UI Automation）。模型每步只看到一棵无障碍树，只选一个动作。
- **确定性路由。** `focus.py` 按前台窗口的进程和窗口类决定用哪个观察器。Chrome 画在页面之外的对话框（打印预览、权限气泡）通过 UIA 找到并合并进来。
- **焦点安全的执行。** 其他应用抢走前台时把 agent 的窗口拉回来；只有观察到的窗口仍在前台时才发按键；窗口消失时动作失败而不是敲进别的窗口。
- **不用 LLM 的技能。** 成功一次的运行压缩成技能，下次先回放，只花一次模型调用做验证。界面变了，模型从断掉的那步接管，技能被改写。从页面复制的值会变成变量，数据变了回放照样对。
- **模型绕不过去的护栏。** 权限清单（可用的应用、目录、窗口、凭证站点）、风险动作的原生确认框、从系统钥匙串代填的密码、审计日志。
- **三个入口一个核心。** 命令行、一个小桌面窗口、一个 MCP server，其他 agent 可以直接用同一套执行层。

## 架构

```
                 agent.py: observe -> route -> decide -> act, skills, self-heal
                                   |
      focus.py ----------- llm/ ------------- actions.py ----------- guard.py
   who has focus?    Anthropic / OpenAI     one action schema,    permissions,
                      compatible           two executors        confirmation, audit
         |                                        |
 observers/browser.py  observers/desktop.py   BrowserExecutor  DesktopExecutor
 Playwright DOM scan   Windows UIA walk       (Playwright)     (uiautomation)
         \______________ tree.py: UINode / Observation ______________/

 entry points: cli.py        gui.py (PySide6)        mcp_server.py
 skills/: store, replay, params      credentials/: keyring broker      eval.py: task runner
```

路由规则：前台窗口属于 agent 自己的浏览器且是浏览器主窗口类，读 DOM；其余情况（原生窗口、浏览器自己弹的对话框、别的浏览器、Chrome 弹出窗口）走 UIA。执行按引用前缀分发。模型从不选择观察器或执行器。

## 快速开始

```powershell
git clone https://github.com/Erikdai/bridge-agent.git
cd bridge-agent
python -m venv .venv
.\.venv\Scripts\pip install -e ".[gui,dev]"
.\.venv\Scripts\playwright install chromium
copy .env.example .env        # 填 API key
```

DeepSeek 的 `.env`（任何 OpenAI 兼容接口写法相同）：

```
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_MODEL=deepseek-chat
BRIDGE_PROVIDER=openai
```

```powershell
bridgeagent run "读出页面上的两个数字，用 Windows 计算器相加，报告结果并关掉计算器" --start-url file:///<仓库路径>/tasks/pages/numbers.html
bridgeagent run "..."            # 第二次运行回放学到的技能，只做一次验证调用
bridgeagent gui                  # 任务输入、进度、审批、停止、结果
bridgeagent mcp                  # 作为 MCP server 提供执行层
```

其他命令：`focus`、`dump`、`scan URL`（不用 LLM 看树）、`cred set 站点`（密码进系统钥匙串）、`skill list|show|delete|replay`、`perms`、`eval`（跑 `tasks/examples.yaml`）、`notify`。

安全：把 `permissions.example.yaml` 复制为 `permissions.yaml`，限制 agent 可以碰的应用、目录、窗口和凭证站点。每个动作都写入 `runs/audit.jsonl`。

## 证据

- `docs/EVAL.md`：8 个跨边界任务三轮评测、一个真实的资源管理器到网页上传工作流、十场景确定性矩阵（`scripts/bridge_matrix.py`）。样本小，不是可靠性统计。
- `docs/RELATED_WORK.md`：与 UFO2、Agent S2、browser-use 和 RPA 工具的比较。

## 局限

- 仅 Windows。Excel 网格对 UIA 只暴露可见表头，不支持读单元格。
- Chrome 对话框识别依赖 Chrome 的视图类名，Chrome 升级后可能要更新。
- 技能匹配是关键词重叠；参数化只覆盖输入文本和下载目录，不覆盖“最新的文件”这类选择。
- 稀疏树的截图兜底需要能看图的模型。

## 许可证

MIT，见 `LICENSE`。
