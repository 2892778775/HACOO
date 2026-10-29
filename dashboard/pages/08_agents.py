"""P08 Agent 管理 (SPEC-dashboard §2): 状态表 / 启停与 sync (管理员) / 详情展开。"""
from __future__ import annotations

import streamlit as st

from dashboard import common

st.header("Agent 管理")

client = common.client_or_stop()
common.refresh_button("p08_refresh")

try:
    agents = client.list_agents()
except Exception as exc:
    common.show_error(exc)
    st.stop()

# ------------------------------------------------------------ 汇总表

rows = []
stats_by_agent = {}
for agent in agents:
    try:
        stats = common.dd("dd_agent_stats", agent["name"], 30)
    except Exception:
        stats = None
    stats_by_agent[agent["name"]] = stats
    rate = stats["success_rate"] if stats else None
    rows.append({
        "agent": agent["name"], "title": agent["title"], "stage": agent["stage"],
        "version": agent["version"],
        "enabled": "是" if agent["enabled"] else "否",
        "工具白名单": ", ".join(agent.get("tools") or []),
        "知识条目数": stats["knowledge_count"] if stats else "-",
        "近30天决策成功率": ("%.0f%%" % (rate * 100)) if rate is not None else "-",
    })
st.dataframe(rows, hide_index=True, alt="Agent 状态表")

# ------------------------------------------------------------ 启停与 sync (管理员)

st.subheader("启停与同步")
if not common.is_admin():
    st.caption("启停与 sync 仅管理员可用；如需调整请联系管理员。")
else:
    op_cols = st.columns([2, 1, 1, 3], vertical_alignment="bottom")
    op_agent = op_cols[0].selectbox("Agent", [a["name"] for a in agents],
                                    key="p08_op_agent")
    if op_cols[1].button("启用", key="p08_enable"):
        result = common.run_cli("agents", "enable", op_agent)
        if result.returncode == 0:
            st.toast("agent '%s' 已启用" % op_agent)
            st.cache_data.clear()
            st.rerun()
        else:
            st.error(result.stderr.strip() or result.stdout.strip())
    if op_cols[2].button("停用", key="p08_disable"):
        result = common.run_cli("agents", "disable", op_agent)
        if result.returncode == 0:
            st.toast("agent '%s' 已停用" % op_agent)
            st.cache_data.clear()
            st.rerun()
        else:
            st.error(result.stderr.strip() or result.stdout.strip())
    if op_cols[3].button("同步 OpenCode agent 文件", key="p08_sync"):
        result = common.run_cli("agents", "sync")
        if result.returncode == 0:
            st.toast("sync 完成")
            st.code(result.stdout)
        else:
            st.error(result.stderr.strip() or result.stdout.strip())

st.divider()

# ------------------------------------------------------------ 点击展开: 最近决策 + KB Top 10

st.subheader("Agent 详情")
for agent in agents:
    stats = stats_by_agent.get(agent["name"])
    with st.expander("%s — %s（v%s）" % (agent["name"], agent["title"],
                                         agent["version"])):
        st.caption(agent.get("description", ""))
        if stats:
            m_cols = st.columns(4)
            m_cols[0].metric("近30天决策", stats["decisions_total"])
            m_cols[1].metric("成功 / 失败", "%d / %d" % (stats["decisions_ok"],
                                                         stats["decisions_failed"]))
            m_cols[2].metric("知识条目", stats["knowledge_count"])
            m_cols[3].metric("人类纠正", stats["corrections"])
        try:
            decisions = client.decision_query(agent=agent["name"], limit=10)
            knowledge = common.dd("dd_knowledge_list", agent["name"], 100)
        except Exception as exc:
            common.show_error(exc)
            continue
        st.markdown("**最近决策**")
        if decisions:
            st.dataframe(
                [{"decision_id": d["decision_id"], "stage": d.get("stage"),
                  "action": d.get("action"), "status": d.get("status"),
                  "时间": common.fmt_ts(d.get("ts"))} for d in decisions],
                hide_index=True, alt="最近决策")
        else:
            st.caption("（无决策记录）")
        st.markdown("**知识库 Top 10（按 weight × reuse_count）**")
        top = sorted(knowledge,
                     key=lambda e: (e.get("weight", 1) or 0)
                     * (e.get("reuse_count", 0) or 0),
                     reverse=True)[:10]
        if top:
            st.dataframe(
                [{"id": e["id"], "symptom": (e.get("symptom") or "")[:60],
                  "weight": e.get("weight"), "reuse_count": e.get("reuse_count"),
                  "source": e.get("source"),
                  "review_status": e.get("review_status")} for e in top],
                hide_index=True, alt="知识库 Top 10")
        else:
            st.caption("（知识库为空）")
