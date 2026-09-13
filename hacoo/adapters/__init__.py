"""适配层注册: 工具名 -> adapter 类。

两种扩展方式:
  1. 内置登记: 在 ADAPTERS 字典加一行 "tool_name": "module:Class"
  2. 插件目录 (推荐): 把含 ToolAdapter 子类的 .py 文件放进
     HACOO_ADAPTERS_PATH 指定的目录 (os.pathsep 可分隔多个), 自动发现,
     无需修改平台代码。tool_name 冲突时插件覆盖内置并记录告警。
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
from typing import Dict, List, Type

from .base import ToolAdapter

log = logging.getLogger("hacoo.adapters")

# tool_name -> "module:Class"  (内置)
ADAPTERS: Dict[str, str] = {
    "mock_eda": "hacoo.adapters.mock_eda:MockEDAAdapter",  # 开发/测试用
    "isf": "hacoo.adapters.isf:ISFAdapter",                # ISF 自动化平台 (mock 形态)
    # ---- 生产 EDA 工具 (T2: 命令语义封装在各 adapter 内) ----
    "primetime": "hacoo.adapters.primetime:PrimeTimeAdapter",      # STA
    "icc2": "hacoo.adapters.icc2:ICC2Adapter",                     # APR (Synopsys)
    "innovus": "hacoo.adapters.innovus:InnovusAdapter",            # APR (Cadence)
    "calibre": "hacoo.adapters.calibre:CalibreAdapter",            # PV
    "voltus": "hacoo.adapters.voltus:VoltusAdapter",               # IR/EM + thermal
    "redhawk_sc": "hacoo.adapters.redhawk_sc:RedHawkSCAdapter",    # IR/EM
}

# tool_name -> 已加载的类 (插件发现填充)
_PLUGIN_CLASSES: Dict[str, Type[ToolAdapter]] = {}
_plugins_loaded = False


def _discover_plugins() -> None:
    """扫描 HACOO_ADAPTERS_PATH 目录, 注册其中的 ToolAdapter 子类。"""
    global _plugins_loaded
    if _plugins_loaded:
        return
    _plugins_loaded = True
    paths = [p for p in os.environ.get("HACOO_ADAPTERS_PATH", "").split(os.pathsep) if p]
    for directory in paths:
        if not os.path.isdir(directory):
            log.warning("HACOO_ADAPTERS_PATH entry not a directory: %s", directory)
            continue
        for fname in sorted(os.listdir(directory)):
            if not fname.endswith(".py") or fname.startswith("_"):
                continue
            module_name = "hacoo_plugin_" + fname[:-3]
            try:
                spec = importlib.util.spec_from_file_location(
                    module_name, os.path.join(directory, fname))
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
            except Exception as exc:
                log.error("failed to load adapter plugin %s: %s", fname, exc)
                continue
            for attr in vars(module).values():
                if (isinstance(attr, type) and issubclass(attr, ToolAdapter)
                        and attr is not ToolAdapter and attr.tool_name != "base"):
                    if attr.tool_name in ADAPTERS or attr.tool_name in _PLUGIN_CLASSES:
                        log.warning("adapter tool_name '%s' overridden by plugin %s",
                                    attr.tool_name, fname)
                    _PLUGIN_CLASSES[attr.tool_name] = attr
                    log.info("registered plugin adapter: %s (%s)", attr.tool_name, fname)


def list_tools() -> List[str]:
    _discover_plugins()
    return sorted(set(ADAPTERS) | set(_PLUGIN_CLASSES))


def get_adapter_class(tool: str) -> Type[ToolAdapter]:
    _discover_plugins()
    if tool in _PLUGIN_CLASSES:
        return _PLUGIN_CLASSES[tool]
    if tool not in ADAPTERS:
        raise KeyError("unknown EDA tool: '%s' (registered: %s)" % (tool, ", ".join(list_tools())))
    module_name, class_name = ADAPTERS[tool].split(":")
    module = importlib.import_module(module_name)
    return getattr(module, class_name)
