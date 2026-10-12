# herdr-router

herdr-router decides which coding agent, model, and reasoning effort should
handle a task, then starts that agent in a new [Herdr](https://herdr.dev) pane
and sends it the task.

If you run several coding agents, picking one for every task by hand gets
tedious, and the defaults are often wrong in both directions: a strong model at
high effort spends quota on a one-line rename, and a small model fails at a
design problem. herdr-router makes that choice from a policy you can see and
edit, and explains each decision.

https://github.com/user-attachments/assets/a3fac404-4d1f-4d71-b9cc-b34376324952

*16-second demo ([MP4](docs/media/herdr-router-demo.mp4)): Ctrl+B, then d opens
the router. Enter a task, see the recommended agent, model and effort, then
press Enter to start Codex in a new pane. The interface is in English; typing
and waiting are sped up, with the popup's local path hidden.*

![Routing map: profile dots placed by difficulty and duration, with the background shaded by the profile that wins each area](docs/images/routing-map-en.png)

*The routing map in the web editor. Each dot is a profile; the shading shows
which profile a task at that difficulty and duration would go to.*

## How it works

1. **Assess the task.** The task text is scored on a set of dimensions:
   difficulty, duration, scope, and task type by default. This uses the free
   `jev-1.13-free` model on OpenCode Zen (no API key), with English and Chinese
   keyword rules as an offline fallback.
2. **Pick a profile.** A profile is an agent, model, and effort with a position
   on those dimensions, for example Codex with GPT-6.1 Sol at medium effort.
   Each profile gets a penalty for being too weak, being more than the task
   needs, mismatching the task type, and its price and time per task. The
   lowest penalty wins.
3. **Dispatch in Herdr.** The router splits the current pane, starts the chosen
   agent with the right model and effort flags, sends the task, and waits until
   Herdr sees the agent working or blocked.

## Features

- **Routing is data, not code.** Dimensions, profiles, weights, and aliases live
  in one JSON policy. Adding a dimension needs no code change.
- **Territory map with draggable profiles.** The editor shades the plane by the
  winning profile, computed by the same scoring code the CLI uses. Drag a dot
  and the map updates.
- **Decisions you can inspect.** `route --json` shows the full ranking and each
  profile's penalty breakdown. The editor's trial run does the same against
  unsaved changes, without starting anything.
- **Several agent CLIs.** Claude Code, Codex, Cursor, Kimi, OpenCode, and Droid,
  each with its own model and effort flags. The same model can be switched
  between agents that can run it.
- **Your call wins.** `@opus` or `@<profile-id>` in the task pins a profile,
  `!hard` or `!duration=3` pins a dimension, and `--profile`, `--kind`,
  `--model`, `--effort` do the same from the command line.
- **Careful delivery.** Each dispatch reports whether the task was `not_sent`,
  `unknown`, or `confirmed`, and the router never resends a prompt on its own.
- **Sessions, handoff, and worktrees.** Dispatches are recorded in a local
  SQLite database. A follow-up task can continue a session in a new pane with a
  handoff file, and `--isolation worktree` runs a task on its own Git branch and
  working tree.
- **Small footprint.** Python standard library only, no frontend build. The
  terminal output and the local editor are available in English or Chinese.
  The editor needs a token and cannot dispatch agents.

The bundled policy is derived from [Artificial Analysis](https://artificialanalysis.ai/)
benchmark, price, and time data; the [guide](docs/guide.md#the-default-policy)
explains how.

## Requirements

- macOS or Linux, with Python 3.11 or newer available as `python3`.
- [Herdr](https://herdr.dev), tested with 0.9.3. Dispatching only works from
  inside a Herdr pane.
- At least one agent CLI used by the enabled profiles, such as Codex, Claude
  Code, or OpenCode, installed and signed in. The router only checks that the
  binary is on `PATH`; model access depends on that CLI's account.

## Install

```bash
git clone https://github.com/Qingswe/herdr-router.git
cd herdr-router
./bin/herdr-router setup
```

`setup` links the CLI into `~/.local/bin`, detects installed agent CLIs,
installs the bundled `herdr-route` skill for them, links the Herdr plugin,
and adds available router shortcuts. It reports each result and preserves
existing skills, CLI links, disabled/different plugin installations, and
conflicting keys. Existing Herdr config is backed up before changes. It tries
to reload the running server; if the server is stopped, start Herdr to apply
the saved config. No agent CLI, account, or model subscription is installed.

The CLI, plugin, and skills run from this checkout, so keep it where it is.
Add `~/.local/bin` to `PATH` in your shell startup file if setup reports it
missing. Start a new agent session after setup to load the skill.

Check the installation at any time (read-only, no server or network needed):

```bash
./bin/herdr-router doctor
herdr-router doctor --json
```

Both commands exit with status 1 when required items are missing, conflicting,
or invalid. PATH and deferred config reload reminders are warnings. To install
or check skills for specific agents, repeat `--agent`:

```bash
./bin/herdr-router setup --agent claude --agent codex
./bin/herdr-router setup --no-shortcuts   # keep shortcuts entirely manual
./bin/herdr-router setup --no-plugin      # skip plugin linking
```

Skill locations (each contains `herdr-route/SKILL.md`):

| Agent | User skill directory |
|---|---|
| [Claude Code](https://code.claude.com/docs/en/skills) | `~/.claude/skills` (`CLAUDE_CONFIG_DIR` overrides `.claude`) |
| [Codex](https://learn.chatgpt.com/docs/build-skills) | `~/.agents/skills` |
| [Cursor](https://prod.cursor.com/docs/skills) | `~/.agents/skills` |
| [Kimi](https://github.com/MoonshotAI/kimi-cli/blob/main/docs/en/customization/skills.md) | `~/.kimi/skills` |
| [OpenCode](https://opencode.ai/docs/skills/) | `~/.agents/skills` |
| [Droid](https://docs.factory.com/harness/skills) | `~/.agents/skills` |

Compatible agents share one skill link in `~/.agents/skills`.
The skill includes dispatch, wait, and result inspection; a separate `herdr`
skill is not required. It teaches agents to delegate when you request it.
Human-facing terminal commands and the popup remind you to run setup if a
detected agent's skill is missing or points to another checkout. JSON output
stays unchanged. `herdr plugin link` or `herdr plugin install` on its own does
not run this setup; run the checkout's `bin/herdr-router setup` afterward.

## Usage

Check where a task would go. This launches nothing and needs no network:

```bash
herdr-router route "rename one variable" --no-systemone
```

It prints the estimate for each dimension, the chosen profile, and the top
three with their penalties. Terminal output is in Chinese by default. For
English, add `"terminal_language": "en"` to your policy (it also applies to
the popup), or set `HERDR_ROUTER_LANGUAGE=en` for a single command.

From inside a Herdr pane, dispatch it for real:

```bash
herdr-router dispatch "fix the intermittent crash on login"
herdr-router dispatch "@opus review the session schema"    # pin a profile by alias
herdr-router dispatch "..." --dry-run                      # decide and validate only
```

If a dispatch fails, read `prompt_status` before retrying: `unknown` or
`confirmed` means the task may already have arrived, so look at the pane instead
of sending it again. See [Dispatch and recovery](docs/guide.md#dispatch-and-recovery).

Open the web editor:

```bash
herdr-router ui --open     # 127.0.0.1:8765, opens the browser
herdr-router ui --stop     # stop an editor the plugin started in the background
```

Saving writes `~/.config/herdr/plugins/config/herdr-router/policy.json` and
keeps the last 10 versions.

## Herdr shortcuts

`setup` adds these bindings to `~/.config/herdr/config.toml` (or
`HERDR_CONFIG_PATH`), preserving keys already in use. If you skipped shortcuts
or need different keys, add them manually:

```toml
[[keys.command]]
key = "prefix+d"
type = "plugin_action"
command = "herdr-router.route"

[[keys.command]]
key = "prefix+ctrl+d"
type = "plugin_action"
command = "herdr-router.route-selection"

[[keys.command]]
key = "prefix+shift+e"
type = "plugin_action"
command = "herdr-router.open-editor"
```

Run `herdr server reload-config` to apply them. `prefix` means your configured
Herdr prefix key: press it first, then the key below. Linking the plugin alone
does not install these bindings; `setup` does.

| After the prefix | Action |
|---|---|
| `d` | Open the router popup for the focused pane |
| `Ctrl+d` | Open the popup with selected terminal text as the task |
| `Shift+e` | Open the local web editor |

In the popup, enter a task and press Enter to see the recommendation. Press
Enter again to dispatch it: the popup hides itself at once and dispatches in
the background, so you keep working while the pane is created; a failure or an
unconfirmed delivery reopens it with the details. `Esc` (or Ctrl-C) closes the
popup from any prompt. To adjust instead, type `k` (profile), `m` (model),
`e` (effort), or `t` (task), then press Enter. The
[guide](docs/guide.md#herdr-plugin-actions) has more details.

## More

The [user guide](docs/guide.md) covers the scoring formula, the default policy,
the policy file, sessions and handoff, worktree isolation, the editor tabs, file
locations, and troubleshooting.

Run the tests with:

```bash
python3 -m unittest discover -s tests -t .
node tests/test_frontend.js      # needs Node.js
```
