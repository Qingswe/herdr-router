# Router 演进方案

讨论日期：2026-10-11。用户选择「与 Opus 讨论后实现第一阶段」。

## 第一阶段：可靠派发

先补齐执行边界，再添加账户与会话功能。当前 `dispatch` 在发送任务失败时丢失 pane/agent 和日志，`--json` 的 stdout 混有进度文字。一次超时可能发生在任务已送达之后，不能据此重新派发。

本阶段验收：

- 默认派发在 Herdr 观察到 `working` 或 `blocked` 后才报告送达确认，不等待整个任务结束。stalled 时最多再观察一次 working/blocked，绝不重发。`--wait` 在确认之后单独等 settled；等待结束失败仍保留 confirmed。
- 每次尝试有 `dispatch_id`，终态日志保留旧的 `event=dispatch`、`decision`、`cwd`、`agent`、`pane_id`、`error` 字段，并补充 `stage`、`status`、`prompt_status`。
- `prompt_status` 为 `not_sent`、`unknown`、`confirmed`。前置失败是未发送；发送阶段超时、未恢复的 stalled 或送达无法确认的通信错误是未知；成功确认后是已送达。仅明确发生在写入前的拒绝或进程无法启动可报告未发送。
- 所有预期失败统一为 `HerdrError`，尽可能返回 pane、agent、dispatch_id、stage 与恢复提示。保留已创建 pane，不自动关闭或重发。
- 准备与命名尽量在创建 pane 前完成。CLI 调用用 argv，不用 shell，任何 task 正文都只是一个参数。
- `dispatch --json` 成功 stdout 只有一个 JSON 对象；失败 stdout 为空，stderr 为结构化错误。非 JSON 模式给出具体恢复提示。
- popup 不把送达未知描述成「任务没有发出」，失败退出码为非零。
- 不改变评分策略、默认 policy、网页可派发权限或用户已有 CLI 布局修改。

测试使用 mock、临时配置与状态目录，覆盖成功确认、等待 settled、启动失败、prompt 超时与拒绝、准备/命名/split 失败、CLI JSON、RPC 解析与通信错误。真实验证只使用这轮创建的协作 agent，不操作用户其他 pane。

## 后续阶段边界

### 额度资格层

放在评分前，返回资格和排除原因，不改变本地罚分公式。账户与 profile 分开建模，profile 关联 account，account 定义 ownership 与 quota pool。

`UsageSnapshot` 最小字段：account_id、source、observed_at、certainty、windows（pool、remaining_ratio、reset_at）。单位统一为 0..1，保留各时间窗口，资格判断使用适用窗口的最紧约束。

采集方式待下一阶段比较：Opus 倾向先做手动声明「耗尽到某时恢复」，主代理认为可读取本地 statusline/cache，但自动数据必须展示来源与新鲜度。固定每任务美元 cost 不能推导真实订阅消耗比例。个人账户缺失数据可允许路由，但明确标记；是否严格避开个人 0% 应是可配置政策。共享账户采用严格 fresh/known 检查和 reserve floor。没有可靠任务消耗估算前，不照搬 upstream 的估算比例预留。

图形 territory 仍展示静态策略切片；额度随账户与时间变化，应作为可选资格叠加层，不能把几何覆盖率误称为真实使用比例。

### Session 与 worktree

先加入 `Session`：id、parent_id、phase、task/handoff、route、cwd、dispatch_id、pane/agent、status、workspace。存储待下一阶段比较：Opus 倾向先用原子写 JSON 文件，主代理倾向在需要并发更新与历史查询时用标准库 SQLite。决策日志继续作为调试历史。明确区分发送、活动确认与任务完成。

交接依赖文件及显式摘要，不假设读取了前一 agent 的全部对话。worktree 创建前检查仓库和工作区状态，路由后再次验证 HEAD 与状态，生成不含任务正文的分支名与路径，后续 session 复用并校验同一 worktree。失败保留目录，不自动 merge/push/delete。

live effort、跨机器协调与网页派发暂不纳入这些最小阶段。后续若实现 live effort，必须单独设计身份验证、输入框保护和重复发送边界。

## 技术栈决策

本阶段保留 Python 标准库后端与原生 JS/SVG 前端。

