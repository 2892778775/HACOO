"""主动发现层 — insights (SPEC-agent-growth-loop §3)。

定期从观测/决策/知识库 miss 日志中检测五类问题, 生成带证据的 insight
条目; 工程师 confirm 后回流知识库, dismissed 的同类问题不再报警
(避免告警疲劳)。X-5 新增跨域关联检测器 (cross_domain): 双侧 agent
hotspot 按坐标容差网格匹配, 需双侧观测, 在逐 agent 检测之后运行。

检测器全部规则化、确定性、纯函数可单测, 不依赖 LLM; 阈值集中为模块常量。
存储: logs/insights.jsonl, append-only + 读-改-写更新状态。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

log = logging.getLogger("hacoo.insights")

# ------------------------------------------------------------ 检测阈值 (集中调优)

TREND_MIN_POINTS = 3                # 趋势异常: 连续 N 个 run 单调恶化
# 方向表 (X-1 扩充): LOWER_IS_WORSE 越小越差, HIGHER_IS_WORSE 越大越差;
# 未登记的数值指标默认按越大越差处理并记 debug 日志 (便于发现该补表的指标)。
LOWER_IS_WORSE = {"wns", "tns", "slack", "test_coverage", "fault_coverage"}
HIGHER_IS_WORSE = {"drc_count", "violator_lines", "hotspot_count",
                   "overflow_max", "skew", "latency_max", "clock_power",
                   "area", "leakage", "total_power", "ir_drop_mv",
                   "shorts", "opens", "net_mismatch", "total_errors",
                   "untestable", "unwaived_count"}

ANOMALY_WINDOW_S = 7 * 86400        # 失败签名聚类窗口 (默认 7 天)
ANOMALY_MIN_OCCURRENCES = 3         # 同一 error_signature 出现次数阈值

MISS_MIN_COUNT = 2                  # 知识盲区: 同一 (agent, keyword) 未命中次数阈值

SELF_ISSUE_LOOKBACK = 10            # 自我问题: 考察最近 N 条决策
SELF_ISSUE_MIN_DECISIONS = 5        #   至少 5 条才判定
SELF_ISSUE_FAIL_RATIO = 0.3         #   failed 占比阈值
CORRECTION_WINDOW_S = 7 * 86400     #   人类纠正统计窗口
CORRECTION_MIN_COUNT = 2            #   human_correction 条目数阈值

PROPOSED_ACTIONS = {
    "trend": "建议复查该指标恶化的根因, 并将结论沉淀为知识条目",
    "anomaly": "建议沉淀该错误签名的处理经验为知识条目",
    "blind_spot": "建议补充该主题的知识条目, 消除知识盲区",
    "self_issue": "建议调整决策策略, 并复查近期失败决策/人类纠正",
    "cross_domain": "建议两个 domain owner 联合会诊该重叠热点 "
                    "(热-压降互相加剧区域需同步优化: decap/PG/散热)",
}

# X-5 跨域关联: hotspot 坐标容差 (um) — 按容差取整划分坐标网格,
# 同网格即视为重叠; 网格坐标进签名保证去重稳定
CROSS_DOMAIN_COORD_TOL_UM = 5.0
CROSS_DOMAIN_PAIRS = (("thermal", "irem"),)  # 可扩展的跨域配对


def _signature(agent: str, kind: str, key: str) -> str:
    """insight 去重签名: <agent>:<kind>:<稳定关键串的 sha1[:12]>。"""
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return "%s:%s:%s" % (agent, kind, digest)


def _candidate(agent: str, kind: str, title: str, detail: str,
               evidence: List[Dict[str, Any]], confidence: float,
               signature_key: str) -> Dict[str, Any]:
    return {
        "agent": agent,
        "kind": kind,
        "title": title,
        "detail": detail,
        "evidence": evidence,
        "confidence": confidence,
        "signature": _signature(agent, kind, signature_key),
        "proposed_action": PROPOSED_ACTIONS[kind],
    }


# ------------------------------------------------------------ 检测器 1: 趋势异常

def _is_worsening(key: str, vals: List[float]) -> bool:
    """按方向表判定三点序列是否单调恶化 (X-1)。"""
    if key in LOWER_IS_WORSE:
        return vals[0] > vals[1] > vals[2]
    if key not in HIGHER_IS_WORSE:
        log.debug("metric '%s' not in direction table; "
                  "default higher-is-worse", key)
    return vals[0] < vals[1] < vals[2]


def detect_trend(agent: str, observations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同一 (agent, metric key) 取最近 >=3 个 run 的值, 连续 3 点单调恶化即报警。

    方向表 (X-1): LOWER_IS_WORSE 越小越差, HIGHER_IS_WORSE 越大越差,
    未知 key 默认越大越差 (debug 日志提示)。
    """
    by_key: Dict[str, List[Dict[str, Any]]] = {}
    for obs in observations:
        for key, val in (obs.get("metrics") or {}).items():
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                continue
            by_key.setdefault(key, []).append(obs)
    out: List[Dict[str, Any]] = []
    for key in sorted(by_key):
        per_run: Dict[str, Dict[str, Any]] = {}
        for obs in sorted(by_key[key], key=lambda o: o.get("ts", 0)):
            per_run[obs.get("run_id") or obs.get("obs_id", "")] = obs  # 同 run 取最新
        points = sorted(per_run.values(), key=lambda o: o.get("ts", 0))
        if len(points) < TREND_MIN_POINTS:
            continue
        tail = points[-TREND_MIN_POINTS:]
        vals = [o["metrics"][key] for o in tail]
        if not _is_worsening(key, vals):
            continue
        detail = " -> ".join("%s: %s" % (o.get("run_id") or "?", v)
                              for o, v in zip(tail, vals))
        out.append(_candidate(
            agent, "trend",
            title="%s 连续 %d 个 run 恶化" % (key, TREND_MIN_POINTS),
            detail=detail,
            evidence=[{"type": "observation", "ref": o.get("obs_id", ""),
                       "excerpt": (o.get("excerpt") or "")[:120]} for o in tail],
            confidence=0.8, signature_key="metric:%s" % key))
    return out


