# -*- coding: utf-8 -*-
"""Fetch 515450 accumulated NAV and compute annual-MA metrics → data.json."""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))
MA_WINDOW = 250
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def fetch(url: str, headers: dict | None = None, retries: int = 4) -> str:
    h = {"User-Agent": UA, "Accept": "*/*", "Referer": "https://fund.eastmoney.com/"}
    if headers:
        h.update(headers)
    last: Exception | None = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
            for enc in ("utf-8", "gbk", "gb18030"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", errors="ignore")
        except Exception as e:  # noqa: BLE001 - network retry
            last = e
            time.sleep(1.2 * (i + 1))
    raise RuntimeError(f"fetch failed: {url}") from last


def load_nav_pair() -> tuple[list[tuple[str, float, float]], list[dict]]:
    """Return ([(date, unit_nav, acc_nav), ...], dividend_events)."""
    url = "https://fund.eastmoney.com/pingzhongdata/515450.js"
    txt = fetch(url)

    m_ac = re.search(r"Data_ACWorthTrend\s*=\s*(\[.*?\]);", txt, re.S)
    m_net = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", txt, re.S)
    if not m_ac or not m_net:
        raise RuntimeError("NAV series not found in pingzhongdata")

    ac_rows = json.loads(m_ac.group(1))
    net_rows = json.loads(m_net.group(1))

    ac_map: dict[str, float] = {}
    for ts, val in ac_rows:
        d = datetime.fromtimestamp(ts / 1000, tz=CST).strftime("%Y-%m-%d")
        ac_map[d] = float(val)

    net_map: dict[str, float] = {}
    divs: list[dict] = []
    for row in net_rows:
        d = datetime.fromtimestamp(row["x"] / 1000, tz=CST).strftime("%Y-%m-%d")
        net_map[d] = float(row["y"])
        um = (row.get("unitMoney") or "").strip()
        if um:
            # "分红：每10份派现金元" / "分红：每份派现金0.0200元" etc.
            amt = None
            m = re.search(r"派现金([0-9.]+)元", um)
            if m:
                amt = float(m.group(1))
                if "每10份" in um:
                    amt = amt / 10.0
            divs.append({"date": d, "text": um, "per_share": amt})

    dates = sorted(set(ac_map) & set(net_map))
    series = [(d, net_map[d], ac_map[d]) for d in dates]
    return series, divs


def load_yield10(start_date: str = "2019-12-01") -> dict[str, float]:
    """China treasury yield curve Y10 (percent)."""
    out: dict[str, float] = {}
    page = 1
    while page <= 20:
        url = (
            "https://datacenter-web.eastmoney.com/api/data/v1/get?"
            "reportName=RPT_YIELD_CURVE&columns=TRADE_DATE,Y10"
            "&sortColumns=TRADE_DATE&sortTypes=-1"
            f"&pageSize=500&pageNumber={page}"
        )
        try:
            obj = json.loads(fetch(url, headers={"Referer": "https://data.eastmoney.com/"}))
        except Exception:  # noqa: BLE001
            break
        rows = (obj.get("result") or {}).get("data") or []
        if not rows:
            break
        stop = False
        for r in rows:
            d = (r.get("TRADE_DATE") or "")[:10]
            y10 = r.get("Y10")
            if d and y10 is not None:
                out[d] = float(y10)
            if d and d < start_date:
                stop = True
        if stop:
            break
        page += 1
        time.sleep(0.15)
    return out


def load_holdings() -> list[dict]:
    """Full stock holdings from fund mid-year/year-end reports (weight %, mv 万元, shares 万股)."""
    quarters: dict[str, dict] = {}
    for year in range(2020, datetime.now(CST).year + 1):
        url = (
            "https://fundf10.eastmoney.com/FundArchivesDatas.aspx?"
            f"type=jjcc&code=515450&topline=50&year={year}&month=12"
        )
        try:
            raw = fetch(url, headers={"Referer": "https://fundf10.eastmoney.com/ccmx_515450.html"})
        except Exception as e:  # noqa: BLE001
            print(f"warn: holdings {year}: {e}")
            continue
        m = re.search(r'content:"(.*)",arryear:', raw, re.S)
        html = m.group(1).replace('\\"', '"').replace("\\/", "/") if m else raw
        for part in re.split(r"<h4 class='t'>", html)[1:]:
            mq = re.search(r"(\d{4})\s*年\s*(\d)\s*季度", part)
            md = re.search(r"截止至：<font class='px12'>(\d{4}-\d{2}-\d{2})</font>", part)
            if not mq or not md:
                continue
            items: dict[str, dict] = {}
            for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", part, re.S):
                tds = [
                    re.sub(r"<[^>]+>", "", t).replace("&nbsp;", " ").replace("\xa0", " ").strip()
                    for t in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
                ]
                if len(tds) < 4:
                    continue
                code = code_i = None
                for i, t in enumerate(tds):
                    if re.fullmatch(r"\d{6}", t):
                        code, code_i = t, i
                        break
                if not code or code_i is None:
                    continue
                name = tds[code_i + 1] if code_i + 1 < len(tds) else ""
                nums = []
                for t in tds[code_i + 2 :]:
                    tt = t.replace("%", "").replace(",", "").strip()
                    if re.fullmatch(r"-?\d+(\.\d+)?", tt):
                        nums.append(float(tt))
                if len(nums) < 3:
                    continue
                weight, shares, mv = nums[-3], nums[-2], nums[-1]
                if shares <= 0 or mv <= 0:
                    continue
                prev = items.get(code)
                if prev is None or mv > prev["mv"]:
                    items[code] = {
                        "code": code,
                        "name": name,
                        "weight": weight,
                        "shares": shares,
                        "mv": mv,
                    }
            if not items:
                continue
            lst = list(items.values())
            wsum = sum(x["weight"] for x in lst)
            key = md.group(1)
            rec = {"end": key, "quarter": f"{mq.group(1)}Q{mq.group(2)}", "wsum": wsum, "items": lst}
            # keep the parse with more names for the same quarter-end
            if key not in quarters or len(lst) > len(quarters[key]["items"]):
                quarters[key] = rec
        time.sleep(0.12)

    # complete portfolios only (mid-year / year-end reports cover all holdings)
    complete = [q for q in quarters.values() if q["wsum"] >= 90 and len(q["items"]) >= 40]
    complete.sort(key=lambda q: q["end"])
    if not complete:
        raise RuntimeError("no complete holdings snapshots")
    print(f"holdings snapshots: {[(q['end'], len(q['items']), round(q['wsum'], 1)) for q in complete]}")
    return complete


def load_dividend_events(codes: list[str]) -> dict[str, list[dict]]:
    """Eastmoney RPT_SHAREBONUS_DET cash dividends: {code: [{report_date, ex_date, dps}]}.

    dps is pre-tax cash per share (PRETAX_BONUS_RMB is per 10 shares). Proposal-only
    rows (方案进度 以「预」开头) are excluded.
    """
    out: dict[str, list[dict]] = {}
    got = 0
    for code in codes:
        url = (
            "https://datacenter-web.eastmoney.com/api/data/v1/get?"
            "reportName=RPT_SHAREBONUS_DET"
            "&columns=PRETAX_BONUS_RMB,EX_DIVIDEND_DATE,REPORT_DATE,ASSIGN_PROGRESS"
            f"&filter=(SECURITY_CODE%3D%22{code}%22)"
            "&sortColumns=EX_DIVIDEND_DATE&sortTypes=-1&pageNumber=1&pageSize=50"
            "&source=WEB&client=WEB"
        )
        try:
            obj = json.loads(fetch(url, headers={"Referer": "https://data.eastmoney.com/"}))
        except Exception as e:  # noqa: BLE001
            print(f"warn: dividends {code}: {e}")
            out[code] = []
            continue
        rows = (obj.get("result") or {}).get("data") or []
        evs = []
        for e in rows:
            rd = (e.get("REPORT_DATE") or "")[:10]
            ex = (e.get("EX_DIVIDEND_DATE") or "")[:10]
            cash = e.get("PRETAX_BONUS_RMB")
            if not rd or not cash or (e.get("ASSIGN_PROGRESS") or "").startswith("预"):
                continue
            evs.append({"report_date": rd, "ex_date": ex, "dps": float(cash) / 10.0})
        out[code] = evs
        if evs:
            got += 1
        time.sleep(0.06)
    if not got:
        raise RuntimeError("no dividend events from RPT_SHAREBONUS_DET")
    print(f"dividend events loaded for {got}/{len(codes)} stocks")
    return out


def load_stock_prices(code: str, start: str) -> dict[str, float]:
    """Daily close from Sina. {date: close}."""
    market = "sh" if code.startswith("6") else "sz"
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={market}{code}&scale=240&ma=no&datalen=3000"
    )
    txt = fetch(url)
    if not txt or txt.strip() in ("null", "[]", ""):
        return {}
    try:
        rows = json.loads(txt)
    except json.JSONDecodeError:
        return {}
    out: dict[str, float] = {}
    for r in rows or []:
        d = r.get("day")
        c = r.get("close")
        if d and c and d >= start:
            try:
                out[d] = float(c)
            except (TypeError, ValueError):
                continue
    return out


