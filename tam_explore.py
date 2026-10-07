#!/usr/bin/env python3
"""QBR / quarterly reporting — one account, one date range, then exit.

Uses tam_top.py for auth and GraphQL only. Escalation *counts* use IsEscalated
on Technical Support cases (not the live-board Escalation record / ParentId join).
"""
from __future__ import annotations

import argparse
import html
import json
import sys
import time
import webbrowser
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import tam_top as tt

HERE = Path(__file__).resolve().parent

FIELDS = """
    Id
    CaseNumber__c { value }
    Subject { value }
    Status { value }
    Priority { value }
    SBR_Group__c { value }
    IsEscalated { value }
    SBT__c { value }
    CreatedDate { value }
    LastModifiedDate { value }
    RedHatSupportAccount { Name { value } }
    RedHatSupportContact { Name { value } }
    Owner {
      ... on RedHatSupportUser  { Name { value } }
      ... on RedHatSupportGroup { Name { value } }
    }
"""


def parse_dt(value) -> Optional[datetime]:
    text = tt.v(value) if not isinstance(value, datetime) else value
    if isinstance(text, datetime):
        return text
    return tt.parse_dt(str(text) if text else "")


def quarter_bounds(label: str):
    year_s, q_s = label.upper().split("-Q")
    year, q = int(year_s), int(q_s)
    start_month = (q - 1) * 3 + 1
    start = datetime(year, start_month, 1, tzinfo=timezone.utc)
    if q == 4:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, start_month + 3, 1, tzinfo=timezone.utc)
    return start, end


def fetch_all_for_account(account_keyword: str, max_pages: int = 100, stop_before=None):
    cases, cursor, page = [], None, 0
    while page < max_pages:
        after = f'after: "{cursor}"' if cursor else ""
        query = f"""
query ExploreCases {{
  redhat_support_uiapi {{ query {{
    RedHatSupportCase(
      first: 100
      {after}
      orderBy: {{ CreatedDate: {{ order: DESC }} }}
      where: {{ and: [
        {{ RedHatSupportRecordType: {{ Name: {{ eq: "Technical Support" }} }} }}
        {{ AccessRestrictions__c: {{ eq: "None" }} }}
        {{ RedHatSupportAccount: {{ Name: {{ like: "%{account_keyword}%" }} }} }}
      ]}}
    ) {{
      pageInfo {{ hasNextPage endCursor }}
      edges {{ node {{ {FIELDS} }} }}
    }}
  }} }}
}}
"""
        result = tt.gql(query, operation_name="ExploreCases")
        data = result["data"]["redhat_support_uiapi"]["query"]["RedHatSupportCase"]
        page_cases = [e["node"] for e in (data.get("edges") or [])]
        page += 1
        if not page_cases:
            break
        if stop_before:
            kept, done = [], False
            for c in page_cases:
                dt = parse_dt(c.get("CreatedDate"))
                if dt and dt < stop_before:
                    done = True
                else:
                    kept.append(c)
            cases.extend(kept)
            if done or not (data.get("pageInfo") or {}).get("hasNextPage"):
                break
        else:
            cases.extend(page_cases)
            if not (data.get("pageInfo") or {}).get("hasNextPage"):
                break
        cursor = (data.get("pageInfo") or {}).get("endCursor")
        if not cursor:
            break
        time.sleep(0.5)
    return cases


def _is_escalated(c) -> bool:
    val = tt.v(c.get("IsEscalated"))
    return val is True or (isinstance(val, str) and val.strip().lower() == "true")


def _is_closed(c) -> bool:
    return str(tt.v(c.get("Status"))).strip().lower() in tt.CLOSED_STATUSES


def _is_worh(c) -> bool:
    status = str(tt.v(c.get("Status"))).lower()
    return "waiting on red hat" in status or status == "in progress"


def _is_woc(c) -> bool:
    return "waiting on customer" in str(tt.v(c.get("Status"))).lower()


