"""Dashboard 共享逻辑 (SPEC-dashboard §3 通用 UI 规范)。

页面文件是直接脚本; 连接管理 / 离线判定 / 错误码翻译 / 二次确认 /
刷新 / 状态着色等共享逻辑集中在此模块。

会话状态键 (app.py 初始化):
    _server / _token      连接参数 (侧边栏输入)
    _offline              离线只读模式标志
    _offline_dir          离线数据目录 (默认当前工作目录)
    _who                  whoami 结果 (在线时)
    _last_refresh         上次刷新时间戳
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any, Callable, Dict, Optional

import streamlit as st

import hacoo.dashboard_data as ddmod
from hacoo.access.sdk import HacooClient, HacooClientError

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SERVER = "127.0.0.1:9877"
DATA_TTL_S = 15  # st.cache_data 缓存秒数 (轮询式刷新, 无实时推送)

# SPEC §3: SDK 结构化错误码 -> 中文引导文案 (不直接抛堆栈)
ERROR_GUIDANCE: Dict[str, str] = {
    "UNAUTHORIZED": "身份验证失败：请检查侧边栏 Token 是否正确"
                    "（token 与 HACOO_AUTH_FILE 授权配置对应）。",
    "QUOTA_EXCEEDED": "后端实例配额已满：请先释放不用的实例"
                      "（python -m hacoo instances 查看），或联系管理员调整配额。",
    "AGENT_DISABLED": "该 Agent 未参与本次实验（已禁用）："
                      "请管理员执行 python -m hacoo agents enable <name>。",
    "AGENT_FORBIDDEN": "该 Agent 无权使用此工具（工具白名单限制），请检查调用归属。",
    "NO_INSTANCE": "实例或对象不存在（可能已释放）：请检查输入的 ID 后重试。",
    "UNREGISTERED_METHOD": "方法未注册：可用方法见 python -m hacoo methods。",
    "ARGUMENT_ERROR": "参数错误：请检查表单输入是否完整、合法。",
    "PROTOCOL_ERROR": "协议错误：请确认 SDK 与 server 版本一致。",
    "TIMEOUT": "操作超时：长任务请稍后在 P02 / P10 查看进度。",
    "BACKEND_ERROR": "后端执行失败：请到 P10 观测与日志页排查错误签名。",
    "GATEWAY_ERROR": "网关内部错误：请查看 server 侧日志。",
}

# SPEC §3 颜色语义: ok/success=绿, failed=红, pending/running=黄, skipped/dismissed=灰
STATUS_COLORS: Dict[str, str] = {
    "ok": "green", "success": "green", "approved": "green",
    "confirmed": "green", "done": "green", "pass": "green",
    "failed": "red", "rejected": "red", "fail": "red",
    "pending": "orange", "running": "orange", "new": "orange",
    "proposed": "orange", "in_progress": "orange", "accepted": "blue",
    "unreviewed": "violet",
    "skipped": "gray", "dismissed": "gray", "disabled": "gray",
}


# ------------------------------------------------------------ 连接与模式

@st.cache_resource
def get_client(server: str, token: str) -> HacooClient:
    """SDK client 为共享资源 (长连接), 按 (server, token) 缓存。"""
    return HacooClient(server, token=token)


def try_connect(server: str, token: str):
    """尝试连接并 whoami。返回 (whoami dict 或 None, 错误码或 None, 详情)。"""
    try:
        client = get_client(server, token)
        return client.whoami(), None, ""
    except HacooClientError as exc:
        return None, exc.code, str(exc)
    except (OSError, ConnectionError) as exc:
        return None, "UNREACHABLE", str(exc)


def is_offline() -> bool:
    return bool(st.session_state.get("_offline"))


def offline_dir() -> str:
    return st.session_state.get("_offline_dir") or os.getcwd()


def current_who() -> Optional[Dict[str, Any]]:
    return st.session_state.get("_who")


def is_admin() -> bool:
    who = current_who() or {}
    return who.get("role") == "admin"


def client_or_stop() -> HacooClient:
    """在线模式返回 SDK client; 离线模式提示写不可用并停止渲染。"""
    if is_offline():
        st.warning("离线只读模式下写操作不可用（server 未运行）。")
        st.stop()
    return get_client(st.session_state["_server"], st.session_state["_token"])


# ------------------------------------------------------------ 数据加载 (带缓存)

@st.cache_data(ttl=DATA_TTL_S, show_spinner=False)
def _dd_cached(fn_name: str, ctx, args, kwargs):
    server, token, offline, off_dir = ctx
    fn = getattr(ddmod, fn_name)
    if offline:
        return fn(*args, offline_dir=off_dir, **kwargs)
    return fn(*args, client=get_client(server, token), **kwargs)


def dd(fn_name: str, *args, **kwargs):
    """调数据层函数: 在线走 SDK (缓存 ttl=15s), 离线走 offline_dir 分支。

    用法: dd("dd_recent_runs", limit=10)
    """
    ctx = (st.session_state.get("_server", DEFAULT_SERVER),
           st.session_state.get("_token", ""),
           is_offline(), offline_dir())
    return _dd_cached(fn_name, ctx, args, kwargs)


def refresh_button(key: str = "refresh") -> None:
    """刷新按钮 + 上次刷新时间 (SPEC §3: Streamlit 无自动推送)。"""
    cols = st.columns([1, 3], vertical_alignment="center")
    if cols[0].button("刷新", key=key, icon=":material/refresh:"):
        st.cache_data.clear()
        st.session_state["_last_refresh"] = time.time()
        st.rerun()
    last = st.session_state.get("_last_refresh")
    cols[1].caption("上次刷新：%s" % (time.strftime("%H:%M:%S", time.localtime(last))
                                      if last else "（页面加载时）"))


# ------------------------------------------------------------ 错误与状态展示

def show_error(exc: Exception) -> None:
    """SDK 错误翻译为中文引导文案 (SPEC §3), 不抛堆栈。"""
    if isinstance(exc, HacooClientError):
        guidance = ERROR_GUIDANCE.get(exc.code, "操作失败。")
        st.error("%s\n\n错误码：`%s`" % (guidance, exc.code))
    elif isinstance(exc, (OSError, ConnectionError)):
        st.error("无法连接 HACOO server：%s。请确认 server 已启动"
                 "（python -m hacoo serve），或使用离线只读模式。" % exc)
    else:
        st.error("操作失败：%s" % exc)


def status_badge(status: Any, label: Optional[str] = None) -> None:
    text = str(label if label is not None else status)
    st.badge(text, color=STATUS_COLORS.get(str(status), "gray"))


def status_text(status: Any, label: Optional[str] = None) -> str:
    """内联着色徽标 (markdown 指令, 可嵌入表格外的文本)。"""
    text = str(label if label is not None else status)
    color = STATUS_COLORS.get(str(status), "gray")
    return ":%s-badge[%s]" % (color, text)


def fmt_ts(ts: Any) -> str:
    if not ts:
        return "-"
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return str(ts)


# ------------------------------------------------------------ 写操作确认 (SPEC §3 二次确认)

def confirm_button(label: str, key: str, danger: bool = False) -> bool:
    """两步确认按钮: 第一次点击后变为 确认/取消; 确认后返回 True 一次。

    (SPEC §3 写的 st.confirm 在 Streamlit 中不存在, 用两步确认实现同等语义)
    """
    armed_key = "_confirm_%s" % key
    if st.session_state.get(armed_key):
        cols = st.columns(2)
        ok = cols[0].button("确认%s" % label, key="%s_ok" % key, type="primary")
        cancel = cols[1].button("取消", key="%s_cancel" % key)
        if cancel:
            st.session_state[armed_key] = False
            st.rerun()
        if ok:
            st.session_state[armed_key] = False
            return True
        return False
    if st.button(label, key="%s_ask" % key,
                 type="primary" if not danger else "secondary"):
        st.session_state[armed_key] = True
        st.rerun()
    return False


def run_cli(*cli_args: str) -> subprocess.CompletedProcess:
    """以子进程跑 hacoo CLI (agents enable/disable/sync 等本地管理命令)。"""
    return subprocess.run(
        [sys.executable, "-m", "hacoo"] + list(cli_args),
        capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=300)
