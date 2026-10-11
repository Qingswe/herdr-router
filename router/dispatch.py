"""Turn a Decision into a running agent: split a pane, start the agent, send the task."""

from __future__ import annotations

from dataclasses import dataclass
import re
import uuid

from . import agents, herdr, log
from .score import Decision


@dataclass
class Dispatched:
    agent: str
    pane_id: str
    prompt_result: dict | None
    dispatch_id: str
    stage: str
    prompt_status: str


_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_CONFIRM_STATES = ("working", "blocked")
_CONFIRM_TIMEOUT_MS = 15000
# Only the explicit pre-write rejection is certain. agent_not_found can also
# happen during the post-write activity wait if the agent exits quickly.
_NOT_SENT_ERRORS = {"agent_blocked"}


def dispatch(decision: Decision, *, target_pane: str | None, cwd: str,
             name: str | None = None, direction: str = "auto",
             start_timeout_ms: int = 60000, wait: bool = False,
             wait_timeout_ms: int | None = None) -> Dispatched:
    dispatch_id = str(uuid.uuid4())
    stage, prompt_status, status = "prepare", "not_sent", "failed"
    pane_id, agent = None, name
    error = "unexpected_error"
    record = {"event": "dispatch", "cwd": cwd, "decision": None}
    try:
        record["decision"] = decision.to_dict()
        try:
            agents.prepare(decision.kind, decision.model, decision.effort)
        except OSError:
            raise herdr.HerdrError("prepare_failed", "could not prepare agent launch files") from None

        stage = "resolve"
        if agent is None:
            base = (decision.model or decision.kind).split("/")[-1][:16]
            agent = herdr.unique_agent_name(f"r-{base}-{dispatch_id[:8]}")
        if not _NAME_RE.fullmatch(agent):
            raise herdr.HerdrError("invalid_agent_name", "agent name must match [a-z][a-z0-9_-]{0,31}")
        target = target_pane or herdr.current_pane_id()
        if direction == "auto":
            direction = herdr.auto_direction(target)

        stage = "split"
        pane_id = herdr.split(target, direction, cwd)
        stage = "start"
        herdr.agent_start(agent, decision.kind, pane_id, decision.args, start_timeout_ms)

        stage, prompt_status = "prompt", "unknown"
        try:
            prompt_result = herdr.agent_prompt(agent, decision.prompt, wait=True,
                                               until=_CONFIRM_STATES, timeout_ms=_CONFIRM_TIMEOUT_MS)
        except herdr.HerdrError as e:
            if e.code in _NOT_SENT_ERRORS or e.before_submission:
                prompt_status = "not_sent"
            if e.code != "agent_prompt_stalled":
                raise
            # The write may have succeeded. Observe once; never send the task again.
            prompt_result = herdr.agent_wait(agent, until=_CONFIRM_STATES,
                                             timeout_ms=_CONFIRM_TIMEOUT_MS)
        prompt_status = "confirmed"
        if wait:
            stage = "wait"
            # Herdr's default wait observes idle/done/blocked; "settled" is not a CLI state.
            prompt_result = herdr.agent_wait(agent, timeout_ms=wait_timeout_ms)
        stage, status, error = "complete", "confirmed", None
        return Dispatched(agent=agent, pane_id=pane_id, prompt_result=prompt_result,
                          dispatch_id=dispatch_id, stage=stage, prompt_status=prompt_status)
    except herdr.HerdrError as e:
        error = e.code
        pane_id = pane_id if pane_id is not None else e.pane_id
        agent = agent if agent is not None else e.agent
        e.pane_id, e.agent = pane_id, agent
        e.dispatch_id, e.stage, e.prompt_status = dispatch_id, stage, prompt_status
        raise
    except (KeyboardInterrupt, SystemExit):
        error = "interrupted"
        raise
    finally:
        record.update(agent=agent, pane_id=pane_id, dispatch_id=dispatch_id,
                      stage=stage, prompt_status=prompt_status, status=status)
        if error is not None:
            record["error"] = error
        try:
            log.append(record)
        except OSError:
            pass  # logging is best effort, including during an interrupted RPC
