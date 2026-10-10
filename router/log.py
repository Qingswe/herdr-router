"""Append-only JSONL decision log, shared by the CLI and the plugin."""

from __future__ import annotations

import json
import time

from .model import state_dir


def append(record: dict) -> None:
    try:
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True)
        record = {"ts": int(time.time() * 1000), **record}
        with (d / "decisions.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # logging must never break a dispatch
