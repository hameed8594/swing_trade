#!/usr/bin/env python3
"""
swing_scanner.py — Minervini SEPA/VCP swing trading scanner.

Reads tickers from snapshot_results.csv, fetches Yahoo Finance data and
applies Mark Minervini's rules to each stock:
  1. Trend Template — all 8 criteria (incl. RS rating ≥ 70) = Stage 2
  2. SEPA fundamentals — EPS and sales growth
  3. VCP — a tightening base with a defined pivot
  4. Entry — buy range up to 5% above the pivot, on breakout volume
  5. General market — no new buys while the S&P 500 is in a correction
Each stock gets a verdict (Buy / Setup / Watch / Extended / Avoid) and the
results are injected into docs/swing.html.

Usage:
    python3 swing_scanner.py
"""

import json
import re
import sys
from pathlib import Path
from datetime import datetime

import pandas as pd
import yfinance as yf

try:
    from zoneinfo import ZoneInfo
    _TZ_NY = ZoneInfo("America/New_York")
except ImportError:
    _TZ_NY = None

HERE           = Path(__file__).parent
SNAPSHOT_CSV   = HERE / "snapshot_results.csv"
STOCK_LIST     = HERE / "../Stock List.xlsx"
DASHBOARD_HTML = HERE / "../docs/swing.html"


def load_snapshot_data():
    """Load valuation data from snapshot CSV keyed by ticker."""
    if not SNAPSHOT_CSV.exists():
        return {}
    df = pd.read_csv(SNAPSHOT_CSV)
    out = {}
    for _, row in df.iterrows():
        ticker = row.get("tickerSymbol")
        if not ticker:
            continue
        def _f(k):
            v = row.get(k)
            try:
                return float(v) if (v is not None and not pd.isna(v)) else None
            except (TypeError, ValueError):
                return None
        out[ticker] = {
            "pe_ratio":           _f("trailingPE"),
            "price_to_sales":     _f("priceToSales"),
            "price_to_cash_flow": _f("priceToCashFlow"),
            "price_to_fcf":       _f("priceToFreeCashFlow"),
            "price_to_book":      _f("priceToBook"),
            "div_yield":          _f("divYield"),
        }
    return out


def adjust_volume_intraday(df):
    """Scale today's partial volume to a full-day estimate if market is currently open."""
    if _TZ_NY is None:
        return df
    last_bar_date = df.index[-1].date()
    today = datetime.now().date()
    if last_bar_date != today:
        return df
    now_et = datetime.now(_TZ_NY)
    if now_et.weekday() >= 5:
        return df
    market_open  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    if now_et < market_open or now_et >= market_close:
        return df
    elapsed  = (now_et - market_open).total_seconds()
    total    = (market_close - market_open).total_seconds()  # 23400 s
    fraction = elapsed / total
    if fraction < 0.02:
        return df
    df = df.copy()
    df.iloc[-1, df.columns.get_loc("Volume")] = int(df["Volume"].iloc[-1] / fraction)
    return df


# ── Minervini rule thresholds ─────────────────────────────────────────────────
# Sources: Mark Minervini, "Trade Like a Stock Market Wizard" (Trend Template,
# SEPA, VCP) and "Think & Trade Like a Champion" (risk management).
RS_MIN              = 70     # Trend Template #8: RS rating ≥ 70 (prefers 80–90+)
EPS_GROWTH_MIN      = 20.0   # SEPA earnings: latest-quarter EPS growth ≥ 20%
SALES_GROWTH_MIN    = 15.0   # SEPA sales: latest-quarter revenue growth ≥ 15%
VCP_LOOKBACK        = 130    # ~6 months of daily bars searched for the base
SWING_WINDOW        = 5      # bars on each side that define a swing high
MIN_CONTRACTION_PCT = 2.0    # dips shallower than this are noise, not contractions
MAX_FIRST_DEPTH     = 40.0   # bases deeper than this are failure-prone
MAX_FINAL_DEPTH     = 10.0   # final contraction must be tight
VOLUME_DRY_RATIO    = 0.80   # final-contraction volume ≤ 80% of 50-day average
PRIOR_UPTREND_MIN   = 30.0   # stock must have run ≥ 30% before the base
MIN_BASE_DAYS       = 15     # base should last at least ~3 weeks
BUY_RANGE_PCT       = 5.0    # buy only up to 5% above the pivot — beyond is chasing
NEAR_PIVOT_PCT      = 5.0    # within 5% below the pivot = on the alert list
BREAKOUT_VOL_PCT    = 40.0   # breakout day volume ≥ 40% above 50-day average
MAX_STOP_PCT        = 8.0    # never risk more than ~8% from entry