def _sbt_mins(c) -> Optional[float]:
    val = tt.v(c.get("SBT__c"))
    if val in ("", None):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def analyse(cases, start: datetime, end: datetime) -> dict:
    filtered = []
    for c in cases:
        dt = parse_dt(c.get("CreatedDate"))
        if dt and start <= dt < end:
            filtered.append(c)

    open_cases = [c for c in filtered if not _is_closed(c)]
    closed = [c for c in filtered if _is_closed(c)]
    worh = [c for c in filtered if _is_worh(c)]
    by_status = Counter(str(tt.v(c.get("Status"))) or "Unknown" for c in filtered)
    by_sev = Counter(tt.sev_num(str(tt.v(c.get("Priority")))) for c in filtered)
    by_sbr = Counter(str(tt.v(c.get("SBR_Group__c"))) or "Unknown" for c in filtered)
    by_contact = Counter(
        str(tt.v((c.get("RedHatSupportContact") or {}).get("Name"))) or "Unknown"
        for c in filtered
    )

    monthly = Counter()
    monthly_sev = {}
    for c in filtered:
        dt = parse_dt(c.get("CreatedDate"))
        if not dt:
            continue
        key = dt.strftime("%Y-%m")
        monthly[key] += 1
        monthly_sev.setdefault(key, Counter())[tt.sev_num(str(tt.v(c.get("Priority"))))] += 1

    sbt_present = [c for c in filtered if _sbt_mins(c) is not None]
    breached = [c for c in sbt_present if (_sbt_mins(c) or 0) < 0]
    nep = []
    for c in breached:
        created = parse_dt(c.get("CreatedDate"))
        mins = abs(_sbt_mins(c) or 0)
        if created:
            age_mins = (datetime.now(timezone.utc) - created).total_seconds() / 60
            if mins > age_mins:
                nep.append(c)

    def age_key(c, field):
        dt = parse_dt(c.get(field))
        return dt or datetime.now(timezone.utc)

    oldest_open = sorted(open_cases, key=lambda c: age_key(c, "CreatedDate"))[:10]
    stale = sorted(open_cases, key=lambda c: age_key(c, "LastModifiedDate"))[:10]
    long_worh = sorted([c for c in open_cases if _is_worh(c)], key=lambda c: age_key(c, "CreatedDate"))[:10]
    long_woc = sorted([c for c in open_cases if _is_woc(c)], key=lambda c: age_key(c, "CreatedDate"))[:10]

    res_by_sev = {}
    for c in closed:
        created = parse_dt(c.get("CreatedDate"))
        modified = parse_dt(c.get("LastModifiedDate"))
        sev = tt.sev_num(str(tt.v(c.get("Priority"))))
        if created and modified and modified >= created:
            res_by_sev.setdefault(sev, []).append((modified - created).total_seconds() / 86400)

    sev1 = [c for c in filtered if tt.sev_num(str(tt.v(c.get("Priority")))) == 1]
    escalated = [c for c in filtered if _is_escalated(c)]

    return {
        "filtered": filtered,
        "start": start,
        "end": end,
        "open": open_cases,
        "closed": closed,
        "worh": worh,
        "by_status": by_status,
        "by_sev": by_sev,
        "by_sbr": by_sbr,
        "by_contact": by_contact,
        "monthly": monthly,
        "monthly_sev": monthly_sev,
        "sbt_present": sbt_present,
        "breached": breached,
        "nep": nep,
        "oldest_open": oldest_open,
        "stale": stale,
        "long_worh": long_worh,
        "long_woc": long_woc,
        "res_by_sev": res_by_sev,
        "sev1": sev1,
        "escalated": escalated,
    }


def _paint(text, colour, plain):
    if plain:
        return text
    return f"{colour}{text}{tt.RESET}"


def _case_line(c) -> str:
    num = tt.v(c.get("CaseNumber__c"))
    subj = html.unescape(str(tt.v(c.get("Subject")) or ""))[:70]
    status = tt.v(c.get("Status"))
    contact = tt.v((c.get("RedHatSupportContact") or {}).get("Name")) or "-"
    created = parse_dt(c.get("CreatedDate"))
    date = created.strftime("%Y-%m-%d") if created else "?"
    return f"  {date}  {num}  Sev{tt.sev_num(str(tt.v(c.get('Priority'))))}  {status}  {contact}  {subj}"


