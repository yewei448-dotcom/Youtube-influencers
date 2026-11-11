"""Read-only Streamlit cooperation history and reusable contextual references."""
import hashlib
import json

import pandas as pd
import streamlit as st

from cooperation_history import API_STATUS, build_history, record_display, text


def _table(records):
    if not records:
        return
    st.dataframe(pd.DataFrame([record_display(record) for record in records]), hide_index=True,
                 width="stretch", column_config={
                     "主页": st.column_config.LinkColumn("主页", display_text="打开主页"),
                     "视频": st.column_config.LinkColumn("合作视频", display_text="打开视频")})


def _trace(records, key):
    if not records:
        return
    with st.expander("核对原始记录与来源行", expanded=False):
        labels = ["%s 第%s行 · %s · %s" % (record["source"], record["source_row"], record["name"] or "未填写达人", record["date"] or "未记录日期") for record in records]
        selected = st.selectbox("查看原始行", range(len(records)), format_func=lambda position: labels[position], key=key + "_trace")
        st.json(records[selected]["raw"])
        st.caption("此处只读；原表的额外列、金额与账号归属均不改写。")


def render_cooperation_history(effect_frame, history_frame=None, library_frame=None, key="cooperation_history", products=None):
    history = build_history(effect_frame, history_frame, library_frame)
    st.caption(API_STATUS)
    st.caption("仅已有合作效果记录计入本页；邮件报价、已发送邀约或加入建联库不代表成交。")
    available = sorted(set(history.products()) | {text(product) for product in (products or []) if text(product)})
    options = ["（全部产品）"] + available
    if any(not record["product"] for record in history.records):
        options.append("（未填写产品）")
    if st.session_state.get(key + "_product") not in options:
        st.session_state[key + "_product"] = options[0]
    left, right = st.columns([1, 2])
    selected = left.selectbox("合作产品", options, key=key + "_product")
    search = right.text_input("搜索频道或邮箱", key=key + "_search", placeholder="达人名、频道 ID 或邮箱")
    records = history.filter_records("" if selected in ("（全部产品）", "（未填写产品）") else selected, search)
    if selected == "（未填写产品）":
        records = [record for record in records if not record["product"]]
    columns = st.columns(3)
    columns[0].metric("本地合作记录数", len(records))
    columns[1].metric("已编号合作数", len({record["cooperation_id"] for record in records if record["cooperation_id"]}))
    columns[2].metric("待核实旧记录", sum(not record["cooperation_id"] for record in records))
    st.caption("记录数按本地原始行统计；缺少编号的旧记录未去重，不等同于已核实的独立合作次数。不同币种不合计、不换算。")
    if any(record["duplicate_note"] for record in records):
        st.warning("存在相同合作 ID 或相同视频/日期/邮箱/产品/形式的多行记录，已完整保留，请核对来源行；不会自动合并 0 与非零金额。")
    if records:
        _table(records)
        _trace(records, key)
    else:
        st.info("当前筛选下没有本地合作记录。未关联外部 API，不能据此判断从未合作。")
    return {"records": records, "record_count": len(records), "api_status": API_STATUS}


def render_cooperation_reference(row, effect_frame, history_frame=None, library_frame=None, product=None,
                                 form=None, context_key="cooperation_reference", expanded=False):
    history = build_history(effect_frame, history_frame, library_frame)
    summary = history.channel_summary(row, product=product, form=form)
    label = "合作历史参考 · %s 条本地记录" % summary["record_count"]
    with st.expander(label, expanded=expanded):
        st.caption(API_STATUS)
        st.caption("历史参考只读，不替换本轮达人报价，不改变邮件或发件账号归属。")
        st.caption("本地记录 %s 条；已编号合作 %s 个；待核实旧记录 %s 条。" %
                   (summary["record_count"], summary["identified_cooperation_count"], summary["unidentified_record_count"]))
        for warning in summary["warnings"]:
            st.caption(warning)
        if summary["records"]:
            st.caption("关联依据：" + "；".join(summary["match_basis"]))
            _table(summary["records"])
        if summary["reference_prices"]:
            st.write("同产品、同合作形式的历史非零价格（逐单参考）")
            _table(summary["reference_prices"])
            st.caption("原记录未填币种的金额明确沿用 USD 旧表口径；不与其他币种混算，也不视作本次报价。")
        else:
            st.caption("暂无同产品、同合作形式的有效非零历史参考价。")
        if summary["contact_sources"]:
            st.write("联系资料与来源")
            st.dataframe(pd.DataFrame([{"联系方式": item["email"], "来源": item["source"], "记录时间": item["recorded_at"] or "未记录"}
                                       for item in summary["contact_sources"]]), hide_index=True, width="stretch")
            st.caption("仅展示已有资料，未验证邮箱可达性，不覆盖当前邮箱。共享邮箱不作为同一达人的判断依据。")
        token = hashlib.sha256(json.dumps([context_key, dict(row), summary["product"], summary["form"]], ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
        _trace(summary["records"], context_key + "_" + token)
    return summary
