"""
iOS 热点检测器 v1
抓取渠道：科技网站 RSS + Reddit + Apple 论坛 + YouTube(免费 scraper)
按 iOS 相关性 + 热度打分，生成 Markdown 报告
运行：python3 hot_topic_detector.py
"""
import re, json, time, requests, feedparser
from datetime import datetime, timedelta, timezone

# ============ 数据源 ============

RSS_FEEDS = [
    ("9to5Mac", "https://9to5mac.com/feed/"),
    ("MacRumors", "https://feeds.macrumors.com/MacRumors-All"),
    ("AppleInsider", "https://appleinsider.com/rss/news/"),
    ("Macworld", "https://www.macworld.com/feed/"),
    ("iMore", "https://www.imore.com/rss.xml"),
]

REDDIT_SUBREDDITS = ["ios", "iphone", "jailbreak", "apple"]

# YouTube 用免费 scraper 搜的词（不烧 API 配额）
YOUTUBE_QUERIES = ["iOS 18 bug", "iOS 18 new features", "iPhone stuck fix"]

# iOS 相关性关键词（bug 动词 + 新功能/版本）
BUG_WORDS = [
    "not working", "error", "unable", "crash", "stuck", "frozen", "black screen",
    "won't turn on", "boot loop", "recovery mode", "battery drain", "overheat",
    "wifi", "bluetooth", "bug", "issue", "fix", "problem", "glitch", "broken",
    "failed", "reboot", "restart", "brick", "restore", "jailbreak", "downgrade",
    "battery", "lag", "freeze", "hang",
]
FEATURE_WORDS = [
    "new feature", "update", "release", "beta", "new in", "hands-on", "what's new",
]
VERSION_WORDS = ["ios 18", "ios 19", "ios 26", "iphone 16", "iphone 17", "ipad", "apple"]

def score_relevance(title):
    t = title.lower()
    score = 0
    hits = []
    for w in BUG_WORDS:
        if w in t:
            score += 2
            hits.append(w)
    for w in FEATURE_WORDS:
        if w in t:
            score += 1
            hits.append(w)
    if any(v in t for v in VERSION_WORDS):
        score += 2
        hits.append("iOS版本")
    return score, hits

# ============ 抓取函数 ============

def fetch_rss(feeds):
    items = []
    for name, url in feeds:
        try:
            f = feedparser.parse(url)
            for e in f.entries[:25]:
                pub = None
                if hasattr(e, "published_parsed") and e.published_parsed:
                    pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
                elif hasattr(e, "updated_parsed") and e.updated_parsed:
                    pub = datetime(*e.updated_parsed[:6], tzinfo=timezone.utc)
                items.append({
                    "title": e.get("title", ""),
                    "url": e.get("link", ""),
                    "source": name,
                    "published": pub,
                    "heat": 1,
                })
        except Exception as ex:
            print(f"  [RSS失败] {name}: {ex}")
    return items

def get_reddit_token():
    """用 client_credentials 获取 Reddit 应用令牌（读 email_config.ini 的 [reddit] 段）"""
    import configparser
    cfg = configparser.ConfigParser()
    cfg.read("email_config.ini")
    cid = cfg.get("reddit", "client_id", fallback="").strip()
    sec = cfg.get("reddit", "client_secret", fallback="").strip()
    if not cid or not sec:
        return None, "未配置 Reddit 凭证（email_config.ini 的 [reddit] client_id/client_secret）"
    auth = requests.auth.HTTPBasicAuth(cid, sec)
    r = requests.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=auth,
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": "ios-hot-topic-detector/1.0 by partner_ella"},
        timeout=15,
    )
    if r.status_code != 200:
        return None, f"Reddit 令牌获取失败：HTTP {r.status_code}"
    return r.json().get("access_token"), None

def fetch_reddit(subs):
    items = []
    token, err = get_reddit_token()
    if err:
        print(f"  [Reddit跳过] {err}")
        return items
    headers = {
        "User-Agent": "ios-hot-topic-detector/1.0 by partner_ella",
        "Authorization": f"Bearer {token}",
    }
    for sub in subs:
        try:
            r = requests.get(f"https://oauth.reddit.com/r/{sub}/hot?limit=40", headers=headers, timeout=15)
            if r.status_code != 200:
                print(f"  [Reddit失败] r/{sub}: {r.status_code}")
                continue
            for post in r.json()["data"]["children"]:
                d = post["data"]
                items.append({
                    "title": d.get("title", ""),
                    "url": "https://www.reddit.com" + d.get("permalink", ""),
                    "source": f"Reddit r/{sub}",
                    "published": datetime.fromtimestamp(d.get("created_utc", 0), tz=timezone.utc),
                    "heat": d.get("score", 0) + d.get("num_comments", 0),
                })
            time.sleep(1)
        except Exception as ex:
            print(f"  [Reddit失败] r/{sub}: {ex}")
    return items

