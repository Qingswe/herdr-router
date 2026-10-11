# herdr-router 开发与调试手册

给在这个仓库里协作的 agent（Claude Code / Sonnet 等）看的。用户向的功能说明见 `README.md`，本文只讲**代码怎么组织、数据怎么流、出问题去哪查、改动时别踩哪些坑**。

## 一句话

把一段任务文本按 N 个维度（难度、时长、范围、任务类型…）打出概率分布，再给每个「档位」（agent + model + effort + 在各维度上的一个坐标点）算罚分，罚分最低的档位胜出，然后用 Herdr 在旁边开 pane 启动这个 agent 并把任务发过去。

- Python ≥ 3.11，**只用标准库**（不要引入第三方依赖；`certifi` 只是可选兜底）。
- 前端是原生 JS + SVG，无构建、无 CDN。
- 维度和档位全是数据（`config/policy.default.json`），**加维度 / 调坐标不应该需要改代码**。如果某个需求逼你去改 `score.py` 来"特判某个维度"，先停下来想能不能用数据表达。

## 安装位置（本机）

| 东西 | 位置 |
|---|---|
| 仓库 | `~/Projects/herdr-router`（按阶段提交，保留可回退快照） |
| CLI | `~/.local/bin/herdr-router` → `bin/herdr-router`（symlink） |
| Herdr 插件 | `herdr plugin link ~/Projects/herdr-router`（已 link，改代码即生效，`herdr-plugin.toml` 改了才需要重新 link / reload） |
| Claude Code skill | `~/.claude/skills/herdr-route` → `skill/`（symlink） |
| 用户 policy | `~/.config/herdr/plugins/config/herdr-router/policy.json`（网页保存后才存在；不存在时用内置默认） |
| 备份 | 同目录 `policy.backups/`，保留最近 10 份 |
| 状态目录 | `~/.local/state/herdr/plugins/herdr-router/` |
| ├ `decisions.jsonl` | 每次 dispatch 追加一行（网页图上的历史点就来自这里） |
| ├ `sessions.sqlite3` | 派发 Session 与交接快照（标准库 SQLite） |
| ├ `ui-token` | 网页令牌（22 字符，0600） |
| ├ `ui.json` | 后台网页服务的 `{pid, host, port}` |
| └ `ui.log` | 插件 action 后台启动的网页服务日志 |

插件运行时 Herdr 会注入 `HERDR_PLUGIN_CONFIG_DIR` / `HERDR_PLUGIN_STATE_DIR`，`model.config_dir()` / `state_dir()` 优先读它们。**测试时把这两个变量指到临时目录，就不会碰到用户真实配置。**

## 目录与模块

```
bin/herdr-router          入口；把仓库根加进 sys.path 后调 router.cli.main
herdr-plugin.toml         插件清单：popup pane + 3 个 action
config/policy.default.json 内置默认 policy（version 3：档位用 point；v2 的 ranges 在 load 时自动迁移）
router/
  __init__.py   __version__, PLUGIN_ID
  model.py      policy 的读 / 存 / 校验；config_dir / state_dir；KNOWN_KINDS
  classify.py   任务 → Assessment（每个维度一个概率分布）
  score.py      Assessment × policy → 排名 → Decision
  agents.py     (kind, model, effort) → 各 agent CLI 的原生参数
  catalog.py    各 kind 可选模型（claude 静态别名、codex 读 models_cache.json、cursor 跑 --list-models 并缓存 24h）
  herdr.py      herdr CLI 的薄封装（全部走 subprocess + JSON）
  dispatch.py   Decision → prepare / name → split pane → start → 确认送达 → 可选 wait → 终态日志
  sessions.py   Session 持久化、短事务 checkpoint、父子关联与只读查询
  log.py        decisions.jsonl 追加（失败静默）
  cli.py        argparse 子命令、终端渲染、popup、插件 action
  web.py        本地网页服务（ThreadingHTTPServer）
web/            index.html / app.js / style.css（编辑器前端）
skill/SKILL.md  Claude Code skill：教 agent 用 herdr-router dispatch
tools/build_default_policy.py  从 AA 数据生成默认 policy
tests/          test_router.py（引擎）、test_web.py（API 安全）、fixtures/（录制的 Jev 响应）
```