def load_share_counts(codes: list[str]) -> dict[str, dict[str, float]]:
    """Issuer share counts (总股本/流通股本, unit=share) via Eastmoney quote batch."""
    out: dict[str, dict[str, float]] = {}
    secids = [f"{'1' if c.startswith('6') else '0'}.{c}" for c in codes]
    for i in range(0, len(secids), 30):
        batch = secids[i : i + 30]
        url = (
            "https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&secids="
            + ",".join(batch)
            + "&fields=f12,f38,f39"
        )
        try:
            obj = json.loads(fetch(url, headers={"Referer": "https://quote.eastmoney.com/"}))
        except Exception as e:  # noqa: BLE001
            print(f"warn: shares batch {i}: {e}")
            continue
        for it in (obj.get("data") or {}).get("diff") or []:
            code = it.get("f12")
            if not code:
                continue
            rec: dict[str, float] = {}
            ts, fs = it.get("f38"), it.get("f39")
            if isinstance(ts, (int, float)) and ts > 0:
                rec["total_sh"] = float(ts)
            if isinstance(fs, (int, float)) and fs > 0:
                rec["float_sh"] = float(fs)
            if rec:
                out[code] = rec
        time.sleep(0.12)
    print(f"share counts loaded for {len(out)}/{len(codes)} stocks")
    return out


