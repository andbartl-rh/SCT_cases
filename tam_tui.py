#!/usr/bin/env python3
"""TAM case dashboard — Textual TUI.

Click a row (or press Enter) to open the case in both Customer Portal and SFDC.
p opens Portal only, s opens Salesforce only. r refreshes. e toggles other-people
escalations. l (or click the status bar) shows refresh-latency history. j opens
Jira tracking. Digit keys / b toggle backup-coverage accounts. q quits.
"""
from __future__ import annotations

import sys
import time
import webbrowser
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

try:
    from rich.text import Text
    from textual import work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical
    from textual.message import Message
    from textual.screen import ModalScreen
    from textual.widgets import DataTable, Footer, Static
except ImportError:
    sys.exit(
        "tam_tui.py requires the 'textual' package.\n"
        "macOS:  python3 -m pip install --user 'textual>=0.80,<2'\n"
        "RHEL:   sudo dnf install python3-textual"
    )

import tam_top as tt
from tam_top import (
    LAST_FETCH,
    LAST_WOC,
    SSO_USERNAME,
    STATUS_RICH,
    Case,
    ansi_to_rich,
    apply_saved_webhook,
    build_parser,
    debug_uniques,
    detect_and_alert,
    fmt_last_reply,
    fmt_owner,
    fmt_sbt,
    fmt_stat,
    grouped_cases,
    is_breached,
    load_backup_accounts,
    load_tui_board,
    name_is_mine,
    now_stamp,
)


SEV_RICH = {"1": "bold red", "2": "bold yellow", "3": "cyan", "4": "dim"}
LINE_PRIMARY = "#3a3a3a"
LINE_BACKUP = "#5a3a10"
DETAIL_BLUE = "#66ccff"
_JIRA_STATUS_ORDER = ["New", "Review", "Planning", "In Progress", "Release Pending"]

BACKUP_ACCOUNT_LABELS = sorted(set(load_backup_accounts().values()))
_BACKUP_KEYS = "123456789"
_BACKUP_BINDINGS = [
    Binding(_BACKUP_KEYS[i], f"toggle_backup_account('{label}')", label)
    for i, label in enumerate(BACKUP_ACCOUNT_LABELS)
    if i < len(_BACKUP_KEYS)
]


def _fillers(detail: Text, is_backup: bool = False) -> List[Text]:
    line = LINE_BACKUP if is_backup else LINE_PRIMARY
    return [
        Text("─" * 4, style=line),
        Text("─" * 6, style=line),
        Text("─" * 8, style=line),
        Text("─" * 18, style=line),
        Text("─" * 10, style=line),
        Text("─" * 22, style=line),
        detail,
    ]


def _relative_style(values: List[float], current: Optional[float]) -> str:
    if current is None:
        return "dim"
    return tt.latency_relative_colour(values, current)


def _hour_of(ts: str) -> Optional[int]:
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if parsed.tzinfo:
            parsed = parsed.astimezone()
        return parsed.hour
    except Exception:
        return None


class StatusBar(Static):
    class Clicked(Message):
        pass

    def on_click(self) -> None:
        self.post_message(self.Clicked())


class LatencyScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("q", "dismiss", "Close"),
    ]
    CSS = """
    LatencyScreen {
        align: center middle;
    }
    #lat-wrap {
        width: 96%;
        height: 90%;
        background: #0b0f14;
        border: solid #1a4a73;
    }
    #lat-title {
        height: 1;
        background: #15202b;
        color: #7ec8e3;
        padding: 0 1;
        text-style: bold;
    }
    #lat-chart {
        height: auto;
        max-height: 26;
        padding: 0 1;
    }
    """

    def __init__(self, history: List[dict]):
        super().__init__()
        self.history = list(history or [])

    def compose(self) -> ComposeResult:
        with Vertical(id="lat-wrap"):
            yield Static("REFRESH LATENCY  —  hour-of-day averages + recent samples", id="lat-title")
            yield Static("", id="lat-chart")
            yield DataTable(id="lat-table", cursor_type="row", zebra_stripes=False)
            yield Footer()

    def on_mount(self) -> None:
        self._draw_chart()
        table = self.query_one("#lat-table", DataTable)
        table.add_columns("WHEN", "ELAPSED", "PATH", "CASES", "API", "FAILED")
        elapsed_vals = [s.get("elapsed") for s in self.history if s.get("elapsed") is not None]
        path_vals = [s.get("path_ms") for s in self.history if s.get("path_ms") is not None]
        for sample in reversed(self.history[-40:]):
            elapsed = sample.get("elapsed")
            path_ms = sample.get("path_ms")
            table.add_row(
                Text(str(sample.get("ts") or "")),
                Text(f"{elapsed:.1f}s" if elapsed is not None else "?", style=_relative_style(elapsed_vals, elapsed)),
                Text(f"{path_ms:.0f}ms" if path_ms is not None else "·", style=_relative_style(path_vals, path_ms)),
                Text(str(sample.get("cases") or 0)),
                Text(str(sample.get("api_calls") or 0)),
                Text(str(sample.get("failed") or 0)),
            )

    def _draw_chart(self) -> None:
        buckets: Dict[int, List[float]] = {h: [] for h in range(24)}
        path_buckets: Dict[int, List[float]] = {h: [] for h in range(24)}
        for sample in self.history:
            hour = _hour_of(sample.get("ts") or "")
            if hour is None:
                continue
            if sample.get("elapsed") is not None:
                buckets[hour].append(float(sample["elapsed"]))
            if sample.get("path_ms") is not None:
                path_buckets[hour].append(float(sample["path_ms"]))
        avgs = {h: (sum(v) / len(v) if v else None) for h, v in buckets.items()}
        peak = max((v for v in avgs.values() if v is not None), default=1.0) or 1.0
        all_elapsed = [v for v in avgs.values() if v is not None]
        all_path = [sum(v) / len(v) for v in path_buckets.values() if v]
        lines = Text()
        for hour in range(24):
            avg = avgs[hour]
            if avg is None:
                lines.append(f"{hour:02d}:00  ·  no data\n", style="dim")
                continue
            width = max(1, int(round((avg / peak) * 40)))
            bar = "█" * width
            style = _relative_style(all_elapsed, avg)
            path_avg = (sum(path_buckets[hour]) / len(path_buckets[hour])) if path_buckets[hour] else None
            path_bit = f"  · path {path_avg:.0f}ms" if path_avg is not None else ""
            path_style = _relative_style(all_path, path_avg) if path_avg is not None else "dim"
            lines.append(f"{hour:02d}:00  {bar}  {avg:.1f}s  ({len(buckets[hour])})", style=style)
            if path_bit:
                lines.append(path_bit, style=path_style)
            lines.append("\n")
        self.query_one("#lat-chart", Static).update(lines)


def _jira_key_prefix(key: str) -> str:
    return (key or "").split("-", 1)[0] or key


def _jira_status_rank(name: str) -> int:
    n = (name or "").strip()
    if n.lower() in {"closed", "done", "resolved", "cancelled"}:
        return 99
    try:
        return _JIRA_STATUS_ORDER.index(n)
    except ValueError:
        return len(_JIRA_STATUS_ORDER)


class JiraScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("q", "dismiss", "Close"),
        Binding("a", "toggle_all", "All/changed"),
        Binding("c", "toggle_closed", "Closed"),
        Binding("enter", "open_jira", "Open Jira", show=False),
        Binding("p", "open_portal", "Portal"),
        Binding("s", "open_sfdc", "SFDC"),
    ]
    CSS = """
    JiraScreen {
        align: center middle;
    }
    #jira-wrap {
        width: 96%;
        height: 90%;
        background: #0b0f14;
        border: solid #5b3d8c;
    }
    #jira-title {
        height: 1;
        background: #2a1a44;
        color: #c9b3ff;
        padding: 0 1;
        text-style: bold;
    }
    """

    def __init__(self, data: Dict[str, dict], show_all: bool = False, show_closed: bool = False):
        super().__init__()
        self._jira_data = data or {}
        self._show_all = show_all
        self._show_closed = show_closed
        self._rows: Dict[str, dict] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="jira-wrap"):
            yield Static("", id="jira-title")
            yield DataTable(id="jira-table", cursor_type="row", zebra_stripes=False)
            yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#jira-table", DataTable)
        table.add_columns("JIRA", "STATE", "AGE", "UPDATED", "CASE#", "SUBJECT")
        self._rebuild()

    def action_toggle_all(self) -> None:
        self._show_all = not self._show_all
        self._rebuild()

    def action_toggle_closed(self) -> None:
        self._show_closed = not self._show_closed
        self._rebuild()

    def _keep(self, info: dict) -> bool:
        issue = info.get("issue")
        if not self._show_closed and tt.jira_is_closed(issue):
            return False
        if self._show_all:
            return True
        return bool(info.get("needs_addressing"))

    def _rebuild(self) -> None:
        table = self.query_one("#jira-table", DataTable)
        table.clear()
        self._rows = {}
        mode = "all" if self._show_all else "changed"
        closed = "+CLOSED" if self._show_closed else "open"
        self.query_one("#jira-title", Static).update(
            f"JIRA TRACKING  —  {mode}  {closed}  (a=all/changed  c=closed  enter=jira  p=portal  s=sfdc)"
        )
        if not self._jira_data:
            hint = "No Jira data yet."
            if not tt.jira_token_present():
                hint = "No ~/.rh_atlassian_token — create an Atlassian API token and save it there."
            table.add_row(Text(hint, style="dim"), Text(""), Text(""), Text(""), Text(""), Text(""))
            return

        from_cases = {k: v for k, v in self._jira_data.items() if v.get("case_id")}
        watching = {k: v for k, v in self._jira_data.items() if not v.get("case_id")}

        table.add_row(
            Text("══ FROM CASES ══", style="bold #c9b3ff"),
            Text(""), Text(""), Text(""), Text(""), Text(""),
            key="sec-cases",
        )
        by_account: Dict[str, List[Tuple[str, dict]]] = defaultdict(list)
        acct_all: Dict[str, List[Case]] = defaultdict(list)
        for key, info in from_cases.items():
            case = info.get("case")
            label = case.account_label if case else "?"
            acct_all[label].append(case) if case else None
            if self._keep(info):
                by_account[label].append((key, info))
        acct_order = sorted(acct_all, key=lambda a: tt.account_sort_key(acct_all.get(a, [])))
        for label in acct_order:
            rows = by_account.get(label) or []
            if not rows and not self._show_all:
                continue
            n_jiras = len([k for k, i in from_cases.items() if (i.get("case") and i["case"].account_label == label)])
            n_cases = len({i["case"].id for i in from_cases.values() if i.get("case") and i["case"].account_label == label})
            table.add_row(
                Text(f"── {label}", style="bold cyan"),
                Text(f"({n_jiras} jiras / {n_cases} cases)", style="dim"),
                Text(""), Text(""), Text(""), Text(""),
                key=f"h-{label}",
            )
            rows.sort(key=lambda kv: kv[1].get("last_activity") or "", reverse=True)
            for key, info in rows:
                self._add_jira_row(table, key, info)

        table.add_row(
            Text("══ WATCHING — no case ══", style="bold #c9b3ff"),
            Text(""), Text(""), Text(""), Text(""), Text(""),
            key="sec-watch",
        )
        by_prefix: Dict[str, List[Tuple[str, dict]]] = defaultdict(list)
        for key, info in watching.items():
            if self._keep(info):
                by_prefix[_jira_key_prefix(key)].append((key, info))
        for prefix in sorted(by_prefix):
            table.add_row(
                Text(f"── {prefix}", style="bold #88aaff"),
                Text(""), Text(""), Text(""), Text(""), Text(""),
                key=f"p-{prefix}",
            )
            rows = by_prefix[prefix]
            rows.sort(key=lambda kv: kv[1].get("last_activity") or "", reverse=True)
            rows.sort(
                key=lambda kv: _jira_status_rank(
                    (((kv[1].get("issue") or {}).get("fields") or {}).get("status") or {}).get("name", "")
                )
            )
            for key, info in rows:
                self._add_jira_row(table, key, info)

    def _add_jira_row(self, table: DataTable, key: str, info: dict) -> None:
        issue = info.get("issue") or {}
        fields = issue.get("fields") or {}
        status = (fields.get("status") or {}).get("name") or "?"
        summary = fields.get("summary") or info.get("change_summary") or ""
        updated = info.get("last_activity") or fields.get("updated") or ""
        age = updated[11:16] if len(updated) >= 16 else updated
        case = info.get("case")
        style = "bold #e0c3ff" if info.get("needs_addressing") else ""
        row_key = f"j-{key}"
        self._rows[row_key] = info
        table.add_row(
            Text(key, style=style or "cyan"),
            Text(status, style=style),
            Text(age, style=style),
            Text(updated[:19].replace("T", " ") if updated else "", style=style),
            Text(case.number if case else "—", style=style),
            Text(summary[:70], style=style),
            key=row_key,
        )

    def _current(self) -> Optional[dict]:
        table = self.query_one("#jira-table", DataTable)
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return None
        value = key.value if hasattr(key, "value") else key
        return self._rows.get(str(value))

    def action_open_jira(self) -> None:
        info = self._current()
        if info:
            webbrowser.open(tt.jira_browse_url(info.get("key") or ""))

    def action_open_portal(self) -> None:
        info = self._current()
        case = info.get("case") if info else None
        if case:
            webbrowser.open(case.portal_url())

    def action_open_sfdc(self) -> None:
        info = self._current()
        case = info.get("case") if info else None
        if case:
            webbrowser.open(case.sfdc_url())

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        value = event.row_key.value if hasattr(event.row_key, "value") else event.row_key
        info = self._rows.get(str(value))
        if info:
            webbrowser.open(tt.jira_browse_url(info.get("key") or ""))


