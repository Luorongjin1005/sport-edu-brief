# -*- coding: utf-8 -*-
"""
体育教育专业信息破壁日报 · 每日自动聚合与推送程序
=================================================
特点：
1. 只用 Python 标准库，不需要 pip install 任何第三方包 —— 部署最省事
2. 一次运行同时产出 5 种格式：md / txt / json / html / feed.xml（RSS）
3. 三条抓取通道：Google News 关键词检索转 RSS、现成 RSS、政府栏目页面监控
4. 双层去重：标题哈希记忆 + 标题 3-gram 相似度，避免旧闻反复推送
5. 多通道推送，各自独立 try/except，任一渠道失败不影响其它渠道

运行命令：
    python sport_brief.py              # 正常抓取并推送
    python sport_brief.py --dry-run    # 抓取但不推送（先验证内容对不对）
    python sport_brief.py --self-test  # 不联网，用内置样例验证程序逻辑
    python sport_brief.py --test-push  # 只发一条测试通知，验证推送通道
    python sport_brief.py --no-gnews   # 跳过 Google News（国内网络环境下用）
"""

import argparse
import hashlib
import html as htmllib
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

# ============================ ① 配置区（只改这一段） ============================
# ---- 1.1 推送渠道：想用哪个就填哪个，留空自动跳过，程序不会报错 ----
BARK_KEY = ""                  # Bark App 里显示的那串 key，例如 "aBcDeF123456"
BARK_SERVER = "https://api.day.app"          # Bark 官方服务器；自建服务器改这里
BARK_GROUP = "体育教育破壁日报"                # Bark 通知的分组名，便于集中管理

WXPUSHER_APPTOKEN = ""         # WxPusher 应用的 appToken
WXPUSHER_UIDS = []             # 收件人 UID 列表，例如 ["UID_abc123"]

FEISHU_WEBHOOK = ""            # 飞书群机器人 webhook 完整地址（https://open.feishu.cn/...）

EMAIL_ENABLED = False          # 想用邮件推送就把这里改成 True
EMAIL_SMTP_HOST = "smtp.qq.com"
EMAIL_SMTP_PORT = 465
EMAIL_USER = ""                # 你的邮箱账号，例如 123456@qq.com
EMAIL_PASS = ""                # 邮箱"授权码"，不是登录密码
EMAIL_TO = ""                  # 收件邮箱，可以填自己

# ---- 1.2 日报行为参数 ----
TIMEZONE_OFFSET = 8            # 北京时间 UTC+8，不用改
FRESH_HOURS = 36               # 只收最近 N 小时内发布的内容，防止旧闻
MAX_ITEMS_PER_TAG = 6           # 每个标签下最多保留几条
TOTAL_MAX_ITEMS = 40           # 整份日报最多几条
PAGE_ITEMS_PER_SOURCE = 8      # 页面监控每个栏目每次最多取几条（页面按时间倒序）
SEEN_KEEP_DAYS = 60            # 去重记忆保留天数
SIMILARITY_THRESHOLD = 0.55    # 标题相似度阈值，超过即判定为同一条

# ---- 1.3 一句话摘要 ----
AI_SUMMARY_ENABLED = False     # 想给前几条加 AI 摘要就改成 True，并填下面的 Key
AI_BASE_URL = "https://api.deepseek.com/chat/completions"   # 兼容 OpenAI 协议的都可
AI_MODEL = "deepseek-chat"
AI_API_KEY = ""
AI_MAX_ITEMS = 12              # 只给前 N 条做 AI 摘要，控制花费

# ---- 1.4 输出与回链 ----
OUT_DIR = "out"                # 生成文件目录
DATA_DIR = "data"              # 去重记忆与 7 天历史目录（要提交回仓库）
RAW_BASE = "https://raw.githubusercontent.com/你的GitHub用户名/sport-edu-brief/main/"  # 结尾必须有斜杠

# ============================ ② 标签定义（顺序=日报里的显示顺序）============================
TAG_ORDER = [
    "规划-十五五",
    "政策-体教融合",
    "经济-产业数据",
    "学术-征稿",
    "就业-教师",
    "国际-PE",
    "深度-行业",
    "综合-资讯",
]

