"""通信层 (C1-C4): 结构化 socket-based RPC 消息协议。

关键数据结构 (SPEC):
  - 每个 RPC 携带唯一 request_id (调用追踪 / 响应匹配 / 错误报告)
  - instance_id 作为路由句柄, 在多次调用间保持不变

消息格式: 4 字节大端长度前缀 + UTF-8 JSON body。
"""
from __future__ import annotations

import json
import socket
import struct
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = "1.0"
MAX_MESSAGE_BYTES = 64 * 1024 * 1024  # 64 MB 上限, 防止畸形包撑爆内存


class ProtocolError(Exception):
    """G1 协议验证失败 / 底层帧错误。"""


@dataclass
class RPCRequest:
    """C1: 结构化 RPC 请求消息。"""

    method: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    instance_id: Optional[str] = None  # C2: 会话路由句柄 (gateway 级方法为 None)
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    metadata: Dict[str, Any] = field(default_factory=dict)  # token / timeout / async 等执行元数据
    version: str = PROTOCOL_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RPCRequest":
        if not isinstance(d, dict):
            raise ProtocolError("RPC message must be a JSON object")
        if not isinstance(d.get("method"), str) or not d["method"]:
            raise ProtocolError("RPC message missing string field 'method'")
        arguments = d.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ProtocolError("field 'arguments' must be an object")
        return cls(
            method=d["method"],
            arguments=arguments,
            instance_id=d.get("instance_id"),
            request_id=d.get("request_id") or uuid.uuid4().hex,
            metadata=d.get("metadata") or {},
            version=d.get("version", PROTOCOL_VERSION),
        )


@dataclass
class RPCResponse:
    """C4/G9: 结构化响应 — 状态 + typed data / text output / raw artifacts。"""

    request_id: str
    status: str = "ok"  # "ok" | "error"
    data: Any = None  # 类型化数据
    text: Optional[str] = None  # 文本输出
    artifacts: List[Dict[str, Any]] = field(default_factory=list)  # 原始工件 (报告/日志路径等)
    error: Optional[Dict[str, str]] = None  # {"code": ..., "message": ...}
    elapsed_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RPCResponse":
        return cls(
            request_id=d.get("request_id", ""),
            status=d.get("status", "ok"),
            data=d.get("data"),
            text=d.get("text"),
            artifacts=d.get("artifacts") or [],
            error=d.get("error"),
            elapsed_ms=d.get("elapsed_ms", 0.0),
        )


# ---------------------------------------------------------------- 帧收发

def send_message(sock: socket.socket, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message exceeds %d bytes" % MAX_MESSAGE_BYTES)
    sock.sendall(struct.pack(">I", len(body)) + body)


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ProtocolError("connection closed while receiving message")
        buf += chunk
    return buf


def recv_message(sock: socket.socket) -> Dict[str, Any]:
    header = _recv_exactly(sock, 4)
    (length,) = struct.unpack(">I", header)
    if length <= 0 or length > MAX_MESSAGE_BYTES:
        raise ProtocolError("invalid message length: %d" % length)
    body = _recv_exactly(sock, length)
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtocolError("malformed JSON message: %s" % exc)


def roundtrip(sock: socket.socket, payload: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
    """发送一条消息并等待响应 (带可选超时)。"""
    if timeout is not None:
        sock.settimeout(timeout)
    try:
        send_message(sock, payload)
        return recv_message(sock)
    finally:
        if timeout is not None:
            sock.settimeout(None)


def now_ms() -> float:
    return time.perf_counter() * 1000.0
