"""Calibre adapter — PV 签核 (批处理模式: calibre -drc/-lvs/-perc)。

Calibre 无持久交互 shell, 每次 run 是独立子进程; adapter 实例内保存
run 历史与结果摘要 (R6)。结果解析自 RVE 报告 / transcript。
可执行文件: HACOO_CALIBRE_BIN 或 PATH 中的 calibre;
开发/测试可用 HACOO_CALIBRE_MOCK=1 注入录制型 transcript (接口行为一致)。

SO-4: LVS/PERC 报告结构化解析 —— parse_rve (RULECHECK 违例清单) /
parse_lvs_report (shorts/opens/net_mismatch); PERC 增加 perc_type 溯源。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List

from .base import ToolAdapter, api_method
from .shell import resolve_binary, run_batch

_DRC_ERRORS_RE = re.compile(r"TOTAL\s+Original\s+Layer\s+Errors\s*[:=]?\s*(\d+)", re.I)
# 注意: 必须 INCORRECT 在前 + 词边界, 否则 "INCORRECT" 会被匹配成 "CORRECT"
_LVS_RE = re.compile(r"\b(INCORRECT|CORRECT)\b", re.I)
_RULECHECK_RE = re.compile(
    r"RULECHECK\s+(\S+)[^\n]*?TOTAL\s+Result\s+Count\s*=\s*(\d+)", re.I)

# mock 模式的确定性 transcript, 按 Calibre 真实输出风格编写,
# 使 parse_rve / parse_lvs_report 在 mock 下被严肃测试。
_MOCK_TRANSCRIPTS = {
    "drc": (
        "// Calibre v2023.2  nmDRC (mock)\n"
        "RULECHECK M1.S.2 -------------- TOTAL Result Count = 5 (5)\n"
        "RULECHECK VIA1.R.1 ------------ TOTAL Result Count = 2 (2)\n"
        "--- TOTAL Original Layer Errors = 7\n"),
    "lvs": (
        "// Calibre v2023.2  nmLVS (mock)\n"
        "                    LVS REPORT\n"
        "  OVERALL COMPARISON RESULTS:   #     INCORRECT     #\n"
        "  o Initial Correspondence Points: 1245\n"
        "     Shorted Nets    : 2\n"
        "     Open Nets       : 1\n"
        "     Incorrect Nets  : 3\n"),
    "perc": (
        "// Calibre v2023.2  PERC (mock)\n"
        "PERC run completed. Check results in perc.report\n"),
}


def parse_rve(text: str) -> Dict[str, Any]:
    """Calibre ASCII 结果文件解析 (纯函数): RULECHECK 行 → 违例清单。

    返回 {"violations": [{"rule", "count"}], "total": int};
    解析不到时 violations 为空、total 为 0 (宽容解析)。
    """
    violations = [{"rule": m.group(1), "count": int(m.group(2))}
                  for m in _RULECHECK_RE.finditer(text)]
    return {"violations": violations,
            "total": sum(v["count"] for v in violations)}


def parse_lvs_report(text: str) -> Dict[str, Any]:
    """LVS 报告解析 (纯函数): 结论 + short/open/net 失配计数。"""
    m = _LVS_RE.search(text)
    verdict = m.group(1).upper() if m else None

    def _num(*patterns: str) -> int:
        for p in patterns:
            hit = re.search(p, text, re.I)
            if hit:
                return int(hit.group(1))
        return 0

    return {
        "result": verdict,
        "shorts": _num(r"shorted\s+nets\s*[:=]?\s*(\d+)", r"shorts\s*[:=]?\s*(\d+)"),
        "opens": _num(r"open\s+nets\s*[:=]?\s*(\d+)", r"opens\s*[:=]?\s*(\d+)"),
        "net_mismatch": _num(r"incorrect\s+nets\s*[:=]?\s*(\d+)",
                             r"net\s+mismatches?\s*[:=]?\s*(\d+)"),
        "clean": verdict == "CORRECT",
    }


class CalibreAdapter(ToolAdapter):
    tool_name = "calibre"

    def __init__(self) -> None:
        self.runs: List[Dict[str, Any]] = []
        self.waivers: List[Dict[str, Any]] = []  # SO-5: waiver 登记 (R6)

    def _calibre(self) -> str:
        if os.environ.get("HACOO_CALIBRE_MOCK") == "1":
            return "calibre (mock)"
        return resolve_binary("calibre", ["calibre"])

    def _run(self, kind: str, argv: List[str], timeout: float,
             cwd: str) -> str:
        """批处理执行; mock 模式返回录制 transcript (不启动子进程)。"""
        if os.environ.get("HACOO_CALIBRE_MOCK") == "1":
            return _MOCK_TRANSCRIPTS[kind]
        return run_batch(argv, timeout=timeout, cwd=cwd)

    def _record(self, kind: str, summary: Dict[str, Any], elapsed: float) -> Dict[str, Any]:
        record = {"kind": kind, "elapsed_s": round(elapsed, 1), **summary}
        self.runs.append(record)
        return record

    # ------------------------------------------------------------ api_*

    @api_method(capability="write", description="Run Calibre nmDRC with a runset on a layout.")
    def api_run_drc(self, runset: str, layout: str, top: str,
                    work_dir: str = "calibre_drc") -> Dict[str, Any]:
        """calibre -drc -hier; 解析 TOTAL Original Layer Errors 与
        RULECHECK 违例清单 (SO-4: parse_rve)。"""
        os.makedirs(work_dir, exist_ok=True)
        t0 = time.time()
        output = self._run("drc", [self._calibre(), "-drc", "-hier", "-hyper",
                                   runset], timeout=14400, cwd=work_dir)
        m = _DRC_ERRORS_RE.search(output)
        errors = int(m.group(1)) if m else None
        rve = parse_rve(output)
        record = self._record("drc", {"runset": runset, "layout": layout, "top": top,
                                      "total_errors": errors,
                                      "violations": rve["violations"],
                                      "clean": (errors == 0) if errors is not None else None},
                              time.time() - t0)
        return {"data": record,
                "text": "DRC %s: %s errors" % (top, errors if errors is not None else "unknown"),
                "artifacts": [{"kind": "transcript_tail", "content_tail": output[-2000:]}]}

    @api_method(capability="write", description="Run Calibre nmLVS (layout vs schematic).")
    def api_run_lvs(self, runset: str, layout: str, source: str, top: str,
                    work_dir: str = "calibre_lvs") -> Dict[str, Any]:
        """calibre -lvs -hier -spice; SO-4: 解析结论 + shorts/opens/
        net_mismatch 计数 (parse_lvs_report)。"""
        os.makedirs(work_dir, exist_ok=True)
        t0 = time.time()
        output = self._run("lvs", [self._calibre(), "-lvs", "-hier", "-hyper",
                                   "-spice", "%s.sp" % top, runset],
                           timeout=14400, cwd=work_dir)
        parsed = parse_lvs_report(output)
        record = self._record("lvs", {"runset": runset, "layout": layout,
                                      "source": source, "top": top,
                                      "verdict": parsed["result"],
                                      "shorts": parsed["shorts"],
                                      "opens": parsed["opens"],
                                      "net_mismatch": parsed["net_mismatch"],
                                      "clean": parsed["clean"]},
                              time.time() - t0)
        return {"data": record,
                "text": "LVS %s: %s (shorts=%d opens=%d net_mismatch=%d)"
                        % (top, parsed["result"] or "unknown", parsed["shorts"],
                           parsed["opens"], parsed["net_mismatch"]),
                "artifacts": [{"kind": "transcript_tail", "content_tail": output[-2000:]}]}

    @api_method(capability="write", description="Run Calibre PERC (reliability: ESD/latch-up checks).")
    def api_run_perc(self, runset: str, top: str,
                     perc_type: str = "esd",
                     work_dir: str = "calibre_perc") -> Dict[str, Any]:
        """calibre -perc; 可靠性检查 (ESD/latch-up/CD)。perc_type 记录检查
        类型供溯源 (具体检查项由 rule deck 驱动)。"""
        os.makedirs(work_dir, exist_ok=True)
        t0 = time.time()
        output = self._run("perc", [self._calibre(), "-perc", "-hier", runset],
                           timeout=14400, cwd=work_dir)
        record = self._record("perc", {"runset": runset, "top": top,
                                       "perc_type": perc_type}, time.time() - t0)
        return {"data": record, "text": "PERC %s (%s) done" % (top, perc_type),
                "artifacts": [{"kind": "transcript_tail", "content_tail": output[-2000:]}]}

    # ------------------------------------------------------------ SO-5: waiver 流程

    @api_method(capability="write", requires_decision=True,
                description="Waive DRC violations by rule+bbox with justification; a decision record is mandatory (DECISION_REQUIRED otherwise).")
    def api_calibre_waive(self, rule: str, bbox: str, justification: str,
                          work_dir: str = "calibre_drc") -> Dict[str, Any]:
        """waiver 登记 + 最近一次 DRC 结果按 waived/unwaived 分类。

        问责纪律 (硬性, 双重声明 — 此处与 pv subagent prompt):
        调用前必须先 api_decision_record 记录 waiver 理由, 网关强制,
        无有效决策记录拒绝执行 (DECISION_REQUIRED); justification 非空必填。

        waiver 规则追加到 <work_dir>/waivers.jsonl (waiver 规则文件管理,
        真实环境供下一次 calibre run 的 RVE waiver / rule include 使用);
        分类基于最近一次 api_run_drc 的违例清单 (按 rule 匹配)。"""
        if not justification.strip():
            raise ValueError("justification must be non-empty (accountability)")
        drc_runs = [r for r in self.runs if r.get("kind") == "drc"]
        if not drc_runs:
            raise RuntimeError("no DRC run to waive; call api_run_drc first")
        last = drc_runs[-1]
        os.makedirs(work_dir, exist_ok=True)
        waiver_file = os.path.join(work_dir, "waivers.jsonl")
        entry = {"ts": time.time(), "rule": rule, "bbox": bbox,
                 "justification": justification}
        with open(waiver_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.waivers.append(entry)
        violations = last.get("violations") or []
        waived = [v for v in violations if v.get("rule") == rule]
        unwaived = [v for v in violations if v.get("rule") != rule]
        record = self._record("waive",
                              {"rule": rule, "bbox": bbox,
                               "waived_count": sum(v["count"] for v in waived),
                               "unwaived_count": sum(v["count"] for v in unwaived),
                               "waivers_total": len(self.waivers)},
                              0.0)
        return {"data": dict(record, waived=waived, unwaived=unwaived,
                             waiver_file=waiver_file),
                "text": "waived %s (%d violations, bbox %s); unwaived: %d"
                        % (rule, record["waived_count"], bbox,
                           record["unwaived_count"])}

    @api_method(capability="read", description="List PV run history and verdicts.")
    def api_get_run_history(self) -> Dict[str, Any]:
        """PV run 历史 (DRC/LVS/PERC 与结论)。"""
        return {"data": {"runs": list(self.runs), "total": len(self.runs)}}
