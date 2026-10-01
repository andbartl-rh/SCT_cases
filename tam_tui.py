#!/usr/bin/env python3
"""TAM case dashboard — Textual TUI.

Click a row (or press p / Enter) to open the case in the Customer Portal.
Press s for Salesforce. r refreshes. e toggles other-people escalations.
Digit keys / b toggle backup-coverage accounts. q quits.
"""
from __future__ import annotations

import sys
import webbrowser
from typing import Dict, List, Optional, Set

try:
    from rich.text import Text
    from textual import work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
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
        Binding("enter", "portal", "Portal", show=False),
        Binding("s", "sfdc", "SFDC"),
        Binding("e", "toggle_escalations", "Escalations"),
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

    def compose(self) -> ComposeResult:
        yield Static("", id="banner")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=False)
        yield Static("", id="stats")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_columns(
            "CASE#", "SEV", "STAT", "SBT/NEP", "OWNER", "LAST REPLY", "CONTACT", "SUBJECT"
        )
        self.refresh_cases()
        interval = max(15, int(self.args.interval or 300))
        self.set_interval(interval, self.refresh_cases)

    def refresh_cases(self) -> None:
        self.query_one("#stats", Static).update("Refreshing…")
        self._do_load()

    @work(thread=True, exclusive=True, exit_on_error=False)
    def _do_load(self) -> None:
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
        self.call_from_thread(self._populate, board, err)

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

    def _populate(
        self,
        board: Optional[dict],
        err: Optional[str],
        send_alerts: bool = True,
    ) -> None:
        if err:
            self.query_one("#stats", Static).update(f"Error: {err}")
            return
        if not board:
            return
        self._board = board
        self._escalated_ids = set(board.get("esc_ids") or [])
        self._parent_cases = list(board.get("parent_cases") or [])

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
        primary_ids = {c.id or c.number for c in cases}
        backup = [c for c in backup if (c.id or c.number) not in primary_ids]

        debug_uniques(cases + backup, self.args)
        alertable = list(board["cases"]) + list(board["backup"])
        backup_ids = {c.id for c in board["backup"]}
        if send_alerts:
            self.previous = detect_and_alert(
                self.previous,
                alertable,
                self.webhook_url,
                self.webhook_type,
                backup_ids=backup_ids,
            )
        visible_backup = [c for c in backup if c.account_label in self.visible_backup]
        self._fill_table(cases, visible_backup)

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
        self.query_one("#stats", Static).update(
            f"{LAST_FETCH.get('records', len(shown))} records  "
            f"{LAST_FETCH.get('calls', '?')} API calls  "
            f"{LAST_FETCH.get('seconds', '?')}s  "
            f"updated {LAST_FETCH.get('updated', '')}  "
            f"auto-refresh {self.args.interval}s"
            f"{backup_hint}{fail_hint}  "
            "● replied  ◆ TAM contact  ○ other  ▲ escalated  "
            "red CASE# = SBT breached (shown even if WoC)"
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
            for label in sorted(by_acct):
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
            subject.append(case.subject or "", style="bold red")
        else:
            subject.append(case.subject or "", style=subj_style)
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

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key
        value = key.value if hasattr(key, "value") else key
        case = self.cases.get(str(value))
        if case:
            webbrowser.open(case.portal_url())


def main() -> None:
    parser = build_parser("TAM case dashboard (TUI)")
    args = parser.parse_args()
    TamApp(args).run()


if __name__ == "__main__":
    main()
