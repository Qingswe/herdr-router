"""Turn a Decision into a running agent: split a pane, start the agent, send the task."""

from __future__ import annotations

from dataclasses import dataclass
import re
import uuid

from . import agents, herdr, log, sessions
from .score import Decision


@dataclass
class Dispatched:
    agent: str
    pane_id: str
    prompt_result: dict | None
    dispatch_id: str
    stage: str
    prompt_status: str
    session_id: str
    session_warning: str | None = None


_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_CONFIRM_STATES = ("working", "blocked")
_CONFIRM_TIMEOUT_MS = 15000
# Only the explicit pre-write rejection is certain. agent_not_found can also
# happen during the post-write activity wait if the agent exits quickly.
_NOT_SENT_ERRORS = {"agent_blocked"}


def dispatch(decision: Decision, *, target_pane: str | None, cwd: str,
             name: str | None = None, direction: str = "auto",
             start_timeout_ms: int = 60000, wait: bool = False,
             wait_timeout_ms: int | None = None, parent_id: str | None = None,
             phase: str = "task", task: str | None = None,
             handoff: dict | None = None) -> Dispatched:
    dispatch_id = str(uuid.uuid4())
    stage, prompt_status, status = "prepare", "not_sent", "failed"
    pane_id, agent = None, name
    error = "unexpected_error"
    session_id, result, failure = None, None, None
    session_warning = None
    record = {"event": "dispatch", "cwd": cwd, "decision": None,
              "parent_id": parent_id, "phase": phase}

    def checkpoint(*, delivery=None, required=True):
        nonlocal session_warning
        try:
            sessions.update(dispatch_id, stage=stage, status="in_progress",
                            prompt_status=prompt_status if delivery is None else delivery,
                            agent=agent, pane_id=pane_id)
        except sessions.SessionError as e:
            code = e.code if e.code == "session_store_busy" else "session_store_error"
            message = "could not persist dispatch checkpoint"
            if required:
                raise herdr.HerdrError(code, message) from None
            session_warning = session_warning or f"{code}: {message}"

    try:
        record["decision"] = decision.to_dict()
        if "\0" in decision.prompt:
            raise herdr.HerdrError("invalid_prompt", "prompt must not contain NUL")
        try:
            prompt_bytes = len(decision.prompt.encode("utf-8"))
        except UnicodeError:
            # This snapshot cannot be written to UTF-8 JSONL either. Keep one
            # safe terminal record without letting logging mask invalid_prompt.
            record["decision"] = None
            raise herdr.HerdrError("invalid_prompt", "prompt must contain valid UTF-8 text") from None
        try:
            sessions.create(dispatch_id, decision=record["decision"], cwd=cwd,
                            parent_id=parent_id, phase=phase, task=task, handoff=handoff)
        except sessions.SessionError as e:
            raise herdr.HerdrError(e.code, e.message) from None
        session_id = dispatch_id
        if prompt_bytes > 64 * 1024:
            raise herdr.HerdrError("prompt_too_large", "prompt exceeds 64 KiB of UTF-8 text")
        try:
            agents.prepare(decision.kind, decision.model, decision.effort)
        except OSError:
            raise herdr.HerdrError("prepare_failed", "could not prepare agent launch files") from None

        stage = "resolve"
        checkpoint()
        if agent is None:
            base = (decision.model or decision.kind).split("/")[-1][:16]
            agent = herdr.unique_agent_name(f"r-{base}-{dispatch_id[:8]}")
        if not _NAME_RE.fullmatch(agent):
            raise herdr.HerdrError("invalid_agent_name", "agent name must match [a-z][a-z0-9_-]{0,31}")
        checkpoint()
        target = target_pane or herdr.current_pane_id()
        if direction == "auto":
            direction = herdr.auto_direction(target)

        stage = "split"
        checkpoint()
        pane_id = herdr.split(target, direction, cwd)
        stage = "start"
        checkpoint()
        herdr.agent_start(agent, decision.kind, pane_id, decision.args, start_timeout_ms)

        stage = "prompt"
        # Persist uncertainty before the write. If this fails, no prompt is sent
        # and the in-memory outcome remains not_sent.
        checkpoint(delivery="unknown")
        prompt_status = "unknown"
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
        # No further send occurs. Try to persist confirmation, but a storage
        # failure must not change the delivery fact or skip the optional wait.
        stage = "wait" if wait else "prompt"
        checkpoint(required=False)
        if wait:
            # Herdr's default wait observes idle/done/blocked; "settled" is not a CLI state.
            prompt_result = herdr.agent_wait(agent, timeout_ms=wait_timeout_ms)
        stage, status, error = "complete", "confirmed", None
        result = Dispatched(agent=agent, pane_id=pane_id, prompt_result=prompt_result,
                            dispatch_id=dispatch_id, stage=stage, prompt_status=prompt_status,
                            session_id=session_id, session_warning=session_warning)
        return result
    except BaseException as e:
        failure = e
        if isinstance(e, herdr.HerdrError):
            error = e.code
            pane_id = pane_id if pane_id is not None else e.pane_id
            agent = agent if agent is not None else e.agent
        elif isinstance(e, (KeyboardInterrupt, SystemExit)):
            error = "interrupted"
        e.pane_id, e.agent = pane_id, agent
        e.dispatch_id, e.stage, e.prompt_status = dispatch_id, stage, prompt_status
        e.session_id, e.session_warning = session_id, session_warning
        raise
    finally:
        if session_id is not None:
            try:
                sessions.update(session_id, stage=stage, status=status, prompt_status=prompt_status,
                                agent=agent, pane_id=pane_id, error=error)
            except Exception as e:
                code = ("session_store_busy" if isinstance(e, sessions.SessionError)
                        and e.code == "session_store_busy" else "session_store_error")
                session_warning = session_warning or f"{code}: could not persist terminal dispatch snapshot"
            record["session_id"] = session_id
        if result is not None:
            result.session_warning = session_warning
        if failure is not None:
            failure.session_warning = session_warning
        if session_warning is not None:
            record["session_warning"] = session_warning
        record.update(agent=agent, pane_id=pane_id, dispatch_id=dispatch_id,
                      stage=stage, prompt_status=prompt_status, status=status)
        if error is not None:
            record["error"] = error
        try:
            log.append(record)
        except (OSError, UnicodeError):
            pass  # logging is best effort, including during an interrupted RPC