# ============================ ③ 检索词矩阵 ============================
# tag  = 这条搜索命中的内容默认归到哪个标签
# q    = Google News 检索式（支持 site: 限定站点）
QUERIES = [
    # —— 政策与规划 ——
    {"tag": "规划-十五五", "q": "十五五 规划 体育"},
    {"tag": "规划-十五五", "q": "政府工作报告 体育"},
    {"tag": "规划-十五五", "q": "中央经济工作会议 体育"},
    {"tag": "规划-十五五", "q": "体育强国 建设 规划"},
    {"tag": "政策-体教融合", "q": "site:gov.cn 体育 意见"},
    {"tag": "政策-体教融合", "q": "site:gov.cn 体育 规划"},
    {"tag": "政策-体教融合", "q": "site:gov.cn 体育 批复"},
    {"tag": "政策-体教融合", "q": "体教融合 体育教育"},
    {"tag": "政策-体教融合", "q": "教育部 体育师资 扩招"},
    {"tag": "政策-体教融合", "q": "三大球 后备人才"},
    {"tag": "政策-体教融合", "q": "学校体育 体育课 课时"},
    {"tag": "政策-体教融合", "q": "专兼职教练员岗位 学校"},
    {"tag": "政策-体教融合", "q": "公费师范生 体育"},
    # —— 经济与数据 ——
    {"tag": "经济-产业数据", "q": "体育产业 总规模 公告"},
    {"tag": "经济-产业数据", "q": "体育教育与培训 增加值"},
    {"tag": "经济-产业数据", "q": "赛事经济 消费"},
    {"tag": "经济-产业数据", "q": "户外运动 产业 目的地"},
    {"tag": "经济-产业数据", "q": "冰雪经济 规模"},
    {"tag": "经济-产业数据", "q": "体育用品 制造 升级"},
    # —— 学术与征稿 ——
    {"tag": "学术-征稿", "q": "体教融合 征稿"},
    {"tag": "学术-征稿", "q": "学校体育 征文"},
    {"tag": "学术-征稿", "q": "体育教育 课程改革"},
    {"tag": "学术-征稿", "q": "体育学 CSSCI 期刊 目录"},
    {"tag": "学术-征稿", "q": "AI 学校体育"},
    {"tag": "学术-征稿", "q": "体育科学学会 征文 通知"},
    # —— 国际 PE ——
    {"tag": "国际-PE", "q": "ICSSPE physical education"},
    {"tag": "国际-PE", "q": "AIESEP conference"},
    {"tag": "国际-PE", "q": "FIEP physical education"},
    # —— 就业招聘 ——
    {"tag": "就业-教师", "q": "体育教师 招聘 编制"},
    {"tag": "就业-教师", "q": "体育教育 公费师范生"},
    {"tag": "就业-教师", "q": "教育局 体育教师 公告"},
    # —— 行业深度 ——
    {"tag": "深度-行业", "q": "懒熊体育 体育培训"},
    {"tag": "深度-行业", "q": "体育大生意 体教融合"},
    # —— 综合 ——
    {"tag": "综合-资讯", "q": "体育 政策 新华社"},
]

# ============================ ④ 现成 RSS 源（已实测可用）============================
RSS_SOURCES = [
    # 国际体育教育组织官网 WordPress feed，实测返回标准 RSS 2.0
    {"tag": "国际-PE", "name": "ICSSPE", "url": "https://icsspe.org/feed",
     "must": r"physical education|sport|pedagog|PE\b|conference|webinar|event|call for|news"},
    {"tag": "国际-PE", "name": "AIESEP", "url": "https://aiesep.org/feed",
     "must": r"physical education|sport|pedagog|PE\b|conference|webinar|event|call for|news"},
    # 中国政府网唯一可用的 RSS 频道（原创推荐），靠关键词筛出体育教育相关
    {"tag": "综合-资讯", "name": "中国政府网原创推荐", "url": "https://www.gov.cn/pushinfo/v150203/rss.xml",
     "must": r"体育|教育|学校|运动|健身|青少年|学生"},
]

# 不同标签的信息稀缺程度不同：国际组织一个月才更新几条，用 36 小时会常年收不到
FRESH_HOURS_BY_TAG = {
    "国际-PE": 24 * 30,      # 国际组织动态：30 天窗口
    "学术-征稿": 24 * 14,    # 征文/预印本：14 天窗口
}

# ============================ ⑤ 页面监控源（无 RSS 的政府/行业栏目）============================
PAGES = [
    {"tag": "政策-体教融合", "name": "体育总局-通知公告", "url": "https://www.sport.gov.cn/n315/n20001395/index.html"},
    {"tag": "政策-体教融合", "name": "体育总局-信息公开", "url": "https://www.sport.gov.cn/n315/index.html"},
    {"tag": "规划-十五五", "name": "中国政府网-最新政策", "url": "https://www.gov.cn/zhengce/zuixin/"},
    {"tag": "经济-产业数据", "name": "国家统计局-最新发布", "url": "https://www.stats.gov.cn/sj/zxfb/"},
    {"tag": "国际-PE", "name": "FIEP-官网动态", "url": "https://fiep.net/"},
    {"tag": "深度-行业", "name": "懒熊体育-首页", "url": "https://www.lanxiongsports.com/"},
]

# 预印本平台：走 OSF 公开 JSON 接口，比抓网页稳。
# 实测结论（2026-11-07）：SportRxiv 最新 15 条预印本全部停留在 2021 年，平台基本停更；
# EdArXiv 每天都有新预印本，但以泛教育为主，必须用 must 关键词筛出体育相关。
OSF_SOURCES = [
    {"provider": "sportrxiv", "name": "SportRxiv", "tag": "学术-征稿",
     "must": r"physical education|sport|athlet|exercise|pedagog|体育"},
    {"provider": "edarxiv", "name": "EdArXiv", "tag": "学术-征稿",
     "must": r"physical education|sport|athlet|exercise|physical activity|体育"},
]
OSF_API = ("https://api.osf.io/v2/preprints/?filter%5Bprovider%5D={provider}"
           "&page%5Bsize%5D=15&sort=-date_published")

