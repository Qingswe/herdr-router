---
name: herdr-route
description: Delegate a task to another coding agent through Herdr when the user has NOT said which agent, model, or effort to use. herdr-router assesses the task along configurable dimensions (difficulty, duration, scope, task type, …), ranks the configured agent profiles (claude/codex/kimi/cursor/droid/opencode with model and effort), then starts the winner in a sibling pane. Use when the user asks to hand off, delegate, parallelize, or "let another agent do" something inside Herdr. Requires HERDR_ENV=1. If the user names a specific agent, still use this but pin it with --kind or --profile.
---

# herdr-route

`herdr-router` picks the agent for you; the `herdr` skill covers everything after the launch.

## Check

```bash
test "${HERDR_ENV:-}" = 1 && command -v herdr-router
```

If either fails, say so and stop.

## Preview (optional, nothing launched)

```bash
herdr-router route "<task>"            # per-dimension distributions, winner, top-3 ranking with penalties
herdr-router route "<task>" --json     # full ranking
```

## Dispatch

```bash
herdr-router dispatch "<task>" --json
```

This splits the calling pane (`--no-focus`), starts the chosen agent with its model and effort flags, and submits the task. The JSON reply carries `agent` (a live Herdr agent name), `pane_id`, and the full `decision`. Then continue with the herdr skill:

```bash
herdr agent wait <agent> --timeout 600000
herdr agent read <agent> --source recent-unwrapped --lines 120
```

Write the task as a complete, self-contained instruction: the receiving agent has none of your context.

## Pinning

User preference always wins. Pass what they specified and let the router fill the rest:

- `--profile <id>` pins a profile; `--kind codex` takes the best-ranked profile of that agent kind
- `--model opus` / `--effort high` (low|medium|high|xhigh|max) patch whichever profile wins
- Or inline tokens in the task: `@opus`, `@codex`, `@haiku`, `@<profile-id>`, `!deep`, `!quick`, `!long`, `!<dimension>=<value>`
- `--name <agent-name>` to choose the Herdr agent name; `--pane <id>` to split a different pane; `--cwd <dir>`.

## Errors

Errors are JSON on stderr with exit 1.
- `agent_not_ready`: the agent is blocked at startup (for example a trust dialog). Inspect it with `herdr agent read` and ask the user before answering.
- `no_agent_available`: no enabled profile has its CLI installed (or the pinned one is missing). Report it; do not guess another agent.
- `not_in_herdr`: you are not inside a Herdr pane.

Do not retry a failed dispatch blindly. A timeout does not prove the prompt was never delivered.

## Tuning

Profiles and dimensions live in a policy the user edits in a web UI (`herdr-router ui`, or the Herdr action `herdr-router.open-editor`). If the user disagrees with a routing choice, point them there rather than overriding silently.