依赖方向：`cli / web → dispatch → herdr`；`cli / web → score → classify → model`；`score → agents`；`web → catalog → model`。`model` 不依赖别的业务模块（只引 `agents.EFFORTS`）。

## 数据流

```
任务文本
  │ classify.parse_overrides   抽掉 @别名 / @档位id / !别名 / !维度=值
  ▼
cleaned task + Overrides(profile, dims, tokens)
  │ classify.assess
  │   ├ backend.enabled → call_systemone()  POST {base_url}/v1/systemone
  │   │                    每个维度 = 一个 question；返回各维度概率分布
  │   └ 失败/关闭 → heuristic()  关键词规则，给"点分布"（全部概率在一个值上）
  │                 置信度 < scoring.min_confidence 时 difficulty 上调一档（bumped=True）
  │   然后把 Overrides.dims 覆盖成点分布
  ▼
Assessment{dists, source, confidence, bumped, details}
  │ score.decide → rank → penalty（每个档位）
  ▼
Decision{profile_id, kind, model, effort, args, prompt, reason, assessment, ranking}
  │ cli.render（人看） / to_dict（--json）
  │ dispatch.dispatch（只有 dispatch 子命令和 popup 回车才会走到）
  ▼
herdr pane split → herdr agent start <name> --kind K --pane P -- <args> → herdr agent prompt
```

派发先用 `agent prompt --wait --until working --until blocked --timeout 15000` 确认活动；stalled 时只补充一次 `agent wait` 观察 working/blocked，不重发。用户 `--wait` 的结束等待在确认后单独执行，且不传 `--until settled`（Herdr 没有这个状态）。等待结束超时仍保留 `prompt_status=confirmed`。

每次调用 `dispatch()` 有 `dispatch_id`，finally 写一条终态记录（`event=dispatch`，保留旧字段），补充 `stage/status/prompt_status`。`not_sent` 是确定未发送，`unknown` 是可能已送达但无法确认，`confirmed` 是观察到提交后的活动。失败 `HerdrError` 带已知 pane/agent 和这些字段。CLI `--json` 成功 stdout 只有一个对象，失败 stdout 空、stderr 结构化错误；不要仅凭 exit 1 判断可以重试。

`dispatch()` 在 prepare 前创建 Session（id = dispatch_id），关键执行边界独立提交 checkpoint，事务不能跨 Herdr RPC。发送前先存 `prompt/unknown`；这一写入失败时不调用 prompt，实际输出保持 `not_sent`。确认后的 checkpoint 和最后存储失败不能遮蔽原异常或已确认送达，也不能跳过用户要求的 wait，输出附带 `session_warning`；锁竞争用 `session_store_busy` 区分其他存储错误。`in_progress` 仅表示缺少终态，不能用它断言 agent 当前工作或任务成功；不靠 PID 猜完成，不自动恢复/重发。

`session list/show` 不加载 policy、不调用 Herdr、不创建不存在的数据库。`dispatch --session <parent> --handoff <file>` 是新 pane/新尝试，通过 parent_id 关联，继承并验证同一 realpath cwd；前一 agent 的对话不会自动复制。父尝试必须 confirmed，但这不证明前一任务完成；交接前先人工检查结果。handoff 普通 UTF-8 文件最多 48KiB，保留正文快照；只用新 task 路由，`decide()` 后再拼交接（防止摘要里的 @alias/!override 改路由或流入 Jev），最终 prompt 最多 64KiB。网页不读取 Session、不能派发。

### 打分公式（score.penalty，越低越好）

- **score 维度**：`weight × Σ P(t) × (under_penalty × max(0, t − point − tol) + over_penalty × max(0, point − t − tol))`，单位是「级」，`tol = scoring.tolerance`（默认 0.5，点周围半格不罚）。
  - 档位的 `point` 里没写某维度 = 不挑这个维度（罚 0）。专才档位（codex-review）干脆不写 point。
  - Assessment 里没有这个维度的分布 → **该维度不参与**（不罚）。
  - 结果是一张加权、非对称的 Voronoi 图：强档位的领地往下延伸得多，往上只延伸一点。
  - `model.migrate()`：v2 的 `ranges` → 区间中点；全范围 → 不写（不挑）。`load()`、网页的 PUT / route / map 都会先迁移。
