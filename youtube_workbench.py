"""
YouTube 达人挖掘工作台（本地网页）
运行: python3 -m streamlit run youtube_workbench.py
"""

import os, re, json, time, requests
from datetime import datetime
import pandas as pd
import streamlit as st
import email_module

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

# ============ 建联库 ============

def load_library():
    if os.path.exists(LIBRARY_FILE):
        return pd.read_csv(LIBRARY_FILE, dtype=str)
    return pd.DataFrame()

def save_to_library(rows_df):
    existing = load_library()
    if not existing.empty:
        combined = pd.concat([existing, rows_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=["主页链接"], keep="first")
    else:
        combined = rows_df
    combined.to_csv(LIBRARY_FILE, index=False, encoding="utf-8-sig")

HISTORY_FILE = "达人库.csv"

def load_history():
    if os.path.exists(HISTORY_FILE):
        return pd.read_csv(HISTORY_FILE, dtype=str)
    return pd.DataFrame()

def save_history(rows_df):
    existing = load_history()
    if not existing.empty:
        combined = pd.concat([existing, rows_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=["主页链接"], keep="first")
    else:
        combined = rows_df
    combined.to_csv(HISTORY_FILE, index=False, encoding="utf-8-sig")

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
                    "title": sn.get("title", ""),
                    "channel_id": sn.get("channelId", ""),
                    "channel_name": sn.get("channelTitle", ""),
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
                    "title": s.get("title", ""),
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
            pub = datetime.fromisoformat(item["snippet"]["publishedAt"].replace("Z", "+00:00")).replace(tzinfo=None)
            if (datetime.now() - pub).days <= days:
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
        results.append({
            "频道名称": ch["title"],
            "邮箱": ch["email"],
            "主页链接": ch["url"],
            "代表视频标题": v.get("title", ""),
            "代表视频链接": f"https://www.youtube.com/watch?v={vid}" if vid else "",
            "缩略图": f"https://img.youtube.com/vi/{vid}/hqdefault.jpg" if vid else "",
            "订阅数": ch["subs"],
            "近6条均播": avg_views,
            "互动率": round(er, 1),
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
                "vid": vid, "title": title, "channel": channel,
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

# ============ 界面 ============

st.set_page_config(page_title="YouTube 达人挖掘工作台", layout="wide")
st.title("🎯 YouTube 达人挖掘工作台")

with st.sidebar:
    st.header("频道筛选条件")
    min_subs = st.number_input("最小粉丝数", 0, 100_000_000, 5000, step=1000)
    max_subs = st.number_input("最大粉丝数", 0, 100_000_000, 100000, step=10000)
    english_only = st.checkbox("仅英语频道", True)
    exclude_brand = st.checkbox("排除竞品官方号", True)
    has_email = st.checkbox("仅显示有邮箱", False)
    active_days = st.number_input("近 N 天活跃（0=不检查）", 0, 365, 30, step=1,
                                  help="0 表示不检查活跃度，速度更快")
    st.divider()
    st.subheader("质量筛选")
    min_avg_views = st.number_input("近6条平均播放 ≥", 0, 100_000_000, 0, step=500,
                                    help="0=不筛；填数值会拉取近6条视频算均播")
    min_er = st.number_input("互动率 ≥ %", 0.0, 100.0, 0.0, step=0.5,
                             help="0=不筛；互动率=近6条均播÷粉丝数")

tab1, tab2, tab3 = st.tabs(["🔍 关键词挖掘", "📚 达人库", "📧 建联库"])

# ============ Tab 1: 关键词挖掘 ============
with tab1:
    st.markdown("在下方输入关键词（**一行一个**），点「开始挖掘」。")

    keywords_text = st.text_area(
        "关键词",
        height=180,
        placeholder="iphone stuck on apple logo\nwhatsapp transfer android to iphone\nios 18 black screen",
    )

    if st.button("🚀 开始挖掘", type="primary", use_container_width=True):
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

            if not results:
                st.info("没有找到符合条件的频道，试试放宽条件或换关键词")
            else:
                st.session_state["last_results"] = results
                st.success(f"找到 {len(results)} 个频道（已自动存入达人库）")
                hist_rows = [{
                    "频道名称": r["频道名称"], "邮箱": r["邮箱"], "主页链接": r["主页链接"],
                    "代表视频标题": r["代表视频标题"], "代表视频链接": r["代表视频链接"],
                    "订阅数": r["订阅数"], "近6条均播": r["近6条均播"], "互动率": r["互动率"],
                    "命中关键词": r["命中关键词"], "来源": "关键词",
                    "挖掘时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
                } for r in results]
                save_history(pd.DataFrame(hist_rows))

    if "last_results" in st.session_state:
        results = st.session_state["last_results"]
        if results:
            pc1, pc2 = st.columns(2)
            with pc1:
                kw_product = st.selectbox("合作产品", email_module.PRODUCTS, key="kw_product")
            with pc2:
                kw_type = st.selectbox("合作类型", email_module.COOP_TYPES, key="kw_type")
            st.markdown("**勾选满意的达人 → 点「加入建联库」**（勾选框在最左边）")
            df = pd.DataFrame(results)
            event = st.dataframe(
                df,
                on_select="rerun",
                selection_mode="multi-row",
                column_config={
                    "缩略图": st.column_config.ImageColumn("视频", width="medium"),
                    "主页链接": st.column_config.LinkColumn("主页", display_text="打开"),
                    "代表视频链接": st.column_config.LinkColumn("代表视频", display_text="看视频"),
                    "互动率": st.column_config.NumberColumn("互动率(%)", format="%.1f"),
                },
                column_order=["缩略图","频道名称","邮箱","主页链接","代表视频标题","代表视频链接",
                              "订阅数","近6条均播","互动率","命中关键词"],
                hide_index=True,
                use_container_width=True,
            )

            selected_idx = event.selection.rows if event.selection else []

            col1, col2 = st.columns([1, 3])
            with col1:
                if st.button("💾 加入建联库", type="primary", use_container_width=True, key="add_lib_kw"):
                    if selected_idx:
                        sel = df.iloc[selected_idx].copy()
                        sel["状态"] = "待建联"
                        sel["合作产品"] = kw_product
                        sel["合作类型"] = kw_type
                        sel["发送状态"] = "待发送"
                        sel["加入时间"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                        save_to_library(sel)
                        st.success(f"已加入 {len(sel)} 个达人")
                    else:
                        st.warning("请先在表格左边勾选达人")
            with col2:
                lib = load_library()
                if not lib.empty:
                    st.markdown(f"📚 建联库已有 **{len(lib)}** 个达人（待建联 {sum(lib['状态']=='待建联')} 个）")
                else:
                    st.markdown("📚 建联库为空，勾选后加入")

# ============ 达人库 Tab: 相似达人推荐 ============
with tab2:
    st.subheader("🔗 相似达人推荐")
    st.markdown("输入**视频链接或频道主页链接**（一行一个），推荐相似视频（含频道名）。**纯爬虫，不消耗搜索配额**。")
    video_links_text = st.text_area(
        "视频 / 频道链接",
        height=100,
        placeholder="https://www.youtube.com/watch?v=xxxx\nhttps://www.youtube.com/@channel\nhttps://www.youtube.com/channel/UCxxxx",
    )

    if st.button("🔗 推荐相似视频", type="primary", use_container_width=True):
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
                    "缩略图": f"https://img.youtube.com/vi/{r['vid']}/hqdefault.jpg" if r["vid"] else "",
                    "视频标题": r["title"],
                    "视频链接": f"https://www.youtube.com/watch?v={r['vid']}" if r["vid"] else "",
                    "频道名称": r["channel"],
                    "频道链接": r["channel_url"],
                    "订阅数": ch.get("subs", 0),
                    "邮箱": ch.get("email", ""),
                    "播放量": r["views"],
                    "发布时间": r["age"],
                })
            st.session_state["similar_results"] = sim
            st.success(f"找到 {len(sim)} 个相似视频（已自动存入达人库）")
            hist_rows = [{
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
            sc1, sc2 = st.columns(2)
            with sc1:
                sim_product = st.selectbox("合作产品", email_module.PRODUCTS, key="sim_product")
            with sc2:
                sim_type = st.selectbox("合作类型", email_module.COOP_TYPES, key="sim_type")
            st.markdown("**勾选 → 加入建联库**")
            sdf = pd.DataFrame(sim)
            sev = st.dataframe(
                sdf,
                on_select="rerun",
                selection_mode="multi-row",
                column_config={
                    "缩略图": st.column_config.ImageColumn("视频", width="medium"),
                    "视频链接": st.column_config.LinkColumn("视频", display_text="看视频"),
                    "频道链接": st.column_config.LinkColumn("频道", display_text="打开频道"),
                },
                column_order=["缩略图","视频标题","视频链接","频道名称","频道链接","订阅数","邮箱","播放量","发布时间"],
                hide_index=True,
                use_container_width=True,
            )
            sel_idx = sev.selection.rows if sev.selection else []
            if st.button("💾 加入建联库", type="primary", use_container_width=True, key="add_lib_sim"):
                if sel_idx:
                    rows = []
                    for i in sel_idx:
                        r = sim[i]
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
                    st.success(f"已加入 {len(rows)} 个达人")
                else:
                    st.warning("请先在表格左边勾选")

# ============ 达人库 Tab: 历史达人库 ============
with tab2:
    st.divider()
    st.subheader("📚 历史达人库")
    hist = load_history()
    if hist.empty:
        st.info("达人库还是空的。去「关键词挖掘」或「相似达人推荐」跑一次，结果会自动存到这里，下次不用重复搜索。")
    else:
        c1, c2 = st.columns([3, 1])
        with c1:
            search = st.text_input("🔎 搜索（频道名 / 邮箱 / 命中关键词）", key="hist_search")
        with c2:
            st.markdown(f"共 **{len(hist)}** 个达人")
        h = hist
        if search:
            h = h[h.apply(lambda row: search.lower() in str(row).lower(), axis=1)]
        event = st.dataframe(
            h,
            on_select="rerun",
            selection_mode="multi-row",
            column_config={
                "主页链接": st.column_config.LinkColumn("主页链接", display_text="打开"),
                "代表视频链接": st.column_config.LinkColumn("代表视频链接", display_text="看视频"),
            },
            hide_index=True,
            use_container_width=True,
        )
        selected_idx = event.selection.rows if event.selection else []

        cb1, cb2 = st.columns([1, 3])
        with cb1:
            if st.button("📥 加入建联库", type="primary", use_container_width=True, key="hist_add_lib"):
                if selected_idx:
                    sel = h.iloc[selected_idx]
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
                            "合作产品": "",
                            "合作类型": "独立视频",
                            "发送状态": "待发送",
                            "状态": "待建联",
                            "加入时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        })
                    lib = load_library()
                    lib = pd.concat([lib, pd.DataFrame(rows)], ignore_index=True)
                    lib = lib.drop_duplicates(subset=["主页链接"], keep="first")
                    save_library_full(lib)
                    st.success(f"已加入 {len(rows)} 个达人，去「📧 建联&邮件」标签页选产品并发送")
                else:
                    st.warning("请先在表格左边勾选达人")
            if st.button("🗑 删除选中", use_container_width=True, key="hist_del"):
                if selected_idx:
                    sel_links = set(h.iloc[selected_idx]["主页链接"].tolist())
                    hist = hist[~hist["主页链接"].isin(sel_links)]
                    hist.to_csv(HISTORY_FILE, index=False, encoding="utf-8-sig")
                    st.success(f"已删除 {len(selected_idx)} 个达人")
                    st.rerun()
                else:
                    st.warning("请先勾选要删除的达人")
        with cb2:
            st.markdown("勾选达人 → 加入建联库 → 去「📧 建联&邮件」选产品/类型并发送")

        from io import BytesIO
        buf = BytesIO()
        hist.to_excel(buf, index=False, engine="openpyxl")
        st.download_button("📥 下载达人库 Excel", data=buf.getvalue(),
                           file_name=f"达人库_{datetime.now().strftime('%Y%m%d')}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ============ Tab 3: 建联库 ============
def save_library_full(df):
    """覆盖保存建联库（不按主页链接去重）"""
    df.to_csv(LIBRARY_FILE, index=False, encoding="utf-8-sig")

with tab3:
    st.markdown("## 建联库")

    # ---- 手动添加达人（单表格，直接发送）----
    st.subheader("手动添加达人（粘贴表格，直接发送）")
    st.markdown("每行一个达人，填 **频道 / 链接 / 合作类型 / 邮箱 / 合作产品**，填完点「发送」即可。")
    if "manual_df" not in st.session_state:
        st.session_state["manual_df"] = pd.DataFrame(
            [["", "", email_module.COOP_TYPES[0], "", email_module.PRODUCTS[0]] for _ in range(30)],
            columns=["频道名称", "链接", "合作类型", "邮箱", "合作产品"],
        )
    manual = st.data_editor(
        st.session_state["manual_df"],
        num_rows="fixed",
        column_config={
            "合作类型": st.column_config.SelectboxColumn("合作类型", options=email_module.COOP_TYPES),
            "合作产品": st.column_config.SelectboxColumn("合作产品", options=email_module.PRODUCTS),
        },
        use_container_width=True,
    )
    st.session_state["manual_df"] = manual
    valid = manual[manual["邮箱"].fillna("").str.strip() != ""]
    st.write(f"有效达人：**{len(valid)}** 条（有邮箱的）")
    if st.button("🚀 发送这些达人", type="primary", use_container_width=True):
        if len(valid) == 0:
            st.warning("请先填写至少一个达人的邮箱")
        else:
            st.session_state["confirm_manual"] = True

    if st.session_state.get("confirm_manual", False):
        st.warning(f"⚠️ 即将发送给 **{len(valid)}** 个达人，请确认无误后再发送：")
        st.dataframe(valid[["频道名称", "邮箱", "合作产品", "合作类型"]], hide_index=True, use_container_width=True)
        cc1, cc2 = st.columns(2)
        with cc1:
            if st.button("✅ 确认发送", type="primary", use_container_width=True):
                ok = fail = 0
                res = []
                for i, (_, row) in enumerate(valid.iterrows()):
                    name = row.get("频道名称", "") or "there"
                    product = row["合作产品"]
                    coop_type = row.get("合作类型", "独立视频")
                    video_link = row.get("链接", "")
                    subject, body = email_module.render_template(product, coop_type, name, video_link)
                    email = email_module.clean_email(row["邮箱"])
                    try:
                        email_module.send_email(email, subject, body)
                        ok += 1
                        res.append((email, "成功", ""))
                        email_module.log_sent_email(email, name, product, coop_type, "成功")
                    except Exception as e:
                        fail += 1
                        res.append((email, "失败", str(e)[:80]))
                        email_module.log_sent_email(email, name, product, coop_type, "失败", str(e)[:80])
                    time.sleep(1)
                st.session_state["confirm_manual"] = False
                st.success(f"发送完成：成功 {ok}，失败 {fail}")
                if fail:
                    st.dataframe(pd.DataFrame(res, columns=["邮箱", "状态", "错误"]), hide_index=True)
        with cc2:
            if st.button("❌ 取消", use_container_width=True):
                st.session_state["confirm_manual"] = False
                st.rerun()

    # ---- 可编辑建联库 ----
    st.subheader("建联库（可编辑）")
    lib = load_library()
    if lib.empty:
        st.info("建联库还是空的，去「关键词挖掘」或「相似达人」勾选加入，或上面手动添加")
    else:
        for col in ["合作产品", "合作类型", "发送状态"]:
            if col not in lib.columns:
                lib[col] = ""
        # 只展示可编辑的关键列
        edit_cols = ["频道名称", "主页链接", "合作类型", "邮箱", "合作产品", "发送状态", "订阅数"]
        edit_cols = [c for c in edit_cols if c in lib.columns]
        lib["删除"] = False  # 临时删除标记列
        edit_base = lib[edit_cols + ["删除"]].copy()
        if ("lib_edit_df" not in st.session_state) or (len(st.session_state["lib_edit_df"]) != len(edit_base)):
            st.session_state["lib_edit_df"] = edit_base
        edited = st.data_editor(
            st.session_state["lib_edit_df"],
            column_config={
                "合作产品": st.column_config.SelectboxColumn("合作产品", options=email_module.PRODUCTS),
                "合作类型": st.column_config.SelectboxColumn("合作类型", options=email_module.COOP_TYPES),
                "发送状态": st.column_config.SelectboxColumn("发送状态", options=["待发送", "已发送", "已回复"]),
                "删除": st.column_config.CheckboxColumn("删除"),
            },
            num_rows="fixed",
            use_container_width=True,
        )
        st.session_state["lib_edit_df"] = edited
        c_save, c_del = st.columns(2)
        with c_save:
            if st.button("💾 保存修改", use_container_width=True):
                for c in edit_cols:
                    lib[c] = edited[c].values
                lib = lib.drop(columns=["删除"])
                save_library_full(lib)
                st.success("已保存")
        with c_del:
            if st.button("🗑 删除勾选行", use_container_width=True):
                del_mask = edited["删除"].fillna(False).astype(bool)
                del_links = [str(l) for l, d in zip(edited["主页链接"], del_mask) if d and str(l).strip()]
                if del_links:
                    lib = lib[~lib["主页链接"].astype(str).isin(del_links)]
                    lib = lib.drop(columns=["删除"])
                    save_library_full(lib)
                    st.success(f"已删除 {len(del_links)} 行")
                    st.rerun()
                else:
                    st.warning("请先勾选要删除的行")

        # 发送邮件按钮（建联库下方）
        if st.button("🚀 发送建联库待发送邮件", type="primary", use_container_width=True, key="send_lib_btn"):
            st.session_state["confirm_lib"] = True

        if st.session_state.get("confirm_lib", False):
            lib_send = load_library()
            for col in ["合作产品", "合作类型", "发送状态"]:
                if col not in lib_send.columns:
                    lib_send[col] = ""
            to_send = lib_send[
                (lib_send["邮箱"].fillna("").str.strip() != "")
                & (lib_send["发送状态"] != "已发送")
                & (lib_send["合作产品"].fillna("").str.strip() != "")
            ]
            if len(to_send) == 0:
                st.warning("没有待发送的达人（需有邮箱 + 已选产品 + 状态不是已发送）")
                st.session_state["confirm_lib"] = False
            else:
                st.warning(f"⚠️ 即将发送给 **{len(to_send)}** 个达人，请确认无误后再发送：")
                st.dataframe(to_send[["频道名称", "邮箱", "合作产品", "合作类型"]], hide_index=True, use_container_width=True)
                cc1, cc2 = st.columns(2)
                with cc1:
                    if st.button("✅ 确认发送", type="primary", use_container_width=True):
                        ok, fail = 0, 0
                        for _, row in to_send.iterrows():
                            name = row.get("频道名称", "") or "there"
                            product = row["合作产品"]
                            coop_type = row.get("合作类型", "独立视频")
                            video_link = row.get("代表视频链接", "") or row.get("主页链接", "")
                            subject, body = email_module.render_template(product, coop_type, name, video_link)
                            email = email_module.clean_email(row["邮箱"])
                            try:
                                email_module.send_email(email, subject, body)
                                ok += 1
                                email_module.log_sent_email(email, name, product, coop_type, "成功")
                            except Exception as e:
                                fail += 1
                                email_module.log_sent_email(email, name, product, coop_type, "失败", str(e)[:200])
                            time.sleep(1)  # 避免发太快被限流
                        st.session_state["confirm_lib"] = False
                        st.success(f"发送完成：成功 {ok}，失败 {fail}（详情见下方发送记录）")
                with cc2:
                    if st.button("❌ 取消", use_container_width=True):
                        st.session_state["confirm_lib"] = False
                        st.rerun()

    # ---- 发送记录 ----
    st.subheader("发送记录")
    mail_log = email_module.load_mail_log()
    if mail_log.empty:
        st.info("还没有发送记录")
    else:
        st.write(f"共发送 **{len(mail_log)}** 封邮件")
        st.dataframe(mail_log, hide_index=True, use_container_width=True)
        from io import BytesIO
        mbuf = BytesIO()
        mail_log.to_excel(mbuf, index=False, engine="openpyxl")
        st.download_button("📥 下载邮件库 Excel", data=mbuf.getvalue(),
                           file_name=f"邮件库_{datetime.now().strftime('%Y%m%d')}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    # ---- 邮件模板预览 ----
    with st.expander("📄 邮件模板预览"):
        ca, cb = st.columns(2)
        with ca:
            p = st.selectbox("选择产品查看模板", email_module.PRODUCTS, key="tpl_preview")
        with cb:
            ct = st.selectbox("选择合作类型", email_module.COOP_TYPES, key="tpl_preview_type")
        subject, body = email_module.render_template(p, ct, "【达人名】")
        if subject is None:
            st.warning("该产品 + 该类型暂无模板")
        else:
            st.write(f"**主题**：{subject}")
            st.text(body)