def price_on(price_map: dict[str, float], date: str) -> float | None:
    """Last close on or before date."""
    if not price_map:
        return None
    if date in price_map:
        return price_map[date]
    lo, hi = 0, None
    keys = list(price_map.keys())  # sorted if built from kline ascending
    # binary search on sorted keys
    if keys != sorted(keys):
        keys = sorted(keys)
        price_map = {k: price_map[k] for k in keys}
    left, right = 0, len(keys) - 1
    ans = None
    while left <= right:
        mid = (left + right) // 2
        if keys[mid] <= date:
            ans = price_map[keys[mid]]
            left = mid + 1
        else:
            right = mid - 1
    return ans


def ttm_dps(evs: list[dict], asof: str, start: str, date_key: str = "ex_date") -> float:
    """TTM 税前每股分红。除息日落入 (start, asof]；若窗口内为 0（年付一次且间隔>365天
    造成的空窗，例如中国神华 2020-06-15→2021-07-12），回退为最近一个已除息日历年的分红，
    避免分子被清零导致股息率假性塌陷。"""
    ttm = 0.0
    for e in evs:
        day = e.get(date_key) or ""
        if day and start < day <= asof:
            ttm += e["dps"]
    if ttm > 0:
        return ttm
    paid = [e for e in evs if e.get(date_key) and e[date_key] <= asof]
    if not paid:
        return 0.0
    paid.sort(key=lambda e: e[date_key])
    year = paid[-1][date_key][:4]
    return sum(e["dps"] for e in paid if e[date_key][:4] == year)


