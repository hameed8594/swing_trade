#!/usr/bin/env python3
"""
send_alerts.py — open a GitHub issue for each new Buy / Setup from the swing scanner.

Run by the "Update Swing Dashboard" workflow after the scan. Reads the alerts
file written by swing_scanner.py (SWING_ALERTS_FILE) and uses the GitHub CLI
(`gh`, authenticated with GH_TOKEN). The issue is assigned to the repository
owner so GitHub notifies them by email and in the GitHub mobile app.

At most one issue per ticker, status and day: a stock that flickers in and
out of Setup during the session doesn't re-alert.

Usage:
    python send_alerts.py ALERTS_JSON
    python send_alerts.py --test      # send a sample alert to check notifications
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

LABEL = "swing-alert"
ICONS = {"buy": "🟢", "setup": "🟡"}


def gh(*args, check=True):
    return subprocess.run(["gh", *args], check=check, capture_output=True, text=True)


def money(v):
    return f"${v:,.2f}" if v is not None else "—"


def pct(v, d=1):
    return f"{v:+.{d}f}%" if v is not None else "—"


def issue_body(a):
    t = a.get("trade") or {}
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    owner, _, name = repo.partition("/")
    links = []
    if repo:
        links.append(f"[Dashboard](https://{owner}.github.io/{name}/swing.html)")
    if os.environ.get("GITHUB_RUN_ID"):
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        links.append(f"[Scan run]({server}/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']})")
    rows = [
        ("Price", f"{money(a['price'])} ({pct(a.get('pct_vs_pivot'))} vs pivot)"),
        ("Pivot → buy limit", f"{money(a.get('pivot'))} → {money(t.get('buy_limit'))}"),
        ("Stop", f"{money(t.get('stop'))} ({t.get('risk_pct', '—')}% risk, {t.get('stop_basis', '—')})"),
        ("Targets (2R / 3R)", f"{money(t.get('t1'))} / {money(t.get('t2'))}"),
        ("RS rating", str(a.get("rs_rank", "—"))),
        ("EPS / sales growth", f"{pct(a.get('eps_growth'))} / {pct(a.get('sales_growth'))}"),
        ("Volume vs 50-day avg", pct(a.get("vol_vs_avg"), 0)),
        ("Market", a.get("market") or "—"),
    ]
    was = f" (was {a['previous'].title()})" if a.get("previous") else ""
    return "\n".join([
        f"**{a['ticker']}** ({a['company']}) is now **{a['label']}**{was} on the Minervini swing scanner.",
        "",
        f"> {a['headline']}",
        "",
        "| | |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in rows],
        "",
        f"Scan: {a['generated_at']}" + (" · " + " · ".join(links) if links else ""),
        "",
        "_Automated alert from the Update Swing Dashboard workflow. "
        "Setup = buy only on a breakout above the pivot with volume; Buy = breaking out now. "
        "Close this issue when you're done with it._",
    ])


def sample_alert():
    return [{
        "ticker": "TEST", "company": "Sample alert", "code": "setup", "label": "Setup", "previous": "watch",
        "headline": "This is a test alert to confirm notifications reach you. No action needed.",
        "price": 100.0, "pivot": 101.5, "pct_vs_pivot": -1.5,
        "trade": {"buy_limit": 106.58, "stop": 94.4, "risk_pct": 7.0, "stop_basis": "final-contraction low",
                  "t1": 115.7, "t2": 122.8},
        "rs_rank": 90, "eps_growth": 35.0, "sales_growth": 25.0, "vol_vs_avg": 10.0,
        "market": "Market in confirmed uptrend",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }]


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    test = sys.argv[1] == "--test"
    if test:
        alerts = sample_alert()
    else:
        path = Path(sys.argv[1])
        alerts = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if not alerts:
        print("No new Buy/Setup alerts.")
        return

    repo_owner = os.environ.get("GITHUB_REPOSITORY", "").partition("/")[0]
    gh("label", "create", LABEL, "--color", "2DA44E",
       "--description", "Buy/Setup alerts from the Minervini swing scanner", "--force", check=False)
    listed = gh("issue", "list", "--label", LABEL, "--state", "all", "--limit", "500", "--json", "title")
    existing = {i["title"] for i in json.loads(listed.stdout or "[]")}

    for a in alerts:
        # Test alerts carry the time so every test run opens a fresh issue
        day = a["generated_at"] if test else a["generated_at"][:10]
        title = f"{ICONS.get(a['code'], '')} {a['label']}: {a['ticker']} ({day})".strip()
        if title in existing:
            print(f"Already alerted: {title}")
            continue
        args = ["issue", "create", "--title", title, "--body", issue_body(a), "--label", LABEL]
        res = gh(*args, "--assignee", repo_owner, check=False) if repo_owner else None
        if res is None or res.returncode != 0:
            # Assigning can fail (e.g. owner is an org); still open the issue.
            res = gh(*args)
        print(f"Opened: {title} → {res.stdout.strip()}")


if __name__ == "__main__":
    main()