VERDICTS = {
    "buy":      {"rank": 5, "label": "Buy"},
    "setup":    {"rank": 4, "label": "Setup"},
    "watch":    {"rank": 3, "label": "Watch"},
    "extended": {"rank": 2, "label": "Extended"},
    "avoid":    {"rank": 1, "label": "Avoid"},
}

TEMPLATE_LABELS = {
    "price_above_150_and_200ma":      "Price above 150- and 200-day MA",
    "ma150_above_ma200":              "150-day MA above 200-day MA",
    "ma200_trending_up":              "200-day MA trending up (1 month+)",
    "ma50_above_ma150_and_ma200":     "50-day MA above 150- and 200-day MA",
    "price_above_ma50":               "Price above 50-day MA",
    "price_30pct_above_52w_low":      "Price ≥ 30% above 52-week low",
    "price_within_25pct_of_52w_high": "Price within 25% of 52-week high",
    "rs_rank_70plus":                 f"RS rating ≥ {RS_MIN}",
}


def check_trend_template(df):
    """
    Minervini's 8-point Trend Template. Criteria 1–7 are computed here;
    #8 (RS rating ≥ 70) needs the whole universe, so main() fills it in.
    52-week high/low use intraday highs/lows.
    """
    close = df["Close"]
    if len(close) < 222:
        return None, {}

    ma50  = close.rolling(50).mean()
    ma150 = close.rolling(150).mean()
    ma200 = close.rolling(200).mean()

    price     = float(close.iloc[-1])
    ma50_now  = float(ma50.iloc[-1])
    ma150_now = float(ma150.iloc[-1])
    ma200_now = float(ma200.iloc[-1])
    ma200_1m  = float(ma200.iloc[-22])
    ma200_5m  = float(ma200.iloc[-106]) if len(close) >= 306 else None

    high_52w = float(df["High"].tail(252).max())
    low_52w  = float(df["Low"].tail(252).min())

    checks = {
        "price_above_150_and_200ma":      price > ma150_now and price > ma200_now,
        "ma150_above_ma200":              ma150_now > ma200_now,
        "ma200_trending_up":              ma200_now > ma200_1m,
        "ma50_above_ma150_and_ma200":     ma50_now > ma150_now and ma50_now > ma200_now,
        "price_above_ma50":               price > ma50_now,
        "price_30pct_above_52w_low":      price >= low_52w * 1.30,
        "price_within_25pct_of_52w_high": price >= high_52w * 0.75,
        "rs_rank_70plus":                 False,   # set in main() once RS ranks exist
    }
    levels = {
        "ma50":     round(ma50_now, 2),
        "ma150":    round(ma150_now, 2),
        "ma200":    round(ma200_now, 2),
        "ma200_up_5m": (ma200_now > ma200_5m) if ma200_5m is not None else None,
        "high_52w": round(high_52w, 2),
        "low_52w":  round(low_52w, 2),
        "pct_from_52w_high": round((price - high_52w) / high_52w * 100, 1),
        "pct_above_52w_low": round((price - low_52w) / low_52w * 100, 1),
    }
    return checks, levels


def find_swing_highs(high, window=SWING_WINDOW):
    """Indices whose high is the maximum of the surrounding ±window bars."""
    out = []
    for i in range(window, len(high) - window):
        seg = high[i - window: i + window + 1]
        if high[i] == seg.max() and (not out or i - out[-1] > window):
            out.append(i)
    return out


def _contractions(high, low, highs, n):
    """Depth (%) and low index of the pullback that follows each swing high."""
    res = []
    for k, hi in enumerate(highs):
        end = highs[k + 1] if k + 1 < len(highs) else n
        lo  = hi + int(low[hi:end].argmin())
        res.append((float((high[hi] - low[lo]) / high[hi] * 100), lo))
    return res


