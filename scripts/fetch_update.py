# -*- coding: utf-8 -*-
"""Fetch 515450 accumulated NAV and compute annual-MA metrics → data.json."""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
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


def load_ac_series() -> list[tuple[str, float]]:
    """Return [(date, acc_nav), ...] for 515450 accumulated unit NAV."""
    url = "https://fund.eastmoney.com/pingzhongdata/515450.js"
    txt = fetch(url)
    m = re.search(r"Data_ACWorthTrend\s*=\s*(\[.*?\]);", txt, re.S)
    if not m:
        raise RuntimeError("Data_ACWorthTrend not found")
    rows = json.loads(m.group(1))
    out: list[tuple[str, float]] = []
    seen: dict[str, float] = {}
    for ts, val in rows:
        d = datetime.fromtimestamp(ts / 1000, tz=CST).strftime("%Y-%m-%d")
        seen[d] = float(val)
    for d in sorted(seen):
        out.append((d, seen[d]))
    if len(out) < MA_WINDOW + 5:
        raise RuntimeError(f"too few points: {len(out)}")
    return out


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
    series = load_ac_series()
    dates = [d for d, _ in series]
    values = [v for _, v in series]
    mas = ma_series(values)

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

    def point(d, p, m, b):
        return {
            "d": d,
            "p": round(p, 4),
            "ma": None if m is None else round(m, 4),
            "b": None if b is None else round(b * 100, 3),
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
            "source": "天天基金 pingzhongdata（515450 累计净值）",
        },
        "series": [point(dates[i], values[i], mas[i], bias[i]) for i in range(len(values))],
        "periods": major,
        "all_periods_count": len(periods),
        "underwater": top_uw,
        "underwater_all_count": len(underwater),
        "stats": {
            "current": {
                "date": cur[0],
                "p": round(cur[1], 4),
                "ma": round(cur[2], 4),
                "b": round(cur[3] * 100, 3),
                "bias": round(cur[3] * 100, 3),
                "side": "above" if cur[3] > 0 else "below",
            },
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
