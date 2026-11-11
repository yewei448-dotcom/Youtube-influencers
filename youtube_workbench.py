"""
YouTube 达人挖掘工作台（本地网页）
运行: python3 -m streamlit run youtube_workbench.py
"""

import os, re, json, time, requests
from contextlib import nullcontext
from datetime import datetime, timezone
import pandas as pd
import streamlit as st
import email_module
from html import unescape
from workbench_io import read_csv_file, write_csv_atomic, csv_transaction
import pricing
from reminder_center import (DELIVERY_FIELDS, ensure_delivery_fields,
                             render_reminder_center, reply_worker, sync_caption)
from uuid import uuid4
from pathlib import Path
from contact_store import load_contacts, merge_contacts, persist_editor_changes, canonical_channel
from workbench_ui import render_navigation, render_filters
from monitor_core import MonitorStore
from monitor_view import render_monitor_page
from negotiation_view import render_quote_selection, markdown_text
from negotiation_metrics import refresh_channel_metrics
from mail_accounts import get_store as get_account_store, account_scope
from template_store import get_template_store
from mail_settings_view import render_account_controls, render_template_manager
from mail_compose_view import render_contact_workspace
from mail_followup_view import render_followup
from workbench_components import section_heading
from resource_views import render_resource_views
from resource_table_style import style_creator_names
from editor_layout_state import seed_editor_layout
from resource_workspace import (TAG_COLUMN, LABEL_COLUMN, COOP_COLUMN, context as review_context,
    decision_store, discovery_frame, decorate, review_columns, clean_review_columns,
    editor_suffix, render_review_controls, render_record_lookup, render_source_videos, merge_history)
from resource_decisions import dedupe_channels
from resource_review_view import render_review_pool, review_version
from review_position import render_review_position
from cooperation_history import build_history
from cooperation_history_view import render_cooperation_history


def product_options():
    return get_template_store().products()

def _load_api_key():
    """从 email_config.ini 读 API key，或从环境变量读（不在代码里放明文）"""
    import configparser
    cfg = configparser.ConfigParser()
    cfg.read("email_config.ini")
    if cfg.has_option("youtube", "api_key"):
        return cfg.get("youtube", "api_key")
    return os.environ.get("YOUTUBE_API_KEY", "")

API_KEY = _load_api_key()
BASE_URL = "https://www.googleapis.com/youtube/v3"
LIBRARY_FILE = "建联库.csv"

COMPETITOR_BRANDS = {
    "tenorshare", "wondershare", "drfone", "dr.fone", "coolmuster",
    "imobie", "imazing", "anytrans", "apeaksoft", "wootechy",
    "mobiletrans", "icarefone", "magfone", "itoolab", "unicool",
    "fonetool", "easeus", "ultfone", "mutsapper", "whatsmover",
    "appgeeker", "imyfone", "stellar data recovery", "tuneskit",
    "datagenius", "washeet", "aomei", "mobikin", "recoverit",
    "anyto", "d-back", "phonerescue", "4ukey", "reiboot", "ianygo",
    "whatsgo", "unlockgo", "idelock", "mobimover", "mobisaver",
    "aiseesoft", "mobietrans", "whatsgo", "fixppo",
}
def is_brand(name):
    return any(b in name.lower() for b in COMPETITOR_BRANDS)

def youtube_text(value):
    return unescape(value) if isinstance(value, str) else value

def display_text_copy(frame):
    """Decode YouTube title entities for display without modifying saved records."""
    shown = frame.copy()
    for column in ("频道名称", "代表视频标题", "视频标题", "标题", "频道"):
        if column in shown.columns:
            shown[column] = shown[column].map(youtube_text)
    return shown

# ============ 建联库 ============

def load_library():
    return load_contacts(LIBRARY_FILE)

def save_to_library(rows_df):
    result = merge_contacts(LIBRARY_FILE, rows_df)
    if result.get("added") or result.get("updated") or result.get("unchanged"):
        st.success(f"新增 {result.get('added', 0)}，补充关联 {result.get('updated', 0)}，保留已有 {result.get('unchanged', 0)}。")
    if result.get("errors"):
        st.warning("部分记录未保存：" + "；".join(map(str, result["errors"])))
    return result

def save_library_full(df, before):
    result = persist_editor_changes(LIBRARY_FILE, before, df)
    if result.get("conflicts"):
        st.warning("部分字段已有较新修改，已保留最新记录。请刷新后查看。")
    return result

HISTORY_FILE = "达人库.csv"