def detect_vcp(df):
    """
    Volatility Contraction Pattern, per Minervini:
      • a prior uptrend of ≥ 30% into the base
      • the base starts at its highest high (left side) and lasts ≥ 3 weeks
      • 2+ pullbacks ("contractions"), each shallower than the one before
      • first pullback not deeper than ~40%, final pullback ≤ 10% (tight)
      • volume dries up in the final contraction
    The pivot is the high of the final contraction.
    """
    out = {"detected": False, "pivot_price": None, "pct_vs_pivot": None,
           "contractions": 0, "contraction_depths": [], "criteria": [], "highs": []}
    if len(df) < VCP_LOOKBACK + 30:
        out["criteria"].append({"key": "data", "label": "Enough price history", "pass": False,
                                "detail": f"{len(df)} bars"})
        return out

    offset = len(df) - VCP_LOOKBACK
    win    = df.tail(VCP_LOOKBACK)
    high   = win["High"].to_numpy(dtype=float)
    low    = win["Low"].to_numpy(dtype=float)
    vol    = win["Volume"].to_numpy(dtype=float)
    price  = float(win["Close"].iloc[-1])
    n      = len(win)

    swing = find_swing_highs(high)
    if not swing:
        out["criteria"].append({"key": "base", "label": "Base with a defined high", "pass": False,
                                "detail": "no swing high in the last 6 months"})
        return out

    base_i = max(swing, key=lambda i: high[i])          # left side of the base
    highs  = [i for i in swing if i >= base_i]

    # Merge noise: an intermediate dip shallower than MIN_CONTRACTION_PCT means
    # the two highs are really one top — keep the higher of the pair.
    while True:
        cons = _contractions(high, low, highs, n)
        idx = next((k for k in range(len(cons) - 1) if cons[k][0] < MIN_CONTRACTION_PCT), None)
        if idx is None:
            break
        drop = idx if high[highs[idx]] < high[highs[idx + 1]] else idx + 1
        if highs[drop] == base_i:
            drop = idx + 1
        highs.pop(drop)

    depths     = [round(c[0], 1) for c in cons]
    pivot_i    = highs[-1]
    pivot      = float(high[pivot_i])
    final_low  = float(low[cons[-1][1]])
    base_days  = n - base_i

    # Prior uptrend: base high vs lowest low in the ~6 months before the base began
    abs_base   = offset + base_i
    pre        = df["Low"].iloc[max(0, abs_base - 126): abs_base]
    prior_up   = (float(high[base_i]) / float(pre.min()) - 1) * 100 if len(pre) else None

    # Volume in the final contraction (excluding today, which may be a breakout)
    # vs. the 50-day average leading into it — volume should shrink as price tightens.
    abs_pivot  = offset + pivot_i
    ref_vol    = float(df["Volume"].iloc[max(0, abs_pivot - 50): abs_pivot].mean())
    fc_vol     = vol[pivot_i + 1: n - 1]
    fc_ratio   = (float(fc_vol.mean()) / ref_vol) if (len(fc_vol) and ref_vol) else None

    contracting = len(depths) >= 2 and all(depths[k + 1] < depths[k] for k in range(len(depths) - 1))
    chain = " → ".join(f"{d}%" for d in depths)

    criteria = [
        {"key": "prior_uptrend", "label": f"Prior uptrend ≥ {PRIOR_UPTREND_MIN:.0f}%",
         "pass": prior_up is not None and prior_up >= PRIOR_UPTREND_MIN,
         "detail": f"+{prior_up:.0f}% into the base" if prior_up is not None else "n/a"},
        {"key": "base_length", "label": f"Base ≥ {MIN_BASE_DAYS} trading days",
         "pass": base_days >= MIN_BASE_DAYS, "detail": f"{base_days} days"},
        {"key": "contractions", "label": "2+ contractions",
         "pass": len(depths) >= 2, "detail": f"{len(depths)} so far: {chain}"},
        {"key": "contracting", "label": "Each pullback shallower than the last",
         "pass": contracting, "detail": chain if len(depths) >= 2 else "needs 2+ contractions"},
        {"key": "first_depth", "label": f"First pullback ≤ {MAX_FIRST_DEPTH:.0f}%",
         "pass": depths[0] <= MAX_FIRST_DEPTH, "detail": f"{depths[0]}%"},
        {"key": "final_tight", "label": f"Final contraction ≤ {MAX_FINAL_DEPTH:.0f}%",
         "pass": depths[-1] <= MAX_FINAL_DEPTH, "detail": f"{depths[-1]}%"},
        {"key": "volume_dry", "label": "Volume dried up in final contraction",
         "pass": fc_ratio is not None and fc_ratio <= VOLUME_DRY_RATIO,
         "detail": f"{fc_ratio * 100:.0f}% of prior 50-day avg" if fc_ratio is not None else "n/a"},
    ]

    dates = win.index
    out.update({
        "detected":           all(c["pass"] for c in criteria),
        "criteria":           criteria,
        "contractions":       len(depths),
        "contraction_depths": depths,
        "final_depth_pct":    depths[-1],
        "pivot_price":        round(pivot, 2),
        "pivot_date":         dates[pivot_i].strftime("%Y-%m-%d"),
        "base_high":          round(float(high[base_i]), 2),
        "above_base_high":    price > float(high[base_i]),
        "pct_vs_pivot":       round((price - pivot) / pivot * 100, 1),
        "final_low":          round(final_low, 2),
        "base_days":          base_days,
        "prior_uptrend_pct":  round(prior_up, 1) if prior_up is not None else None,
        "volume_dry":         criteria[-1]["pass"],
        "final_vol_ratio":    round(fc_ratio, 2) if fc_ratio is not None else None,
        "highs": [{"date": dates[i].strftime("%Y-%m-%d"), "price": round(float(high[i]), 2)}
                  for i in highs],
    })
    return out