# ------------------------------------------------------------ 检测器 2: 失败签名聚类

def detect_anomaly(agent: str, observations: List[Dict[str, Any]],
                   now_ts: float) -> List[Dict[str, Any]]:
    """窗口内同一 agent 的同一 error_signature 出现 >=3 次即报警。"""
    cutoff = now_ts - ANOMALY_WINDOW_S
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for obs in observations:
        if obs.get("ok", True) or obs.get("ts", 0) < cutoff:
            continue
        for sig in obs.get("error_signatures") or []:
            groups.setdefault(sig, []).append(obs)
    out: List[Dict[str, Any]] = []
    for sig in sorted(groups):
        obs_list = groups[sig]
        if len(obs_list) < ANOMALY_MIN_OCCURRENCES:
            continue
        out.append(_candidate(
            agent, "anomaly",
            title="错误签名 %d 天内重复出现 %d 次" % (ANOMALY_WINDOW_S // 86400,
                                                   len(obs_list)),
            detail=sig,
            evidence=[{"type": "observation", "ref": o.get("obs_id", ""),
                       "excerpt": sig[:120]} for o in obs_list[:5]],
            confidence=0.7, signature_key="sig:%s" % sig[:40]))
    return out


# ------------------------------------------------------------ 检测器 3: 知识盲区

