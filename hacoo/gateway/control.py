"""网关层 — 执行前控制检查 (G1-G4) 与能力级授权 / 多用户主体识别。

  G1 协议验证:  RPC 消息格式符合通信契约
  G2 注册表查找: 拒绝未注册方法 (见 registry.Registry)
  G3 参数检查:  类型 / 数量 / 必填项匹配方法签名
  G4 能力级授权: 调用者 token -> Principal(user, role, capabilities)

多用户模型:
  - 每个 token 对应一个 Principal (用户名 + 角色)
  - 实例/任务按 user 归属, 只有属主或 admin 角色可访问 (见 gateway)
  - HACOO_AUTH_FILE JSON 格式:
      {"tokens": {"tok-alice": {"user": "alice", "role": "engineer"},
                  "tok-admin": "admin"},                # 兼容简写: user=role
       "roles":  {"admin": ["read","write","admin"], ...}}
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set

from hacoo.adapters.base import MethodSpec
from hacoo.comm.protocol import RPCRequest

# 默认角色能力 (可被 HACOO_AUTH_FILE 指定的 JSON 覆盖)
DEFAULT_ROLES: Dict[str, Set[str]] = {
    "admin": {"read", "write", "admin"},
    "engineer": {"read", "write"},
    "viewer": {"read"},
}
DEFAULT_TOKENS: Dict[str, Any] = {"dev-token": {"user": "dev", "role": "admin"}}


class HacooError(Exception):
    """网关错误, code 用于结构化错误报告 (C4/request_id 追踪)。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Principal:
    """调用者主体: 多用户环境下的身份与能力。"""

    user: str
    role: str
    capabilities: frozenset  # type: ignore[valid-type]

    @property
    def is_admin(self) -> bool:
        return "admin" in self.capabilities


def _normalize_tokens(raw: Dict[str, Any], roles: Dict[str, Set[str]]) -> Dict[str, Principal]:
    """兼容两种 token 配置: "tok": "role" 或 "tok": {"user": ..., "role": ...}。"""
    tokens: Dict[str, Principal] = {}
    for token, value in raw.items():
        if isinstance(value, str):
            user, role = value, value
        else:
            user, role = value.get("user", "unknown"), value.get("role", "viewer")
        tokens[token] = Principal(user=user, role=role,
                                  capabilities=frozenset(roles.get(role, set())))
    return tokens


def load_auth(auth_file: Optional[str] = None) -> Dict[str, Any]:
    path = auth_file or os.environ.get("HACOO_AUTH_FILE")
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        roles = {r: set(c) for r, c in cfg.get("roles", {}).items()} or DEFAULT_ROLES
        return {"tokens": _normalize_tokens(cfg.get("tokens", DEFAULT_TOKENS), roles),
                "roles": roles}
    return {"tokens": _normalize_tokens(DEFAULT_TOKENS, DEFAULT_ROLES),
            "roles": DEFAULT_ROLES}


class PreExecutionControl:
    """阶段1: 执行前控制检查。"""

    def __init__(self, auth: Optional[Dict[str, Any]] = None) -> None:
        cfg = auth or load_auth()
        self.tokens: Dict[str, Principal] = cfg["tokens"]
        self.roles: Dict[str, Set[str]] = cfg["roles"]

    def identify(self, token: Optional[str]) -> Principal:
        """token -> Principal; 未认证即拒绝。"""
        principal = self.tokens.get(token or "")
        if principal is None:
            raise HacooError("UNAUTHORIZED",
                             "invalid or missing access token; ask the platform admin "
                             "for a token and pass it via HACOO_TOKEN / metadata.token")
        return principal

    def check_protocol(self, req: RPCRequest) -> None:
        """G1: 协议验证 (from_dict 已做结构校验, 这里校验语义契约)。"""
        if not req.method.startswith("api_"):
            raise HacooError("PROTOCOL_ERROR",
                             "method must be a registered api_* name, got '%s'; "
                             "arbitrary shell execution is not allowed" % req.method)

    def check_arguments(self, spec: MethodSpec, arguments: Dict[str, Any]) -> None:
        """G3: 参数检查 — 必填项 / 未知参数 / 简单类型匹配。"""
        for pname, meta in spec.params.items():
            if meta["required"] and pname not in arguments:
                raise HacooError("ARGUMENT_ERROR",
                                 "missing required argument '%s' for %s "
                                 "(see hacoo_method_detail / api_method_detail)"
                                 % (pname, spec.name))
        unknown = set(arguments) - set(spec.params)
        if unknown and spec.params:
            raise HacooError("ARGUMENT_ERROR",
                             "unknown argument(s) %s for %s; allowed: %s"
                             % (sorted(unknown), spec.name, sorted(spec.params)))
        type_map = {"str": str, "int": int, "float": (int, float), "bool": bool,
                    "dict": dict, "list": list}
        for pname, value in arguments.items():
            meta = spec.params.get(pname)
            if not meta:
                continue
            expected = type_map.get(meta.get("type", "any"))
            if expected is not None and value is not None and not isinstance(value, expected):
                raise HacooError("ARGUMENT_ERROR",
                                 "argument '%s' of %s expects %s, got %s"
                                 % (pname, spec.name, meta["type"], type(value).__name__))

    def authorize(self, spec: MethodSpec, principal: Principal) -> None:
        """G4: 能力级授权。"""
        if spec.capability not in principal.capabilities:
            raise HacooError("UNAUTHORIZED",
                             "user '%s' (role '%s') lacks capability '%s' required by %s"
                             % (principal.user, principal.role, spec.capability, spec.name))
