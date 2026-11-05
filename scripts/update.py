#!/usr/bin/env python3
"""NVDA daily news + risk dashboard updater (stdlib only).

Runs in GitHub Actions on a schedule. Writes docs/data.json, which the static
page (docs/index.html) reads. Optional env vars:
  ANTHROPIC_API_KEY   -> Thai summaries + better headline sentiment via Claude
  NTFY_TOPIC          -> push to iPhone/iPad/Android via the ntfy app (NTFY_SERVER optional)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID -> push alerts to Telegram
  DISCORD_WEBHOOK_URL -> push alerts to Discord
  FINNHUB_API_KEY     -> analyst recommendation counts (free key at finnhub.io)
  SEC_USER_AGENT      -> contact string SEC asks automated clients to send
  OFFLINE_SAMPLE=1    -> generate synthetic data (for local testing only)
"""
from __future__ import annotations

import datetime as dt
import email.utils
import html
import json
import math
import os
import random
import re
import statistics
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data.json"
STATE = ROOT / "data" / "state.json"
TICKER = "NVDA"
UA = "Mozilla/5.0 (compatible; nvda-news-dashboard/1.0)"
SEC_UA = os.environ.get("SEC_USER_AGENT") or "nvda-radar dashboard (github.com/nytoff5389-boop/nvda-radar) nvda-radar-bot@users.noreply.github.com"

# --------------------------------------------------------------------------
# Trusted sources. tier 1 = primary source / major wire, tier 2 = major
# financial press with editorial standards. Anything else is dropped.
# --------------------------------------------------------------------------
TRUSTED = {
    "nvidianews.nvidia.com": ("NVIDIA Newsroom (ทางการ)", 1),
    "investor.nvidia.com": ("NVIDIA IR (ทางการ)", 1),
    "sec.gov": ("SEC EDGAR (เอกสารทางการ)", 1),
    "reuters.com": ("Reuters", 1),
    "bloomberg.com": ("Bloomberg", 1),
    "apnews.com": ("Associated Press", 1),
    "wsj.com": ("The Wall Street Journal", 1),
    "ft.com": ("Financial Times", 1),
    "cnbc.com": ("CNBC", 2),
    "barrons.com": ("Barron's", 2),
    "marketwatch.com": ("MarketWatch", 2),
    "nytimes.com": ("The New York Times", 2),
    "economist.com": ("The Economist", 2),
    "asia.nikkei.com": ("Nikkei Asia", 2),
    "theinformation.com": ("The Information", 2),
}
NEWS_DOMAINS = [d for d in TRUSTED if d not in ("nvidianews.nvidia.com", "investor.nvidia.com", "sec.gov")]

# Small finance-specific lexicon, in the spirit of Loughran & McDonald (2011).
POS = """beat beats record surge surges soar soars jump jumps rally rallies gain gains rise rises
upgrade upgrades upgraded strong stronger growth outperform raise raises raised boost boosts
expand expands partnership deal wins win approval approved buyback bullish tops exceeds
higher demand breakthrough launch launches""".split()
NEG = """miss misses missed fall falls fell drop drops plunge plunges slump slumps tumble tumbles
sink sinks downgrade downgrades downgraded weak weaker cut cuts lawsuit probe investigation
antitrust ban bans restriction restrictions export curbs tariff tariffs decline declines
loss losses warn warns warning bearish fraud recall delay delays shortage sell-off selloff
fine fined subpoena halt halts lower risk risks concern concerns slowdown""".split()