def rs_score(close):
    """
    IBD-style weighted performance (the basis of the RS rating Minervini uses):
    40% weight on the last 3 months, 20% on each of the prior three quarters.
    """
    if len(close) < 253:
        return None
    c = float(close.iloc[-1])
    def ret(n):
        base = float(close.iloc[-n - 1])
        return c / base - 1 if base else 0.0
    return (0.4 * ret(63) + 0.2 * ret(126) + 0.2 * ret(189) + 0.2 * ret(252)) * 100


def twelve_month_return(close):
    if len(close) < 253:
        return None
    start = float(close.iloc[-253])
    return None if start == 0 else (float(close.iloc[-1]) / start - 1) * 100


def market_condition(spy_df):
    """
    Minervini only buys breakouts when the general market is healthy.
    Uses the S&P 500 (SPY) vs its 50- and 200-day moving averages.
    """
    if spy_df is None or len(spy_df) < 200:
        return {"status": "unknown", "label": "Market trend unknown",
                "note": "S&P 500 data unavailable."}
    c = spy_df["Close"]
    price = float(c.iloc[-1])
    ma50  = float(c.rolling(50).mean().iloc[-1])
    ma200 = float(c.rolling(200).mean().iloc[-1])
    if price > ma50 and ma50 > ma200:
        status, label = "uptrend", "Market in confirmed uptrend"
        note = "S&P 500 is above its rising 50- and 200-day averages — breakouts have a tailwind."
    elif price > ma200:
        status, label = "pressure", "Market uptrend under pressure"
        note = ("S&P 500 is below its 50-day but above its 200-day average. "
                "Be selective, keep positions small and stops tight.")
    else:
        status, label = "correction", "Market in correction"
        note = ("S&P 500 is below its 200-day average. Most breakouts fail in corrections — "
                "Minervini moves to cash and waits. Buy signals are downgraded to Setup.")
    return {"status": status, "label": label, "note": note,
            "spy_price": round(price, 2), "spy_ma50": round(ma50, 2), "spy_ma200": round(ma200, 2)}


def _pct(v):
    return f"{v:+.1f}%" if v is not None else "n/a"


def trade_plan(s):
    """Entry at the pivot (or current price inside the buy range), stop under the
    final-contraction low but never more than MAX_STOP_PCT away, targets at 2R/3R."""
    vcp = s["vcp"]
    if not vcp.get("detected"):
        return None
    pivot = vcp["pivot_price"]
    entry = round(max(pivot, s["price"]), 2)
    structural = round(vcp["final_low"] * 0.99, 2)
    floor = round(entry * (1 - MAX_STOP_PCT / 100), 2)
    stop  = max(structural, floor)
    risk  = entry - stop
    if risk <= 0:
        return None
    return {
        "entry":      entry,
        "buy_limit":  round(pivot * (1 + BUY_RANGE_PCT / 100), 2),
        "stop":       stop,
        "stop_basis": "final-contraction low" if stop == structural else f"{MAX_STOP_PCT:.0f}% max-loss rule",
        "risk_pct":   round(risk / entry * 100, 1),
        "t1":         round(entry + 2 * risk, 2),
        "t2":         round(entry + 3 * risk, 2),
    }


