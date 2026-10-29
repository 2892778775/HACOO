"""集中配置: 所有可运维参数一处管理, 环境变量覆盖, 可选 JSON 配置文件。

优先级: 显式传参 > 环境变量 > HACOO_CONFIG 指定的 JSON > 默认值。

环境变量一览:
    HACOO_HOST / HACOO_PORT            服务监听地址 (默认 127.0.0.1:9877)
    HACOO_IDLE_TIMEOUT_S               实例空闲回收秒数 (默认 1800)
    HACOO_HEARTBEAT_INTERVAL_S         心跳周期秒数 (默认 5)
    HACOO_READY_TIMEOUT_S              后端就绪等待秒数 (默认 20)
    HACOO_AUTO_RESTART                 实例故障自动重启 (默认 1)
    HACOO_MAX_INSTANCES_PER_USER       每用户实例配额 (默认 8)
    HACOO_AUDIT_LOG                    审计日志路径 (默认 logs/hacoo-audit.jsonl, "off" 关闭)
    HACOO_AUTH_FILE                    授权配置 JSON 路径
    HACOO_ADAPTERS_PATH                插件 adapter 目录 (os.pathsep 分隔多个)
    HACOO_CONFIG                       JSON 配置文件路径
    HACOO_AGENTS_FILE                  Agent 启停配置 (默认 hacoo.agents.json)
    HACOO_KNOWLEDGE_DIR                Agent 知识库目录 (默认 knowledge/)
    HACOO_DECISIONS_LOG                决策日志路径 (默认 logs/decisions.jsonl)
    HACOO_RUNS_DIR                     flow run manifest 目录 (默认 runs/)
    HACOO_OBSERVATIONS_DIR             观测存储目录 (默认 observations/)
    HACOO_INSIGHTS_LOG                 insight 存储路径 (默认 logs/insights.jsonl)
    HACOO_KB_MISSES_LOG                知识库未命中日志 (默认 logs/kb-misses.jsonl)
    HACOO_UPGRADES_LOG                 升级提案存储路径 (默认 logs/upgrade-proposals.jsonl)
    HACOO_ISF_SHELL                    ISF 持久 shell 可执行文件 (默认 bash)
    HACOO_ISF_MOCK                     "1" 强制 ISF mock 模式 (测试用)
    HACOO_ISF_STATUS_CMD               ISF flow 状态查询命令模板
    HACOO_DASH_PORT                    Dashboard 端口 (默认 8501; dashboard 为可选组件)
    HACOO_DASH_OFFLINE                 "1" 强制 Dashboard 离线只读模式 (默认 0)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 9877
    idle_timeout_s: float = 1800.0
    heartbeat_interval_s: float = 5.0
    ready_timeout_s: float = 20.0
    auto_restart: bool = True
    max_instances_per_user: int = 8
    audit_log: Optional[str] = "logs/hacoo-audit.jsonl"
    auth_file: Optional[str] = None
    adapters_path: List[str] = field(default_factory=list)
    agents_file: str = "hacoo.agents.json"
    knowledge_dir: str = "knowledge"
    decisions_log: str = "logs/decisions.jsonl"
    runs_dir: str = "runs"
    observations_dir: str = "observations"
    insights_log: str = "logs/insights.jsonl"
    kb_misses_log: str = "logs/kb-misses.jsonl"
    upgrades_log: str = "logs/upgrade-proposals.jsonl"

    @classmethod
    def from_env(cls, config_file: Optional[str] = None) -> "Config":
        cfg: Dict[str, Any] = {}
        path = config_file or os.environ.get("HACOO_CONFIG")
        if path and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                cfg.update(json.load(fh))

        env = os.environ

        def pick(key: str, cast, default):
            raw = env.get(key)
            if raw is None or raw == "":
                return cfg.get(key.lower(), default)
            return cast(raw)

        adapters_env = env.get("HACOO_ADAPTERS_PATH", "")
        adapters = [p for p in adapters_env.split(os.pathsep) if p] or cfg.get("adapters_path", [])
        audit = pick("HACOO_AUDIT_LOG", str, "logs/hacoo-audit.jsonl")
        return cls(
            host=pick("HACOO_HOST", str, "127.0.0.1"),
            port=pick("HACOO_PORT", int, 9877),
            idle_timeout_s=pick("HACOO_IDLE_TIMEOUT_S", float, 1800.0),
            heartbeat_interval_s=pick("HACOO_HEARTBEAT_INTERVAL_S", float, 5.0),
            ready_timeout_s=pick("HACOO_READY_TIMEOUT_S", float, 20.0),
            auto_restart=pick("HACOO_AUTO_RESTART", lambda v: v not in ("0", "false", "no"), True),
            max_instances_per_user=pick("HACOO_MAX_INSTANCES_PER_USER", int, 8),
            audit_log=None if str(audit).lower() in ("off", "none", "") else audit,
            auth_file=env.get("HACOO_AUTH_FILE") or cfg.get("auth_file"),
            adapters_path=adapters,
            agents_file=env.get("HACOO_AGENTS_FILE") or cfg.get("agents_file", "hacoo.agents.json"),
            knowledge_dir=env.get("HACOO_KNOWLEDGE_DIR") or cfg.get("knowledge_dir", "knowledge"),
            decisions_log=env.get("HACOO_DECISIONS_LOG") or cfg.get("decisions_log", "logs/decisions.jsonl"),
            runs_dir=env.get("HACOO_RUNS_DIR") or cfg.get("runs_dir", "runs"),
            observations_dir=env.get("HACOO_OBSERVATIONS_DIR") or cfg.get("observations_dir", "observations"),
            insights_log=env.get("HACOO_INSIGHTS_LOG") or cfg.get("insights_log", "logs/insights.jsonl"),
            kb_misses_log=env.get("HACOO_KB_MISSES_LOG") or cfg.get("kb_misses_log", "logs/kb-misses.jsonl"),
            upgrades_log=env.get("HACOO_UPGRADES_LOG") or cfg.get("upgrades_log", "logs/upgrade-proposals.jsonl"),
        )
