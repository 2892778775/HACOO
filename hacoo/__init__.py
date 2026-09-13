"""HACOO — Human Agent CO-wOrk eTV design platform.

FluxEDA 五层协议架构的可执行实现:
  access/   访问层   (A1-A3): MCP Server + Python SDK
  comm/     通信层   (C1-C4): socket-based RPC, request_id / instance_id
  gateway/  网关层   (G1-G9): 执行前控制、能力发现、方法分发
  adapters/ 适配层   (T1-T4): api_* 方法注册制, 工具语义适配
  runtime/  运行时层 (R1-R11): 后端实例全生命周期管理
Skills 层: .opencode/skills/ 下的工作流知识编码 (独立于五层)
"""

__version__ = "0.1.0"
