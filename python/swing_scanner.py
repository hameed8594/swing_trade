#!/usr/bin/env python3
"""
swing_scanner.py — Minervini SEPA/VCP swing trading scanner.

Reads tickers from snapshot_results.csv, fetches Yahoo Finance data,
runs trend template + VCP checks, and injects results into swing.html.

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


def compute_swing_analysis(score, vcp, price, ma50):
    """Return swing_score (0-10), buy_signal, ideal_entry, and targets dict."""
    actionable_vcp = vcp.get("detected") and not vcp.get("above_pivot", False)
    swing_score = min(10, score
                      + (1 if actionable_vcp else 0)
                      + (1 if (actionable_vcp and vcp.get("volume_dry")) else 0))

    pct_from_pivot = vcp.get("pct_from_pivot", 999) if vcp.get("detected") else 999
    already_extended = vcp.get("detected") and vcp.get("above_pivot", False)

    if already_extended:
        buy_signal  = "No"
        pct_above   = vcp.get("pct_above_pivot", 0)
        pivot_price = vcp.get("pivot_price", price)
        ideal_entry = (f"Extended — price is {pct_above:.1f}% above the ${pivot_price:.2f} pivot. "
                       f"Wait for a pullback to base before entering.")
    elif vcp.get("detected") and swing_score >= 8 and pct_from_pivot <= 5:
        buy_signal = "Yes"
        pivot       = vcp["pivot_price"]
        entry_price = round(pivot * 1.005, 2)
        ideal_entry = f"Buy above ${pivot:.2f} pivot on volume (entry ~${entry_price:.2f})"
    elif vcp.get("detected") and swing_score >= 6:
        buy_signal = "Wait"
        pivot       = vcp["pivot_price"]
        entry_price = round(pivot * 1.005, 2)
        ideal_entry = f"Buy above ${pivot:.2f} pivot on volume (entry ~${entry_price:.2f})"
    elif score >= 7 and ma50:
        buy_signal  = "Wait"
        ideal_entry = f"Wait for VCP to form — potential support near MA50 ${ma50:.2f}"
    else:
        buy_signal  = "No"
        ideal_entry = "Not actionable — needs Stage 2 structure and a VCP setup"

    target = None
    if not already_extended and vcp.get("detected") and vcp.get("pivot_price") and vcp.get("stop_loss"):
        entry = round(vcp["pivot_price"] * 1.005, 2)
        stop  = vcp["stop_loss"]
        risk  = entry - stop
        if risk > 0:
            target = {
                "entry": entry,
                "stop":  stop,
                "risk":  round(risk, 2),
                "t1":    round(entry + 2 * risk, 2),
                "t2":    round(entry + 3 * risk, 2),
                "rr1":   2.0,
                "rr2":   3.0,
            }

    return {
        "swing_score": swing_score,
        "buy_signal":  buy_signal,
        "ideal_entry": ideal_entry,
        "target":      target,
    }


def generate_comments(s):
    """Generate Minervini-style analysis text from a completed stock result dict."""
    lines   = []
    score   = s["score"]
    vcp     = s.get("vcp", {})
    fund    = s.get("fundamentals", {})
    checks  = s.get("checks", {})
    rs_rank = s.get("rs_rank")
    high_52w           = s.get("high_52w", 0)
    pct_from_52w_high  = s.get("pct_from_52w_high", -100)

    # Trend / stage
    if score == 8:
        lines.append("All 8 Minervini trend-template criteria met — textbook Stage 2 uptrend with full MA alignment.")
    elif score >= 7:
        lines.append(f"Strong Stage 2 structure ({score}/8). Near-perfect trend alignment with only minor gaps.")
    elif score >= 5:
        lines.append(f"Partial trend template ({score}/8). Not yet a clean Stage 2 — some criteria still missing.")
    else:
        lines.append(f"Weak trend template ({score}/8). Stock is not in an actionable Stage 2 position.")

    if checks.get("ma200_trending_up"):
        lines.append("200-day MA is trending upward, confirming long-term institutional accumulation phase.")

    # Momentum / 52w position
    if pct_from_52w_high >= -5:
        lines.append(f"Price is within 5% of the 52-week high (${high_52w:.2f}) — strong momentum, leadership territory.")
    elif pct_from_52w_high >= -15:
        lines.append(f"Price is {abs(pct_from_52w_high):.1f}% below the 52-week high — within a healthy consolidation range.")
    else:
        lines.append(f"Price is {abs(pct_from_52w_high):.1f}% off the 52-week high — needs to reclaim more ground before an ideal entry.")

    # VCP quality
    if vcp.get("detected"):
        depths     = vcp.get("contraction_depths", [])
        depth_str  = " → ".join(f"{d}%" for d in depths) if depths else ""
        if len(depths) >= 3:
            lines.append(f"High-quality VCP: {vcp['contractions']} contractions of shrinking depth ({depth_str}) — classic Minervini setup.")
        elif len(depths) >= 2:
            lines.append(f"Valid VCP with {vcp['contractions']} contractions ({depth_str}) — base is tightening.")

        if vcp.get("above_pivot"):
            pct_above = vcp.get("pct_above_pivot", 0)
            lines.append(f"Price has already broken out {pct_above:.1f}% above the ${vcp.get('pivot_price', 0):.2f} pivot — the base entry is in the past. "
                         f"This is now extended; Minervini would wait for a new base to form before entering.")
        else:
            if vcp.get("volume_dry"):
                lines.append("Volume is drying up within the base — diminishing supply is a bullish precondition for a breakout.")
            else:
                lines.append("Volume has not fully dried up yet. Watch for a volume contraction before committing to an entry.")
            pct = vcp.get("pct_from_pivot", 0)
            if pct <= 2:
                lines.append(f"Currently within {pct:.1f}% of the pivot — price is in the ideal breakout buy zone.")
            elif pct <= 6:
                lines.append(f"About {pct:.1f}% below the pivot — approaching but not yet at the breakout point. Wait for volume confirmation.")
            else:
                lines.append(f"Currently {pct:.1f}% below the pivot — allow more time for the base to develop before acting.")
    else:
        lines.append("No VCP detected. The stock may need more consolidation before an ideal low-risk entry emerges.")

    # Relative strength
    if rs_rank is not None:
        if rs_rank >= 90:
            lines.append(f"Exceptional relative strength (RS {rs_rank}) — a true market leader outperforming nearly all peers.")
        elif rs_rank >= 80:
            lines.append(f"Strong relative strength (RS {rs_rank}) — outperforming the majority of the market.")
        elif rs_rank >= 60:
            lines.append(f"Moderate relative strength (RS {rs_rank}). Acceptable, but Minervini prefers RS above 80 for ideal setups.")
        else:
            lines.append(f"Weak relative strength (RS {rs_rank}). Minervini avoids stocks lagging the market — proceed with extra caution.")

    # Fundamentals
    eps = fund.get("eps_growth")
    rev = fund.get("revenue_growth")
    if eps is not None:
        if eps >= 25:
            lines.append(f"EPS growth of +{eps}% provides strong institutional-quality fundamental support.")
        elif eps >= 10:
            lines.append(f"EPS growing at +{eps}% — decent but below the explosive 25%+ Minervini prefers.")
        elif eps < 0:
            lines.append(f"Negative EPS growth ({eps}%) — fundamental weakness. Use tighter stops and smaller position size.")
    if rev is not None and rev >= 20:
        lines.append(f"Revenue growing at +{rev}% — demand-driven business supporting the technical setup.")

    # Red flags
    red_flags = []
    if not checks.get("ma200_trending_up"):
        red_flags.append("200-day MA not yet trending up")
    if not checks.get("price_within_25pct_of_52w_high"):
        red_flags.append("price more than 25% from 52-week high")
    if rs_rank is not None and rs_rank < 70:
        red_flags.append(f"RS rank below 70 (at {rs_rank})")
    if eps is not None and eps < 0:
        red_flags.append("negative EPS growth")
    if red_flags:
        lines.append("Red flags: " + "; ".join(red_flags) + ". Size positions accordingly.")

    return " ".join(lines)


def get_tickers():
    df = pd.read_csv(SNAPSHOT_CSV)
    return sorted(df["tickerSymbol"].dropna().unique().tolist())


def sepa_check(score, vcp, fundamentals, rs_rank, checks):
    """
    Evaluate Minervini SEPA criteria.

    Returns (sepa_checks dict, sepa_qualified bool, sepa_pass bool).
      sepa_qualified = E + P criteria met (watchlist-ready, waiting for VCP)
      sepa_pass      = sepa_qualified + VCP detected (full actionable setup)
    Announcement (A) cannot be automated and is excluded.
    """
    eps  = fundamentals.get("eps_growth")
    rev  = fundamentals.get("revenue_growth")
    inst = fundamentals.get("institutional_pct")

    sepa_checks = {
        "stage2_trend":         score >= 7,
        "eps_growth_20pct":     eps is not None and eps >= 20,
        "revenue_growth_15pct": rev is not None and rev >= 15,
        "rs_rank_70plus":       rs_rank is not None and rs_rank >= 70,
        "within_25pct_52w_high": checks.get("price_within_25pct_of_52w_high", False),
        "vcp_detected":         bool(vcp.get("detected")),
        "inst_ownership_ok":    inst is not None and 30 <= inst <= 70,
    }

    core = ["stage2_trend", "eps_growth_20pct", "revenue_growth_15pct",
            "rs_rank_70plus", "within_25pct_52w_high"]
    sepa_qualified = all(sepa_checks[k] for k in core)
    sepa_pass      = sepa_qualified and sepa_checks["vcp_detected"]

    return sepa_checks, sepa_qualified, sepa_pass


def check_trend_template(close):
    if len(close) < 210:
        return None, 0, {}

    ma50  = close.rolling(50).mean()
    ma150 = close.rolling(150).mean()
    ma200 = close.rolling(200).mean()

    price      = float(close.iloc[-1])
    ma50_now   = float(ma50.iloc[-1])
    ma150_now  = float(ma150.iloc[-1])
    ma200_now  = float(ma200.iloc[-1])
    ma200_ago  = float(ma200.iloc[-22])

    low_52w  = float(close.tail(252).min())
    high_52w = float(close.tail(252).max())

    checks = {
        "price_above_200ma":              price > ma200_now,
        "price_above_150ma":              price > ma150_now,
        "ma150_above_ma200":              ma150_now > ma200_now,
        "ma200_trending_up":              ma200_now > ma200_ago,
        "ma50_above_ma150_and_ma200":     ma50_now > ma150_now and ma50_now > ma200_now,
        "price_above_ma50":               price > ma50_now,
        "price_30pct_above_52w_low":      price >= low_52w * 1.30,
        "price_within_25pct_of_52w_high": price >= high_52w * 0.75,
    }
    score = sum(checks.values())
    ma_vals = {
        "ma50": round(ma50_now, 2),
        "ma150": round(ma150_now, 2),
        "ma200": round(ma200_now, 2),
    }
    return checks, score, ma_vals


def find_swing_highs(series, window=7):
    highs = []
    for i in range(window, len(series) - window):
        if series.iloc[i] == series.iloc[i - window: i + window + 1].max():
            highs.append(i)
    return highs


def detect_vcp(df):
    if len(df) < 60:
        return {"detected": False}

    recent = df.tail(126).copy()
    close  = recent["Close"].reset_index(drop=True)
    volume = recent["Volume"].reset_index(drop=True)

    highs = find_swing_highs(close, window=7)
    if len(highs) < 2:
        return {"detected": False}

    highs = highs[-4:]

    contractions = []
    for i in range(len(highs) - 1):
        h1 = float(close.iloc[highs[i]])
        h2 = float(close.iloc[highs[i + 1]])
        trough = float(close.iloc[highs[i]: highs[i + 1] + 1].min())
        ref    = max(h1, h2)
        depth  = (ref - trough) / ref * 100
        contractions.append(depth)

    is_contracting = len(contractions) >= 2 and all(
        contractions[j] > contractions[j + 1]
        for j in range(len(contractions) - 1)
    )

    last_high_idx = highs[-1]
    pivot         = float(close.iloc[last_high_idx])
    current       = float(close.iloc[-1])
    depth_raw     = (pivot - current) / pivot * 100   # negative = price above pivot
    current_depth = max(0.0, depth_raw)
    above_pivot   = depth_raw < -3                    # >3% above pivot = already broke out

    recent_vol = float(volume.tail(10).mean())
    prior_vol  = float(volume.iloc[max(0, len(volume) - 40): len(volume) - 10].mean())
    volume_dry = bool(prior_vol > 0 and recent_vol < prior_vol * 0.8)

    contraction_slice = close.iloc[last_high_idx:]
    stop_loss = float(contraction_slice.min()) * 0.98

    detected = is_contracting and current_depth < 25.0

    return {
        "detected":           detected,
        "contractions":       len(contractions),
        "contraction_depths": [round(c, 1) for c in contractions],
        "current_depth_pct":  round(current_depth, 1),
        "pivot_price":        round(pivot, 2),
        "pct_from_pivot":     round(current_depth, 1),
        "pct_above_pivot":    round(-depth_raw, 1) if above_pivot else 0.0,
        "above_pivot":        above_pivot,
        "volume_dry":         volume_dry,
        "stop_loss":          round(stop_loss, 2),
    }


def rs_performance(close, period=252):
    if len(close) < period:
        return None
    start = float(close.iloc[-period])
    end   = float(close.iloc[-1])
    return None if start == 0 else (end - start) / start * 100


def pct_fmt(v):
    return round(float(v) * 100, 1) if v is not None else None


def analyze_ticker(ticker, spy_perf, snap=None):
    try:
        tk = yf.Ticker(ticker)
        df = tk.history(period="2y", auto_adjust=True)
        if df is None or len(df) < 210:
            return None

        df.index = df.index.tz_localize(None) if df.index.tz else df.index

        # Compute 50-day avg volume from complete trading days only
        raw_vol = df["Volume"]
        last_bar_date = df.index[-1].date()
        today_date = datetime.now().date()
        if last_bar_date == today_date:
            prior_vols = raw_vol.iloc[:-1]
        else:
            prior_vols = raw_vol
        avg_vol_50 = int(prior_vols.tail(50).mean()) if len(prior_vols) >= 10 else None

        df = adjust_volume_intraday(df)
        current_vol_adj = int(df["Volume"].iloc[-1])
        close = df["Close"]
        price = float(close.iloc[-1])

        checks, score, ma_vals = check_trend_template(close)
        if checks is None:
            return None

        vcp       = detect_vcp(df)
        ticker_perf = rs_performance(close)
        rs_raw    = (ticker_perf - spy_perf) if (ticker_perf is not None and spy_perf is not None) else None

        high_52w = float(close.tail(252).max())
        low_52w  = float(close.tail(252).min())

        # Price history for chart (260 days → enough for 200-day MA)
        hist = df.tail(260)
        hist_close  = hist["Close"].reset_index(drop=True)
        swing_idxs  = find_swing_highs(hist_close, window=7)[-10:]

        history = {
            "dates":       [d.strftime("%Y-%m-%d") for d in hist.index],
            "closes":      [round(float(c), 2) for c in hist["Close"]],
            "volumes":     [int(v) for v in hist["Volume"]],
            "swing_highs": swing_idxs,
        }

        # Fundamentals from Yahoo info
        try:
            info = tk.info or {}
        except Exception:
            info = {}

        company_name = info.get("longName") or info.get("shortName") or ticker

        snap = snap or {}
        inst_raw = info.get("heldPercentInstitutions")
        inst_pct = round(float(inst_raw) * 100, 1) if inst_raw else None

        fundamentals = {
            # Growth from Yahoo (best available source for these)
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
            # Institutional ownership from Yahoo
            "institutional_pct":  inst_pct,
            "sector":             info.get("sector"),
            "industry":           info.get("industry"),
        }

        analysis = compute_swing_analysis(score, vcp, price, ma_vals["ma50"])

        # SEPA check runs after rs_rank is assigned in main(); placeholder None here
        return {
            "ticker":            ticker,
            "company_name":      company_name,
            "price":             round(price, 2),
            "avg_vol_50":        avg_vol_50,
            "current_vol_adj":   current_vol_adj,
            "score":             score,
            "checks":            checks,
            "vcp":               vcp,
            "rs_raw":            round(rs_raw, 1) if rs_raw is not None else None,
            "rs_rank":           None,
            "ma50":              ma_vals["ma50"],
            "ma150":             ma_vals["ma150"],
            "ma200":             ma_vals["ma200"],
            "high_52w":          round(high_52w, 2),
            "low_52w":           round(low_52w, 2),
            "pct_from_52w_high": round((price - high_52w) / high_52w * 100, 1),
            "fundamentals":      fundamentals,
            "history":           history,
            "analysis":          analysis,
            "sepa_checks":       None,
            "sepa_qualified":    False,
            "sepa_pass":         False,
            "comments":          "",
        }
    except Exception as e:
        print(f"  Error — {ticker}: {e}")
        return None


def inject(data, html_path):
    html = Path(html_path).read_text(encoding="utf-8")
    replacement = f"const SWING_DATA = {json.dumps(data, separators=(',', ':'))};"
    html = re.sub(r"const SWING_DATA = \{.*?\};", lambda _: replacement, html, flags=re.DOTALL)
    Path(html_path).write_text(html, encoding="utf-8")


def main():
    snapshot_data = load_snapshot_data()
    print(f"Snapshot data loaded: {len(snapshot_data)} tickers")

    tickers = get_tickers()
    print(f"Tickers loaded: {len(tickers)}")

    print("Fetching SPY benchmark...")
    spy_df   = yf.Ticker("SPY").history(period="2y", auto_adjust=True)
    spy_perf = rs_performance(spy_df["Close"]) if spy_df is not None else None

    results = []
    for i, ticker in enumerate(tickers, 1):
        print(f"[{i}/{len(tickers)}] {ticker}")
        result = analyze_ticker(ticker, spy_perf, snap=snapshot_data.get(ticker))
        if result:
            results.append(result)

    # Assign RS ranks first — sepa_check needs them
    raw_vals = [r["rs_raw"] for r in results if r["rs_raw"] is not None]
    for r in results:
        if r["rs_raw"] is not None:
            r["rs_rank"] = round(
                sum(1 for v in raw_vals if v <= r["rs_raw"]) / len(raw_vals) * 100
            )

    # Run SEPA checks now that rs_rank is populated
    for r in results:
        sc, sq, sp = sepa_check(r["score"], r["vcp"], r["fundamentals"],
                                r["rs_rank"], r["checks"])
        r["sepa_checks"]    = sc
        r["sepa_qualified"] = sq
        r["sepa_pass"]      = sp

    for r in results:
        r["comments"] = generate_comments(r)

    results.sort(key=lambda x: (x["sepa_pass"], x["sepa_qualified"],
                                x["score"], x["vcp"]["detected"]), reverse=True)

    stage2_count = sum(1 for r in results if r["score"] >= 7)
    vcp_count    = sum(1 for r in results if r["vcp"]["detected"])
    sepa_count   = sum(1 for r in results if r["sepa_pass"])

    if _TZ_NY:
        generated_at = datetime.now(_TZ_NY).strftime("%Y-%m-%d %H:%M ET")
    else:
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M UTC")

    payload = {
        "generated_at":  generated_at,
        "total_scanned": len(tickers),
        "total_results": len(results),
        "stage2_count":  stage2_count,
        "vcp_count":     vcp_count,
        "sepa_count":    sepa_count,
        "stocks":        results,
    }

    inject(payload, DASHBOARD_HTML)

    print(f"\nDone — {len(results)}/{len(tickers)} stocks scanned")
    print(f"Stage 2 (≥7/8):   {stage2_count}")
    print(f"VCP setups:        {vcp_count}")
    print(f"SEPA passes:       {sepa_count}")
    print(f"Injected into:     {DASHBOARD_HTML.resolve()}")


if __name__ == "__main__":
    main()
