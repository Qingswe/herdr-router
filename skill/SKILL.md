---
name: herdr-route
description: Delegates tasks to another coding agent through Herdr, selecting the agent, model, and effort with herdr-router. Use when the user asks to hand off, delegate, parallelize, or let another agent do work inside Herdr. Supports Claude Code, Codex, Kimi, Cursor, Droid, and OpenCode. If the user names an agent, model, or effort, honor it with --kind, --profile, --model, or --effort. Requires a local Herdr session and herdr-router on PATH.
---

# herdr-route

`herdr-router` picks the agent and launches it. This skill includes the follow-up commands; no separate `herdr` skill is required. Dispatch only when the user requests delegation or parallel work.

## Check

```bash
{ test "${HERDR_ENV:-}" = 1 || test -n "${HERDR_SOCKET_PATH:-}"; } && command -v herdr-router
```

If this fails, stop and explain which prerequisite is missing. Run `herdr-router doctor` to inspect the local installation. Use `herdr-router setup` to repair missing integrations when the user asks for setup; start a new agent session afterward to load newly installed skills. Dispatch must run inside Herdr.

## Preview (optional, nothing launched)

```bash
herdr-router route "<task>"            # per-dimension distributions, winner, top-3 ranking with penalties
herdr-router route "<task>" --json     # full ranking
```

## Dispatch

```bash
herdr-router dispatch "<task>" --json
```

This splits the calling pane (`--no-focus`), starts the chosen agent with its model and effort flags, submits the task once, and confirms Herdr observed `working` or `blocked`. The JSON reply carries `agent` (a live Herdr agent name), `pane_id`, `dispatch_id`, `stage`, `prompt_status`, and the full `decision`. `confirmed` means activity was observed; it does not prove the task succeeded. Use the returned agent name to wait and inspect the result:

```bash
herdr agent wait <agent> --timeout 600000
herdr agent read <agent> --source recent-unwrapped --lines 120
```

Inspect the actual output and relevant files before reporting completion. If the agent is blocked, read its pane and report what needs user input. Keep the pane available; do not close it, resend the task, or answer trust/update dialogs automatically.

Write the task as a complete, self-contained instruction: the receiving agent has none of your context.

## Pinning

User preference always wins. Pass what they specified and let the router fill the rest:

- `--profile <id>` pins a profile; `--kind codex` takes the best-ranked profile of that agent kind
- `--model opus` / `--effort high` (low|medium|high|xhigh|max) patch whichever profile wins
- Or inline tokens in the task: `@opus`, `@codex`, `@sol`, `@haiku`, `@glm`, `@mimo`, `@deepseek`, `@<profile-id>`, `!deep`, `!quick`, `!long`, `!<dimension>=<value>`
- `--name <agent-name>` to choose the Herdr agent name; `--pane <id>` to split a different pane; `--cwd <dir>`.

## Errors

Errors are JSON on stderr with exit 1.
- `agent_not_ready`: the agent is blocked at startup (for example a trust dialog). Inspect it with `herdr agent read` and ask the user before answering.
- `no_agent_available`: no enabled profile has its CLI installed (or the pinned one is missing). Report it; do not guess another agent.
- `not_in_herdr`: you are not inside a Herdr pane.

On success stdout contains only one JSON object; on failure stdout is empty. Failure JSON includes known `agent`, `pane_id`, `dispatch_id`, `stage`, and a recovery message. An error before dispatch begins has no dispatch id.

Always inspect `prompt_status` before deciding what to do:
- `not_sent`: fix the error and inspect any retained pane before trying to send work.
- `unknown`: the task may already be running. Read the named agent/pane; do not resubmit automatically.
- `confirmed`: activity was observed, including when a later `--wait` times out. Read the existing agent; do not resubmit.

`--wait` waits for a settled state after delivery confirmation; `--timeout <ms>` limits that extra wait. A blocked or settled state is not proof of task completion. The router never automatically resends a prompt or closes a pane it created after failure. A split communication error may have created a pane without returning its id: inspect Herdr before retrying that operation.

## Sessions

Each real dispatch saves a Session; `session_id` equals `dispatch_id`. Inspect it without contacting Herdr:

```bash
herdr-router session list --json
herdr-router session show <session-id> --json
herdr-router dispatch "<next task>" --session <session-id> --handoff handoff.md --phase review --json
```

Before continuing, inspect the prior result and confirm its work has finished. Continuation requires confirmed parent delivery and a handoff file; it creates a new pane and attempt linked by `parent_id`, inheriting the parent's cwd. An explicitly different cwd is rejected. The previous conversation is not automatically available. Write decisions, relevant file paths, completed checks and remaining work in an ordinary UTF-8 handoff file (up to 48 KiB). Only the new task is routed; the copied handoff is added afterward. Final prompts are limited to 64 KiB. `--dry-run` creates no Session or pane.

`in_progress` means no terminal state was recorded, including after a crash; it is not evidence of a live agent. A `session_warning` means the final storage update failed, while `prompt_status` still describes actual delivery. Inspect the original pane/log and never retry automatically.

## Optional isolation

New tasks use `shared` by default without Git scans. Choose a worktree only when independent edits justify checkout/setup time:

```bash
herdr-router dispatch "<task>" --isolation worktree --dry-run --json
herdr-router dispatch "<task>" --isolation worktree --worktree-timeout 180000 --json
```

Creation needs committed HEAD and a clean tracked/untracked source; sparse checkout/submodules are unsupported. The budget covers Git inspection/checkout, excluding assessment (default 60000 ms). Normal checkout hooks/filters can add time or network access; ignored local assets and dependencies are not copied. Dry-run creates nothing, and creation times are measured rather than guessed.

Sessions inherit isolation: worktree continuations reuse the same path/branch, accepting prior edits and commits. Do not switch isolation mid-session; use a fresh task with handoff when needed. On failure inspect `workspace.path`, `branch`, `state` and the original Session. The router retains known artifacts and stops owned Git processes on timeout; it never retries, falls back to shared, merges, pushes or deletes automatically.

## Tuning

Profiles and dimensions live in a policy the user edits in a web UI (`herdr-router ui`, or the Herdr action `herdr-router.open-editor`). If the user disagrees with a routing choice, point them there rather than overriding silently.
