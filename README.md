# herdr-router

Route a coding task to the right agent, model, and effort, then launch it in [Herdr](https://herdr.dev).

```
 difficulty
     ▲
   4 │ ┌ opus-high ┐ ┌───── opus-xhigh ─────┐
   3 │ │           │ │   (plan first)        │
   2 │ └───────────┘ └───────────────────────┘
   1 │ ┌ haiku-low ┐ ┌──── codex-medium ─────┐
   0 │ └───────────┘ └───────────────────────┘
     └──────────────────────────────────────▶ duration
          0      1        2          3
```

A task is assessed along any number of **dimensions** (difficulty, duration, scope, task type, …). Each **profile** (an agent + model + effort) occupies a range on every dimension. The profile with the lowest penalty wins. Dimensions and profiles are data, so you can add a dimension or move a profile without touching code. The web editor lets you drag the rectangles above.

## How a decision is made

1. **Inline overrides** are read and removed from the prompt:
   - `@opus`, `@codex`, `@haiku`, … are aliases that pin a profile. `@<profile-id>` also works.
   - `!hard`, `!long`, `!quick`, … pin a dimension. `!<dimension>=<value>` also works.
2. **Assessment**: every dimension becomes one question in a single System One call. By default this goes to [Jev](https://opencode.ai/docs/zen/) on OpenCode Zen (`jev-1.13-free`, no key needed). The answer is a probability distribution per dimension. When the call fails, keyword heuristics (Chinese and English) fill in difficulty, duration, and task type as point estimates. If their confidence is low, difficulty is bumped up one step.
3. **Scoring** (lower is better):
   - score dimension: `weight × (under_penalty × P(above range) + over_penalty × P(below range))`
   - choice dimension: `weight × Σ P(option) × (1 − affinity)`. When nothing is known about a choice dimension, a uniform prior is used.
   - plus `cost_weight × cost`

   `under_penalty` (3) is larger than `over_penalty` (1) on purpose: an underpowered agent costs more than an overpowered one. A profile whose CLI is not on PATH is skipped. Ties go to the cheaper profile.

`herdr-router route "<task>"` prints the per-dimension distributions, the winner, and the top 3 profiles with their penalty breakdown. `--json` prints the full ranking.

## Install

```bash
ln -s ~/Projects/herdr-router/bin/herdr-router ~/.local/bin/herdr-router   # CLI
herdr plugin link ~/Projects/herdr-router                                  # Herdr plugin
ln -s ~/Projects/herdr-router/skill ~/.claude/skills/herdr-route           # Claude Code skill
```

Python ≥ 3.11, standard library only.

## Use

```bash
herdr-router route "修复登录偶现的崩溃"            # explain the decision, launch nothing
herdr-router dispatch "修复登录偶现的崩溃"         # split a pane, start the agent, send the task
herdr-router dispatch "@codex 把所有 Debug.Log 换成 Logger.Info" --json
herdr-router route "…" --profile opus-high          # pin a profile
herdr-router route "…" --kind codex --effort high   # best codex profile, effort patched
herdr-router classify "设计状态同步架构"            # raw per-dimension distributions
```

Every dispatch is appended to `~/.local/state/herdr/plugins/herdr-router/decisions.jsonl`. The editor plots these entries on the chart.

## Web editor

```bash
herdr-router ui            # http://127.0.0.1:8765/?token=…
herdr-router ui --lan      # also listen on LAN / Tailscale addresses (prints each URL)
herdr-router ui --stop     # stop a background editor started by the plugin action
```

- **坐标与档位** (coordinates and profiles): pick any two score dimensions as the axes. Drag a rectangle to move a profile, or drag an edge or corner to resize its range. Touch works too. The side panel edits everything else: agent, model, effort, cost, ranges on the other dimensions, type affinity, and prompt prefix. Profiles limited to one task type (for example "仅 review") are drawn dotted.
- **维度** (dimensions): add, remove, reorder, or rename dimensions, and edit their question, levels or options, and weight. When a dimension loses levels, existing ranges shrink to fit.
- **试运行** (trial run): runs a task against the unsaved draft. It shows each dimension's distribution and the full ranking, and draws the task's probability heat and crosshair on the chart.
- **设置 / JSON** (settings / JSON): penalty knobs, backend, aliases, and raw JSON for the rest.

Saving validates the policy on the server, keeps the last 10 versions in `policy.backups/`, and writes `~/.config/herdr/plugins/config/herdr-router/policy.json`. The file is replaced atomically. The CLI reads the policy on every run, so changes apply immediately.

Security: every API call needs the token. It is stored in the state dir; the first visit trades the `?token=` link for an HttpOnly cookie. Requests whose `Host` header is an unknown domain are refused, which defends against DNS rebinding. The editor can change launch arguments but **cannot dispatch agents**.

Plugin actions:
- `herdr-router.route` opens a popup. Type a task, review the ranking, then press Enter to dispatch. Use `k`/`m`/`e` to change the profile, model, or effort.
- `herdr-router.route-selection` does the same with the terminal selection as the task.
- `herdr-router.open-editor` starts the editor in the background if needed and opens it in the browser.

Keybinding example (`~/.config/herdr/config.toml`, then `herdr server reload-config`):

```toml
[[keys.command]]
key = "cmd+shift+r"
type = "plugin_action"
command = "herdr-router.route"
```

## Policy file

The default is `config/policy.default.json`. A user `policy.json` replaces it wholesale.

```jsonc
{
  "dimensions": [
    { "id": "difficulty", "type": "score", "weight": 1, "instructions": "How hard…", "levels": ["Trivial…", "…"] },
    { "id": "task_type",  "type": "choice", "weight": 2, "instructions": "What kind…", "options": { "review": "…" } }
  ],
  "profiles": [
    { "id": "codex-review", "kind": "codex", "effort": "high", "cost": 3,
      "ranges":   { "difficulty": [0, 4] },                      // missing dimension = full range
      "affinity": { "task_type": { "*": 0, "review": 1 } } }     // "*" = default for unlisted options
  ],
  "scoring": { "under_penalty": 3, "over_penalty": 1, "cost_weight": 0.05 },
  "aliases": { "@review": "codex-review", "!hard": { "difficulty": 3 } }
}
```

Backends that speak the same System One API:
- Paid `jev-1.13`: set `backend.model = "jev-1.13"` and export `OPENCODE_API_KEY`.
- Local [Kev](https://github.com/jaredpalmer/kev): set `backend.base_url = "http://127.0.0.1:8009"` and `model = "kev-latest"`.

Agent flags (checked against each CLI's `--help`):

| kind | model | effort |
|---|---|---|
| claude | `--model` | `--effort low…max` |
| codex | `-m` | `-c model_reasoning_effort=…` (max → xhigh) |
| kimi | `-m` | `--thinking` / `--no-thinking` (low) |
| cursor | `--model name[effort=…]` | in the model bracket |
| opencode, droid | — | — (interactive mode has no flags; their own config applies) |

## Test

```bash
python3 -m unittest discover -s tests -t .
```

`tests/fixtures/` holds recorded `jev-1.13-free` responses. They are regression cases that pin the default policy's routing.
