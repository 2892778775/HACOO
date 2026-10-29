"""P02 Run 中心 (SPEC-dashboard §2): run 列表 + 问责详情 + 反馈入口 + 指标趋势。"""
from __future__ import annotations

import datetime

import streamlit as st

from dashboard import common

st.header("Run 中心")
common.refresh_button("p02_refresh")

# ------------------------------------------------------------ Run 列表 (按状态/时间筛选)

filter_cols = st.columns([2, 2, 4])
status_filter = filter_cols[0].selectbox(
    "状态筛选", ["全部", "success", "failed", "running"], key="p02_status")
limit = filter_cols[1].number_input("条数", min_value=5, max_value=200, value=20,
                                    step=5, key="p02_limit")

try:
    runs = common.dd("dd_recent_runs", limit=int(limit),
                     status=None if status_filter == "全部" else status_filter)
except Exception as exc:
    common.show_error(exc)
    st.stop()

if not runs:
    st.info("没有符合条件的 run。")
    st.stop()

st.dataframe(
    [{"run_id": r["run_id"], "模板": r["template"], "状态": r["status"],
      "用户": r["user"], "开始时间": common.fmt_ts(r["started_at"]),
      "时长(s)": r["duration_s"], "阶段数": r["n_stages"],
      "错误": r.get("error") or ""} for r in runs],
    hide_index=True, alt="Run 列表")

run_ids = [r["run_id"] for r in runs]
default_idx = (run_ids.index(st.session_state["p02_run_id"])
               if st.session_state.get("p02_run_id") in run_ids else 0)
run_id = st.selectbox("Run 详情", run_ids, index=default_idx, key="p02_run_sel")

# ------------------------------------------------------------ Run 详情 (问责视图)

try:
    trace = common.dd("dd_run_trace", run_id)
except Exception as exc:
    common.show_error(exc)
    st.stop()

run = trace["run"]
st.subheader("Run %s" % run_id)
st.markdown("模板 **%s** ｜ 状态 %s ｜ 用户 `%s`"
            % (run.get("template", "?"),
               common.status_text(run.get("status", "unknown")),
               run.get("user", "?")))
if run.get("error"):
    st.error(run["error"])

# ---- stage 时间线 (横向步骤条, ok/failed/skipped 着色)
stages = run.get("stages") or []
if stages:
    st.markdown("**Stage 时间线**")
    stage_cols = st.columns(len(stages))
    for col, stage in zip(stage_cols, stages):
        with col:
            with st.container(border=True):
                st.markdown("**%s**" % stage.get("name", "?"))
                st.caption("agent: %s" % stage.get("agent", "?"))
                common.status_badge(stage.get("status", "unknown"))
                if stage.get("error"):
                    st.caption(stage["error"][:120])

# ---- 决策表 (每行末尾反馈按钮)
decisions = trace.get("decisions") or []
st.markdown("**决策链（%d 条）**" % len(decisions))


@st.dialog("提交反馈")
def _feedback_dialog(decision) -> None:
    st.caption("决策 `%s`（agent: %s, action: %s）"
               % (decision["decision_id"], decision.get("agent"),
                  decision.get("action")))
    with st.form("p02_feedback_form"):
        content = st.text_area("反馈内容（纠正将落入知识库, weight=5）")
        ftype = st.selectbox("类型", ["correction", "confirmation", "note"])
        tags_raw = st.text_input("标签（逗号分隔, 可空）")
        if st.form_submit_button("提交反馈"):
            if not content.strip():
                st.warning("反馈内容不能为空。")
                return
            try:
                client = common.client_or_stop()
                entry = client.feedback(
                    agent=decision["agent"], content=content,
                    decision_id=decision["decision_id"], feedback_type=ftype,
                    tags=[t.strip() for t in tags_raw.split(",") if t.strip()],
                    run_id=decision.get("run_id"))
            except Exception as exc:
                common.show_error(exc)
                return
            st.toast("反馈已记录：%s（source=%s, weight=%s）"
                     % (entry["id"], entry["source"], entry["weight"]))
            st.cache_data.clear()
            st.rerun()


for dec in decisions:
    with st.container(border=True):
        head = st.columns([2, 2, 3, 1, 2], vertical_alignment="center")
        head[0].markdown("`%s`" % dec["decision_id"])
        head[1].markdown("%s / %s" % (dec.get("agent"), dec.get("action")))
        head[2].caption(dec.get("rationale", ""))
        with head[3]:
            common.status_badge(dec.get("status", "pending"))
        if not common.is_offline():
            if head[4].button("反馈", key="fb_%s" % dec["decision_id"]):
                _feedback_dialog(dec)
        meta = []
        if dec.get("metrics"):
            meta.append("metrics: %s" % dec["metrics"])
        if dec.get("note"):
            meta.append("note: %s" % dec["note"])
        if meta:
            st.caption(" ｜ ".join(meta))

# ---- 关联审计条目 (request_id 级, 折叠)
with st.expander("关联审计条目（%d 条）" % len(trace.get("audit") or [])):
    audit = trace.get("audit") or []
    if audit:
        st.dataframe(audit, hide_index=True, alt="审计条目")
    else:
        st.caption("（server 未开启审计或该 run 无关联条目）")

# ---- 指标趋势图 (该 agent 历史 run 的 WNS/TNS 折线)
st.markdown("**指标趋势**")
agents_in_run = sorted({d.get("agent") for d in decisions if d.get("agent")})
trend_cols = st.columns(2)
trend_agent = trend_cols[0].selectbox("Agent", agents_in_run or ["sta"],
                                      key="p02_trend_agent")
metric_key = trend_cols[1].selectbox("指标", ["wns", "tns", "slack"],
                                     key="p02_trend_metric")
try:
    points = common.dd("dd_metric_trend", trend_agent, metric_key)
except Exception as exc:
    common.show_error(exc)
    points = []
if points:
    st.line_chart(
        [{"时间": datetime.datetime.fromtimestamp(p["ts"]), metric_key: p["value"]}
         for p in points],
        x="时间", y=metric_key,
        alt="%s 历史 %s 趋势" % (trend_agent, metric_key))
    st.caption("数据来自观测层（近 %d 个 run）。" % len(points))
else:
    st.info("暂无 %s 的 %s 指标观测数据。" % (trend_agent, metric_key))