| 方案 | 收益 | 成本与适用条件 |
| --- | --- | --- |
| Python 后端增加明确类型与边界校验 | 保留直接运行和标准库优势；Session/Usage 可用 dataclass、TypedDict、Literal 表达 | 不自动修复运行时协议，需要测试和验证外部 JSON |
| 前端 JSDoc + TypeScript checkJs | 先检查 policy、API、编辑器状态等契约，不改变浏览器运行文件 | 仍需开发期类型检查工具；应先拆状态/API/图形模块，再逐步启用，避免全文件 any 化 |
| 仅前端迁移 TypeScript | UI 扩大后能更清晰表达状态与 API 契约 | 引入 JS 生成与发布流程；后端和前端仍需共享 schema 或契约检查 |
| 全量 TypeScript/Node | 一种语言、共享 DTO 类型、Node SDK 直接集成 | 重写评分、配置、HTTP 安全、CLI/RPC、测试和分发；静态类型不能替代运行时 JSON 验证 |

目前已有问题集中在派发状态与协议边界，不是 Python 特有缺陷。全量迁移没有已测得的性能或可靠性收益。重新评估的触发条件是：必须使用 Node-only SDK；UI/业务状态复杂到前后端契约重复成为持续问题；团队明确以 Node 分发为主；或实测证明 Python 的并发/性能成为瓶颈。

评分迁移还有具体兼容风险：Python `round(0.03125, 4)` 为 `0.0312`，普通 JavaScript `Math.round(0.03125 * 10000) / 10000` 为 `0.0313`。排序使用已舍入的罚分，迁移必须保留舍入与 tie-break 行为，并用同一套 fixtures 比较完整排名。TypeScript 不要求使用某个 SQLite 原生包，依赖选型要单独评估，不能把 upstream 的依赖当成语言本身的要求。

TypeScript 渐进检查参考：[checkJs](https://www.typescriptlang.org/tsconfig/checkJs.html)、[JSDoc 支持类型](https://www.typescriptlang.org/docs/handbook/jsdoc-supported-types.html)。

## 协作与验证记录

Opus 通过当前 router 的 `opus-xhigh` profile 参与只读架构讨论，实际启动为 Opus 5.5/xhigh。主代理负责 CLI 与文档集成，实现 worker 通过 router 分配，Opus 在实现后复查关键边界。

实现 worker 由 Jev 自动选择 `sol-xhigh`（Codex / GPT-6.1 Sol / xhigh），仅修改派发与 Herdr 包装层、补充离线测试；主代理集成 CLI、启动配置校验、README 与附带 skill，保留原有 CLI 窄屏布局修改。

已完成：独立 dispatch id；默认活动确认；stalled 一次观察；独立结束等待；失败/中断终态日志；本地 RPC 超时与解析保护；未知送达恢复提示；纯 JSON stdout；启动方向/超时校验。对未发送的判定只信任明确的 Herdr 预写入拒绝和本地进程无法启动的证据，不把未知远端错误码当成未发送。

验证：Python 全套 107 个测试通过，前端 26 个测试通过。真实 Herdr 冒烟使用 Luna low，任务以 `-` 开头、包含多行，禁止工具和文件操作；实际返回 `router-smoke-ok`。CLI stdout 为单个可解析 JSON，送达状态为 `confirmed`，对应 dispatch id 恰有一条终态日志。冒烟没有改变项目文件，临时测试 pane 已关闭。

Opus 最终复查未发现约定范围内的阻断问题，并验证了真实只读 RPC 的返回结构和当前用户 policy。主代理进一步处理其两个边界提醒：`prompt --wait` 的 `agent_not_found` 可能发生在提交后的确认阶段，统一保守记 unknown；CLI UTF-8 解码采用 replace，并以真实本地子进程的无效 UTF-8 输出测试验证不会跳过结构化异常路径。

已知边界：Herdr 的活动观察不跟踪特定轮次。stalled 后若 agent 已快速完成，后续观察可能保守地记为 unknown；必须查看 pane 后决定恢复。`--wait` 超时仍使用兼容的 exit 1，送达事实通过 `prompt_status=confirmed` 表达。Ctrl-C/SystemExit 会写 best-effort 日志，但进程被强杀、机器掉电或日志存储失败无法保证终态记录；这不是持久化 Session 的实现。