class TamApp(App):
    CSS = """
    Screen {
        background: #0b0f14;
    }
    #banner {
        height: 1;
        background: #15202b;
        color: #7ec8e3;
        padding: 0 1;
        text-style: bold;
    }
    #stats {
        height: 1;
        background: #1a4a73;
        color: #c5d4e3;
        padding: 0 1;
    }
    DataTable {
        height: 1fr;
        background: #0b0f14;
    }
    DataTable > .datatable--header {
        background: #15202b;
        color: #8b9bb4;
        text-style: none;
    }
    DataTable > .datatable--cursor {
        background: #1e3a5f;
    }
    Footer {
        background: #1a4a73;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("p", "portal", "Portal"),
        Binding("enter", "open_both", "Portal+SFDC", show=False),
        Binding("s", "sfdc", "SFDC"),
        Binding("e", "toggle_escalations", "Escalations"),
        Binding("l", "show_latency", "Latency"),
        Binding("j", "show_jira", "Jira"),
        *_BACKUP_BINDINGS,
        Binding("b", "toggle_backup_all", "Backup: all"),
    ]

    def __init__(self, args):
        super().__init__()
        self.args = args
        self.cases: Dict[str, Case] = {}
        self.previous: Dict[str, Case] = {}
        self.webhook_url, self.webhook_type = apply_saved_webhook(args)
        self.visible_backup: Set[str] = set()
        self.show_other_escalations = False
        self._board: Optional[dict] = None
        self._escalated_ids: Set[str] = set()
        self._parent_cases: List[Case] = []
        self._row_epoch = 0
        self._row_seq = 0
        self._latency_history = tt.load_latency_history()
        self._jira_data: Dict[str, dict] = {}
        self._jira_alert_cache = tt.load_jira_alert_cache()
        self._jira_alert_seeded = bool(self._jira_alert_cache)
        self._case_jiras: Dict[str, List[str]] = {}
        self._case_lookup: Dict[str, Case] = {}
        self._last_comment: Dict[str, Tuple[str, str]] = {}
        self._primary_ids: Set[str] = set()
        self._backup_ids: Set[str] = set()
        self._sd_cache = tt.load_id_set_cache(tt.SD_GAMING_CACHE_PATH)
        self._owner_cache = tt.load_id_set_cache(tt.OWNER_ALERT_CACHE_PATH)
        self._integrity_seeded = bool(self._sd_cache or self._owner_cache)
        self.jira_show_all = False
        self.jira_show_closed = False
        self._jira_kicked = False

    def compose(self) -> ComposeResult:
        yield Static("", id="banner")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=False)
        yield StatusBar("", id="stats")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_columns(
            "CASE#", "SEV", "STAT", "SBT/NEP", "OWNER", "LAST REPLY", "CONTACT", "SUBJECT"
        )
        self.refresh_cases()
        interval = max(15, int(self.args.interval or 300))
        self.set_interval(interval, self.refresh_cases)
        self.set_interval(900, self._poll_jira)
        self.set_interval(1800, self._poll_case_integrity)

    def refresh_cases(self) -> None:
        self.query_one("#stats", StatusBar).update("Refreshing…")
        self._do_load()

    @work(thread=True, exclusive=True, exit_on_error=False)
    def _do_load(self) -> None:
        t0 = time.time()
        path_ms = tt.measure_path_latency()
        try:
            board = load_tui_board(
                self.args,
                prev_esc_ids=self._escalated_ids or None,
                prev_parents=self._parent_cases or None,
            )
            err = None
        except Exception as exc:
            board = None
            err = str(exc)
        elapsed = time.time() - t0
        sample = None
        if board is not None:
            n_cases = (
                len(board.get("cases") or [])
                + len(board.get("backup") or [])
                + len(board.get("parent_cases") or [])
            )
            n_api = LAST_FETCH.get("calls") or 0
            n_failed = len(board.get("failed") or [])
            tt.log_latency_sample(
                elapsed, n_cases=n_cases, n_api=n_api, n_failed=n_failed, path_ms=path_ms
            )
            sample = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "elapsed": round(elapsed, 2),
                "cases": n_cases,
                "api_calls": n_api,
                "failed": n_failed,
                "path_ms": path_ms,
            }
            LAST_FETCH["seconds"] = round(elapsed, 1)
            LAST_FETCH["updated"] = datetime.now().strftime("%H:%M:%S")
        self.call_from_thread(self._populate, board, err, True, sample)

    def action_toggle_backup_account(self, label: str) -> None:
        if label in self.visible_backup:
            self.visible_backup.discard(label)
        else:
            self.visible_backup.add(label)
        if self._board:
            self._populate(self._board, None, send_alerts=False)

    def action_toggle_backup_all(self) -> None:
        self.visible_backup = set() if self.visible_backup else set(BACKUP_ACCOUNT_LABELS)
        if self._board:
            self._populate(self._board, None, send_alerts=False)

    def action_toggle_escalations(self) -> None:
        self.show_other_escalations = not self.show_other_escalations
        if self._board:
            self._populate(self._board, None, send_alerts=False)

    def action_show_latency(self) -> None:
        self.push_screen(LatencyScreen(list(self._latency_history)))

    def action_show_jira(self) -> None:
        self.push_screen(
            JiraScreen(self._jira_data, show_all=self.jira_show_all, show_closed=self.jira_show_closed)
        )

    def on_status_bar_clicked(self, message: StatusBar.Clicked) -> None:
        self.action_show_latency()

    def _populate(
        self,
        board: Optional[dict],
        err: Optional[str],
        send_alerts: bool = True,
        sample: Optional[dict] = None,
    ) -> None:
        if sample:
            self._latency_history.append(sample)
            self._latency_history = self._latency_history[-5000:]
        if err:
            self.query_one("#stats", StatusBar).update(f"Error: {err}")
            return
        if not board:
            return
        self._board = board
        self._escalated_ids = set(board.get("esc_ids") or [])
        self._parent_cases = list(board.get("parent_cases") or [])
        self._last_comment = dict(board.get("last_comment") or {})
        self._case_jiras = dict(board.get("case_jiras") or {})

        cases: List[Case] = list(board["cases"])
        backup: List[Case] = list(board["backup"])
        for case in board.get("mine_parents") or []:
            if case.is_backup:
                backup.append(case)
            else:
                cases.append(case)
        if self.show_other_escalations:
            have = {c.id for c in cases} | {c.id for c in backup}
            for case in board.get("other_parents") or []:
                if case.id in have:
                    continue
                if case.is_backup:
                    backup.append(case)
                else:
                    cases.append(case)
                have.add(case.id)

        def _dedupe(rows: List[Case]) -> List[Case]:
            seen: Set[str] = set()
            out: List[Case] = []
            for case in rows:
                key = case.id or case.number
                if not key or key in seen:
                    continue
                seen.add(key)
                out.append(case)
            return out

        cases = _dedupe(cases)
        backup = _dedupe(backup)
        lookup = {c.id: c for c in cases + backup if c.id}
        self._case_lookup = lookup
        commented = set(board.get("commented") or [])
        authors = board.get("all_authors") or {}
        contacts = tt.load_contacts()
        if self.args.mine:
            self._primary_ids = {
                c.id for c in cases if tt.is_mine_visible(c, commented, authors, contacts)
            }
            self._backup_ids = {
                c.id for c in backup if tt.is_mine_visible(c, commented, authors, contacts)
            }
        else:
            self._primary_ids = {c.id for c in cases if c.id}
            self._backup_ids = {c.id for c in backup if c.id}

        backup_ids = {c.id for c in backup}
        if send_alerts:
            self.previous = detect_and_alert(
                self.previous,
                cases + backup,
                self.webhook_url,
                self.webhook_type,
                backup_ids=backup_ids,
            )
        visible_backup = [c for c in backup if c.account_label in self.visible_backup]
        self._fill_table(cases, visible_backup)
        if not self._jira_kicked:
            self._jira_kicked = True
            self._poll_jira()
            self._poll_case_integrity()

        shown = cases + visible_backup
        breach = sum(1 for c in shown if is_breached(c))
        banner = Text()
        banner.append("TAM CASES  ", style="bold cyan")
        banner.append(f"{SSO_USERNAME}  {now_stamp()}  ", style="cyan")
        banner.append(f"{len(shown)} open  ", style="white")
        if breach:
            banner.append(f"{breach} BREACH", style="bold red")
        else:
            banner.append("0 BREACH", style="dim")
        if self.show_other_escalations:
            banner.append("  e=all ▲", style="bold red")
        self.query_one("#banner", Static).update(banner)

        if visible_backup:
            backup_hint = f"  backup shown: {', '.join(sorted({c.account_label for c in visible_backup}))}"
        elif board["backup"]:
            hint_keys = "/".join(
                _BACKUP_KEYS[i] for i in range(len(BACKUP_ACCOUNT_LABELS))
            )
            backup_hint = f"  backup hidden ({hint_keys}=one, b=all)"
        else:
            backup_hint = ""
        failed = LAST_FETCH.get("failed") or []
        fail_hint = f"  failed: {', '.join(failed)}" if failed else ""
        elapsed = LAST_FETCH.get("seconds")
        today = datetime.now().date()
        todays = [
            s.get("elapsed")
            for s in self._latency_history
            if s.get("elapsed") is not None and tt.same_local_day(s.get("ts") or "", today)
        ]
        if elapsed is not None and len(todays) >= 2 and max(todays) > min(todays):
            elapsed_colour = tt.latency_gradient_colour(
                (float(elapsed) - min(todays)) / (max(todays) - min(todays))
            )
        else:
            elapsed_colour = "#aaaaaa"
        arrow, arrow_colour = tt.latency_trend_arrow(
            float(elapsed or 0),
            [s.get("elapsed") for s in self._latency_history[-6:-1] if s.get("elapsed") is not None],
        )
        elapsed_bit = (
            f"[{elapsed_colour}]{elapsed}s[/{elapsed_colour}]"
            if elapsed is not None
            else "?s"
        )
        if arrow:
            elapsed_bit += f" [{arrow_colour}]{arrow}[/{arrow_colour}]"
        self.query_one("#stats", StatusBar).update(
            f"{LAST_FETCH.get('records', len(shown))} records  "
            f"{LAST_FETCH.get('calls', '?')} API calls  "
            f"{elapsed_bit}  "
            f"updated {LAST_FETCH.get('updated', '')}  "
            f"auto-refresh {self.args.interval}s"
            f"{backup_hint}{fail_hint}  "
            "● replied  ◆ TAM contact  ✱ other TAM (co-TAM, see CO_TAMS)  ○ other  ▲ escalated  ▪ Jira  "
            "red CASE# = SBT breached (shown even if WoC)  l=latency  j=jira"
        )

    def _row_key(self, base: str) -> str:
        self._row_seq += 1
        return f"{self._row_epoch}-{self._row_seq}-{base}"

    def _fill_table(self, cases: List[Case], backup: List[Case]) -> None:
        table = self.query_one("#table", DataTable)
        table.clear()
        self._row_epoch += 1
        self._row_seq = 0
        self.cases = {}
        for label, group in grouped_cases(cases):
            self._render_group(table, label, group, is_backup=False)
        if backup:
            by_acct: Dict[str, List[Case]] = {}
            for case in backup:
                by_acct.setdefault(case.account_label, []).append(case)
            banner = Text()
            banner.append("▼ BACKUP", style="bold #ffaa33")
            detail = Text()
            detail.append("── ", style=LINE_BACKUP)
            detail.append(
                f"({', '.join(sorted(by_acct))}  {len(backup)} cases)",
                style=DETAIL_BLUE,
            )
            table.add_row(banner, *_fillers(detail, True), key=self._row_key("backup-banner"))
            for label in sorted(by_acct, key=lambda a: tt.account_sort_key(by_acct[a])):
                self._render_group(table, label, by_acct[label], is_backup=True)

    def _render_group(
        self, table: DataTable, label: str, group: List[Case], is_backup: bool
    ) -> None:
        n_breach = sum(1 for c in group if is_breached(c))
        n_woc = (LAST_WOC.get(label) or {}).get("count", 0)
        line = LINE_BACKUP if is_backup else LINE_PRIMARY
        accent = "bold #ffaa33" if is_backup else f"bold {tt.account_color(label)}"
        hdr = Text()
        hdr.append("─ ", style=line)
        hdr.append(label, style=accent)

        detail = Text()
        detail.append("── ", style=line)
        full_name = (group[0].account if group else (LAST_WOC.get(label) or {}).get("name") or label)
        detail.append(f"{full_name} ", style=f"bold {DETAIL_BLUE}")
        if is_backup:
            detail.append("(backup) ", style="#cc8833")
        detail.append(f"{len(group)} cases", style=DETAIL_BLUE)
        if n_breach:
            detail.append(f"  {n_breach} BREACH", style="bold red")
        if n_woc:
            detail.append(f"  ({n_woc} WoC hidden)", style="dim cyan")
        kind = "backup" if is_backup else "acct"
        table.add_row(hdr, *_fillers(detail, is_backup), key=self._row_key(f"h-{kind}-{label}"))
        if not group:
            table.add_row(
                Text("─", style=line),
                *_fillers(Text("  (all open cases are waiting on customer)", style="dim"), is_backup),
                key=self._row_key(f"empty-{kind}-{label}"),
            )
            return
        for case in group:
            key = self._row_key(case.id or case.number or "case")
            self.cases[key] = case
            table.add_row(*self._row(case), key=key)

    def _row(self, case: Case):
        breached = is_breached(case)
        if case.mine:
            mark = Text("● ", style="bold green")
        elif case.is_contact:
            mark = Text("◆ ", style="bold yellow")
        elif case.co_tam:
            mark = Text("✱ ", style="bold cyan")
        else:
            mark = Text("○ ", style="dim")
        contact = Text.assemble(mark, ((case.contact or "-")[:28], "white"))
        if case.unclaimed and not breached:
            contact.stylize("dim")

        sbt_txt, sbt_ansi = fmt_sbt(case.sbt)
        sbt_cell = Text(sbt_txt, style=ansi_to_rich(sbt_ansi))
        reply_txt, reply_ansi = fmt_last_reply(case.last_reply)
        reply_cell = Text(reply_txt, style=ansi_to_rich(reply_ansi))

        row_dim = "dim" if case.unclaimed and not breached else None
        owner_style = "bold green" if name_is_mine(case.owner) else (row_dim or "white")
        owner = Text(fmt_owner(case.owner, 22), style=owner_style)

        if breached:
            caseno = Text(case.number, style="bold white on dark_red")
            sev = Text(f"Sev{case.severity}", style="bold red")
            stat = Text(fmt_stat(case.status), style=STATUS_RICH.get(case.status, "bold red"))
            subj_style = "bold"
        elif case.escalated:
            caseno = Text(case.number, style="bold red")
            sev = Text(f"Sev{case.severity}", style=row_dim or SEV_RICH.get(case.severity, "cyan"))
            stat = Text(fmt_stat(case.status), style=row_dim or STATUS_RICH.get(case.status, ""))
            subj_style = "bold red"
        else:
            caseno = Text(case.number, style=row_dim or "cyan")
            sev = Text(f"Sev{case.severity}", style=row_dim or SEV_RICH.get(case.severity, "cyan"))
            stat = Text(fmt_stat(case.status), style=row_dim or STATUS_RICH.get(case.status, ""))
            subj_style = row_dim or ""

        subject = Text()
        if case.escalated:
            subject.append("▲ ", style="bold red")
        if case.jira_keys:
            subject.append("▪ ", style="#b388ff")
        subject.append(case.subject or "", style="bold red" if case.escalated else subj_style)
        return caseno, sev, stat, sbt_cell, owner, reply_cell, contact, subject

    def _current_case(self) -> Optional[Case]:
        table = self.query_one("#table", DataTable)
        if table.row_count == 0:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return None
        value = key.value if hasattr(key, "value") else key
        text = str(value)
        if text.startswith("__"):
            return None
        return self.cases.get(text)

    def action_refresh(self) -> None:
        self.refresh_cases()

    def action_portal(self) -> None:
        case = self._current_case()
        if case:
            webbrowser.open(case.portal_url())

    def action_sfdc(self) -> None:
        case = self._current_case()
        if case:
            webbrowser.open(case.sfdc_url())

    def action_open_both(self) -> None:
        case = self._current_case()
        if case:
            webbrowser.open(case.portal_url())
            webbrowser.open(case.sfdc_url())

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key
        value = key.value if hasattr(key, "value") else key
        case = self.cases.get(str(value))
        if case:
            webbrowser.open(case.portal_url())
            webbrowser.open(case.sfdc_url())

    def _visible_jira_case(self, cid: str) -> bool:
        if cid in self._primary_ids:
            return True
        if cid in self._backup_ids:
            case = self._case_lookup.get(cid)
            return bool(case and case.account_label in self.visible_backup)
        return False

    @work(thread=True, exclusive=True, group="jira", exit_on_error=False)
    def _poll_jira(self) -> None:
        if not tt.jira_token_present() or not self._case_lookup:
            return
        data: Dict[str, dict] = {}
        cache = dict(self._jira_alert_cache)
        key_to_case: Dict[str, str] = {}
        for cid, keys in self._case_jiras.items():
            if not self._visible_jira_case(cid):
                continue
            for key in keys:
                key_to_case.setdefault(key, cid)
        for key, cid in key_to_case.items():
            issue = tt.fetch_jira_issue(key)
            time.sleep(0.15)
            if not issue:
                continue
            last = self._last_comment.get(cid)
            cutoff = last[0] if last else None
            activity = tt.jira_last_activity(issue)
            needs = bool(activity and (not cutoff or activity > cutoff))
            info = {
                "key": key,
                "issue": issue,
                "case_id": cid,
                "case": self._case_lookup.get(cid),
                "last_activity": activity,
                "needs_addressing": needs,
                "change_summary": tt.jira_change_summary(issue, cutoff),
            }
            data[key] = info
            self._maybe_alert_jira(info, cache)
        watched = tt.fetch_watched_jira_keys()
        for key in [k for k in watched if k not in key_to_case]:
            issue = tt.fetch_jira_issue(key)
            time.sleep(0.15)
            if not issue:
                continue
            cutoff = cache.get(key)
            activity = tt.jira_last_activity(issue)
            needs = bool(activity and (not cutoff or activity > cutoff))
            info = {
                "key": key,
                "issue": issue,
                "case_id": None,
                "case": None,
                "last_activity": activity,
                "needs_addressing": needs,
                "change_summary": tt.jira_change_summary(issue, cutoff),
            }
            data[key] = info
            self._maybe_alert_jira(info, cache)
        seeded = True
        self.call_from_thread(self._store_jira, data, cache, seeded)

    def _maybe_alert_jira(self, info: dict, cache: dict) -> None:
        key = info.get("key")
        activity = info.get("last_activity")
        if not key or not activity:
            return
        if not self._jira_alert_seeded:
            cache[key] = activity
            return
        if cache.get(key) == activity or not info.get("needs_addressing"):
            cache.setdefault(key, activity)
            return
        cache[key] = activity
        if self.webhook_url:
            tt.webhook_notify_jira(
                self.webhook_url,
                self.webhook_type,
                key,
                ((info.get("issue") or {}).get("fields") or {}).get("summary") or key,
                info.get("change_summary") or "updated",
                info.get("case"),
            )

    def _store_jira(self, data: dict, cache: dict, seeded: bool) -> None:
        self._jira_data = data
        self._jira_alert_cache = cache
        self._jira_alert_seeded = seeded
        tt.save_jira_alert_cache(cache)

    @work(thread=True, exclusive=True, group="integrity", exit_on_error=False)
    def _poll_case_integrity(self) -> None:
        ids = set(self._primary_ids) | {
            cid for cid in self._backup_ids
            if (self._case_lookup.get(cid) and self._case_lookup[cid].account_label in self.visible_backup)
        }
        if not ids:
            return
        sd_new = set(self._sd_cache)
        owner_new = set(self._owner_cache)
        alerts: List[tuple] = []
        id_list = [cid for cid in ids if self._case_lookup.get(cid)]
        histories = tt.fetch_case_histories(id_list)
        timelines = tt.fetch_case_comment_timelines(id_list)
        flagged: List[str] = []
        pending: List[tuple] = []
        for cid in id_list:
            case = self._case_lookup.get(cid)
            if not case:
                continue
            history = histories.get(cid) or []
            comments = timelines.get(cid) or []
            gaming = tt.detect_status_gaming(history, comments, case.subject)
            reverts = tt.detect_owner_reversion(history)
            if gaming or reverts:
                flagged.append(cid)
                pending.append((cid, case, gaming, reverts))
        actors = tt.fetch_case_history_actors_batch(flagged) if flagged else {}
        for cid, case, gaming, reverts in pending:
            gaming = [g for g in gaming if not tt._is_system_actor(actors.get(g.get("id") or "", ""))]
            reverts = [r for r in reverts if not tt._is_system_actor(actors.get(r.get("id") or "", ""))]
            if not self._integrity_seeded:
                sd_new.update(g.get("id") for g in gaming if g.get("id"))
                owner_new.update(r.get("id") for r in reverts if r.get("id"))
                continue
            for flag in gaming:
                hid = flag.get("id")
                if not hid or hid in sd_new:
                    continue
                sd_new.add(hid)
                alerts.append(("sd", case, flag))
            for flag in reverts:
                hid = flag.get("id")
                if not hid or hid in owner_new:
                    continue
                owner_new.add(hid)
                alerts.append(("owner", case, flag))
        self.call_from_thread(self._store_integrity, sd_new, owner_new, alerts)

    def _store_integrity(self, sd_new: Set[str], owner_new: Set[str], alerts: List[tuple]) -> None:
        self._sd_cache = sd_new
        self._owner_cache = owner_new
        self._integrity_seeded = True
        tt.save_id_set_cache(tt.SD_GAMING_CACHE_PATH, sd_new)
        tt.save_id_set_cache(tt.OWNER_ALERT_CACHE_PATH, owner_new)
        if not self.webhook_url:
            return
        for kind, case, flag in alerts:
            if kind == "sd":
                detail = (
                    f"{flag.get('kind')}: {flag.get('old')} → {flag.get('new')} "
                    f"at {flag.get('created')}"
                )
                tt.webhook_notify_integrity(
                    self.webhook_url, self.webhook_type, case,
                    "SD status gaming", detail, "#ff9900", "🟠",
                )
            else:
                detail = (
                    f"{flag.get('old')} → {flag.get('new')} at {flag.get('created')}"
                )
                tt.webhook_notify_integrity(
                    self.webhook_url, self.webhook_type, case,
                    "Owner reversion", detail, "#ff1f6b", "🔴",
                )


def main() -> None:
    parser = build_parser("TAM case dashboard (TUI)")
    args = parser.parse_args()
    TamApp(args).run()


if __name__ == "__main__":
    main()
