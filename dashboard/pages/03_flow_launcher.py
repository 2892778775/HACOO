"""P03 Flow 启动台 (SPEC-dashboard §2): 模板启动 / Agent 参与矩阵 / ISF flow 启动。"""
from __future__ import annotations

import streamlit as st

from dashboard import common

st.header("Flow 启动台")
common.refresh_button("p03_refresh")

client = common.client_or_stop()

# ------------------------------------------------------------ 模板选择 + 参数表单

st.subheader("本地 flow 模板")
try:
    templates = client.list_flow_templates()
except Exception as exc:
    common.show_error(exc)
    st.stop()

if not templates:
    st.info("没有可用的 flow 模板。")
else:
    by_name = {t["name"]: t for t in templates}
    template_name = st.selectbox(
        "模板", list(by_name),
        format_func=lambda n: "%s — %s" % (n, by_name[n]["description"]),
        key="p03_template")
    template = by_name[template_name]
    with st.form("p03_flow_form"):
        st.caption("stages: %s" % " → ".join(template.get("stages") or []))
        params = {}
        for key, default in (template.get("params") or {}).items():
            params[key] = st.text_input("参数 %s" % key, value=str(default))
        submitted = st.form_submit_button("启动 flow", type="primary")
    if submitted:
        st.session_state["_p03_arm"] = True
    if st.session_state.get("_p03_arm"):
        st.warning("即将以模板 **%s** 启动 run，参数：%s" % (template_name, params))
        cols = st.columns(2)
        if cols[0].button("确认启动", key="p03_go", type="primary"):
            st.session_state["_p03_arm"] = False
            try:
                run = client.flow_start(template_name, params)
            except Exception as exc:
                common.show_error(exc)
                st.stop()
            st.toast("run %s 完成：%s" % (run["run_id"], run["status"]))
            st.session_state["p02_run_id"] = run["run_id"]
            st.cache_data.clear()
            st.switch_page("pages/02_runs.py")
        if cols[1].button("取消", key="p03_cancel"):
            st.session_state["_p03_arm"] = False
            st.rerun()

st.divider()

# ------------------------------------------------------------ Agent 参与矩阵

st.subheader("Agent 参与矩阵")
try:
    agents = client.list_agents()
except Exception as exc:
    common.show_error(exc)
    agents = []

editable = common.is_admin()
if not editable:
    st.caption("当前身份非管理员，参与矩阵只读——如需调整请联系管理员。")

matrix: dict = {}
if agents:
    rows = [agents[i:i + 3] for i in range(0, len(agents), 3)]
    for row in rows:
        cols = st.columns(3)
        for col, agent in zip(cols, row):
            with col:
                matrix[agent["name"]] = st.checkbox(
                    "%s（%s）" % (agent["name"], agent["stage"]),
                    value=agent["enabled"], key="p03_agent_%s" % agent["name"],
                    disabled=not editable)

if editable and agents:
    if common.confirm_button("应用参与矩阵变更", "p03_matrix"):
        changed = []
        for agent in agents:
            if matrix[agent["name"]] != agent["enabled"]:
                result = common.run_cli("agents",
                                        "enable" if matrix[agent["name"]] else "disable",
                                        agent["name"])
                changed.append("%s -> %s (%s)"
                               % (agent["name"],
                                  "启用" if matrix[agent["name"]] else "停用",
                                  "ok" if result.returncode == 0
                                  else result.stderr.strip()[:100]))
        st.toast("已变更：%s" % ("；".join(changed) if changed else "无变化"))
        st.cache_data.clear()
        st.rerun()

st.divider()

# ------------------------------------------------------------ ISF flow 启动表单

st.subheader("ISF flow 启动")
st.caption("经 isf adapter 在指定 TO 项目环境中执行 fw start；"
           "提交后展示返回的 airflow_urls 与 flow_id。")

with st.form("p03_isf_form"):
    isf_cols = st.columns(2)
    project = isf_cols[0].text_input("project（TO 项目）", value="N3CTV1")
    flow = isf_cols[1].text_input("flow 类型", value="APR+TC+PV")
    block = isf_cols[0].text_input("block", value="BLOCK1")
    flow_path = isf_cols[1].text_input("flow_path", value="/FLOW/PATH/NAME")
    data_version = isf_cols[0].text_input("data_version（可空）")
    design_version = isf_cols[1].text_input("design_version（可空）")
    usertcl = st.text_input("usertcl（可空）")
    isf_submitted = st.form_submit_button("启动 ISF flow", type="primary")

if isf_submitted:
    if not project.strip() or not flow.strip() or not block.strip() \
            or not flow_path.strip():
        st.warning("project / flow / block / flow_path 为必填。")
    else:
        instance_id = None
        try:
            instance_id = client.spawn("isf")
            setup = client.call("api_isf_setup", instance_id=instance_id,
                                arguments={"project": project}, agent="flow")["data"]
            st.caption("ISF 环境：%s（%s 模式）" % (setup["project"], setup["mode"]))
            started = client.call(
                "api_isf_fw_start", instance_id=instance_id, agent="flow",
                arguments={"flow": flow, "block": block, "flow_path": flow_path,
                           "usertcl": usertcl, "data_version": data_version,
                           "design_version": design_version})["data"]
        except Exception as exc:
            common.show_error(exc)
            st.stop()
        finally:
            if instance_id:
                try:
                    client.release(instance_id)
                except Exception:
                    pass
        st.success("flow 已启动：`%s`" % started["flow_id"])
        st.code(started["command"])
        urls = started.get("airflow_urls") or []
        if urls:
            st.markdown("**Airflow 链接**")
            for url in urls:
                st.markdown("- %s" % url)
        else:
            st.caption("（启动 log 中未解析到 Airflow URL；mock 模式下属正常）")
        st.session_state["p03_last_isf"] = started

if st.session_state.get("p03_last_isf"):
    with st.expander("最近一次启动记录"):
        st.json(st.session_state["p03_last_isf"])
