"""升级提案管理 — 反馈汇聚为可追踪的升级 (SPEC-evolution Part A)。

用户反馈 / insight / 复核结论汇聚成**带验收标准的升级提案**, 升级落地为
带版本号的 Agent/Skill 变更, 从而可回答: "这个 Agent 相比上个月变强了吗?
哪次变更让它变强的?"

存储: logs/upgrade-proposals.jsonl, append-only + 读-改-写更新状态
(与 insights/decisions 同一模式, 单服务进程 + 锁, 无外部 DB)。

状态机:
    proposed -> accepted | rejected
    accepted -> in_progress | rejected
    in_progress -> done | accepted (回退)
    done / rejected 为终态
accept 前强制检查 acceptance 非空 (无验收标准的提案不允许立项)。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

DEFAULT_UPGRADES_LOG = os.path.join("logs", "upgrade-proposals.jsonl")

KINDS = ("agent_capability", "skill_new", "skill_update",
         "knowledge_bulk", "adapter_method", "platform_feature")
SOURCES = ("human", "insight", "review")
STATUSES = ("proposed", "accepted", "in_progress", "done", "rejected")

# 合法流转表: done / rejected 为终态
TRANSITIONS: Dict[str, tuple] = {
    "proposed": ("accepted", "rejected"),
    "accepted": ("in_progress", "rejected"),
    "in_progress": ("done", "accepted"),  # accepted 为回退 (实现中发现需要重新立项)
    "done": (),
    "rejected": (),
}


class UpgradeStore:
    """升级提案存储: append-only JSONL, 状态读-改-写更新 (进程内锁)。"""

    def __init__(self, path: str = DEFAULT_UPGRADES_LOG) -> None:
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 读写基础

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

    # ------------------------------------------------------------ 提案

    def propose(self, title: str, kind: str, target: str, motivation: str,
                acceptance: str = "", proposed_change: str = "",
                source: str = "human",
                source_ref: Optional[str] = None) -> Dict[str, Any]:
        """创建升级提案 (status=proposed)。

        acceptance 允许暂空, 但空 acceptance 的提案不允许 accept
        (见 transition) — 验收标准是立项的前置条件。
        """
        if not title or not title.strip():
            raise ValueError("title must be non-empty")
        if kind not in KINDS:
            raise ValueError("kind must be one of %s, got '%s'"
                             % ("|".join(KINDS), kind))
        if not target or not target.strip():
            raise ValueError("target must be non-empty")
        if not motivation or not motivation.strip():
            raise ValueError("motivation must be non-empty")
        if source not in SOURCES:
            raise ValueError("source must be one of %s, got '%s'"
                             % ("|".join(SOURCES), source))
        entry = {
            "proposal_id": "up-" + uuid.uuid4().hex[:10],
            "ts": round(time.time(), 3),
            "title": title,
            "kind": kind,
            "target": target,
            "motivation": motivation,
            "source": source,
            "source_ref": source_ref,
            "proposed_change": proposed_change,
            "acceptance": acceptance,
            "status": "proposed",
            "version_after": None,
            "history": [],
        }
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def transition(self, proposal_id: str, to_status: str, note: str = "",
                   version_after: Optional[str] = None) -> Dict[str, Any]:
        """状态流转 (读-改-写)。非法流转 / 未知提案 / accept 无验收标准均报错。"""
        if to_status not in STATUSES:
            raise ValueError("to_status must be one of %s, got '%s'"
                             % ("|".join(STATUSES), to_status))
        with self._lock:
            entries = self._load_all()
            for e in entries:
                if e.get("proposal_id") != proposal_id:
                    continue
                current = e.get("status", "proposed")
                if to_status not in TRANSITIONS.get(current, ()):
                    raise ValueError(
                        "illegal transition: %s -> %s (allowed: %s)"
                        % (current, to_status,
                           "|".join(TRANSITIONS.get(current, ())) or "<none, terminal>"))
                if to_status == "accepted" and not (e.get("acceptance") or "").strip():
                    raise ValueError(
                        "cannot accept proposal %s: acceptance criteria is empty; "
                        "fill acceptance first" % proposal_id)
                e["status"] = to_status
                if version_after:
                    e["version_after"] = version_after
                e.setdefault("history", []).append({
                    "ts": round(time.time(), 3),
                    "from": current, "to": to_status, "note": note,
                })
                self._rewrite(entries)
                return e
        raise KeyError("unknown proposal_id: '%s'" % proposal_id)

    def query(self, status: Optional[str] = None, target: Optional[str] = None,
              limit: int = 100) -> List[Dict[str, Any]]:
        """查询提案, 默认按 ts 倒序 (最新在前)。"""
        entries = self._load_all()
        if status:
            entries = [e for e in entries if e.get("status") == status]
        if target:
            entries = [e for e in entries if e.get("target") == target]
        entries.sort(key=lambda e: e.get("ts", 0), reverse=True)
        return entries[:limit]
