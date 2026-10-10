"""Turn a Decision into a running agent: split a pane, start the agent, send the task."""

from __future__ import annotations

from dataclasses import dataclass

from . import agents, herdr, log
from .score import Decision


@dataclass
class Dispatched:
    agent: str
    pane_id: str
    prompt_result: dict | None


def dispatch(decision: Decision, *, target_pane: str | None, cwd: str,
             name: str | None = None, direction: str = "auto",
             start_timeout_ms: int = 60000, wait: bool = False,
             wait_timeout_ms: int | None = None) -> Dispatched:
    target = target_pane or herdr.current_pane_id()
    if direction == "auto":
        direction = herdr.auto_direction(target)
    pane_id = herdr.split(target, direction, cwd)
    agent = name or herdr.unique_agent_name(f"r-{(decision.model or decision.kind).split('/')[-1]}")

    record = {"event": "dispatch", "agent": agent, "pane_id": pane_id, "cwd": cwd,
              "decision": decision.to_dict()}
    try:
        agents.prepare(decision.kind, decision.model, decision.effort)
        herdr.agent_start(agent, decision.kind, pane_id, decision.args, start_timeout_ms)
    except herdr.HerdrError as e:
        # agent_not_ready = blocked at startup (e.g. a trust dialog). The pane
        # and name stay live; the caller must look before prompting.
        log.append({**record, "error": e.code})
        e.pane_id, e.agent = pane_id, agent
        raise
    prompt_result = herdr.agent_prompt(agent, decision.prompt, wait=wait, timeout_ms=wait_timeout_ms)
    log.append(record)
    return Dispatched(agent=agent, pane_id=pane_id, prompt_result=prompt_result)
