"""Streamlit creator-monitor view. No API clients, scheduler or mail sending.

The caller supplies the monitor database and the *existing* contact CSV paths.
Only explicit controls change configuration, request background work, or merge
contacts. Rendering/refreshing reads local snapshots and never calls YouTube.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

_CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
_VIDEO = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+\Z")
_BEIJING = ZoneInfo("Asia/Shanghai")


def _text(value):
    return "" if value is None else str(value).strip()


def _token(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:18]


def channel_id_from_input(value):
    """Accept a verified canonical ID/standard channel URL, never guess handles."""
    value = _text(value)
    if _CHANNEL.fullmatch(value):
        return value
    parsed = urlsplit(value)
    if parsed.scheme in ("https", "http") and parsed.hostname in (
        "youtube.com", "www.youtube.com", "m.youtube.com"
    ):
        match = re.fullmatch(r"/channel/(UC[A-Za-z0-9_-]{22})(?:/(?:videos|about|featured))?/?", parsed.path)
        if match:
            return match.group(1)
    raise ValueError("请填写频道 ID（UC 开头）或标准 /channel/UC… 主页链接；不猜测短名对应的频道。")


def safe_homepage(row):
    cid = _text(row.get("channel_id", row.get("频道ID")))
    if _CHANNEL.fullmatch(cid):
        return "https://www.youtube.com/channel/" + cid
    return ""


def safe_video_url(row):
    vid = _text(row.get("video_id"))
    if not _VIDEO.fullmatch(vid):
        value = _text(row.get("video_url"))
        try:
            parsed = urlsplit(value)
        except ValueError:
            return ""
        if parsed.scheme not in ("http", "https"):
            return ""
        if parsed.hostname == "youtu.be":
            vid = parsed.path.strip("/")
        elif parsed.hostname in ("youtube.com", "www.youtube.com", "m.youtube.com"):
            if parsed.path == "/watch":
                vid = parse_qs(parsed.query).get("v", [""])[0]
            elif parsed.path.startswith(("/shorts/", "/live/")):
                vid = parsed.path.split("/")[2]
    return "https://www.youtube.com/watch?v=" + vid if _VIDEO.fullmatch(vid) else ""


def video_title_anchor(row):
    title = html.escape(_text(row.get("title")) or "视频标题暂未返回", quote=True)
    url = safe_video_url(row)
    if not url:
        return "<strong>" + title + "</strong>"
    return '<a href="' + html.escape(url, quote=True) + '" target="_blank" rel="noopener noreferrer" style="font-weight:650;text-decoration:none">' + title + "</a>"


def _parse_time(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def format_time(value):
    parsed = _parse_time(value)
    return parsed.astimezone(_BEIJING).strftime("%Y-%m-%d %H:%M") if parsed else "未记录"


def period_since(period, now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(_BEIJING)
    if period == "今天":
        return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    if period == "近7天":
        return (now - timedelta(days=7)).astimezone(timezone.utc).isoformat()
    return None


def filter_updates(rows, period="全部更新", matched_only=False, now=None):
    since = _parse_time(period_since(period, now))
    output = []
    for row in rows:
        published = _parse_time(row.get("published_at"))
        if since and (published is None or published < since):
            continue
        if matched_only and not row.get("matched_keywords"):
            continue
        output.append(row)
    return output


def number_text(value, decimals=0):
    if value is None or isinstance(value, bool):
        return "—"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "—"
    return format(value, ",." + str(decimals) + "f") if math.isfinite(value) and value >= 0 else "—"


def interval_text(row):
    if row.get("interval_status") == "adjustment":
        return "平台计数调整，暂不计算"
    if row.get("interval_vph") is None:
        return "待下一次采样"
    return number_text(row.get("interval_vph"), 1) + " 次/小时"


def contact_row(row):
    """Keep the monitor product intact; the shared contact store owns mapping."""
    return {
        "频道ID": _text(row.get("channel_id")),
        "频道名称": _text(row.get("name")) or _text(row.get("channel_id")),
        "主页链接": safe_homepage(row),
        "合作产品": _text(row.get("product")),
        "合作类型": "",
        "邮箱": _text(row.get("email")),
        "来源视频": [safe_video_url(row)] if safe_video_url(row) else [],
        "来源词": row.get("matched_keywords") or [],
        "监测关联": [{"product": _text(row.get("product")),
                      "video_id": _text(row.get("video_id")),
                      "kind": "new" if row.get("is_new") else "history",
                      "first_seen_at": row.get("first_seen_at"),
                      "observed_at": row.get("observed_at")}],
    }


def _row_key(row):
    return _text(row.get("product")) + "|" + _text(row.get("channel_id")) + "|" + _text(row.get("video_id"))


def _save_contacts(rows, contact_path, contact_merger):
    try:
        summary = contact_merger(str(contact_path), [contact_row(row) for row in rows])
    except Exception:
        st.error("暂未确认保存成功，请到建联库核对后重试。")
        return False
    added, updated, unchanged, skipped = (int(summary.get(key, 0)) for key in ("added", "updated", "unchanged", "skipped"))
    message = f"新增 {added}，补充关联 {updated}，保留已有 {unchanged}，跳过 {skipped}。"
    if added or updated or unchanged:
        st.success(message + " 加入后在建联库继续议价与处理回信；本页没有发信。")
    elif skipped:
        st.warning(message + " 尚未加入，请先核对身份或合作类型。")
    else:
        st.info("没有新增或关联的联系人记录。")
    for error in summary.get("errors", [])[:5]:
        if isinstance(error, dict):
            reason = error.get("reason", error.get("message", error.get("error", "")))
        else:
            reason = error
        if reason:
            st.caption(_text(reason)[:240])
    return bool(added or updated or unchanged)


def _render_video_rows(rows, prefix, contact_path, contact_merger, history=False):
    if not rows:
        st.caption("尚无可展示的历史视频快照。")
        return
    choices = st.session_state.setdefault(prefix + "_choices", {})
    page_size = 10 if history else 20
    pages = max(1, math.ceil(len(rows) / page_size))
    if pages > 1:
        page = st.selectbox("结果页", list(range(1, pages + 1)), key=prefix + "_page")
    else:
        page = 1
    visible = rows[(page - 1) * page_size:page * page_size]
    st.caption(f"共 {len(rows)} 条，当前显示 {(page - 1) * page_size + 1}–{min(page * page_size, len(rows))} 条。时间均为北京时间。")
    for row in visible:
        identity = _row_key(row)
        widget = prefix + "_pick_" + _token(identity)
        if widget not in st.session_state:
            st.session_state[widget] = bool(choices.get(identity, False))
        with st.container(border=True, key=prefix + "_video_" + _token(identity)):
            pick, video, metrics, action = st.columns([.25, 4.7, 2.0, 1.9], vertical_alignment="top")
            with pick:
                choices[identity] = st.checkbox("选择视频", key=widget, label_visibility="collapsed")
            with video:
                st.markdown('<div class="wb-monitor-video-title">' + video_title_anchor(row) + '</div>', unsafe_allow_html=True)
                st.caption((_text(row.get("name")) or _text(row.get("channel_id"))) + " · " + _text(row.get("product")))
                hits = row.get("matched_keywords") or []
                if hits:
                    st.caption("关键词命中：" + " / ".join(str(value) for value in hits[:2]) + (f" 等 {len(hits)} 个" if len(hits) > 2 else ""))
                else:
                    st.caption("未命中关键词 · 锁定频道更新仍保留")
                if row.get("available") is False:
                    st.caption("视频暂不可用，保留已取得的历史资料。")
                if history:
                    st.caption("历史参考 · 不属于新动态")
                st.caption("发布 " + format_time(row.get("published_at")) + " · 采集 " + format_time(row.get("observed_at")))
                st.caption("邮箱（来源未核验）：" + (_text(row.get("email")) or "未提供，可先加入待补邮箱"))
            with metrics:
                st.markdown('<div class="wb-monitor-video-metrics"><div><span>累计播放</span><strong>' +
                            number_text(row.get("views")) + '</strong></div><div><span>累计均速 VPH</span><strong>' +
                            number_text(row.get("lifetime_vph"), 1) + '</strong></div></div>', unsafe_allow_html=True)
                st.caption("区间 VPH " + interval_text(row))
                if row.get("early_sample"):
                    st.caption("发布不足1小时，速度仅作参考")
            with action:
                home_col, video_col = st.columns(2)
                home = safe_homepage(row)
                if home:
                    home_col.link_button("频道主页", home, icon=":material/open_in_new:", width="stretch")
                url = safe_video_url(row)
                if url:
                    video_col.link_button("打开视频", url, icon=":material/play_circle:", width="stretch")
                if st.button("加入建联库", icon=":material/person_add:", key=prefix + "_add_" + _token(identity), width="stretch"):
                    _save_contacts([row], contact_path, contact_merger)
    selected = [row for row in rows if choices.get(_row_key(row), False)]
    add, note = st.columns([1, 3])
    with add:
        if st.button(f"加入选中（{len(selected)}）", key=prefix + "_batch", icon=":material/group_add:", disabled=not selected, type="primary", width="stretch"):
            _save_contacts(selected, contact_path, contact_merger)
    with note:
        st.caption("按频道 + 产品去重。缺邮箱可先收录；原有邮箱、议价及回信状态由同一个建联库保留。")


def _render_settings(store, product, prefix):
    rules = store.get_rules(product)
    channels = rules.get("channels", [])
    with st.expander("监测配置", expanded=False, icon=":material/settings:"):
        st.markdown("**当前配置产品：" + product + "**")
        with st.form(prefix + "_rules_form"):
            keywords = st.text_area("固定关键词 · 一行一个", "\n".join(rules.get("keywords", [])), height=145, key=prefix + "_words_" + _token(product))
            st.caption("只用于相关性高亮；默认仍展示锁定频道的全部新视频。")
            if channels:
                records = [{"启用": bool(row.get("enabled")), "移除": False,
                            "频道": _text(row.get("name")) or row["channel_id"],
                            "邮箱": _text(row.get("email")), "主页": safe_homepage(row),
                            "_channel_id": row["channel_id"]} for row in channels]
                edited = st.data_editor(pd.DataFrame(records), hide_index=True, width="stretch",
                                        key=prefix + "_channels_" + _token(product),
                                        disabled=["频道", "邮箱", "主页", "_channel_id"],
                                        column_config={"_channel_id": None, "主页": st.column_config.LinkColumn("主页", display_text="打开主页 ↗"),
                                                       "启用": st.column_config.CheckboxColumn("启用"), "移除": st.column_config.CheckboxColumn("移除")})
            else:
                edited = pd.DataFrame()
                st.info("该产品尚未配置锁定达人；不会自动套用其他产品的频道名单。")
            st.caption("取消启用为暂停；移除只解除当前产品的监测关系，保留历史视频及已入建联记录。")
            save_rules = st.form_submit_button("保存名单与关键词", icon=":material/save:")
        if save_rules:
            invalid = [row for row in edited.to_dict("records") if _text(row.get("邮箱")) and not _EMAIL.fullmatch(_text(row.get("邮箱")))]
            if invalid:
                st.error("名单中有非空邮箱格式不正确，请修改后保存。")
            else:
                old = {row["channel_id"]: row for row in channels}
                try:
                    store.set_keywords(product, [line.strip() for line in keywords.splitlines() if line.strip()])
                    for row in edited.to_dict("records"):
                        cid = row["_channel_id"]
                        if row.get("移除"):
                            store.remove_watch(cid, product)
                        elif bool(row["启用"]) != bool(old[cid].get("enabled")) or _text(row.get("邮箱")) != _text(old[cid].get("email")):
                            store.set_watch(cid, product, enabled=bool(row["启用"]), channel=({"email": _text(row.get("邮箱"))} if _text(row.get("邮箱")) != _text(old[cid].get("email")) else None))
                    st.success("监测名单与关键词已保存。")
                    st.rerun()
                except Exception:
                    st.error("设置未能全部保存，请核对后重试。")
        with st.form(prefix + "_new_channel"):
            st.markdown("**新增当前产品的锁定频道**")
            reference = st.text_input("频道 ID 或标准主页链接", placeholder="https://www.youtube.com/channel/UC…")
            name = st.text_input("频道名称（可留空，后台检查后补全）")
            email = st.text_input("邮箱（可留空）")
            if st.form_submit_button("新增锁定频道", icon=":material/add:"):
                try:
                    cid = channel_id_from_input(reference)
                    if email.strip() and not _EMAIL.fullmatch(email.strip()):
                        raise ValueError("邮箱格式不正确；也可以先留空。")
                    store.set_watch(cid, product, channel={"name": name.strip(), "email": email.strip()})
                    st.success("已新增锁定关系。首次检查建立独立基线，不把旧视频当作新动态。")
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))
                except Exception:
                    st.error("暂未保存频道，请重试。")
        schedule = store.get_schedule()
        st.divider()
        st.markdown("**全局检查计划 · 所有已启用频道**")
        with st.form(prefix + "_schedule"):
            enabled = st.toggle("启用每日自动检查", value=bool(schedule.get("enabled", True)))
            try:
                hour, minute = map(int, schedule.get("time", "09:00").split(":"))
                current_time = time(hour, minute)
            except (ValueError, TypeError):
                current_time = time(9)
            check_time = st.time_input("每日检查时间 · 北京时间（Asia/Shanghai）", value=current_time)
            if st.form_submit_button("保存检查计划", icon=":material/schedule:"):
                try:
                    store.set_schedule(time=check_time.strftime("%H:%M"), enabled=enabled)
                    st.success("检查计划已保存，由独立后台工作进程执行。")
                    st.rerun()
                except Exception:
                    st.error("检查计划暂未保存，请重试。")
        st.caption("电脑关机或休眠期间不运行；后台启动后补查。企微通知为后续规划，本页不配置通知或发信。")


def render_monitor_page(db_path, contact_path, seed_path=None, *, request_check=None, store_factory=None, contact_merger=None, product_registry=None):
    """Render inside the caller's existing navigation/tab.

    db_path: independent monitor SQLite. contact_path: existing workbench CSV.
    seed_path: optional idempotent source import, no network. request_check:
    optional zero-argument callback; default only enqueues store.request_check().
    Dependencies can be injected for offline UI tests; workers are never started.
    """
    if not db_path or not contact_path:
        raise ValueError("必须显式传入监测数据库及原建联库路径")
    if store_factory is None:
        from monitor_core import MonitorStore
        store_factory = MonitorStore
    if contact_merger is None:
        from contact_store import merge_contacts
        contact_merger = merge_contacts
    prefix = "monitor_" + _token(str(Path(db_path).resolve()))
    st.markdown('''<style>
    .wb-monitor-video-title { font-size:1.02rem; line-height:1.5; margin-bottom:.32rem; }
    .wb-monitor-video-title a { color:var(--wb-text,#27334D)!important; }
    .wb-monitor-video-title a:hover { color:var(--wb-accent,#7275DF)!important; text-decoration:underline!important; }
    .wb-monitor-video-metrics { display:flex; gap:1.4rem; padding:.15rem 0 .5rem; }
    .wb-monitor-video-metrics>div { min-width:0; }
    .wb-monitor-video-metrics span { display:block; color:var(--wb-muted,#7B87A1); font-size:.7rem; letter-spacing:.02em; }
    .wb-monitor-video-metrics strong { display:block; color:var(--wb-text,#27334D); font-size:1.4rem; font-weight:650; line-height:1.45; font-variant-numeric:tabular-nums; }
    .wb-monitor-overview { padding:.65rem 0 .9rem; border-bottom:1px solid var(--wb-border,#DEE4F1); }
    .wb-monitor-overview span { display:block; color:var(--wb-muted,#7B87A1); font-size:.74rem; margin-bottom:.3rem; }
    .wb-monitor-overview strong { display:block; color:var(--wb-text,#27334D); font-size:1.35rem; font-weight:650; line-height:1.4; font-variant-numeric:tabular-nums; }
    </style>''', unsafe_allow_html=True)
    try:
        store = store_factory(str(db_path))
        if seed_path and not st.session_state.get(prefix + "_seeded"):
            store.seed(str(seed_path))
            st.session_state[prefix + "_seeded"] = True
        if product_registry:
            for name in product_registry:
                store.ensure_product({"ReiBoot": "ReiBoot for iOS"}.get(name, name))
        products = store.products()
    except Exception:
        st.error("监测配置暂时无法读取，请检查数据目录权限后重试。")
        return
    if not products:
        st.info("还没有监测产品配置，请先导入产品与频道清单。")
        return
    product = st.selectbox("合作产品", products, key=prefix + "_product")
    st.caption("锁定频道的新视频直接显示；关键词只作可选相关性筛选。加入后在建联库继续议价与处理回信，本页不发信。")

    @st.fragment(run_every="5s")
    def live_updates():
        try:
            overview = store.overview(product)
            schedule = store.get_schedule()
            channels = store.list_channels(product)
            raw = store.list_updates(product)
        except Exception:
            st.error("监测快照暂时无法读取；当前不能据此判断没有新视频。")
            return
        period_col, match_col, run_col = st.columns([3, 2, 1.6], vertical_alignment="bottom")
        with period_col:
            period = st.radio("视频发布时间", ["全部更新", "今天", "近7天"], horizontal=True, key=prefix + "_period")
        with match_col:
            matched = st.checkbox("只看命中固定关键词", key=prefix + "_matched")
        with run_col:
            busy = bool(overview.get("running") or overview.get("request_pending"))
            if st.button("立即检查全部", icon=":material/refresh:", disabled=busy, key=prefix + "_check", width="stretch", help="请求后台检查所有已启用频道；不会阻塞页面。"):
                try:
                    (request_check or store.request_check)()
                    if overview.get("worker_alive"):
                        st.success("检查请求已排队，结果将自动刷新。")
                    else:
                        st.info("检查请求已保存，后台启动后执行。")
                except Exception:
                    st.error("检查请求暂未保存，请重试。")
        schedule_label = ("每天 " + schedule.get("time", "09:00") + " 北京时间") if schedule.get("enabled") else "每日计划已停用"
        worker_label = "后台检查中" if overview.get("running") else "请求已排队" if overview.get("request_pending") else "后台在线" if overview.get("worker_alive") else "后台尚未启动"
        latest_run = overview.get("run") or {}
        checked_at = latest_run.get("completed_at")
        check_label = "最近一轮完成：" + format_time(checked_at) if checked_at else "首次检查进行中" if overview.get("running") else "尚未完成检查"
        st.caption(schedule_label + "｜" + worker_label + "｜" + check_label)
        filtered = filter_updates(raw, period, matched)
        enabled_count = sum(bool(channel.get("enabled")) for channel in channels)
        summary = st.columns([1, 1, 2])
        for column, label, value in (
                (summary[0], "启用监测 / 锁定频道", f"{enabled_count} / {len(channels)}"),
                (summary[1], "当前筛选 · 新动态", str(len(filtered))),
                (summary[2], "最近成功检查 · 北京时间", format_time(overview.get("last_success")))):
            column.markdown('<div class="wb-monitor-overview"><span>' + html.escape(label) + '</span><strong>' +
                            html.escape(value) + '</strong></div>', unsafe_allow_html=True)
        st.caption(f"本产品锁定 {len(channels)} 个频道（启用 {sum(bool(c.get('enabled')) for c in channels)} 个） · 当前 {len(filtered)} 条新动态")
        if overview.get("status") in ("failed", "partial", "interrupted") or overview.get("failed_count"):
            st.warning(f"全局本轮已完成 {int(overview.get('checked_count') or 0)} 个频道，{int(overview.get('failed_count') or 0)} 个暂未成功；已取得结果照常显示，后台会继续重试。")
        if not channels:
            st.info("该产品尚未配置锁定达人，请展开监测配置新增频道。其他产品的名单不会自动归到本产品。")
        elif not raw:
            pending = sum(not row.get("baseline_complete") for row in channels)
            if pending:
                st.info(f"{pending} 个频道尚待首次基线。首次成功检查的旧视频只进入历史参考，不伪装成新动态。")
            elif overview.get("last_success"):
                st.info("当前没有监测到新增视频。可在下方查看近期视频参考。")
            else:
                st.info("尚无成功检查记录；可以请求后台立即检查。")
        elif not filtered:
            st.info("当前筛选范围没有动态；可切回全部更新或取消关键词筛选，补查到的较早视频仍会保留。")
        if filtered:
            _render_video_rows(filtered, prefix + "_new_" + _token(product), contact_path, contact_merger)
        with st.expander("播放与 VPH 口径", icon=":material/info:"):
            st.caption("累计均速 VPH = 采集时累计播放 ÷ 距发布小时数；区间 VPH = 两次播放增量 ÷ 两次采样间隔。首次采样显示待下一次采样，平台计数减少时暂不计算区间速度。每条都保留发布时间及采集时间。")
        if not raw and channels:
            with st.expander("近期视频参考（历史）", expanded=False, icon=":material/history:"):
                st.caption("首次基线或重新锁定前已存在的视频，仅作参考，不计入上方新动态；链接指向真实视频。")
                try:
                    historical = [row for row in store.list_updates(product, include_history=True) if not row.get("is_new")]
                    _render_video_rows(historical, prefix + "_history_" + _token(product), contact_path, contact_merger, history=True)
                except Exception:
                    st.caption("历史参考暂时无法读取，请重试。")

    live_updates()
    _render_settings(store, product, prefix)
