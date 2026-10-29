"""按 Agent 隔离的知识库 — Agent 自我成长的持久记忆。

每个 Agent 一个 JSONL 文件 (knowledge/<agent>.jsonl), 条目即"经验":
  symptom (问题现象) / root_cause / resolution (解决方案) / tags / run_id /
  source (agent 自动沉淀 or human 标注) / confidence (被复用验证次数)

成长闭环扩展 (SPEC-agent-growth-loop §2):
  decision_id (绑定的决策) / review_status (unreviewed|approved|rejected) /
  weight (human_correction 默认 5, agent 默认 1; 排序键 (weight, reuse_count, ts)) /
  insight_id (由 insight confirm 自动生成的条目关联来源)
  query() 在 keyword 非空且零命中时追加 miss 日志 (知识盲区检测器的数据源)。

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
    def __init__(self, directory: str = DEFAULT_KNOWLEDGE_DIR,
                 miss_log: Optional[str] = None) -> None:
        self.directory = directory
        self.miss_log = miss_log  # 零命中查询日志路径 (None 关闭)
        os.makedirs(directory, exist_ok=True)
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._miss_lock = threading.Lock()

    def _lock_for(self, agent: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(agent, threading.Lock())

    def _path(self, agent: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", agent)
        return os.path.join(self.directory, "%s.jsonl" % safe)

    # ------------------------------------------------------------ 写入

    def add(self, agent: str, symptom: str, resolution: str,
            root_cause: str = "", tags: Optional[List[str]] = None,
            run_id: Optional[str] = None, source: str = "agent",
            decision_id: Optional[str] = None,
            review_status: str = "unreviewed",
            weight: Optional[int] = None,
            insight_id: Optional[str] = None,
            agent_version: Optional[str] = None,
            profile: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        # 人类纠正的权重高于 Agent 自我沉淀, 排序时优先被复用
        if weight is None:
            weight = 5 if source == "human_correction" else 1
        entry = {
            "id": "kb-" + uuid.uuid4().hex[:10],
            "ts": round(time.time(), 3),
            "agent": agent,
            "agent_version": agent_version,  # 沉淀时的 Agent 版本 (按版本归因经验)
            "run_id": run_id,
            "symptom": symptom,
            "root_cause": root_cause,
            "resolution": resolution,
            "tags": tags or [],
            "profile": dict(profile) if profile else None,  # X-2: design 画像 (foundry/node/block_type)
            "source": source,
            "reuse_count": 0,
            "decision_id": decision_id,
            "review_status": review_status,
            "weight": weight,
            "insight_id": insight_id,
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

    @staticmethod
    def _profile_factor(entry: Dict[str, Any], profile: Dict[str, str]) -> float:
        """X-2: profile 匹配系数 = 1 + 命中维度数 (foundry/node/block_type
        等); 条目无 profile 或维度值不等不计匹配。"""
        entry_profile = entry.get("profile") or {}
        matched = sum(1 for k, v in profile.items()
                      if v is not None and entry_profile.get(k) == v)
        return 1.0 + matched

    def query(self, agent: str, keyword: str = "",
              tags: Optional[List[str]] = None, limit: int = 10,
              profile: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        """关键词 + tag 检索; 命中 reuse_count+1 (被复用的经验权重上升)。

        排序键 (weight, reuse_count, ts) 降序 — 人类纠正 (weight=5) 优先被复用;
        被 reject 的条目 (weight=0) 不再被推举, 但保留在文件中供审计。
        keyword 非空且零命中时追加 miss 日志 (知识盲区检测器的数据源)。
        X-2: 传入 profile (design 画像) 时按
        weight × (reuse_count+1) × profile匹配系数 加权 — 同画像经验优先,
        匹配维度越多系数越高; 不传 profile 时排序行为与旧版完全一致。
        """
        entries = self._load(agent)
        kw = keyword.lower().strip()
        tagset = {t.lower() for t in (tags or [])}
        hits = []
        for e in entries:
            if e.get("weight", 1) <= 0:  # rejected 条目不参与推举
                continue
            if tagset and not tagset & {t.lower() for t in e.get("tags", [])}:
                continue
            if kw:
                haystack = " ".join([e.get("symptom", ""), e.get("root_cause", ""),
                                     e.get("resolution", ""),
                                     " ".join(e.get("tags", []))]).lower()
                if kw not in haystack:
                    continue
            hits.append(e)
        # 权重高、复用次数高、时间新的优先; X-2: profile 加权 (同画像优先)
        if profile:
            hits.sort(key=lambda e: (e.get("weight", 1)
                                     * (e.get("reuse_count", 0) + 1)
                                     * self._profile_factor(e, profile),
                                     e.get("ts", 0)), reverse=True)
        else:
            hits.sort(key=lambda e: (e.get("weight", 1), e.get("reuse_count", 0),
                                     e.get("ts", 0)), reverse=True)
        hits = hits[:limit]
        if hits:  # 记录复用 (rewrite file, 条目量级小可接受)
            hit_ids = {h["id"] for h in hits}
            with self._lock_for(agent):
                updated = []
                for e in self._load(agent):
                    if e["id"] in hit_ids:
                        e["reuse_count"] = e.get("reuse_count", 0) + 1
                    updated.append(e)
                self._rewrite(agent, updated)
                # 返回递增后的快照, 让调用者看到真实权重
                by_id = {e["id"]: e for e in updated}
                hits = [by_id[h["id"]] for h in hits]
        elif kw:
            self._log_miss(agent, keyword, tags)
        return hits

    def _log_miss(self, agent: str, keyword: str,
                  tags: Optional[List[str]]) -> None:
        """KB miss 日志: keyword 非空且零命中时追加一行 (检测器 3 的数据源)。"""
        if not self.miss_log:
            return
        entry = {"ts": round(time.time(), 3), "agent": agent,
                 "keyword": keyword, "tags": tags or []}
        try:
            directory = os.path.dirname(os.path.abspath(self.miss_log))
            os.makedirs(directory, exist_ok=True)
            with self._miss_lock:
                with open(self.miss_log, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # miss 日志失败不影响查询主流程

    def list(self, agent: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self._load(agent)[-limit:]

    # ------------------------------------------------------------ 复核 (成长闭环 §2)

    def _agents_on_disk(self) -> List[str]:
        if not os.path.isdir(self.directory):
            return []
        return sorted(f[:-6] for f in os.listdir(self.directory) if f.endswith(".jsonl"))

    def _rewrite(self, agent: str, entries: List[Dict[str, Any]]) -> None:
        with open(self._path(agent), "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")

    def review(self, entry_id: str, action: str, note: str = "") -> Dict[str, Any]:
        """人类复核知识条目: approve -> approved; reject -> rejected 且 weight=0
        (不再被排序推举, 但保留记录供审计)。读-改-写更新。"""
        if action not in ("approve", "reject"):
            raise ValueError("action must be approve|reject, got '%s'" % action)
        for agent in self._agents_on_disk():
            with self._lock_for(agent):
                entries = self._load(agent)
                for e in entries:
                    if e.get("id") == entry_id:
                        e["review_status"] = "approved" if action == "approve" else "rejected"
                        if action == "reject":
                            e["weight"] = 0
                        e["review_note"] = note
                        self._rewrite(agent, entries)
                        return e
        raise KeyError("unknown knowledge entry: '%s'" % entry_id)

    def unreviewed(self, agent: Optional[str] = None,
                   limit: int = 50) -> List[Dict[str, Any]]:
        """待复核条目 (复核队列数据源): 未复核的 Agent 自我沉淀 (source=agent)
        与人类纠正 (source=human_correction — 纠正也需第二人复核确认,
        见 SPEC-dashboard §9 验收闭环: 反馈 -> 复核队列 -> 批准)。"""
        agents = [agent] if agent else self._agents_on_disk()
        out: List[Dict[str, Any]] = []
        for name in agents:
            for e in self._load(name):
                if (e.get("source") in ("agent", "human_correction")
                        and e.get("review_status", "unreviewed") == "unreviewed"):
                    out.append(e)
        out.sort(key=lambda e: e.get("ts", 0))
        return out[-limit:]

    def stats(self) -> Dict[str, int]:
        out = {}
        if os.path.isdir(self.directory):
            for fname in sorted(os.listdir(self.directory)):
                if fname.endswith(".jsonl"):
                    agent = fname[:-6]
                    out[agent] = len(self._load(agent))
        return out