def evaluate(s, market):
    """
    Decide what Minervini's rules say about the stock, in priority order:
      Avoid    — not in a Stage 2 uptrend (fails any Trend Template criterion)
      Extended — more than 5% past its pivot; don't chase
      Watch    — Stage 2, but fundamentals, the VCP, or timing aren't there yet
      Setup    — everything lines up; buy only on a breakout above the pivot
      Buy      — breaking out of the VCP, inside the buy range, on volume
    Returns the verdict plus the SEPA checklist that backs it.
    """
    fund   = s["fundamentals"]
    vcp    = s["vcp"]
    eps    = fund.get("eps_growth")
    rev    = fund.get("revenue_growth")
    pvp    = vcp.get("pct_vs_pivot")
    volp   = s.get("vol_vs_avg_pct")

    stage2   = all(s["checks"].values())
    eps_ok   = eps is not None and eps >= EPS_GROWTH_MIN
    rev_ok   = rev is not None and rev >= SALES_GROWTH_MIN
    vcp_ok   = bool(vcp.get("detected"))
    in_range = pvp is not None and -NEAR_PIVOT_PCT <= pvp <= BUY_RANGE_PCT
    broke    = pvp is not None and 0 <= pvp <= BUY_RANGE_PCT
    extended = pvp is not None and pvp > BUY_RANGE_PCT and vcp.get("above_base_high")
    mid_base = pvp is not None and pvp > BUY_RANGE_PCT and not vcp.get("above_base_high")
    vol_ok   = volp is not None and volp >= BREAKOUT_VOL_PCT
    mkt_ok   = market.get("status") != "correction"

    checklist = [
        {"key": "stage2", "label": "Stage 2 — all 8 Trend Template criteria", "pass": stage2,
         "detail": f"{s['score']}/8"},
        {"key": "eps", "label": f"EPS growth ≥ {EPS_GROWTH_MIN:.0f}% (latest qtr YoY)", "pass": eps_ok,
         "detail": _pct(eps)},
        {"key": "sales", "label": f"Sales growth ≥ {SALES_GROWTH_MIN:.0f}% (latest qtr YoY)", "pass": rev_ok,
         "detail": _pct(rev)},
        {"key": "vcp", "label": "Valid VCP base", "pass": vcp_ok,
         "detail": (f"{vcp['contractions']} contraction{'s' if vcp['contractions'] != 1 else ''}"
                    if vcp.get("contractions") else "no base")},
        {"key": "range", "label": f"Price within {NEAR_PIVOT_PCT:.0f}% below to {BUY_RANGE_PCT:.0f}% above pivot",
         "pass": in_range, "detail": f"{pvp:+.1f}% vs pivot" if pvp is not None else "no pivot"},
        {"key": "volume", "label": f"Breakout volume ≥ +{BREAKOUT_VOL_PCT:.0f}% vs 50-day avg",
         "pass": broke and vol_ok, "detail": f"{volp:+.0f}% today" if volp is not None else "n/a"},
        {"key": "market", "label": "General market not in correction", "pass": mkt_ok,
         "detail": market.get("label", "")},
    ]

    # Everything standing between this stock and a buy, most fundamental first
    gaps = []
    failed_tt = [TEMPLATE_LABELS[k] for k, v in s["checks"].items() if not v]
    if failed_tt:
        gaps.append(f"Fails the Trend Template ({s['score']}/8): " + "; ".join(failed_tt) + ".")
    if not eps_ok:
        gaps.append(f"EPS growth {_pct(eps)} is below the {EPS_GROWTH_MIN:.0f}% minimum." if eps is not None
                    else "No EPS growth data — can't confirm earnings strength.")
    if not rev_ok:
        gaps.append(f"Sales growth {_pct(rev)} is below the {SALES_GROWTH_MIN:.0f}% minimum." if rev is not None
                    else "No sales growth data — can't confirm revenue strength.")
    if not stage2:
        pass   # timing and base details don't matter until the stock is in Stage 2
    elif extended:
        gaps.append(f"Already {pvp:.1f}% above the ${vcp['pivot_price']:.2f} pivot — past the {BUY_RANGE_PCT:.0f}% "
                    f"buy range. Don't chase; wait for a new base.")
    elif mid_base:
        gaps.append(f"Rallying inside the base, still below its ${vcp['base_high']:.2f} high. Wait for a tight "
                    f"pullback to set a new pivot.")
    elif not vcp_ok:
        crit = vcp.get("criteria", [])
        few = any(c["key"] == "contractions" and not c["pass"] for c in crit)
        failed_vcp = [f"{c['label']} ({c['detail']})" for c in crit
                      if not c["pass"] and not (few and c["key"] == "contracting")]
        gaps.append("No valid VCP yet — " + "; ".join(failed_vcp) + "." if failed_vcp else "No valid VCP yet.")
    elif pvp < -NEAR_PIVOT_PCT:
        gaps.append(f"{abs(pvp):.1f}% below the ${vcp['pivot_price']:.2f} pivot — let it work closer to the pivot.")
    elif broke and not vol_ok:
        gaps.append(f"Above the pivot but volume is only {volp:+.0f}% vs average — Minervini wants "
                    f"+{BREAKOUT_VOL_PCT:.0f}% or more to confirm a breakout.")
    if not mkt_ok:
        gaps.append("General market is in a correction — most breakouts fail; stay mostly in cash.")

    pivot_txt = f"${vcp['pivot_price']:.2f}" if vcp.get("pivot_price") else None
    if not stage2:
        code = "avoid"
        headline = (f"Not a Stage 2 uptrend ({s['score']}/8 on the Trend Template). "
                    f"Minervini only buys stocks that pass all 8.")
    elif extended:
        code = "extended"
        headline = (f"Stage 2 leader, but extended {pvp:.1f}% past its {pivot_txt} pivot. "
                    f"Don't chase — wait for the next base.")
    elif not (eps_ok and rev_ok):
        code = "watch"
        headline = "Stage 2 chart, but the fundamentals don't meet SEPA standards yet."
    elif mid_base:
        code = "watch"
        headline = "Stage 2 with strong fundamentals, still building the right side of its base."
    elif not vcp_ok:
        code = "watch"
        headline = "Stage 2 with strong fundamentals — wait for a proper VCP base to form."
    elif pvp < -NEAR_PIVOT_PCT:
        code = "watch"
        headline = f"Valid VCP, but {abs(pvp):.1f}% below the {pivot_txt} pivot. Keep watching."
    elif broke and vol_ok and mkt_ok:
        code = "buy"
        headline = (f"Breaking out of a VCP above the {pivot_txt} pivot on {volp:+.0f}% volume, with every "
                    f"SEPA criterion met. This is the setup Minervini buys — up to ${s['trade']['buy_limit']:.2f}."
                    if s.get("trade") else
                    f"Breaking out of a VCP above the {pivot_txt} pivot on volume with every SEPA criterion met.")
    else:
        code = "setup"
        if broke and vol_ok:
            headline = f"Breaking out above {pivot_txt} on volume, but the market is in a correction — wait."
        elif broke:
            headline = f"Above the {pivot_txt} pivot but without volume confirmation. Buy only on a volume surge."
        else:
            headline = (f"All SEPA criteria met with a valid VCP. Set an alert: buy only if it clears "
                        f"{pivot_txt} on heavy volume.")

    return {
        "code":     code,
        "label":    VERDICTS[code]["label"],
        "rank":     VERDICTS[code]["rank"],
        "headline": headline,
        "gaps":     gaps,
    }, checklist


