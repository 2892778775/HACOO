"""P05 知识库浏览器 (SPEC-dashboard §2): 检索 + 权重可视化 + human_correction 高亮。"""
from __future__ import annotations

import streamlit as st

from dashboard import common

st.header("知识库浏览器")
common.refresh_button("p05_refresh")

try:
    agent_names = [a["name"] for a in common.dd("dd_agents_status")]
except Exception as exc:
    common.show_error(exc)
    st.stop()

controls = st.columns([2, 3, 3])
agent = controls[0].selectbox("Agent", agent_names, key="p05_agent")
keyword = controls[1].text_input("关键词", key="p05_keyword")
tags_raw = controls[2].text_input("标签（逗号分隔, 可空）", key="p05_tags")

tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
try:
    entries = common.dd("dd_knowledge_search", agent, keyword, tags or None, 50)
except Exception as exc:
    common.show_error(exc)
    st.stop()

st.caption("命中 %d 条（按 weight × reuse_count × 时间排序）。"
           "高亮卡片为 human_correction——工程师教过它的经验。" % len(entries))

for entry in entries:
    human = entry.get("source") == "human_correction"
    title = "%s `%s` ｜ weight=%s ｜ 复用 %s 次" % (
        ":red-badge[人类纠正]" if human else ":blue-badge[%s]" % entry.get("source"),
        entry["id"], entry.get("weight"), entry.get("reuse_count", 0))
    with st.container(border=True):
        st.markdown(title)
        st.markdown("**现象**：%s" % entry.get("symptom", ""))
        if entry.get("root_cause"):
            st.markdown("**根因**：%s" % entry["root_cause"])
        if entry.get("resolution"):
            st.markdown("**解决方案**：%s" % entry["resolution"])
        meta = ["复核: %s" % entry.get("review_status", "unreviewed"),
                "agent_version: %s" % (entry.get("agent_version") or "-"),
                "时间: %s" % common.fmt_ts(entry.get("ts"))]
        if entry.get("tags"):
            meta.append("tags: %s" % ", ".join(entry["tags"]))
        if entry.get("run_id"):
            meta.append("run: `%s`" % entry["run_id"])
        st.caption(" ｜ ".join(meta))