def build_dividend_yield(
    dates: list[str],
    holdings: list[dict],
    events: dict[str, list[dict]],
    shares: dict[str, dict[str, float]] | None = None,
    date_key: str = "ex_date",
    share_key: str = "total_sh",
) -> list[float | None]:
    """官方口径：股息率 = Σ(近12月现金股利) / Σ(指数总市值)

        = Σ(w_i × TTM每股税前分红_i) / Σ(w_i × 股价_i)

    不是个股股息率的加权平均 Σ(w×dy)/Σw。
    权重 w 取基金中报/年报完整持仓（成分/权重代理）。
    TTM 现金分红按除息日 EX_DIVIDEND_DATE 落入 (t-365, t] 汇总，税前、剔除预案；
    窗口空窗用 ttm_dps 回退，避免假性塌陷。
    历史锚点对照：2024-09-20 官方 5.16%；2026-06-30 官方 5.08%；2026-10-28 官方 4.59%。
    总股本直乘口径在 2026-06 会偏低约 0.3pp，故不用。
    """
    codes: set[str] = set()
    for q in holdings:
        for it in q["items"]:
            codes.add(it["code"])
    codes = sorted(codes)
    print(f"loading prices for {len(codes)} stocks ...")

    cache_path = ROOT / "scripts" / "_price_cache.json"
    prices: dict[str, dict[str, float]] = {}
    if cache_path.exists():
        try:
            prices = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            prices = {}

    missing = [c for c in codes if c not in prices]
    if missing:
        print(f"  fetching {len(missing)} ({len(codes) - len(missing)} cached) ...")

        def _one(code: str) -> tuple[str, dict[str, float]]:
            try:
                return code, load_stock_prices(code, start=dates[0])
            except Exception as e:  # noqa: BLE001
                print(f"warn: prices {code}: {e}")
                return code, {}

        done = 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(_one, c) for c in missing]
            for fut in as_completed(futs):
                code, series = fut.result()
                prices[code] = series
                done += 1
                if done % 30 == 0:
                    print(f"  ... {done}/{len(missing)}")
        try:
            cache_path.write_text(json.dumps(prices, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    snap_ends = [q["end"] for q in holdings]
    dy: list[float | None] = [None] * len(dates)
    for i, d in enumerate(dates):
        si = 0
        for j, end in enumerate(snap_ends):
            if end <= d:
                si = j
            else:
                break
        snap = holdings[si]
        start = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=365)).strftime("%Y-%m-%d")
        num = den = 0.0
        for it in snap["items"]:
            code = it["code"]
            w = it.get("weight") or 0.0
            if w <= 0:
                continue
            px = price_on(prices.get(code, {}), d)
            if not px:
                continue
            dps = ttm_dps(events.get(code, []), d, start, date_key=date_key)
            # Σ(现金股利) = Σ(w × DPS)；Σ(市值) = Σ(w × 价)
            num += w * dps
            den += w * px
        if den > 0:
            dy[i] = num / den * 100.0
    return dy


def ma_series(values: list[float], window: int = MA_WINDOW) -> list[float | None]:
    mas: list[float | None] = [None] * len(values)
    acc = 0.0
    for i, v in enumerate(values):
        acc += v
        if i >= window:
            acc -= values[i - window]
        if i >= window - 1:
            mas[i] = acc / window
    return mas


def build_periods(dates, values, mas):
    periods = []
    state = None
    start_i = None
    for i in range(MA_WINDOW - 1, len(values)):
        side = "above" if values[i] > mas[i] else "below"
        if state is None:
            state, start_i = side, i
        elif side != state:
            periods.append(_period(dates, values, mas, state, start_i, i - 1, False))
            state, start_i = side, i
    if state is not None:
        periods.append(_period(dates, values, mas, state, start_i, len(values) - 1, True))
    return periods


def _period(dates, values, mas, side, start_i, end_i, ongoing):
    seg_b = []
    for i in range(start_i, end_i + 1):
        if mas[i]:
            seg_b.append(values[i] / mas[i] - 1.0)
    return {
        "side": side,
        "start": dates[start_i],
        "end": dates[end_i],
        "days": end_i - start_i + 1,
        "calendar_days": (
            datetime.strptime(dates[end_i], "%Y-%m-%d")
            - datetime.strptime(dates[start_i], "%Y-%m-%d")
        ).days,
        "min_bias": round(min(seg_b) * 100, 2) if seg_b else 0.0,
        "max_bias": round(max(seg_b) * 100, 2) if seg_b else 0.0,
        "start_px": round(values[start_i], 4),
        "end_px": round(values[end_i], 4),
        "ongoing": ongoing,
    }