def generate_comments(s):
    """Plain-English read of the stock through Minervini's rules."""
    lines  = []
    lv     = s["levels"]
    vcp    = s["vcp"]
    fund   = s["fundamentals"]
    rs     = s.get("rs_rank")

    if s["score"] == 8:
        lines.append("Passes all 8 Trend Template criteria — a confirmed Stage 2 uptrend.")
        if lv.get("ma200_up_5m"):
            lines.append("The 200-day MA has been rising for 5+ months, which Minervini prefers.")
    else:
        lines.append(f"Passes {s['score']} of 8 Trend Template criteria — not a Stage 2 uptrend yet.")

    if lv["pct_from_52w_high"] >= -5:
        lines.append(f"Trading within 5% of its 52-week high (${lv['high_52w']:.2f}) — leaders make new highs.")
    else:
        lines.append(f"{abs(lv['pct_from_52w_high']):.1f}% below its 52-week high (${lv['high_52w']:.2f}).")

    if rs is not None:
        if rs >= 90:
            lines.append(f"RS {rs}: a true market leader.")
        elif rs >= 80:
            lines.append(f"RS {rs}: strong relative strength.")
        elif rs >= RS_MIN:
            lines.append(f"RS {rs}: acceptable, though Minervini prefers 80–90+.")
        else:
            lines.append(f"RS {rs}: lagging — Minervini wants at least {RS_MIN}.")

    if vcp.get("contraction_depths"):
        chain = " → ".join(f"{d}%" for d in vcp["contraction_depths"])
        if vcp.get("detected"):
            lines.append(f"Valid VCP: {vcp['contractions']} contractions ({chain}) over {vcp['base_days']} days, "
                         f"with volume drying up to {vcp['final_vol_ratio'] * 100:.0f}% of average.")
        else:
            lines.append(f"Base pullbacks so far: {chain}.")

    eps, rev = fund.get("eps_growth"), fund.get("revenue_growth")
    if eps is not None and rev is not None:
        lines.append(f"Latest quarter: EPS {eps:+.1f}%, sales {rev:+.1f}% year over year.")

    return " ".join(lines)


