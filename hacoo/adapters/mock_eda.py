"""Mock EDA 工具适配器 — 模拟时序分析 / 综合工具, 用于开发与端到端测试。

真实部署时用相同模式封装实际 EDA 工具 (T2: 把统一 API 调用翻译为
工具特定命令语义, 差异被限制在 api_* 方法之后)。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, List

from .base import ToolAdapter, api_method


class MockEDAAdapter(ToolAdapter):
    """模拟一个有时序分析 / 综合能力的 EDA 后端。

    持久状态 (R6): 已加载设计、库、综合历史 —— 同一 instance 多次调用间保持不变。
    """

    tool_name = "mock_eda"

    def __init__(self) -> None:
        self.design: Dict[str, Any] = {}      # 已加载设计
        self.libs: List[str] = []             # 已读入库
        self.synthesis_runs: List[Dict[str, Any]] = []
        self._seed = int(hashlib.md5(str(id(self)).encode()).hexdigest()[:8], 16)

    # ------------------------------------------------------------ 工具语义

    def _require_design(self) -> None:
        if not self.design:
            raise RuntimeError("no design loaded; call api_load_design first")

    def _slack(self, tag: str) -> float:
        """由持久状态确定性地生成伪时序数 (同一实例多次调用结果一致)。"""
        h = hashlib.md5(("%s|%s|%d" % (self.design.get("top", ""), tag, self._seed)).encode()).hexdigest()
        return round((int(h[:8], 16) % 2000 - 1000) / 1000.0, 3)

    # ------------------------------------------------------------ api_* 方法

    @api_method(capability="write", description="Load a design netlist into the backend instance.")
    def api_load_design(self, path: str, top: str) -> Dict[str, Any]:
        """加载设计网表 (mock: 记录路径与顶层模块)。"""
        self.design = {"path": path, "top": top, "loaded_at": time.time(), "cells": 1000 + self._seed % 9000}
        return {"data": {"loaded": True, "top": top, "cells": self.design["cells"]},
                "text": "design '%s' loaded from %s" % (top, path)}

    @api_method(capability="write", description="Read a timing library (.lib).")
    def api_read_liberty(self, path: str) -> Dict[str, Any]:
        """读入时序库。"""
        self.libs.append(path)
        return {"text": "liberty loaded: %s (total %d libs)" % (path, len(self.libs))}

    @api_method(capability="write", description="Run synthesis with given effort (low/medium/high).")
    def api_run_synthesis(self, effort: str = "medium") -> Dict[str, Any]:
        """运行综合 (mock: 模拟耗时并记录历史)。"""
        self._require_design()
        if effort not in ("low", "medium", "high"):
            raise ValueError("effort must be low|medium|high")
        time.sleep({"low": 0.2, "medium": 0.5, "high": 1.0}[effort])  # 模拟长任务 (R8)
        run = {"effort": effort, "area": round(1000 + self._seed % 500 + len(self.synthesis_runs) * 7.3, 1)}
        self.synthesis_runs.append(run)
        return {"data": dict(run, runs_total=len(self.synthesis_runs)),
                "text": "synthesis done (effort=%s)" % effort}

    @api_method(capability="read", description="Report timing: WNS/TNS and top critical paths.")
    def api_report_timing(self, max_paths: int = 5) -> Dict[str, Any]:
        """时序报告 — 数值由实例状态决定, 证明 R6 状态保持。"""
        self._require_design()
        paths = [{"endpoint": "reg_%d/D" % i, "slack": self._slack("path%d" % i)}
                 for i in range(min(max_paths, 20))]
        paths.sort(key=lambda p: p["slack"])
        wns = paths[0]["slack"]
        tns = round(sum(p["slack"] for p in paths if p["slack"] < 0), 3)
        return {"data": {"top": self.design["top"], "wns": wns, "tns": tns,
                         "paths": paths, "synthesis_runs": len(self.synthesis_runs)},
                "text": "WNS=%.3f ns, TNS=%.3f ns over %d paths" % (wns, tns, len(paths))}

    @api_method(capability="read", description="Dump current backend state (for debugging/state checks).")
    def api_get_design_state(self) -> Dict[str, Any]:
        """查看当前实例的持久状态。"""
        return {"data": {"design": self.design or None, "libs": list(self.libs),
                         "synthesis_runs": len(self.synthesis_runs)}}
