"""Dashboard 数据层 (SPEC-dashboard §4) — 纯函数, 不含任何 streamlit import。

每个 dd_* 函数: 输入 HacooClient (在线, 一切读写经 SDK 走网关) 或
offline_dir (离线只读, 直接读 JSONL/manifest 文件, 绝不写入),
返回 list[dict] 或 dict, 可脱离 streamlit 单测。

离线目录布局与 Config 默认值一致:
    <offline_dir>/hacoo.agents.json           Agent 启停/版本
    <offline_dir>/runs/<run_id>.json          flow run manifest
    <offline_dir>/knowledge/<agent>.jsonl     知识库
    <offline_dir>/observations/<agent>.jsonl  观测记录
    <offline_dir>/logs/decisions.jsonl        决策日志
    <offline_dir>/logs/insights.jsonl         insight
    <offline_dir>/logs/upgrade-proposals.jsonl 升级提案
    <offline_dir>/logs/hacoo-audit.jsonl      审计日志

离线分支与在线分支的差异 (只读语义):
  - 知识检索离线不递增 reuse_count、不记 KB miss 日志 (只读)
  - 其余过滤/排序规则与网关侧实现保持一致
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from hacoo import agents as agent_registry
from hacoo.upgrades import STATUSES as UPGRADE_STATUSES

_TODO_QUERY_LIMIT = 500  # 待办计数的上限 (超出显示为上限值, 量级足够)


# ------------------------------------------------------------ 基础工具

def _require_exactly_one(client: Any, offline_dir: Optional[str]) -> None:
    if (client is None) == (offline_dir is None):
        raise ValueError("pass exactly one of client= (online) or "
                         "offline_dir= (offline read-only)")


def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    """读 JSONL 文件, 容忍坏行; 文件不存在返回 []。"""
    if not path or not os.path.isfile(path):
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
    return entries


class _OfflineLayout:
    """离线目录下各数据文件的路径解析 (与 Config 默认布局一致)。"""

    def __init__(self, root: str) -> None:
        self.root = root
        self.agents_file = os.path.join(root, "hacoo.agents.json")
        self.runs_dir = os.path.join(root, "runs")
        self.knowledge_dir = os.path.join(root, "knowledge")
        self.observations_dir = os.path.join(root, "observations")
        self.decisions_log = os.path.join(root, "logs", "decisions.jsonl")
        self.insights_log = os.path.join(root, "logs", "insights.jsonl")
        self.upgrades_log = os.path.join(root, "logs",
                                         "upgrade-proposals.jsonl")
        self.audit_log = os.path.join(root, "logs", "hacoo-audit.jsonl")

    def knowledge_file(self, agent: str) -> str:
        safe = "".join(c if (c.isalnum() or c in "-_") else "_"
                       for c in agent)
        return os.path.join(self.knowledge_dir, "%s.jsonl" % safe)

    def observation_file(self, agent: str) -> str:
        safe = "".join(c if (c.isalnum() or c in "-_") else "_"
                       for c in agent)
        return os.path.join(self.observations_dir, "%s.jsonl" % safe)

    def agents_on_disk(self, kind: str) -> List[str]:
        """kind = "knowledge" | "observations": 列出磁盘上有数据的 agent。"""
        directory = {"knowledge": self.knowledge_dir,
                     "observations": self.observations_dir}[kind]
        if not os.path.isdir(directory):
            return []
        return sorted(f[:-6] for f in os.listdir(directory)
                      if f.endswith(".jsonl"))


# ------------------------------------------------------------ 离线读取 (只读)

def _offline_runs(layout: _OfflineLayout) -> List[Dict[str, Any]]:
    runs: List[Dict[str, Any]] = []
    if not os.path.isdir(layout.runs_dir):
        return runs
    for fname in os.listdir(layout.runs_dir):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(layout.runs_dir, fname), "r",
                      encoding="utf-8") as fh:
                runs.append(json.load(fh))
        except (OSError, ValueError):
            continue
    return runs


def _run_summary(run: Dict[str, Any]) -> Dict[str, Any]:
    """run manifest -> 摘要 (与 FlowEngine.list_runs 的字段一致)。"""
    started = run.get("started_at") or 0
    finished = run.get("finished_at")
    return {
        "run_id": run.get("run_id", ""),
        "template": run.get("template", ""),
        "status": run.get("status", "unknown"),
        "user": run.get("user", ""),
        "started_at": started,
        "finished_at": finished,
        "duration_s": (round(finished - started, 3)
                       if finished and started else None),
        "n_stages": len(run.get("stages") or []),
        "error": run.get("error"),
    }


def _offline_knowledge_query(layout: _OfflineLayout, agent: str,
                             keyword: str = "",
                             tags: Optional[List[str]] = None,
                             limit: int = 50) -> List[Dict[str, Any]]:
    """KnowledgeStore.query 的只读复刻: 同样的过滤与 (weight, reuse_count,
    ts) 降序排序, 但不递增 reuse_count、不写 KB miss 日志。"""
    entries = _read_jsonl(layout.knowledge_file(agent))
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
    hits.sort(key=lambda e: (e.get("weight", 1), e.get("reuse_count", 0),
                             e.get("ts", 0)), reverse=True)
    return hits[:limit]


def _offline_observations(layout: _OfflineLayout, agent: Optional[str] = None,
                          tool: Optional[str] = None,
                          run_id: Optional[str] = None,
                          limit: int = 200) -> List[Dict[str, Any]]:
    agents = [agent] if agent else layout.agents_on_disk("observations")
    out: List[Dict[str, Any]] = []
    for name in agents:
        for e in _read_jsonl(layout.observation_file(name)):
            if tool and e.get("tool") != tool:
                continue
            if run_id and e.get("run_id") != run_id:
                continue
            out.append(e)
    out.sort(key=lambda e: e.get("ts", 0))
    return out[-limit:]


# ------------------------------------------------------------ P01 总览

def dd_agents_status(client: Any = None,
                     offline_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """9 个 Agent 的 enabled/version/title/stage/tools 状态列表。"""
    _require_exactly_one(client, offline_dir)
    if offline_dir is not None:
        return agent_registry.list_agents(_OfflineLayout(offline_dir).agents_file)
    return client.list_agents()


def dd_recent_runs(client: Any = None, limit: int = 10,
                   status: Optional[str] = None,
                   offline_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """最近 flow run 摘要 (最新在前), 可按 status 过滤。"""
    _require_exactly_one(client, offline_dir)
    if offline_dir is not None:
        runs = [_run_summary(r) for r in _offline_runs(_OfflineLayout(offline_dir))]
        if status:
            runs = [r for r in runs if r["status"] == status]
        runs.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
        return runs[:limit]
    return client.runs_list(limit=limit, status=status)


def dd_todo_counts(client: Any = None,
                   offline_dir: Optional[str] = None) -> Dict[str, int]:
    """待办计数: {pending_decisions, new_insights, proposed_upgrades}。"""
    _require_exactly_one(client, offline_dir)
    if offline_dir is not None:
        layout = _OfflineLayout(offline_dir)
        pending = sum(1 for d in _read_jsonl(layout.decisions_log)
                      if d.get("status") == "pending")
        new_insights = sum(1 for i in _read_jsonl(layout.insights_log)
                           if i.get("status") == "new")
        proposed = sum(1 for p in _read_jsonl(layout.upgrades_log)
                       if p.get("status") == "proposed")
        return {"pending_decisions": pending, "new_insights": new_insights,
                "proposed_upgrades": proposed}
    queue = client.review_queue(limit=_TODO_QUERY_LIMIT)
    insights = client.insights_list(status="new", limit=_TODO_QUERY_LIMIT)
    proposals = client.upgrade_list(status="proposed", limit=_TODO_QUERY_LIMIT)
    return {"pending_decisions": len(queue.get("decisions") or []),
            "new_insights": len(insights),
            "proposed_upgrades": len(proposals)}


# ------------------------------------------------------------ P02 Run 中心

def dd_run_trace(client: Any = None, run_id: str = "",
                 offline_dir: Optional[str] = None) -> Dict[str, Any]:
    """单个 run 的问责视图: {run, decisions, audit}。"""
    _require_exactly_one(client, offline_dir)
    if offline_dir is not None:
        layout = _OfflineLayout(offline_dir)
        run: Optional[Dict[str, Any]] = None
        for candidate in _offline_runs(layout):
            if candidate.get("run_id") == run_id:
                run = candidate
                break
        if run is None:
            raise KeyError("unknown run_id: '%s'" % run_id)
        decisions = [d for d in _read_jsonl(layout.decisions_log)
                     if d.get("run_id") == run_id]
        audit = [a for a in _read_jsonl(layout.audit_log)
                 if a.get("run_id") == run_id]
        return {"run": run, "decisions": decisions, "audit": audit}
    return client.flow_trace(run_id)


def dd_metric_trend(client: Any = None, agent: str = "", metric_key: str = "",
                    limit: int = 50,
                    offline_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """某 agent 历史观测中指定指标的取值序列: [{ts, value, run_id}]。

    同一 run 取最新一条观测 (与 insights 趋势检测器口径一致)。
    """
    _require_exactly_one(client, offline_dir)
    if offline_dir is not None:
        observations = _offline_observations(_OfflineLayout(offline_dir),
                                             agent=agent, limit=10000)
    else:
        observations = client.call(
            "api_observations_query",
            arguments={"agent": agent, "limit": 10000})["data"]
    per_run: Dict[str, Dict[str, Any]] = {}
    for obs in sorted(observations, key=lambda o: o.get("ts", 0)):
        value = (obs.get("metrics") or {}).get(metric_key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        per_run[obs.get("run_id") or obs.get("obs_id", "")] = {
            "ts": obs.get("ts", 0), "value": value,
            "run_id": obs.get("run_id") or ""}
    points = sorted(per_run.values(), key=lambda p: p["ts"])
    return points[-limit:]


# ------------------------------------------------------------ P04 决策复核

def dd_review_queue(client: Any = None, agent: Optional[str] = None,
                    offline_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """待复核队列: pending 决策 + unreviewed 知识条目 (agent 沉淀与人类纠正)。

    返回扁平列表, 每条带 item_type ("decision" | "knowledge") 便于 UI 分卡。
    """
    _require_exactly_one(client, offline_dir)
    if offline_dir is not None:
        layout = _OfflineLayout(offline_dir)
        decisions = [d for d in _read_jsonl(layout.decisions_log)
                     if d.get("status") == "pending"
                     and (not agent or d.get("agent") == agent)]
        knowledge: List[Dict[str, Any]] = []
        agents = [agent] if agent else layout.agents_on_disk("knowledge")
        for name in agents:
            for e in _read_jsonl(layout.knowledge_file(name)):
                if (e.get("source") in ("agent", "human_correction")
                        and e.get("review_status", "unreviewed") == "unreviewed"):
                    knowledge.append(e)
    else:
        queue = client.review_queue(agent=agent, limit=_TODO_QUERY_LIMIT)
        decisions = queue.get("decisions") or []
        knowledge = queue.get("knowledge") or []
    out: List[Dict[str, Any]] = []
    for d in decisions:
        out.append(dict(d, item_type="decision"))
    for e in knowledge:
        out.append(dict(e, item_type="knowledge"))
    out.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return out


# ------------------------------------------------------------ P05 知识库

def dd_knowledge_search(client: Any = None, agent: str = "", keyword: str = "",
                        tags: Optional[List[str]] = None,
                        limit: int = 50,
                        offline_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """按关键词/tag 检索某 agent 的知识条目 (按权重排序)。"""
    _require_exactly_one(client, offline_dir)
    if offline_dir is not None:
        return _offline_knowledge_query(_OfflineLayout(offline_dir), agent,
                                        keyword=keyword, tags=tags, limit=limit)
    return client.knowledge_query(agent, keyword=keyword, tags=tags, limit=limit)


def dd_knowledge_list(client: Any = None, agent: str = "", limit: int = 50,
                      offline_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """某 agent 的最近知识条目 (不过滤, 含 rejected; P08 展开视图用)。"""
    _require_exactly_one(client, offline_dir)
    if offline_dir is not None:
        return _read_jsonl(_OfflineLayout(offline_dir).knowledge_file(agent))[-limit:]
    return client.call("api_knowledge_list",
                       arguments={"agent": agent, "limit": limit})["data"]


# ------------------------------------------------------------ P06 Insight 中心

def dd_insights(client: Any = None, agent: Optional[str] = None,
                kind: Optional[str] = None, status: Optional[str] = None,
                limit: int = 100,
                offline_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """insight 列表 (self_issue 置顶, 其余按 ts 倒序)。"""
    _require_exactly_one(client, offline_dir)
    if offline_dir is not None:
        entries = _read_jsonl(_OfflineLayout(offline_dir).insights_log)
        if agent:
            entries = [e for e in entries if e.get("agent") == agent]
        if kind:
            entries = [e for e in entries if e.get("kind") == kind]
        if status:
            entries = [e for e in entries if e.get("status") == status]
        entries = entries[-limit:]
    else:
        entries = client.insights_list(agent=agent, kind=kind, status=status,
                                       limit=limit)
    # self_issue 类置顶 (最需要人关注), 其余按 ts 倒序
    entries = sorted(entries, key=lambda e: e.get("ts", 0), reverse=True)
    entries.sort(key=lambda e: 0 if e.get("kind") == "self_issue" else 1)
    return entries


# ------------------------------------------------------------ P07 升级管理

def dd_upgrades_by_status(client: Any = None,
                          offline_dir: Optional[str] = None
                          ) -> Dict[str, List[Dict[str, Any]]]:
    """升级提案按状态分列 (kanban 数据源); 5 个状态的键始终存在。"""
    _require_exactly_one(client, offline_dir)
    if offline_dir is not None:
        proposals = _read_jsonl(_OfflineLayout(offline_dir).upgrades_log)
        proposals.sort(key=lambda e: e.get("ts", 0), reverse=True)
    else:
        proposals = client.upgrade_list(limit=_TODO_QUERY_LIMIT)
    board: Dict[str, List[Dict[str, Any]]] = {s: [] for s in UPGRADE_STATUSES}
    for p in proposals:
        board.setdefault(p.get("status", "proposed"), []).append(p)
    return board


# ------------------------------------------------------------ P08 Agent 管理

def dd_agent_stats(client: Any = None, agent: str = "", days: int = 30,
                   offline_dir: Optional[str] = None) -> Dict[str, Any]:
    """单 agent 聚合: 近 N 天决策成功率 / 知识条目数 / 人类纠正次数。

    返回 {agent, days, decisions_total, decisions_ok, decisions_failed,
          decisions_pending, success_rate, knowledge_count, corrections}。
    success_rate = ok / (ok + failed); 无已闭环决策时为 None。
    """
    _require_exactly_one(client, offline_dir)
    since = time.time() - days * 86400.0
    if offline_dir is not None:
        layout = _OfflineLayout(offline_dir)
        decisions = [d for d in _read_jsonl(layout.decisions_log)
                     if d.get("agent") == agent]
        knowledge = _read_jsonl(layout.knowledge_file(agent))
    else:
        decisions = client.decision_query(agent=agent, limit=10000)
        knowledge = client.call("api_knowledge_list",
                                arguments={"agent": agent, "limit": 10000})["data"]
    recent = [d for d in decisions if d.get("ts", 0) >= since]
    ok = sum(1 for d in recent if d.get("status") == "ok")
    failed = sum(1 for d in recent if d.get("status") == "failed")
    pending = sum(1 for d in recent if d.get("status") == "pending")
    closed = ok + failed
    corrections = sum(1 for e in knowledge
                      if e.get("source") == "human_correction"
                      and e.get("ts", 0) >= since)
    return {"agent": agent, "days": days,
            "decisions_total": len(recent),
            "decisions_ok": ok, "decisions_failed": failed,
            "decisions_pending": pending,
            "success_rate": (round(ok / float(closed), 4) if closed else None),
            "knowledge_count": len(knowledge),
            "corrections": corrections}


# ------------------------------------------------------------ P10 观测与日志

def dd_observations(client: Any = None, agent: Optional[str] = None,
                    tool: Optional[str] = None, run_id: Optional[str] = None,
                    limit: int = 200,
                    offline_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """观测记录查询 (agent/tool/run_id 过滤, 按 ts 升序取最近 limit 条)。"""
    _require_exactly_one(client, offline_dir)
    if offline_dir is not None:
        return _offline_observations(_OfflineLayout(offline_dir), agent=agent,
                                     tool=tool, run_id=run_id, limit=limit)
    return client.call("api_observations_query",
                       arguments={"agent": agent, "tool": tool,
                                  "run_id": run_id, "limit": limit})["data"]


def dd_error_signatures(client: Any = None, agent: Optional[str] = None,
                        days: int = 7, top: int = 10,
                        offline_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """error_signatures 聚类 Top N (P10): [{signature, count, agents, last_ts}]。

    窗口内同一签名出现次数降序; 数据源同 dd_observations。
    """
    since = time.time() - days * 86400.0
    observations = dd_observations(client=client, agent=agent, limit=5000,
                                   offline_dir=offline_dir)
    groups: Dict[str, Dict[str, Any]] = {}
    for obs in observations:
        if obs.get("ok", True) or obs.get("ts", 0) < since:
            continue
        for sig in obs.get("error_signatures") or []:
            bucket = groups.setdefault(sig, {"signature": sig, "count": 0,
                                             "agents": set(), "last_ts": 0})
            bucket["count"] += 1
            if obs.get("agent"):
                bucket["agents"].add(obs["agent"])
            bucket["last_ts"] = max(bucket["last_ts"], obs.get("ts", 0))
    out = sorted(groups.values(), key=lambda g: (g["count"], g["last_ts"]),
                 reverse=True)[:top]
    for bucket in out:
        bucket["agents"] = sorted(bucket["agents"])
    return out


def dd_audit_tail(client: Any = None, limit: int = 100,
                  run_id: Optional[str] = None,
                  offline_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """审计日志 tail (只读)。

    审计日志在 server 侧落盘, 无网关查询接口 (flow_trace 只按 run_id 关联);
    Dashboard 与 server 同机部署, 两种模式都直接读文件尾部。
    client 在线时 offline_dir 缺省为当前工作目录。
    """
    root = offline_dir or os.getcwd()
    entries = _read_jsonl(_OfflineLayout(root).audit_log)
    if run_id:
        entries = [e for e in entries if e.get("run_id") == run_id]
    return entries[-limit:]
