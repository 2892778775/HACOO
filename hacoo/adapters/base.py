"""适配层 (T1-T4): 工具适配基类与 api_* 方法规范。

设计原则 (SPEC): 不消除工具特定行为, 而是将其封装在统一接口之后。
每个 adapter 实例驻留在一个后端 worker 进程中, 跨请求保持状态 (R6)。
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class MethodSpec:
    """网关注册表中的方法条目 (T1: 方法注册)。"""

    name: str
    description: str = ""
    capability: str = "read"  # read | write | admin — 供 G4 能力级授权
    kind: str = "tool"  # "generic" (网关通用 API) | "tool" (工具特定 API)
    tool: Optional[str] = None
    params: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # params: {"arg": {"type": "str", "required": True, "default": None}}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "capability": self.capability,
            "kind": self.kind,
            "tool": self.tool,
            "params": self.params,
        }


def api_method(capability: str = "read", description: str = "") -> Callable:
    """装饰器: 声明一个 api_* 方法的元数据 (能力级别 / 描述)。"""

    def wrap(fn: Callable) -> Callable:
        fn._hacoo_capability = capability
        fn._hacoo_description = description or (fn.__doc__ or "").strip().splitlines()[0] if (description or fn.__doc__) else ""
        return fn

    return wrap


class ToolAdapter:
    """EDA 工具适配器基类。

    子类约定:
      - tool_name: 工具标识 (spawn 时使用)
      - api_* 方法: 用 @api_method 装饰, 封装工具原生功能 (T2 语义适配)
      - 方法返回 dict -> 作为 typed data; 返回 str -> 作为 text output
      - 实例属性即持久状态 (R6), 由后端 worker 进程持有
    """

    tool_name: str = "base"

    @classmethod
    def api_specs(cls) -> List[MethodSpec]:
        """T1: 自省所有 api_* 方法, 生成注册表条目。"""
        specs: List[MethodSpec] = []
        for name, member in inspect.getmembers(cls, predicate=inspect.isfunction):
            if not name.startswith("api_"):
                continue
            sig = inspect.signature(member)
            params: Dict[str, Dict[str, Any]] = {}
            for pname, p in sig.parameters.items():
                if pname == "self":
                    continue
                anno = p.annotation
                type_name = getattr(anno, "__name__", str(anno)) if anno is not inspect.Parameter.empty else "any"
                params[pname] = {
                    "type": type_name,
                    "required": p.default is inspect.Parameter.empty,
                    "default": None if p.default is inspect.Parameter.empty else p.default,
                }
            specs.append(
                MethodSpec(
                    name=name,
                    description=getattr(member, "_hacoo_description", "") or (inspect.getdoc(member) or "").splitlines()[0] if (getattr(member, "_hacoo_description", "") or inspect.getdoc(member)) else "",
                    capability=getattr(member, "_hacoo_capability", "read"),
                    kind="tool",
                    tool=cls.tool_name,
                    params=params,
                )
            )
        return sorted(specs, key=lambda s: s.name)

    def invoke(self, method: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """T4: 调用并把原生输出规范化为结构化返回。"""
        fn = getattr(self, method, None)
        if fn is None or not method.startswith("api_"):
            raise AttributeError("no such api method: %s" % method)
        result = fn(**arguments)
        if isinstance(result, dict) and ("data" in result or "text" in result or "artifacts" in result):
            return {
                "data": result.get("data"),
                "text": result.get("text"),
                "artifacts": result.get("artifacts") or [],
            }
        if isinstance(result, str):
            return {"data": None, "text": result, "artifacts": []}
        return {"data": result, "text": None, "artifacts": []}