def load_stock_list():
    """Read Stock List.xlsx and return (all_stocks_set, growth_set)."""
    if not STOCK_LIST.exists():
        print(f"Warning: {STOCK_LIST} not found — scanning all tickers from snapshot CSV")
        return None, set()
    df = pd.read_excel(STOCK_LIST, engine="openpyxl")
    df.columns = [c.strip() for c in df.columns]
    all_stocks = set(df.loc[df["ALL STOCKS"] == "Y", "TICKER"].dropna().str.strip())
    growth     = set(df.loc[df["GROWTH COMPANIES"] == "Y", "TICKER"].dropna().str.strip())
    return all_stocks, growth


def get_tickers(all_stocks_filter=None):
    if all_stocks_filter is not None:
        return sorted(all_stocks_filter)
    df = pd.read_csv(SNAPSHOT_CSV)
    return sorted(df["tickerSymbol"].dropna().unique().tolist())


def pct_fmt(v):
    return round(float(v) * 100, 1) if v is not None else None


def build_fundamentals(info, snap):
    inst_raw = info.get("heldPercentInstitutions")
    return {
        # Yahoo's earningsGrowth / revenueGrowth are latest quarter vs the year-ago quarter
        "eps_growth":         pct_fmt(info.get("earningsGrowth")),
        "revenue_growth":     pct_fmt(info.get("revenueGrowth")),
        "roe":                pct_fmt(info.get("returnOnEquity")),
        "profit_margin":      pct_fmt(info.get("profitMargins")),
        "trailing_eps":       info.get("trailingEps"),
        "forward_eps":        info.get("forwardEps"),
        "forward_pe":         info.get("forwardPE"),
        # Valuation multiples: prefer snapshot CSV (more reliable than Yahoo info)
        "pe_ratio":           snap.get("pe_ratio") or info.get("trailingPE"),
        "price_to_sales":     snap.get("price_to_sales"),
        "price_to_cash_flow": snap.get("price_to_cash_flow"),
        "price_to_fcf":       snap.get("price_to_fcf"),
        "price_to_book":      snap.get("price_to_book"),
        "div_yield":          snap.get("div_yield"),
        "institutional_pct":  round(float(inst_raw) * 100, 1) if inst_raw else None,
        "sector":             info.get("sector"),
        "industry":           info.get("industry"),
    }


def analyze_df(ticker, df, info, snap=None, partial_today=False):
    """Technical + fundamental read of one stock from its daily OHLCV frame.
    Returns None when there isn't enough history for the Trend Template."""
    snap = snap or {}
    if df is None or len(df) < 222:
        return None

    # 50-day average volume from complete sessions only
    prior_vols = df["Volume"].iloc[:-1] if partial_today else df["Volume"]
    avg_vol_50 = int(prior_vols.tail(50).mean()) if len(prior_vols) >= 10 else None

    df = adjust_volume_intraday(df)
    current_vol_adj = int(df["Volume"].iloc[-1])
    close = df["Close"]
    price = float(close.iloc[-1])

    checks, levels = check_trend_template(df)
    if checks is None:
        return None

    vcp = detect_vcp(df)

    hist = df.tail(260)
    history = {
        "dates":   [d.strftime("%Y-%m-%d") for d in hist.index],
        "closes":  [round(float(c), 2) for c in hist["Close"]],
        "volumes": [int(v) for v in hist["Volume"]],
    }

    ret12 = twelve_month_return(close)
    return {
        "ticker":          ticker,
        "company_name":    info.get("longName") or info.get("shortName") or ticker,
        "price":           round(price, 2),
        "avg_vol_50":      avg_vol_50,
        "current_vol_adj": current_vol_adj,
        "vol_vs_avg_pct":  round((current_vol_adj / avg_vol_50 - 1) * 100, 1) if avg_vol_50 else None,
        "score":           0,          # set once RS rank is known
        "checks":          checks,
        "levels":          levels,
        "ma50":            levels["ma50"],
        "ma150":           levels["ma150"],
        "ma200":           levels["ma200"],
        "vcp":             vcp,
        "rs_weighted":     rs_score(close),
        "return_12m":      round(ret12, 1) if ret12 is not None else None,
        "rs_rank":         None,
        "fundamentals":    build_fundamentals(info, snap),
        "history":         history,
    }


