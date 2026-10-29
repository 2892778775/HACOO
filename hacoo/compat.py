"""契约快照 (SPEC-evolution B.2, 兼容性层) — 旧行为有没有被破坏?

对网关注册表全量 api_* 方法与 MCP 工具表导出快照, 入库到 tests/contracts/;
每次 e2e 重新导出并 diff:

- **删除/改名方法、删除参数、参数 required False->True、参数类型变化、
  capability/kind/tool 变化** -> 破坏性变更, 测试失败
  (必须显式更新快照并在 commit message 声明 BREAKING)
- **新增方法 / 新增可选参数 / required True->False** -> 宽松方向,
  测试给出提示并自动更新快照

快照 key: 通用方法用方法名; 工具方法用 "<tool>:<name>" (不同工具的同名
api_* 方法签名不同, 必须按工具命名空间区分, 与注册表一致)。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

CONTRACTS_DIR = os.path.join("tests", "contracts")
METHODS_SNAPSHOT_FILE = "methods-snapshot.json"
MCP_SNAPSHOT_FILE = "mcp-snapshot.json"


# ------------------------------------------------------------ 导出

def export_methods_snapshot(registry: Any = None) -> Dict[str, Any]:
    """导出网关注册表全量方法契约:
    {key: {params: {p: {type, required}}, capability, kind, tool}}"""
    if registry is None:
        from hacoo.gateway.registry import Registry
        registry = Registry()
    out: Dict[str, Any] = {}
    for m in registry.list():
        key = m["name"] if m["kind"] == "generic" else "%s:%s" % (m["tool"], m["name"])
        out[key] = {
            "params": {p: {"type": v["type"], "required": v["required"]}
                       for p, v in sorted((m.get("params") or {}).items())},
            "capability": m["capability"],
            "kind": m["kind"],
            "tool": m["tool"],
        }
    return dict(sorted(out.items()))


def export_mcp_snapshot() -> Dict[str, Any]:
    """导出 MCP 工具表契约: {tool_name: inputSchema}。"""
    from hacoo.access import mcp_server
    return dict(sorted((t["name"], t["inputSchema"]) for t in mcp_server.TOOLS))


# ------------------------------------------------------------ 比较

def diff_snapshot(old: Dict[str, Any], new: Dict[str, Any],
                  label: str = "entry") -> Tuple[List[str], List[str]]:
    """比较两份快照, 返回 (breaking, additions) 人性化描述列表。

    方法级契约比对外加 MCP 工具级 (inputSchema) 比对都适用:
    值含 "params" 键时按方法契约处理, 否则按 JSON 结构整体比较
    (MCP inputSchema 中 required 列表收窄、属性删除视为破坏)。
    """
    breaking: List[str] = []
    additions: List[str] = []
    for key in sorted(old):
        if key not in new:
            breaking.append("removed %s: %s" % (label, key))
            continue
        breaking.extend(_diff_entry(key, old[key], new[key]))
        additions.extend(_diff_entry_additions(key, old[key], new[key]))
    for key in sorted(new):
        if key not in old:
            additions.append("added %s: %s" % (label, key))
    return breaking, additions


def _diff_entry(key: str, old: Dict[str, Any], new: Dict[str, Any]) -> List[str]:
    """单个条目的破坏性变化 (方法契约)。"""
    out: List[str] = []
    if "params" not in old:  # MCP inputSchema: 整体比较, 任何收窄都算破坏
        if old != new:
            out.extend(_diff_schema(key, old, new))
        return out
    for attr in ("capability", "kind", "tool"):
        if old.get(attr) != new.get(attr):
            out.append("%s: %s changed %s -> %s" % (key, attr, old.get(attr), new.get(attr)))
    old_params = old.get("params") or {}
    new_params = new.get("params") or {}
    for pname in sorted(old_params):
        if pname not in new_params:
            out.append("%s: param '%s' removed" % (key, pname))
            continue
        o, n = old_params[pname], new_params[pname]
        if o.get("type") != n.get("type"):
            out.append("%s: param '%s' type changed %s -> %s"
                       % (key, pname, o.get("type"), n.get("type")))
        if not o.get("required") and n.get("required"):
            out.append("%s: param '%s' became required (was optional)" % (key, pname))
    return out


def _diff_entry_additions(key: str, old: Dict[str, Any], new: Dict[str, Any]) -> List[str]:
    """单个条目的宽松方向变化 (新增可选参数 / required 放宽 / schema 扩展)。"""
    out: List[str] = []
    if "params" not in old:
        if old != new:  # 无破坏性差异但内容有变 -> 宽松方向 (如新增可选 property)
            out.append("%s: schema extended (non-breaking)" % key)
        return out
    old_params = old.get("params") or {}
    new_params = new.get("params") or {}
    for pname in sorted(new_params):
        if pname not in old_params:
            out.append("%s: param '%s' added (%s)" % (key, pname,
                                                      "required" if new_params[pname].get("required")
                                                      else "optional"))
        elif old_params[pname].get("required") and not new_params[pname].get("required"):
            out.append("%s: param '%s' relaxed to optional" % (key, pname))
    return out


def _diff_schema(key: str, old: Any, new: Any, prefix: str = "") -> List[str]:
    """MCP inputSchema 的结构化 diff: 属性删除 / required 收窄 / 类型变化为破坏。"""
    out: List[str] = []
    if isinstance(old, dict) and isinstance(new, dict):
        if old.get("type") != new.get("type"):
            out.append("%s: %stype changed %s -> %s"
                       % (key, prefix, old.get("type"), new.get("type")))
        for prop, sub in (old.get("properties") or {}).items():
            if prop not in (new.get("properties") or {}):
                out.append("%s: %sproperty '%s' removed" % (key, prefix, prop))
            else:
                out.extend(_diff_schema(key, sub, new["properties"][prop],
                                        prefix="%s%s." % (prefix, prop)))
        for req in old.get("required") or []:
            if req not in (new.get("required") or []):
                pass  # required 放宽是宽松方向, 不算破坏
        for req in new.get("required") or []:
            if req not in (old.get("required") or []) \
                    and req in (old.get("properties") or {}):
                out.append("%s: property '%s' became required" % (key, req))
    elif old != new:
        out.append("%s: %sschema changed" % (key, prefix))
    return out


# ------------------------------------------------------------ 读写与校验

def _load(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write(path: str, snapshot: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def write_snapshots(contracts_dir: str = CONTRACTS_DIR) -> List[str]:
    """重建两份快照并写盘, 返回写入路径列表。"""
    methods_path = os.path.join(contracts_dir, METHODS_SNAPSHOT_FILE)
    mcp_path = os.path.join(contracts_dir, MCP_SNAPSHOT_FILE)
    _write(methods_path, export_methods_snapshot())
    _write(mcp_path, export_mcp_snapshot())
    return [methods_path, mcp_path]


def check_snapshots(contracts_dir: str = CONTRACTS_DIR,
                    update_additions: bool = False) -> Dict[str, Any]:
    """重新导出并与入库快照 diff。

    update_additions=True 时, 仅有宽松方向变化会自动更新快照文件
    (测试使用); 破坏性变化永远返回 ok=False 且不更新。
    """
    result = {"ok": True, "breaking": [], "additions": [], "updated": []}
    for fname, exporter, label in (
            (METHODS_SNAPSHOT_FILE, export_methods_snapshot, "method"),
            (MCP_SNAPSHOT_FILE, export_mcp_snapshot, "mcp_tool")):
        path = os.path.join(contracts_dir, fname)
        old = _load(path)
        new = exporter()
        breaking, additions = diff_snapshot(old, new, label=label)
        result["breaking"].extend(breaking)
        result["additions"].extend(additions)
        if breaking:
            result["ok"] = False
        elif additions and update_additions:
            _write(path, new)
            result["updated"].append(path)
    return result
