"""P09 基准与兼容性 (SPEC-dashboard §2): bench run / 历史结果 / baseline 对比 / 契约快照。

bench_runs/ 与 baselines/ 目录按 SPEC 约定直接离线读取 (只读不写);
`bench run` 触发是写操作, 离线模式隐藏。
"""
from __future__ import annotations

import glob
import json
import os

import streamlit as st

from dashboard import common


def _load_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


st.header("基准与兼容性")
common.refresh_button("p09_refresh")

bench_runs_dir = os.path.join(common.PROJECT_ROOT, "bench_runs")
baselines_dir = os.path.join(common.PROJECT_ROOT, "baselines")

# ------------------------------------------------------------ bench run 触发 (写操作, 离线隐藏)

if not common.is_offline():
    run_cols = st.columns([2, 2, 4], vertical_alignment="bottom")
    bench_agent = run_cols[0].text_input("只跑指定 Agent（可空）", key="p09_agent")
    bench_tag = run_cols[1].text_input("只跑指定 tag（可空）", key="p09_tag")
    if run_cols[2].button("运行 bench", key="p09_run", type="primary"):
        cli_args = ["bench", "run"]
        if bench_agent.strip():
            cli_args += ["--agent", bench_agent.strip()]
        for tag in [t.strip() for t in bench_tag.split(",") if t.strip()]:
            cli_args += ["--tag", tag]
        with st.spinner("bench 运行中（每个 case 在独立沙箱执行）..."):
            result = common.run_cli(*cli_args)
        st.code(result.stdout[-3000:] or result.stderr[-3000:])
        if result.returncode == 0:
            st.success("bench 全部通过。")
        else:
            st.warning("bench 存在失败 case（退出码 %d）。" % result.returncode)
        st.cache_data.clear()

# ------------------------------------------------------------ 历史 bench_runs 结果 (离线只读)

st.subheader("历史 bench 结果")
run_files = sorted(glob.glob(os.path.join(bench_runs_dir, "*.json")))
if not run_files:
    st.info("bench_runs/ 暂无历史结果。")
else:
    summaries = []
    for path in run_files:
        report = _load_json(path)
        if not report:
            continue
        summaries.append({"文件": os.path.basename(path),
                          "时间": common.fmt_ts(report.get("ts")),
                          "agent": report.get("agent") or "全部",
                          "通过率": "%d/%d" % (report.get("passed", 0),
                                               report.get("total", 0))})
    st.dataframe(summaries, hide_index=True, alt="历史 bench 结果")
    with st.expander("查看单次结果详情"):
        chosen = st.selectbox("结果文件", [os.path.basename(p) for p in run_files],
                              index=len(run_files) - 1, key="p09_run_file")
        report = _load_json(os.path.join(bench_runs_dir, chosen)) or {}
        for case in report.get("cases") or []:
            st.markdown("- %s `%s`（%.2fs）%s"
                        % (common.status_text("pass" if case.get("pass") else "fail"),
                           case.get("id"), case.get("duration_s", 0),
                           (" — " + "; ".join(case.get("failures") or []))
                           if not case.get("pass") else ""))

# ------------------------------------------------------------ baseline 对比视图

st.subheader("Baseline 对比")
baseline_files = sorted(glob.glob(os.path.join(baselines_dir, "*.json")))
if not baseline_files or not run_files:
    st.info("需要 baselines/*.json 与 bench_runs/*.json 同时存在才能对比。")
else:
    cmp_cols = st.columns(2)
    baseline_name = cmp_cols[0].selectbox(
        "Baseline", [os.path.basename(p) for p in baseline_files], key="p09_base")
    run_name = cmp_cols[1].selectbox(
        "对比的 bench 结果", [os.path.basename(p) for p in run_files],
        index=len(run_files) - 1, key="p09_cmp_run")
    baseline = _load_json(os.path.join(baselines_dir, baseline_name)) or {}
    report = _load_json(os.path.join(bench_runs_dir, run_name)) or {}
    old = {cid: bool(v.get("pass"))
           for cid, v in (baseline.get("cases") or {}).items()}
    new = {c["id"]: bool(c.get("pass")) for c in report.get("cases") or []}
    fail_to_pass = sorted(c for c in old if not old[c] and new.get(c))
    pass_to_fail = sorted(c for c in old if old[c] and not new.get(c, False))
    old_rate = baseline.get("pass_rate") or 0.0
    new_rate = report.get("pass_rate") or 0.0
    m_cols = st.columns(3)
    m_cols[0].metric("pass 率", "%.0f%%" % (new_rate * 100),
                     "%+.0f pp" % ((new_rate - old_rate) * 100), border=True)
    m_cols[1].metric("fail → pass", len(fail_to_pass), border=True)
    m_cols[2].metric("pass → fail", len(pass_to_fail), border=True)
    if fail_to_pass:
        st.success("fail → pass: %s" % ", ".join(fail_to_pass))
    if pass_to_fail:
        st.error("pass → fail（回退, 升级不能标记 done）: %s"
                 % ", ".join(pass_to_fail))

# ------------------------------------------------------------ 契约快照状态

st.subheader("契约快照")
try:
    from hacoo import compat
    check = compat.check_snapshots(update_additions=False)
except Exception as exc:
    st.error("契约检查执行失败：%s" % exc)
else:
    if check["ok"] and not check["additions"]:
        st.success("契约快照一致——无破坏性漂移。")
    if check["breaking"]:
        for line in check["breaking"]:
            st.error("BREAKING: %s" % line)
    for line in check["additions"]:
        st.info("addition: %s" % line)
