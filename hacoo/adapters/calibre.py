"""Calibre adapter — PV 签核 (批处理模式: calibre -drc/-lvs/-perc)。

Calibre 无持久交互 shell, 每次 run 是独立子进程; adapter 实例内保存
run 历史与结果摘要 (R6)。结果解析自 RVE 报告 / transcript。
可执行文件: HACOO_CALIBRE_BIN 或 PATH 中的 calibre。
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List

from .base import ToolAdapter, api_method
from .shell import resolve_binary, run_batch

_DRC_ERRORS_RE = re.compile(r"TOTAL\s+Original\s+Layer\s+Errors\s*[:=]?\s*(\d+)", re.I)
_LVS_RE = re.compile(r"(CORRECT|INCORRECT)", re.I)


class CalibreAdapter(ToolAdapter):
    tool_name = "calibre"

    def __init__(self) -> None:
        self.runs: List[Dict[str, Any]] = []

    def _calibre(self) -> str:
        return resolve_binary("calibre", ["calibre"])

    def _record(self, kind: str, summary: Dict[str, Any], elapsed: float) -> Dict[str, Any]:
        record = {"kind": kind, "elapsed_s": round(elapsed, 1), **summary}
        self.runs.append(record)
        return record

    # ------------------------------------------------------------ api_*

    @api_method(capability="write", description="Run Calibre nmDRC with a runset on a layout.")
    def api_run_drc(self, runset: str, layout: str, top: str,
                    work_dir: str = "calibre_drc") -> Dict[str, Any]:
        """calibre -drc -hier; 解析 TOTAL Original Layer Errors。"""
        os.makedirs(work_dir, exist_ok=True)
        t0 = time.time()
        output = run_batch([self._calibre(), "-drc", "-hier", "-hyper", runset],
                           timeout=14400, cwd=work_dir)
        m = _DRC_ERRORS_RE.search(output)
        errors = int(m.group(1)) if m else None
        record = self._record("drc", {"runset": runset, "layout": layout, "top": top,
                                      "total_errors": errors,
                                      "clean": (errors == 0) if errors is not None else None},
                              time.time() - t0)
        return {"data": record,
                "text": "DRC %s: %s errors" % (top, errors if errors is not None else "unknown"),
                "artifacts": [{"kind": "transcript_tail", "content_tail": output[-2000:]}]}

    @api_method(capability="write", description="Run Calibre nmLVS (layout vs schematic).")
    def api_run_lvs(self, runset: str, layout: str, source: str, top: str,
                    work_dir: str = "calibre_lvs") -> Dict[str, Any]:
        """calibre -lvs -hier -spice; 解析 CORRECT/INCORRECT。"""
        os.makedirs(work_dir, exist_ok=True)
        t0 = time.time()
        output = run_batch([self._calibre(), "-lvs", "-hier", "-hyper",
                            "-spice", "%s.sp" % top, runset],
                           timeout=14400, cwd=work_dir)
        m = _LVS_RE.search(output)
        verdict = m.group(1).upper() if m else None
        record = self._record("lvs", {"runset": runset, "layout": layout,
                                      "source": source, "top": top,
                                      "verdict": verdict, "clean": verdict == "CORRECT"},
                              time.time() - t0)
        return {"data": record,
                "text": "LVS %s: %s" % (top, verdict or "unknown"),
                "artifacts": [{"kind": "transcript_tail", "content_tail": output[-2000:]}]}

    @api_method(capability="write", description="Run Calibre PERC (reliability: ESD/latch-up checks).")
    def api_run_perc(self, runset: str, top: str,
                     work_dir: str = "calibre_perc") -> Dict[str, Any]:
        """calibre -perc; 可靠性检查 (ESD/latch-up/P2P)。"""
        os.makedirs(work_dir, exist_ok=True)
        t0 = time.time()
        output = run_batch([self._calibre(), "-perc", "-hier", runset],
                           timeout=14400, cwd=work_dir)
        record = self._record("perc", {"runset": runset, "top": top}, time.time() - t0)
        return {"data": record, "text": "PERC %s done" % top,
                "artifacts": [{"kind": "transcript_tail", "content_tail": output[-2000:]}]}

    @api_method(capability="read", description="List PV run history and verdicts.")
    def api_get_run_history(self) -> Dict[str, Any]:
        """PV run 历史 (DRC/LVS/PERC 与结论)。"""
        return {"data": {"runs": list(self.runs), "total": len(self.runs)}}
