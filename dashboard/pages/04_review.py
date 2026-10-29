"""P04 决策复核 (SPEC-dashboard §2): 复核队列 + 批准/打回 + 主动反馈表单。"""
from __future__ import annotations

import streamlit as st

from dashboard import common

st.header("决策复核")

client = common.client_or_stop()

# ------------------------------------------------------------ 主动反馈表单 (顶部)

with st.expander("主动反馈（不绑定具体决策的纠正/建议）", expanded=False):
    try:
        agent_names = [a["name"] for a in client.list_agents()]
    except Exception as exc:
        common.show_error(exc)
        st.stop()
    with st.form("p04_feedback_form"):
        fb_agent = st.selectbox("Agent", agent_names, key="p04_fb_agent")
        fb_type = st.selectbox("类型", ["correction", "confirmation", "note"],
                               key="p04_fb_type")
        fb_content = st.text_area("反馈内容")
        fb_tags = st.text_input("标签（逗号分隔, 可空）")
        if st.form_submit_button("提交反馈", type="primary"):
            if not fb_content.strip():
                st.warning("反馈内容不能为空。")
            else:
                try:
                    entry = client.feedback(
                        agent=fb_agent, content=fb_content, feedback_type=fb_type,
                        tags=[t.strip() for t in fb_tags.split(",") if t.strip()])
                except Exception as exc:
                    common.show_error(exc)
                else:
                    st.toast("反馈已记录：%s（source=%s, weight=%s）"
                             % (entry["id"], entry["source"], entry["weight"]))
                    st.cache_data.clear()

st.divider()
common.refresh_button("p04_refresh")

# ------------------------------------------------------------ 复核队列

queue_agent = st.selectbox("按 Agent 筛选", ["全部"] + agent_names, key="p04_agent")
try:
    items = common.dd("dd_review_queue",
                      None if queue_agent == "全部" else queue_agent)
except Exception as exc:
    common.show_error(exc)
    st.stop()

st.caption("共 %d 条待复核（pending 决策 + 未复核的 agent 知识条目）。" % len(items))
if not items:
    st.success("复核队列已清空。")
    st.stop()


@st.dialog("复核知识条目")
def _review_dialog(entry, action) -> None:
    st.caption("条目 `%s`（agent: %s）" % (entry["id"], entry.get("agent")))
    st.markdown("**现象**：%s" % entry.get("symptom", ""))
    if entry.get("resolution"):
        st.markdown("**解决方案**：%s" % entry["resolution"])
    with st.form("p04_review_form"):
        note = st.text_input("复核备注%s" % ("（打回必填）" if action == "reject"
                                             else "（可空）"))
        if st.form_submit_button("确认打回" if action == "reject" else "确认批准",
                                 type="primary"):
            if action == "reject" and not note.strip():
                st.warning("打回必须填写备注。")
                return
            try:
                common.client_or_stop().knowledge_review(entry["id"], action,
                                                         note=note)
            except Exception as exc:
                common.show_error(exc)
                return
            st.toast("条目 %s 已%s" % (entry["id"],
                                       "批准" if action == "approve" else "打回"))
            st.cache_data.clear()
            st.rerun()


for item in items:
    with st.container(border=True):
        if item["item_type"] == "decision":
            st.markdown("%s **决策** `%s` ｜ agent `%s` ｜ %s / %s"
                        % (common.status_text(item.get("status", "pending")),
                           item["decision_id"], item.get("agent"),
                           item.get("stage"), item.get("action")))
            st.caption("依据：%s ｜ run: %s ｜ %s"
                       % (item.get("rationale", ""), item.get("run_id") or "-",
                          common.fmt_ts(item.get("ts"))))
            st.caption("决策闭环由 flow 引擎/Agent 完成；如需纠正请到 P02 对该决策提交反馈。")
        else:
            st.markdown("%s **知识条目** `%s` ｜ agent `%s` ｜ weight=%s"
                        % (common.status_text(item.get("review_status",
                                                       "unreviewed")),
                           item["id"], item.get("agent"), item.get("weight")))
            st.markdown("**现象**：%s" % item.get("symptom", ""))
            if item.get("root_cause"):
                st.markdown("**根因**：%s" % item["root_cause"])
            if item.get("resolution"):
                st.markdown("**解决方案**：%s" % item["resolution"])
            evidence = []
            if item.get("run_id"):
                evidence.append("run: `%s`" % item["run_id"])
            if item.get("decision_id"):
                evidence.append("decision: `%s`" % item["decision_id"])
            if item.get("insight_id"):
                evidence.append("insight: `%s`" % item["insight_id"])
            if evidence:
                st.caption("证据链接：" + " ｜ ".join(evidence))
            cols = st.columns(2)
            if cols[0].button("批准", key="p04_ap_%s" % item["id"],
                              type="primary"):
                _review_dialog(item, "approve")
            if cols[1].button("打回", key="p04_rj_%s" % item["id"]):
                _review_dialog(item, "reject")