def load_history():
    try:
        return pd.read_csv(HISTORY_FILE, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    except (FileNotFoundError,pd.errors.EmptyDataError):
        return pd.DataFrame()

def save_history(rows_df):
    with csv_transaction(HISTORY_FILE):
        write_csv_atomic(merge_history(load_history(), rows_df), HISTORY_FILE)

EFFECT_FILE = "合作效果.csv"

# 合作效果表标准列（合作方名单 + 金额 + 效果 统一存这一张表）
EFFECT_COLUMNS = ["达人名", "邮箱", "合作产品", "合作类型", "合作日期", "合作金额",
                  "视频链接", "核心关键词", "排名", "发布时间", "播放量", "VPH",
                  "点赞", "评论", "会话数", "下载数", "短链", "短链点击",
                  "二次合作", "记录时间"]
EFFECT_COLUMNS += DELIVERY_FIELDS

def load_cooperation_records():
    """Keep original rows for history provenance; do not synthesize cooperation IDs."""
    try:
        return pd.read_csv(EFFECT_FILE,dtype=str,encoding="utf-8-sig",keep_default_na=False)
    except (FileNotFoundError,pd.errors.EmptyDataError):
        return pd.DataFrame()

def load_effect():
    df = pd.DataFrame()
    if os.path.exists(EFFECT_FILE):
        df = read_csv_file(EFFECT_FILE)
    for c in EFFECT_COLUMNS:
        if c not in df.columns:
            df[c] = ""
    return ensure_delivery_fields(df)

def save_effect(df):
    write_csv_atomic(df, EFFECT_FILE)

# ============ API ============

def api_call(endpoint, params):
    params["key"] = API_KEY
    for attempt in range(3):
        try:
            resp = requests.get(f"{BASE_URL}/{endpoint}", params=params, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                time.sleep(min((2**attempt)*3, 30))
            else:
                return None
        except:
            time.sleep(2)
    return None

def search_api(query, max_results=50):
    params = {
        "part": "snippet", "q": query, "type": "video",
        "maxResults": max_results, "relevanceLanguage": "en",
        "regionCode": "US", "order": "relevance",
    }
    data = api_call("search", params)
    results = []
    if data:
        for item in data.get("items", []):
            vid = item["id"].get("videoId")
            sn = item.get("snippet", {})
            if vid:
                results.append({
                    "vid": vid,
                    "title": youtube_text(sn.get("title", "")),
                    "channel_id": sn.get("channelId", ""),
                    "channel_name": youtube_text(sn.get("channelTitle", "")),
                })
    return results

def api_channels(cids):
    if not cids:
        return {}
    channels = {}
    unique = list(set(cids))
    for i in range(0, len(unique), 50):
        batch = unique[i:i+50]
        data = api_call("channels", {
            "part": "snippet,statistics,brandingSettings,contentDetails",
            "id": ",".join(batch), "maxResults": 50,
        })
        if data:
            for item in data.get("items", []):
                cid = item["id"]
                s = item.get("snippet", {})
                stt = item.get("statistics", {})
                b = item.get("brandingSettings", {})
                cd = item.get("contentDetails", {})
                desc = s.get("description", "") + "\n" + b.get("channel", {}).get("description", "")
                emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,6}(?![a-zA-Z])', desc)
                excluded = {"example.com","test.com","email.com","domain.com","youtube.com"}
                valid = [e for e in emails if not any(d in e.lower() for d in excluded)]
                lang = s.get("defaultLanguage", "")
                channels[cid] = {
                    "title": youtube_text(s.get("title", "")),
                    "subs": int(stt.get("subscriberCount", 0)),
                    "hidden": stt.get("hiddenSubscriberCount", False),
                    "videos": int(stt.get("videoCount", 0)),
                    "country": s.get("country", ""),
                    "is_english": (not lang) or lang == "en",
                    "email": valid[0] if valid else "",
                    "url": f"https://www.youtube.com/channel/{cid}",
                    "uploads": cd.get("relatedPlaylists", {}).get("uploads", ""),
                }
        time.sleep(0.3)
    return channels

def api_channel_active(uploads_id, days):
    if not uploads_id or days <= 0:
        return True
    data = api_call("playlistItems", {"part": "snippet", "playlistId": uploads_id, "maxResults": 5})
    if data:
        for item in data.get("items", []):
            pub = datetime.fromisoformat(item["snippet"]["publishedAt"].replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - pub).days <= days:
                return True
    return False

def api_recent_avg_views(uploads_id, count=6):
    if not uploads_id:
        return 0
    data = api_call("playlistItems", {"part": "contentDetails", "playlistId": uploads_id, "maxResults": count})
    if not data:
        return 0
    vids = [item["contentDetails"]["videoId"] for item in data.get("items", [])]
    if not vids:
        return 0
    vdata = api_call("videos", {"part": "statistics", "id": ",".join(vids), "maxResults": count})
    if not vdata:
        return 0
    views = [int(it["statistics"].get("viewCount", 0)) for it in vdata.get("items", [])]
    if not views:
        return 0
    return round(sum(views) / len(views))

def api_video_stats(vids):
    """返回 {vid: {"views": int, "published": datetime|None}}，用于计算 VPH"""
    if not vids:
        return {}
    stats = {}
    for i in range(0, len(vids), 50):
        batch = vids[i:i+50]
        data = api_call("videos", {"part": "statistics,snippet", "id": ",".join(batch), "maxResults": 50})
        if data:
            for item in data.get("items", []):
                pub = None
                try:
                    pub = datetime.fromisoformat(item["snippet"]["publishedAt"].replace("Z", "+00:00"))
                except Exception:
                    pass
                stats[item["id"]] = {
                    "views": int(item["statistics"].get("viewCount", 0) or 0),
                    "likes": int(item["statistics"].get("likeCount", 0) or 0),
                    "comments": int(item["statistics"].get("commentCount", 0) or 0),
                    "published": pub,
                }
        time.sleep(0.3)
    return stats

def track_hot_topics(keywords, per_kw=10):
    """按关键词搜视频，返回按 VPH 排序的上升视频（产品无关，关键词可录入）"""
    vids = {}
    for kw in keywords:
        results = search_api(kw, per_kw)
        if not results:
            break
        for r in results:
            vid = r["vid"]
            if vid and vid not in vids:
                vids[vid] = {"title": r["title"], "channel": r["channel_name"], "kw": kw}
        time.sleep(0.5)
    stats = api_video_stats(list(vids.keys()))
    out = []
    for vid, info in vids.items():
        s = stats.get(vid, {})
        views = s.get("views", 0)
        pub = s.get("published")
        vph = 0.0
        if pub and views > 0:
            hours = max((datetime.now(timezone.utc) - pub).total_seconds() / 3600, 1.0)
            vph = round(views / hours, 1)
        out.append({
            "标题": info["title"],
            "频道": info["channel"],
            "命中关键词": info["kw"],
            "视频链接": f"https://www.youtube.com/watch?v={vid}",
            "播放量": views,
            "VPH": vph,
            "发布时间": pub.strftime("%Y-%m-%d %H:%M") if pub else "",
        })
    out.sort(key=lambda x: -x["VPH"])
    return out

def api_trending(region="US", category_id=None, max_results=25):
    """YouTube 官方趋势（mostPopular），可按地区 + 分类，含 VPH"""
    params = {"part": "snippet,statistics", "chart": "mostPopular",
              "regionCode": region, "maxResults": max_results}
    if category_id:
        params["videoCategoryId"] = category_id
    data = api_call("videos", params)
    out = []
    if data:
        for item in data.get("items", []):
            sn = item.get("snippet", {})
            stt = item.get("statistics", {})
            pub = None
            try:
                pub = datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00"))
            except Exception:
                pass
            views = int(stt.get("viewCount", 0))
            vph = 0.0
            if pub:
                hours = max((datetime.now(timezone.utc) - pub).total_seconds() / 3600, 1.0)
                vph = round(views / hours, 1)
            out.append({
                "标题": youtube_text(sn.get("title", "")),
                "频道": youtube_text(sn.get("channelTitle", "")),
                "视频链接": f"https://www.youtube.com/watch?v={item['id']}",
                "播放量": views,
                "VPH": vph,
                "发布时间": pub.strftime("%Y-%m-%d %H:%M") if pub else "",
            })
    return out

YOUTUBE_CATEGORIES = {
    "科技": "28", "游戏": "20", "娱乐": "24", "教育": "27",
    "音乐": "10", "体育": "17", "新闻与政治": "25", "电影与动画": "1",
    "汽车": "2", "宠物与动物": "15", "旅行与活动": "19", "人物与博客": "22",
    "搞笑": "23", "日常与造型": "26", "公益": "29",
}

# Google Trends 地区代码（直接作为 geo 使用）
GT_REGIONS = ["US", "GB", "IN", "ES", "BR", "DE", "FR", "JP", "TW", "HK"]

# 竞品品牌默认关键词（可改）
竞品默认关键词 = [
    "dr.fone", "wondershare", "easeus data recovery", "imyfone",
    "imazing", "anytrans", "coolmuster", "mobikin", "aiseesoft",
    "recoverit data recovery", "stellar data recovery", "phonerescue",
]

def _pytrends():
    from pytrends.request import TrendReq
    return TrendReq(hl="en-US", tz=0, timeout=(10, 25))

def google_related_queries(keyword, geo="US"):
    """Google Trends 相关上升查询（rising），返回 (list, None) 或 (None, 错误信息)"""
    try:
        pt = _pytrends()
        pt.build_payload([keyword], timeframe="today 3-m", geo=geo)
        rq = pt.related_queries()
        rising = rq.get(keyword, {}).get("rising")
        if rising is None or rising.empty:
            return [], None
        out = []
        for _, row in rising.head(30).iterrows():
            out.append({"相关查询": row.get("query", ""), "热度": row.get("value", "")})
        return out, None
    except Exception as e:
        return None, f"Google Trends 拉取失败（可能被限流）：{e}"

def google_interest_over_time(keywords, geo="US"):
    """Google Trends 关键词热度走势（最多 5 词），返回 (DataFrame, None) 或 (None, 错误信息)"""
    try:
        pt = _pytrends()
        pt.build_payload(keywords[:5], timeframe="today 3-m", geo=geo)
        df = pt.interest_over_time()
        if df is None or df.empty:
            return None, "没有数据"
        df = df.drop(columns=["isPartial"], errors="ignore")
        return df, None
    except Exception as e:
        return None, f"Google Trends 拉取失败（可能被限流）：{e}"

def google_trending_rss(geo="US"):
    """Google Trends 实时热搜榜（RSS，过去24小时），返回 (list, None) 或 (None, 错误信息)"""
    try:
        r = requests.get(
            f"https://trends.google.com/trending/rss?geo={geo}",
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
        )
        if r.status_code != 200:
            return None, f"Google Trends 返回 {r.status_code}"
        items = re.findall(r"<item>(.*?)</item>", r.text, re.DOTALL)
        out = []
        for i, it in enumerate(items, 1):
            t = re.search(r"<title>(.*?)</title>", it)
            tr = re.search(r"approx_traffic>\s*(.*?)<", it)
            n = re.search(r"news_item_title>(.*?)<", it)
            out.append({
                "排名": i,
                "热搜词": t.group(1).strip() if t else "",
                "热度": tr.group(1).strip() if tr else "",
                "相关新闻": n.group(1).strip() if n else "",
            })
        return out, None
    except Exception as e:
        return None, f"Google 热搜拉取失败（可能被限流）：{e}"

def ga4_run_report(metrics, dimensions=None, start_date="30daysAgo", end_date="today", filter_expr=None):
    """用服务账号拉 GA4 数据（读 email_config.ini 的 [ga4]），返回 (DataFrame, None) 或 (None, 错误)"""
    import configparser
    from google.oauth2 import service_account
    import google.auth.transport.requests as g_requests

    cfg = configparser.ConfigParser()
    cfg.read("email_config.ini")
    cred_file = cfg.get("ga4", "credentials_file", fallback="").strip()
    prop = cfg.get("ga4", "property_id", fallback="").strip()

    if not cred_file or not os.path.exists(cred_file):
        return None, f"找不到服务账号 JSON 文件：{cred_file}（把下载的私钥放到项目目录，或改 email_config.ini）"
    if not prop:
        return None, "未配置 GA4 Property ID（email_config.ini 的 [ga4] property_id）"

    try:
        creds = service_account.Credentials.from_service_account_file(
            cred_file, scopes=["https://www.googleapis.com/auth/analytics.readonly"]
        )
        creds.refresh(g_requests.Request())
        body = {
            "dateRanges": [{"startDate": start_date, "endDate": end_date}],
            "metrics": [{"name": m} for m in metrics],
        }
        if dimensions:
            body["dimensions"] = [{"name": d} for d in dimensions]
        if filter_expr:
            body["dimensionFilter"] = filter_expr
        r = None
        for _attempt in range(3):
            try:
                r = requests.post(
                    f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport",
                    headers={"Authorization": f"Bearer {creds.token}"},
                    json=body,
                    timeout=30,
                )
                break
            except Exception:
                if _attempt == 2:
                    raise
                time.sleep(2)
        if r is None or r.status_code != 200:
            return None, f"GA4 请求失败：HTTP {r.status_code if r else 'N/A'} {(r.text[:200] if r else '')}"
        data = r.json()
        dim_headers = [h.get("name", "") for h in data.get("dimensionHeaders", [])]
        met_headers = [h.get("name", "") for h in data.get("metricHeaders", [])]
        rows = []
        for row in data.get("rows", []):
            d = {}
            for i, dv in enumerate(row.get("dimensionValues", [])):
                if i < len(dim_headers):
                    d[dim_headers[i]] = dv.get("value", "")
            for i, mv in enumerate(row.get("metricValues", [])):
                if i < len(met_headers):
                    d[met_headers[i]] = mv.get("value", "")
            rows.append(d)
        return pd.DataFrame(rows), None
    except Exception as e:
        return None, f"GA4 拉取失败：{e}"

def ga4_channel_conversion(channel_code="yw", days=90):
    """拉取某渠道所有 utm_term 的会话数 + Download 事件数，返回 ({utm_term: {"sessions":, "downloads":}}, None) 或 (None, 错误)"""
    start = f"{days}daysAgo"
    sess_df, e1 = ga4_run_report(
        metrics=["sessions"], dimensions=["sessionManualTerm"],
        start_date=start, end_date="today",
        filter_expr={"filter": {"fieldName": "sessionManualTerm", "stringFilter": {"matchType": "CONTAINS", "value": channel_code}}},
    )
    dl_df, e2 = ga4_run_report(
        metrics=["eventCount"], dimensions=["sessionManualTerm"],
        start_date=start, end_date="today",
        filter_expr={"andGroup": {"expressions": [
            {"filter": {"fieldName": "sessionManualTerm", "stringFilter": {"matchType": "CONTAINS", "value": channel_code}}},
            {"filter": {"fieldName": "eventName", "stringFilter": {"matchType": "EXACT", "value": "Download"}}},
        ]}},
    )
    if e1 or e2:
        return None, e1 or e2
    result = {}
    if sess_df is not None and not sess_df.empty:
        for _, r in sess_df.iterrows():
            t = str(r.get("sessionManualTerm", "")).strip()
            if t:
                result[t] = {"sessions": int(float(r.get("sessions", 0) or 0)), "downloads": 0}
    if dl_df is not None and not dl_df.empty:
        for _, r in dl_df.iterrows():
            t = str(r.get("sessionManualTerm", "")).strip()
            if t:
                result.setdefault(t, {"sessions": 0, "downloads": 0})
                result[t]["downloads"] = int(float(r.get("eventCount", 0) or 0))
    return result, None

def match_influencer_utm(name, utm_map):
    """达人名 → 匹配 utm_term，返回 (sessions, downloads) 或 (0, 0)"""
    n = str(name).strip().lower()
    if not n:
        return 0, 0
    for t, v in utm_map.items():
        tn = t.lower()
        if tn.startswith(n) or n in tn:
            return v["sessions"], v["downloads"]
    return 0, 0

def refresh_influencers(eff, channel_code="yw", days=90, progress=None, status=None):
    """刷新所有达人的 YouTube 数据 + GA4 转化，返回 (更新后的 eff, 未匹配名单, 错误)"""
    utm_map, err = ga4_channel_conversion(channel_code, days)
    if err:
        return eff, [], err
    n = len(eff)
    unmatched = []
    for i in range(n):
        if status:
            status.text(f"刷新达人 {i+1}/{n}")
        link = str(eff.at[i, "视频链接"] or "").strip()
        if link:
            info = fetch_video_info(link)
            if info:
                eff.at[i, "播放量"] = info["views"]
                eff.at[i, "点赞"] = info["likes"]
                eff.at[i, "评论"] = info["comments"]
                eff.at[i, "发布时间"] = info["published"]
                if info["published"] and info["views"] > 0:
                    try:
                        pub_dt = datetime.strptime(info["published"], "%Y-%m-%d")
                        hours = max((datetime.now() - pub_dt).total_seconds() / 3600, 1.0)
                        eff.at[i, "VPH"] = round(info["views"] / hours, 1)
                    except Exception:
                        pass
        name = eff.at[i, "达人名"]
        sess, dls = match_influencer_utm(name, utm_map)
        if sess or dls:
            eff.at[i, "会话数"] = sess
            eff.at[i, "下载数"] = dls
        else:
            unmatched.append(str(name))
        if progress:
            progress.progress((i + 1) / n)
        time.sleep(0.2)
    save_effect(eff)
    return eff, unmatched, None

# ============ 挖掘 ============

def mine(keywords, min_subs, max_subs, english_only, exclude_brand, active_days,
         min_avg_views, min_er, has_email, progress, status):
    def log(msg):
        if status:
            status.text(msg)

    channel_kw = {}
    channel_name = {}
    channel_video = {}
    api_q = 0
    total = len(keywords)
    for i, kw in enumerate(keywords):
        results = search_api(kw, 50)
        if not results:
            log(f"⚠️ 搜索配额用尽，已处理 {api_q}/{total} 个词")
            break
        api_q += 1
        for r in results:
            cid = r["channel_id"]
            channel_kw.setdefault(cid, [])
            if kw not in channel_kw[cid] and len(channel_kw[cid]) < 5:
                channel_kw[cid].append(kw)
            channel_name[cid] = r["channel_name"]
            if cid not in channel_video:
                channel_video[cid] = {"vid": r["vid"], "title": r["title"]}
        if progress:
            progress.progress((i+1)/total * 0.5)
        time.sleep(0.7)

    log(f"搜索完成：{len(channel_kw)} 个候选频道，查频道详情...")

    cdata = api_channels(list(channel_kw.keys()))
    log(f"频道详情完成：{len(cdata)} 个")

    candidates = []
    for cid, ch in cdata.items():
        if english_only and not ch["is_english"]:
            continue
        if exclude_brand and is_brand(ch["title"]):
            continue
        if ch["hidden"] or ch["subs"] < min_subs or ch["subs"] > max_subs:
            continue
        if has_email and not ch["email"]:
            continue
        candidates.append((cid, ch))
    log(f"基础筛选后：{len(candidates)} 个，计算近6条均播/互动率...")

    results = []
    need_views = (min_avg_views > 0 or min_er > 0)
    # VPH：拉取代表视频的播放量 + 发布时间
    rep_vids = [channel_video.get(cid, {}).get("vid", "") for cid, _ in candidates]
    rep_vids = [v for v in rep_vids if v]
    vid_stats = api_video_stats(rep_vids) if rep_vids else {}
    for idx, (cid, ch) in enumerate(candidates):
        avg_views = 0
        if need_views:
            avg_views = api_recent_avg_views(ch.get("uploads", ""), 6)
            time.sleep(0.3)
        er = (avg_views / ch["subs"] * 100) if (ch["subs"] > 0 and avg_views > 0) else 0.0

        if active_days > 0 and not api_channel_active(ch.get("uploads", ""), active_days):
            continue
        if min_avg_views > 0 and avg_views < min_avg_views:
            continue
        if min_er > 0 and er < min_er:
            continue

        v = channel_video.get(cid, {})
        vid = v.get("vid", "")
        vs = vid_stats.get(vid, {})
        vph = 0.0
        if vs.get("published") and vs.get("views", 0) > 0:
            hours = max((datetime.now(timezone.utc) - vs["published"]).total_seconds() / 3600, 1.0)
            vph = round(vs["views"] / hours, 1)
        results.append({
            "频道ID": cid,
            "频道名称": ch["title"],
            "邮箱": ch["email"],
            "主页链接": ch["url"],
            "代表视频标题": v.get("title", ""),
            "代表视频链接": f"https://www.youtube.com/watch?v={vid}" if vid else "",
            "缩略图": f"https://img.youtube.com/vi/{vid}/hqdefault.jpg" if vid else "",
            "订阅数": ch["subs"],
            "近6条均播": avg_views,
            "互动率": round(er, 1),
            "VPH": vph,
            "命中关键词": " | ".join(channel_kw.get(cid, [])),
        })
        if progress:
            progress.progress(0.5 + (idx+1)/len(candidates) * 0.5)

    results.sort(key=lambda x: (-x["近6条均播"], -x["订阅数"]))
    return results

# ============ 相似视频（爬虫，不耗搜索配额） ============

def extract_video_id(link):
    link = link.strip()
    if not link:
        return None
    m = re.search(r'(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([\w-]{11})', link)
    if m:
        return m.group(1)
    if re.match(r'^[\w-]{11}$', link):
        return link
    return None

def scrape_related_videos(video_id):
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        r = s.get(f"https://www.youtube.com/watch?v={video_id}&hl=en", timeout=15)
        if r.status_code != 200:
            return []
    except:
        return []
    m = re.search(r'var ytInitialData\s*=\s*(.+?);</script>', r.text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
        sections = data['contents']['twoColumnWatchNextResults']['secondaryResults']['secondaryResults']['results']
    except:
        return []
    out = []
    for sec in sections:
        for item in sec.get('itemSectionRenderer', {}).get('contents', []):
            lv = item.get('lockupViewModel')
            if not lv:
                continue
            sources = lv.get('contentImage', {}).get('thumbnailViewModel', {}).get('image', {}).get('sources', [])
            vid = ''
            if sources:
                mm = re.search(r'/vi/([\w-]{11})/', sources[0]['url'])
                if mm:
                    vid = mm.group(1)
            meta = lv.get('metadata', {}).get('lockupMetadataViewModel', {})
            title = meta.get('title', {}).get('content', '')
            rows = meta.get('metadata', {}).get('contentMetadataViewModel', {}).get('metadataRows', [])
            channel = views = age = ''
            if len(rows) >= 1:
                p0 = rows[0].get('metadataParts', [])
                if p0:
                    channel = p0[0].get('text', {}).get('content', '')
            if len(rows) >= 2:
                p1 = rows[1].get('metadataParts', [])
                if len(p1) >= 1:
                    views = p1[0].get('text', {}).get('content', '')
                if len(p1) >= 2:
                    age = p1[1].get('text', {}).get('content', '')
            ch_id = ch_url = ''
            try:
                onTap = meta['image']['decoratedAvatarViewModel']['rendererContext']['commandContext']['onTap']
                be = onTap['innertubeCommand'].get('browseEndpoint', {})
                ch_id = be.get('browseId', '')
                ch_url = be.get('canonicalBaseUrl', '')
            except:
                pass
            out.append({
                "vid": vid, "title": youtube_text(title), "channel": youtube_text(channel),
                "channel_id": ch_id,
                "channel_url": f"https://www.youtube.com{ch_url}" if ch_url else "",
                "views": views, "age": age,
            })
    return out

def resolve_channel_first_video(channel_url):
    """频道主页链接 → 频道第一条视频ID（用于拿它去推荐相似视频）"""
    s = channel_url.strip()
    ch_id = None
    handle = None
    m = re.search(r'channel/(UC[\w-]{22})', s)
    if m:
        ch_id = m.group(1)
    else:
        m2 = re.search(r'/@([\w.-]+)', s)
        if m2:
            handle = '@' + m2.group(1)
    if not ch_id and not handle:
        if s.startswith('@'):
            handle = s
        elif re.match(r'^UC[\w-]{22}$', s):
            ch_id = s
    if handle:
        data = api_call("channels", {"part": "contentDetails", "forHandle": handle})
    elif ch_id:
        data = api_call("channels", {"part": "contentDetails", "id": ch_id})
    else:
        return None
    if not data or not data.get("items"):
        return None
    uploads = data["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    pl = api_call("playlistItems", {"part": "contentDetails", "playlistId": uploads, "maxResults": 1})
    if not pl or not pl.get("items"):
        return None
    return pl["items"][0]["contentDetails"]["videoId"]

def fetch_video_info(link):
    """从视频链接自动拉取 播放量/点赞/评论/发布时间/VPH，返回 dict 或 None"""
    vid = extract_video_id(link)
    if not vid:
        return None
    stats = api_video_stats([vid])
    s = stats.get(vid, {})
    if not s:
        return None
    pub = s.get("published")
    views = s.get("views", 0)
    vph = 0.0
    if pub and views > 0:
        hours = max((datetime.now(timezone.utc) - pub).total_seconds() / 3600, 1.0)
        vph = round(views / hours, 1)
    return {
        "views": views,
        "likes": s.get("likes", 0),
        "comments": s.get("comments", 0),
        "published": pub.strftime("%Y-%m-%d") if pub else "",
        "vph": vph,
    }

@st.cache_data(ttl=21600, show_spinner=False)
def cached_negotiation_metrics(homepage):
    return refresh_channel_metrics(homepage, api_call)


def automatic_negotiation_metrics(candidate):
    updated = dict(candidate)
    # A dated successful lookup or an existing complete row needs no request.
    current = candidate.get("avg_views") is not None and candidate.get("country")
    stamp = candidate.get("views_time")
    if current and stamp:
        try:
            measured = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            current = measured.tzinfo is None or (datetime.now(timezone.utc) - measured).total_seconds() < 21600
        except (TypeError, ValueError):
            pass
    if current:
        return updated
    homepage = candidate.get("homepage")
    if not homepage:
        return updated
    with st.spinner("正在自动匹配频道数据..."):
        fresh = cached_negotiation_metrics(homepage)
    if fresh is not None:
        if fresh.get("avg_views") is not None:
            updated.update(avg_views=fresh["avg_views"],
                           views_sample_count=fresh["sample_count"],
                           views_time=fresh["measured_at"], views_source=fresh["views_source"])
        if fresh.get("country"):
            updated["country"] = fresh["country"]
    return updated


# ============ 界面 ============

st.set_page_config(page_title="达人合作工作台", page_icon="🎯", layout="wide",
                   initial_sidebar_state="expanded")
APP_DIR = Path(__file__).resolve().parent
MONITOR_DB = Path(os.environ.get("WORKBENCH_MONITOR_DB", str(APP_DIR / "达人监测.sqlite3")))
MONITOR_SEED = APP_DIR / "monitor_seed.json"

def add_to_monitor(rows, product):
    store = MonitorStore(MONITOR_DB)
    store.seed(MONITOR_SEED)
    product = {"ReiBoot": "ReiBoot for iOS"}.get(product, product)
    saved = skipped = 0
    for row in rows.to_dict("records"):
        url = row.get("主页链接", row.get("频道链接", ""))
        homepage, channel_id = canonical_channel(url)
        if not channel_id:
            skipped += 1
            continue
        channel = {"name": str(row.get("频道名称", "") or "")}
        email = str(row.get("邮箱", "") or "").strip()
        if email and email.lower() != "nan":
            channel["email"] = email
        try:
            store.set_watch(channel_id, product, channel=channel)
            saved += 1
        except ValueError:
            skipped += 1
    if saved:
        store.request_check()
        st.success(f"已锁定 {saved} 个频道，后台会建立基线；在“达人自动推送”查看更新。")
    if skipped:
        st.info(f"{skipped} 条资料需补充规范频道ID或有效邮箱，可在监测设置中添加。")
    if not saved and not skipped:
        st.info("请先勾选要监测的达人。")
    return saved > 0 and saved == len(rows)

def lock_selected_channels(rows, product, prefix):
    rows = clean_review_columns(rows)
    if add_to_monitor(rows, product):
        for _, row in rows.iterrows():
            decision_store().save(row, reason="monitor", product=product, actor=review_actor)
        st.session_state[prefix+"_review_ui_version"] = st.session_state.get(prefix+"_review_ui_version", 0)+1
        st.rerun()

wb_page = render_navigation()
account_store = get_account_store()
if "_pending_mail_active" in st.session_state:
    st.session_state["mail_active_account_id"] = st.session_state.pop("_pending_mail_active")
active_mail_account = render_account_controls(account_store)
review_actor = (account_store.get_account(active_mail_account).get("name") if active_mail_account else "本机使用者")
cooperation_index = build_history(load_cooperation_records(), load_history(), load_library())
if st.session_state.get("_last_mail_account", "__first__") != active_mail_account:
    # Recipient selection and personal template editing survive an account switch.
    # Account-bound reply drafts and confirmations never travel to another inbox.
    for state_key in list(st.session_state):
        if state_key.startswith(("nego_", "confirm_", "mail_editor", "mail_displayed_", "reminder_", "delivery_", "coop_amount_", "coop_date_", "coop_due_", "resend_", "_resend_")):
            st.session_state.pop(state_key, None)
    for state_key in ("_compose_batch", "_compose_options", "compose_sender", "compose_repeat"):
        st.session_state.pop(state_key, None)
    st.session_state["_last_mail_account"] = active_mail_account
if st.session_state.get("_mail_follow_notice"):
    st.info(st.session_state.pop("_mail_follow_notice"))

with (account_scope(active_mail_account) if active_mail_account else nullcontext()):
    # The worker captures this account; later page selections cannot redirect it.
    if active_mail_account:
        selected_account = account_store.get_account(active_mail_account)
        if selected_account and selected_account.get("enabled") and selected_account.get("imap_ready"):
            reply_worker(active_mail_account).start()

    if wb_page == "find" and not API_KEY:
        st.info("首次使用请先配置 YouTube API Key：打开工作台目录的 email_config.ini，在 [youtube] 的 api_key 填入自己的密钥，保存后重启工作台。安装 SOP 有逐步说明。")

    if wb_page == "find":
        tab_keyword, tab_monitor = st.tabs([":material/search: 关键词挖掘", ":material/radar: 达人自动推送"])
        with tab_keyword:
            with st.container(key="wb_discovery_workspace", border=True):
                section_heading("发现设置", eyebrow="DISCOVERY", caption="确定目标范围，找到值得进一步判断的达人。", icon="filter")
                min_subs, max_subs, english_only, exclude_brand, active_days, min_avg_views, min_er, has_email = render_filters("mining")
                pc1, pc2 = st.columns(2)
                kw_product = pc1.selectbox("合作产品", product_options(), key="kw_product")
                kw_type = pc2.selectbox("合作类型", email_module.COOP_TYPES, key="kw_type")
                kw_context = review_context(kw_product, kw_type, english_only, active_mail_account)
                section_heading("搜索关键词", caption="每行一个关键词，搜索后统一查看频道、表现与联系方式。", icon="search")

                keywords_text = st.text_area(
                    "关键词",
                    height=128,
                    placeholder="iphone stuck on apple logo\nwhatsapp transfer android to iphone\nios 18 black screen",
                )

                search_note, search_action = st.columns([3, 1])
                search_note.empty()
                search_clicked = search_action.button("开始挖掘", icon=":material/search:", type="primary", use_container_width=True)
                if search_clicked:
                    keywords = [k.strip() for k in keywords_text.splitlines() if k.strip()]
                    if not keywords:
                        st.warning("请先输入至少一个关键词")
                    else:
                        progress = st.progress(0.0)
                        status = st.empty()
                        with st.spinner("挖掘中（若开了均播/互动率筛选会较慢）..."):
                            results = mine(keywords, min_subs, max_subs, english_only, exclude_brand, active_days,
                                           min_avg_views, min_er, has_email, progress=progress, status=status)
                        progress.empty()
                        status.empty()
                        st.session_state["last_results"] = results or []
                        st.session_state["mine_batch"] = st.session_state.get("mine_batch", 0) + 1

                        if not results:
                            st.info("没有找到符合条件的频道，试试放宽条件或换关键词")
                        else:
                            st.success(f"找到 {len(results)} 个频道（已自动存入达人库）")
                            hist_rows = [{
                                "频道ID": r.get("频道ID", ""),
                                "频道名称": r["频道名称"], "邮箱": r["邮箱"], "主页链接": r["主页链接"],
                                "代表视频标题": r["代表视频标题"], "代表视频链接": r["代表视频链接"],
                                "订阅数": r["订阅数"], "近6条均播": r["近6条均播"], "互动率": r["互动率"],
                                "VPH": r["VPH"], "命中关键词": r["命中关键词"], "来源": "关键词",
                                "挖掘时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
                            } for r in results]
                            save_history(pd.DataFrame(hist_rows))

            if "last_results" in st.session_state:
                results = st.session_state["last_results"]
                if results:
                    section_heading("推荐达人", eyebrow="RESULTS", caption="先判断内容与表现，再收录或标记；已有判断会自动沿用。", icon="users")
                    # 编辑表格：邮箱可改 + 「加入」勾选（用 key 持久化编辑状态，避免撤回）
                    with st.container(key='review_position_kw_review'):
                        base = discovery_frame(display_text_copy(pd.DataFrame(results)),kw_context,"kw_review",cooperation_index)
                        base.insert(0, "序号", range(1, len(base) + 1))
                        base["加入"] = False
                        keyword_editor_key = f"mine_editor_{st.session_state.get('mine_batch', 0)}_"+editor_suffix(base,kw_context,"kw_review")
                        keyword_display = seed_editor_layout(base, keyword_editor_key, ["邮箱", "加入", TAG_COLUMN])
                        edited = st.data_editor(
                            style_creator_names(keyword_display, "keyword", pinned=True),
                            key=keyword_editor_key,
                            column_config={
                                **review_columns(),
                                "序号": st.column_config.NumberColumn("序号", width="small", format="%d", pinned=True),
                                "缩略图": st.column_config.ImageColumn("视频封面", width="small"),
                                "频道名称": st.column_config.TextColumn("频道", width="medium", pinned=True),
                                "邮箱": st.column_config.TextColumn("联系邮箱", width="medium"),
                                "近6条均播": st.column_config.NumberColumn("近期均播", width="small", format="compact"),
                                "VPH": st.column_config.NumberColumn("VPH", width="small", format="%.1f"),
                                "主页链接": st.column_config.LinkColumn("主页", display_text="打开主页"),
                                "代表视频链接": st.column_config.LinkColumn("代表视频", display_text="观看视频"),
                                "互动率": st.column_config.NumberColumn("互动率(%)", format="%.1f"),
                                "加入": st.column_config.CheckboxColumn("选择", width="small", pinned=True),
                            },
                            disabled=[c for c in base.columns if c not in ["邮箱", "加入", TAG_COLUMN]],
                            column_order=["序号","加入","频道名称","近6条均播","VPH",TAG_COLUMN,COOP_COLUMN,
                                          "邮箱","主页链接","代表视频链接",LABEL_COLUMN,"订阅数","互动率","代表视频标题","缩略图","命中关键词"],
                            height=540, row_height=44,
                            hide_index=True,
                            use_container_width=True,
                        )

                        checked_n = int(edited["加入"].fillna(False).astype(bool).sum())
                        st.caption(f"当前 {len(edited)} 个达人 · 已选择 {checked_n} 个 · 可直接编辑邮箱")

                        col1, col2 = st.columns([1, 3])
                        with col1:
                            if st.button("加入建联库", icon=":material/person_add:", type="primary", use_container_width=True, key="add_lib_kw"):
                                sel = edited[edited["加入"].fillna(False).astype(bool) ].copy()
                                if len(sel) == 0:
                                    st.warning("请先勾选「加入」；邮箱可以稍后在建联库补充")
                                else:
                                    sel = clean_review_columns(sel.drop(columns=["加入"]))
                                    sel["状态"] = "待建联"
                                    sel["合作产品"] = kw_product
                                    sel["合作类型"] = kw_type
                                    sel["发送状态"] = "待发送"
                                    sel["加入时间"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                                    save_to_library(sel)
                        with col2:
                            lib = load_library()
                            if not lib.empty:
                                st.caption(f"建联库 {len(lib)} 个达人 · {sum(lib['状态']=='待建联')} 个待跟进")
                            else:
                                st.caption("选择达人后加入建联库，继续邀约与跟进。")


                        render_review_controls(edited,kw_context,"kw_review",
                            selected=edited[edited["加入"].fillna(False).astype(bool)],
                            monitor_callback=lambda rows,p:add_to_monitor(pd.DataFrame(rows),p),actor=review_actor)
                        render_review_position('kw_review', json.dumps([kw_context, st.session_state.get("kw_review_list_view", "all"), st.session_state.get("mine_batch", 0), st.session_state.get("kw_review_show_processed", False), [r.get("频道ID") or r.get("主页链接") for r in results]], ensure_ascii=False, sort_keys=True), review_version('kw_review'))
                    render_record_lookup(edited,load_cooperation_records(),load_history(),load_library(),
                        product=kw_product,form=kw_type,prefix="kw_cooperation")
                    render_source_videos(base)
            if st.session_state.get("last_results"):
                if st.button("锁定勾选达人，持续监测更新", icon=":material/notifications_active:", key="watch_kw"):
                    lock_selected_channels(edited[edited["加入"].fillna(False).astype(bool)].copy(), kw_product,"kw_review")
        with tab_monitor:
            render_monitor_page(db_path=MONITOR_DB, contact_path=LIBRARY_FILE, seed_path=MONITOR_SEED,
                                product_registry=product_options())

    elif wb_page == "resources":
        resource_history, resource_cooperation, resource_pool, resource_similar = st.tabs(
            [":material/database: 历史达人资源", ":material/handshake: 合作记录", ":material/label: 人工筛选池", ":material/hub: 相似达人推荐"])
        with resource_cooperation:
            render_cooperation_history(load_cooperation_records(),load_history(),load_library(),products=product_options())
        with resource_pool:
            render_review_pool(account_id=active_mail_account,actor=review_actor,store=decision_store())
        with resource_history:
            section_heading("历史达人库", caption="所有已收录资料集中保留；可按联系方式与合作历史快速查看。", icon="library")
            hist = load_history()
            if hist.empty:
                st.info("达人库还是空的。去「关键词挖掘」或「相似达人推荐」跑一次，结果会自动存到这里，下次不用重复搜索。")
            else:
                c1, c2 = st.columns([3, 1])
                with c1:
                    search = st.text_input("搜索达人", placeholder="频道名称、邮箱或关键词", key="hist_search")
                with c2:
                    st.markdown(f"共 **{len(hist)}** 个达人")
                h = display_text_copy(hist)
                if search:
                    h = h[h.apply(lambda row: search.lower() in str(row).lower(), axis=1)]
                hp1, hp2 = st.columns(2)
                hist_product = hp1.selectbox("合作产品", product_options(), key="hist_product")
                hist_type = hp2.selectbox("合作类型", email_module.COOP_TYPES, key="hist_type")
                hist_context = review_context(hist_product,hist_type,None,active_mail_account)
                h = decorate(h,hist_context,cooperation_index)
                h = render_resource_views(h,"hist_review",cooperation=cooperation_index,product=hist_product,form=hist_type)
                # 去来源列、加序号、加 加入/删除 勾选列
                h = h.copy()
                if "来源" in h.columns:
                    h = h.drop(columns=["来源"])
                h.insert(0, "序号", range(1, len(h) + 1))
                h["加入"] = False
                h["删除"] = False

                show_cols = ["序号","频道名称",TAG_COLUMN,LABEL_COLUMN,COOP_COLUMN,"邮箱","主页链接","代表视频标题","代表视频链接",
                             "订阅数","近6条均播","互动率","命中关键词","加入","删除","频道ID"]
                show_cols = [c for c in show_cols if c in h.columns]
                with st.container(key='review_position_hist_review'):
                    history_editor_key = f"hist_editor_{len(hist)}_{search or 'all'}_"+editor_suffix(h,hist_context,"hist_review")
                    history_display = seed_editor_layout(h[show_cols], history_editor_key, ["邮箱", "加入", "删除", TAG_COLUMN])
                    edited = st.data_editor(
                        style_creator_names(history_display, "history", pinned=True),
                        key=history_editor_key,
                        column_config={
                            **review_columns(), "频道ID": None,
                            "序号": st.column_config.NumberColumn("序号", width="small", format="%d", pinned=True),
                            "频道名称": st.column_config.TextColumn("频道", width="medium", pinned=True),
                            "邮箱": st.column_config.TextColumn("联系邮箱", width="medium"),
                            "主页链接": st.column_config.LinkColumn("主页链接", display_text="打开主页"),
                            "代表视频链接": st.column_config.LinkColumn("代表视频链接", display_text="观看视频"),
                            "加入": st.column_config.CheckboxColumn("加入"),
                            "删除": st.column_config.CheckboxColumn("删除"),
                        },
                        disabled=[c for c in show_cols if c not in ["邮箱", "加入", "删除", TAG_COLUMN]],
                        hide_index=True,
                        use_container_width=True, height=540, row_height=44,
                    )

                    btn1, btn2 = st.columns(2)
                    with btn1:
                        if st.button("加入建联库", icon=":material/person_add:", type="primary", use_container_width=True, key="hist_add_lib"):
                            sel = edited[edited["加入"].fillna(False).astype(bool) ]
                            if len(sel) == 0:
                                st.warning("请先勾选「加入」；邮箱可以稍后在建联库补充")
                            else:
                                rows = []
                                for _, r in sel.iterrows():
                                    rows.append({
                                        "频道名称": r.get("频道名称", ""),
                                        "邮箱": r.get("邮箱", ""),
                                        "主页链接": r.get("主页链接", ""),
                                        "代表视频标题": r.get("代表视频标题", ""),
                                        "代表视频链接": r.get("代表视频链接", ""),
                                        "订阅数": r.get("订阅数", ""),
                                        "近6条均播": r.get("近6条均播", ""),
                                        "互动率": r.get("互动率", ""),
                                        "命中关键词": r.get("命中关键词", ""),
                                        "合作产品": hist_product,
                                        "合作类型": hist_type,
                                        "发送状态": "待发送",
                                        "状态": "待建联",
                                        "加入时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                    })
                                save_to_library(pd.DataFrame(rows))
                    with btn2:
                        if st.button("保存邮箱修改", icon=":material/save:", use_container_width=True, key="hist_save_email"):
                            email_map = dict(zip(edited["主页链接"].astype(str), edited["邮箱"].astype(str)))
                            hist["邮箱"] = hist["主页链接"].astype(str).map(email_map).fillna(hist["邮箱"])
                            write_csv_atomic(hist, HISTORY_FILE)
                            st.success("已保存邮箱修改")

                    btn3, btn4 = st.columns(2)
                    with btn3:
                        if st.button("删除勾选行", icon=":material/delete_outline:", use_container_width=True, key="hist_del"):
                            del_mask = edited["删除"].fillna(False).astype(bool)
                            del_links = [str(l) for l, d in zip(edited["主页链接"], del_mask) if d and str(l).strip()]
                            if del_links:
                                hist = hist[~hist["主页链接"].astype(str).isin(del_links)]
                                write_csv_atomic(hist, HISTORY_FILE)
                                st.success(f"已删除 {len(del_links)} 个达人")
                                st.rerun()
                            else:
                                st.warning("请先勾选「删除」列")
                    with btn4:
                        if st.button("整理重复达人", icon=":material/filter_alt:", use_container_width=True, key="hist_dedup"):
                            before = len(hist)
                            hist2 = dedupe_channels(hist)
                            write_csv_atomic(hist2, HISTORY_FILE)
                            st.success(f"已剔除 {before - len(hist2)} 个重复达人（剩 {len(hist2)} 个）")
                            st.rerun()

                    from io import BytesIO
                    buf = BytesIO()
                    hist.to_excel(buf, index=False, engine="openpyxl")
                    st.download_button("导出达人库", icon=":material/download:", data=buf.getvalue(),
                                       file_name=f"达人库_{datetime.now().strftime('%Y%m%d')}.xlsx",
                                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


                    if st.button("锁定勾选达人，持续监测更新", icon=":material/notifications_active:", key="watch_history"):
                        lock_selected_channels(edited[edited["加入"].fillna(False).astype(bool)].copy(), hist_product,"hist_review")
                    render_review_controls(edited,hist_context,"hist_review",
                        selected=edited[edited["加入"].fillna(False).astype(bool)],
                        monitor_callback=lambda rows,p:add_to_monitor(pd.DataFrame(rows),p),actor=review_actor)
                    render_review_position('hist_review', json.dumps([hist_context, st.session_state.get("hist_review_list_view", "all"), search, [r.get("频道ID") or r.get("主页链接") for r in hist.to_dict("records")]], ensure_ascii=False, sort_keys=True), review_version('hist_review'))
                render_record_lookup(edited,load_cooperation_records(),load_history(),load_library(),
                    product=hist_product,form=hist_type,prefix="hist_cooperation")
        with resource_similar:
            with st.expander("相似推荐的筛选条件", expanded=False):
                min_subs, max_subs, english_only, exclude_brand, active_days, min_avg_views, min_er, has_email = render_filters("similar")
            sc1, sc2 = st.columns(2)
            sim_product = sc1.selectbox("合作产品", product_options(), key="sim_product")
            sim_type = sc2.selectbox("合作类型", email_module.COOP_TYPES, key="sim_type")
            sim_context = review_context(sim_product,sim_type,english_only,active_mail_account)
            section_heading("相似达人推荐", caption="从值得参考的视频或频道出发，拓展相近内容的达人。", icon="users")
            st.caption("输入视频链接或频道主页，每行一个。")
            video_links_text = st.text_area(
                "视频 / 频道链接",
                height=100,
                placeholder="https://www.youtube.com/watch?v=xxxx\nhttps://www.youtube.com/@channel\nhttps://www.youtube.com/channel/UCxxxx",
            )

            if st.button("推荐相似达人", icon=":material/hub:", type="primary", use_container_width=True):
                ids = []
                for link in video_links_text.splitlines():
                    link = link.strip()
                    if not link:
                        continue
                    vid = extract_video_id(link)
                    if vid:
                        ids.append(vid)
                    else:
                        first = resolve_channel_first_video(link)
                        if first:
                            ids.append(first)
                if not ids:
                    st.warning("请先输入有效的视频或频道链接")
                else:
                    with st.spinner("抓取相似视频 + 查频道详情..."):
                        all_rel = []
                        for vid in ids:
                            all_rel.extend(scrape_related_videos(vid))
                            time.sleep(1)
                        seen = set()
                        dedup = []
                        for r in all_rel:
                            if r["vid"] and r["vid"] not in seen:
                                seen.add(r["vid"])
                                dedup.append(r)
                        cids = [r["channel_id"] for r in dedup if r["channel_id"]]
                        cdata = api_channels(cids)
                    sim = []
                    for r in dedup:
                        ch = cdata.get(r["channel_id"], {})
                        sim.append({
                            "频道ID": r.get("channel_id", ""),
                            "缩略图": f"https://img.youtube.com/vi/{r['vid']}/hqdefault.jpg" if r["vid"] else "",
                            "视频标题": r["title"],
                            "视频链接": f"https://www.youtube.com/watch?v={r['vid']}" if r["vid"] else "",
                            "频道名称": r["channel"],
                            "频道链接": f"https://www.youtube.com/channel/{r['channel_id']}" if r.get("channel_id") else r["channel_url"],
                            "订阅数": ch.get("subs", 0),
                            "邮箱": ch.get("email", ""),
                            "播放量": r["views"],
                            "发布时间": r["age"],
                        })
                    st.session_state["similar_results"] = sim
                    st.session_state["sim_batch"] = st.session_state.get("sim_batch", 0) + 1  # 批次+1，重置编辑表格
                    st.success(f"找到 {len(sim)} 个相似视频（已自动存入达人库）")
                    hist_rows = [{
                        "频道ID": r.get("频道ID", ""),
                        "频道名称": r["频道名称"], "邮箱": r["邮箱"], "主页链接": r["频道链接"],
                        "代表视频标题": r["视频标题"], "代表视频链接": r["视频链接"],
                        "订阅数": r["订阅数"], "近6条均播": "", "互动率": "",
                        "命中关键词": "相似视频", "来源": "相似视频",
                        "挖掘时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    } for r in sim]
                    save_history(pd.DataFrame(hist_rows))

            if "similar_results" in st.session_state:
                sim = st.session_state["similar_results"]
                if sim:
                    # 编辑表格：邮箱可改 + 「加入」勾选（用 key 持久化编辑状态，避免撤回）
                    with st.container(key='review_position_sim_review'):
                        base = discovery_frame(display_text_copy(pd.DataFrame(sim)),sim_context,"sim_review",cooperation_index)
                        base.insert(0, "序号", range(1, len(base) + 1))
                        base["加入"] = False
                        similar_editor_key = f"sim_editor_{st.session_state.get('sim_batch', 0)}_"+editor_suffix(base,sim_context,"sim_review")
                        similar_display = seed_editor_layout(base, similar_editor_key, ["邮箱", "加入", TAG_COLUMN])
                        sedited = st.data_editor(
                            style_creator_names(similar_display, "similar"),
                            key=similar_editor_key,
                            column_config={
                                **review_columns(),
                                "序号": st.column_config.NumberColumn("序号", width="small", format="%d", pinned=True),
                                "缩略图": st.column_config.ImageColumn("视频", width="medium"),
                                "视频链接": st.column_config.LinkColumn("视频", display_text="观看视频"),
                                "频道链接": st.column_config.LinkColumn("频道", display_text="打开主页"),
                                "加入": st.column_config.CheckboxColumn("加入"),
                            },
                            disabled=[c for c in base.columns if c not in ["邮箱", "加入", TAG_COLUMN]],
                            column_order=["序号","加入","频道名称",TAG_COLUMN,LABEL_COLUMN,COOP_COLUMN,
                                          "缩略图","视频标题","视频链接","频道链接","订阅数","邮箱","播放量","发布时间"],
                            hide_index=True,
                            use_container_width=True,
                        )

                        checked_n = int(sedited["加入"].fillna(False).astype(bool).sum())
                        st.markdown(f"**本批共 {len(sedited)} 个相似达人**，已勾选 {checked_n} 个。编辑邮箱 → 勾选「加入」→ 点「加入建联库」")

                        if st.button("加入建联库", icon=":material/person_add:", type="primary", use_container_width=True, key="add_lib_sim"):
                            sel = sedited[sedited["加入"].fillna(False).astype(bool) ].copy()
                            if len(sel) == 0:
                                st.warning("请先勾选「加入」；邮箱可以稍后在建联库补充")
                            else:
                                rows = []
                                for _, r in sel.iterrows():
                                    rows.append({
                                        "频道名称": r["频道名称"],
                                        "邮箱": r["邮箱"],
                                        "主页链接": r["频道链接"],
                                        "代表视频标题": r["视频标题"],
                                        "代表视频链接": r["视频链接"],
                                        "缩略图": r["缩略图"],
                                        "订阅数": r["订阅数"],
                                        "近6条均播": "",
                                        "互动率": "",
                                        "命中关键词": "相似视频",
                                        "合作产品": sim_product,
                                        "合作类型": sim_type,
                                        "发送状态": "待发送",
                                        "状态": "待建联",
                                        "加入时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                    })
                                save_to_library(pd.DataFrame(rows))

                        render_review_controls(sedited,sim_context,"sim_review",
                            selected=sedited[sedited["加入"].fillna(False).astype(bool)],
                            monitor_callback=lambda rows,p:add_to_monitor(pd.DataFrame(rows),p),actor=review_actor)
                        render_review_position('sim_review', json.dumps([sim_context, st.session_state.get("sim_review_list_view", "all"), st.session_state.get("sim_batch", 0), st.session_state.get("sim_review_show_processed", False), [r.get("频道ID") or r.get("频道链接") for r in sim]], ensure_ascii=False, sort_keys=True), review_version('sim_review'))
                    render_record_lookup(sedited,load_cooperation_records(),load_history(),load_library(),
                        product=sim_product,form=sim_type,prefix="sim_cooperation")
                    render_source_videos(base)

            if st.session_state.get("similar_results"):
                if st.button("锁定勾选达人，持续监测更新", icon=":material/notifications_active:", key="watch_similar"):
                    lock_selected_channels(sedited[sedited["加入"].fillna(False).astype(bool)].copy(), sim_product,"sim_review")
    elif wb_page == "contacts":
        people_tab, follow_tab, template_tab = st.tabs([":material/contacts: 联系人", ":material/mail: 邮件跟进", ":material/article: 模板与产品"])
        with people_tab:
            render_contact_workspace(load_library, save_library_full, save_to_library,
                                     active_account_id=active_mail_account)
            render_record_lookup(load_library(),load_cooperation_records(),load_history(),load_library(),
                                 prefix="contact_cooperation")
        with follow_tab:
            render_followup(active_mail_account, load_history, load_library, load_effect, save_effect,
                        refresh_metrics=automatic_negotiation_metrics,cooperation_records=load_cooperation_records)
        with template_tab:
            render_template_manager(get_template_store(), account_store, active_mail_account)

    elif wb_page == "analysis":
        da1, da2, da3 = st.tabs([":material/insights: 合作效果", ":material/mark_email_read: 回复率", ":material/trending_up: 热点追踪"])

        # ---- 二级：合作效果 ----
        with da1:
            st.markdown("### 达人效果评估（定期评估，判断二次合作）")
            eff = load_effect()
            if "二次合作" not in eff.columns:
                eff["二次合作"] = "待评估"
            eff["二次合作"] = eff["二次合作"].fillna("").replace("", "待评估")

            # 汇总
            if not eff.empty:
                amount = pd.to_numeric(eff["合作金额"], errors="coerce").fillna(0).sum()
                partners = int(eff["达人名"].fillna("").astype(str).str.strip().ne("").sum())
                dl = pd.to_numeric(eff["下载数"], errors="coerce").fillna(0).sum()
                views = pd.to_numeric(eff["播放量"], errors="coerce").fillna(0).sum()
                cpm = amount / (views / 1000) if views > 0 else 0
                s1, s2, s3, s4 = st.columns(4)
                s1.metric("合作达人", partners)
                s2.metric("总金额（$）", f"{amount:,.0f}")
                s3.metric("总下载", f"{dl:,.0f}")
                s4.metric("平均 CPM（$）", f"{cpm:.1f}")

            # ① 刷新数据
            st.markdown("**① 刷新数据**（重新拉 YouTube 播放/点赞/评论 + GA4 会话/下载）")
            c1, c2, c3 = st.columns([1, 1, 2])
            with c1:
                roi_channel = st.text_input("渠道代码", "yw", key="roi_channel")
            with c2:
                roi_days = st.number_input("GA4 天数", 7, 365, 90, step=7, key="roi_days")
            with c3:
                st.write("")
                if st.button("刷新合作数据", icon=":material/refresh:", use_container_width=True, key="roi_refresh"):
                    if eff.empty:
                        st.warning("还没有达人，先添加")
                    else:
                        prog = st.progress(0.0)
                        stat = st.empty()
                        with st.spinner("刷新中（YouTube + GA4）..."):
                            eff2, unmatched, err = refresh_influencers(eff.copy(), roi_channel.strip().lower(), int(roi_days), progress=prog, status=stat)
                        prog.empty()
                        stat.empty()
                        if err:
                            st.warning(f"GA4 拉取失败（已自动重试，可能网络抖动）：{err}")
                        else:
                            st.success("已刷新所有达人数据")
                            if unmatched:
                                st.warning(f"⚠️ {len(unmatched)} 个达人**没匹配到 GA4**（会话/下载为 0，可能是名字和 utm_term 不一致）：\n\n" + "、".join(unmatched))
                            st.rerun()

            # ② 添加达人
            st.markdown("**② 添加达人**（达人名 + 视频链接 必填，其余可留空）")
            with st.expander("➕ 新增达人"):
                for _k, _v in [("ef_pub", ""), ("ef_views", 0), ("ef_likes", 0), ("ef_comments", 0), ("ef_sessions", 0), ("ef_dl", 0)]:
                    if _k not in st.session_state:
                        st.session_state[_k] = _v

                ec1, ec2, ec3 = st.columns(3)
                with ec1:
                    ef_name = st.text_input("达人名 *", key="ef_name")
                    ef_link = st.text_input("视频链接 *", key="ef_link")
                    ef_kw = st.text_input("核心关键词", key="ef_kw")
                    if st.button("从链接获取", icon=":material/link:", use_container_width=True, key="ef_autofetch"):
                        info = fetch_video_info(ef_link)
                        if info:
                            st.session_state["ef_pub"] = info["published"]
                            st.session_state["ef_views"] = info["views"]
                            st.session_state["ef_likes"] = info["likes"]
                            st.session_state["ef_comments"] = info["comments"]
                            # 同时按达人名拉 GA4 会话/下载
                            ga4_msg = ""
                            if ef_name.strip():
                                try:
                                    utm_map, gerr = ga4_channel_conversion("yw", 90)
                                    if gerr:
                                        ga4_msg = f"（GA4 拉取失败：{gerr}）"
                                    else:
                                        sess, dls = match_influencer_utm(ef_name, utm_map)
                                        st.session_state["ef_sessions"] = sess
                                        st.session_state["ef_dl"] = dls
                                        if sess or dls:
                                            ga4_msg = f"，GA4 会话 {sess}、下载 {dls}"
                                        else:
                                            ga4_msg = "，GA4 未匹配到（检查达人名是否和 utm_term 一致）"
                                except Exception as ex:
                                    ga4_msg = f"（GA4 失败：{ex}）"
                            st.success(f"已获取：播放量 {info['views']:,}，点赞 {info['likes']:,}，评论 {info['comments']:,}，VPH {info['vph']}{ga4_msg}")
                            st.rerun()
                        else:
                            st.warning("无法解析该视频链接，或视频无数据")
                with ec2:
                    ef_amount = st.number_input("合作金额（$）", 0, 10**9, 0, step=10, key="ef_amount")
                    ef_date = st.date_input("合作日期", datetime.now().date(), key="ef_date")
                    ef_rank = st.text_input("排名（无痕搜索）", key="ef_rank")
                with ec3:
                    ef_pub = st.text_input("发布时间", key="ef_pub")
                    ef_views = st.number_input("播放量", 0, 10**9, step=1000, key="ef_views")
                    ef_dl = st.number_input("下载数", 0, 10**9, step=1, key="ef_dl")

                ec4, ec5, ec6 = st.columns(3)
                with ec4:
                    ef_likes = st.number_input("点赞", 0, 10**9, step=10, key="ef_likes")
                with ec5:
                    ef_comments = st.number_input("评论", 0, 10**9, step=10, key="ef_comments")
                with ec6:
                    ef_sessions = st.number_input("会话数（GA4）", 0, 10**9, step=1, key="ef_sessions")

                if st.button("保存记录", icon=":material/save:", type="primary", use_container_width=True, key="ef_save"):
                    if not ef_name.strip() or not ef_link.strip():
                        st.warning("达人名 和 视频链接 为必填项")
                    else:
                        vph = 0.0
                        pub_dt = None
                        if ef_pub.strip():
                            try:
                                pub_dt = datetime.strptime(ef_pub.strip(), "%Y-%m-%d")
                            except Exception:
                                pub_dt = None
                        if pub_dt and ef_views > 0:
                            hours = max((datetime.now() - pub_dt).total_seconds() / 3600, 1.0)
                            vph = round(ef_views / hours, 1)
                        row = pd.DataFrame([{
                            "达人名": ef_name, "邮箱": "", "合作产品": "", "合作类型": "",
                            "合作日期": str(ef_date), "合作金额": ef_amount,
                            "视频链接": ef_link, "核心关键词": ef_kw,
                            "排名": ef_rank, "发布时间": ef_pub, "播放量": ef_views, "VPH": vph,
                            "点赞": ef_likes, "评论": ef_comments, "会话数": ef_sessions,
                            "下载数": ef_dl, "短链": "", "短链点击": 0, "二次合作": "待评估",
                            "记录时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        }])
                        save_effect(pd.concat([eff, row], ignore_index=True) if not eff.empty else row)
                        st.success("已保存")
                        st.rerun()

            # ③ 达人集合（判断二次合作）
            if eff.empty:
                st.info("还没有合作达人。点上方「➕ 新增达人」录入，或在「建联库 → 议价」里「标记已合作」")
            else:
                st.markdown("**③ 达人集合**（查看所有合作达人，判断是否二次合作）")
                show = eff.copy()
                for c in ["合作金额", "播放量", "下载数", "会话数"]:
                    if c in show.columns:
                        show[c] = pd.to_numeric(show[c], errors="coerce").fillna(0)
                if "播放量" in show.columns:
                    show["CPM"] = (show["合作金额"] * 1000 / show["播放量"].replace(0, float("nan"))).round(1)
                    show["转化率%"] = (show["下载数"] / show["播放量"].replace(0, float("nan")) * 100).round(2)
                    show["每下载成本"] = (show["合作金额"] / show["下载数"].replace(0, float("nan"))).round(1)
                    show["每会话成本"] = (show["合作金额"] / show["会话数"].replace(0, float("nan"))).round(1)
                show_cols = [c for c in ["达人名", "合作金额", "合作日期", "播放量", "点赞", "评论", "会话数", "下载数",
                                         "CPM", "转化率%", "每下载成本", "每会话成本", "二次合作", "视频链接"] if c in show.columns]
                edited = st.data_editor(
                    show[show_cols],
                    key="effect_editor",
                    column_config={
                        "二次合作": st.column_config.SelectboxColumn("二次合作", options=["待评估", "可二次合作", "不合作", "已二次合作"]),
                        "视频链接": st.column_config.LinkColumn("视频链接", display_text="观看视频"),
                    },
                    disabled=[c for c in show_cols if c not in ["二次合作"]],
                    hide_index=True,
                    use_container_width=True,
                )
                if st.button("保存合作判断", icon=":material/save:", key="effect_save_judge"):
                    eff2 = load_effect()
                    if "二次合作" not in eff2.columns:
                        eff2["二次合作"] = "待评估"
                    judge = dict(zip(edited["达人名"].astype(str), edited["二次合作"].astype(str)))
                    eff2["二次合作"] = eff2["达人名"].astype(str).map(judge).fillna("待评估")
                    save_effect(eff2)
                    st.success("已保存二次合作判断")
                    st.rerun()

                from io import BytesIO
                ebuf = BytesIO()
                eff.to_excel(ebuf, index=False, engine="openpyxl")
                st.download_button("导出合作评估", icon=":material/download:", data=ebuf.getvalue(),
                                   file_name=f"达人效果评估_{datetime.now().strftime('%Y%m%d')}.xlsx",
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

            st.divider()
            st.markdown("### GA4 数据（我的渠道 + 主站 / 子站）")
            import configparser
            _cfg = configparser.ConfigParser()
            _cfg.read("email_config.ini")
            _cred = _cfg.get("ga4", "credentials_file", fallback="").strip()
            _prop = _cfg.get("ga4", "property_id", fallback="").strip()
            if not os.path.exists(_cred) or not _prop:
                st.warning("GA4 还没配好：需要把「服务账号 JSON 私钥」放到项目目录，并把「Property ID」填进 email_config.ini 的 [ga4] 段。")
            else:
                st.success(f"GA4 已配置（Property {_prop}，服务账号 {os.path.basename(_cred)}）")

            c1, c2 = st.columns(2)
            with c1:
                ga4_days = st.number_input("拉取最近 N 天", 7, 365, 30, step=7, key="ga4_days")
            with c2:
                ga4_channel = st.text_input("我的渠道代码（utm_term 里的后缀）", "yw", key="ga4_channel")
            if st.button("获取渠道数据", icon=":material/monitoring:", key="ga4_fetch"):
                ch = ga4_channel.strip().lower()
                with st.spinner("拉取 GA4 数据..."):
                    yw_sum, e1 = ga4_run_report(
                        metrics=["sessions"],
                        start_date=f"{ga4_days}daysAgo", end_date="today",
                        filter_expr={"filter": {"fieldName": "sessionManualTerm", "stringFilter": {"matchType": "CONTAINS", "value": ch}}},
                    )
                    yw_dl, e2 = ga4_run_report(
                        metrics=["eventCount"],
                        start_date=f"{ga4_days}daysAgo", end_date="today",
                        filter_expr={"andGroup": {"expressions": [
                            {"filter": {"fieldName": "sessionManualTerm", "stringFilter": {"matchType": "CONTAINS", "value": ch}}},
                            {"filter": {"fieldName": "eventName", "stringFilter": {"matchType": "EXACT", "value": "Download"}}},
                        ]}},
                    )
                    conv_map, e3 = ga4_channel_conversion(ch, int(ga4_days))
                    trend_df, e4 = ga4_run_report(
                        metrics=["sessions"], dimensions=["date"],
                        start_date=f"{ga4_days}daysAgo", end_date="today",
                        filter_expr={"filter": {"fieldName": "sessionManualTerm", "stringFilter": {"matchType": "CONTAINS", "value": ch}}},
                    )
                    trend_dl_df, e5 = ga4_run_report(
                        metrics=["eventCount"], dimensions=["date"],
                        start_date=f"{ga4_days}daysAgo", end_date="today",
                        filter_expr={"andGroup": {"expressions": [
                            {"filter": {"fieldName": "sessionManualTerm", "stringFilter": {"matchType": "CONTAINS", "value": ch}}},
                            {"filter": {"fieldName": "eventName", "stringFilter": {"matchType": "EXACT", "value": "Download"}}},
                        ]}},
                    )
                if e1 or e2 or e3 or e4 or e5:
                    st.warning(e1 or e2 or e3 or e4 or e5)
                else:
                    st.session_state["ga4_yw_sum"] = yw_sum
                    st.session_state["ga4_yw_dl"] = yw_dl
                    st.session_state["ga4_conv"] = conv_map
                    st.session_state["ga4_trend"] = trend_df
                    st.session_state["ga4_trend_dl"] = trend_dl_df
                    st.session_state["ga4_channel_saved"] = ch
                    st.success("拉取成功")

            # ① 我的渠道 汇总（会话 + 下载）
            if st.session_state.get("ga4_yw_sum") is not None and not st.session_state["ga4_yw_sum"].empty:
                ch = st.session_state.get("ga4_channel_saved", "yw")
                ssum = st.session_state["ga4_yw_sum"].iloc[0]
                sessions = int(float(ssum.get("sessions", 0)))
                dl = 0
                if st.session_state.get("ga4_yw_dl") is not None and not st.session_state["ga4_yw_dl"].empty:
                    dl = int(float(st.session_state["ga4_yw_dl"].iloc[0].get("eventCount", 0)))
                st.markdown(f"**① 我的渠道（{ch}）汇总**")
                m1, m2 = st.columns(2)
                m1.metric("会话（Sessions）", f"{sessions:,}")
                m2.metric("下载（Download）", f"{dl:,}")
                st.caption("会话 = 一次访问（用户进入网站的一次交互）；下载 = GA4 里的 Download 事件次数。")

            # 整体流量趋势（会话 + 下载 按天）
            if st.session_state.get("ga4_trend") is not None and not st.session_state["ga4_trend"].empty:
                st.markdown(f"**整体流量趋势（{st.session_state.get('ga4_channel_saved', 'yw')} 渠道）**")
                t = st.session_state["ga4_trend"].copy()
                t["sessions"] = pd.to_numeric(t["sessions"], errors="coerce").fillna(0)
                t = t.rename(columns={"sessions": "会话"})
                if st.session_state.get("ga4_trend_dl") is not None and not st.session_state["ga4_trend_dl"].empty:
                    td = st.session_state["ga4_trend_dl"].copy()
                    td["eventCount"] = pd.to_numeric(td["eventCount"], errors="coerce").fillna(0)
                    t = t.merge(td.rename(columns={"eventCount": "下载"}), on="date", how="left")
                    t["下载"] = t["下载"].fillna(0)
                t = t.sort_values("date")
                st.line_chart(t.set_index("date"))

            # ② 我的渠道 按达人（会话 + 下载）
            if st.session_state.get("ga4_conv"):
                st.markdown(f"**② 我的渠道（{st.session_state.get('ga4_channel_saved', 'yw')}）按达人**")
                conv = st.session_state["ga4_conv"]
                rows = [{"utm_term": k, "会话": v["sessions"], "下载": v["downloads"]} for k, v in conv.items()]
                d = pd.DataFrame(rows).sort_values("会话", ascending=False).reset_index(drop=True)
                st.dataframe(d, hide_index=True, use_container_width=True)

        # ---- 二级：回复率 ----
        with da2:
            st.markdown("### 回复率分析")
            mail_log = email_module.load_mail_log()
            if not mail_log.empty:
                purpose = mail_log.get("邮件类型", pd.Series("", index=mail_log.index)).fillna("")
                mail_log = mail_log[~purpose.isin(["议价回复", "接受报价"])]
            st.caption("回复率按当前查看邮箱的邀约记录统计；议价和接受报价的回复邮件不重复计入邀约数。")
            if mail_log.empty:
                st.info("还没有发送记录，先去「建联库」发送邮件")
            else:
                mail = mail_log.copy()
                if "回信状态" not in mail.columns:
                    mail["回信状态"] = ""
                mail["已回信"] = mail["回信状态"].fillna("").apply(
                    lambda s: 0 if str(s).strip() in ("", "从未回信") else 1
                )
                total_sent = len(mail)
                total_replied = int(mail["已回信"].sum())
                rate = total_replied / total_sent * 100 if total_sent else 0
                ok_sent = int((mail["状态"].fillna("") == "成功").sum()) if "状态" in mail.columns else total_sent
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("总发送", total_sent)
                m2.metric("已回信", total_replied)
                m3.metric("回复率", f"{rate:.1f}%")
                m4.metric("发送成功", ok_sent)

                c1, c2 = st.columns(2)
                for dim_name, col in [("合作产品", c1), ("合作类型", c2)]:
                    with col:
                        if dim_name in mail.columns:
                            st.markdown(f"**按 {dim_name}**")
                            g = mail.groupby(dim_name).agg(发送数=("邮箱", "count"), 回信数=("已回信", "sum")).reset_index()
                            g["回复率"] = (g["回信数"] / g["发送数"] * 100).round(1).astype(str) + "%"
                            st.dataframe(g, hide_index=True, use_container_width=True)

        # ---- 二级：热点追踪 ----
        with da3:
            st.markdown("### 热点追踪")
            st.markdown("**① 官方趋势**（YouTube Trending，可按地区 + 分类）")
            tc1, tc2, tc3 = st.columns([1, 1, 1])
            with tc1:
                off_region = st.selectbox("地区", ["US", "GB", "IN", "ES", "BR", "DE", "FR", "JP", "TW", "HK"], key="off_region")
            with tc2:
                off_cat = st.selectbox("分类", list(YOUTUBE_CATEGORIES.keys()), index=0, key="off_cat")
            with tc3:
                st.write("")
                st.write("")
                if st.button("获取官方趋势", icon=":material/trending_up:", key="off_fetch"):
                    with st.spinner("拉取官方趋势..."):
                        off = api_trending(off_region, YOUTUBE_CATEGORIES[off_cat], 25)
                    if not off:
                        st.warning("拉取失败（可能配额用尽）")
                    else:
                        st.session_state["official_trending"] = off
                        st.success(f"拉取到 {len(off)} 个官方趋势视频")
            if st.session_state.get("official_trending"):
                odf = pd.DataFrame(st.session_state["official_trending"])
                st.data_editor(
                    odf,
                    key="official_trending_editor",
                    column_config={"视频链接": st.column_config.LinkColumn("视频链接", display_text="观看视频")},
                    disabled=list(odf.columns),
                    hide_index=True,
                    use_container_width=True,
                )

            st.markdown("**② 关键词热点**（按关键词搜上升视频，VPH 排序，产品可改）")
            hot_kw_text = st.text_area(
                "热点关键词（一行一个）",
                value="iphone stuck on apple logo\nwhatsapp transfer\ndata recovery",
                key="hot_kw",
                height=130,
            )
            hot_per_kw = st.number_input("每个关键词抓取视频数", 5, 50, 10, step=5, key="hot_per_kw")
            if st.button("追踪热点", icon=":material/trending_up:", key="hot_fetch"):
                hot_keywords = [k.strip() for k in hot_kw_text.splitlines() if k.strip()]
                if not hot_keywords:
                    st.warning("请先输入至少一个热点关键词")
                else:
                    with st.spinner("抓取热点视频 + 计算 VPH..."):
                        trending = track_hot_topics(hot_keywords, int(hot_per_kw))
                    if not trending:
                        st.warning("抓取失败（可能配额用尽）")
                    else:
                        st.session_state["trending"] = trending
                        st.success(f"抓取到 {len(trending)} 个上升视频（按 VPH 排序）")
            if st.session_state.get("trending"):
                tdf = pd.DataFrame(st.session_state["trending"])
                st.data_editor(
                    tdf,
                    key="trending_editor",
                    column_config={"视频链接": st.column_config.LinkColumn("视频链接", display_text="观看视频")},
                    disabled=list(tdf.columns),
                    hide_index=True,
                    use_container_width=True,
                )

            st.markdown("**③ 竞品热点**（搜竞品品牌，看它们在推什么、哪些在涨）")
            comp_kw_text = st.text_area(
                "竞品品牌关键词（一行一个）",
                value="\n".join(竞品默认关键词),
                key="comp_kw",
                height=130,
            )
            if st.button("追踪竞品热点", icon=":material/travel_explore:", key="comp_fetch"):
                comp_keywords = [k.strip() for k in comp_kw_text.splitlines() if k.strip()]
                if not comp_keywords:
                    st.warning("请先输入至少一个竞品关键词")
                else:
                    with st.spinner("抓取竞品热点视频 + 计算 VPH..."):
                        comp = track_hot_topics(comp_keywords, 10)
                    if not comp:
                        st.warning("抓取失败（可能配额用尽）")
                    else:
                        st.session_state["competitor_hot"] = comp
                        st.success(f"抓取到 {len(comp)} 个竞品上升视频（按 VPH 排序）")
            if st.session_state.get("competitor_hot"):
                cdf = pd.DataFrame(st.session_state["competitor_hot"])
                st.data_editor(
                    cdf,
                    key="competitor_hot_editor",
                    column_config={"视频链接": st.column_config.LinkColumn("视频链接", display_text="观看视频")},
                    disabled=list(cdf.columns),
                    hide_index=True,
                    use_container_width=True,
                )

            st.markdown("**④ Google 热榜 / 趋势**（Google Trends，非官方接口，偶尔会限流）")
            gc1, gc2 = st.columns(2)
            with gc1:
                gt_geo = st.selectbox("Google Trends 地区", GT_REGIONS, index=0, key="gt_geo")
            with gc2:
                st.write("")
                if st.button("获取实时热搜", icon=":material/trending_up:", key="gt_rss_btn", use_container_width=True):
                    with st.spinner("拉取 Google 实时热搜..."):
                        rss, err = google_trending_rss(gt_geo)
                    if err:
                        st.warning(err)
                    elif not rss:
                        st.info("没有热搜数据")
                    else:
                        st.session_state["gt_rss"] = rss
                        st.success(f"拉到 {len(rss)} 条实时热搜")
            if st.session_state.get("gt_rss"):
                st.dataframe(pd.DataFrame(st.session_state["gt_rss"]), hide_index=True, use_container_width=True)

            gt_kw = st.text_input("相关上升查询关键词", "data recovery", key="gt_kw")
            if st.button("查看上升查询", icon=":material/search:", key="gt_related_btn"):
                with st.spinner("查 Google Trends 相关上升查询..."):
                    rel, err = google_related_queries(gt_kw, gt_geo)
                if err:
                    st.warning(err)
                elif not rel:
                    st.info("没有找到相关上升查询")
                else:
                    st.session_state["gt_related"] = rel
                    st.success(f"找到 {len(rel)} 个相关上升查询")
            if st.session_state.get("gt_related"):
                st.dataframe(pd.DataFrame(st.session_state["gt_related"]), hide_index=True, use_container_width=True)

            gt_iot_kw = st.text_input(
                "热度走势对比（最多 5 个关键词，逗号分隔）",
                "data recovery, whatsapp transfer, ios 18",
                key="gt_iot",
            )
            if st.button("查看热度走势", icon=":material/show_chart:", key="gt_iot_btn"):
                iot_kws = [k.strip() for k in gt_iot_kw.split(",") if k.strip()][:5]
                if not iot_kws:
                    st.warning("请至少输入一个关键词")
                else:
                    with st.spinner("查 Google Trends 热度走势..."):
                        iot_df, err = google_interest_over_time(iot_kws, gt_geo)
                    if err:
                        st.warning(err)
                    else:
                        st.session_state["gt_iot_df"] = iot_df
                        st.success("已拉取热度走势")
            if st.session_state.get("gt_iot_df") is not None:
                st.line_chart(st.session_state["gt_iot_df"])
    elif wb_page == "reminders":
        render_reminder_center(EFFECT_FILE)
