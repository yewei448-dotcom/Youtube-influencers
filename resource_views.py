"""Read-only resource list categories; no persistence, mail or network calls."""
import math
import re

import pandas as pd


CATEGORY_LABELS = {"all": "全部达人", "contactable": "可直接建联",
                   "needs_contact": "待补联系方式", "cooperated": "有合作记录"}
_LOCAL = re.compile(r"[A-Za-z0-9_%+-]+(?:\.[A-Za-z0-9_%+-]+)*\Z")
_DOMAIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_LOCAL_RECORDS = re.compile(r"^\s*([0-9]+)\s*条本地记录")


def _has_email(row):
    from email_module import clean_email
    for column in ("邮箱", "email"):
        value = row.get(column)
        if value is None:
            continue
        address = clean_email(value)
        if len(address) > 254 or address.count("@") != 1:
            continue
        local, domain = address.rsplit("@", 1)
        labels = domain.split(".")
        if (len(local) <= 64 and _LOCAL.fullmatch(local) and len(labels) >= 2
                and all(_DOMAIN.fullmatch(label) for label in labels)
                and re.fullmatch(r"[A-Za-z]{2,63}", labels[-1])):
            return True
    return False


def _positive_count(value):
    if value is None or isinstance(value, bool):
        return False
    try:
        value = float(value)
        return math.isfinite(value) and value > 0
    except (TypeError, ValueError, OverflowError):
        return False


def _has_cooperation(row, cooperation, product, form):
    if cooperation is not None:
        try:
            summary = cooperation.channel_summary(row, product=product, form=form)
            return isinstance(summary, dict) and _positive_count(summary.get("record_count"))
        except Exception:
            return False
    value = row.get("__wb_cooperation_summary", "")
    match = _LOCAL_RECORDS.match(value) if isinstance(value, str) else None
    return bool(match and int(match.group(1)) > 0)


def classify_resource_frame(frame, category="all", cooperation=None, product="", form=""):
    """Return a filtered copy and counts without changing rows or columns.

    Email categories partition the original rows; cooperation is an independent
    category and may overlap either email group. No deduplication is performed.
    """
    frame = pd.DataFrame() if frame is None else frame
    if not isinstance(frame, pd.DataFrame):
        frame = pd.DataFrame(frame)
    category = category if category in CATEGORY_LABELS else "all"
    email_positions, cooperation_positions = [], []
    for position, (_, row) in enumerate(frame.iterrows()):
        if _has_email(row):
            email_positions.append(position)
        if _has_cooperation(row, cooperation, product, form):
            cooperation_positions.append(position)
    email_set = set(email_positions)
    positions = {"all": list(range(len(frame))), "contactable": email_positions,
                 "needs_contact": [position for position in range(len(frame)) if position not in email_set],
                 "cooperated": cooperation_positions}
    counts = {name: len(indices) for name, indices in positions.items()}
    return frame.iloc[positions[category]].copy(deep=True), counts


def render_resource_views(frame, prefix, cooperation=None, product="", form=""):
    import streamlit as st
    source = None if isinstance(frame, pd.DataFrame) and "__wb_cooperation_summary" in frame.columns else cooperation
    full, counts = classify_resource_frame(frame, cooperation=source, product=product, form=form)
    category = st.segmented_control(
        label="列表分类", options=list(CATEGORY_LABELS),
        format_func=lambda value: CATEGORY_LABELS[value] + " · " + str(counts[value]),
        key=prefix + "_list_view", default="all", label_visibility="collapsed", width="stretch")
    if category is None or category == "all":
        result = full
    else:
        result, _ = classify_resource_frame(frame, category, source, product, form)
    if result.empty:
        st.info("当前分类暂无达人，可切换其它分类查看。")
    return result