def http_get(url: str, headers: dict | None = None, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ------------------------------- prices -----------------------------------
def fetch_daily() -> list[dict]:
    """1y daily OHLCV. Yahoo chart API, fallback Stooq CSV."""
    try:
        raw = json.loads(http_get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{TICKER}?range=2y&interval=1d"))
        r = raw["chart"]["result"][0]
        q = r["indicators"]["quote"][0]
        rows = []
        for i, t in enumerate(r["timestamp"]):
            if None in (q["open"][i], q["high"][i], q["low"][i], q["close"][i]):
                continue
            rows.append({"t": dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d"),
                         "o": q["open"][i], "h": q["high"][i], "l": q["low"][i],
                         "c": q["close"][i], "v": q["volume"][i] or 0})
        if len(rows) > 210:
            return rows
    except Exception as e:  # noqa: BLE001
        print("yahoo daily failed:", e, file=sys.stderr)
    txt = http_get(f"https://stooq.com/q/d/l/?s={TICKER.lower()}.us&i=d").decode()
    rows = []
    for line in txt.strip().splitlines()[1:]:
        d, o, h, l, c, v = line.split(",")[:6]
        rows.append({"t": d, "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": float(v)})
    return rows[-504:]


def fetch_intraday() -> dict | None:
    try:
        raw = json.loads(http_get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{TICKER}?range=1d&interval=5m"))
        r = raw["chart"]["result"][0]
        m = r["meta"]
        closes = [c for c in r["indicators"]["quote"][0]["close"] if c is not None]
        return {"price": m.get("regularMarketPrice"), "prev_close": m.get("chartPreviousClose"),
                "time": m.get("regularMarketTime"), "closes_5m": closes}
    except Exception as e:  # noqa: BLE001
        print("intraday failed:", e, file=sys.stderr)
        return None


# ----------------------------- indicators ---------------------------------
def sma(xs, n):
    return [None if i + 1 < n else sum(xs[i + 1 - n:i + 1]) / n for i in range(len(xs))]


def ema(xs, n):
    k, out, prev = 2 / (n + 1), [], None
    for x in xs:
        prev = x if prev is None else x * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(xs, n=14):
    gains, losses, out = 0.0, 0.0, [None] * len(xs)
    for i in range(1, len(xs)):
        d = xs[i] - xs[i - 1]
        g, l = max(d, 0), max(-d, 0)
        if i <= n:
            gains += g; losses += l
            if i == n:
                gains /= n; losses /= n
                out[i] = 100 - 100 / (1 + gains / losses) if losses else 100
        else:
            gains = (gains * (n - 1) + g) / n
            losses = (losses * (n - 1) + l) / n
            out[i] = 100 - 100 / (1 + gains / losses) if losses else 100
    return out


def compute_technicals(rows):
    c = [r["c"] for r in rows]
    v = [r["v"] for r in rows]
    s20, s50, s200 = sma(c, 20), sma(c, 50), sma(c, 200)
    r14 = rsi(c)
    macd = [a - b for a, b in zip(ema(c, 12), ema(c, 26))]
    sig = ema(macd, 9)
    hist = [a - b for a, b in zip(macd, sig)]
    rets = [0.0] + [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]
    sd20 = [None if i < 20 else statistics.pstdev(c[i - 19:i + 1]) for i in range(len(c))]
    vol20 = [None if i < 21 else statistics.pstdev(rets[i - 19:i + 1]) * math.sqrt(252) for i in range(len(c))]
    for i, r in enumerate(rows):
        r.update(sma20=s20[i], sma50=s50[i], sma200=s200[i], rsi=r14[i], macd_hist=hist[i])
    i = len(c) - 1
    hi52 = max(r["h"] for r in rows[-252:])
    vols = [x for x in vol20[-252:] if x]
    return {
        "close": c[i], "prev_close": c[i - 1], "chg_pct": (c[i] / c[i - 1] - 1) * 100,
        "sma20": s20[i], "sma50": s50[i], "sma200": s200[i], "rsi": r14[i],
        "macd_hist": hist[i], "vol20_ann": vol20[i], "vol_median_1y": statistics.median(vols),
        "boll_up": s20[i] + 2 * sd20[i], "boll_dn": s20[i] - 2 * sd20[i],
        "hi52": hi52, "drawdown_pct": (c[i] / hi52 - 1) * 100,
        "avg_vol20": sum(v[i - 20:i]) / 20, "volume": v[i], "_rets": rets,
    }


def detect_anomalies(rows, tech, intraday):
    """Flags unusual chart behaviour. Each anomaly: key, severity(1-3), Thai text."""
    out = []
    rets = tech.pop("_rets")
    i = len(rows) - 1
    window = rets[i - 60:i]
    mu, sd = statistics.mean(window), statistics.pstdev(window)
    z = (rets[i] - mu) / sd if sd else 0
    d = rows[i]["t"]
    if abs(z) >= 2.5:
        out.append({"key": f"z-{d}", "sev": 3 if abs(z) >= 3.5 else 2,
                    "text": f"ราคาเคลื่อนไหวผิดปกติ {tech['chg_pct']:+.2f}% (z-score {z:+.1f} เทียบ 60 วัน)"})
    if tech["avg_vol20"] and tech["volume"] / tech["avg_vol20"] >= 2:
        out.append({"key": f"vol-{d}", "sev": 2,
                    "text": f"ปริมาณซื้อขายพุ่ง {tech['volume'] / tech['avg_vol20']:.1f} เท่าของค่าเฉลี่ย 20 วัน"})
    gap = (rows[i]["o"] / rows[i - 1]["c"] - 1) * 100
    if abs(gap) >= 3:
        out.append({"key": f"gap-{d}", "sev": 2, "text": f"ราคาเปิดกระโดด (gap) {gap:+.2f}% จากราคาปิดวันก่อน"})
    if tech["close"] > tech["boll_up"]:
        out.append({"key": f"bbu-{d}", "sev": 1, "text": "ราคาปิดทะลุกรอบบน Bollinger Band (20,2) อาจร้อนแรงเกินไป"})
    if tech["close"] < tech["boll_dn"]:
        out.append({"key": f"bbd-{d}", "sev": 2, "text": "ราคาปิดหลุดกรอบล่าง Bollinger Band (20,2)"})
    if tech["rsi"] is not None and tech["rsi"] >= 75:
        out.append({"key": f"rsih-{d}", "sev": 1, "text": f"RSI {tech['rsi']:.0f} อยู่ในโซนซื้อมากเกินไป (overbought)"})
    if tech["rsi"] is not None and tech["rsi"] <= 25:
        out.append({"key": f"rsil-{d}", "sev": 1, "text": f"RSI {tech['rsi']:.0f} อยู่ในโซนขายมากเกินไป (oversold)"})
    for k in range(max(1, i - 4), i + 1):
        a, b = rows[k - 1], rows[k]
        if None in (a["sma50"], a["sma200"]):
            continue
        if a["sma50"] <= a["sma200"] and b["sma50"] > b["sma200"]:
            out.append({"key": f"gx-{b['t']}", "sev": 1, "text": f"Golden Cross: SMA50 ตัดขึ้นเหนือ SMA200 ({b['t']})"})
        if a["sma50"] >= a["sma200"] and b["sma50"] < b["sma200"]:
            out.append({"key": f"dx-{b['t']}", "sev": 3, "text": f"Death Cross: SMA50 ตัดลงใต้ SMA200 ({b['t']})"})
    if tech["drawdown_pct"] <= -20:
        out.append({"key": f"dd-{d}", "sev": 2, "text": f"ราคาต่ำกว่าจุดสูงสุด 52 สัปดาห์ {tech['drawdown_pct']:.1f}% (เข้าเขต bear market)"})
    if tech["vol_median_1y"] and tech["vol20_ann"] and tech["vol20_ann"] > 1.5 * tech["vol_median_1y"]:
        out.append({"key": f"hv-{d}", "sev": 2,
                    "text": f"ความผันผวน 20 วัน {tech['vol20_ann'] * 100:.0f}% สูงกว่าปกติ (ค่ากลาง 1 ปี {tech['vol_median_1y'] * 100:.0f}%)"})
    if intraday and intraday.get("price") and intraday.get("prev_close"):
        mv = (intraday["price"] / intraday["prev_close"] - 1) * 100
        if abs(mv) >= 4:
            day = dt.datetime.fromtimestamp(intraday["time"] or 0, dt.timezone.utc).strftime("%Y-%m-%d")
            out.append({"key": f"intra-{day}-{int(mv // 2)}", "sev": 3 if abs(mv) >= 7 else 2,
                        "text": f"ระหว่างวันราคาเปลี่ยนแปลง {mv:+.2f}% จากราคาปิดก่อนหน้า"})
    return out


# -------------------------------- news ------------------------------------
def domain_of(url: str) -> str:
    host = urllib.parse.urlparse(url).hostname or ""
    for d in TRUSTED:
        if host == d or host.endswith("." + d):
            return d
    return ""


def parse_date(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return email.utils.parsedate_to_datetime(s).astimezone(dt.timezone.utc)
    except Exception:  # noqa: BLE001
        try:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
        except Exception:  # noqa: BLE001
            return None


def _gnews(q: str) -> list[dict]:
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    out = []
    for it in ET.fromstring(http_get(url)).iter("item"):
        src = it.find("source")
        d = domain_of((src.get("url") if src is not None else "") or "")
        if not d:
            continue  # publisher not on the trusted list -> drop
        title = re.sub(r"\s+-\s+[^-]+$", "", html.unescape(it.findtext("title") or "")).strip()
        out.append({"title": title, "url": it.findtext("link"), "domain": d,
                    "published": parse_date(it.findtext("pubDate"))})
    return out


def fetch_google_news(days: int) -> list[dict]:
    """One Google News query per trusted outlet (so each outlet is searched on its own, which
    makes cross-outlet corroboration possible), plus one combined query. Publisher domain is re-verified."""
    items, fails = [], 0
    for d in NEWS_DOMAINS:
        try:
            items += _gnews(f"(Nvidia OR NVDA) site:{d} when:{days}d")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print("gnews", d, e, file=sys.stderr)
    try:
        items += _gnews(f"(Nvidia OR NVDA) ({' OR '.join('site:' + d for d in NEWS_DOMAINS)}) when:{days}d")
    except Exception as e:  # noqa: BLE001
        fails += 1
    if fails > len(NEWS_DOMAINS) // 2:
        raise RuntimeError(f"{fails} Google News queries failed")
    return items


def fetch_nvidia_newsroom() -> list[dict]:
    root = ET.fromstring(http_get("https://nvidianews.nvidia.com/releases.xml"))
    return [{"title": html.unescape(it.findtext("title") or "").strip(), "url": it.findtext("link"),
             "domain": "nvidianews.nvidia.com", "published": parse_date(it.findtext("pubDate"))}
            for it in root.iter("item")]


FORM_TH = {"8-K": "รายงานเหตุการณ์สำคัญ", "10-Q": "งบการเงินรายไตรมาส", "10-K": "รายงานประจำปี",
           "4": "ผู้บริหาร/วงในซื้อขายหุ้น", "144": "แจ้งความประสงค์ขายหุ้น", "SC 13G": "ผู้ถือหุ้นใหญ่",
           "SC 13G/A": "ผู้ถือหุ้นใหญ่ (แก้ไข)", "DEF 14A": "หนังสือเชิญประชุมผู้ถือหุ้น"}


def fetch_sec_filings() -> list[dict]:
    raw = json.loads(http_get("https://data.sec.gov/submissions/CIK0001045810.json",
                              {"User-Agent": SEC_UA, "Accept-Encoding": "identity"}))
    r = raw["filings"]["recent"]
    out = []
    for i in range(min(40, len(r["form"]))):
        form, acc, doc = r["form"][i], r["accessionNumber"][i], r["primaryDocument"][i]
        if form not in FORM_TH:
            continue
        url = f"https://www.sec.gov/Archives/edgar/data/1045810/{acc.replace('-', '')}/{doc}"
        when = r.get("acceptanceDateTime", [None] * 99)[i] or r["filingDate"][i]
        out.append({"title": f"SEC Form {form}: {FORM_TH[form]}", "url": url, "domain": "sec.gov",
                    "published": parse_date(when)})
    return out


SYN = {"curb": "restrict", "restriction": "restrict", "limit": "restrict", "ban": "restrict", "control": "restrict",
       "weigh": "consider", "mull": "consider", "plan": "consider", "eye": "consider",
       "slump": "fall", "drop": "fall", "tumble": "fall", "sink": "fall", "slide": "fall", "plunge": "fall",
       "jump": "rise", "surge": "rise", "soar": "rise", "climb": "rise", "rally": "rise", "gain": "rise",
       "semiconductor": "chip", "processor": "chip", "gpu": "chip", "beijing": "china", "chinese": "china",
       "washington": "us", "u.s.": "us", "biden": "us", "trump": "us", "administration": "us",
       "result": "earning", "quarterly": "earning", "revenue": "earning", "sale": "earning"}


def tokens(s: str) -> set[str]:
    stop = {"nvidia", "nvda", "the", "a", "an", "to", "of", "in", "on", "for", "and", "as", "is", "with", "its",
            "at", "by", "from", "says", "said", "after", "stock", "shares", "share", "new", "over", "amid", "more"}
    out = set()
    for w in re.findall(r"[a-z0-9$%.]+", s.lower()):
        w = w.strip(".")
        if w in stop or len(w) < 2:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        w = SYN.get(w, w)
        if len(w) > 2 or w in ("us", "ai"):
            out.add(w)
    return out


def lexicon_sentiment(title: str) -> float:
    ws = re.findall(r"[a-z\-]+", title.lower())
    p = sum(w in POS for w in ws)
    n = sum(w in NEG for w in ws)
    return 0.0 if p + n == 0 else (p - n) / (p + n)


def claude_enrich(items: list[dict]) -> bool:
    """Optional: Thai summary + sentiment per headline using the Claude API."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not items:
        return False
    listing = "\n".join(f"{i}. [{it['source']}] {it['title']}" for i, it in enumerate(items))
    prompt = (
        "You are a careful equity news analyst. For each NVIDIA (NVDA) headline below, return JSON "
        "array items {\"i\": index, \"th\": one-sentence Thai summary of the headline only (no speculation),"
        " \"sent\": number from -1 (very negative for NVDA shareholders) to 1 (very positive), "
        "\"impact\": 1-3 expected materiality}. Base it only on the headline text. Output JSON only.\n\n"
        + listing)
    body = json.dumps({"model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"), "max_tokens": 4000,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            txt = json.loads(r.read())["content"][0]["text"]
        arr = json.loads(txt[txt.find("["): txt.rfind("]") + 1])
        for a in arr:
            it = items[int(a["i"])]
            it["summary_th"] = a.get("th", "")
            it["sentiment"] = max(-1.0, min(1.0, float(a.get("sent", it["sentiment"]))))
            it["impact"] = int(a.get("impact", 1))
        return True
    except Exception as e:  # noqa: BLE001
        print("claude enrich failed:", e, file=sys.stderr)
        return False


def collect_news(days: int = 2) -> tuple[list[dict], list[str]]:
    errors, raw = [], []
    for name, fn in (("Google News", lambda: fetch_google_news(days)),
                     ("NVIDIA Newsroom", fetch_nvidia_newsroom), ("SEC EDGAR", fetch_sec_filings)):
        try:
            raw += fn()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
    cutoff = now_utc() - dt.timedelta(days=days)
    relevant = re.compile(r"nvidia|nvda|jensen huang|blackwell|rubin|cuda|geforce|hopper|h100|h200|gb200|ai chip", re.I)
    raw = [it for it in raw if it["domain"] in ("nvidianews.nvidia.com", "sec.gov") or relevant.search(it["title"] or "")]
    junk = re.compile(r"stock (price|quote)|\| ?(nvidia|nvda)|^nvidia corp\.?$|^nvda\b.*quote|price target.*\| ", re.I)
    raw = [it for it in raw if not junk.search(it["title"] or "") and len((it["title"] or "").split()) >= 4]
    seen, items = set(), []
    for it in sorted(raw, key=lambda x: x["published"] or cutoff, reverse=True):
        if not it["title"] or (it["published"] and it["published"] < cutoff):
            continue
        k = it["title"].lower()[:80]
        if k in seen:
            continue
        seen.add(k)
        name, tier = TRUSTED[it["domain"]]
        items.append({**it, "source": name, "tier": tier,
                      "published": it["published"].isoformat() if it["published"] else None,
                      "sentiment": lexicon_sentiment(it["title"]), "impact": 1, "summary_th": ""})
    items = items[:60]
    # Verification: a story counts as verified only if it comes from the company/regulator itself,
    # or at least 2 *different* trusted outlets (one of them Tier 1) report the same story within 48h.
    toks = [tokens(i["title"]) for i in items]
    when = [dt.datetime.fromisoformat(i["published"]) if i["published"] else None for i in items]
    for a, ia in enumerate(items):
        outlets, tiers = {ia["domain"]}, {ia["tier"]}
        for b, ib in enumerate(items):
            if a == b or ib["domain"] in outlets or not toks[a] or not toks[b]:
                continue
            shared = toks[a] & toks[b]
            j = len(shared) / len(toks[a] | toks[b])
            close_in_time = not (when[a] and when[b]) or abs((when[a] - when[b]).total_seconds()) <= 48 * 3600
            if close_in_time and (len(shared) >= 3 or (len(shared) >= 2 and j >= 0.2)):
                outlets.add(ib["domain"]); tiers.add(ib["tier"])
        ia["corroborated_by"] = len(outlets)
        ia["sources"] = sorted(TRUSTED[d][0] for d in outlets)
        official = ia["domain"] in ("nvidianews.nvidia.com", "investor.nvidia.com", "sec.gov")
        ia["status"] = "official" if official else ("verified" if len(outlets) >= 2 and 1 in tiers else "single")
    # keep the most-corroborated version of each story first
    items.sort(key=lambda x: ({"official": 0, "verified": 1, "single": 2}[x["status"]], -(x["corroborated_by"]), -x.get("_t", 0)))
    # collapse the same story told by several outlets into one card (sources list keeps every outlet)
    kept, kept_toks = [], []
    for it in items:
        t = tokens(it["title"])
        if it["status"] != "official" and any(len(t & k) >= 3 or (len(t & k) >= 2 and len(t & k) / len(t | k) >= 0.2) for k in kept_toks):
            continue
        kept.append(it); kept_toks.append(t)
    items[:] = kept
    used_claude = claude_enrich(items)
    return items, errors + ([] if used_claude or not os.environ.get("ANTHROPIC_API_KEY") else ["Claude enrich failed"])


# ------------------------------ outlook -----------------------------------
THEMES = [  # (key, Thai label, regex)
    ("china", "จีน / กฎส่งออกชิป", r"china|chinese|export|curb|ban|beijing|huawei|h20|tariff"),
    ("regulation", "กฎหมาย / คดี / ภาครัฐ", r"antitrust|lawsuit|probe|regulat|doj|ftc|\beu\b|court|sue|fine"),
    ("competition", "คู่แข่ง (AMD, Intel, ชิปของ Big Tech)", r"\bamd\b|intel|broadcom|tpu|trainium|custom chip|asic|rival|competit"),
    ("demand", "ดีมานด์ AI / Data center", r"demand|data.?center|hyperscal|capex|order|backlog|blackwell|rubin|ai spending"),
    ("supply", "การผลิต / ซัพพลาย (TSMC ฯลฯ)", r"tsmc|supply|shortage|foxconn|hon hai|packaging|cowos|yield|delay"),
    ("earnings", "งบการเงิน / คาดการณ์รายได้", r"earnings|revenue|guidance|forecast|quarter|results|eps|profit"),
    ("valuation", "ราคาแพง / ฟองสบู่ AI", r"bubble|valuation|overvalued|expensive|sell-?off|correction"),
]


def news_themes(news: list[dict]) -> list[dict]:
    out = []
    for key, label, rx in THEMES:
        hits = [n for n in news if re.search(rx, n["title"], re.I)]
        if not hits:
            continue
        tone = sum(n["sentiment"] for n in hits) / len(hits)
        out.append({"key": key, "label": label, "count": len(hits), "tone": round(tone, 2),
                    "example": hits[0]["summary_th"] or hits[0]["title"], "url": hits[0]["url"]})
    return sorted(out, key=lambda x: (x["tone"], -x["count"]))


def claude_outlook(news: list[dict], tech: dict) -> list[str] | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not news:
        return None
    heads = "\n".join(f"- [{n['source']}] {n['title']}" for n in news[:25])
    prompt = ("From only these recent NVIDIA headlines, list the 3 most important forward-looking risks or catalysts "
              "for NVDA shareholders over the next 1-3 months. Write each as one short, casual, easy Thai sentence "
              "(no jargon, no price predictions, no advice). Output a JSON array of 3 strings only.\n\n" + heads)
    body = json.dumps({"model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"), "max_tokens": 800,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            txt = json.loads(r.read())["content"][0]["text"]
        return [str(x) for x in json.loads(txt[txt.find("["): txt.rfind("]") + 1])][:3]
    except Exception as e:  # noqa: BLE001
        print("claude outlook failed:", e, file=sys.stderr)
        return None


def build_outlook(news, tech):
    vol = tech["vol20_ann"]
    c = tech["close"]
    rng = {}
    for label, days in (("1w", 5), ("1m", 21)):
        m = vol * math.sqrt(days / 252)  # 1-sigma move under a random-walk assumption
        rng[label] = {"pct": round(m * 100, 1), "lo": round(c * math.exp(-m), 2), "hi": round(c * math.exp(m), 2)}
    good = [n for n in news if n.get("status") in ("official", "verified") or n["tier"] == 1]
    return {"themes": news_themes(good), "range": rng, "ai": claude_outlook(good, tech)}


# --------------------------- investor sentiment ---------------------------
def fetch_stocktwits() -> dict:
    raw = json.loads(http_get(f"https://api.stocktwits.com/api/2/streams/symbol/{TICKER}.json"))
    msgs = raw.get("messages", [])
    bull = bear = 0
    sample = []
    for m in msgs:
        tag = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
        bull += tag == "Bullish"
        bear += tag == "Bearish"
        if tag and len(sample) < 6:
            sample.append({"tag": tag, "body": re.sub(r"\s+", " ", m.get("body", ""))[:220],
                           "user": (m.get("user") or {}).get("username", ""), "at": m.get("created_at")})
    tagged = bull + bear
    return {"messages": len(msgs), "bull": bull, "bear": bear,
            "bull_pct": round(100 * bull / tagged, 1) if tagged else None,
            "watchers": (raw.get("symbol") or {}).get("watchlist_count"), "sample": sample}


def fetch_fear_greed() -> dict:
    raw = json.loads(http_get("https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
                              {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36", "Referer": "https://edition.cnn.com/"}))
    f = raw["fear_and_greed"]
    return {k: (round(f[k], 1) if isinstance(f.get(k), (int, float)) else f.get(k))
            for k in ("score", "rating", "previous_close", "previous_1_week", "previous_1_month")}


def fetch_vix() -> dict:
    raw = json.loads(http_get("https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=1mo&interval=1d"))
    r = raw["chart"]["result"][0]
    closes = [c for c in r["indicators"]["quote"][0]["close"] if c is not None]
    v = r["meta"].get("regularMarketPrice") or closes[-1]
    return {"value": round(v, 2), "prev_close": round(closes[-2], 2) if len(closes) > 1 else None,
            "week_ago": round(closes[-6], 2) if len(closes) > 5 else None,
            "month_ago": round(closes[0], 2) if closes else None}


def fetch_analysts() -> dict | None:
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return None
    arr = json.loads(http_get(f"https://finnhub.io/api/v1/stock/recommendation?symbol={TICKER}&token={key}"))
    if not arr:
        return None
    keys = ("period", "strongBuy", "buy", "hold", "sell", "strongSell")
    a = {k: arr[0].get(k) for k in keys}
    a["history"] = [{k: x.get(k) for k in keys} for x in arr[:6]]
    try:
        today = now_utc().date()
        cal = json.loads(http_get(
            f"https://finnhub.io/api/v1/calendar/earnings?symbol={TICKER}&from={today}"
            f"&to={today + dt.timedelta(days=120)}&token={key}"))
        ev = sorted(cal.get("earningsCalendar", []), key=lambda e: e.get("date", ""))
        if ev:
            e = ev[0]
            a["next_earnings"] = {"date": e.get("date"), "hour": e.get("hour"), "eps_est": e.get("epsEstimate"),
                                  "rev_est": e.get("revenueEstimate")}
    except Exception as e:  # noqa: BLE001
        print("earnings calendar failed:", e, file=sys.stderr)
    return a


def collect_sentiment(state: dict) -> tuple[dict, list[str]]:
    out, errs = {}, []
    for name, fn in (("stocktwits", fetch_stocktwits), ("fear_greed", fetch_fear_greed), ("vix", fetch_vix), ("analysts", fetch_analysts)):
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001
            out[name] = None
            errs.append(f"{name}: {e}")
    if out.get("vix"):  # CNN often blocks bots; VIX stands in, so no need to surface that error
        errs = [e for e in errs if not e.startswith("fear_greed")]
    st = out.get("stocktwits")
    hist = state.get("senti_hist", [])
    if st and st.get("bull_pct") is not None:
        hist.append({"t": now_utc().isoformat(timespec="minutes"), "bull_pct": st["bull_pct"]})
    state["senti_hist"] = hist[-200:]
    out["history"] = state["senti_hist"]
    return out, errs


# ------------------------------- scoring ----------------------------------
LEVELS = [  # (upper bound of score, label, lean, colour key)
    (-2.5, "เสี่ยงมาก", "โน้มไปทาง SELL / ลดสถานะ", "vbad"),
    (-0.75, "เสี่ยง", "ระวัง / พิจารณาขายบางส่วน", "bad"),
    (0.75, "ปกติ", "HOLD / รอสัญญาณ", "neutral"),
    (2.5, "ดี", "ทยอยสะสม (โน้มไปทาง BUY)", "good"),
    (99, "ดีมาก", "โน้มไปทาง BUY", "vgood"),
]


def score(tech, anomalies, news):
    parts = []

    def add(name, val, why):
        parts.append({"name": name, "value": round(val, 2), "why": why})

    c = tech["close"]
    add("แนวโน้มระยะยาว", 1 if c > tech["sma200"] else -1,
        "ราคาอยู่เหนือ SMA200" if c > tech["sma200"] else "ราคาอยู่ใต้ SMA200")
    add("แนวโน้มระยะกลาง", 0.5 if tech["sma50"] > tech["sma200"] else -0.5,
        "SMA50 > SMA200" if tech["sma50"] > tech["sma200"] else "SMA50 < SMA200")
    add("โมเมนตัม MACD", 0.5 if tech["macd_hist"] > 0 else -0.5,
        "MACD histogram เป็นบวก" if tech["macd_hist"] > 0 else "MACD histogram เป็นลบ")
    r = tech["rsi"]
    if r >= 70:
        add("RSI", -0.5, f"RSI {r:.0f} ซื้อมากเกินไป เสี่ยงย่อตัว")
    elif r <= 30:
        add("RSI", 0.5, f"RSI {r:.0f} ขายมากเกินไป มีโอกาสเด้ง")
    else:
        add("RSI", 0, f"RSI {r:.0f} อยู่ในช่วงปกติ")
    hv = tech["vol20_ann"] > 1.3 * tech["vol_median_1y"]
    add("ความผันผวน", -0.75 if hv else 0.25, "ผันผวนสูงกว่าปกติ" if hv else "ผันผวนในระดับปกติ")
    used = [n for n in news if n.get("status") in ("official", "verified")]
    if not used:  # fall back to single-source Tier 1 stories at half weight
        used = [dict(n, impact=n.get("impact", 1) * 0.5) for n in news if n["tier"] == 1]
    news = used
    if news:
        w = [(1.5 if n["tier"] == 1 else 1.0) * n.get("impact", 1) * (1 + 0.25 * (n["corroborated_by"] - 1))
             for n in news]
        s = sum(n["sentiment"] * wi for n, wi in zip(news, w)) / sum(w)
        add("ข่าว (sentiment ถ่วงน้ำหนัก)", s * 2, f"ค่าเฉลี่ยโทนข่าว {s:+.2f} จาก {len(news)} ข่าวที่ยืนยันแล้ว")
    else:
        add("ข่าว (sentiment ถ่วงน้ำหนัก)", 0, "ไม่มีข่าวจากแหล่งน่าเชื่อถือในช่วงนี้")
    sev = sum(a["sev"] for a in anomalies if not a["key"].startswith(("gx-", "rsil-")))
    add("ความผิดปกติของกราฟ", -min(1.5, sev * 0.25), f"พบ {len(anomalies)} สัญญาณผิดปกติ")
    total = sum(p["value"] for p in parts)
    for ub, label, lean, key in LEVELS:
        if total <= ub:
            return {"score": round(total, 2), "level": label, "lean": lean, "key": key, "parts": parts}


# ----------------------------- notifications ------------------------------
def notify(text: str, title: str = "NVDA", priority: int = 3, tags: str = "chart_with_upwards_trend"):
    # ntfy: real push to iPhone/iPad/Android via the free ntfy app (APNs on iOS)
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        body = {"topic": topic, "title": title, "message": text, "priority": priority,
                "tags": [t for t in tags.split(",") if t]}
        if os.environ.get("PAGE_URL"):
            body["click"] = os.environ["PAGE_URL"]
        try:
            urllib.request.urlopen(urllib.request.Request(
                server + "/", data=json.dumps(body, ensure_ascii=False).encode(),
                headers={"content-type": "application/json", "User-Agent": UA}), timeout=20)
        except Exception as e:  # noqa: BLE001
            print("ntfy failed:", e, file=sys.stderr)
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"https://api.telegram.org/bot{tok}/sendMessage",
                data=urllib.parse.urlencode({"chat_id": chat, "text": text,
                                             "disable_web_page_preview": "true"}).encode()), timeout=20)
        except Exception as e:  # noqa: BLE001
            print("telegram failed:", e, file=sys.stderr)
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if hook:
        try:
            urllib.request.urlopen(urllib.request.Request(
                hook, data=json.dumps({"content": text[:1900]}).encode(),
                headers={"content-type": "application/json", "User-Agent": UA}), timeout=20)
        except Exception as e:  # noqa: BLE001
            print("discord failed:", e, file=sys.stderr)


# ------------------------------ sample mode -------------------------------
def synthetic_daily():
    random.seed(7)
    rows, p, day = [], 120.0, dt.date(2024, 11, 1)
    while len(rows) < 504:
        day += dt.timedelta(days=1)
        if day.weekday() >= 5:
            continue
        r = random.gauss(0.0012, 0.028)
        if len(rows) == 503:
            r = -0.071
        o = p * (1 + random.gauss(0, 0.006))
        c = p * math.exp(r)
        rows.append({"t": day.isoformat(), "o": o, "h": max(o, c) * 1.01, "l": min(o, c) * 0.99, "c": c,
                     "v": random.randint(150, 300) * 1e6 * (2.6 if len(rows) == 503 else 1)})
        p = c
    return rows


def synthetic_news():
    t = now_utc()
    raw = [("Nvidia shares slump as US weighs new chip export curbs to China", "reuters.com"),
           ("US considers tighter export restrictions on Nvidia AI chips", "bloomberg.com"),
           ("NVIDIA Announces Financial Results for Third Quarter Fiscal 2027", "nvidianews.nvidia.com"),
           ("Analysts raise Nvidia price targets on record data center demand", "cnbc.com"),
           ("SEC: 8-K - Current report", "sec.gov")]
    return [{"title": a, "url": "https://" + d, "domain": d, "published": t - dt.timedelta(hours=i * 3)}
            for i, (a, d) in enumerate(raw)]


# --------------------------------- main -----------------------------------
def main():
    if os.environ.get("MODE") == "test":
        notify("ถ้าเห็นข้อความนี้ แปลว่าการแจ้งเตือนบนมือถือใช้งานได้แล้ว ✅", title="ทดสอบแจ้งเตือน NVDA",
               priority=4, tags="white_check_mark")
        print("test notification sent")
        return
    offline = os.environ.get("OFFLINE_SAMPLE") == "1"
    errors = []
    if offline:
        rows, intraday = synthetic_daily(), None
        global fetch_google_news, fetch_nvidia_newsroom, fetch_sec_filings
        sn = synthetic_news()
        fetch_google_news = lambda days: [x for x in sn if x["domain"] not in ("nvidianews.nvidia.com", "sec.gov")]  # noqa: E731
        fetch_nvidia_newsroom = lambda: [x for x in sn if x["domain"] == "nvidianews.nvidia.com"]  # noqa: E731
        fetch_sec_filings = lambda: [x for x in sn if x["domain"] == "sec.gov"]  # noqa: E731
    else:
        rows, intraday = fetch_daily(), fetch_intraday()
    state = json.loads(STATE.read_text()) if STATE.exists() else {"sent": []}
    if offline:
        sentiment = {"stocktwits": {"messages": 30, "bull": 14, "bear": 9, "bull_pct": 60.9, "watchers": 712345, "sample": [
            {"tag": "Bearish", "body": "Export curbs headline again, expecting a gap down tomorrow", "user": "sample1", "at": now_utc().isoformat()},
            {"tag": "Bullish", "body": "Data center demand is not slowing, buying the dip", "user": "sample2", "at": now_utc().isoformat()}]},
            "vix": {"value": 21.4, "prev_close": 18.9, "week_ago": 17.2, "month_ago": 16.1},
            "fear_greed": {"score": 38.0, "rating": "fear", "previous_close": 44.0, "previous_1_week": 52.0, "previous_1_month": 61.0},
            "analysts": {"period": "2026-11-01", "strongBuy": 24, "buy": 38, "hold": 7, "sell": 1, "strongSell": 0,
                         "history": [{"period": "2026-11-01", "strongBuy": 24, "buy": 38, "hold": 7, "sell": 1, "strongSell": 0},
                                     {"period": "2026-10-01", "strongBuy": 23, "buy": 37, "hold": 8, "sell": 1, "strongSell": 0},
                                     {"period": "2026-09-01", "strongBuy": 22, "buy": 36, "hold": 9, "sell": 2, "strongSell": 0},
                                     {"period": "2026-08-01", "strongBuy": 22, "buy": 35, "hold": 10, "sell": 2, "strongSell": 0}],
                         "next_earnings": {"date": "2026-11-19", "hour": "amc", "eps_est": 1.42, "rev_est": 6.3e10}},
            "history": [{"t": (now_utc() - dt.timedelta(hours=40 - i)).isoformat(timespec="minutes"),
                         "bull_pct": round(70 - i * 0.35 + random.uniform(-4, 4), 1)} for i in range(40)]}
        serr = []
    else:
        sentiment, serr = collect_sentiment(state)
    errors += serr
    tech = compute_technicals(rows)
    anomalies = detect_anomalies(rows, tech, intraday)
    news, nerr = collect_news()
    errors += nerr
    verdict = score(tech, anomalies, news)

    data = {
        "ticker": TICKER, "generated_at": now_utc().isoformat(), "sample": offline,
        "intraday": intraday and {k: intraday[k] for k in ("price", "prev_close", "time")},
        "tech": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in tech.items()},
        "verdict": verdict, "anomalies": anomalies, "sentiment": sentiment, "outlook": build_outlook(news, tech), "news": news, "errors": errors,
        "candles": [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()} for r in rows[-260:]],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    print(f"wrote {OUT}: level={verdict['level']} score={verdict['score']} news={len(news)} anomalies={len(anomalies)}")

    # alerts: send each anomaly once; plus a daily summary when MODE=daily
    new = [a for a in anomalies if a["key"] not in state["sent"]]
    page = os.environ.get("PAGE_URL", "")
    if new and not offline:
        sev = max(a["sev"] for a in new)
        notify("\n".join("• " + a["text"] for a in new) +
               f"\nระดับ: {verdict['level']} ({verdict['lean']})\n{page}\n(ไม่ใช่คำแนะนำการลงทุน)",
               title="⚠️ NVDA กราฟผิดปกติ", priority=5 if sev >= 3 else 4, tags="warning")
    if os.environ.get("MODE") == "daily" and not offline:
        top = "\n".join(f"• [{n['source']}] {n['summary_th'] or n['title']}" for n in news[:6])
        notify(f"ราคา ${tech['close']:.2f} ({tech['chg_pct']:+.2f}%)\n"
               f"ระดับ: {verdict['level']} → {verdict['lean']}\n{top}\n{page}\n(ไม่ใช่คำแนะนำการลงทุน)",
               title="📰 สรุป NVDA ประจำวัน", tags="newspaper")
    state["sent"] = (state["sent"] + [a["key"] for a in new])[-300:]
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