# ============================ ⑥ 标签细化规则 ============================
# 只对"政策类/规划类/综合类"的命中结果做二次归位，避免学术/就业条目被误抢
POLICY_LIKE_TAGS = {"政策-体教融合", "规划-十五五", "综合-资讯"}
REFINE_RULES = [
    (r"十五五|五年规划|规划纲要", "规划-十五五"),
    (r"政府工作报告|中央经济工作会议", "规划-十五五"),
    (r"体教融合|学校体育|体育课|体育师资|公费师范|教练员岗位|三大球|后备人才|3\+4", "政策-体教融合"),
    (r"体育产业|增加值|总规模|赛事经济|户外运动|冰雪经济|体育消费|体育用品", "经济-产业数据"),
    (r"征稿|征文|选题|CSSCI|期刊", "学术-征稿"),
    (r"招聘|编制|教育局|教师岗", "就业-教师"),
    (r"ICSSPE|AIESEP|FIEP|SportRxiv", "国际-PE"),
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
TZ = timezone(timedelta(hours=TIMEZONE_OFFSET))
LOG_LINES = []


# ============================ ⑦ 基础工具 ============================
def log(msg):
    """同时写进内存日志和标准输出，Actions 的控制台就是靠这个看"""
    line = "[%s] %s" % (datetime.now(TZ).strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    LOG_LINES.append(line)


def http_get(url, timeout=15, retries=2):
    """带重试的 GET，返回 bytes；失败抛异常由调用方兜住"""
    last_err = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last_err = e
            if i < retries:
                time.sleep(2 + i)
    raise last_err


def http_post_json(url, payload, timeout=20):
    """POST JSON，用于 Bark / WxPusher / 飞书 / AI 接口"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def strip_html(s):
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", s or "")
    s = re.sub(r"(?s)<[^>]+>", "", s)
    s = htmllib.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def norm_title(t):
    """标题归一化：去标点、去空白、去"- 来源"尾巴，用来做去重指纹"""
    t = strip_html(t)
    t = re.sub(r"\s*-\s*[^-]{1,20}$", "", t)      # 去掉 Google News 加的 " - 媒体名"
    t = re.sub(r"[【】\[\]（）()《》「」“”\"'·、，,。.：:；;！!？?—\-_\s]", "", t)
    return t.lower()


def title_hash(t):
    return hashlib.sha1(norm_title(t).encode("utf-8")).hexdigest()[:16]


def shingles(s, n=3):
    s = norm_title(s)
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}


def similarity(a, b):
    """3-gram Jaccard 相似度，0~1，越大越像"""
    if not a or not b:
        return 0.0
    A, B = shingles(a), shingles(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def now_str():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M")


# ============================ ⑧ 解析器 ============================
def parse_rss(raw_bytes, source_name, default_tag):
    """解析标准 RSS 2.0，返回统一结构的条目列表"""
    items = []
    try:
        root = ET.fromstring(raw_bytes)
    except Exception as e:
        log("  ! RSS 解析失败（%s）：%s" % (source_name, e))
        return items
    for node in root.iter("item"):
        title = node.findtext("title") or ""
        link = node.findtext("link") or ""
        desc = node.findtext("description") or ""
        pub = node.findtext("pubDate") or ""
        # 有些源用 dc:date
        if not pub:
            for child in node:
                if child.tag.endswith("date") and child.text:
                    pub = child.text
                    break
        src = node.findtext("source") or source_name
        dt = None
        if pub:
            try:
                dt = parsedate_to_datetime(pub)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except Exception:
                dt = None
        if not dt:
            try:
                dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except Exception:
                dt = None
        # 标题里的 " - 媒体名" 拆出来当来源
        clean_title = title
        m = re.match(r"(?s)(.+?)\s*-\s*([^-]{2,20})$", title)
        if m:
            clean_title, src2 = m.group(1).strip(), m.group(2).strip()
            if not node.findtext("source"):
                src = src2
        items.append({
            "title": strip_html(clean_title),
            "url": link.strip(),
            "summary": strip_html(desc)[:300],
            "source": strip_html(src)[:30] or source_name,
            "pub": dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M") if dt else "",
            "published_dt": dt,
            "tag": default_tag,
            "channel": "rss",
        })
    return items


NAV_WORDS = ("首页", "机构", "公开", "资讯", "服务", "互动", "更多", "登录", "注册",
             "无障碍", "简体", "繁体", "English", "网站地图", "联系我们", "关于我们")

# 页面监控噪音黑名单：政府页面底部/导航里的条目必须挡掉，否则日报会被污染
JUNK_TITLE = (r"ICP备|公安备案|备案号|客户端|小程序|官方微博|官方微信|微博、微信|邮箱|@|"
              r"版权所有|网站地图|免责声明|意见反馈|内部服务|建议办理结果|提案办理结果|"
              r"无障碍|订阅|RSS|分享到|打印本页|关闭窗口|上一篇|下一篇|返回顶部|更多")
# 只保留跟体育/教育真正相关的条目标题（英文站点按英文关键词判断）
PAGE_MUST_KEYWORD = (r"体育|学校|教育|师资|赛事|运动|训练|体质|户外|冰雪|健身|全运|球类|"
                     r"physical education|sport|pedagog|athlet|exercise")
# 导航型 URL：栏目首页、备案站、内部平台，一律不当作"新文件"
JUNK_URL = (r"beian\.miit\.gov\.cn|beian\.gov\.cn|zongju\.sport\.gov\.cn|/cdn-cgi/|/zt_\d+/|"
            r"/n\d+/index\.html|/index\.html$|javascript:|mailto:")


def parse_page(raw_bytes, page_url, source_name, default_tag, limit=8):
    """
    页面监控：把栏目页里的 <a href> 标题抓出来。
    政府网站普遍没有 RSS，这是最实用的替代方案。
    过滤三层：导航词 → 垃圾标题/垃圾 URL → 必须命中体育教育关键词
    """
    try:
        text = raw_bytes.decode("utf-8", "ignore")
    except Exception:
        text = str(raw_bytes)
    if not text.strip():
        text = raw_bytes.decode("gbk", "ignore")
    anchors = re.findall(r'(?is)<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', text)
    items, seen_local = [], set()
    for href, inner in anchors:
        title = strip_html(inner).replace("&nbsp;", " ").strip()
        if len(title) < 8 or len(title) > 90 or title in NAV_WORDS:
            continue
        if title.startswith(("+", "var ", "document", "\"")):
            continue
        full = urllib.parse.urljoin(page_url, href)
        if re.search(JUNK_URL, href, re.I) or re.search(JUNK_URL, full, re.I):
            continue
        if re.search(JUNK_TITLE, title, re.I):
            continue
        if not re.search(PAGE_MUST_KEYWORD, title, re.I):
            continue
        if full in seen_local:
            continue
        seen_local.add(full)
        items.append({
            "title": title,
            "url": full,
            "summary": "",
            "source": source_name,
            "pub": now_str(),
            "published_dt": datetime.now(TZ),
            "tag": default_tag,
            "channel": "page",
        })
        if len(items) >= limit:
            break
    return items


def parse_osf(raw_bytes, source_name="SportRxiv"):
    """解析 OSF 预印本 JSON 接口"""
    items = []
    try:
        data = json.loads(raw_bytes.decode("utf-8", "ignore"))
    except Exception as e:
        log("  ! %s 接口解析失败：%s" % (source_name, e))
        return items
    for row in data.get("data", []):
        attr = row.get("attributes", {}) or {}
        links = row.get("links", {}) or {}
        title = strip_html(attr.get("title") or "")
        if not title:
            continue
        pub = (attr.get("date_published") or attr.get("date_created") or "")[:10]
        dt = None
        if pub:
            try:
                dt = datetime.fromisoformat(pub).replace(tzinfo=timezone.utc)
            except Exception:
                dt = None
        items.append({
            "title": title,
            "url": links.get("html") or ("https://osf.io/preprints/sportrxiv/"),
            "summary": strip_html(attr.get("description") or "")[:300],
            "source": source_name,
            "pub": dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M") if dt else pub,
            "published_dt": dt,
            "tag": "学术-征稿",
            "channel": "osf",
        })
    return items


# ============================ ⑨ 抓取调度 ============================
def gnews_url(q):
    """把检索式转成 Google News RSS 地址（无需 API Key，永久免费）"""
    return ("https://news.google.com/rss/search?q=%s&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
            % urllib.parse.quote(q))


def collect(skip_gnews=False, skip_page=False):
    all_items = []

    if not skip_gnews:
        log("① 关键词检索通道：共 %d 条检索式" % len(QUERIES))
        ok, fail = 0, 0
        for item in QUERIES:
            url = gnews_url(item["q"])
            try:
                raw = http_get(url, timeout=15, retries=1)
                got = parse_rss(raw, "GoogleNews", item["tag"])
                all_items.extend(got)
                ok += 1
            except Exception as e:
                fail += 1
                log("  ! 检索失败 [%s] %s" % (item["q"], str(e)[:60]))
            time.sleep(0.6)   # 轻微限速，避免被 Google 判为爬虫
        log("  ✓ 检索通道成功 %d / 失败 %d" % (ok, fail))

    log("② 现成 RSS 通道：共 %d 个源" % len(RSS_SOURCES))
    for src in RSS_SOURCES:
        try:
            raw = http_get(src["url"])
            got = parse_rss(raw, src["name"], src["tag"])
            # 关键词筛：政府网/国际组织的通稿里大量非体育内容，必须按 must 规则过滤
            if src.get("must"):
                got = [g for g in got
                       if re.search(src["must"], (g["title"] or "") + " " + (g["summary"] or ""), re.I)]
            all_items.extend(got)
            log("  ✓ %s：%d 条" % (src["name"], len(got)))
        except Exception as e:
            log("  ! %s 抓取失败：%s" % (src["name"], str(e)[:70]))

    log("③ 预印本平台（OSF 接口）：共 %d 个" % len(OSF_SOURCES))
    for osf in OSF_SOURCES:
        try:
            raw = http_get(OSF_API.format(provider=osf["provider"]))
            got = parse_osf(raw, osf["name"])
            if osf.get("must"):
                got = [g for g in got
                       if re.search(osf["must"], (g["title"] or "") + " " + (g["summary"] or ""), re.I)]
            all_items.extend(got)
            log("  ✓ %s：%d 条" % (osf["name"], len(got)))
        except Exception as e:
            log("  ! %s 失败：%s" % (osf["name"], str(e)[:70]))

    if not skip_page:
        log("④ 页面监控通道：共 %d 个栏目" % len(PAGES))
        for p in PAGES:
            try:
                raw = http_get(p["url"])
                got = parse_page(raw, p["url"], p["name"], p["tag"], PAGE_ITEMS_PER_SOURCE)
                all_items.extend(got)
                log("  ✓ %s：%d 条候选" % (p["name"], len(got)))
            except Exception as e:
                # 政府网站反爬/超时很常见，只记录不中断
                log("  ! %s 监控失败：%s" % (p["name"], str(e)[:70]))
    log("合计抓取候选 %d 条" % len(all_items))
    return all_items


# ============================ ⑩ 去重 / 打标 / 摘要 ============================
def refine_tag(item):
    """政策类命中做二次归位，让标签更准"""
    if item["tag"] not in POLICY_LIKE_TAGS:
        return item["tag"]
    blob = (item["title"] or "") + " " + (item["summary"] or "")
    for pattern, tag in REFINE_RULES:
        if re.search(pattern, blob, re.I):
            return tag
    return item["tag"]


def dedup_and_filter(items, seen, history):
    """
    三层过滤：
    1) 时效：只留最近 FRESH_HOURS 小时（页面监控条目按"新出现"判断，不受此限）
    2) 历史指纹：seen.json 里出现过的直接丢掉 —— 这是"旧闻不复推"的核心
    3) 批内相似度：同一天同一事件被多家转载，只留第一条
    """
    now = datetime.now(TZ)
    # 历史里出现过的标题也算"已见过"，双保险
    hist_hashes = {title_hash(h["title"]) for h in history.get("items", [])}

    kept, dropped_old, dropped_dup, dropped_same = [], 0, 0, 0
    for it in items:
        h = title_hash(it["title"])
        if not it["title"] or len(it["title"]) < 6:
            continue
        if h in seen["titles"] or h in hist_hashes:
            dropped_dup += 1
            continue
        dt = it.get("published_dt")
        # 页面监控条目没有真实发布时间，按"本次新出现"处理，不受时效限制
        if it["channel"] != "page" and dt:
            hours = FRESH_HOURS_BY_TAG.get(it["tag"], FRESH_HOURS)
            if dt < now - timedelta(hours=hours) or dt > now + timedelta(hours=6):
                dropped_old += 1
                continue
        # 批内相似度去重
        dup = False
        for k in kept:
            if similarity(it["title"], k["title"]) >= SIMILARITY_THRESHOLD:
                dup = True
                break
        if dup:
            dropped_same += 1
            continue
        it["tag"] = refine_tag(it)
        kept.append(it)

    log("去重结果：保留 %d 条 | 历史重复 %d | 超出时效 %d | 批内相似 %d"
        % (len(kept), dropped_dup, dropped_old, dropped_same))
    return kept


def rule_summary(item):
    """零成本的抽取式摘要：取导语第一句，裁到 80 字"""
    text = strip_html(item.get("summary") or "")
    if not text:
        return "（该源未提供摘要，点标题看原文）"
    text = re.sub(r"^(本文|该文|记者|来源)[^，。]{0,10}[，。]", "", text)
    sents = re.split(r"(?<=[。！？!?])", text)
    out = ""
    for s in sents:
        if not s.strip():
            continue
        if len(out) + len(s) > 80:
            break
        out += s
    out = (out or text)[:90].strip()
    return out.rstrip("，。；") + "。"


def ai_summary(item):
    """可选的 AI 一句话摘要，兼容 OpenAI 协议的任何服务"""
    prompt = ("用一句不超过30字的中文，客观概括这条信息对体育教育专业学生的影响或信号，"
              "不要用'本文''该文''据悉'开头。\n标题：%s\n内容：%s"
              % (item["title"], (item.get("summary") or "")[:500]))
    payload = {
        "model": AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 100,
        "temperature": 0.2,
    }
    try:
        raw = http_post_json(AI_BASE_URL, payload, timeout=30)
        data = json.loads(raw)
        return data["choices"][0]["message"]["content"].strip().replace("\n", "")
    except Exception as e:
        log("  ! AI 摘要失败，回退规则摘要：%s" % str(e)[:60])
        return rule_summary(item)


def apply_summary(items):
    for idx, it in enumerate(items):
        if AI_SUMMARY_ENABLED and AI_API_KEY and idx < AI_MAX_ITEMS:
            it["brief"] = ai_summary(it)
        else:
            it["brief"] = rule_summary(it)
    return items


# ============================ ⑪ 产出文件 ============================
def build_sections(items):
    groups = {t: [] for t in TAG_ORDER}
    for it in items:
        groups.setdefault(it["tag"], []).append(it)
    sections = []
    for t in TAG_ORDER + [k for k in groups if k not in TAG_ORDER]:
        arr = groups.get(t) or []
        if not arr:
            continue
        # 有发布时间的按时间倒序，页面监控条目本身已按页面顺序
        arr.sort(key=lambda x: x.get("pub") or "", reverse=True)
        sections.append({"tag": t, "items": arr[:MAX_ITEMS_PER_TAG]})
    return sections


def render_txt(date_str, sections):
    total = sum(len(s["items"]) for s in sections)
    lines = ["体育教育破壁日报  %s" % date_str, "=" * 34,
             "今日共 %d 条 · 八类信息壁垒全扫描" % total, ""]
    for s in sections:
        lines.append("【%s】" % s["tag"])
        for i, it in enumerate(s["items"], 1):
            lines.append("%d. %s" % (i, it["title"]))
            if it.get("brief"):
                lines.append("   → %s" % it["brief"])
            lines.append("   %s | %s" % (it.get("source", ""), it.get("pub", "")[:16]))
            lines.append("   %s" % it.get("url", ""))
        lines.append("")
    lines.append("-" * 34)
    lines.append("由「体育教育破壁日报」自动生成，无需人工干预")
    return "\n".join(lines)


def render_md(date_str, sections):
    total = sum(len(s["items"]) for s in sections)
    md = ["# 体育教育破壁日报 %s" % date_str, "",
          "> 今日共 **%d** 条 · 八类信息壁垒全扫描 · 自动生成于 %s" % (total, now_str()), ""]
    for s in sections:
        md.append("## %s（%d）" % (s["tag"], len(s["items"])))
        md.append("")
        for i, it in enumerate(s["items"], 1):
            md.append("%d. **%s**" % (i, it["title"]))
            if it.get("brief"):
                md.append("   %s" % it["brief"])
            md.append("   `%s` · %s · [原文](%s)" % (it.get("source", ""), it.get("pub", "")[:16], it.get("url", "")))
            md.append("")
    md.append("---")
    md.append("本文件由 GitHub Actions 每日自动生成，手机端通过 Bark / 快捷指令 / RSS 阅读器接收。")
    return "\n".join(md)


def render_html(date_str, sections):
    total = sum(len(s["items"]) for s in sections)
    color = {"规划-十五五": "#1f4e79", "政策-体教融合": "#1f6b57", "经济-产业数据": "#8a5a1f",
             "学术-征稿": "#4a3b8a", "就业-教师": "#8a1f3d", "国际-PE": "#1f5f8a",
             "深度-行业": "#333333", "综合-资讯": "#5a5a5a"}
    cards = []
    for s in sections:
        c = color.get(s["tag"], "#333")
        rows = []
        for i, it in enumerate(s["items"], 1):
            rows.append(
                '<div class="item"><div class="it"><a href="%s" target="_blank">%s. %s</a></div>'
                '<div class="ib">%s</div>'
                '<div class="im">%s · %s</div></div>'
                % (htmllib.escape(it.get("url", "")), i, htmllib.escape(it["title"]),
                   htmllib.escape(it.get("brief", "")), htmllib.escape(it.get("source", "")),
                   htmllib.escape(it.get("pub", "")[:16]))
            )
        cards.append('<section><h2 style="border-left:5px solid %s">%s<span>%d</span></h2>%s</section>'
                     % (c, htmllib.escape(s["tag"]), len(s["items"]), "".join(rows)))
    return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>体育教育破壁日报 %(d)s</title><style>
:root{--bg:#f6f5f2;--fg:#1b1b1b;--sub:#6b6b6b}
*{box-sizing:border-box}
body{margin:0;padding:26px 22px 40px;background:var(--bg);color:var(--fg);
font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;line-height:1.6}
h1{font-size:22px;margin:0 0 6px;letter-spacing:.5px}
.meta{color:var(--sub);font-size:13px;margin-bottom:22px}
section{margin-bottom:26px}
h2{font-size:15px;margin:0 0 10px;padding-left:10px;display:flex;justify-content:space-between}
h2 span{color:var(--sub);font-weight:400;font-size:12px}
.item{background:#fff;border-radius:10px;padding:11px 14px;margin-bottom:8px;
box-shadow:0 1px 2px rgba(0,0,0,.05)}
.it a{color:var(--fg);text-decoration:none;font-weight:600;font-size:14px}
.ib{color:#3d3d3d;font-size:13px;margin-top:4px}
.im{color:var(--sub);font-size:11.5px;margin-top:6px}
footer{color:var(--sub);font-size:12px;text-align:center;margin-top:30px}
</style></head><body>
<h1>体育教育破壁日报</h1>
<div class="meta">%(d)s · 共 %(n)d 条 · 自动生成 %(t)s</div>
%(c)s
<footer>GitHub Actions 每日自动运行 · 无需人工干预</footer>
</body></html>""" % {"d": date_str, "n": total, "t": now_str(), "c": "".join(cards)}