- **choice 维度**：`weight × Σ P(opt) × (1 − affinity[opt])`
  - affinity 没写的选项取 `affinity["*"]`，再没有就是 1。
  - Assessment 里没有这个维度 → **按均匀先验**算（不然 `{"*":0, "review":1}` 这种专才档位会在"不知道类型"时白赢）。
- 加 `cost_weight × cost`（每任务美元）和 `time_weight × time`（每任务分钟）。
- 排序键 `(total, cost, 在 profiles 里的下标)`。`enabled:false` 或二进制不在 PATH（`binaries` 映射后用 `shutil.which` 查）的档位不可选。
- 默认 `under_penalty=3, over_penalty=1`：能力不够比大材小用罚得重。`task_type` 权重 4：类型不符要罚得比「远一级」更重，否则专才档位会在空旷角落白赢。

`--profile` / `@别名` 钉死档位时忽略排名（disabled 也能钉，但不在 PATH 会报错）；`--kind` 只在该 kind 的档位里挑；`--model` / `--effort` 只修补胜出档位的字段。

### Jev / System One 后端

- 默认 OpenCode Zen：`base_url=https://opencode.ai/zen`，`model=jev-1.13-free`，**免费模型不需要 key**。付费 `jev-1.13` 读 `backend.api_key_env`（默认 `OPENCODE_API_KEY`）作为 Bearer。
- 请求：`{"state": 任务文本, "model": ..., "questions": {维度id: {type, instructions, criteria}}}`；score 维度的 criteria 是 `levels` 列表，choice 维度是 `options` 字典。
- 响应：`answers[维度id].probabilities`（score 维度的 key 是 `"0"`,`"1"`…字符串）和 `confidence`。`parse_systemone` 会归一化、丢掉不认识的选项。
- **必须带自定义 User-Agent**，否则 Cloudflare 返回 403 / error 1010。
- python.org 版 Python 没装 CA 根证书 → `_ssl_context()` 依次试 certifi、`/etc/ssl/cert.pem`。
- `backend.include_repo_signals` 默认 false：实测把仓库信号塞进 state 会让简单任务被判难（rename 的 P(hard) 从 0.10 升到 0.34）。
- 本地替代：Kev，`base_url=http://127.0.0.1:8009`，`model=kev-latest`。
- 超时默认 5 秒，失败自动走关键词兜底，错误写在 `assessment.details.systemone_error`。

## 默认 policy 速览

维度：`difficulty`（5 档，w=1）、`duration`（4 档，w=1）、`scope`（4 档，w=0.5）、`task_type`（choice：implement/bugfix/refactor/review/research/ui/test/docs，w=4）。

