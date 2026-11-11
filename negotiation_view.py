"""Automatic quote selection and pricing display, without sending messages."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from negotiation_data import build_negotiation_candidates
import pricing
PRICED_PRODUCTS = ("ReiBoot", "iCareFone WhatsApp transfer", "4DDiG")

DRAFT_KEYS = ("nego_subject", "nego_body", "nego_email", "nego_name", "nego_form",
              "nego_source_key", "nego_seen_count", "nego_subject_edit", "nego_body_edit", "confirm_nego",
              "nego_draft", "nego_confirmation")


def clear_negotiation_draft():
    for key in DRAFT_KEYS:
        st.session_state.pop(key, None)


def markdown_text(value):
    """Keep currency signs literal in Streamlit's Markdown/LaTeX renderer."""
    return str(value).replace("$", "\\$")


def evaluate_quote(form, country, views, amount, currency, budget_cap=150, product=None):
    """Only evaluate known inputs in the pricing table's currency."""
    def missing(reason):
        return {"结论": "资料待补齐", "最佳价格": None, "建议价格区间": None,
                "建议动作": "等待数据", "说明": reason}
    if product is not None and product not in PRICED_PRODUCTS:
        return missing("此产品尚未配置价格策略；保留达人报价和播放数据，由你决定对外金额。")
    if amount is None:
        return missing("尚未识别到可直接使用的单项报价；请从达人提供的报价方案中选择。")
    if views is None:
        return missing("尚未匹配到近期视频播放数据，暂不生成价格建议。")
    if not country:
        return missing("频道未公开国家/地区，现有地区报价表暂时无法匹配；不会代填美国。")
    group = pricing.国家映射.get(str(country).strip().lower(),
                              pricing.国家映射.get(str(country).strip(), str(country).strip()))
    table_currency = "EUR" if group == "法语" else "USD"
    if currency != table_currency:
        return missing(f"达人报价为 {currency or '未明确币种'}，该地区报价表使用 {table_currency}；暂不自动换汇比较。")
    result = pricing.评估报价(form, group, views, amount)
    # This campaign's existing dedicated-video ceiling is USD 150.
    if form == "独立视频" and currency == "USD" and amount > budget_cap:
        best = result.get("最佳价格")
        result = dict(result)
        target = min(best, budget_cap) if best is not None else None
        result.update({"结论": "可砍价", "建议动作": "砍价",
                       "最佳价格": target,
                       "说明": (f"建议先议价至 ${target}；" if target is not None else "该地区暂无可匹配的参考价；") +
                               f"本轮独立视频报价 ${amount:,.2f} 超过内部 ${budget_cap} 上限，需议价。"})
    if table_currency == "EUR":
        result = dict(result, 说明=result["说明"].replace("$", "€"))
    return result


def _quote_label(option):
    amount = option.get("amount")
    if amount is None:
        lo, hi = option.get("min_amount", option.get("amount_min")), option.get("max_amount", option.get("amount_max"))
        value = f"{lo}–{hi}" if lo is not None else "金额待确认"
    else:
        value = f"{float(amount):,.2f}".rstrip("0").rstrip(".")
    quantity = option.get("quantity")
    package = f" · {quantity} 条套餐总价" if isinstance(quantity, (int, float)) and quantity > 1 else ""
    if option.get("price_basis") == "package" and not package:
        package = " · 套餐总价"
    return f"{option.get('coop_type') or '合作形式待确认'} · {option.get('currency') or '币种待确认'} {value}{package}"


def display_time(value):
    if not value:
        return "尚未记录"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M") + "（北京时间）"
    except (ValueError, TypeError):
        pass
    return str(value)