def render_feed(history):
    """生成标准 RSS 2.0，供 RSS 阅读器 / 桌面小组件订阅（这是路径C的关键）"""
    items = []
    for h in history.get("items", [])[:80]:
        dt = h.get("pub") or ""
        try:
            rfc = datetime.strptime(dt[:16], "%Y-%m-%d %H:%M").replace(tzinfo=TZ).strftime("%a, %d %b %Y %H:%M:%S +0800")
        except Exception:
            rfc = datetime.now(TZ).strftime("%a, %d %b %Y %H:%M:%S +0800")
        items.append(
            "<item><title>%s</title><link>%s</link>"
            "<description>%s</description>"
            "<category>%s</category><pubDate>%s</pubDate>"
            "<guid isPermaLink=\"false\">%s</guid></item>"
            % (htmllib.escape(h["title"]), htmllib.escape(h.get("url", "")),
               htmllib.escape(h.get("brief") or ""), htmllib.escape(h.get("tag", "")),
               rfc, title_hash(h["title"]))
        )
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0"><channel>'
            '<title>体育教育破壁日报</title>'
            '<link>%s</link><description>体育教育专业八类信息壁垒每日聚合</description>'
            '<language>zh-cn</language><lastBuildDate>%s</lastBuildDate>%s'
            '</channel></rss>'
            % (RAW_BASE, datetime.now(TZ).strftime("%a, %d %b %Y %H:%M:%S +0800"), "".join(items)))


