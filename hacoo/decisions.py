"""决策日志 — 问题定位与问责 (accountability)。

每次 Agent 决策记录一条:
  decision_id / ts / agent / run_id / stage / action / rationale / params /
  status (pending|ok|failed|skipped) / metrics / note / finished_ts

问题定位路径: api_flow_trace(run_id) -> 该 run 的全部决策时间线
(谁、何时、依据什么、做了什么、结果如何) + 关联 audit (request_id)。

存储: append-only JSONL (logs/decisions.jsonl), outcome 更新采用
"读-改-写" (单服务进程 + 锁, 量级可接受; 生产可换 SQLite)。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional


class DecisionLog:
    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 写入

    def record(self, agent: str, run_id: Optional[str], stage: str,
               action: str, rationale: str,
               params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        entry = {
            "decision_id": "dec-" + uuid.uuid4().hex[:10],
            "ts": round(time.time(), 3),
            "agent": agent,
            "run_id": run_id,
            "stage": stage,
            "action": action,
            "rationale": rationale,
            "params": params or {},
            "status": "pending",
            "metrics": {},
            "note": "",
            "finished_ts": None,
        }
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def outcome(self, decision_id: str, status: str,
                metrics: Optional[Dict[str, Any]] = None, note: str = "") -> Dict[str, Any]:
        with self._lock:
            entries = self._load_all()
            for e in entries:
                if e["decision_id"] == decision_id:
                    e["status"] = status
                    e["metrics"] = metrics or {}
                    e["note"] = note
                    e["finished_ts"] = round(time.time(), 3)
                    self._rewrite(entries)
                    return e
        raise KeyError("unknown decision_id: '%s'" % decision_id)

    # ------------------------------------------------------------ 查询

    def _load_all(self) -> List[Dict[str, Any]]:
        if not os.path.isfile(self.path):
            return []
        entries = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue
        return entries

    def _rewrite(self, entries: List[Dict[str, Any]]) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")

    def query(self, agent: Optional[str] = None, run_id: Optional[str] = None,
              status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        entries = self._load_all()
        if agent:
            entries = [e for e in entries if e["agent"] == agent]
        if run_id:
            entries = [e for e in entries if e.get("run_id") == run_id]
        if status:
            entries = [e for e in entries if e["status"] == status]
        return entries[-limit:]