def build_underwater(dates, values):
    peak_v, peak_i = values[0], 0
    eps = []
    cur = None
    for i, v in enumerate(values):
        if v >= peak_v:
            if cur is not None:
                cur["end"] = dates[i]
                cur["recover_px"] = round(v, 4)
                cur["cal_days"] = (
                    datetime.strptime(dates[i], "%Y-%m-%d")
                    - datetime.strptime(cur["peak_date"], "%Y-%m-%d")
                ).days
                cur["trade_days"] = i - cur["peak_i"]
                eps.append(cur)
                cur = None
            peak_v, peak_i = v, i
        else:
            if cur is None:
                cur = {
                    "peak_date": dates[peak_i],
                    "peak_i": peak_i,
                    "peak_px": round(peak_v, 4),
                    "min_px": round(v, 4),
                    "min_date": dates[i],
                    "max_dd": round((v / peak_v - 1.0) * 100, 2),
                }
            else:
                if v < cur["min_px"]:
                    cur["min_px"] = round(v, 4)
                    cur["min_date"] = dates[i]
                cur["max_dd"] = round((cur["min_px"] / cur["peak_px"] - 1.0) * 100, 2)
    if cur is not None:
        cur["end"] = None
        cur["ongoing"] = True
        cur["cal_days"] = (
            datetime.strptime(dates[-1], "%Y-%m-%d")
            - datetime.strptime(cur["peak_date"], "%Y-%m-%d")
        ).days
        cur["trade_days"] = (len(values) - 1) - cur["peak_i"]
        eps.append(cur)
    return eps


