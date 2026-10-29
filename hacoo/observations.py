"""观测沉淀层 — Agent 的"从业经历" (SPEC-agent-growth-loop §1)。

每次带 agent 的工具调用 (kind="tool") 后, 网关自动落盘一条观测记录
(成功/失败均记), 让 Agent 的经历跨 run、跨项目累积, 供 insights 检测器
离线分析 (趋势异常 / 失败签名聚类)。

存储: observations/<agent>.jsonl, append-only JSONL, 每行一条:
  obs_id / ts / agent / run_id / tool / method / instance_id / ok /
  metrics (白名单扁平数值) / error_signatures (错误行签名) / excerpt (输出尾部)
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

DEFAULT_OBSERVATIONS_DIR = "observations"

# 指标提取白名单: 已知 key 优先排列, 其余扁平数值字段按名字序补充
# (BE-2 追加 APR 阶段指标, 增强版归一后: utilization/hotspot_count/overflow_max/
#  skew/latency_max/clock_power/drc_count (congestion_hotspots->hotspot_count,
#  latency->latency_max 已迁移, 旧 key 移除);
#  FE-1 追加综合 QoR 指标: area/leakage/total_power)
#  SO-4 追加 LVS 指标: shorts/opens/net_mismatch/total_errors;
#  SO-7 追加 EMIR 指标: ir_drop_mv;
#  FE-4 追加 DFT 覆盖率指标: test_coverage/fault_coverage/untestable)
KNOWN_METRIC_KEYS = ("wns", "tns", "slack", "violator_lines",
                     "paths_analyzed", "version", "run_no",
                     "utilization", "hotspot_count", "overflow_max",
                     "skew", "latency_max", "clock_power", "drc_count",
                     "area", "leakage", "total_power",
                     "ir_drop_mv", "shorts", "opens", "net_mismatch",
                     "total_errors",
                     "test_coverage", "fault_coverage", "untestable",
                     "waived_count", "unwaived_count", "waivers_total")

# 错误签名: 匹配含 ERROR / Error / VIOLATED / *** ... fail 的整行
_ERROR_LINE_RE = re.compile(r"(?m)^.*(?:\bERROR\b|\bError\b|VIOLATED|\*\*\*.*\bfail).{0,80}$")
MAX_SIGNATURES = 5
SIGNATURE_MAX_LEN = 120
EXCERPT_MAX_LEN = 500


def extract_metrics(tool: str, data: Any) -> Dict[str, Any]:
    """白名单式指标提取: 只取 data 中值为 int/float 的扁平字段 (不深度遍历)。

    已知 key (wns/tns/slack 等) 排前面, 其余按名字序; bool 不算数值。
    tool 参数保留给将来按工具定制白名单。
    """
    if not isinstance(data, dict):
        return {}
    flat = {k: v for k, v in data.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}
    ordered: Dict[str, Any] = {}
    for key in KNOWN_METRIC_KEYS:
        if key in flat:
            ordered[key] = flat[key]
    for key in sorted(flat):
        if key not in ordered:
            ordered[key] = flat[key]
    return ordered


def extract_error_signatures(text: Optional[str]) -> List[str]:
    """从文本输出提取错误行签名: 最多 5 条, 每条截断 120 字符, 去重保序。"""
    if not text:
        return []
    out: List[str] = []
    for match in _ERROR_LINE_RE.finditer(text):
        line = match.group(0).strip()[:SIGNATURE_MAX_LEN]
        if line and line not in out:
            out.append(line)
        if len(out) >= MAX_SIGNATURES:
            break
    return out


MAX_OBS_HOTSPOTS = 10  # X-5: 观测随附 hotspot 结构的上限 (防爆 jsonl)


def extract_hotspots(data: Any) -> List[Dict[str, Any]]:
    """X-5: 从返回 data 提取 hotspot 坐标结构 (供跨域关联检测器按
    (x, y) 容差匹配)。只收带数值 x/y 的 dict, 最多 MAX_OBS_HOTSPOTS 条;
    无 hotspots 字段或不合规 -> []。"""
    if not isinstance(data, dict):
        return []
    raw = data.get("hotspots")
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for h in raw:
        if (isinstance(h, dict)
                and isinstance(h.get("x"), (int, float)) and not isinstance(h.get("x"), bool)
                and isinstance(h.get("y"), (int, float)) and not isinstance(h.get("y"), bool)):
            out.append(dict(h))
        if len(out) >= MAX_OBS_HOTSPOTS:
            break
    return out


class ObservationStore:
    """按 Agent 分文件的观测存储 (append-only JSONL + 进程内锁)。"""

    def __init__(self, directory: str = DEFAULT_OBSERVATIONS_DIR) -> None:
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

    def record(self, agent: str, run_id: Optional[str], tool: str, method: str,
               instance_id: Optional[str], ok: bool, metrics: Dict[str, Any],
               error_signatures: List[str], excerpt: str = "",
               hotspots: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        entry = {
            "obs_id": "obs-" + uuid.uuid4().hex[:10],
            "ts": round(time.time(), 3),
            "agent": agent,
            "run_id": run_id,
            "tool": tool,
            "method": method,
            "instance_id": instance_id,
            "ok": bool(ok),
            "metrics": metrics or {},
            "error_signatures": error_signatures or [],
            "excerpt": (excerpt or "")[-EXCERPT_MAX_LEN:],
        }
        if hotspots:  # X-5: 仅当存在坐标结构时落盘 (保持旧记录 schema 不变)
            entry["hotspots"] = hotspots
        with self._lock_for(agent):
            with open(self._path(agent), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    # ------------------------------------------------------------ 查询

    def _agents_on_disk(self) -> List[str]:
        if not os.path.isdir(self.directory):
            return []
        return sorted(f[:-6] for f in os.listdir(self.directory) if f.endswith(".jsonl"))

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

    def query(self, agent: Optional[str] = None, tool: Optional[str] = None,
              run_id: Optional[str] = None, since_ts: Optional[float] = None,
              limit: int = 500) -> List[Dict[str, Any]]:
        agents = [agent] if agent else self._agents_on_disk()
        out: List[Dict[str, Any]] = []
        for name in agents:
            for e in self._load(name):
                if tool and e.get("tool") != tool:
                    continue
                if run_id and e.get("run_id") != run_id:
                    continue
                if since_ts is not None and e.get("ts", 0) < since_ts:
                    continue
                out.append(e)
        out.sort(key=lambda e: e.get("ts", 0))
        return out[-limit:]
