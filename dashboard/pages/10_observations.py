"""P10 观测与日志 (SPEC-dashboard §2): 观测查询 / 错误签名聚类 / 审计 tail。"""
from __future__ import annotations

import streamlit as st

from dashboard import common

st.header("观测与日志")
common.refresh_button("p10_refresh")

# ------------------------------------------------------------ 观测查询

st.subheader("观测查询")
try:
    agent_names = [a["name"] for a in common.dd("dd_agents_status")]
except Exception as exc:
    common.show_error(exc)
    st.stop()

filters = st.columns(4)
f_agent = filters[0].selectbox("Agent", ["全部"] + agent_names, key="p10_agent")
f_tool = filters[1].selectbox(
    "工具", ["全部", "mock_eda", "primetime", "icc2", "innovus", "calibre",
             "voltus", "redhawk_sc", "isf"], key="p10_tool")
f_run = filters[2].text_input("run_id（可空）", key="p10_run")
f_limit = filters[3].number_input("条数", min_value=10, max_value=500,
                                  value=100, step=10, key="p10_limit")

try:
    observations = common.dd("dd_observations",
                             None if f_agent == "全部" else f_agent,
                             None if f_tool == "全部" else f_tool,
                             f_run.strip() or None, int(f_limit))
except Exception as exc:
    common.show_error(exc)
    st.stop()

st.caption("共 %d 条（按时间升序, 取最近 %d 条）。" % (len(observations),
                                                     int(f_limit)))
if observations:
    st.dataframe(
        [{"obs_id": o["obs_id"], "时间": common.fmt_ts(o.get("ts")),
          "agent": o.get("agent"), "tool": o.get("tool"),
          "method": o.get("method"), "run_id": o.get("run_id") or "-",
          "ok": "是" if o.get("ok") else "否",
          "metrics": ", ".join("%s=%s" % kv
                               for kv in (o.get("metrics") or {}).items()),
          "错误签名数": len(o.get("error_signatures") or [])}
         for o in observations],
        hide_index=True, alt="观测记录")
    with st.expander("查看单条观测详情"):
        picked = st.selectbox("obs_id", [o["obs_id"] for o in observations],
                              key="p10_pick")
        st.json([o for o in observations if o["obs_id"] == picked][0])
else:
    st.info("没有符合条件的观测记录。")

# ------------------------------------------------------------ error_signatures 聚类 Top N

st.subheader("错误签名聚类（近 7 天 Top 10）")
try:
    signatures = common.dd("dd_error_signatures",
                           None if f_agent == "全部" else f_agent, 7, 10)
except Exception as exc:
    common.show_error(exc)
    signatures = []
if signatures:
    st.dataframe(
        [{"签名": s["signature"][:100], "出现次数": s["count"],
          "涉及 agent": ", ".join(s["agents"]),
          "最近出现": common.fmt_ts(s["last_ts"])} for s in signatures],
        hide_index=True, alt="错误签名聚类")
else:
    st.success("窗口内无失败签名。")

# ------------------------------------------------------------ 审计日志 tail (只读)

st.subheader("审计日志")
st.caption("审计日志在 server 侧落盘；Dashboard 与 server 同机部署，此处只读 tail。")
audit_limit = st.number_input("审计条数", min_value=10, max_value=500, value=50,
                              step=10, key="p10_audit_limit")
try:
    audit = common.dd("dd_audit_tail", int(audit_limit),
                      f_run.strip() or None)
except Exception as exc:
    common.show_error(exc)
    audit = []
if audit:
    st.dataframe(
        [{"时间": common.fmt_ts(a.get("ts")), "user": a.get("user"),
          "method": a.get("method"), "status": a.get("status"),
          "error_code": a.get("error_code") or "-",
          "agent": a.get("agent") or "-", "run_id": a.get("run_id") or "-",
          "耗时(ms)": a.get("elapsed_ms"), "request_id": a.get("request_id")}
         for a in reversed(audit)],
        hide_index=True, alt="审计日志 tail")
else:
    st.info("审计日志为空或未开启（HACOO_AUDIT_LOG）。")