def main() -> int:
    pair, div_events = load_nav_pair()
    if len(pair) < MA_WINDOW + 5:
        raise RuntimeError(f"too few points: {len(pair)}")

    dates = [d for d, _, _ in pair]
    values = [ac for _, _, ac in pair]  # total-return proxy
    mas = ma_series(values)

    # ---- dividend yield & treasury spread ----
    # 官方口径：Σ(近12月现金股利) / Σ(总市值) = Σ(w×DPS)/Σ(w×价)
    n = len(values)
    try:
        holdings = load_holdings()
        codes = sorted({it["code"] for q in holdings for it in q["items"]})
        events = load_dividend_events(codes)
        dy = build_dividend_yield(dates, holdings, events)
    except Exception as e:  # noqa: BLE001
        print(f"warn: dividend yield failed: {e}")
        dy = [None] * n

    yield10 = load_yield10(start_date="2019-12-01")
    if not yield10:
        print("warn: yield10 empty, spread will be null")

    # forward-fill yield10 onto series dates
    sorted_y10_keys = sorted(yield10)

    def yield10_on(date: str) -> float | None:
        if date in yield10:
            return yield10[date]
        # last known value on or before date
        lo, hi = 0, len(sorted_y10_keys) - 1
        ans = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if sorted_y10_keys[mid] <= date:
                ans = yield10[sorted_y10_keys[mid]]
                lo = mid + 1
            else:
                hi = mid - 1
        return ans

    y10_series: list[float | None] = [None] * n
    spread: list[float | None] = [None] * n
    for i, d in enumerate(dates):
        y = yield10_on(d)
        y10_series[i] = y
        if y is not None and dy[i] is not None:
            spread[i] = round(dy[i] - y, 3)

    # current indicators
    last_i = n - 1
    while last_i > 0 and dy[last_i] is None:
        last_i -= 1

    bias = [None if mas[i] is None else values[i] / mas[i] - 1.0 for i in range(len(values))]
    periods = build_periods(dates, values, mas)
    underwater = build_underwater(dates, values)

    valid = [(dates[i], values[i], mas[i], bias[i]) for i in range(len(values)) if bias[i] is not None]
    if not valid:
        raise RuntimeError("no valid bias points")

    cur = valid[-1]
    max_pos = max(valid, key=lambda x: x[3])
    max_neg = min(valid, key=lambda x: x[3])
    max_above = max((p for p in periods if p["side"] == "above"), key=lambda p: p["days"], default=None)
    max_below = max((p for p in periods if p["side"] == "below"), key=lambda p: p["days"], default=None)
    max_uw = max(underwater, key=lambda x: x.get("cal_days") or 0)

    above_days = sum(p["days"] for p in periods if p["side"] == "above")
    below_days = sum(p["days"] for p in periods if p["side"] == "below")

    major = [
        p
        for p in periods
        if p["days"] >= 5
        or abs(p["min_bias"]) >= 2
        or abs(p["max_bias"]) >= 2
        or p["ongoing"]
    ]
    top_uw = [u for u in sorted(underwater, key=lambda x: x.get("cal_days") or 0, reverse=True) if (u.get("cal_days") or 0) >= 60][:12]

    def window_around(center: str, left: int = 25, right: int = 25) -> tuple[str, str]:
        try:
            idx = dates.index(center)
        except ValueError:
            return center, center
        return dates[max(0, idx - left)], dates[min(len(dates) - 1, idx + right)]

    max_dd_ep = min(underwater, key=lambda x: x["max_dd"]) if underwater else max_uw
    pos_lo, pos_hi = window_around(max_pos[0])
    neg_lo, neg_hi = window_around(max_neg[0])

    # current slice: ongoing period if any, else last 60 points
    cur_period = next((p for p in periods if p.get("ongoing")), None)
    if cur_period:
        cur_start, cur_end = cur_period["start"], cur_period["end"]
    else:
        cur_start, cur_end = dates[max(0, len(dates) - 60)], dates[-1]

    highlights = {
        "current": {
            "key": "current",
            "label": "当前 vs 年线",
            "start": cur_start,
            "end": cur_end,
            "focus": cur[0],
            "mode": "range",
        },
        "max_above": {
            "key": "max_above",
            "label": "最长连续年线上",
            "start": max_above["start"] if max_above else dates[-1],
            "end": max_above["end"] if max_above else dates[-1],
            "mode": "range",
            "side": "above",
        },
        "max_below": {
            "key": "max_below",
            "label": "最长连续年线下",
            "start": max_below["start"] if max_below else dates[-1],
            "end": max_below["end"] if max_below else dates[-1],
            "mode": "range",
            "side": "below",
        },
        "max_underwater": {
            "key": "max_underwater",
            "label": "最大套牢时间",
            "start": max_uw["peak_date"],
            "end": max_uw.get("end") or dates[-1],
            "focus": max_uw["min_date"],
            "mode": "range",
            "side": "below",
        },
        "max_pos_bias": {
            "key": "max_pos_bias",
            "label": "最大正偏离",
            "start": pos_lo,
            "end": pos_hi,
            "focus": max_pos[0],
            "mode": "range",
            "side": "above",
        },
        "max_neg_bias": {
            "key": "max_neg_bias",
            "label": "最大负偏离",
            "start": neg_lo,
            "end": neg_hi,
            "focus": max_neg[0],
            "mode": "range",
            "side": "below",
        },
        "above_share": {
            "key": "above_share",
            "label": "年线上占比",
            "mode": "multi",
            "side": "above",
            "ranges": [
                {"start": p["start"], "end": p["end"]}
                for p in periods
                if p["side"] == "above"
            ],
        },
        "max_dd": {
            "key": "max_dd",
            "label": "最深回撤",
            "start": max_dd_ep["peak_date"],
            "end": max_dd_ep.get("end") or dates[-1],
            "focus": max_dd_ep["min_date"],
            "mode": "range",
            "side": "below",
        },
    }

    # ---- hold returns: any-time entry vs below-MA entry ----
    def pct(x: float) -> float:
        return round(x * 100, 2)

    def summarize(returns: list[float]) -> dict:
        if not returns:
            return {"n": 0, "avg": 0.0, "median": 0.0, "win_rate": 0.0, "min": 0.0, "max": 0.0}
        rs = sorted(returns)
        n = len(rs)
        mid = n // 2
        median = rs[mid] if n % 2 else (rs[mid - 1] + rs[mid]) / 2
        avg = sum(rs) / n
        win = sum(1 for x in rs if x > 0) / n
        return {
            "n": n,
            "avg": pct(avg),
            "median": pct(median),
            "win_rate": pct(win),
            "min": pct(rs[0]),
            "max": pct(rs[-1]),
        }

    def hold_block(hold_days: int) -> dict:
        any_rets: list[float] = []
        below_rets: list[float] = []
        above_rets: list[float] = []
        for i in range(len(values) - hold_days):
            if mas[i] is None:
                continue
            r = values[i + hold_days] / values[i] - 1.0
            any_rets.append(r)
            if values[i] < mas[i]:
                below_rets.append(r)
            else:
                above_rets.append(r)
        s_any = summarize(any_rets)
        s_below = summarize(below_rets)
        s_above = summarize(above_rets)
        return {
            "hold_days": hold_days,
            "label": f"持有{hold_days // 250}年" if hold_days >= 250 and hold_days % 250 == 0 else f"持有{hold_days}日",
            "any": s_any,
            "below_ma": s_below,
            "above_ma": s_above,
            "pair": {
                "avg_diff": round(s_below["avg"] - s_any["avg"], 2),
                "median_diff": round(s_below["median"] - s_any["median"], 2),
            },
        }

    hold_stats = {
        "note": "持有期按 250/750 个交易日近似 1/3 年；收益基于 515450 累计净值（全收益代理）",
        "windows": [hold_block(250), hold_block(750)],
        # keep 1y top-level keys for backward compatibility
        **hold_block(250),
    }

    def point(i: int) -> dict:
        return {
            "d": dates[i],
            "p": round(values[i], 4),
            "ma": None if mas[i] is None else round(mas[i], 4),
            "b": None if bias[i] is None else round(bias[i] * 100, 3),
            "dy": None if dy[i] is None else round(dy[i], 3),
            "y10": None if y10_series[i] is None else round(y10_series[i], 3),
            "sp": None if spread[i] is None else round(spread[i], 3),
        }

    ind = {
        "dividend_yield": None if dy[last_i] is None else round(dy[last_i], 3),
        "yield10": None if y10_series[last_i] is None else round(y10_series[last_i], 3),
        "spread": None if spread[last_i] is None else round(spread[last_i], 3),
        "date": dates[last_i],
        "note": "股息率=Σ成分股近12月现金股利和÷Σ指数总市值=Σ(权重×TTM税前每股分红)÷Σ(权重×股价)；TTM按除息日落入(t-365,t]、税前、剔除预案；若窗口空窗（年付间隔>365天）回退最近已除息日历年分红；权重取基金中报/年报完整持仓；不是个股股息率加权平均；利差=股息率-10Y国债收益率",
    }

    payload = {
        "meta": {
            "title": "标普红利低波50 全收益 · 年线结构",
            "index_name": "标普中国A股大盘红利低波50指数",
            "series_note": "以南方红利低波50ETF（515450）累计净值作为全收益代理（含分红，含费用与跟踪误差）",
            "proxy_code": "515450",
            "ma": MA_WINDOW,
            "start": dates[0],
            "end": dates[-1],
            "n": len(dates),
            "updated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
            "source": "天天基金 pingzhongdata（515450 累计净值/单位净值）+ 东方财富（持仓/分红/国债收益率曲线）+ 新浪财经日线",
        },
        "series": [point(i) for i in range(len(dates))],
        "periods": major,
        "all_periods_count": len(periods),
        "underwater": top_uw,
        "underwater_all_count": len(underwater),
        "highlights": highlights,
        "hold_stats": hold_stats,
        "indicators": ind,
        "dividends": [
            {"date": d["date"], "per_share": d["per_share"], "text": d["text"]}
            for d in div_events[-20:]
        ],
        "stats": {
            "current": {
                "date": cur[0],
                "p": round(cur[1], 4),
                "ma": round(cur[2], 4),
                "b": round(cur[3] * 100, 3),
                "bias": round(cur[3] * 100, 3),
                "side": "above" if cur[3] > 0 else "below",
            },
            "indicators": ind,
            "max_pos_bias": {"d": max_pos[0], "p": round(max_pos[1], 4), "ma": round(max_pos[2], 4), "b": round(max_pos[3] * 100, 3)},
            "max_neg_bias": {"d": max_neg[0], "p": round(max_neg[1], 4), "ma": round(max_neg[2], 4), "b": round(max_neg[3] * 100, 3)},
            "max_above": {
                "start": max_above["start"] if max_above else None,
                "end": max_above["end"] if max_above else None,
                "days": max_above["days"] if max_above else 0,
                "calendar_days": max_above["calendar_days"] if max_above else 0,
            },
            "max_below": {
                "start": max_below["start"] if max_below else None,
                "end": max_below["end"] if max_below else None,
                "days": max_below["days"] if max_below else 0,
                "calendar_days": max_below["calendar_days"] if max_below else 0,
            },
            "max_underwater": {
                "peak_date": max_uw["peak_date"],
                "end": max_uw.get("end"),
                "min_date": max_uw["min_date"],
                "cal_days": max_uw.get("cal_days"),
                "trade_days": max_uw.get("trade_days"),
                "max_dd": max_uw["max_dd"],
                "peak_px": max_uw["peak_px"],
                "min_px": max_uw["min_px"],
            },
            "totals": {
                "above_days": above_days,
                "below_days": below_days,
                "above_share": round(above_days / (above_days + below_days) * 100, 1) if above_days + below_days else 0,
            },
        },
    }

    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {OUT} points={len(dates)} end={dates[-1]} current_bias={payload['stats']['current']['b']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
