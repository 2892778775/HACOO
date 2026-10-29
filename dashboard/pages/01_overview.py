"""P01 总览 (SPEC-dashboard §2): Agent 状态条 / 最近 Run / 待办计数 / 配额与身份。"""
from __future__ import annotations

import streamlit as st

from dashboard import common

st.header("总览")
common.refresh_button("p01_refresh")

# ------------------------------------------------------------ 配额与身份 (在线)

who = common.current_who()
if who:
    with st.container(horizontal=True):
        st.metric("用户", who["user"], border=True)
        st.metric("角色", who["role"], border=True)
        st.metric("实例占用", "%d/%d" % (who["instances_used"],
                                         who["instances_quota"]), border=True)

# ------------------------------------------------------------ 待办计数

try:
    todos = common.dd("dd_todo_counts")
except Exception as exc:
    common.show_error(exc)
    st.stop()

st.subheader("待办")
todo_cols = st.columns(3)
with todo_cols[0]:
    st.metric("待复核决策", todos["pending_decisions"], border=True)
    if not common.is_offline():
        st.page_link("pages/04_review.py", label="前往决策复核",
                     icon=":material/rate_review:")
with todo_cols[1]:
    st.metric("新 insight", todos["new_insights"], border=True)
    if not common.is_offline():
        st.page_link("pages/06_insights.py", label="前往 Insight 中心",
                     icon=":material/lightbulb:")
with todo_cols[2]:
    st.metric("待立项升级提案", todos["proposed_upgrades"], border=True)
    if not common.is_offline():
        st.page_link("pages/07_upgrades.py", label="前往升级管理",
                     icon=":material/upgrade:")

# ------------------------------------------------------------ Agent 状态条

try:
    agents = common.dd("dd_agents_status")
except Exception as exc:
    common.show_error(exc)
    st.stop()

st.subheader("Agent 状态")
for agent in agents:
    with st.container(border=True):
        cols = st.columns([2, 3, 2, 3], vertical_alignment="center")
        cols[0].markdown("**%s**" % agent["name"])
        cols[1].caption(agent["title"])
        cols[2].caption("v%s" % agent["version"])
        with cols[3]:
            common.status_badge("enabled" if agent["enabled"] else "disabled",
                                "启用" if agent["enabled"] else "停用")

# ------------------------------------------------------------ 最近 Run

st.subheader("最近 Run")
try:
    runs = common.dd("dd_recent_runs", limit=10)
except Exception as exc:
    common.show_error(exc)
    st.stop()

if not runs:
    st.info("暂无 flow run。")
else:
    st.dataframe(
        [{"run_id": r["run_id"], "模板": r["template"], "状态": r["status"],
          "用户": r["user"], "开始时间": common.fmt_ts(r["started_at"]),
          "时长(s)": r["duration_s"], "阶段数": r["n_stages"]} for r in runs],
        hide_index=True, alt="最近 Run 列表")
    selected = st.selectbox("选择 run 查看问责详情",
                            [r["run_id"] for r in runs], key="p01_run_pick")
    if st.button("查看 Run 详情", key="p01_goto_run"):
        st.session_state["p02_run_id"] = selected
        st.switch_page("pages/02_runs.py")