def write_outputs(date_str, sections, all_items, history):
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(os.path.join(OUT_DIR, "logs"), exist_ok=True)
    txt = render_txt(date_str, sections)
    md = render_md(date_str, sections)
    html_doc = render_html(date_str, sections)
    # published_dt 是 datetime 对象，不能直接进 JSON：这里只保留可序列化字段
    clean_sections = []
    for s in sections:
        clean_sections.append({
            "tag": s["tag"],
            "items": [{k: v for k, v in it.items() if k != "published_dt"} for it in s["items"]],
        })
    payload = {
        "date": date_str,
        "generated_at": now_str(),
        "total": sum(len(s["items"]) for s in sections),
        "sections": clean_sections,
        "text": txt,
    }
    files = {
        "brief.txt": txt,
        "brief.md": md,
        "brief.html": html_doc,
        "brief.json": json.dumps(payload, ensure_ascii=False, indent=1, default=str),
        "feed.xml": render_feed(history),
    }
    for name, content in files.items():
        with open(os.path.join(OUT_DIR, name), "w", encoding="utf-8") as f:
            f.write(content)
    log("已写出 %d 个文件到 %s/" % (len(files), OUT_DIR))
    return txt


# ============================ ⑫ 推送通道 ============================
def push_bark(txt, date_str, total):
    if not BARK_KEY:
        log("  - Bark 未配置，跳过")
        return
    try:
        title = "体育教育破壁日报 %s（%d条）" % (date_str[5:], total)
        body = txt[:900] + ("\n\n……全文请点开" if len(txt) > 900 else "")
        payload = {
            "device_key": BARK_KEY,
            "title": title,
            "body": body,
            "group": BARK_GROUP,
            "isArchive": 1,
            "url": RAW_BASE + "out/brief.html",
            "icon": "https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/1f3c5.png",
        }
        resp = http_post_json(BARK_SERVER.rstrip("/") + "/push", payload)
        log("  ✓ Bark 推送成功：%s" % resp[:100])
    except Exception as e:
        log("  ! Bark 推送失败：%s" % str(e)[:120])


