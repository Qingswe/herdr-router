# herdr-router 开发与调试手册

给在这个仓库里协作的 agent（Claude Code / Sonnet 等）看的。用户向的功能说明见 `README.md`，本文只讲**代码怎么组织、数据怎么流、出问题去哪查、改动时别踩哪些坑**。

## 一句话

把一段任务文本按 N 个维度（难度、时长、范围、任务类型…）打出概率分布，再给每个「档位」（agent + model + effort + 各维度坐标区间）算罚分，罚分最低的档位胜出，然后用 Herdr 在旁边开 pane 启动这个 agent 并把任务发过去。

- Python ≥ 3.11，**只用标准库**（不要引入第三方依赖；`certifi` 只是可选兜底）。
- 前端是原生 JS + SVG，无构建、无 CDN。
- 维度和档位全是数据（`config/policy.default.json`），**加维度 / 调坐标不应该需要改代码**。如果某个需求逼你去改 `score.py` 来"特判某个维度"，先停下来想能不能用数据表达。

## 安装位置（本机）

| 东西 | 位置 |
|---|---|
| 仓库 | `~/Projects/herdr-router`（git 已 init，尚无提交） |
| CLI | `~/.local/bin/herdr-router` → `bin/herdr-router`（symlink） |
| Herdr 插件 | `herdr plugin link ~/Projects/herdr-router`（已 link，改代码即生效，`herdr-plugin.toml` 改了才需要重新 link / reload） |
| Claude Code skill | `~/.claude/skills/herdr-route` → `skill/`（symlink） |
| 用户 policy | `~/.config/herdr/plugins/config/herdr-router/policy.json`（网页保存后才存在；不存在时用内置默认） |
| 备份 | 同目录 `policy.backups/`，保留最近 10 份 |
| 状态目录 | `~/.local/state/herdr/plugins/herdr-router/` |
| ├ `decisions.jsonl` | 每次 dispatch 追加一行（网页图上的历史点就来自这里） |
| ├ `ui-token` | 网页令牌（22 字符，0600） |
| ├ `ui.json` | 后台网页服务的 `{pid, host, port}` |
| └ `ui.log` | 插件 action 后台启动的网页服务日志 |

插件运行时 Herdr 会注入 `HERDR_PLUGIN_CONFIG_DIR` / `HERDR_PLUGIN_STATE_DIR`，`model.config_dir()` / `state_dir()` 优先读它们。**测试时把这两个变量指到临时目录，就不会碰到用户真实配置。**

## 目录与模块

```
bin/herdr-router          入口；把仓库根加进 sys.path 后调 router.cli.main
herdr-plugin.toml         插件清单：popup pane + 3 个 action
config/policy.default.json 内置默认 policy（version 2）
router/
  __init__.py   __version__, PLUGIN_ID
  model.py      policy 的读 / 存 / 校验；config_dir / state_dir；KNOWN_KINDS
  classify.py   任务 → Assessment（每个维度一个概率分布）
  score.py      Assessment × policy → 排名 → Decision
  agents.py     (kind, model, effort) → 各 agent CLI 的原生参数
  herdr.py      herdr CLI 的薄封装（全部走 subprocess + JSON）
  dispatch.py   Decision → split pane → agent start → agent prompt → 写日志
  log.py        decisions.jsonl 追加（失败静默）
  cli.py        argparse 子命令、终端渲染、popup、插件 action
  web.py        本地网页服务（ThreadingHTTPServer）
web/            index.html / app.js / style.css（编辑器前端）
skill/SKILL.md  Claude Code skill：教 agent 用 herdr-router dispatch
tests/          test_router.py（引擎）、test_web.py（API 安全）、fixtures/（录制的 Jev 响应）
```

依赖方向：`cli / web → dispatch → herdr`；`cli / web → score → classify → model`；`score → agents`。`model` 不依赖别的业务模块（只引 `agents.EFFORTS`）。

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

### 打分公式（score.penalty，越低越好）

- **score 维度**：`weight × (under_penalty × P(x > max) + over_penalty × P(x < min))`
  - 档位没写某维度的 range = 全范围 `[0, len(levels)-1]`。
  - Assessment 里没有这个维度的分布 → **该维度不参与**（不罚）。