def report(stats: dict, account: str, plain: bool = False) -> str:
    p = lambda s, c=tt.CYAN: _paint(s, c, plain)
    lines = []
    start, end = stats["start"], stats["end"]
    lines.append(p(f"QBR  {account}  {start.date()} → {end.date()}", tt.BOLD + tt.CYAN))
    lines.append("")
    lines.append(p("Summary", tt.BOLD))
    lines.append(f"  Total logged: {len(stats['filtered'])}")
    lines.append(f"  Still open:   {len(stats['open'])}")
    lines.append(f"  Closed:       {len(stats['closed'])}")
    lines.append(f"  Currently WoRH/InPrg: {len(stats['worh'])}")
    lines.append("")

    lines.append(p("Status", tt.BOLD))
    for k, n in stats["by_status"].most_common():
        lines.append(f"  {n:4d}  {k}")
    lines.append("")
    lines.append(p("Severity", tt.BOLD))
    for sev in (1, 2, 3, 4, 0):
        n = stats["by_sev"].get(sev, 0)
        if n:
            lines.append(f"  {n:4d}  Sev{sev or '?'}")
    lines.append("")
    lines.append(p("SBR", tt.BOLD))
    for k, n in stats["by_sbr"].most_common(15):
        lines.append(f"  {n:4d}  {k}")
    lines.append("")

    lines.append(p("Sev1", tt.BOLD))
    if not stats["sev1"]:
        lines.append("  None this period.")
    else:
        for c in stats["sev1"]:
            lines.append(_case_line(c))
        lines.append("  by contact:")
        for k, n in Counter(
            str(tt.v((c.get("RedHatSupportContact") or {}).get("Name"))) or "Unknown"
            for c in stats["sev1"]
        ).most_common():
            lines.append(f"    {n:4d}  {k}")
        lines.append("  by SBR:")
        for k, n in Counter(
            str(tt.v(c.get("SBR_Group__c"))) or "Unknown" for c in stats["sev1"]
        ).most_common():
            lines.append(f"    {n:4d}  {k}")
    lines.append("")

    lines.append(p("Escalations", tt.BOLD))
    if not stats["escalated"]:
        lines.append("  None logged this period.")
    else:
        for c in stats["escalated"]:
            lines.append(_case_line(c))
        lines.append("  by contact:")
        for k, n in Counter(
            str(tt.v((c.get("RedHatSupportContact") or {}).get("Name"))) or "Unknown"
            for c in stats["escalated"]
        ).most_common():
            lines.append(f"    {n:4d}  {k}")
        lines.append("  by SBR:")
        for k, n in Counter(
            str(tt.v(c.get("SBR_Group__c"))) or "Unknown" for c in stats["escalated"]
        ).most_common():
            lines.append(f"    {n:4d}  {k}")
    lines.append("")

    lines.append(p("Who raised cases (top 10)", tt.BOLD))
    for k, n in stats["by_contact"].most_common(10):
        lines.append(f"  {n:4d}  {k}")
    lines.append("")

    lines.append(p("Monthly volume", tt.BOLD))
    for month in sorted(stats["monthly"]):
        bar = "#" * stats["monthly"][month]
        sev_bits = "  ".join(
            f"S{s}:{stats['monthly_sev'][month].get(s, 0)}" for s in (1, 2, 3, 4) if stats["monthly_sev"][month].get(s)
        )
        lines.append(f"  {month}  {stats['monthly'][month]:4d}  {bar}  {sev_bits}")
    lines.append("")

    lines.append(p("Approx resolution time (closed cases, LastModifiedDate proxy)", tt.BOLD))
    for sev in (1, 2, 3, 4):
        days = stats["res_by_sev"].get(sev)
        if not days:
            continue
        avg = sum(days) / len(days)
        lines.append(f"  Sev{sev}: {avg:.1f}d average over {len(days)} closed")
    lines.append("")

    def dump(title, rows):
        lines.append(p(title, tt.BOLD))
        if not rows:
            lines.append("  (none)")
        for c in rows:
            lines.append(_case_line(c))
        lines.append("")

    dump("Oldest open", stats["oldest_open"])
    dump("Most stale (LastModifiedDate — reporting proxy only)", stats["stale"])
    dump("Longest Waiting on Red Hat", stats["long_worh"])
    dump("Longest Waiting on Customer", stats["long_woc"])

    lines.append(p("SBT / SLA", tt.BOLD))
    lines.append(f"  With SBT data: {len(stats['sbt_present'])}")
    lines.append(f"  Breached:      {len(stats['breached'])}")
    lines.append(f"  NEP artefact:  {len(stats['nep'])}")
    lines.append("")
    lines.append("Notes: close dates are LastModifiedDate on closed cases, not an audit trail.")
    lines.append("SBT is intermittently unavailable. Pre-migration NEP values can exceed case age.")
    return "\n".join(lines) + "\n"


