"""HACOO 可视化操作平台入口 (SPEC-dashboard §1.3)。

启动: python -m hacoo dashboard [--port 8501]
  或: streamlit run dashboard/app.py

架构: 浏览器 -> Streamlit -> HacooClient SDK -> HACOO server (网关)。
server 未运行时自动降级为离线只读模式 (直接读 JSONL, 写操作隐藏)。
"""
from __future__ import annotations

import os
import sys

import streamlit as st

# streamlit run 只把 dashboard/ 加进 sys.path; 补项目根目录使
# `import hacoo` / `from dashboard.common import ...` 可用。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from dashboard import common  # noqa: E402

st.set_page_config(page_title="HACOO 可视化操作平台", layout="wide",
                   page_icon=":material/dashboard:")

# ------------------------------------------------------------ 连接设置 (侧边栏)

st.session_state.setdefault("_server",
                            os.environ.get("HACOO_SERVER", common.DEFAULT_SERVER))
st.session_state.setdefault("_token", os.environ.get("HACOO_TOKEN", "dev-token"))
st.session_state.setdefault("_offline_dir", os.getcwd())

with st.sidebar:
    st.header("连接设置")
    st.text_input("Server 地址", key="_server")
    st.text_input("Token", key="_token", type="password")

server = st.session_state["_server"]
token = st.session_state["_token"]

forced_offline = os.environ.get("HACOO_DASH_OFFLINE") == "1"
who, err_code, err_detail = (None, None, "")
if not forced_offline:
    who, err_code, err_detail = common.try_connect(server, token)

if forced_offline or (who is None and err_code == "UNREACHABLE"):
    # 离线只读模式 (SPEC §6): 仅 P01/P02/P05/P09/P10 可用, 写操作隐藏
    st.session_state["_offline"] = True
    st.session_state["_who"] = None
    with st.sidebar:
        st.text_input("离线数据目录", key="_offline_dir")
    st.warning("离线只读模式——%s，写操作不可用。"
               "仅 总览 / Run 中心 / 知识库 / 基准 / 观测日志 可用。"
               % ("已设置 HACOO_DASH_OFFLINE=1 强制离线" if forced_offline
                  else "server 未运行"))
elif who is None:
    # server 可达但鉴权失败等: 全页面显示中文连接引导, 不抛堆栈 (SPEC §1.4/§3)
    st.session_state["_offline"] = False
    st.session_state["_who"] = None
    st.title("HACOO 可视化操作平台")
    if err_code == "UNAUTHORIZED":
        st.error("身份验证失败：Token 无效。\n\n"
                 "- 请在左侧边栏输入正确的访问令牌（token 由管理员在 "
                 "HACOO_AUTH_FILE 授权配置中分配）\n"
                 "- 本地开发的默认令牌为 `dev-token`")
    else:
        st.error("连接失败（%s）：%s\n\n请检查 server 地址与 token。"
                 % (err_code, err_detail))
    st.stop()
else:
    st.session_state["_offline"] = False
    st.session_state["_who"] = who
    with st.sidebar:
        st.success("已连接：%s（%s）" % (who["user"], who["role"]))
        st.caption("实例占用 %d/%d" % (who["instances_used"],
                                       who["instances_quota"]))

# ------------------------------------------------------------ 导航 (st.navigation + st.Page)

_OFFLINE_PAGES = {"01_overview.py", "02_runs.py", "05_knowledge.py",
                  "09_bench.py", "10_observations.py"}

_GROUP_SPEC = {
    "运行": [
        ("01_overview.py", "总览", ":material/home:"),
        ("02_runs.py", "Run 中心", ":material/playlist_play:"),
        ("03_flow_launcher.py", "Flow 启动台", ":material/rocket_launch:"),
    ],
    "成长闭环": [
        ("04_review.py", "决策复核", ":material/rate_review:"),
        ("05_knowledge.py", "知识库", ":material/menu_book:"),
        ("06_insights.py", "Insight 中心", ":material/lightbulb:"),
    ],
    "治理与运维": [
        ("07_upgrades.py", "升级管理", ":material/upgrade:"),
        ("08_agents.py", "Agent 管理", ":material/smart_toy:"),
        ("09_bench.py", "基准与兼容性", ":material/monitoring:"),
        ("10_observations.py", "观测与日志", ":material/bug_report:"),
    ],
}

pages_nav = {}
for group, specs in _GROUP_SPEC.items():
    pages = [st.Page(os.path.join("pages", fname), title=title, icon=icon)
             for fname, title, icon in specs
             if not st.session_state["_offline"] or fname in _OFFLINE_PAGES]
    if pages:
        pages_nav[group] = pages

nav = st.navigation(pages_nav, position="sidebar")
nav.run()