- **choice 维度**：`weight × Σ P(opt) × (1 − affinity[opt])`
  - affinity 没写的选项取 `affinity["*"]`，再没有就是 1。
  - Assessment 里没有这个维度 → **按均匀先验**算（不然 `{"*":0, "review":1}` 这种专才档位会在"不知道类型"时白赢）。
- 加 `cost_weight × cost`。
- 排序键 `(total, cost, 在 profiles 里的下标)`。`enabled:false` 或二进制不在 PATH（`binaries` 映射后用 `shutil.which` 查）的档位不可选。
- 默认 `under_penalty=3, over_penalty=1`：能力不够比大材小用罚得重。

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

维度：`difficulty`（5 档，w=1）、`duration`（4 档，w=1）、`scope`（4 档，w=0.5）、`task_type`（choice：implement/bugfix/refactor/review/research/ui/test/docs，w=2）。

| 档位 | agent | 难度 | 时长 | 说明 |
|---|---|---|---|---|
| haiku-low | claude haiku low | 0-1 | 0-1 | cost 1 |
| kimi-low | kimi --no-thinking | 0-1 | 0-1 | cost 1.2 |
| opencode-default | opencode | 0-1 | 0-1 | cost 1.5，无参数 |
| codex-medium | codex medium | 0-1 | 1-3 | 批量/机械活 |
| droid-default | droid | 0-1 | 1-3 | 无参数 |
| sonnet-bulk | claude sonnet medium | 0-1 | 1-3 | |
| opus-high | claude opus high | 2-4 | 0-1 | |
| opus-xhigh | claude opus xhigh | 2-4 | 2-3 | 带「先给计划」prompt_prefix |
| codex-review | codex high | 全 | 全 | 只适合 review |
| sonnet-research | claude sonnet medium | 全 | 0-1 | 只适合 research |
| cursor-ui | cursor-agent | 0-1 | 0-2 | 只适合 ui |

别名：`@opus @deep @sonnet @haiku @codex @review @kimi @cursor @droid @opencode`；`!quick !easy !hard !deep !long !short`。任何 `@<档位id>` 和 `!<维度>=<值>` 也能用。

agent 参数映射（`agents.build_args`）：claude `--model/--effort`；codex `-m` + `-c model_reasoning_effort=`（max→xhigh）；kimi `-m` + `--thinking/--no-thinking`（low=关）；cursor `--model name[effort=x]`；opencode / droid 不传参数。kind 必须在 `herdr agent` 打印的 kinds 列表里（`herdr.supported_kinds()` 实时读，读不到用 `model.KNOWN_KINDS`）。

## 常用命令

