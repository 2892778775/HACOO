"""P07 升级管理 (SPEC-dashboard §2): 状态看板 / 新建提案 / 状态流转。"""
from __future__ import annotations

import streamlit as st

from dashboard import common
from hacoo.upgrades import KINDS, TRANSITIONS

_STATUS_LABELS = {"proposed": "已提案", "accepted": "已立项",
                  "in_progress": "进行中", "done": "已完成", "rejected": "已拒绝"}
_NEXT_LABELS = {"accepted": "立项", "in_progress": "开始实施",
                "done": "完成", "rejected": "拒绝"}

st.header("升级管理")

client = common.client_or_stop()

# ------------------------------------------------------------ 新建提案表单

with st.expander("新建升级提案", expanded=False):
    with st.form("p07_propose_form"):
        title = st.text_input("标题 *")
        p_cols = st.columns(2)
        kind = p_cols[0].selectbox("类型 *", list(KINDS))
        target = p_cols[1].text_input("目标 *（agent/skill/tool 名或 platform）")
        motivation = st.text_area("动机 *")
        acceptance = st.text_input("验收标准（立项前必填, 建议填可验证的 bench case）")
        proposed_change = st.text_area("拟定变更（可空）")
        if st.form_submit_button("创建提案", type="primary"):
            if not (title.strip() and target.strip() and motivation.strip()):
                st.warning("标题 / 目标 / 动机为必填。")
            else:
                try:
                    entry = common.client_or_stop().upgrade_propose(
                        title=title, kind=kind, target=target,
                        motivation=motivation, acceptance=acceptance,
                        proposed_change=proposed_change)
                except Exception as exc:
                    common.show_error(exc)
                else:
                    st.toast("提案 %s 已创建（proposed）" % entry["proposal_id"])
                    st.cache_data.clear()

st.divider()
common.refresh_button("p07_refresh")

# ------------------------------------------------------------ 看板

try:
    board = common.dd("dd_upgrades_by_status")
except Exception as exc:
    common.show_error(exc)
    st.stop()

columns = st.columns(5)
for col, status in zip(columns, ["proposed", "accepted", "in_progress",
                                 "done", "rejected"]):
    with col:
        st.markdown("**%s（%d）**" % (_STATUS_LABELS[status],
                                     len(board.get(status) or [])))
        for proposal in board.get(status) or []:
            with st.container(border=True):
                st.markdown("**%s**" % proposal.get("title", ""))
                st.caption("`%s` ｜ %s ｜ target `%s`"
                           % (proposal["proposal_id"], proposal.get("kind"),
                              proposal.get("target")))
                st.caption("来源: %s ｜ %s"
                           % (proposal.get("source"),
                              common.fmt_ts(proposal.get("ts"))))
                if proposal.get("acceptance"):
                    st.caption("验收: %s" % proposal["acceptance"])
                if proposal.get("version_after"):
                    st.caption("落地版本: %s" % proposal["version_after"])
                with st.expander("详情"):
                    st.markdown("**动机**：%s" % proposal.get("motivation", ""))
                    if proposal.get("proposed_change"):
                        st.markdown("**拟定变更**：%s" % proposal["proposed_change"])
                    for h in proposal.get("history") or []:
                        st.caption("%s: %s → %s %s"
                                   % (common.fmt_ts(h.get("ts")), h.get("from"),
                                      h.get("to"),
                                      ("（%s）" % h["note"]) if h.get("note") else ""))
                # 状态流转按钮: 仅渲染合法流转 (非法流转不显示)
                for nxt in TRANSITIONS.get(status, ()):
                    if st.button(_NEXT_LABELS[nxt],
                                 key="p07_%s_%s" % (proposal["proposal_id"], nxt)):
                        st.session_state["_p07_transition"] = (
                            proposal["proposal_id"], nxt, proposal.get("title", ""))

transition = st.session_state.get("_p07_transition")


@st.dialog("状态流转确认")
def _transition_dialog() -> None:
    transition = st.session_state.get("_p07_transition")
    if not transition:
        st.rerun()
        return
    proposal_id, to_status, title = transition
    st.markdown("将提案 **%s**（`%s`）流转为 **%s**？"
                % (title, proposal_id, _STATUS_LABELS[to_status]))
    with st.form("p07_transition_form"):
        note = st.text_input("备注（可空）")
        version_after = ""
        if to_status == "done":
            version_after = st.text_input("落地版本（如 sta@1.3.0, 建议填写）")
        if st.form_submit_button("确认流转", type="primary"):
            try:
                common.client_or_stop().upgrade_update(
                    proposal_id, to_status, note=note,
                    version_after=version_after or None)
            except Exception as exc:
                common.show_error(exc)
                return
            st.session_state["_p07_transition"] = None
            st.toast("提案 %s -> %s" % (proposal_id, to_status))
            st.cache_data.clear()
            st.rerun()


if transition:
    _transition_dialog()
