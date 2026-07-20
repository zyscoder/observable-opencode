from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .models import JsonDict, NodeJudgment, TraceNode, judgment_from_dict, stable_json


JUDGMENT_CACHE_VERSION = "1.0"


def build_judge_cache_key(
    *,
    stage: str,
    model: str,
    system: str,
    messages: List[Dict[str, str]],
    max_tokens: int,
    thinking_config: Optional[Dict[str, Any]],
    prompt_schema_version: str,
) -> str:
    payload = {
        "cache_version": JUDGMENT_CACHE_VERSION,
        "stage": stage,
        "model": model,
        "system": system,
        "messages": messages,
        "max_tokens": max_tokens,
        "thinking_config": thinking_config,
        "prompt_schema_version": prompt_schema_version,
    }
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


class JudgmentCache:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else None
        self._entries: Dict[str, JsonDict] = {}
        self._hits = 0
        self._misses = 0
        self._writes = 0
        self._invalid_entries = 0
        self._corrupt_entries = 0
        self._write_errors: List[str] = []
        self._load()

    def get(self, *, key: str, node: TraceNode) -> Optional[NodeJudgment]:
        payload = self.get_payload(key=key)
        if payload is None:
            return None
        return judgment_from_dict(payload, node)

    def get_payload(self, *, key: str) -> Optional[JsonDict]:
        payload = self.peek_payload(key=key)
        if payload is None:
            self._misses += 1
            return None
        self._hits += 1
        return payload

    def peek_payload(self, *, key: str) -> Optional[JsonDict]:
        entry = self._entries.get(key)
        if not entry:
            return None
        payload = entry.get("payload")
        if payload is None:
            payload = entry.get("judgment")
        if not isinstance(payload, dict):
            return None
        return dict(payload)

    def get_validated_payload(
        self, *, key: str, validator: Callable[[JsonDict], Any]
    ) -> Optional[JsonDict]:
        entry = self._entries.get(key)
        if not entry:
            self._misses += 1
            return None
        payload = entry.get("payload")
        if payload is None:
            payload = entry.get("judgment")
        if not isinstance(payload, dict):
            self._misses += 1
            self._invalid_entries += 1
            return None
        payload = dict(payload)
        try:
            validator(payload)
        except (TypeError, ValueError):
            self._misses += 1
            self._invalid_entries += 1
            return None
        self._hits += 1
        return payload

    def put(
        self,
        *,
        key: str,
        stage: str,
        model: str,
        node: TraceNode,
        judgment: NodeJudgment,
    ) -> None:
        self.put_payload(
            key=key,
            stage=stage,
            model=model,
            node_ref=node.ref,
            payload=asdict(judgment),
        )

    def put_payload(
        self,
        *,
        key: str,
        stage: str,
        model: str,
        node_ref: str,
        payload: JsonDict,
    ) -> None:
        if not self.path:
            return
        entry = self._entry(
            key=key,
            stage=stage,
            model=model,
            node_ref=node_ref,
            payload=payload,
        )
        self._append_and_fsync(entry)

    def _entry(
        self,
        *,
        key: str,
        stage: str,
        model: str,
        node_ref: str,
        payload: JsonDict,
    ) -> JsonDict:
        return {
            "cache_version": JUDGMENT_CACHE_VERSION,
            "key": key,
            "stage": stage,
            "node_ref": node_ref,
            "model": model,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "payload": dict(payload),
        }

    def _append_and_fsync(self, entry: JsonDict) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            self._write_errors.append(f"{type(exc).__name__}: {exc}")
            return
        self._entries[str(entry["key"])] = entry
        self._writes += 1

    def stats(self) -> JsonDict:
        return {
            "enabled": self.path is not None,
            "path": str(self.path) if self.path else "",
            "loaded_entries": len(self._entries),
            "hits": self._hits,
            "misses": self._misses,
            "writes": self._writes,
            "invalid_entries": self._invalid_entries,
            "corrupt_entries": self._corrupt_entries,
            "write_error_count": len(self._write_errors),
            "write_errors": list(self._write_errors),
        }

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        entry = json.loads(text)
                    except json.JSONDecodeError:
                        self._corrupt_entries += 1
                        continue
                    payload = entry.get("payload") if isinstance(entry, dict) else None
                    if payload is None and isinstance(entry, dict):
                        payload = entry.get("judgment")
                    if not isinstance(entry, dict) or not entry.get("key") or not isinstance(payload, dict):
                        self._corrupt_entries += 1
                        continue
                    self._entries[str(entry["key"])] = entry
        except OSError as exc:
            self._write_errors.append(f"load {type(exc).__name__}: {exc}")