def fetch_youtube(queries):
    items = []
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    for q in queries:
        try:
            url = f"https://www.youtube.com/results?search_query={requests.utils.quote(q)}&hl=en&gl=US"
            resp = s.get(url, timeout=10)
            if resp.status_code != 200:
                continue
            m = re.search(r'var ytInitialData\s*=\s*(.+?);</script>', resp.text, re.DOTALL)
            if not m:
                continue
            data = json.loads(m.group(1))
            contents = data["contents"]["twoColumnSearchResultsRenderer"]["primaryContents"]["sectionListRenderer"]["contents"]
            count = 0
            for sec in contents:
                for item in sec.get("itemSectionRenderer", {}).get("contents", []):
                    v = item.get("videoRenderer")
                    if not v:
                        continue
                    vid = v.get("videoId", "")
                    title = v.get("title", {}).get("runs", [{}])[0].get("text", "")
                    views = v.get("viewCountText", {}).get("simpleText", "")
                    ch = v.get("ownerText", {}).get("runs", [{}])[0].get("text", "")
                    items.append({
                        "title": title,
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "source": f"YouTube {ch}",
                        "published": None,
                        "heat": parse_views(views),
                    })
                    count += 1
                    if count >= 10:
                        break
                if count >= 10:
                    break
        except Exception as ex:
            print(f"  [YouTube失败] {q}: {ex}")
    return items

def parse_views(v):
    v = v.lower().replace("views", "").replace(",", "").strip()
    try:
        if "k" in v:
            return int(float(v.replace("k", "")) * 1000)
        if "m" in v:
            return int(float(v.replace("m", "")) * 1000000)
        return int(v) if v else 0
    except:
        return 0

# ============ 主流程 ============

def main():
    print("=" * 60)
    print(f"iOS 热点检测（{datetime.now().strftime('%Y-%m-%d %H:%M')}）")
    print("=" * 60)

    print("\n[1] 抓取 RSS 科技网站...")
    items = fetch_rss(RSS_FEEDS)
    print(f"    RSS: {len(items)} 条")

    print("[2] 抓取 Reddit...")
    items += fetch_reddit(REDDIT_SUBREDDITS)
    print(f"    累计: {len(items)} 条")

    print("[3] 抓取 YouTube（免费 scraper）...")
    items += fetch_youtube(YOUTUBE_QUERIES)
    print(f"    累计: {len(items)} 条")

    # 过滤 + 打分
    print("\n[4] 打分 + 过滤...")
    now = datetime.now(timezone.utc)
    scored = []
    for it in items:
        score, hits = score_relevance(it["title"])
        if score == 0:
            continue
        # 近期性：48小时内加分
        recent_bonus = 0
        if it["published"]:
            age_h = (now - it["published"]).total_seconds() / 3600
            if age_h <= 48:
                recent_bonus = 3
            elif age_h <= 72:
                recent_bonus = 1
        it["score"] = score + recent_bonus
        it["hits"] = hits
        scored.append(it)

    # 按热度+分数排序（综合：score 为主，heat 加权）
    scored.sort(key=lambda x: (-x["score"], -(x["heat"] or 0)))

    # 去重（按标题前 40 字符）
    seen = set()
    dedup = []
    for it in scored:
        key = it["title"][:40].lower()
        if key in seen:
            continue
        seen.add(key)
        dedup.append(it)

    print(f"    相关条目: {len(dedup)} 条")

    # 按渠道分组
    def category_of(source):
        if source.startswith("Reddit"):
            return "Reddit"
        if source.startswith("YouTube"):
            return "YouTube"
        return "科技网站"

    groups = {"科技网站": [], "Reddit": [], "YouTube": []}
    for it in dedup:
        cat = category_of(it["source"])
        if len(groups[cat]) < 20:
            groups[cat].append(it)

    # 生成 HTML 报告
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    html = ['<!DOCTYPE html><html><head><meta charset="utf-8"><title>iOS热点报告</title>',
            '<style>body{font-family:-apple-system,sans-serif;max-width:900px;margin:30px auto;padding:0 20px;color:#222}',
            'h1{font-size:24px}h2{font-size:19px;border-bottom:2px solid #eee;padding-bottom:6px;margin-top:28px}',
            '.item{margin:10px 0;padding:8px 0;border-bottom:1px solid #f0f0f0}',
            'a{color:#0066cc;text-decoration:none;font-size:16px}a:hover{text-decoration:underline}',
            '.meta{color:#888;font-size:13px;margin-top:3px}',
            '.badge{display:inline-block;background:#eef4ff;color:#2a6;padding:1px 7px;border-radius:10px;font-size:12px;margin-right:5px}',
            '</style></head><body>',
            f'<h1>📱 iOS 热点检测报告</h1><p style="color:#888">生成时间：{now_str}</p>']

    icons = {"科技网站": "📰", "Reddit": "👥", "YouTube": "🎬"}
    for cat in ["科技网站", "Reddit", "YouTube"]:
        items = groups[cat]
        html.append(f'<h2>{icons[cat]} {cat}（{len(items)} 条）</h2>')
        if not items:
            html.append('<p style="color:#999">暂无</p>')
        for it in items:
            hits = "、".join(it["hits"][:4])
            pub = it["published"].strftime('%m-%d %H:%M') if it["published"] else ""
            heat = it["heat"] if it["heat"] else 0
            html.append('<div class="item">')
            html.append(f'<a href="{it["url"]}" target="_blank">{it["title"]}</a>')
            meta = f'<span class="badge">{it["source"]}</span>热度 {heat}'
            if hits:
                meta += f' · 命中 {hits}'
            if pub:
                meta += f' · {pub}'
            html.append(f'<div class="meta">{meta}</div>')
            html.append('</div>')
    html.append('</body></html>')

    out_path = f"/Users/yewei/Desktop/iOS热点报告_{datetime.now().strftime('%Y%m%d')}.html"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(html))
    print(f"\n报告已生成：{out_path}")

    # 打印预览
    print(f"\n===== 各渠道数量 =====")
    for cat in ["科技网站", "Reddit", "YouTube"]:
        print(f"  {cat}: {len(groups[cat])} 条")

if __name__ == "__main__":
    main()