def push_wxpusher(txt, date_str, total):
    if not WXPUSHER_APPTOKEN or not WXPUSHER_UIDS:
        log("  - WxPusher 未配置，跳过")
        return
    try:
        payload = {
            "appToken": WXPUSHER_APPTOKEN,
            "content": "## 体育教育破壁日报 %s\n\n%s" % (date_str, txt[:18000]),
            "summary": "破壁日报 %s，共 %d 条" % (date_str, total),
            "contentType": 3,
            "uids": WXPUSHER_UIDS,
        }
        resp = http_post_json("https://wxpusher.zjiecode.com/api/send/message", payload)
        log("  ✓ WxPusher 推送成功：%s" % resp[:100])
    except Exception as e:
        log("  ! WxPusher 推送失败：%s" % str(e)[:120])


def push_feishu(txt, date_str, total):
    if not FEISHU_WEBHOOK:
        log("  - 飞书未配置，跳过")
        return
    try:
        payload = {"msg_type": "text",
                   "content": {"text": "体育教育破壁日报 %s（%d条）\n\n%s" % (date_str, total, txt[:6000])}}
        resp = http_post_json(FEISHU_WEBHOOK, payload)
        log("  ✓ 飞书推送成功：%s" % resp[:100])
    except Exception as e:
        log("  ! 飞书推送失败：%s" % str(e)[:120])


