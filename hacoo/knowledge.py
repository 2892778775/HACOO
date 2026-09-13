"""按 Agent 隔离的知识库 — Agent 自我成长的持久记忆。

每个 Agent 一个 JSONL 文件 (knowledge/<agent>.jsonl), 条目即"经验":
  symptom (问题现象) / root_cause / resolution (解决方案) / tags / run_id /
  source (agent 自动沉淀 or human 标注) / confidence (被复用验证次数)

决策支持: Agent 在做出非平凡决策前查询 KB (见生成的 OpenCode subagent
prompt 中的 Knowledge loop); 解决新问题后沉淀新条目。
存储为 append-only JSONL — 简单、可 git 追踪、可人工审阅。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

DEFAULT_KNOWLEDGE_DIR = "knowledge"


class KnowledgeStore:
    def __init__(self, directory: str = DEFAULT_KNOWLEDGE_DIR) -> None:
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, agent: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(agent, threading.Lock())

    def _path(self, agent: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", agent)
        return os.path.join(self.directory, "%s.jsonl" % safe)

    # ------------------------------------------------------------ 写入

    def add(self, agent: str, symptom: str, resolution: str,
            root_cause: str = "", tags: Optional[List[str]] = None,
            run_id: Optional[str] = None, source: str = "agent") -> Dict[str, Any]:
        entry = {
            "id": "kb-" + uuid.uuid4().hex[:10],
            "ts": round(time.time(), 3),
            "agent": agent,
            "run_id": run_id,
            "symptom": symptom,
            "root_cause": root_cause,
            "resolution": resolution,
            "tags": tags or [],
            "source": source,
            "reuse_count": 0,
        }
        with self._lock_for(agent):
            with open(self._path(agent), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    # ------------------------------------------------------------ 查询

    def _load(self, agent: str) -> List[Dict[str, Any]]:
        path = self._path(agent)
        if not os.path.isfile(path):
            return []
        entries = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue
        return entries

    def query(self, agent: str, keyword: str = "",
              tags: Optional[List[str]] = None, limit: int = 10) -> List[Dict[str, Any]]:
        """关键词 + tag 检索; 命中 reuse_count+1 (被复用的经验权重上升)。"""
        entries = self._load(agent)
        kw = keyword.lower().strip()
        tagset = {t.lower() for t in (tags or [])}
        hits = []
        for e in entries:
            if tagset and not tagset & {t.lower() for t in e.get("tags", [])}:
                continue
            if kw:
                haystack = " ".join([e.get("symptom", ""), e.get("root_cause", ""),
                                     e.get("resolution", ""),
                                     " ".join(e.get("tags", []))]).lower()
                if kw not in haystack:
                    continue
            hits.append(e)
        # 复用次数高、时间新的优先
        hits.sort(key=lambda e: (e.get("reuse_count", 0), e.get("ts", 0)), reverse=True)
        hits = hits[:limit]
        if hits:  # 记录复用 (rewrite file, 条目量级小可接受)
            hit_ids = {h["id"] for h in hits}
            with self._lock_for(agent):
                updated = []
                for e in self._load(agent):
                    if e["id"] in hit_ids:
                        e["reuse_count"] = e.get("reuse_count", 0) + 1
                    updated.append(e)
                with open(self._path(agent), "w", encoding="utf-8") as fh:
                    for e in updated:
                        fh.write(json.dumps(e, ensure_ascii=False) + "\n")
                # 返回递增后的快照, 让调用者看到真实权重
                by_id = {e["id"]: e for e in updated}
                hits = [by_id[h["id"]] for h in hits]
        return hits

    def list(self, agent: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self._load(agent)[-limit:]

    def stats(self) -> Dict[str, int]:
        out = {}
        if os.path.isdir(self.directory):
            for fname in sorted(os.listdir(self.directory)):
                if fname.endswith(".jsonl"):
                    agent = fname[:-6]
                    out[agent] = len(self._load(agent))
        return out