def report_html(stats: dict, account: str, path: Path) -> None:
    text = report(stats, account, plain=True)
    escaped = html.escape(text)
    path.write_text(
        "<!doctype html><meta charset=utf-8>"
        "<title>QBR {acct}</title>"
        "<body style='background:#0b0f14;color:#c5d4e3;font-family:ui-monospace,monospace;"
        "white-space:pre;padding:24px'>{body}</body>\n".format(acct=html.escape(account), body=escaped)
    )


def send_gchat_summary(stats: dict, account: str) -> None:
    cfg = tt.load_config()
    url = cfg.get("webhook")
    if not url:
        print("No webhook in tam_config.json", file=sys.stderr)
        return
    sev_line = "  ".join(f"Sev{s}={stats['by_sev'].get(s, 0)}" for s in (1, 2, 3, 4))
    top_sbr = ", ".join(f"{k} ({n})" for k, n in stats["by_sbr"].most_common(5))
    payload = {
        "text": (
            f"*{account} QBR* {stats['start'].date()} → {stats['end'].date()}\n"
            f"Total {len(stats['filtered'])}  open {len(stats['open'])}  closed {len(stats['closed'])}\n"
            f"{sev_line}\n"
            f"Top SBR: {top_sbr or '-'}\n"
            f"Sev1: {len(stats['sev1'])}  Escalations: {len(stats['escalated'])}  "
            f"Breached in period: {len(stats['breached'])}"
        )
    }
    tt.webhook_send(url, payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="TAM QBR / quarterly reporting")
    parser.add_argument("--account", required=True, help="Salesforce account search term (not the short label)")
    parser.add_argument("--quarter", help="e.g. 2026-Q3")
    parser.add_argument("--from", dest="date_from", help="YYYY-MM-DD inclusive")
    parser.add_argument("--to", dest="date_to", help="YYYY-MM-DD exclusive")
    parser.add_argument("--plain", action="store_true", help="No ANSI — safe to paste")
    parser.add_argument("--html", action="store_true", help="Write a dark HTML report and open it")
    parser.add_argument("--gchat", action="store_true", help="Post a summary to the saved webhook")
    args = parser.parse_args()

    if args.quarter:
        start, end = quarter_bounds(args.quarter)
    else:
        if not (args.date_from and args.date_to):
            parser.error("provide --quarter or both --from and --to")
        start = datetime.fromisoformat(args.date_from).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(args.date_to).replace(tzinfo=timezone.utc)

    print(f"Fetching {args.account} history…", file=sys.stderr)
    cases = fetch_all_for_account(args.account, stop_before=start)
    stats = analyse(cases, start, end)
    print(report(stats, args.account, plain=args.plain))
    if args.html:
        fname = HERE / f"report-{args.account.replace(' ', '_')}-{start.date()}-{end.date()}.html"
        report_html(stats, args.account, fname)
        webbrowser.open(fname.as_uri())
        print(f"Wrote {fname}", file=sys.stderr)
    if args.gchat:
        send_gchat_summary(stats, args.account)


if __name__ == "__main__":
    main()