def push_email(html_doc, date_str, total):
    if not EMAIL_ENABLED or not EMAIL_USER or not EMAIL_PASS:
        log("  - 邮件未配置，跳过")
        return
    try:
        msg = MIMEText(html_doc, "html", "utf-8")
        msg["Subject"] = Header("体育教育破壁日报 %s（%d条）" % (date_str, total), "utf-8")
        msg["From"] = EMAIL_USER
        msg["To"] = EMAIL_TO or EMAIL_USER
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(EMAIL_SMTP_HOST, EMAIL_SMTP_PORT, context=ctx, timeout=20) as s:
            s.login(EMAIL_USER, EMAIL_PASS)
            s.sendmail(EMAIL_USER, [EMAIL_TO or EMAIL_USER], msg.as_string())
        log("  ✓ 邮件推送成功")
    except Exception as e:
        log("  ! 邮件推送失败：%s" % str(e)[:120])


# ============================ ⑬ 主流程 ============================
def main():
    ap = argparse.ArgumentParser(description="体育教育破壁日报聚合程序")
    ap.add_argument("--dry-run", action="store_true", help="抓取并生成文件，但不推送")
    ap.add_argument("--self-test", action="store_true", help="不联网，用内置样例自检逻辑")
    ap.add_argument("--test-push", action="store_true", help="只发一条测试通知")
    ap.add_argument("--no-gnews", action="store_true", help="跳过 Google News（国内网络）")
    ap.add_argument("--no-page", action="store_true", help="跳过页面监控")
    args = ap.parse_args()

    # 环境变量优先（GitHub Actions 通过 Secrets 注入）
    global BARK_KEY, WXPUSHER_APPTOKEN, WXPUSHER_UIDS, FEISHU_WEBHOOK, AI_API_KEY, RAW_BASE
    BARK_KEY = os.environ.get("BARK_KEY", BARK_KEY)
    WXPUSHER_APPTOKEN = os.environ.get("WXPUSHER_APPTOKEN", WXPUSHER_APPTOKEN)
    if os.environ.get("WXPUSHER_UIDS"):
        WXPUSHER_UIDS = [x.strip() for x in os.environ["WXPUSHER_UIDS"].split(",") if x.strip()]
    FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", FEISHU_WEBHOOK)
    AI_API_KEY = os.environ.get("AI_API_KEY", AI_API_KEY)
    RAW_BASE = os.environ.get("RAW_BASE", RAW_BASE)
    if os.environ.get("EMAIL_USER"):
        globals()["EMAIL_ENABLED"] = True
        globals()["EMAIL_USER"] = os.environ["EMAIL_USER"]
        globals()["EMAIL_PASS"] = os.environ.get("EMAIL_PASS", "")
        globals()["EMAIL_TO"] = os.environ.get("EMAIL_TO", "")

    date_str = datetime.now(TZ).strftime("%Y-%m-%d")
    log("===== 体育教育破壁日报 开始运行 %s =====" % now_str())

    if args.test_push:
        push_bark("【测试】如果你想看到这条，说明推送通道已经通了。", date_str, 0)
        push_wxpusher("【测试】推送通道测试成功。", date_str, 0)
        push_feishu("【测试】推送通道测试成功。", date_str, 0)
        return

    # 自检模式：用内置样例走完整流程，不联网、不推送
    if args.self_test:
        sample = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>