def analyze_ticker(ticker, snap=None):
    try:
        tk = yf.Ticker(ticker)
        df = tk.history(period="2y", auto_adjust=True)
        if df is None or len(df) < 222:
            return None
        df.index = df.index.tz_localize(None) if df.index.tz else df.index
        partial_today = df.index[-1].date() == datetime.now().date()
        try:
            info = tk.info or {}
        except Exception:
            info = {}
        return analyze_df(ticker, df, info, snap, partial_today)
    except Exception as e:
        print(f"  Error — {ticker}: {e}")
        return None


def finalize(results, market, spy_return_12m=None):
    """Universe-level steps: RS ranks, Trend Template #8, trade plan, verdicts."""
    raw = [r["rs_weighted"] for r in results if r["rs_weighted"] is not None]
    for r in results:
        if r["rs_weighted"] is not None and raw:
            pct = sum(1 for v in raw if v <= r["rs_weighted"]) / len(raw) * 100
            r["rs_rank"] = max(1, min(99, round(pct)))
        r["checks"]["rs_rank_70plus"] = r["rs_rank"] is not None and r["rs_rank"] >= RS_MIN
        r["score"] = sum(r["checks"].values())
        r["rs_vs_spy"] = (round(r["return_12m"] - spy_return_12m, 1)
                          if r["return_12m"] is not None and spy_return_12m is not None else None)
        r["trade"] = trade_plan(r)
        r["verdict"], r["sepa_checks"] = evaluate(r, market)
        r["comments"] = generate_comments(r)

    results.sort(key=lambda r: (r["verdict"]["rank"], r["score"], r["rs_rank"] or 0), reverse=True)
    return results


def _json_default(o):
    """numpy / pandas scalars (e.g. numpy.bool_ from comparisons) → plain Python."""
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def inject(data, html_path):
    html = Path(html_path).read_text(encoding="utf-8")
    replacement = f"const SWING_DATA = {json.dumps(data, separators=(',', ':'), default=_json_default)};"
    html = re.sub(r"const SWING_DATA = \{.*?\};", lambda _: replacement, html, flags=re.DOTALL)
    Path(html_path).write_text(html, encoding="utf-8")


def main():
    snapshot_data = load_snapshot_data()
    print(f"Snapshot data loaded: {len(snapshot_data)} tickers")

    all_stocks_filter, growth_set = load_stock_list()
    tickers = get_tickers(all_stocks_filter)
    print(f"Tickers loaded: {len(tickers)}")

    print("Fetching SPY benchmark...")
    try:
        spy_df = yf.Ticker("SPY").history(period="2y", auto_adjust=True)
    except Exception as e:
        print(f"  SPY fetch failed: {e}")
        spy_df = None
    market = market_condition(spy_df)
    spy_ret = twelve_month_return(spy_df["Close"]) if spy_df is not None and len(spy_df) else None
    print(f"Market: {market['label']}")

    results = []
    for i, ticker in enumerate(tickers, 1):
        print(f"[{i}/{len(tickers)}] {ticker}")
        result = analyze_ticker(ticker, snap=snapshot_data.get(ticker))
        if result:
            result["growth_company"] = ticker in growth_set
            results.append(result)

    finalize(results, market, spy_ret)

    def count(code):
        return sum(1 for r in results if r["verdict"]["code"] == code)

    if _TZ_NY:
        generated_at = datetime.now(_TZ_NY).strftime("%Y-%m-%d %H:%M ET")
    else:
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M UTC")

    payload = {
        "schema":        2,
        "generated_at":  generated_at,
        "market":        market,
        "total_scanned": len(tickers),
        "total_results": len(results),
        "stage2_count":  sum(1 for r in results if r["score"] == 8),
        "vcp_count":     sum(1 for r in results if r["score"] == 8 and r["vcp"]["detected"]),
        "counts":        {code: count(code) for code in VERDICTS},
        "stocks":        results,
    }

    inject(payload, DASHBOARD_HTML)

    print(f"\nDone — {len(results)}/{len(tickers)} stocks scanned")
    print(f"Stage 2 (8/8):     {payload['stage2_count']}")
    print(f"Stage 2 + VCP:     {payload['vcp_count']}")
    for code in VERDICTS:
        print(f"{VERDICTS[code]['label']:<18} {payload['counts'][code]}")
    print(f"Injected into:     {DASHBOARD_HTML.resolve()}")


if __name__ == "__main__":
    main()