默认 policy 由 `tools/build_default_policy.py` 从 [Artificial Analysis](https://artificialanalysis.ai/) 数据生成（2026-10-11 抓取；`/leaderboards/models` 是服务端渲染的表格，`/models` 页内嵌 `intelligenceIndexTimePerTask`，`/agents/coding-agents` 内嵌每个 agent+模型的记录）。**改默认档位就改这个脚本里的数据再跑一遍，不要手改 JSON**，然后跑测试。
- **一个模型 × 一个思考档位 = 一个档位**。Luna / Haiku / Sonnet / Sol / Opus 各 5 档（low…max），在图上连成线（像 AA 的每模型曲线）。OpenCode 跑的 GLM / MiMo / DeepSeek 没有 effort 参数，只有一个点。
- **线条画全，但默认只启用常用档位**（`ROUTED`）：Luna low/high、Haiku medium/high、Sonnet medium、Sol medium/high/xhigh、Opus medium/high/xhigh；其余灰显不参与路由。全启用时 25 个点把平面铺满，Sol low 在 AA 数据上全面压制 GLM Flash / DeepSeek Flash，开源模型就看不见了（用户明确不要）。
- 难度坐标 = `(AA 智力指数 − 30) / 7`（58 → 4.0）；时长坐标按档位：low 0.3、medium 0.9、high 1.5、xhigh 2.2、max 2.8。
- `cost` = AA 每任务美元；`time` = AA「Intelligence Index vs. Time per Task」图里该模型测试档（max）的分钟数 × 统一档位系数（low .35 / medium .45 / high .55 / xhigh .64 / max 1，来自 AA 编程 agent 测试里 Sol 各档的耗时）。按各家族端到端响应比例缩放的做法已弃用：会把低档缩小约 20 倍，让 Sol medium 看起来比 GLM Flash 快 30 倍。
- 打分参数（和用户一起调的，见脚本顶部）：`under_penalty 4`、`over_penalty 0.5`、`cost_weight 0.6`、`agent_cost {opencode: 0.25}`（OpenCode Go 是包月，Codex / Claude 额度稀缺）、`time_weight 0.01`（只作参考：单档模型只有 max 档耗时，和别人的 medium 档比不公平）、`tolerance 0.5`、`task_type` 权重 6（类型不符必须比「离得远」罚得重，否则专才档位会在空角落胜出）。
- 结果（不限类型的面积）：Sol 约 47%、Luna 约 16%、Claude 约 22%、开源约 10%。琐碎/简单任务给开源 flash 模型（rename → DeepSeek Flash，加文本 / 批量替换 → GLM Flash），常规到难的给 Sol，专家级架构给 Opus xhigh。
- 用户偏好（明确说过）：Sol 应该占大面积但不能「太大」；简单活不要让 Sol / Sonnet 做，交给便宜的开源模型；开源模型要能看到；不要无理由偏向 Claude；有 Codex 和 OpenCode Go 订阅。调参后用 `score.territory` 统计各家面积，并对照这几条。
- 回归样例里的险胜写在 `RegressionTest` 的 docstring 里，调参时最先变。

**模型目录 `models`**：`{模型 id: {label, vendor, via: {agent kind: 该 agent 里的模型 ID}}}`。编辑器靠它做两件事：按 vendor 上色；选中点时显示「运行于」切换条（`harnessSeg()` / `switchHarness()`），切换时把档位的 `kind` 和 `model` 按 `via` 一起改。档位本身仍然只存 `kind` + `model`（启动的就是它们），目录只是反查表；`model.validate` 校验 via 的 kind 和 ID 格式。OpenCode 的 ID 来自 `opencode models`（用户有 OpenCode Go 订阅，`opencode-go/*`；Zen 付费的是 `opencode/*`），Droid 的 ID 是 droid 二进制里内置的（GLM / DeepSeek / Claude / GPT 都有，MiMo 没有）。

别名：`@opus`（opus-medium）`@deep`（opus-xhigh）`@sonnet`（sonnet-research）`@haiku`（haiku-high）`@codex @sol`（sol-medium）`@luna`（luna-high）`@review @kimi @cursor @glm @mimo @deepseek`；任何 `@档位id` 也行（如 `@opus-high`、`@sol-max`）；`!quick !easy !hard !deep !long !short`。任何 `@<档位id>` 和 `!<维度>=<值>` 也能用。

agent 参数映射（`agents.build_args`）：claude `--model/--effort`；codex `-m` + `-c model_reasoning_effort=`（原样传，codex 0.162 的 gpt-6.x 支持到 max，老模型只到 xhigh——编辑器按 catalog 限制可选等级）；kimi `-m` + `--thinking/--no-thinking`（low=关）；cursor `--model name[effort=x]`；opencode 有模型时用 `mini -m provider/model`（完整 TUI 没有模型参数，mini 有，且 herdr 能识别为 opencode 就绪；已实测），effort 没有参数；droid 用 `--settings <state>/droid/<model>-<effort>.json`，文件内容 `{"sessionDefaultSettings": {"model", "reasoningEffort"}}`，由 `agents.prepare()` 在 `dispatch` 启动前写入（`route` / 试运行不写文件）。kind 必须在 `herdr agent` 打印的 kinds 列表里（`herdr.supported_kinds()` 实时读，读不到用 `model.KNOWN_KINDS`）。

## 常用命令

```bash
cd ~/Projects/herdr-router

# 全部测试（58 个，<1 秒，不联网）
python3 -m unittest discover -s tests -t .
python3 -m unittest tests.test_router.RegressionTest -v   # 只跑回归

# 看一个任务怎么被路由（不启动任何东西）
herdr-router route "修复登录偶现的崩溃"
herdr-router route "…" --json | python3 -m json.tool       # 完整排名 + 罚分明细
herdr-router route "…" --no-systemone                       # 只用关键词兜底，排除网络因素
herdr-router classify "…"                                   # 只看各维度分布
herdr-router --policy /path/to/draft.json route "…"         # 用另一份 policy

# 真的派发（必须在 Herdr pane 里）
herdr-router dispatch "…" --dry-run
herdr-router dispatch "…" --json

# 网页编辑器
herdr-router ui --open          # 本机，直接开浏览器
herdr-router ui --lan           # 也监听局域网 / Tailscale，打印每个地址
herdr-router ui --stop          # 停掉插件 action 在后台起的那个
curl -s http://127.0.0.1:8765/healthz

# 直接打 Jev，看原始响应
python3 -c "
import json,sys; sys.path.insert(0,'.')
from router import model, classify
p=model.load(); print(json.dumps(classify.systemone_request('把这个变量重命名',p),ensure_ascii=False,indent=1))"
```

## 调试清单（按症状）

**改了默认 policy，用户那边看不到新档位**
- 用户保存过 `policy.json` 就会**整体替换**内置默认值，新加的默认档位不会自动出现。编辑器的档位列表顶部会提示「内置默认值里有 N 个档位不在你保存的配置里」，点「加入」合并（只追加缺的档位、模型目录和别名，不动已有的），再保存。
- 替用户合并时先和上一版默认值 diff，找出用户自己改过的字段再保留；`model.save` 会自动备份。

**路由结果不符合预期**
1. `herdr-router route "<任务>" --json`，看 `assessment.source`：`systemone` 还是 `heuristic`（后者说明 Jev 没调通，看 `details.systemone_error`）。`bumped:true` 表示兜底时难度被上调过。
2. 看 `ranking[]` 每项的 `breakdown`，找是哪个维度把期望档位罚高了。
3. 先判断是"分布错了"（Jev 判断问题 → 改维度的 `instructions` / `levels` 措辞）还是"坐标错了"（档位坐标 / affinity / weight / cost 问题 → 改 policy）。**不要为单个任务在代码里写特判。**
4. 改完默认 policy 必须跑回归测试；如果有意改变某个 fixture 的期望结果，同步改 `RegressionTest.EXPECT` 并说明原因。

**Jev 调不通**
- `HTTP 403` + `1010`：User-Agent 被改掉了。
- `CERTIFICATE_VERIFY_FAILED`：`_ssl_context()` 兜底失效。
- 超时：`backend.timeout_s`；网络走代理时注意本机默认路由在 198.18.x 的代理 TUN 上。
- **不要**尝试读取 / 导出 opencode 的凭据（`opencode auth export` 之类）——免费模型不需要，这类操作会被权限系统拒绝。

**dispatch 失败**
- `not_in_herdr`：不在 Herdr pane 里（`HERDR_ENV` 不是 1）。
- `agent_not_ready`：agent 启动时被弹窗卡住（比如信任目录对话框），pane 和名字仍然存活，用 `herdr agent read <name>` 看界面，**问用户**再决定按什么键。
- 档位"not on PATH"：`policy.env.extra_path` 会在启动时追加到 PATH；`binaries` 把 kind 映射到实际可执行名（cursor → cursor-agent）。
- 每次 dispatch 的完整 Decision 都在 `decisions.jsonl` 里，出错的那条带 `"error"`。
- **看日志同时看 `error/stage/prompt_status`**：启动失败通常是 `not_sent`；prompt 阶段超时或 stalled 可能已送达，是 `unknown`；确认后结束等待失败仍是 `confirmed`。后两种必须先查看原 pane，不要重新派发。启动弹窗常见于 Codex 更新提示（`Update available … 1. Update now (runs npm install -g)`）和信任目录对话框。弹窗里的「更新」会改用户的全局安装，**不要替用户按**，让用户选。popup 失败会显示恢复建议与已知 pane 的画面。

**网页打不开 / 报错**
- `{"error":"token required"}` 或「链接里的令牌不对」页：链接被截断（窄 pane 折行）或服务重启后令牌变了 → 用 `herdr-router ui --open`。令牌在 `state/ui-token`。
- `{"error":"host not allowed"}`：Host 头不是 IP / localhost / 本机名 / `*.ts.net`。用别的域名访问时加 `--allow-host 名字`。
- 端口占用：`lsof -iTCP:8765 -sTCP:LISTEN`；后台实例看 `state/ui.json`，`herdr-router ui --stop` 停。
- 改了 `web.py` 必须重启服务；改 `web/*` 静态文件刷新浏览器即可（服务每次请求都现读文件）。
- 保存返回 422：`errors[].path` 形如 `profiles[3].point.difficulty`，前端会用 `data-path` 把对应输入框标红。

**popup 卡住 / Herdr 看起来没反应**
- 插件 popup 会抢焦点。`_ask()` 有 300 秒空闲自动退出。手动处理：`ps aux | grep "herdr-router popup"` 找到 pid 后 kill。
- 用户经常从手机（Moshi）远程操作 Herdr，看不到桌面上的窗口。**测试时自己打开的 popup / 网页服务 / 浏览器，测完一定关掉。**

## 修改指南

**加一个维度**（不改代码）：在 policy `dimensions` 里加 `{id, label, type:"score", weight, instructions, levels:[...]}` 或 `type:"choice"` + `options:{...}`；给需要的档位在 `point` 里加坐标 / 加 `affinity`。Jev 请求会自动带上新问题。`tests/test_router.py::ExtensibilityTest` 是范例（security 维度）。关键词兜底只认识 difficulty / duration / task_type，新维度在兜底时不参与打分，这是预期行为。

**加一个 agent kind 的参数映射**：改 `agents.build_args`，加 `AgentArgsTest` 用例。参数以该 CLI 的 `--help` 为准，不要凭记忆。

**改校验规则**：`model.validate` 返回 `[{path, message}]`，path 要能被前端 `data-path` 定位（格式 `profiles[i].field` / `dimensions[i].field`）。`model.save` 校验失败抛 `InvalidPolicy`，成功时原子写入（tempfile + replace）并轮转备份。

**网页 API 一览**：`GET /api/policy`、`GET /api/policy/default`、`GET /api/log`、`GET /api/models`（catalog）、`PUT /api/policy`（保存）、`POST /api/route`（试运行）、`POST /api/map`（领地图：`score.territory()` 在平面上按 `steps`×`steps` 采样，每个采样点用点分布跑一遍 `rank()`，返回 `{x, y, ids, grid}`；带 `at: [x, y]` 时只返回该点前三名，给悬停读数用。前端不复制打分公式）。

**改网页 API**：所有 `/api/*` 都必须过 `_guard()`（Host 白名单）和 `_authed()`（cookie `hr_token` 或 `X-Router-Token`，`hmac.compare_digest` 比较）；写接口要求 `Content-Type: application/json`（挡表单 CSRF）。**网页永远不能触发 dispatch**——它能改启动参数，再能派发就等于远程执行。新增接口同步在 `tests/test_web.py` 加 401/403 用例。

**改前端**：`web/app.js` 是单文件，全局状态在 `S`（`S.policy` 草稿、`S.base` 已保存版本的 JSON 字符串、`S.sel` 当前档位、`S.choice` 领地图的类型过滤、`S.map` 领地图结果、`S.models` 模型目录），改完调 `changed()` 触发重绘、"未保存"标记和领地图刷新（`refreshMap()` 防抖；`fetchMap()` 同时只发一个请求，结束后补发最新状态；拖动中 `steps=48` 约 0.1 秒，松手后 `steps=96` 约 0.5 秒）。领地图由 `mapImage()` 画：每个采样点一个像素画进小 canvas，再平滑放大到绘图区并描出胜者变化处的细线，作为 SVG `<image>`（CSP 允许 `data:`）。路由图分层：领地图 → 网格 → 历史点 / 试运行十字 → 选中档位到两轴的虚线和数值 → 不挑某轴的档位画成沿该轴的虚线 → 档位点（`anchors()`，同位置的扇开，仅显示用）→ 标签（`placeLabels()` 贪心避让，放不下用引线）。拖点是连续的（`valAt()` 反算平面坐标，吸附 0.05），只改档位写了坐标的那些维度。颜色按模型厂商（`VENDORS`，从 `models` 目录反查 `vendorOf()`，查不到按 kind 猜），同厂商的档位用不同明度；标签在「非厂商自家 agent」时加 `· kind` 后缀。选中点下方浮出「运行于」切换条（`.dotbar`，HTML 叠在 SVG 上；按住点拖动时隐藏，松手后 `onChartUp` 重绘）。右栏 `renderEditor()` 未选中时是按厂商分组的档位列表，选中时是编辑器。样式只做暗色（Cursor 式：近黑底、纯白字、白底黑字主按钮，界面黑白灰，彩色只给数据），颜色都在 `:root` token 里；注意 `[hidden]` 全局 `display:none !important`，别让组件的 display 盖掉它。图表几何在 `G` / `geom()` / `fitGeometry()`；拖拽是 pointer events（`onChartDown/Move/Up`），手机触摸同一套。CSP 禁止内联脚本，事件都靠 `data-*` 属性委托。验证要同时看桌面（1360 宽）和手机（390 宽，不能出现横向滚动）。

**改终端输出**：Herdr 的 pane 可能只有 ~60 列，长 URL 会折行导致点击截断——要给人点的链接单独一行、不缩进、尽量 ≤ 60 字符。

**改插件清单**（`herdr-plugin.toml`）：`command` 路径相对仓库根。`herdr plugin action invoke` 是异步的，看不到返回值，调试 action 时直接运行 `bin/herdr-router action <id>` 并手动设 `HERDR_PLUGIN_CONTEXT_JSON`。

## 测试约定

- 测试不联网、不调用 herdr：`decide(..., which=ALL)` 假装所有二进制都在；Jev 响应用 `tests/fixtures/*.json`（`{task, response}`）。
- 新录一个 fixture：用真实 Jev 跑一次 `call_systemone` 拿到原始响应，存成 `{"task": ..., "response": ...}`，再把期望档位加进 `RegressionTest.EXPECT`（`test_all_fixtures_covered` 要求两边一致）。
- 涉及配置读写的测试用 `mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": tmp, "HERDR_PLUGIN_STATE_DIR": tmp})`。
- 手动起网页做实验时换个端口（如 `--port 8796`）并同样隔离这两个目录，避免冲掉用户正在用的 8765 实例和令牌。
- 看网页效果**不要用用户的 Chrome**（包括 headless 跑他的 Chrome 和 Claude in Chrome 扩展）。用 Playwright 自带的独立浏览器 `~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-mac-arm64/chrome-headless-shell`，配临时 `--user-data-dir`，通过 `--remote-debugging-pipe` 用标准库驱动 CDP（fd 3/4 用 `sh -c '… 3<&N 4>&M'` 接上）。测完删掉临时 profile。

## 和 Herdr 交互的注意事项

- Herdr 0.9.3。CLI 语法以 `herdr --help` / `herdr agent` / `herdr pane` 的输出为准。
- 只在 `HERDR_ENV=1` 时操作 Herdr；默认 `--no-focus`，不要关闭不是自己创建的 pane / tab / workspace，不要 `herdr server stop`。
- `agent start` 只能用于停在 shell 提示符的空 pane；router 总是先 `pane split` 出新 pane 再启动。
- agent 名字规则 `[a-z][a-z0-9_-]{0,31}`，router 默认名称含 `dispatch_id` 短后缀，再由 `herdr.unique_agent_name` 避免已存在的名称；命名和校验在 split 前完成。

## 已知限制 / 待办

- 关键词兜底只覆盖三个内置维度。
- opencode 交互模式不支持 effort（用模型默认档）。
- 本机 droid 启动时报 `FACTORY_API_KEY is set but appears to be invalid`，而 herdr 仍报告启动成功（agent 立即退出）。凭据是用户的事，不要去读或改；dispatch 到 droid 前先提醒用户。
- 还没做：在新 Claude 会话里测试 skill 触发；Herdr 快捷键绑定（README 有示例）；Moshi 的 Web 标签能否打开编辑器。