```bash
cd ~/Projects/herdr-router

# 全部测试（43 个，<1 秒，不联网）
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

**路由结果不符合预期**
1. `herdr-router route "<任务>" --json`，看 `assessment.source`：`systemone` 还是 `heuristic`（后者说明 Jev 没调通，看 `details.systemone_error`）。`bumped:true` 表示兜底时难度被上调过。
2. 看 `ranking[]` 每项的 `breakdown`，找是哪个维度把期望档位罚高了。
3. 先判断是"分布错了"（Jev 判断问题 → 改维度的 `instructions` / `levels` 措辞）还是"坐标错了"（档位区间 / affinity / weight / cost 问题 → 改 policy）。**不要为单个任务在代码里写特判。**
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

**网页打不开 / 报错**
- `{"error":"token required"}` 或「链接里的令牌不对」页：链接被截断（窄 pane 折行）或服务重启后令牌变了 → 用 `herdr-router ui --open`。令牌在 `state/ui-token`。
- `{"error":"host not allowed"}`：Host 头不是 IP / localhost / 本机名 / `*.ts.net`。用别的域名访问时加 `--allow-host 名字`。
- 端口占用：`lsof -iTCP:8765 -sTCP:LISTEN`；后台实例看 `state/ui.json`，`herdr-router ui --stop` 停。
- 改了 `web.py` 必须重启服务；改 `web/*` 静态文件刷新浏览器即可（服务每次请求都现读文件）。
- 保存返回 422：`errors[].path` 形如 `profiles[3].ranges.difficulty`，前端会用 `data-path` 把对应输入框标红。

**popup 卡住 / Herdr 看起来没反应**
- 插件 popup 会抢焦点。`_ask()` 有 300 秒空闲自动退出。手动处理：`ps aux | grep "herdr-router popup"` 找到 pid 后 kill。
- 用户经常从手机（Moshi）远程操作 Herdr，看不到桌面上的窗口。**测试时自己打开的 popup / 网页服务 / 浏览器，测完一定关掉。**

## 修改指南

**加一个维度**（不改代码）：在 policy `dimensions` 里加 `{id, label, type:"score", weight, instructions, levels:[...]}` 或 `type:"choice"` + `options:{...}`；给需要的档位加 `ranges` / `affinity`。Jev 请求会自动带上新问题。`tests/test_router.py::ExtensibilityTest` 是范例（security 维度）。关键词兜底只认识 difficulty / duration / task_type，新维度在兜底时不参与打分，这是预期行为。

**加一个 agent kind 的参数映射**：改 `agents.build_args`，加 `AgentArgsTest` 用例。参数以该 CLI 的 `--help` 为准，不要凭记忆。

**改校验规则**：`model.validate` 返回 `[{path, message}]`，path 要能被前端 `data-path` 定位（格式 `profiles[i].field` / `dimensions[i].field`）。`model.save` 校验失败抛 `InvalidPolicy`，成功时原子写入（tempfile + replace）并轮转备份。

**改网页 API**：所有 `/api/*` 都必须过 `_guard()`（Host 白名单）和 `_authed()`（cookie `hr_token` 或 `X-Router-Token`，`hmac.compare_digest` 比较）；写接口要求 `Content-Type: application/json`（挡表单 CSRF）。**网页永远不能触发 dispatch**——它能改启动参数，再能派发就等于远程执行。新增接口同步在 `tests/test_web.py` 加 401/403 用例。

**改前端**：`web/app.js` 是单文件，全局状态在 `S`（`S.policy` 草稿、`S.base` 已保存版本的 JSON 字符串、`S.sel` 当前档位），改完调 `changed()` 触发重绘和"未保存"标记。图表几何在 `G` / `geom()` / `fitGeometry()`；拖拽是 pointer events（`onChartDown/Move/Up`），手机触摸同一套。CSP 禁止内联脚本，事件都靠 `data-*` 属性委托。验证要同时看桌面（1360 宽）和手机（390 宽，不能出现横向滚动）。

**改终端输出**：Herdr 的 pane 可能只有 ~60 列，长 URL 会折行导致点击截断——要给人点的链接单独一行、不缩进、尽量 ≤ 60 字符。

**改插件清单**（`herdr-plugin.toml`）：`command` 路径相对仓库根。`herdr plugin action invoke` 是异步的，看不到返回值，调试 action 时直接运行 `bin/herdr-router action <id>` 并手动设 `HERDR_PLUGIN_CONTEXT_JSON`。

## 测试约定

- 测试不联网、不调用 herdr：`decide(..., which=ALL)` 假装所有二进制都在；Jev 响应用 `tests/fixtures/*.json`（`{task, response}`）。
- 新录一个 fixture：用真实 Jev 跑一次 `call_systemone` 拿到原始响应，存成 `{"task": ..., "response": ...}`，再把期望档位加进 `RegressionTest.EXPECT`（`test_all_fixtures_covered` 要求两边一致）。
- 涉及配置读写的测试用 `mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": tmp, "HERDR_PLUGIN_STATE_DIR": tmp})`。
- 手动起网页做实验时换个端口（如 `--port 8796`）并同样隔离这两个目录，避免冲掉用户正在用的 8765 实例和令牌。

## 和 Herdr 交互的注意事项

- Herdr 0.9.3。CLI 语法以 `herdr --help` / `herdr agent` / `herdr pane` 的输出为准。
- 只在 `HERDR_ENV=1` 时操作 Herdr；默认 `--no-focus`，不要关闭不是自己创建的 pane / tab / workspace，不要 `herdr server stop`。
- `agent start` 只能用于停在 shell 提示符的空 pane；router 总是先 `pane split` 出新 pane 再启动。
- agent 名字规则 `[a-z][a-z0-9_-]{0,31}`，router 生成 `r-<model或kind>-N`（`herdr.unique_agent_name`）。

## 已知限制 / 待办

- 关键词兜底只覆盖三个内置维度。
- opencode / droid 交互模式没有 model/effort 参数，档位里的 model/effort 对它们无效。
- 还没做：在新 Claude 会话里测试 skill 触发；Herdr 快捷键绑定（README 有示例）；Moshi 的 Web 标签能否打开编辑器。