<item><title>体育总局关于印发《体育强国建设"十五五"规划》的通知 - 国家体育总局</title>
<link>https://www.sport.gov.cn/demo1</link><pubDate>Sat, 07 Nov 2026 00:30:00 +0800</pubDate>
<description>为加快推进体育强国建设，明确十五五时期体育发展的目标任务与重点工程。</description></item>
<item><title>教育部：扩大体育类学科专业招生规模，加强体育师资队伍建设 - 中国教育报</title>
<link>https://www.moe.gov.cn/demo2</link><pubDate>Sat, 07 Nov 2026 01:10:00 +0800</pubDate>
<description>教育部表示将支持高校扩大体育教育专业招生，完善专兼职教练员岗位设置。</description></item>
<item><title>2025年全国体育产业总规模与增加值公告发布 - 国家统计局</title>
<link>https://www.stats.gov.cn/demo3</link><pubDate>Sat, 07 Nov 2026 02:00:00 +0800</pubDate>
<description>体育教育与培训类别增加值同比增长，体育产业总规模持续扩大。</description></item>
<item><title>体育总局关于印发《体育强国建设"十五五"规划》的通知</title>
<link>https://www.sport.gov.cn/demo1b</link><pubDate>Sat, 07 Nov 2026 03:00:00 +0800</pubDate>
<description>同上，用于验证批内相似度去重是否生效。</description></item>
</channel></rss>"""
        items = parse_rss(sample.encode("utf-8"), "自检样例", "政策-体教融合")
        seen = {"titles": {}}
        hist = {"items": []}
        kept = dedup_and_filter(items, seen, hist)
        kept = apply_summary(kept)
        sections = build_sections(kept)
        txt = render_txt(date_str, sections)
        os.makedirs("out_selftest", exist_ok=True)
        with open("out_selftest/brief.txt", "w", encoding="utf-8") as f:
            f.write(txt)
        print(txt)
        log("自检完成：输入 4 条 → 保留 %d 条（应保留 3 条，第 4 条为相似重复被丢弃）" % len(kept))
        return

    seen = load_json(os.path.join(DATA_DIR, "seen.json"), {"titles": {}})
    history = load_json(os.path.join(DATA_DIR, "history.json"), {"items": []})

    # 清理过期的去重记忆
    cutoff = (datetime.now(TZ) - timedelta(days=SEEN_KEEP_DAYS)).strftime("%Y-%m-%d")
    seen["titles"] = {k: v for k, v in seen["titles"].items() if v >= cutoff}
    log("去重记忆：%d 条指纹，保留 %d 天" % (len(seen["titles"]), SEEN_KEEP_DAYS))

    items = collect(skip_gnews=args.no_gnews, skip_page=args.no_page)
    if not items:
        log("!! 一条都没抓到，请检查网络或信源配置")
    kept = dedup_and_filter(items, seen, history)
    kept = apply_summary(kept)

    # 总量控制
    if len(kept) > TOTAL_MAX_ITEMS:
        kept = kept[:TOTAL_MAX_ITEMS]
    sections = build_sections(kept)

    # 更新历史（保留 7 天，供 RSS feed 与桌面端使用）
    today = date_str
    for it in kept:
        seen["titles"][title_hash(it["title"])] = today
        history["items"].insert(0, {
            "title": it["title"], "url": it["url"], "brief": it["brief"],
            "tag": it["tag"], "source": it["source"], "pub": it["pub"], "added": today,
        })
    keep_from = (datetime.now(TZ) - timedelta(days=7)).strftime("%Y-%m-%d")
    history["items"] = [h for h in history["items"] if h.get("added", "") >= keep_from][:300]
    save_json(os.path.join(DATA_DIR, "seen.json"), seen)
    save_json(os.path.join(DATA_DIR, "history.json"), history)

    txt = write_outputs(date_str, sections, kept, history)
    total = sum(len(s["items"]) for s in sections)

    # 运行日志落盘（也作为 Actions 的 artifact）
    log_path = os.path.join(OUT_DIR, "logs", "%s.log" % date_str)
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(LOG_LINES))

    if args.dry_run:
        log("--dry-run 模式：不推送。内容预览：")
        print(txt)
        return

    log("⑤ 推送阶段")
    # 当天没有任何新增时也推一条，方便你区分"程序挂了"和"今天确实没新闻"
    if total == 0:
        txt = ("体育教育破壁日报 %s\n今日暂无新增内容（近 7 天内容仍可在 RSS 中查看）。\n"
               "若连续多日为空，请到 GitHub Actions 日志排查。" % date_str)
        log("今日 0 条，推送空日报提示")
    push_bark(txt, date_str, total)
    push_wxpusher(txt, date_str, total)
    push_feishu(txt, date_str, total)
    if EMAIL_ENABLED:
        push_email(render_html(date_str, sections), date_str, total)
    log("===== 运行结束：共 %d 条 =====" % total)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        log("程序异常退出：%s" % e)
        sys.exit(1)
