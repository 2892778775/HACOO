"""Tessent / DFTMAX adapter — 可测性设计 DFT (持久 Tcl 会话, FE-4)。

阶段化 API (SPEC-evolution C.1 FE-4, T2 原则):
  api_dft_insert_scan(chains) / api_dft_atpg(patterns) / api_dft_report_coverage()
  Tessent (tessent -shell) 的 set_system_mode/insert_test_logic/create_patterns
  系与 DFTMAX (经 dc_shell-t) 的 set_scan_configuration/insert_dft 系命令差异
  封装在各自类内。

可执行文件: HACOO_TESSENT_BIN 或 PATH 中的 tessent; HACOO_DFTMAX_BIN 或
PATH 中的 dc_shell-t; binary 缺失时报错并给出 HACOO_<TOOL>_BIN 配置指引。
开发/测试可用 HACOO_TESSENT_MOCK=1 / HACOO_DFTMAX_MOCK=1 注入录制型
mock shell (接口行为一致, 沿用 FE-1/BE-1 先例)。

api_dft_report_coverage 解析为归一化三字段 {test_coverage, fault_coverage,
untestable} (两工具报告格式不同, 解析差异封装在各 parse_* 纯函数内);
扁平数值字段经网关观测钩子进入观测层 (FE-4 已加入 KNOWN_METRIC_KEYS 与
insights 方向表: 覆盖率 LOWER_IS_WORSE, untestable HIGHER_IS_WORSE)。

注意: 命令语义按公开工具文档编写, 公司环境的工具版本差异 (如 Tessent
setup/dft/analysis 模式切换、DFTMAX ATPG 经 TetraMAX) 是 T2 封装点,
首次在真实环境运行时应校准命令序列。
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List, Optional

from .base import ToolAdapter, api_method
from .shell import MockTclShell, TclShell, resolve_binary

# mock 模式的确定性应答 (按命令子串匹配), 供报告解析路径回归;
# 报告文本按各工具真实输出风格编写 (与 tests/fixtures/ 样例数值不同,
# 使解析器在 mock 下被严肃测试)。
_TESSENT_MOCK_CANNED = {
    # Tessent report_statistics: fault classes 汇总 + coverage 行
    "report_statistics": (
        "//---------------------------------------------------------\n"
        "// Statistics Report\n"
        "//   Design: chip   Patterns: 1000\n"
        "//---------------------------------------------------------\n"
        "  Fault Classes   #faults\n"
        "  -------------   -------\n"
        "  DS (det_simulation)      98741\n"
        "  DI (det_implication)      1021\n"
        "  AU (atpg_untestable)       512\n"
        "  ND (not_detected)          812\n"
        "  -------------\n"
        "  total                   101086\n"
        "\n"
        "  test_coverage:  97.42%\n"
        "  fault_coverage: 96.85%\n"
        "  atpg_effectiveness: 99.19%\n"),
}

_DFTMAX_MOCK_CANNED = {
    # DC (经 dc_shell-t) report_test_coverage 风格
    "report_test_coverage": (
        "****************************************\n"
        "Report : test coverage\n"
        "Design : chip\n"
        "Version: T-2022.03\n"
        "****************************************\n"
        "\n"
        "  Total faults:              45812\n"
        "  Detected faults:           44578\n"
        "  Untestable faults:           423\n"
        "  Pattern count:              1000\n"
        "\n"
        "  Test Coverage:   97.31%\n"
        "  Fault Coverage:  96.12%\n"),
}


# ---------------------------------------------------------- FE-4 报告解析 (纯函数)
# 输入覆盖率报告文本 -> 输出归一化 dict
# {test_coverage, fault_coverage, untestable}; 解析不到的字段为 None。
# 单位: 覆盖率为百分比 (0-100); untestable 为故障个数。

def parse_tessent_statistics(report: str) -> Dict[str, Optional[float]]:
    """Tessent report_statistics: 覆盖率取 test_coverage/fault_coverage 行;
    untestable 取 AU (atpg_untestable) 故障类计数。"""
    tc_m = re.search(r"(?i)test[_ ]coverage[: ]+([\d.]+)\s*%", report)
    fc_m = re.search(r"(?i)fault[_ ]coverage[: ]+([\d.]+)\s*%", report)
    au_m = re.search(r"(?im)^\s*AU\s+(?:\(\S+\)\s*)?(\d+)", report)
    return {"test_coverage": float(tc_m.group(1)) if tc_m else None,
            "fault_coverage": float(fc_m.group(1)) if fc_m else None,
            "untestable": float(au_m.group(1)) if au_m else None}


def parse_dftmax_coverage(report: str) -> Dict[str, Optional[float]]:
    """DFTMAX (dc_shell-t) report_test_coverage: 覆盖率取 Test/Fault
    Coverage 行; untestable 取 Untestable faults 行。"""
    tc_m = re.search(r"(?i)Test\s+Coverage[: ]+([\d.]+)\s*%", report)
    fc_m = re.search(r"(?i)Fault\s+Coverage[: ]+([\d.]+)\s*%", report)
    un_m = re.search(r"(?i)Untestable\s+faults[: ]+(\d+)", report)
    return {"test_coverage": float(tc_m.group(1)) if tc_m else None,
            "fault_coverage": float(fc_m.group(1)) if fc_m else None,
            "untestable": float(un_m.group(1)) if un_m else None}


class _DftTclAdapter(ToolAdapter):
    """公共骨架: 持久 shell 管理 + scan/atpg 状态; 工具命令语义由子类封装 (T2)。"""

    _BIN_DEFAULTS: List[str] = []
    _SHELL_ARGS: List[str] = []
    _MOCK_CANNED: Dict[str, Any] = {}

    def __init__(self) -> None:
        self._shell: Optional[TclShell] = None
        self.scan_inserted = False
        self.atpg_runs: List[Dict[str, Any]] = []

    def _session(self) -> TclShell:
        if self._shell is None:
            if os.environ.get("HACOO_%s_MOCK" % self.tool_name.upper()) == "1":
                self._shell = MockTclShell(self.tool_name, canned=self._MOCK_CANNED)
            else:
                exe = resolve_binary(self.tool_name, self._BIN_DEFAULTS)
                self._shell = TclShell(exe, self._SHELL_ARGS)
        return self._shell

    def __del__(self) -> None:
        if self._shell:
            self._shell.close()

    def _require_scan(self) -> None:
        if not self.scan_inserted:
            raise RuntimeError("no scan inserted; call api_dft_insert_scan first")

    def _require_atpg(self) -> None:
        if not self.atpg_runs:
            raise RuntimeError("no ATPG run; call api_dft_atpg first")

    @api_method(capability="read", description="Dump current DFT session state.")
    def api_get_design_state(self) -> Dict[str, Any]:
        return {"data": {"scan_inserted": self.scan_inserted,
                         "atpg_runs": len(self.atpg_runs)}}


class TessentAdapter(_DftTclAdapter):
    """Siemens Tessent (tessent -shell): set_system_mode/insert_test_logic 系。

    命令语义约定 (公司环境校准点): dft 模式 set_scan_configuration 后
    insert_test_logic 完成 scan 插入; analysis 模式 create_patterns 做
    ATPG, report_statistics 输出覆盖率统计。"""

    tool_name = "tessent"
    _BIN_DEFAULTS = ["tessent"]
    _SHELL_ARGS = ["-shell"]
    _MOCK_CANNED = _TESSENT_MOCK_CANNED

    @api_method(capability="write",
                description="Insert scan chains: dft mode + scan configuration + insert_test_logic.")
    def api_dft_insert_scan(self, chains: int = 8) -> Dict[str, Any]:
        """set_system_mode dft -> 配置扫描链数 -> insert_test_logic。"""
        if chains < 1:
            raise ValueError("chains must be >= 1")
        tess = self._session()
        t0 = time.time()
        for command in ("set_system_mode dft",
                        "set_scan_configuration -chain_count %d" % chains,
                        "insert_test_logic"):
            out = tess.cmd(command, timeout=1800)
            if "Error" in out:
                raise RuntimeError("scan insertion failed (%s):\n%s"
                                   % (command, out[-1500:]))
        self.scan_inserted = True
        return {"data": {"chains": chains, "inserted": True,
                         "elapsed_s": round(time.time() - t0, 1)},
                "text": "tessent scan insertion done (%d chains)" % chains}

    @api_method(capability="write",
                description="Run ATPG: analysis mode + create_patterns with pattern budget.")
    def api_dft_atpg(self, patterns: int = 1000) -> Dict[str, Any]:
        """set_system_mode analysis -> create_patterns (pattern 上限由
        set_pattern_limit 约定)。"""
        self._require_scan()
        if patterns < 1:
            raise ValueError("patterns must be >= 1")
        tess = self._session()
        t0 = time.time()
        for command in ("set_system_mode analysis",
                        "set_pattern_limit %d" % patterns,
                        "create_patterns"):
            out = tess.cmd(command, timeout=7200)
            if "Error" in out:
                raise RuntimeError("ATPG failed (%s):\n%s" % (command, out[-1500:]))
        record = {"patterns": patterns, "elapsed_s": round(time.time() - t0, 1)}
        self.atpg_runs.append(record)
        return {"data": dict(record, atpg_runs=len(self.atpg_runs)),
                "text": "tessent ATPG done (%d patterns budget) in %.1fs"
                        % (patterns, record["elapsed_s"])}

    @api_method(capability="read",
                description="report_statistics: normalized test/fault coverage (%) and untestable fault count.")
    def api_dft_report_coverage(self) -> Dict[str, Any]:
        """Tessent report_statistics 自带 fault class 与覆盖率, 解析三字段。"""
        self._require_atpg()
        report = self._session().cmd("report_statistics", timeout=600)
        data = dict(parse_tessent_statistics(report),
                    commands=["report_statistics"])
        return {"data": data, "text": report[-3000:]}


class DftMaxAdapter(_DftTclAdapter):
    """Synopsys DFTMAX (经 dc_shell-t): set_scan_configuration/insert_dft 系。

    命令语义约定 (公司环境校准点): dc_shell-t 内 set_scan_configuration
    -chain_count 配置链数, insert_dft 完成插入; ATPG 约定经
    create_test_patterns (若公司流程实际调用 TetraMAX, 差异在本类内
    封装, 接口不变)。"""

    tool_name = "dftmax"
    _BIN_DEFAULTS = ["dc_shell-t"]
    _MOCK_CANNED = _DFTMAX_MOCK_CANNED

    @api_method(capability="write",
                description="Insert scan chains: scan configuration + insert_dft.")
    def api_dft_insert_scan(self, chains: int = 8) -> Dict[str, Any]:
        """set_scan_configuration -chain_count -> insert_dft。"""
        if chains < 1:
            raise ValueError("chains must be >= 1")
        dc = self._session()
        t0 = time.time()
        for command in ("set_scan_configuration -chain_count %d" % chains,
                        "insert_dft"):
            out = dc.cmd(command, timeout=1800)
            if "Error" in out:
                raise RuntimeError("scan insertion failed (%s):\n%s"
                                   % (command, out[-1500:]))
        self.scan_inserted = True
        return {"data": {"chains": chains, "inserted": True,
                         "elapsed_s": round(time.time() - t0, 1)},
                "text": "dftmax scan insertion done (%d chains)" % chains}

    @api_method(capability="write",
                description="Run ATPG: create_test_patterns with pattern budget.")
    def api_dft_atpg(self, patterns: int = 1000) -> Dict[str, Any]:
        """create_test_patterns -pattern_count <n> (T2 约定命令)。"""
        self._require_scan()
        if patterns < 1:
            raise ValueError("patterns must be >= 1")
        dc = self._session()
        t0 = time.time()
        out = dc.cmd("create_test_patterns -pattern_count %d" % patterns,
                     timeout=7200)
        if "Error" in out:
            raise RuntimeError("ATPG failed (create_test_patterns):\n%s"
                               % out[-1500:])
        record = {"patterns": patterns, "elapsed_s": round(time.time() - t0, 1)}
        self.atpg_runs.append(record)
        return {"data": dict(record, atpg_runs=len(self.atpg_runs)),
                "text": "dftmax ATPG done (%d patterns budget) in %.1fs"
                        % (patterns, record["elapsed_s"])}

    @api_method(capability="read",
                description="report_test_coverage: normalized test/fault coverage (%) and untestable fault count.")
    def api_dft_report_coverage(self) -> Dict[str, Any]:
        """DFTMAX report_test_coverage 解析三字段 (与 Tessent 归一)。"""
        self._require_atpg()
        report = self._session().cmd("report_test_coverage", timeout=600)
        data = dict(parse_dftmax_coverage(report),
                    commands=["report_test_coverage"])
        return {"data": data, "text": report[-3000:]}