def render_quote_selection(mail_log, history, library, refresh_metrics=None):
    """Return the exact invitation and current selected offer for existing actions."""
    candidates = build_negotiation_candidates(mail_log, history, library)
    if not candidates:
        clear_negotiation_draft()
        st.info("还没有提取到报价。请先同步回信；同步后会自动列出已报价达人。")
        return None
    by_id = {row["id"]: row for row in candidates}
    st.write(f"已报价邀约 **{len(candidates)}** 条：")
    summary = pd.DataFrame([{"达人": x["name"], "合作产品": x["product"],
                             "合作形式": x["coop_type"], "报价": " / ".join(_quote_label(o) for o in x["quote_options"]),
                             "近期平均播放": x["avg_views"], "最近回信时间": display_time(x.get("reply_time", ""))}
                            for x in candidates])
    st.dataframe(summary, hide_index=True, use_container_width=True,
                 column_config={"近期平均播放": st.column_config.NumberColumn(format="%d")})
    options = list(by_id)
    # Keep identity in the option value. Names are labels, never lookup keys.
    if st.session_state.get("nego_sel") not in options:
        st.session_state["nego_sel"] = options[0]
    selected_id = st.selectbox("选择要议价的达人", options, key="nego_sel",
        format_func=lambda key: " · ".join([by_id[key]["name"] or "达人名称待补充", by_id[key]["product"] or "产品待确认",
            by_id[key]["coop_type"] or "合作形式待确认", str(by_id[key]["raw_row"].get("发送时间", "")), key[-6:]]))
    candidate = dict(by_id[selected_id])
    if refresh_metrics is not None:
        candidate = refresh_metrics(candidate)
    quote_options = candidate["quote_options"]
    quote = None
    if len(quote_options) == 1:
        quote = quote_options[0]
    elif quote_options:
        matching = [i for i, item in enumerate(quote_options)
                    if item.get("coop_type") == candidate["coop_type"] and item.get("amount") is not None]
        option_indices = list(range(len(quote_options)))
        default_index = matching[0] if len(matching) == 1 else None
        token = hashlib.sha256(json.dumps(quote_options, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:12]
        selected_quote = st.selectbox("选择达人提供的报价方案", option_indices,
            index=default_index, format_func=lambda i: _quote_label(quote_options[i]),
            key="nego_quote_" + selected_id + token,
            placeholder="请选择合作方案；无需手工输入金额")
        if selected_quote is not None:
            quote = quote_options[selected_quote]
    quote = quote or {}
    amount = quote.get("amount")
    currency = quote.get("currency") or ""
    form = quote.get("coop_type") or candidate["coop_type"]
    if form in ("未知", "未识别", "待确认"):
        form = candidate["coop_type"]
    views, country = candidate["avg_views"], candidate["country"]

    signature = (selected_id, json.dumps(quote, sort_keys=True, ensure_ascii=False, default=str))
    if st.session_state.get("nego_active_selection") != signature:
        # A draft or a confirmation for the previous creator must not travel
        # with the newly selected creator or offer.
        clear_negotiation_draft()
        st.session_state["nego_active_selection"] = signature

    p1, p2, p3 = st.columns(3)
    if amount is not None:
        price_display = f"{currency or '币种未明'} {amount:,.2f}".rstrip("0").rstrip(".")
    elif quote:
        price_display = f"{currency or '币种未明'} {quote.get('amount_min', '?')}–{quote.get('amount_max', '?')}"
    else:
        price_display = "待选择方案"
    p1.metric("对方报价", price_display)
    p2.metric("近期平均播放", f"{views:,.0f}" if views is not None else "暂无数据")
    p3.metric("国家 / 合作形式", f"{country or '未公开'} / {form or '待确认'}")
    if quote.get("coop_type") and quote["coop_type"] != candidate["coop_type"]:
        st.caption(f"达人此方案提供的是“{quote['coop_type']}”；原邀约形式为“{candidate['coop_type'] or '未记录'}”。")
    sample_count = candidate.get("views_sample_count")
    sample_label = f"；样本 {int(sample_count)} 条" if isinstance(sample_count, (int, float)) else ""
    metric_basis = "近期长视频均播" if "长视频" in (candidate.get("views_source") or "") else "近期视频均播"
    st.caption((metric_basis if views is not None else "播放数据尚未匹配") + sample_label +
               (f"；更新于 {display_time(candidate['views_time'])}" if candidate.get("views_time") else ""))
    if quote.get("context"):
        st.caption(markdown_text("报价依据：" + str(quote["context"])))
    elif candidate.get("quote_source"):
        st.caption("报价来源：" + str(candidate["quote_source"]))
    st.write("联系邮箱：" + candidate["email"])
    with st.expander("查看最新回信和报价来源"):
        st.caption("最近回信时间：" + display_time(candidate.get("reply_time")))
        st.text(candidate.get("reply_body") or "历史记录尚未保存回信全文；同步后自动补充。")
        source_time = candidate["raw_row"].get("报价来源时间", "")
        if source_time:
            st.caption("报价出现于：" + display_time(source_time))
        source_text = candidate["raw_row"].get("报价来源原文") or candidate["raw_row"].get("报价原文")
        if source_text:
            st.text(str(source_text))
    quantity = quote.get("quantity") or 1
    valid_quantity = isinstance(quantity, (int, float)) and quantity >= 1 and int(quantity) == quantity
    is_package = quote.get("price_basis") == "package" or (valid_quantity and quantity > 1)
    price_complete = valid_quantity and (not is_package or quantity > 1)
    per_unit = amount / quantity if amount is not None and price_complete else None
    evaluation = evaluate_quote(form, country, views, per_unit, currency, product=candidate["product"])
    if is_package and amount is not None:
        if not price_complete:
            evaluation = {"结论": "资料待补齐", "最佳价格": None, "建议价格区间": None,
                          "建议动作": "等待数据", "说明": "这是套餐总价，视频条数或内容组合尚未明确，暂不与单条报价表比较。"}
        else:
            evaluation = dict(evaluation)
            if evaluation.get("最佳价格") is not None:
                evaluation["最佳价格"] = round(evaluation["最佳价格"] * quantity, 2)
            if evaluation.get("建议价格区间"):
                evaluation["建议价格区间"] = tuple(round(x * quantity, 2) for x in evaluation["建议价格区间"])
            evaluation["说明"] = (f"{int(quantity)} 条视频套餐总价 {currency} {amount:,.2f}，折合 {per_unit:,.2f}/条。" +
                                 "按单条评估：" + evaluation["说明"])
    return {"candidate": candidate, "row": pd.Series(candidate["raw_row"]),
            "id": selected_id, "name": candidate["name"], "email": candidate["email"],
            "form": form, "country": country, "views": views, "offer": amount,
            "currency": currency, "evaluation": evaluation,
            "can_accept": amount is not None and price_complete and currency == "USD" and
                          (candidate["product"] not in PRICED_PRODUCTS or form != "独立视频" or per_unit <= 150),
            "can_bargain": amount is not None and price_complete and currency == "USD" and evaluation.get("最佳价格") is not None,
            "quantity": quantity if price_complete else None,
            "is_package": is_package}
