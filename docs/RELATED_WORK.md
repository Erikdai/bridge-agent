# 相关工作（整理于 2026-10-15）

按 BridgeAgent 的几个技术点分组。每条给出它做了什么、和本项目的关系。arXiv 编号可直接搜索。

## 1. 桌面 agent 与 Windows UI Automation

| 工作 | 要点 | 和本项目的关系 |
|---|---|---|
| UFO (Microsoft, arXiv 2402.07939) | HostAgent + AppAgent 双层结构，用 UIA 枚举控件，配合 GPT-4V 截图 | 最早把 UIA 当 LLM 观察源的工作；只覆盖桌面，不读浏览器 DOM |
| UFO2: The Desktop AgentOS (arXiv 2504.14603) | 混合控件检测：UIA 融合视觉解析；按应用挂原生 API；推测式多动作减少 LLM 调用；画中画虚拟桌面隔离 | 最接近的系统级工作。它把 Chrome 当普通应用，本项目在浏览器内改读 DOM，并显式按焦点路由 |
| Windows Agent Arena (arXiv 2409.08264) | 154 个 Windows 任务，Navi agent 用 UIA 加 OmniParser 做 Set-of-Mark，云端并行评测 | 评测口径参考；本项目评测集更小但全部跨边界 |
| Agent S2 (Simular, arXiv 2504.00906) | Mixture-of-Grounding 定位，主动分层规划；OSWorld 和 WAA 上领先 | 代表“靠更强视觉定位”的路线，是本项目“靠结构化树”路线的对照组 |

## 2. 无障碍树 vs 截图：证据

| 工作 | 发现 | 对本项目的启示 |
|---|---|---|
| OSWorld (arXiv 2404.07972) | 比较纯截图、纯树、截图加树、SoM 四种输入；GPT-4V 最好约 12%，人类 72% | 树加截图通常最好，但取决于模型 |
| OS-Harm (arXiv 2506.14866) | 用新模型重跑：Gemini 2.5 系列树加截图最好 | 同上 |
| OSWorld-Human (arXiv 2506.16042) | 树动辄几千节点，直接喂会增加步数；SoM 叠在树上能减少步数 | 树必须裁剪，这正是 `tree.py` 做的事 |
| EntWorld (arXiv 2601.17722) | 老旧企业软件无障碍元数据不全，纯树表现差 | “树稀疏就退回截图”的兜底有依据 |
| FocusAgent (arXiv 2510.03204) | 检索任务相关的 AxTree 行，平均裁掉一半以上，上限 2k token | 下一步可以把节点筛选从规则改成检索 |
| Agent-E (arXiv 2407.13032) | DOM 蒸馏，原始 DOM 可达 80 万 token；WebVoyager 73.2% | 浏览器侧扫描的思路来源 |
| Efficient GUI Agents 系统综述 (arXiv 2609.02309) | 从观察、记忆、动作、运行时四个维度总结效率优化 | 项目四个模块的划分可以对齐这个综述的框架 |

## 3. 跨应用任务为什么难

| 工作 | 要点 | 关系 |
|---|---|---|
| WindowsWorld (arXiv 2604.27776, ACL 2026 Findings) | 181 个任务、17 个 Windows 应用，78% 天然跨应用；所有 agent 在多应用任务上成功率低于 21%，三个应用以上几乎全败；按中间检查点给分 | 本项目动机的直接证据。它的检查点计分方式值得借鉴到 `tasks/` |
| OSWorld、Windows Agent Arena | 被 WindowsWorld 指出以单应用、短任务为主 | 说明“网页到桌面”这类边界任务在主流 benchmark 里覆盖不足 |

## 4. 技能库与真人示范

| 工作 | 要点 | 关系 |
|---|---|---|
| Agent Workflow Memory (CMU, arXiv 2409.07429, ICML 2025) | 从轨迹归纳可复用工作流写进记忆 | `skills/store.py` 蒸馏的理论来源 |
| SkillWeaver (OSU, arXiv 2504.07079) | agent 自己探索网站、练习并沉淀成 API 式技能；弱模型用强模型的技能提升最多 54% | 技能跨模型复用这个点可以写进评测 |
| ShowUI-Aloha (arXiv 2601.07181) | 记录用户日常键鼠和屏幕，解析成教学轨迹 | 与本项目技能回放同一思路，但它用录制数据训练模型，本项目直接回放脚本 |
| LearnAct (arXiv 2504.13805) | 人类示范数据集加三 agent 框架，手机端 | 示范如何被解析和检索 |
| Instruction Agent (Microsoft, arXiv 2509.07098) | 单次示范抽成步骤说明再执行 | 回放失败交回 LLM 时可借鉴它的“按意图而非坐标”表述 |

## 5. 安全：凭证与注入

| 工作 | 要点 | 关系 |
|---|---|---|
| CaMeL: Defeating Prompt Injections by Design (Google DeepMind, arXiv 2503.18813) | 能力机制：特权 LLM 做计划，隔离 LLM 读不可信数据且无工具权限；AgentDojo 上 77% 任务可证明安全 | `fill_credential` 是同一思想的最小版本：秘密只在执行器一侧 |
| WASP (Meta, NeurIPS 2025) | Web agent 注入攻击 benchmark | 后续安全评测可直接用 |
| BrowseSafe (Perplexity, arXiv 2511.20597) | 本地检测器加策略层 | 同上 |
| Software Engineering for and with GUI Agents (arXiv 2608.09278) | 主张学习模型负责感知推理，显式运行时模块掌握验证、权限、日志、恢复 | 为 `runtime/` 和确认机制提供设计依据 |

## 6. 工业界工具

- Playwright MCP、browser-use：浏览器侧无障碍快照加编号引用，是本项目浏览器观察器的工程参照，但都止步于浏览器。
- Playwright 1.56 自带 Healer、Healenium：只修 selector，不接管任务。

## 定位一句话

现有工作要么只在浏览器里用 DOM（browser-use、Agent-E），要么把整个桌面当像素或 UIA 树统一处理（UFO2、Agent S2）。WindowsWorld 证明跨应用是当前 agent 的最大短板。BridgeAgent 的差异点是**按键盘焦点在 DOM 观察器和 OS 无障碍树之间显式路由**，两边共用同一节点格式、动作格式和技能格式，并用一组可本地复现的跨边界任务报告“树 vs 截图”和“技能回放”的成本与成功率。它是工程加小规模评测，不是新模型。
