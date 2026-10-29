"""Innovus adapter — APR (innovus -nowin 持久 Tcl 会话)。

阶段化 API: floorplan -> place -> cts -> route -> opt, 每步记录历史,
支持 3D IC 场景的 die 分区参数透传。
可执行文件: HACOO_INNOVUS_BIN 或 PATH 中的 innovus;
binary 缺失时报错并给出 HACOO_INNOVUS_BIN 配置指引。
开发/测试可用 HACOO_INNOVUS_MOCK=1 注入录制型 mock shell (接口行为一致)。

子阶段方法 (SPEC-evolution C.3 BE-1, T2 原则): api_fp_create /
api_fp_place_macros / api_place_opt / api_cts_build / api_cts_report /
api_route_opt —— 与 ICC2 adapter 签名一致, 命令差异封装在本文件内。

BE-2: 各子阶段执行后追加报告命令, 由模块级纯函数 (parse_*_report)
将报告文本解析为结构化 metrics 并入返回 data, 经网关观测钩子进观测层。

BE-2 增强: 新增独立报告方法 api_report_congestion / api_route_drc_report,
api_cts_report 扩展 clock_power; 指标 key 与 ICC2 归一
(hotspot_count / overflow_max / skew / latency_max / clock_power / drc_count,
top_rules 为结构化列表不进 metrics)。
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List, Optional

from .base import ToolAdapter, api_method
from .shell import MockTclShell, TclShell, resolve_binary

_STAGES = ("floorplan", "place", "cts", "route", "opt")

# mock 模式的确定性应答 (按命令子串匹配), 供报告解析路径回归;
# 报告文本按 Innovus 真实输出风格编写, 使解析器在 mock 下被严肃测试
_MOCK_CANNED = {
    "report_ccopt_clock_trees": ("*** Report Clock Trees (mock) ***\n"
                                 "Clock tree clk: skew 45.2ps, "
                                 "longest insertion delay 380.1ps\n"
                                 "Total clock tree power: 8.730 mW\n"),
    "report_timing": "WNS: 0.010\nTNS: 0.000\n",
    "report_floorplan": (
        "*** Report: floorplan (mock) ***\n"
        "Floorplan information:\n"
        "  Core Size: width 400.000, height 400.000\n"
        "  Core Aspect Ratio: 1.000\n"
        "  Core Utilization: 0.698\n"
        "  Standard Cell Area: 111680.00\n"),
    "report_congestion": (
        "*** Report: congestion (mock) ***\n"
        "Congestion Analysis after place_opt_design:\n"
        "  Total Overflow: 12 GRCs\n"
        "  Horizontal Overflow: 5, Vertical Overflow: 7\n"
        "  Congestion Hot Spots: 4\n"),
    "verify_drc": (
        "*** verify_drc (mock) ***\n"
        "Verifying DRC violations ...\n"
        "  Short: 2\n"
        "  Spacing: 5\n"
        "Summary: Total number of violations = 7\n"),
}


# ---------------------------------------------------------- BE-2 报告解析 (纯函数)
# 输入报告文本 -> 输出结构化 metrics dict; 解析不到的字段为 None

def parse_utilization_report(report: str) -> Dict[str, Optional[float]]:
    """report_floorplan: 提取 Core Utilization (0~1 小数)。"""
    m = re.search(r"core\s+utilization\s*[:=]?\s*([0-9]*\.?[0-9]+)",
                  report, re.IGNORECASE)
    return {"utilization": float(m.group(1)) if m else None}


def parse_congestion_report(report: str) -> Dict[str, Optional[int]]:
    """report_congestion: 提取 hot spot 数与最大 overflow。

    Innovus 报告无 ICC2 的 "Max = N" 列; overflow_max 取
    Horizontal/Vertical Overflow 的较大者 (报告格式差异封装在此)。
    """
    hot_m = re.search(r"hot\s*spots?[^0-9]*(\d+)", report, re.IGNORECASE)
    ovf_m = re.search(r"max\s+overflow[^0-9]*(\d+)", report, re.IGNORECASE)
    if ovf_m is None:
        dirs = [re.search(r"%s\s+overflow[^0-9]*(\d+)" % direction,
                          report, re.IGNORECASE)
                for direction in ("horizontal", "vertical")]
        values = [int(m.group(1)) for m in dirs if m]
        overflow_max = max(values) if values else None
    else:
        overflow_max = int(ovf_m.group(1))
    return {"hotspot_count": int(hot_m.group(1)) if hot_m else None,
            "overflow_max": overflow_max}


def parse_clock_report(report: str) -> Dict[str, Any]:
    """report_ccopt_clock_trees / report_ccopt_skew_groups:
    提取 skew / 最长 insertion delay / clock power
    (ps, Innovus 报告原生单位; clock_power 单位随报告, mW)。"""
    skew_m = re.search(r"skew[^0-9\-]*(-?\d+\.?\d*)", report, re.IGNORECASE)
    lat_m = re.search(r"(?:latency|insertion delay)[^0-9\-]*(-?\d+\.?\d*)",
                      report, re.IGNORECASE)
    power_m = re.search(r"power[^0-9\-]*(-?\d+\.?\d*)", report, re.IGNORECASE)
    return {"skew": float(skew_m.group(1)) if skew_m else None,
            "latency_max": float(lat_m.group(1)) if lat_m else None,
            "clock_power": float(power_m.group(1)) if power_m else None,
            "units": "ps"}


def parse_drc_report(report: str) -> Dict[str, Any]:
    """verify_drc: DRC 违例总数 + top_rules [(rule, count)]。

    top_rules 取 "<type>: N" 违例行中非零项, 按数量降序; 为结构化列表,
    不进扁平 metrics (extract_metrics 只拾取数值字段)。
    """
    total_m = re.search(r"total\s+number\s+of\s+violations?[^0-9]*(\d+)",
                        report, re.IGNORECASE)
    rules = [(m.group(1).strip(), int(m.group(2)))
             for m in re.finditer(
                 r"(?m)^\s*([A-Za-z][\w ]*?)\s*:\s*(\d+)\s*$", report)
             if "total" not in m.group(1).lower()]
    rules = sorted((rc for rc in rules if rc[1] > 0), key=lambda rc: -rc[1])
    return {"drc_count": int(total_m.group(1)) if total_m else None,
            "top_rules": rules}


class InnovusAdapter(ToolAdapter):
    tool_name = "innovus"

    def __init__(self) -> None:
        self._shell: Optional[TclShell] = None
        self.design: Dict[str, Any] = {}
        self.stage_history: List[Dict[str, Any]] = []

    def _inv(self) -> TclShell:
        if self._shell is None:
            if os.environ.get("HACOO_INNOVUS_MOCK") == "1":
                self._shell = MockTclShell("innovus", canned=_MOCK_CANNED)
            else:
                exe = resolve_binary("innovus", ["innovus"])
                self._shell = TclShell(exe, ["-nowin", "-no_logv"])
        return self._shell

    def __del__(self) -> None:
        if self._shell:
            self._shell.close()

    def _require_design(self) -> None:
        if not self.design:
            raise RuntimeError("no design initialized; call api_load_design first")

    # ------------------------------------------------------------ api_*

    @api_method(capability="write", description="Init design: read LEF/libs/netlist, set top.")
    def api_load_design(self, netlist: str, top: str,
                        lef: list, libs: list) -> Dict[str, Any]:
        """init_design 风格的初始化 (LEF + lib + netlist)。"""
        inv = self._inv()
        inv.cmd("set init_lef_file {%s}" % " ".join(lef))
        inv.cmd("set init_mmmc_file {view_definition.tcl}")
        inv.cmd("set init_verilog {%s}" % netlist)
        inv.cmd("set init_top_cell {%s}" % top)
        out = inv.cmd("init_design", timeout=900)
        if "ERROR" in out or "**ERROR" in out:
            raise RuntimeError("init_design failed:\n%s" % out[-1500:])
        self.design = {"netlist": netlist, "top": top, "lef": lef, "libs": libs}
        return {"data": {"initialized": True, "top": top},
                "text": "design '%s' initialized in Innovus" % top}

    @api_method(capability="write", description="Run one APR stage: floorplan|place|cts|route|opt.")
    def api_run_apr(self, stage: str, extra_tcl: str = "") -> Dict[str, Any]:
        """执行单个 APR 阶段; extra_tcl 可注入 3D IC 分区等定制命令。"""
        self._require_design()
        if stage not in _STAGES:
            raise ValueError("stage must be one of %s" % list(_STAGES))
        inv = self._inv()
        commands = {
            "floorplan": "floorPlan -su 1.0 0.6 4.0 4.0 4.0 4.0",
            "place": "place_design",
            "cts": "ccopt_design",
            "route": "routeDesign",
            "opt": "optDesign -postRoute",
        }
        t0 = time.time()
        if extra_tcl:
            inv.cmd(extra_tcl, timeout=1800)
        out = inv.cmd(commands[stage], timeout=7200)
        if "**ERROR" in out:
            raise RuntimeError("%s failed:\n%s" % (stage, out[-1500:]))
        record = {"stage": stage, "elapsed_s": round(time.time() - t0, 1)}
        self.stage_history.append(record)
        return {"data": dict(record, stages_done=len(self.stage_history)),
                "text": "%s done in %.1fs" % (stage, record["elapsed_s"])}

    # -------------------------------------------- BE-1 子阶段方法 (T2 封装)
    # 与 ICC2 adapter 的 6 个子阶段方法签名一致; 命令差异仅封装在各方法体内。
    # 命令语义已查证: floorPlan -r / planDesign / place_opt_design /
    # ccopt_design -cts / report_ccopt_clock_trees / routeDesign +
    # optDesign -postRoute 均为 Innovus (common UI) 真实命令。

    def _run_substage(self, stage: str, commands: List[str],
                      extra_tcl: str = "",
                      report_command: Optional[str] = None,
                      metric_parser: Any = None) -> Dict[str, Any]:
        """子阶段公共执行骨架: extra_tcl 前置注入 -> 依次执行 -> 记录历史。

        BE-2: report_command 在子阶段命令后追加执行, metric_parser (纯函数,
        报告文本 -> metrics dict) 的解析结果并入 data, 使阶段指标经网关
        观测钩子进入观测层 (扁平数值字段被 extract_metrics 提取)。
        """
        self._require_design()
        inv = self._inv()
        t0 = time.time()
        executed: List[str] = []
        if extra_tcl:
            inv.cmd(extra_tcl, timeout=1800)
            executed.append(extra_tcl)
        for command in commands:
            out = inv.cmd(command, timeout=7200)
            executed.append(command)
            if "**ERROR" in out:
                raise RuntimeError("%s failed (%s):\n%s" % (stage, command, out[-1500:]))
        metrics: Dict[str, Any] = {}
        if report_command:
            report = inv.cmd(report_command, timeout=600)
            executed.append(report_command)
            metrics = metric_parser(report) if metric_parser else {}
        record = {"stage": stage, "elapsed_s": round(time.time() - t0, 1)}
        self.stage_history.append(record)
        data = dict(record, commands=executed,
                    stages_done=len(self.stage_history))
        data.update(metrics)
        return {"data": data,
                "text": "%s done in %.1fs: %s" % (stage, record["elapsed_s"],
                                                  "; ".join(executed))}

    @api_method(capability="write",
                description="Create floorplan: core utilization / aspect ratio / margins.")
    def api_fp_create(self, utilization: float = 0.7, aspect_ratio: float = 1.0,
                      core_margin: float = 2.0, extra_tcl: str = "") -> Dict[str, Any]:
        """floorPlan -r <aspect> <util> <left bottom right top> 建 core 区域
        (Innovus -r 选项依次取长宽比/利用率/四边边距)。"""
        command = ("floorPlan -r %.3f %.3f %.2f %.2f %.2f %.2f"
                   % (aspect_ratio, utilization,
                      core_margin, core_margin, core_margin, core_margin))
        # BE-2: 建 core 后 report_floorplan 提取实际 core utilization
        return self._run_substage("fp_create", [command], extra_tcl,
                                  report_command="report_floorplan",
                                  metric_parser=parse_utilization_report)

    @api_method(capability="write",
                description="Place hard macros only (floorplan-stage macro placement).")
    def api_fp_place_macros(self, extra_tcl: str = "") -> Dict[str, Any]:
        """planDesign: Innovus 的 macro 自动摆放 (floorplan prototyping) 超级命令。"""
        return self._run_substage("fp_place_macros", ["planDesign"], extra_tcl)

    @api_method(capability="write", description="Placement + pre-CTS optimization (place_opt).")
    def api_place_opt(self, extra_tcl: str = "") -> Dict[str, Any]:
        """place_opt_design: coarse/refine placement + pre-CTS 时序/拥塞优化
        (legacy place_design 不含优化, 故用 common UI 的 place_opt_design);
        BE-2: 完成后 report_congestion 提取 congestion hot spot 数。"""
        return self._run_substage("place_opt", ["place_opt_design"], extra_tcl,
                                  report_command="report_congestion",
                                  metric_parser=parse_congestion_report)

    @api_method(capability="write", description="Build clock trees (CTS) with target skew.")
    def api_cts_build(self, target_skew_ps: float = 50.0,
                      extra_tcl: str = "") -> Dict[str, Any]:
        """set_ccopt_property target_skew (ccopt 属性原生单位为 ps) +
        ccopt_design -cts (仅建树; ccopt_design 全量 = -cts + optDesign -postCTS)。"""
        return self._run_substage("cts_build", [
            "set_ccopt_property target_skew %.0f" % target_skew_ps,
            "ccopt_design -cts",
        ], extra_tcl)

    @api_method(capability="read", description="Report clock tree QoR: skew / latency_max / clock_power.")
    def api_cts_report(self) -> Dict[str, Any]:
        """report_ccopt_clock_trees + report_ccopt_skew_groups:
        解析 skew / 最长 insertion delay / clock power
        (ps, Innovus 报告原生单位)。"""
        self._require_design()
        inv = self._inv()
        trees = inv.cmd("report_ccopt_clock_trees", timeout=600)
        skew_groups = inv.cmd("report_ccopt_skew_groups", timeout=600)
        report = trees + "\n" + skew_groups
        data = dict(parse_clock_report(report),
                    commands=["report_ccopt_clock_trees",
                              "report_ccopt_skew_groups"])
        return {"data": data, "text": report[-3000:]}

    @api_method(capability="read", description="Report routing congestion: hotspot_count / overflow_max.")
    def api_report_congestion(self) -> Dict[str, Any]:
        """report_congestion: BE-2 增强独立报告方法, 解析 hot spot 数与
        最大 overflow (命令已查证: Innovus common UI report_congestion)。"""
        self._require_design()
        inv = self._inv()
        report = inv.cmd("report_congestion", timeout=600)
        data = dict(parse_congestion_report(report),
                    commands=["report_congestion"])
        return {"data": data, "text": report[-3000:]}

    @api_method(capability="read", description="Report route DRC: drc_count / top_rules.")
    def api_route_drc_report(self) -> Dict[str, Any]:
        """verify_drc: BE-2 增强独立报告方法, 解析 DRC 违例总数与按数量
        降序的 top_rules [(rule, count)] (top_rules 为结构化列表,
        不进扁平 metrics)。"""
        self._require_design()
        inv = self._inv()
        report = inv.cmd("verify_drc", timeout=600)
        data = dict(parse_drc_report(report),
                    commands=["verify_drc"])
        return {"data": data, "text": report[-3000:]}

    @api_method(capability="write", description="Routing + post-route optimization (route_opt).")
    def api_route_opt(self, extra_tcl: str = "") -> Dict[str, Any]:
        """routeDesign (nanoRoute 布线) + optDesign -postRoute (post-route 时序优化);
        BE-2: 完成后 verify_drc 提取 DRC 违例总数。"""
        return self._run_substage("route_opt", [
            "routeDesign",
            "optDesign -postRoute",
        ], extra_tcl, report_command="verify_drc",
            metric_parser=parse_drc_report)

    @api_method(capability="read", description="Report congestion / timing QoR snapshot.")
    def api_report_qor(self) -> Dict[str, Any]:
        """QoR 快照: setup WNS/TNS + congestion 概要。"""
        self._require_design()
        inv = self._inv()
        timing = inv.cmd("report_timing -machine_readable", timeout=600)
        congestion = inv.cmd("reportCongestion 2>/dev/null || echo NA", timeout=300)
        wns_m = re.search(r"WNS\s*[:=]\s*(-?\d+\.?\d*)", timing)
        tns_m = re.search(r"TNS\s*[:=]\s*(-?\d+\.?\d*)", timing)
        return {"data": {"wns": float(wns_m.group(1)) if wns_m else None,
                         "tns": float(tns_m.group(1)) if tns_m else None,
                         "stages_done": len(self.stage_history)},
                "text": (timing + "\n" + congestion)[-3000:]}

    @api_method(capability="write", description="Save design DB (write_db).")
    def api_save_db(self, path: str) -> Dict[str, Any]:
        """保存 Innovus DB 快照。"""
        self._require_design()
        self._inv().cmd("write_db {%s}" % path, timeout=900)
        return {"text": "db saved: %s" % path}

    @api_method(capability="read", description="Dump current Innovus session state.")
    def api_get_design_state(self) -> Dict[str, Any]:
        return {"data": {"design": self.design or None,
                         "stage_history": list(self.stage_history)}}
