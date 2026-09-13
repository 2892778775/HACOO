"""审计日志: 每次网关调用落一条 JSONL (request_id 全链路可追溯)。

字段: ts / request_id / user / role / method / instance_id / status /
      error_code / elapsed_ms / client

路径由 Config.audit_log 指定; "off" 关闭。文件按行追加, 线程安全。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Optional


class AuditLog:
    def __init__(self, path: Optional[str]) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._fh = None
        if path:
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")

    def record(self, *, request_id: str, user: str, role: str, method: str,
               instance_id: Optional[str], status: str,
               error_code: Optional[str], elapsed_ms: float,
               client: str = "", agent: str = "", run_id: str = "") -> None:
        if not self._fh:
            return
        entry: Dict[str, Any] = {
            "ts": round(time.time(), 3),
            "request_id": request_id,
            "user": user,
            "role": role,
            "agent": agent,
            "run_id": run_id,
            "method": method,
            "instance_id": instance_id,
            "status": status,
            "error_code": error_code,
            "elapsed_ms": round(elapsed_ms, 2),
            "client": client,
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            try:
                self._fh.write(line + "\n")
                self._fh.flush()
            except OSError:
                pass  # 审计失败不阻断业务

    def query(self, run_id: str) -> list:
        """按 run_id 检索审计条目 (flow trace 用)。"""
        if not self.path or not os.path.isfile(self.path):
            return []
        out = []
        with self._lock:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if entry.get("run_id") == run_id:
                        out.append(entry)
        return out

    def close(self) -> None:
        with self._lock:
            if self._fh:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None
