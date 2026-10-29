"""P06 Insight 中心 (SPEC-dashboard §2): 扫描 / 筛选 / confirm / dismiss。"""
from __future__ import annotations

import streamlit as st

from dashboard import common

_KIND_LABELS = {"anomaly": "异常聚类", "trend": "趋势恶化",
                "blind_spot": "知识盲区", "self_issue": "自我问题"}

st.header("Insight 中心")

client = common.client_or_stop()

# ------------------------------------------------------------ 扫描 (顶部)

scan_cols = st.columns([2, 2, 4], vertical_alignment="bottom")
try:
    agent_names = [a["name"] for a in client.list_agents()]
except Exception as exc:
    common.show_error(exc)
    st.stop()
scan_agent = scan_cols[0].selectbox("扫描范围", ["全部启用 Agent"] + agent_names,
                                    key="p06_scan_agent")
window_days = scan_cols[1].number_input("窗口（天）", min_value=1, max_value=90,
                                        value=7, key="p06_window")
if scan_cols[2].button("立即扫描", key="p06_scan", type="primary",
                       icon=":material/refresh:"):
    try:
        result = client.insights_scan(
            agent=None if scan_agent == "全部启用 Agent" else scan_agent,
            window_days=int(window_days))
    except Exception as exc:
        common.show_error(exc)
    else:
        st.session_state["p06_last_scan"] = result
        st.cache_data.clear()
        st.toast("扫描完成：新增 %d 条 insight（%d 条已去重跳过）"
                 % (len(result["created"]), result["skipped"]))

last_scan = st.session_state.get("p06_last_scan")
if last_scan:
    st.caption("上次扫描：新增 %d 条，跳过 %d 条（agents: %s）"
               % (len(last_scan["created"]), last_scan["skipped"],
                  ", ".join(last_scan.get("agents") or [])))
else:
    st.caption("上次扫描：本会话尚未扫描。")

st.divider()
common.refresh_button("p06_refresh")

# ------------------------------------------------------------ 筛选与列表

filters = st.columns(3)
f_agent = filters[0].selectbox("Agent", ["全部"] + agent_names, key="p06_agent")
f_kind = filters[1].selectbox(
    "类型", ["全部"] + list(_KIND_LABELS),
    format_func=lambda k: _KIND_LABELS.get(k, k), key="p06_kind")
f_status = filters[2].selectbox("状态", ["全部", "new", "confirmed", "dismissed"],
                                key="p06_status")

try:
    insights = common.dd(
        "dd_insights",
        None if f_agent == "全部" else f_agent,
        None if f_kind == "全部" else f_kind,
        None if f_status == "全部" else f_status)
except Exception as exc:
    common.show_error(exc)
    st.stop()

st.caption("共 %d 条（self_issue 类置顶标红）。" % len(insights))


@st.dialog("复核 insight")
def _review_dialog(insight, action) -> None:
    st.markdown("**%s**" % insight.get("title", ""))
    st.caption("`%s` ｜ agent `%s` ｜ %s"
               % (insight["insight_id"], insight.get("agent"),
                  _KIND_LABELS.get(insight.get("kind"), insight.get("kind"))))
    with st.form("p06_review_form"):
        note = st.text_input("备注（可空）")
        auto_kb, create_upgrade = True, False
        if action == "confirm":
            auto_kb = st.checkbox("自动生成知识条目（auto_kb）", value=True)
            create_upgrade = st.checkbox("联动生成升级提案（create_upgrade）",
                                         value=False)
        if st.form_submit_button("确认" if action == "confirm" else "忽略",
                                 type="primary"):
            try:
                data = common.client_or_stop().insight_review(
                    insight["insight_id"], action, note=note,
                    auto_kb=auto_kb, create_upgrade=create_upgrade)
            except Exception as exc:
                common.show_error(exc)
                return
            extra = ""
            if data.get("kb_entry_id"):
                extra += "；知识条目 %s" % data["kb_entry_id"]
            if data.get("upgrade_proposal_id"):
                extra += "；升级提案 %s" % data["upgrade_proposal_id"]
            st.toast("insight %s 已%s%s"
                     % (insight["insight_id"],
                        "确认" if action == "confirm" else "忽略", extra))
            st.cache_data.clear()
            st.rerun()


for ins in insights:
    self_issue = ins.get("kind") == "self_issue"
    with st.container(border=True):
        head = ":red-badge[自我问题]" if self_issue else \
            ":orange-badge[%s]" % _KIND_LABELS.get(ins.get("kind"), ins.get("kind"))
        st.markdown("%s **%s** ｜ %s" % (head, ins.get("title", ""),
                                        common.status_text(ins.get("status", "new"))))
        st.caption("`%s` ｜ agent `%s` ｜ 置信度 %.2f ｜ %s"
                   % (ins["insight_id"], ins.get("agent"),
                      ins.get("confidence", 0), common.fmt_ts(ins.get("ts"))))
        if ins.get("detail"):
            st.markdown(ins["detail"])
        if ins.get("evidence"):
            with st.expander("证据（%d 条）" % len(ins["evidence"])):
                st.json(ins["evidence"])
        if ins.get("proposed_action"):
            st.caption("建议动作：%s" % ins["proposed_action"])
        if ins.get("status") == "new":
            cols = st.columns(2)
            if cols[0].button("确认", key="p06_cf_%s" % ins["insight_id"],
                              type="primary"):
                _review_dialog(ins, "confirm")
            if cols[1].button("忽略", key="p06_dm_%s" % ins["insight_id"]):
                _review_dialog(ins, "dismiss")