def detect_blind_spot(agent: str, kb_misses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """KB miss 日志中同一 (agent, keyword) 未命中 >=2 次即报警。"""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for miss in kb_misses:
        kw = (miss.get("keyword") or "").strip()
        if kw:
            groups.setdefault(kw, []).append(miss)
    out: List[Dict[str, Any]] = []
    for kw in sorted(groups):
        misses = groups[kw]
        if len(misses) < MISS_MIN_COUNT:
            continue
        out.append(_candidate(
            agent, "blind_spot",
            title="知识盲区: '%s' 查询 %d 次零命中" % (kw, len(misses)),
            detail="agent '%s' 多次查询 '%s' 均无知识条目命中" % (agent, kw),
            evidence=[{"type": "kb_miss", "ref": "",
                       "excerpt": "ts=%s keyword=%s" % (m.get("ts"), kw)}
                      for m in misses[:5]],
            confidence=0.6, signature_key="miss:%s" % kw))
    return out


# ------------------------------------------------------------ 检测器 4: 自我问题

def detect_self_issue(agent: str, decisions: List[Dict[str, Any]],
                      knowledge_entries: List[Dict[str, Any]],
                      now_ts: float) -> List[Dict[str, Any]]:
    """满足任一即报警:
    (a) 最近 >=5 条决策中 failed 占比 >=30%;
    (b) 窗口内 source=human_correction 条目 >=2 条 (频繁被人类纠正)。
    """
    out: List[Dict[str, Any]] = []
    recent = sorted(decisions, key=lambda d: d.get("ts", 0))[-SELF_ISSUE_LOOKBACK:]
    if len(recent) >= SELF_ISSUE_MIN_DECISIONS:
        failed = [d for d in recent if d.get("status") == "failed"]
        ratio = len(failed) / float(len(recent))
        if ratio >= SELF_ISSUE_FAIL_RATIO:
            out.append(_candidate(
                agent, "self_issue",
                title="最近 %d 条决策中 %d 条失败 (%.0f%%)" % (len(recent), len(failed),
                                                            ratio * 100),
                detail="failed: " + ", ".join(d.get("decision_id", "?") for d in failed),
                evidence=[{"type": "decision", "ref": d.get("decision_id", ""),
                           "excerpt": "%s: %s" % (d.get("stage", ""),
                                                  d.get("action", ""))}
                          for d in failed[:5]],
                confidence=0.75, signature_key="self:%s" % agent))
    cutoff = now_ts - CORRECTION_WINDOW_S
    corrections = [e for e in knowledge_entries
                   if e.get("source") == "human_correction" and e.get("ts", 0) >= cutoff]
    if len(corrections) >= CORRECTION_MIN_COUNT:
        tags = sorted({t for e in corrections for t in e.get("tags", [])})
        out.append(_candidate(
            agent, "self_issue",
            title="%d 天内被人类纠正 %d 次" % (CORRECTION_WINDOW_S // 86400,
                                             len(corrections)),
            detail="corrections: " + ", ".join(e.get("id", "?") for e in corrections),
            evidence=[{"type": "knowledge", "ref": e.get("id", ""),
                       "excerpt": (e.get("symptom") or "")[:120]}
                      for e in corrections[:5]],
            confidence=0.85, signature_key="corr:%s" % ",".join(tags)))
    return out


# ------------------------------------------------------------ 检测器 5: 跨域关联 (X-5)

def _grid_cell(x: float, y: float, tol: float) -> tuple:
    """坐标按容差取整划分网格 (容差取整坐标网格 -> 签名去重稳定)。"""
    return (int(math.floor(x / tol)), int(math.floor(y / tol)))


def detect_cross_domain(agents_obs: Dict[str, List[Dict[str, Any]]],
                        tol: float = CROSS_DOMAIN_COORD_TOL_UM) -> List[Dict[str, Any]]:
    """跨域 hotspot 关联: CROSS_DOMAIN_PAIRS 中两个 domain 的观测所带
    hotspots 按 (x, y) 容差网格匹配, 同网格即重叠 — 为双侧 agent 各生成
    一条 insight, evidence 同时引用两侧观测 (通知两个 domain owner)。

    agents_obs: {agent: [观测条目 (含 hotspots 字段, X-5 观测层扩展)]}。
    纯函数, 不做持久化。"""
    out: List[Dict[str, Any]] = []
    for agent_a, agent_b in CROSS_DOMAIN_PAIRS:
        cells: Dict[tuple, Dict[str, List]] = {}
        for side, agent in (("a", agent_a), ("b", agent_b)):
            for obs in agents_obs.get(agent, []):
                for hs in obs.get("hotspots") or []:
                    if not isinstance(hs, dict):
                        continue
                    x, y = hs.get("x"), hs.get("y")
                    if (not isinstance(x, (int, float)) or isinstance(x, bool)
                            or not isinstance(y, (int, float)) or isinstance(y, bool)):
                        continue
                    cell = _grid_cell(float(x), float(y), tol)
                    cells.setdefault(cell, {}).setdefault(side, []).append((obs, hs))
        for cell in sorted(cells):
            slot = cells[cell]
            if "a" not in slot or "b" not in slot:
                continue
            obs_a, hs_a = slot["a"][0]
            obs_b, hs_b = slot["b"][0]
            evidence = [
                {"type": "observation", "ref": obs_a.get("obs_id", ""),
                 "excerpt": "%s hotspot (%s, %s) run=%s"
                            % (agent_a, hs_a.get("x"), hs_a.get("y"),
                               obs_a.get("run_id") or "?")},
                {"type": "observation", "ref": obs_b.get("obs_id", ""),
                 "excerpt": "%s hotspot (%s, %s) run=%s"
                            % (agent_b, hs_b.get("x"), hs_b.get("y"),
                               obs_b.get("run_id") or "?")}]
            sig_key = "pair:%s-%s:grid:%d:%d" % (agent_a, agent_b,
                                                 cell[0], cell[1])
            for agent, other in ((agent_a, agent_b), (agent_b, agent_a)):
                out.append(_candidate(
                    agent, "cross_domain",
                    title="跨域关联: %s x %s 热点在网格(%d, %d)重叠 "
                          "(容差 %.1fum)" % (agent_a, agent_b, cell[0], cell[1], tol),
                    detail="%s 与 %s 的 hotspot 观测落在同一坐标网格, "
                           "两域问题可能互相加剧, 需联合会诊" % (agent, other),
                    evidence=evidence, confidence=0.7,
                    signature_key=sig_key))
    return out


# ------------------------------------------------------------ 统一入口

def run_detectors(agent: str, observations: List[Dict[str, Any]],
                  decisions: List[Dict[str, Any]],
                  kb_misses: List[Dict[str, Any]],
                  knowledge_entries: List[Dict[str, Any]],
                  now_ts: float) -> List[Dict[str, Any]]:
    """对单个 agent 跑全部 4 类检测器, 返回候选 insight 列表 (不含持久化)。"""
    candidates: List[Dict[str, Any]] = []
    candidates.extend(detect_trend(agent, observations))
    candidates.extend(detect_anomaly(agent, observations, now_ts))
    candidates.extend(detect_blind_spot(agent, kb_misses))
    candidates.extend(detect_self_issue(agent, decisions, knowledge_entries, now_ts))
    return candidates


# ------------------------------------------------------------ 存储

class InsightStore:
    """insight 存储: append-only JSONL, review 状态读-改-写更新。"""

    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()

    def add(self, agent: str, kind: str, title: str, detail: str = "",
            evidence: Optional[List[Dict[str, Any]]] = None,
            confidence: float = 0.5, signature: str = "",
            proposed_action: str = "") -> Dict[str, Any]:
        entry = {
            "insight_id": "ins-" + uuid.uuid4().hex[:10],
            "ts": round(time.time(), 3),
            "agent": agent,
            "kind": kind,
            "title": title,
            "detail": detail,
            "evidence": evidence or [],
            "confidence": confidence,
            "signature": signature,
            "status": "new",
            "proposed_action": proposed_action,
            "review_note": "",
        }
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

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

    def exists_active(self, signature: str) -> bool:
        """同 signature 且 status 为 new/confirmed/dismissed 均视为已存在
        —— dismissed 也不再重复报警 (避免告警疲劳)。"""
        for e in self._load_all():
            if e.get("signature") == signature:
                return True
        return False

    def review(self, insight_id: str, action: str, note: str = "") -> Dict[str, Any]:
        if action not in ("confirm", "dismiss"):
            raise ValueError("action must be confirm|dismiss, got '%s'" % action)
        with self._lock:
            entries = self._load_all()
            for e in entries:
                if e.get("insight_id") == insight_id:
                    e["status"] = "confirmed" if action == "confirm" else "dismissed"
                    e["review_note"] = note
                    self._rewrite(entries)
                    return e
        raise KeyError("unknown insight_id: '%s'" % insight_id)

    def query(self, agent: Optional[str] = None, kind: Optional[str] = None,
              status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        entries = self._load_all()
        if agent:
            entries = [e for e in entries if e.get("agent") == agent]
        if kind:
            entries = [e for e in entries if e.get("kind") == kind]
        if status:
            entries = [e for e in entries if e.get("status") == status]
        return entries[-limit:]
