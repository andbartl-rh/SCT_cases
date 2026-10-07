#!/usr/bin/env python3
"""TAM case dashboard — terminal (top-style) view.

Shared auth, GraphQL fetch, filtering, and webhook logic for tam_tui.py.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import html
import json
import os
import re
import shutil
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# ── Per-TAM constants (edit these) ───────────────────────────────────────────

SSO_USERNAME = "rhn-support-andbartl"
MY_NAME = "Andy Bartlett"

ACCOUNTS = {
    "Tesco": "Tesco",
    "Computershare Technology Services Pty Ltd": "ComputerShare",
    "RBOS FM": "Natwest",
    "The Royal Bank of Scotland Group": "Natwest",
    # Exact names: LIKE %MINISTRY OF DEFENSE% was matching Israel (and others).
    # UK spelling is Defence. Prefix "=" means Salesforce Name eq, not LIKE.
    "=MINISTRY OF DEFENCE": "UKMOD",
    "=Ministry of Defence": "UKMOD",
}

# ── Paths / endpoints ────────────────────────────────────────────────────────

HERE = Path(__file__).resolve().parent
CONTACTS_PATH = HERE / "tam_contacts.json"
CONFIG_PATH = HERE / "tam_config.json"
SLACK_ROUTES_PATH = HERE / "tam_slack_routes.json"
BACKUP_ACCOUNTS_PATH = HERE / "tam_backup_accounts.json"
LATENCY_LOG_PATH = HERE / "tam_latency_log.jsonl"
LATENCY_LOG_MAX_LINES = 10000
JIRA_ALERT_CACHE_PATH = HERE / "tam_jira_alert_cache.json"
SD_GAMING_CACHE_PATH = HERE / "tam_sd_gaming_cache.json"
OWNER_ALERT_CACHE_PATH = HERE / "tam_owner_alert_cache.json"
TOKEN_PATH = Path.home() / ".rh_offline_token"
TOKEN_CACHE_PATH = Path.home() / ".rh_access_token_cache"
JIRA_TOKEN_PATH = Path.home() / ".rh_atlassian_token"

SSO_TOKEN_URL = (
    "https://sso.redhat.com/auth/realms/redhat-external"
    "/protocol/openid-connect/token"
)
GRAPHQL_URL = "https://graphql.redhat.com"
PORTAL_CASE = "https://access.redhat.com/support/cases/#/case/{number}"
SFDC_CASE = "https://redhatsupport.lightning.force.com/lightning/r/Case/{id}/view"
JIRA_BASE = "https://redhat.atlassian.net"
JIRA_BROWSE_URL = JIRA_BASE + "/browse/{key}"
JIRA_RE = re.compile(
    r"(?:issues\.redhat\.com|redhat\.atlassian\.net)/browse/([A-Z][A-Z0-9]+-\d+)"
)

# Named Red Hat colleague TAMs on shared accounts. Exact SFDC display names,
# confirmed by the TAM — not a fuzzy customer-contact list. Empty until Andy
# names colleagues; add them here and ✱ / --mine visibility light up.
CO_TAMS: Dict[str, List[str]] = {}

UNCLAIMED_DAYS = 14
CASE_HISTORY_WINDOW_HOURS = 4
CASE_ROUNDTRIP_HOURS = 48
PAGE_SIZE = 200
MAX_CASE_PAGES = 5
HTTP_TIMEOUT = 60
GQL_TIMEOUT = 30
PAGE_SLEEP = 0.1
BATCH_SLEEP = 0.15
API_CALLS = 0
LAST_FETCH: Dict[str, Any] = {"calls": 0, "seconds": 0.0, "records": 0, "breach": 0}
LAST_WOC: Dict[str, Dict[str, Any]] = {}
LAST_CASE_JIRAS: Dict[str, List[str]] = {}

ACCOUNT_PALETTE = ["#3ecfcf", "#7dce82", "#e0b44a", "#c678dd", "#7aa2f7", "#e06c75"]
STAT_DISPLAY = {
    "WoCAction": "WoCAct",
    "WoCSolution": "WoCSol",
}

# ── Status / product helpers ─────────────────────────────────────────────────

STATUS_ABBREV = {
    "waiting on red hat": "WoRH",
    "in progress": "InPrg",
    "waiting on customer action required": "WoCAction",
    "waiting on customer solution provided": "WoCSolution",
    "waiting on customer": "WoC",
    "waiting on engineering": "WoEng",
    "waiting on collaboration": "WoCol",
    "waiting on translation": "WoTrn",
    "waiting on documentation": "WoDoc",
    "waiting on 3rd party": "Wo3rd",
    "waiting on business unit": "WoBU",
    "waiting on contributor": "WoContrib",
    "waiting on pm": "WoPM",
    "waiting on sales": "WoSales",
    "waiting on qa": "WoQA",
    "waiting on partner": "WoPartner",
    "unassigned": "Unasn",
    "closed": "Closd",
    "new": "New",
    "cancelled": "Cancel",
}

CLOSED_STATUSES = {"closed", "cancelled", "canceled"}
UNCLAIMED_STATUSES = {"worh", "inprg", "unasn"}
# Waiting on Customer (all variants) — hidden from the live TAM view unless SBT is already breached
HIDDEN_STATUS_ABBREV = {"woc", "wocaction", "wocsolution"}

RHEL_PRODUCTS = ["Red Hat Enterprise Linux", "Red Hat Satellite"]
OCP_PRODUCTS = [
    "Red Hat OpenShift Container Platform",
    "OpenShift Container Platform",
    "Red Hat OpenShift Data Foundation",
    "Red Hat OpenShift Container Storage",
    "Red Hat OpenShift Service on AWS",
    "Red Hat OpenShift Dedicated",
    "Azure Red Hat OpenShift",
    "Red Hat OpenShift Virtualization",
    "Red Hat Advanced Cluster Management for Kubernetes",
    "Red Hat Advanced Cluster Security for Kubernetes",
    "Red Hat Quay",
    "Quay.io",
]
MW_PRODUCTS = [
    "Red Hat JBoss Enterprise Application Platform",
    "Red Hat AMQ",
    "Red Hat Fuse",
    "Red Hat Data Grid",
    "Red Hat Single Sign-On",
]
AAP_PRODUCTS = ["Red Hat Ansible Automation Platform"]

# Optional GraphQL selections. Stripped automatically if the schema rejects them.
OPTIONAL_BLOCKS = {
    "SBR_Group__c": "SBR_Group__c { value }",
    "SBT__c": "SBT__c { value }",
    "IsEscalated": "IsEscalated { value }",
    "Product": """
            Product {
              Id
              Name { value }
              ParentProduct__r {
                Id
                Name { value }
              }
            }""",
    "Owner": """
            Owner {
              ... on RedHatSupportUser { Name { value } }
              ... on RedHatSupportGroup { Name { value } }
            }""",
    "LastModifiedBy": "LastModifiedBy { Name { value } }",
    "CaseComments__r": """
            CaseComments__r(first: 15, orderBy: { CreatedDate: { order: DESC } }) {
              edges {
                node { LastModifiedByName__c { value } }
              }
            }""",
}

# ── ANSI ─────────────────────────────────────────────────────────────────────

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAGENTA = "\033[95m"
WHITE = "\033[97m"
GREY = "\033[90m"
BG_RED = "\033[41m"
DARK = "\033[38;5;240m"
SEV_COLORS = {"1": RED + BOLD, "2": YELLOW, "3": CYAN, "4": DIM}
STATUS_ANSI = {
    "WoRH": YELLOW,
    "InPrg": YELLOW,
    "WoEng": YELLOW,
    "WoCol": YELLOW,
    "WoTrn": YELLOW,
    "WoDoc": YELLOW,
    "Wo3rd": YELLOW,
    "WoBU": YELLOW,
    "WoContrib": YELLOW,
    "WoPM": YELLOW,
    "WoQA": YELLOW,
    "WoPartner": YELLOW,
    "WoCAction": CYAN,
    "WoCSolution": CYAN,
    "WoC": CYAN,
    "Unasn": RED + BOLD,
    "New": BOLD,
    "Closd": DIM,
    "Cancel": DIM,
}
STATUS_RICH = {
    "WoRH": "yellow",
    "InPrg": "yellow",
    "WoEng": "yellow",
    "WoCol": "yellow",
    "WoTrn": "yellow",
    "WoDoc": "yellow",
    "Wo3rd": "yellow",
    "WoBU": "yellow",
    "WoContrib": "yellow",
    "WoPM": "yellow",
    "WoQA": "yellow",
    "WoPartner": "yellow",
    "WoCAction": "cyan",
    "WoCSolution": "cyan",
    "WoC": "cyan",
    "Unasn": "bold red",
    "New": "bold",
    "Closd": "dim",
    "Cancel": "dim",
}


def _use_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


def paint(text: str, *codes: str) -> str:
    if not _use_color() or not codes:
        return text
    return "".join(codes) + text + RESET


# ── Data ─────────────────────────────────────────────────────────────────────

@dataclass
class Case:
    id: str
    number: str
    subject: str
    status_raw: str
    status: str
    severity: str
    account: str
    account_label: str
    contact: str
    product: str
    sbr: str
    owner: str
    sbt: Optional[int]
    created: Optional[datetime]
    modified: Optional[datetime]
    comment_authors: List[str] = field(default_factory=list)
    comment_count: int = 0
    marker: str = "○"
    mine: bool = False
    is_contact: bool = False
    unclaimed: bool = False
    escalated: bool = False
    is_backup: bool = False
    last_reply: Optional[datetime] = None
    last_reply_author: str = ""
    co_tam: bool = False
    jira_keys: List[str] = field(default_factory=list)

    def portal_url(self) -> str:
        return PORTAL_CASE.format(number=self.number)

    def sfdc_url(self) -> str:
        return SFDC_CASE.format(id=self.id)


# ── Auth ─────────────────────────────────────────────────────────────────────

def _read_offline_token() -> str:
    if not TOKEN_PATH.exists():
        raise RuntimeError("No offline token")
    token = TOKEN_PATH.read_text().strip()
    if not token:
        raise RuntimeError("No offline token")
    return token


def _load_cached_access_token() -> Optional[str]:
    if not TOKEN_CACHE_PATH.exists():
        return None
    try:
        data = json.loads(TOKEN_CACHE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    expires_at = data.get("expires_at") or data.get("exp") or 0
    token = data.get("access_token") or data.get("token")
    if token and time.time() < expires_at - 60:
        return token
    return None


def _save_cached_access_token(access_token: str, expires_in: int) -> None:
    payload = {
        "access_token": access_token,
        "expires_at": time.time() + int(expires_in or 900),
    }
    TOKEN_CACHE_PATH.write_text(json.dumps(payload))
    os.chmod(TOKEN_CACHE_PATH, 0o600)


def get_access_token(force: bool = False) -> str:
    if not force:
        cached = _load_cached_access_token()
        if cached:
            return cached
    offline = _read_offline_token()
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": "rhsm-api",
            "refresh_token": offline,
        }
    ).encode()
    req = urllib.request.Request(
        SSO_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode(errors="replace")
        if "invalid_grant" in err_body:
            raise RuntimeError(
                "Offline token expired (invalid_grant). "
                "Generate a new one at https://access.redhat.com/management/api"
            ) from exc
        raise RuntimeError(f"SSO token exchange failed: HTTP {exc.code} {err_body[:300]}") from exc
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"SSO response had no access_token: {data}")
    exp = time.time() + int(data.get("expires_in") or 900)
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp", exp)
    except Exception:
        pass
    TOKEN_CACHE_PATH.write_text(json.dumps({"token": token, "access_token": token, "exp": exp, "expires_at": exp}))
    os.chmod(TOKEN_CACHE_PATH, 0o600)
    return token


# ── HTTP / GraphQL ───────────────────────────────────────────────────────────

def _decode_http_body(raw: bytes, headers) -> str:
    enc = (headers.get("Content-Encoding") or "").lower() if headers else ""
    if "gzip" in enc:
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    return raw.decode(errors="replace")


def _http_json(url: str, payload: dict, token: str, timeout: Optional[float] = None) -> dict:
    raw = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=raw,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "apollographql-client-name": SSO_USERNAME,
            "apollographql-client-version": "latest",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout if timeout is not None else GQL_TIMEOUT) as resp:
            body = _decode_http_body(resp.read(), resp.headers)
            return json.loads(body) if body else {}
    except socket.timeout as exc:
        raise RuntimeError("GraphQL request timed out. Retry, or check the VPN.") from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, socket.timeout) or "timed out" in str(reason).lower():
            raise RuntimeError("GraphQL request timed out. Retry, or check the VPN.") from exc
        raise RuntimeError(f"Network error talking to GraphQL: {exc}") from exc
    except urllib.error.HTTPError as exc:
        err_body = _decode_http_body(exc.read(), exc.headers)
        if exc.code == 400:
            try:
                parsed = json.loads(err_body) if err_body else {}
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("errors"):
                return parsed
        if exc.code == 403:
            raise RuntimeError(
                "Tunnel connection failed: 403 Forbidden. "
                "Connect to the Red Hat VPN and retry."
            ) from exc
        if exc.code == 401:
            raise
        raise RuntimeError(f"HTTP {exc.code} from {url}: {err_body[:800]}") from exc


def graphql(
    query: str,
    token: str,
    variables: Optional[dict] = None,
    operation_name: Optional[str] = None,
    debug: bool = False,
) -> dict:
    global API_CALLS
    API_CALLS += 1
    payload: dict = {"query": query}
    if variables is not None:
        payload["variables"] = variables
    if operation_name:
        payload["operationName"] = operation_name
    data = _http_json(GRAPHQL_URL, payload, token, timeout=GQL_TIMEOUT)
    if debug and data.get("errors"):
        print("GraphQL errors:", json.dumps(data["errors"], indent=2)[:4000], file=sys.stderr)
    if data.get("errors") and data.get("data") is None:
        msg = (data["errors"][0] or {}).get("message", "graphql error")
        # Field-stripping callers still need the error body; only raise when
        # there is nothing to recover (no uiapi payload at all).
        if "Cannot query field" not in msg and "must have a selection" not in msg:
            raise RuntimeError(msg)
    return data


def gql(query: str, variables: Optional[dict] = None, operation_name: Optional[str] = None) -> dict:
    """Auth + GraphQL helper for tam_explore.py. Raises if data is null."""
    token = get_access_token()
    data = graphql(query, token, variables=variables, operation_name=operation_name)
    if data.get("errors") and data.get("data") is None:
        msg = (data["errors"][0] or {}).get("message", "graphql error")
        raise RuntimeError(msg)
    return data


def v(field: Any) -> Any:
    """Unwrap a GraphQL { value } field. Strings are HTML-unescaped; bool/float pass through."""
    if field is None:
        return ""
    val = field.get("value") if isinstance(field, dict) else field
    if val is None:
        return ""
    return html.unescape(val) if isinstance(val, str) and val else val


def _unknown_fields(errors: Sequence[dict]) -> List[str]:
    found: List[str] = []
    for err in errors:
        msg = err.get("message") or ""
        m = re.search(r'Cannot query field ["\']([^"\']+)["\']', msg)
        if m:
            found.append(m.group(1))
    return found


def node_id(node: dict) -> str:
    raw = node.get("Id")
    if isinstance(raw, str) and raw:
        return raw
    return _value(node, "Id")


def _value(node: Any, *path: str) -> str:
    cur = node
    for key in path:
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(key)
    if cur is None:
        return ""
    if isinstance(cur, dict) and "value" in cur:
        val = cur.get("value")
        if val is None:
            return ""
        return html.unescape(val) if isinstance(val, str) else str(val)
    return html.unescape(cur) if isinstance(cur, str) else str(cur)


def parse_dt(value: str) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _build_cases_query(optional: Dict[str, str]) -> str:
    extras = "\n".join(optional.values())
    return f"""
query GetCasesForAccount($first: Int, $after: String, $where: RedHatSupportCase_Filter) {{
  redhat_support_uiapi {{
    query {{
      RedHatSupportCase(
        where: $where
        first: $first
        after: $after
        orderBy: {{ LastModifiedDate: {{ order: DESC }} }}
      ) {{
        edges {{
          node {{
            Id
            CaseNumber__c {{ value }}
            Subject {{ value }}
            Status {{ value }}
            Priority {{ value }}
            CreatedDate {{ value }}
            LastModifiedDate {{ value }}
            RedHatSupportAccount {{
              Name {{ value }}
              AccountNumber {{ value }}
            }}
            RedHatSupportContact {{
              Name {{ value }}
              SSOUserName__c {{ value }}
            }}
            {extras}
          }}
          cursor
        }}
        pageInfo {{
          hasNextPage
          endCursor
        }}
      }}
    }}
  }}
}}
"""


def account_where(account_term: str) -> dict:
    if account_term.startswith("="):
        name_filter = {"eq": account_term[1:]}
    else:
        name_filter = {"like": f"%{account_term}%"}
    return {
        "and": [
            {"RedHatSupportRecordType": {"Name": {"eq": "Technical Support"}}},
            {"AccessRestrictions__c": {"eq": "None"}},
            {"RedHatSupportAccount": {"Name": name_filter}},
            {"Status": {"nin": ["Closed"]}},
        ]
    }


def fetch_account_nodes(
    account_term: str,
    token: str,
    debug: bool = False,
    state: Optional[dict] = None,
) -> List[dict]:
    """Fetch RedHatSupportCase nodes for one account name fragment."""
    state = state if state is not None else {}
    optional = dict(state.get("optional", OPTIONAL_BLOCKS))
    nodes: List[dict] = []
    after = None
    pages = 0

    while pages < MAX_CASE_PAGES:
        query = _build_cases_query(optional)
        variables = {
            "first": PAGE_SIZE,
            "after": after,
            "where": account_where(account_term),
        }
        try:
            data = graphql(
                query,
                token,
                variables=variables,
                operation_name="GetCasesForAccount",
                debug=debug,
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                token = get_access_token(force=True)
                data = graphql(
                    query,
                    token,
                    variables=variables,
                    operation_name="GetCasesForAccount",
                    debug=debug,
                )
            else:
                raise

        errors = data.get("errors") or []
        unknown = _unknown_fields(errors)
        msg = " ".join(e.get("message", "") for e in errors)
        if errors:
            stripped = False
            for name in list(optional):
                if name in unknown or name in msg:
                    optional.pop(name)
                    stripped = True
            if stripped:
                state["optional"] = optional
                after = None
                nodes = []
                continue
            if not ((data.get("data") or {}).get("redhat_support_uiapi")):
                raise RuntimeError(f"GraphQL error: {msg[:800]}")

        payload = (
            ((data.get("data") or {}).get("redhat_support_uiapi") or {})
            .get("query", {})
            .get("RedHatSupportCase")
            or {}
        )
        edges = payload.get("edges") or []
        for edge in edges:
            node = edge.get("node") or {}
            if node:
                nodes.append(node)
        page = payload.get("pageInfo") or {}
        pages += 1
        if page.get("hasNextPage") and page.get("endCursor"):
            after = page["endCursor"]
            time.sleep(PAGE_SLEEP)
            continue
        break

    state["optional"] = optional
    return nodes


def comments_from_node(node: dict) -> List[str]:
    authors: List[str] = []
    last = _value(node, "LastModifiedBy", "Name")
    if last:
        authors.append(last)
    edges = ((node.get("CaseComments__r") or {}).get("edges")) or []
    for edge in edges:
        name = _value(edge.get("node") or {}, "LastModifiedByName__c")
        if name:
            authors.append(name)
    return authors


def fetch_comment_activity(
    case_ids: Sequence[str],
) -> Tuple[Set[str], Dict[str, Tuple[str, str]], Dict[str, List[str]], int]:
    """Return (commented_by_me, last_comment, all_authors, api_calls).

    Batches of 10 + full cursor pagination so a chatty case cannot starve a quiet one.
    api_calls is the real gql() count across every batch and page (error #25).
    Body__c is scraped for Jira browse URLs into LAST_CASE_JIRAS.
    """
    global LAST_CASE_JIRAS
    case_ids = [cid for cid in case_ids if cid]
    commented_by_me: Set[str] = set()
    last_comment: Dict[str, Tuple[str, str]] = {}
    authors_by_case: Dict[str, Set[str]] = defaultdict(set)
    case_jiras: Dict[str, Set[str]] = defaultdict(set)
    batch_size = 10
    page_size = 200
    max_pages = 25
    api_calls = 0
    token = get_access_token()

    for i in range(0, len(case_ids), batch_size):
        if i:
            time.sleep(BATCH_SLEEP)
        batch = case_ids[i : i + batch_size]
        id_list = ", ".join(f'"{cid}"' for cid in batch)
        cursor = None
        for _page in range(max_pages):
            if _page:
                time.sleep(PAGE_SLEEP)
            after_clause = f'after: "{cursor}"' if cursor else ""
            query = f"""
query CommentActivity {{
  redhat_support_uiapi {{
    query {{
      RedHatSupportCaseComment__c(
        first: {page_size}
        {after_clause}
        where: {{ Case__c: {{ in: [{id_list}] }} }}
      ) {{
        pageInfo {{ hasNextPage endCursor }}
        edges {{
          node {{
            Case__c {{ value }}
            CreatedDate {{ value }}
            Body__c {{ value }}
            CreatedBy {{ Name {{ value }} }}
          }}
        }}
      }}
    }}
  }}
}}
"""
            try:
                data = graphql(query, token, operation_name="CommentActivity")
                api_calls += 1
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    token = get_access_token(force=True)
                    data = graphql(query, token, operation_name="CommentActivity")
                    api_calls += 1
                else:
                    raise
            if data.get("errors") and not ((data.get("data") or {}).get("redhat_support_uiapi")):
                break
            payload = (
                ((data.get("data") or {}).get("redhat_support_uiapi") or {})
                .get("query", {})
                .get("RedHatSupportCaseComment__c")
                or {}
            )
            edges = payload.get("edges") or []
            for edge in edges:
                node = edge.get("node") or {}
                cid = v(node.get("Case__c"))
                if not cid:
                    continue
                created = v(node.get("CreatedDate"))
                author = v((node.get("CreatedBy") or {}).get("Name"))
                body = v(node.get("Body__c"))
                if not isinstance(cid, str):
                    cid = str(cid)
                if author:
                    authors_by_case[cid].add(str(author))
                if author and MY_NAME.lower() in str(author).lower():
                    commented_by_me.add(cid)
                created_s = str(created) if created else ""
                author_s = str(author) if author else ""
                if created_s and (cid not in last_comment or created_s > last_comment[cid][0]):
                    last_comment[cid] = (created_s, author_s)
                if body:
                    for match in JIRA_RE.finditer(html.unescape(str(body))):
                        case_jiras[cid].add(match.group(1))
            page_info = payload.get("pageInfo") or {}
            if not page_info.get("hasNextPage") or not edges:
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break
    LAST_CASE_JIRAS = {cid: sorted(keys) for cid, keys in case_jiras.items()}
    all_authors = {cid: sorted(names) for cid, names in authors_by_case.items()}
    return commented_by_me, last_comment, all_authors, api_calls


def _escalation_query() -> str:
    return """
query GetEscalations($first: Int, $after: String, $where: RedHatSupportCase_Filter) {
  redhat_support_uiapi {
    query {
      RedHatSupportCase(
        where: $where
        first: $first
        after: $after
        orderBy: { LastModifiedDate: { order: DESC } }
      ) {
        edges {
          node {
            Id
            CaseNumber__c { value }
            Subject { value }
            Status { value }
            Type { value }
            ParentId { value }
            RedHatSupportAccount { Name { value } }
            RedHatSupportContact { Name { value } }
            Owner {
              ... on RedHatSupportUser { Name { value } }
              ... on RedHatSupportGroup { Name { value } }
            }
          }
          cursor
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


def escalation_where(account_term: str) -> dict:
    if account_term.startswith("="):
        name_filter = {"eq": account_term[1:]}
    else:
        name_filter = {"like": f"%{account_term}%"}
    return {
        "and": [
            {"RedHatSupportRecordType": {"Name": {"eq": "Escalation"}}},
            {"RedHatSupportAccount": {"Name": name_filter}},
        ]
    }


def fetch_escalations(debug: bool = False) -> Tuple[List[dict], List[str]]:
    """Escalation *records* for primary + backup accounts. Not board rows."""
    token = get_access_token()
    accounts = dict(ACCOUNTS)
    accounts.update(load_backup_accounts())
    nodes: List[dict] = []
    seen: Set[str] = set()
    failed: List[str] = []
    query = _escalation_query()
    for acct_i, term in enumerate(accounts):
        if acct_i:
            time.sleep(BATCH_SLEEP)
        after = None
        pages = 0
        try:
            while pages < MAX_CASE_PAGES:
                data = graphql(
                    query,
                    token,
                    variables={"first": PAGE_SIZE, "after": after, "where": escalation_where(term)},
                    operation_name="GetEscalations",
                    debug=debug,
                )
                if data.get("errors") and not ((data.get("data") or {}).get("redhat_support_uiapi")):
                    raise RuntimeError((data["errors"][0] or {}).get("message", "graphql error"))
                payload = (
                    ((data.get("data") or {}).get("redhat_support_uiapi") or {})
                    .get("query", {})
                    .get("RedHatSupportCase")
                    or {}
                )
                for edge in payload.get("edges") or []:
                    node = edge.get("node") or {}
                    cid = node_id(node)
                    if not cid or cid in seen:
                        continue
                    seen.add(cid)
                    nodes.append(node)
                page = payload.get("pageInfo") or {}
                pages += 1
                if page.get("hasNextPage") and page.get("endCursor"):
                    after = page["endCursor"]
                    time.sleep(PAGE_SLEEP)
                    continue
                break
        except Exception:
            failed.append(term)
    return nodes, failed


def escalation_parent_ids(esc_nodes: Sequence[dict]) -> Set[str]:
    ids: Set[str] = set()
    for node in esc_nodes:
        parent = v(node.get("ParentId"))
        if parent:
            ids.add(str(parent))
    return ids


def _cases_by_ids_query() -> str:
    extras = "\n".join(OPTIONAL_BLOCKS.values())
    return f"""
query GetCasesByIds($first: Int, $after: String, $where: RedHatSupportCase_Filter) {{
  redhat_support_uiapi {{
    query {{
      RedHatSupportCase(
        where: $where
        first: $first
        after: $after
      ) {{
        edges {{
          node {{
            Id
            CaseNumber__c {{ value }}
            Subject {{ value }}
            Status {{ value }}
            Priority {{ value }}
            CreatedDate {{ value }}
            LastModifiedDate {{ value }}
            RedHatSupportAccount {{
              Name {{ value }}
              AccountNumber {{ value }}
            }}
            RedHatSupportContact {{
              Name {{ value }}
              SSOUserName__c {{ value }}
            }}
            {extras}
          }}
        }}
        pageInfo {{ hasNextPage endCursor }}
      }}
    }}
  }}
}}
"""


def fetch_cases_by_ids(ids: Sequence[str], debug: bool = False) -> Tuple[List[dict], bool]:
    """Fetch Technical Support cases by Salesforce Id. Returns (nodes, failed)."""
    ids = [i for i in ids if i]
    if not ids:
        return [], False
    token = get_access_token()
    contacts = load_contacts()
    nodes: List[dict] = []
    failed = False
    query = _cases_by_ids_query()
    for i in range(0, len(ids), 20):
        if i:
            time.sleep(BATCH_SLEEP)
        batch = ids[i : i + 20]
        try:
            data = graphql(
                query,
                token,
                variables={"first": PAGE_SIZE, "after": None, "where": {"Id": {"in": batch}}},
                operation_name="GetCasesByIds",
                debug=debug,
            )
            if data.get("errors") and not ((data.get("data") or {}).get("redhat_support_uiapi")):
                failed = True
                continue
            payload = (
                ((data.get("data") or {}).get("redhat_support_uiapi") or {})
                .get("query", {})
                .get("RedHatSupportCase")
                or {}
            )
            for edge in payload.get("edges") or []:
                node = edge.get("node") or {}
                if node:
                    nodes.append(node)
        except Exception:
            failed = True
    _ = contacts
    return nodes, failed


def classify_node(node: dict, is_backup: bool = False) -> Case:
    label = short_account(_value(node, "RedHatSupportAccount", "Name"))
    case = classify(node, label, load_contacts(), comments_from_node(node))
    case.is_backup = is_backup or label in set(load_backup_accounts().values())
    return case


# ── Contacts / config ────────────────────────────────────────────────────────

def load_contacts() -> Dict[str, List[str]]:
    if not CONTACTS_PATH.exists():
        return {label: [] for label in ACCOUNTS.values()}
    try:
        data = json.loads(CONTACTS_PATH.read_text())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {CONTACTS_PATH}: {exc}") from exc
    return {k: list(v or []) for k, v in data.items()}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def save_webhook(url: str, webhook_type: str) -> None:
    cfg = load_config()
    cfg["webhook"] = url
    cfg["webhook_type"] = webhook_type
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
    os.chmod(CONFIG_PATH, 0o600)
    print(f"Saved webhook ({webhook_type}) to {CONFIG_PATH}")


@lru_cache(maxsize=1)
def load_backup_accounts() -> Dict[str, str]:
    if not BACKUP_ACCOUNTS_PATH.exists():
        return {}
    try:
        data = json.loads(BACKUP_ACCOUNTS_PATH.read_text())
    except Exception:
        return {}
    return {str(k): str(v) for k, v in (data or {}).items()}


def load_slack_routes() -> dict:
    if not SLACK_ROUTES_PATH.exists():
        return {}
    try:
        return json.loads(SLACK_ROUTES_PATH.read_text())
    except Exception:
        return {}


def short_account(full_name: str) -> str:
    text = full_name or ""
    for keyword, short in ACCOUNTS.items():
        key = keyword[1:] if keyword.startswith("=") else keyword
        if key.lower() in text.lower():
            return short
    for keyword, short in load_backup_accounts().items():
        key = keyword[1:] if keyword.startswith("=") else keyword
        if key.lower() in text.lower():
            return short
    return text[:8] or "?"


# ── Classification ───────────────────────────────────────────────────────────

def abbreviate_status(raw: str) -> str:
    key = (raw or "").strip().lower()
    if key in STATUS_ABBREV:
        return STATUS_ABBREV[key]
    return (raw or "")[:8] or "?"


def severity_of(node: dict) -> str:
    for path in (
        ("Priority",),
        ("Severity__c",),
        ("Severity",),
    ):
        val = _value(node, *path)
        if val:
            m = re.match(r"\s*([1-4])", val)
            if m:
                return m.group(1)
            mapping = {"urgent": "1", "high": "2", "medium": "3", "normal": "3", "low": "4"}
            return mapping.get(val.lower(), val[:1] or "?")
    return "?"


def product_of(node: dict) -> str:
    parent = _value(node, "Product", "ParentProduct__r", "Name")
    child = _value(node, "Product", "Name")
    if parent and child and parent.lower() not in child.lower():
        return f"{parent} / {child}"
    return parent or child or _value(node, "Product__c") or _value(node, "Product")


def sbt_of(node: dict) -> Optional[int]:
    val = _value(node, "SBT__c") or _value(node, "SBT")
    if val in ("", None):
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def _norm_name(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def name_is_mine(text: str) -> bool:
    n = _norm_name(text)
    if not n:
        return False
    needles = [
        _norm_name(MY_NAME),
        _norm_name(SSO_USERNAME),
        _norm_name(SSO_USERNAME.replace("rhn-support-", "")),
    ]
    return any(needle and needle in n for needle in needles)


def is_tam_contact(contact_name: str, account_short: str, contacts: Dict[str, List[str]]) -> bool:
    """Fuzzy match. Blank contact must not match. Shorter side must be at least 4 characters."""
    if not contact_name or not str(contact_name).strip():
        return False
    keywords = contacts.get(account_short, [])
    cn = str(contact_name).lower().strip()
    for raw in keywords:
        if not raw:
            continue
        kk = str(raw).lower().strip()
        if len(kk) >= 4 and kk in cn:
            return True
        if len(cn) >= 4 and cn in kk:
            return True
    return False


def fuzzy_contact(contact: str, names: Sequence[str]) -> bool:
    return is_tam_contact(contact, "_", {"_": list(names)})


def is_co_tam(name: str, account_short: str) -> bool:
    if not name or not str(name).strip():
        return False
    n = str(name).lower().strip()
    for full in CO_TAMS.get(account_short, []):
        f = str(full).lower().strip()
        if len(f) >= 4 and f in n:
            return True
        if len(n) >= 4 and n in f:
            return True
    return False


def is_co_tam_case(
    case: Case,
    all_authors: Optional[Dict[str, Sequence[str]]] = None,
    account_short: Optional[str] = None,
) -> bool:
    """True if a colleague owns this case, or has ever commented on it."""
    acct = account_short or case.account_label
    if is_co_tam(case.owner, acct):
        return True
    authors = []
    if all_authors:
        authors = list(all_authors.get(case.id, ()))
    authors.extend(case.comment_authors or [])
    for author in authors:
        if is_co_tam(author, acct):
            return True
    return False


def is_mine_visible(
    case: Case,
    commented_ids: Set[str],
    all_authors: Optional[Dict[str, Sequence[str]]] = None,
    contacts: Optional[Dict[str, List[str]]] = None,
) -> bool:
    """Does this belong on --mine: replied, TAM contact, co-TAM, or unclaimed."""
    if case.id in commented_ids or case.mine:
        return True
    if case.unclaimed:
        return True
    contacts = contacts if contacts is not None else load_contacts()
    if is_tam_contact(case.contact, case.account_label, contacts):
        return True
    if is_co_tam_case(case, all_authors, case.account_label):
        return True
    return False


def product_matches(product: str, filters: Optional[Sequence[str]]) -> bool:
    if not filters:
        return True
    p = (product or "").lower()
    if not p:
        return True
    for item in filters:
        f = item.lower()
        if f in p:
            return True
        if f.startswith("red hat ") and f[8:] in p:
            return True
    return False


def age_days(created: Optional[datetime]) -> float:
    if not created:
        return 0.0
    return (datetime.now(timezone.utc) - created).total_seconds() / 86400.0


def classify(
    node: dict,
    account_label: str,
    contacts: Dict[str, List[str]],
    comment_authors: Sequence[str],
) -> Case:
    status_raw = _value(node, "Status")
    created = parse_dt(_value(node, "CreatedDate"))
    authors = list(comment_authors)
    owner = _value(node, "Owner", "Name")
    number = _value(node, "CaseNumber__c") or _value(node, "CaseNumber")
    mine = any(name_is_mine(a) for a in authors) or name_is_mine(owner)
    contact = _value(node, "RedHatSupportContact", "Name") or _value(node, "Contact", "Name")
    is_contact = is_tam_contact(contact, account_label, contacts)
    status_abbr = abbreviate_status(status_raw)
    escalated = False
    unclaimed = (
        not mine
        and not any(name_is_mine(a) for a in authors)
        and age_days(created) < UNCLAIMED_DAYS
        and status_abbr.lower() in UNCLAIMED_STATUSES
    )
    if mine:
        marker = "●"
    elif is_contact:
        marker = "◆"
    else:
        marker = "○"
    return Case(
        id=node_id(node),
        number=number,
        subject=html.unescape(_value(node, "Subject")),
        status_raw=status_raw,
        status=status_abbr,
        severity=severity_of(node),
        account=_value(node, "RedHatSupportAccount", "Name") or _value(node, "Account", "Name"),
        account_label=account_label,
        contact=contact,
        product=product_of(node),
        sbr=_value(node, "SBR_Group__c"),
        owner=owner,
        sbt=sbt_of(node),
        created=created,
        modified=parse_dt(_value(node, "LastModifiedDate")),
        comment_authors=authors,
        comment_count=len(authors),
        marker=marker,
        mine=mine,
        is_contact=is_contact,
        unclaimed=unclaimed,
        escalated=escalated,
    )


def is_closed(case: Case) -> bool:
    return (case.status_raw or "").strip().lower() in CLOSED_STATUSES


def fetch_all_cases(
    product_filter: Optional[Sequence[str]] = None,
    sbr_filter: Optional[str] = None,
    mine_only: bool = False,
    debug: bool = False,
    accounts: Optional[Dict[str, str]] = None,
    is_backup: bool = False,
) -> Tuple[List[Case], List[str]]:
    global API_CALLS, LAST_FETCH
    if accounts is None:
        API_CALLS = 0
    started = time.time()
    token = get_access_token()
    contacts = load_contacts()
    gql_state: dict = {}
    raw_nodes: List[Tuple[str, dict]] = []
    failed_accounts: List[str] = []
    source = accounts if accounts is not None else ACCOUNTS
    for i, (term, label) in enumerate(source.items()):
        if i:
            time.sleep(BATCH_SLEEP)
        if debug:
            print(f"Fetching {label} ({term})...", file=sys.stderr)
        try:
            nodes = fetch_account_nodes(term, token, debug=debug, state=gql_state)
        except Exception:
            failed_accounts.append(term)
            continue
        for node in nodes:
            raw_nodes.append((label, node))

    cases: List[Case] = []
    seen = set()
    if not is_backup:
        LAST_WOC.clear()
    for label, node in raw_nodes:
        cid = node_id(node)
        number = _value(node, "CaseNumber__c") or _value(node, "CaseNumber")
        key = cid or number
        if key in seen:
            continue
        seen.add(key)
        case = classify(node, label, contacts, comments_from_node(node))
        case.is_backup = is_backup
        if not case.number:
            continue
        if is_closed(case):
            continue
        if case.status.lower() in HIDDEN_STATUS_ABBREV and not is_breached(case):
            info = LAST_WOC.setdefault(case.account_label, {"count": 0, "name": case.account})
            info["count"] += 1
            info["name"] = case.account or info["name"]
            continue
        if not product_matches(case.product, product_filter):
            continue
        if sbr_filter and sbr_filter.lower() not in (case.sbr or "").lower():
            continue
        if mine_only and not is_mine_visible(case, set(), None, contacts):
            continue
        cases.append(case)

    def sort_key(c: Case):
        sev = int(c.severity) if c.severity.isdigit() else 9
        sbt = c.sbt if c.sbt is not None else 10**9
        created = c.created or datetime.max.replace(tzinfo=timezone.utc)
        return (c.account_label.lower(), 1 if c.unclaimed else 0, sev, sbt, created)

    cases.sort(key=sort_key)
    if not is_backup:
        LAST_FETCH.clear()
        LAST_FETCH.update({
            "calls": API_CALLS,
            "seconds": round(time.time() - started, 1),
            "records": len(cases),
            "breach": sum(1 for c in cases if is_breached(c)),
            "updated": datetime.now().strftime("%H:%M:%S"),
            "failed": failed_accounts,
        })
    return cases, failed_accounts


def fetch_backup_accounts_cases(
    product_filter: Optional[Sequence[str]] = None,
    sbr_filter: Optional[str] = None,
    debug: bool = False,
) -> Tuple[List[Case], List[str]]:
    backup = load_backup_accounts()
    if not backup:
        return [], []
    return fetch_all_cases(
        product_filter=product_filter,
        sbr_filter=sbr_filter,
        debug=debug,
        accounts=backup,
        is_backup=True,
    )


def apply_comment_activity(
    cases: Sequence[Case],
) -> Tuple[Set[str], Dict[str, Tuple[str, str]], Dict[str, List[str]]]:
    commented, last_comment, all_authors, _api_calls = fetch_comment_activity(
        [c.id for c in cases if c.id]
    )
    for case in cases:
        if case.id in commented:
            case.mine = True
            case.marker = "●"
            case.unclaimed = False
        info = last_comment.get(case.id)
        if info:
            case.last_reply = parse_dt(info[0])
            case.last_reply_author = info[1] or ""
        extra = all_authors.get(case.id) or []
        if extra:
            merged = list(dict.fromkeys(list(case.comment_authors) + list(extra)))
            case.comment_authors = merged
        case.jira_keys = list(LAST_CASE_JIRAS.get(case.id) or [])
        case.co_tam = is_co_tam_case(case, all_authors, case.account_label)
        if case.co_tam and not case.mine and not case.is_contact:
            case.marker = "✱"
    return commented, last_comment, all_authors


def apply_escalation_marks(cases: Sequence[Case], esc_ids: Set[str]) -> None:
    for case in cases:
        case.escalated = bool(case.id and case.id in esc_ids)


def is_my_case(
    case: Case,
    commented_ids: Set[str],
    contacts: Dict[str, List[str]],
    all_authors: Optional[Dict[str, Sequence[str]]] = None,
) -> bool:
    return is_mine_visible(case, commented_ids, all_authors, contacts)


def nodes_to_cases(nodes: Sequence[dict], is_backup: bool = False) -> List[Case]:
    contacts = load_contacts()
    backup_labels = set(load_backup_accounts().values())
    cases: List[Case] = []
    seen = set()
    for node in nodes:
        label = short_account(_value(node, "RedHatSupportAccount", "Name"))
        case = classify(node, label, contacts, comments_from_node(node))
        case.is_backup = is_backup or label in backup_labels
        if not case.number or is_closed(case) or case.id in seen:
            continue
        seen.add(case.id)
        cases.append(case)
    return cases


# ── Display ──────────────────────────────────────────────────────────────────

def fmt_age(dt: Optional[datetime]) -> str:
    if not dt:
        return "?"
    secs = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    if secs < 86400 * 14:
        return f"{secs // 86400}d"
    return f"{secs // (86400 * 7)}w"


def is_breached(case: Case) -> bool:
    return case.sbt is not None and case.sbt < 0


def fmt_stat(status: str) -> str:
    return STAT_DISPLAY.get(status, status)


def fmt_owner(name: str, width: int = 18) -> str:
    text = (name or "").strip() or "-"
    return text[:width]


def account_color(label: str) -> str:
    labels = list(dict.fromkeys(ACCOUNTS.values()))
    if label in labels:
        return ACCOUNT_PALETTE[labels.index(label) % len(ACCOUNT_PALETTE)]
    return ACCOUNT_PALETTE[sum(ord(ch) for ch in label) % len(ACCOUNT_PALETTE)]


def account_sort_key(cases: Sequence[Case]) -> Tuple[int, str]:
    """Worst (most negative / soonest) SBT first. Empty groups sort last."""
    sbts = [c.sbt for c in cases if c.sbt is not None]
    worst = min(sbts) if sbts else 10**9
    label = cases[0].account_label.lower() if cases else ""
    return (worst, label)


def grouped_cases(cases: Sequence[Case]) -> List[Tuple[str, List[Case]]]:
    buckets: Dict[str, List[Case]] = {}
    for case in cases:
        buckets.setdefault(case.account_label, []).append(case)
    for label in dict.fromkeys(ACCOUNTS.values()):
        info = LAST_WOC.get(label) or {}
        if label not in buckets and info.get("count"):
            buckets[label] = []
    return [
        (label, buckets[label])
        for label in sorted(buckets, key=lambda a: account_sort_key(buckets[a]))
    ]


def now_stamp() -> str:
    local = datetime.now().astimezone()
    tz = local.tzname() or ""
    return local.strftime(f"%H:%M:%S {tz} %a %d %b %Y").replace("  ", " ")


def fmt_sbt(minutes: Optional[int]) -> Tuple[str, str]:
    """Return (display_string, ansi_colour). RED = hotter / closer to now."""
    if minutes is None:
        return "n/a", GREY
    try:
        mins = float(minutes)
    except (TypeError, ValueError):
        return "n/a", GREY
    if mins < 0:
        days = abs(mins) / (60 * 24)
        if days > 30:
            months = max(1, round(days / 30))
            colour = RED if months < 3 else YELLOW if months < 9 else GREY
            return f"NEP~{months}mo", colour
        return f"!{int(abs(mins))}m", RED
    if mins < 120:
        return f"{int(mins)}m", YELLOW
    hrs = mins / 60
    if hrs < 24:
        return f"{hrs:.1f}h", GREEN
    return f"{hrs / 24:.1f}d", DIM


def fmt_last_reply(dt: Optional[datetime]) -> Tuple[str, str]:
    if not dt:
        return "no reply", DIM
    age_days = (datetime.now(timezone.utc) - dt).days
    text = dt.strftime("%d.%b.%y")
    colour = RED if age_days < 14 else YELLOW if age_days < 30 else DIM
    return text, colour


def fmt_row(case: Case, width: int) -> str:
    if case.mine:
        marker = paint("●", GREEN)
    elif case.is_contact:
        marker = paint("◆", YELLOW)
    elif case.co_tam:
        marker = paint("✱", CYAN)
    else:
        marker = paint("○", DIM)

    sev_txt = f"Sev{case.severity}"
    sev = paint(f"{sev_txt:<4}", SEV_COLORS.get(case.severity, CYAN if case.severity == "3" else ""))
    st = paint(f"{fmt_stat(case.status):<7}", STATUS_ANSI.get(case.status, ""))
    sbt_raw, sbt_colour = fmt_sbt(case.sbt)
    sbt = paint(f"{sbt_raw:<8}", sbt_colour + (BOLD if sbt_colour == RED else ""))
    reply_raw, reply_colour = fmt_last_reply(case.last_reply)
    reply = paint(f"{reply_raw:<10}", reply_colour)
    contact = f"{marker} {(case.contact or '-'):<18.18}"
    owner_raw = fmt_owner(case.owner, 18)
    if name_is_mine(case.owner):
        owner = paint(f"{owner_raw:<18}", GREEN)
    else:
        owner = f"{owner_raw:<18}"
    if is_breached(case):
        caseno = paint(f"{case.number:<10}", BG_RED, WHITE, BOLD)
    elif case.escalated:
        caseno = paint(f"{case.number:<10}", RED, BOLD)
    else:
        caseno = paint(f"{case.number:<10}", CYAN)
    marks = ""
    if case.escalated:
        marks += paint("▲ ", RED, BOLD)
    if case.jira_keys:
        marks += paint("▪ ", MAGENTA)
    body = case.subject or ""
    if case.escalated:
        body = paint(body, RED, BOLD)
    subject = marks + body
    prefix = f"{caseno} {sev} {st} {sbt} {owner} {reply} {contact} "
    remain = max(20, width - len(re.sub(r"\033\[[0-9;]*m", "", prefix)) - 1)
    line = prefix + subject[: remain + 40]
    if case.unclaimed and not is_breached(case):
        return paint(re.sub(r"\033\[[0-9;]*m", "", line), DARK)
    return line


def header_line(width: int) -> str:
    raw = f"{'CASE#':<10} {'SEV':<4} {'STAT':<7} {'SBT/NEP':<8} {'OWNER':<18} {'LAST REPLY':<10} {'CONTACT':<20} SUBJECT"
    return paint(raw[:width], DIM)


def render_terminal(cases: List[Case], extra: str = "") -> None:
    size = shutil.get_terminal_size((120, 40))
    width = size.columns
    print("\033[2J\033[H", end="")
    breach = LAST_FETCH.get("breach", sum(1 for c in cases if is_breached(c)))
    left = f"TAM CASES  {SSO_USERNAME}  {now_stamp()}"
    right = f"{len(cases)} open  {paint(str(breach) + ' BREACH', RED + BOLD) if breach else '0 BREACH'}"
    pad = max(1, width - len(re.sub(r"\033\[[0-9;]*m", "", left)) - len(re.sub(r"\033\[[0-9;]*m", "", right)))
    print(paint(left, BOLD, CYAN) + (" " * pad) + right)
    print(header_line(width))
    if not cases:
        print(paint("  (no cases match the current filter)", DIM))
        return
    for label, group in grouped_cases(cases):
        n_breach = sum(1 for c in group if is_breached(c))
        n_woc = (LAST_WOC.get(label) or {}).get("count", 0)
        heading = f"{label}  {len(group)} cases"
        if n_breach:
            heading += "  " + paint(f"{n_breach} BREACH", RED + BOLD)
        if n_woc:
            heading += "  " + paint(f"({n_woc} WoC hidden)", DIM)
        print(paint(heading, BOLD))
        if not group:
            print(paint("  (all open cases are waiting on customer)", DIM))
            continue
        for case in group:
            print(fmt_row(case, width))
    print()
    stats = (
        f"{LAST_FETCH.get('records', len(cases))} records  "
        f"{LAST_FETCH.get('calls', '?')} API calls  "
        f"{LAST_FETCH.get('seconds', '?')}s  "
        f"updated {LAST_FETCH.get('updated', '')}  {extra}"
    )
    print(paint(stats, DIM))
    print(paint("● replied  ◆ TAM contact  ✱ co-TAM  ○ other  grey = new unclaimed  red CASE# = SBT breached  ▲ escalated  ▪ Jira", DIM))


# ── Webhooks ─────────────────────────────────────────────────────────────────

def _post_webhook(url: str, payload: dict) -> None:
    raw = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=raw,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except Exception as exc:
        print(f"Webhook send failed: {exc}", file=sys.stderr)


def google_chat_card(case: Case, event: str, old_status: str = "") -> dict:
    status_line = case.status_raw or case.status
    if old_status and old_status != status_line:
        status_widget = {
            "decoratedText": {
                "topLabel": "Status",
                "text": f"<font color=\"#8B0000\">{old_status}</font> → "
                f"<font color=\"#228B22\">{status_line}</font>",
            }
        }
    else:
        status_widget = {
            "decoratedText": {"topLabel": "Status", "text": status_line}
        }
    return {
        "cardsV2": [
            {
                "cardId": case.number,
                "card": {
                    "header": {
                        "title": f"{case.number} — {event}",
                        "subtitle": case.subject or "",
                    },
                    "sections": [
                        {
                            "widgets": [
                                {
                                    "decoratedText": {
                                        "topLabel": "Account",
                                        "text": case.account_label,
                                    }
                                },
                                status_widget,
                                {
                                    "decoratedText": {
                                        "topLabel": "Severity",
                                        "text": case.severity,
                                    }
                                },
                                {
                                    "buttonList": {
                                        "buttons": [
                                            {
                                                "text": "Case in Portal",
                                                "onClick": {
                                                    "openLink": {"url": case.portal_url()}
                                                },
                                            },
                                            {
                                                "text": "Case in SFDC",
                                                "onClick": {
                                                    "openLink": {"url": case.sfdc_url()}
                                                },
                                            },
                                        ]
                                    }
                                },
                            ]
                        }
                    ],
                },
            }
        ]
    }


def slack_payload(case: Case, event: str, old_status: str = "") -> dict:
    status_line = case.status_raw or case.status
    if old_status and old_status != status_line:
        status_md = f"~{old_status}~ → *{status_line}*"
    else:
        status_md = status_line
    return {
        "text": f"{case.number} — {event}",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{case.number} — {event}"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{case.subject or '(no subject)'}*"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Account*\n{case.account_label}"},
                    {"type": "mrkdwn", "text": f"*Status*\n{status_md}"},
                    {"type": "mrkdwn", "text": f"*Severity*\n{case.severity}"},
                ],
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Case in Portal"},
                        "url": case.portal_url(),
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Case in SFDC"},
                        "url": case.sfdc_url(),
                    },
                ],
            },
        ],
    }


def send_alert(
    case: Case,
    event: str,
    webhook_url: str,
    webhook_type: str,
    old_status: str = "",
) -> None:
    if not webhook_url:
        return
    if webhook_type == "slack":
        payload = slack_payload(case, event, old_status)
    else:
        payload = google_chat_card(case, event, old_status)
    _post_webhook(webhook_url, payload)


def webhook_send(webhook_url: str, payload: dict) -> None:
    _post_webhook(webhook_url, payload)


def webhook_notify_case(
    webhook_url: str,
    webhook_type: str,
    case: Case,
    event_label: str,
    old_status: str = "",
) -> None:
    send_alert(case, event_label, webhook_url, webhook_type, old_status=old_status)


def route_case_event(case: Case, event_type: str, contacts: Dict[str, List[str]], routes: dict):
    if not routes:
        return None
    route = routes.get(case.account_label)
    if not route or not route.get("webhook_url"):
        return None
    if event_type not in route.get("events", []):
        return None
    if not is_tam_contact(case.contact, case.account_label, contacts):
        return None
    return route


def detect_and_alert(
    previous: Dict[str, Case],
    current: List[Case],
    webhook_url: Optional[str],
    webhook_type: str,
    backup_ids: Optional[Set[str]] = None,
) -> Dict[str, Case]:
    now = {c.number: c for c in current}
    merged = dict(previous)
    merged.update(now)
    if not previous:
        return merged
    contacts = load_contacts()
    routes = load_slack_routes()
    backup_ids = backup_ids or set()

    def _backup_ok(case: Case) -> bool:
        if case.id not in backup_ids and not case.is_backup:
            return True
        return is_tam_contact(case.contact, case.account_label, contacts)

    for number, case in now.items():
        if not _backup_ok(case):
            continue
        if number not in previous:
            label = f"New Case from {case.contact or 'unknown'}"
            send_alert(case, label, webhook_url or "", webhook_type)
            route = route_case_event(case, "new_case", contacts, routes)
            if route:
                send_alert(case, label, route["webhook_url"], "slack")
            continue
        old = previous[number]
        if old.status_raw != case.status_raw:
            send_alert(
                case,
                "Status change",
                webhook_url or "",
                webhook_type,
                old_status=old.status_raw,
            )
        was_breach = is_breached(old)
        now_breach = is_breached(case)
        if now_breach and not was_breach:
            route = route_case_event(case, "breach", contacts, routes)
            if route:
                send_alert(case, "Case Moved Into Breach", route["webhook_url"], "slack")
    return merged


# ── CLI ──────────────────────────────────────────────────────────────────────

def resolve_product_filter(args: argparse.Namespace) -> Optional[List[str]]:
    if getattr(args, "rhel", False):
        return list(RHEL_PRODUCTS)
    if getattr(args, "ocp", False):
        return list(OCP_PRODUCTS)
    if getattr(args, "mw", False):
        return list(MW_PRODUCTS)
    if getattr(args, "aap", False):
        return list(AAP_PRODUCTS)
    if args.product:
        return list(args.product)
    return None


def build_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--product", nargs="+", help="Filter by product name(s)")
    parser.add_argument("--rhel", action="store_true", help="Filter to RHEL + Satellite")
    parser.add_argument("--ocp", action="store_true", help="Filter to OpenShift")
    parser.add_argument("--mw", action="store_true", help="Filter to Middleware")
    parser.add_argument("--aap", action="store_true", help="Filter to AAP")
    parser.add_argument("--sbr", help="Post-fetch filter on SBR_Group__c")
    parser.add_argument(
        "--mine",
        action="store_true",
        help="Only replied, TAM contacts, co-TAM, and new unclaimed",
    )
    parser.add_argument(
        "--worh",
        action="store_true",
        help="Only Waiting on Red Hat / In Progress",
    )
    parser.add_argument(
        "--breach",
        action="store_true",
        help="Compatibility flag; the main list always highlights SBT-breached cases, including WoC",
    )
    parser.add_argument("--watch", action="store_true", help="Auto-refresh (terminal view)")
    parser.add_argument("--interval", type=int, default=300, help="Refresh interval seconds")
    parser.add_argument("--webhook", help="Webhook URL (Google Chat or Slack)")
    parser.add_argument(
        "--webhook-type",
        choices=["google", "slack"],
        default="google",
        help="Webhook payload format",
    )
    parser.add_argument("--save-webhook", action="store_true", help="Persist webhook to tam_config.json")
    parser.add_argument("--debug-status", action="store_true", help="Print unique Status values")
    parser.add_argument("--debug-sbr", action="store_true", help="Print unique SBR_Group__c values")
    parser.add_argument("--debug", action="store_true", help="Print GraphQL errors")
    return parser


def apply_saved_webhook(args: argparse.Namespace) -> Tuple[Optional[str], str]:
    if args.save_webhook and args.webhook:
        save_webhook(args.webhook, args.webhook_type)
    cfg = load_config()
    url = args.webhook or cfg.get("webhook")
    wtype = args.webhook_type if args.webhook else cfg.get("webhook_type", args.webhook_type)
    return url, wtype


def debug_uniques(cases: List[Case], args: argparse.Namespace) -> None:
    if args.debug_status:
        values = sorted({c.status_raw or "(empty)" for c in cases})
        print("Unique Status values:", file=sys.stderr)
        for v in values:
            print(f"  {v}", file=sys.stderr)
    if args.debug_sbr:
        values = sorted({c.sbr or "(empty)" for c in cases})
        print("Unique SBR_Group__c values:", file=sys.stderr)
        for v in values:
            print(f"  {v}", file=sys.stderr)


def load_cases(args: argparse.Namespace) -> List[Case]:
    cases, failed = fetch_all_cases(
        product_filter=resolve_product_filter(args),
        sbr_filter=args.sbr,
        debug=args.debug,
    )
    commented, _last, all_authors = apply_comment_activity(cases)
    try:
        esc, esc_failed = fetch_escalations(debug=args.debug)
    except Exception:
        esc, esc_failed = [], ["escalations"]
    apply_escalation_marks(cases, escalation_parent_ids(esc))
    if args.mine:
        contacts = load_contacts()
        cases = [c for c in cases if is_mine_visible(c, commented, all_authors, contacts)]
    if getattr(args, "worh", False):
        cases = [c for c in cases if c.status in ("WoRH", "InPrg")]
    LAST_FETCH["failed"] = list(failed) + list(esc_failed)
    LAST_FETCH["records"] = len(cases)
    LAST_FETCH["breach"] = sum(1 for c in cases if is_breached(c))
    return cases


def load_tui_board(
    args: argparse.Namespace,
    prev_esc_ids: Optional[Set[str]] = None,
    prev_parents: Optional[List[Case]] = None,
) -> dict:
    product = resolve_product_filter(args)
    sbr = args.sbr
    debug = args.debug
    cases, failed = fetch_all_cases(product_filter=product, sbr_filter=sbr, debug=debug)
    backup, backup_failed = fetch_backup_accounts_cases(
        product_filter=product, sbr_filter=sbr, debug=debug
    )
    all_open = list(cases) + list(backup)
    commented, last_comment, all_authors = apply_comment_activity(all_open)
    try:
        esc, esc_failed = fetch_escalations(debug=debug)
    except Exception:
        esc, esc_failed = [], ["escalations"]
    if esc_failed and prev_esc_ids:
        esc_ids = set(prev_esc_ids)
    else:
        esc_ids = escalation_parent_ids(esc)
    apply_escalation_marks(all_open, esc_ids)
    known_ids = {c.id for c in all_open}
    missing = [i for i in esc_ids if i not in known_ids]
    parent_nodes, par_failed = fetch_cases_by_ids(missing, debug=debug)
    if par_failed and prev_parents:
        parent_cases = [c for c in prev_parents if c.id in esc_ids]
    else:
        parent_cases = [c for c in nodes_to_cases(parent_nodes) if not is_closed(c)]
    prior_jiras = dict(LAST_CASE_JIRAS)
    _, _, parent_authors = apply_comment_activity(parent_cases)
    for cid, names in parent_authors.items():
        all_authors.setdefault(cid, [])
        all_authors[cid] = list(dict.fromkeys(list(all_authors[cid]) + list(names)))
    for cid, keys in LAST_CASE_JIRAS.items():
        prior_jiras.setdefault(cid, [])
        prior_jiras[cid] = list(dict.fromkeys(list(prior_jiras[cid]) + list(keys)))
    LAST_CASE_JIRAS.clear()
    LAST_CASE_JIRAS.update(prior_jiras)
    apply_escalation_marks(parent_cases, esc_ids)
    contacts = load_contacts()
    extra = [c for c in parent_cases if c.id not in known_ids]
    mine_parents = [
        c for c in extra if is_mine_visible(c, commented, all_authors, contacts)
    ]
    other_parents = [
        c for c in extra if not is_mine_visible(c, commented, all_authors, contacts)
    ]
    if args.mine:
        keep = lambda c: is_mine_visible(c, commented, all_authors, contacts)
        cases = [c for c in cases if keep(c)]
        backup = [c for c in backup if keep(c)]
    if getattr(args, "worh", False):
        cases = [c for c in cases if c.status in ("WoRH", "InPrg")]
        backup = [c for c in backup if c.status in ("WoRH", "InPrg")]
    LAST_FETCH["records"] = len(cases) + len(backup)
    LAST_FETCH["breach"] = sum(1 for c in cases + backup if is_breached(c))
    LAST_FETCH["failed"] = list(failed) + list(backup_failed) + list(esc_failed)
    LAST_FETCH["calls"] = API_CALLS
    return {
        "cases": cases,
        "backup": backup,
        "mine_parents": mine_parents,
        "other_parents": other_parents,
        "esc_ids": esc_ids,
        "parent_cases": parent_cases,
        "commented": commented,
        "last_comment": last_comment,
        "all_authors": all_authors,
        "case_jiras": dict(LAST_CASE_JIRAS),
        "failed": LAST_FETCH["failed"],
    }


# ── Latency history ──────────────────────────────────────────────────────────

def measure_path_latency(host: str = "graphql.redhat.com", port: int = 443, timeout: int = 5):
    """Bare TCP connect to the API host. Returns milliseconds, or None."""
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
        return round((time.time() - t0) * 1000, 1)
    except Exception:
        return None


def log_latency_sample(elapsed, n_cases=0, n_api=0, n_failed=0, path_ms=None):
    """Never raises — a logging failure must not take the refresh down."""
    try:
        line = json.dumps(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "elapsed": round(float(elapsed), 2),
                "cases": n_cases,
                "api_calls": n_api,
                "failed": n_failed,
                "path_ms": path_ms,
            }
        )
        with LATENCY_LOG_PATH.open("a") as handle:
            handle.write(line + "\n")
    except Exception:
        return
    try:
        if os.urandom(1)[0] == 0 and LATENCY_LOG_PATH.exists():
            lines = LATENCY_LOG_PATH.read_text().splitlines()
            if len(lines) > LATENCY_LOG_MAX_LINES:
                LATENCY_LOG_PATH.write_text(
                    "\n".join(lines[-LATENCY_LOG_MAX_LINES:]) + "\n"
                )
    except Exception:
        return


def load_latency_history(limit: int = 2000) -> List[dict]:
    if not LATENCY_LOG_PATH.exists():
        return []
    out = []
    try:
        lines = LATENCY_LOG_PATH.read_text().splitlines()
    except Exception:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out[-limit:]


def latency_gradient_colour(pct: float) -> str:
    pct = max(0.0, min(1.0, float(pct)))
    r = int(0x33 + (0xFF - 0x33) * pct)
    g = int(0xCC + (0x33 - 0xCC) * pct)
    b = int(0x33 + (0x33 - 0x33) * pct)
    return f"#{r:02x}{g:02x}{b:02x}"


def latency_relative_colour(values: Sequence[float], current: float) -> str:
    nums = [float(v) for v in values if v is not None]
    if len(nums) < 2 or max(nums) <= min(nums):
        return "#aaaaaa"
    lo, hi = min(nums), max(nums)
    return latency_gradient_colour((float(current) - lo) / (hi - lo))


def latency_trend_arrow(elapsed: float, prev_samples: Sequence[float]) -> Tuple[str, str]:
    if not prev_samples:
        return "", "#aaaaaa"
    avg = sum(prev_samples) / len(prev_samples)
    delta = (elapsed - avg) / avg if avg > 0 else 0
    if delta > 0.10:
        return "↑", "#ff3333"
    if delta < -0.10:
        return "↓", "#33cc33"
    return "→", "#aaaaaa"


def latency_p50_p90(values: Sequence[float]) -> Tuple[float, float]:
    nums = sorted(float(v) for v in values if v is not None)
    if not nums:
        return 0.0, 0.0
    def _pct(p):
        idx = min(len(nums) - 1, max(0, int(round((len(nums) - 1) * p))))
        return nums[idx]
    return _pct(0.50), _pct(0.90)


def same_local_day(ts: str, day) -> bool:
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if parsed.tzinfo:
            parsed = parsed.astimezone()
        return parsed.date() == day
    except Exception:
        return False


# ── Jira Cloud ───────────────────────────────────────────────────────────────

def jira_token_present() -> bool:
    try:
        return JIRA_TOKEN_PATH.exists() and bool(JIRA_TOKEN_PATH.read_text().strip())
    except Exception:
        return False


def _jira_auth_header() -> dict:
    cfg = load_config()
    email = cfg.get("jira_email") or f"{SSO_USERNAME.replace('rhn-support-', '')}@redhat.com"
    try:
        token = JIRA_TOKEN_PATH.read_text().strip()
    except Exception:
        token = ""
    b64 = base64.b64encode(f"{email}:{token}".encode()).decode()
    return {"Authorization": f"Basic {b64}", "Accept": "application/json"}


def fetch_jira_issue(key: str) -> Optional[dict]:
    url = (
        f"{JIRA_BASE}/rest/api/3/issue/{urllib.parse.quote(key)}"
        "?fields=status,summary,updated,comment&expand=changelog"
    )
    try:
        req = urllib.request.Request(url, headers=_jira_auth_header())
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        print(f"  jira {key}: {exc}", file=sys.stderr)
        return None


def fetch_watched_jira_keys() -> List[str]:
    """POST /rest/api/3/search/jql — GET /search is HTTP 410 Gone."""
    keys: List[str] = []
    token = None
    for _page in range(20):
        payload: dict = {
            "jql": "watcher = currentUser()",
            "maxResults": 100,
            "fields": ["key"],
        }
        if token:
            payload["nextPageToken"] = token
        try:
            req = urllib.request.Request(
                f"{JIRA_BASE}/rest/api/3/search/jql",
                data=json.dumps(payload).encode(),
                headers={**_jira_auth_header(), "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
        except Exception as exc:
            print(f"  jira watched: {exc}", file=sys.stderr)
            break
        for issue in data.get("issues") or []:
            key = issue.get("key")
            if key:
                keys.append(key)
        if data.get("isLast") or not data.get("nextPageToken"):
            break
        token = data.get("nextPageToken")
    return keys


def jira_last_status_change(issue_data: dict) -> Optional[str]:
    latest = None
    for hist in ((issue_data or {}).get("changelog") or {}).get("histories") or []:
        if any((item.get("field") or "").lower() == "status" for item in hist.get("items") or []):
            created = hist.get("created")
            if created and (latest is None or created > latest):
                latest = created
    return latest


def jira_last_activity(issue_data: dict) -> str:
    status_ts = jira_last_status_change(issue_data) or ""
    updated = ((issue_data or {}).get("fields") or {}).get("updated") or ""
    return max(status_ts, updated)


def jira_comments_since(issue_data: dict, cutoff_iso: Optional[str]) -> List[Tuple[str, str]]:
    out = []
    comments = (((issue_data or {}).get("fields") or {}).get("comment") or {}).get("comments") or []
    for comment in comments:
        created = comment.get("created") or ""
        if cutoff_iso and created <= cutoff_iso:
            continue
        author = ((comment.get("author") or {}).get("displayName")) or ""
        out.append((created, author))
    return out


def jira_change_summary(issue_data: dict, cutoff_iso: Optional[str]) -> str:
    parts = []
    transition = jira_last_status_transition(issue_data, cutoff_iso)
    if transition:
        parts.append(f"status: {transition[0]} → {transition[1]}")
    comments = jira_comments_since(issue_data, cutoff_iso)
    if comments:
        latest = comments[-1][1] or "unknown"
        parts.append(f"{len(comments)} new comment{'s' if len(comments) != 1 else ''}, latest by {latest}")
    return "; ".join(parts) or "updated"


def jira_last_status_transition(
    issue_data: dict, cutoff_iso: Optional[str]
) -> Optional[Tuple[str, str]]:
    latest = None
    for hist in ((issue_data or {}).get("changelog") or {}).get("histories") or []:
        created = hist.get("created") or ""
        if cutoff_iso and created <= cutoff_iso:
            continue
        for item in hist.get("items") or []:
            if (item.get("field") or "").lower() != "status":
                continue
            if latest is None or created > latest[0]:
                latest = (created, item.get("fromString") or "?", item.get("toString") or "?")
    if not latest:
        return None
    return (latest[1], latest[2])


def jira_browse_url(key: str) -> str:
    return JIRA_BROWSE_URL.format(key=key)


def jira_is_closed(issue_data: Optional[dict]) -> bool:
    name = ((((issue_data or {}).get("fields") or {}).get("status") or {}).get("name") or "")
    return name.strip().lower() in {"closed", "done", "resolved", "cancelled"}


def _load_json_cache(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _save_json_cache(path: Path, data) -> None:
    try:
        path.write_text(json.dumps(data, indent=2) + "\n")
        os.chmod(path, 0o600)
    except Exception:
        return


def load_jira_alert_cache() -> dict:
    data = _load_json_cache(JIRA_ALERT_CACHE_PATH, {})
    return data if isinstance(data, dict) else {}


def save_jira_alert_cache(data: dict) -> None:
    _save_json_cache(JIRA_ALERT_CACHE_PATH, data)


def load_id_set_cache(path: Path) -> Set[str]:
    data = _load_json_cache(path, [])
    if isinstance(data, list):
        return set(str(x) for x in data)
    if isinstance(data, dict):
        return set(str(x) for x in data.keys())
    return set()


def save_id_set_cache(path: Path, ids: Set[str]) -> None:
    _save_json_cache(path, sorted(ids))


def webhook_notify_jira(
    webhook_url: str,
    webhook_type: str,
    key: str,
    summary: str,
    change: str,
    case: Optional[Case] = None,
) -> None:
    if not webhook_url:
        return
    title = f"🔷 {key}"
    url = jira_browse_url(key)
    if webhook_type == "slack":
        fields = [{"type": "mrkdwn", "text": f"*Change*\n{change}"}]
        if case:
            fields.append({"type": "mrkdwn", "text": f"*Case*\n{case.number}"})
        payload = {
            "text": f"{title} — {summary}",
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": f"{title} — {summary}"}},
                {"type": "section", "fields": fields},
                {
                    "type": "actions",
                    "elements": [
                        {"type": "button", "text": {"type": "plain_text", "text": "Open Jira"}, "url": url}
                    ],
                },
            ],
        }
    else:
        widgets = [
            {"decoratedText": {"topLabel": "Change", "text": change}},
            {"buttonList": {"buttons": [{"text": "Open Jira", "onClick": {"openLink": {"url": url}}}]}},
        ]
        payload = {
            "cardsV2": [
                {
                    "cardId": key,
                    "card": {
                        "header": {"title": title, "subtitle": summary},
                        "sections": [{"widgets": widgets}],
                    },
                }
            ]
        }
    _post_webhook(webhook_url, payload)


def webhook_notify_integrity(
    webhook_url: str,
    webhook_type: str,
    case: Case,
    event: str,
    detail: str,
    accent: str,
    icon: str,
) -> None:
    if not webhook_url:
        return
    title = f"{icon} {case.number} — {event}"
    if webhook_type == "slack":
        payload = {
            "text": title,
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": title}},
                {"type": "section", "text": {"type": "mrkdwn", "text": detail}},
                {
                    "type": "actions",
                    "elements": [
                        {"type": "button", "text": {"type": "plain_text", "text": "Case in Portal"}, "url": case.portal_url()},
                        {"type": "button", "text": {"type": "plain_text", "text": "Case in SFDC"}, "url": case.sfdc_url()},
                    ],
                },
            ],
        }
    else:
        payload = {
            "cardsV2": [
                {
                    "cardId": f"{case.number}-{event}",
                    "card": {
                        "header": {"title": title, "subtitle": case.subject or ""},
                        "sections": [
                            {
                                "widgets": [
                                    {"decoratedText": {"topLabel": "Detail", "text": detail}},
                                    {
                                        "buttonList": {
                                            "buttons": [
                                                {"text": "Case in Portal", "onClick": {"openLink": {"url": case.portal_url()}}},
                                                {"text": "Case in SFDC", "onClick": {"openLink": {"url": case.sfdc_url()}}},
                                            ]
                                        }
                                    },
                                ]
                            }
                        ],
                    },
                }
            ]
        }
    _post_webhook(webhook_url, payload)


# ── Case history / integrity ─────────────────────────────────────────────────

_STATUS_VOCAB = {
    "waiting on red hat",
    "in progress",
    "waiting on customer action required",
    "waiting on customer solution provided",
    "waiting on customer",
    "waiting on engineering",
    "waiting on collaboration",
    "waiting on translation",
    "waiting on documentation",
    "waiting on 3rd party",
    "waiting on business unit",
    "unassigned",
    "closed",
    "new",
    "waiting on contributor",
    "waiting on product management",
    "waiting on quality engineering",
    "waiting on partner",
}


def _is_sf_id(value: str) -> bool:
    return bool(re.fullmatch(r"[a-zA-Z0-9]{15}([a-zA-Z0-9]{3})?", value or ""))


def _history_string(field) -> str:
    if field is None:
        return ""
    if isinstance(field, dict):
        if "value" in field:
            return str(field.get("value") or "")
        inner = field.get("RedHatSupportStringValue") or field
        return str(inner.get("value") or "")
    return str(field)


def _history_node_row(node: dict, fallback_cid: str = "") -> dict:
    cid = v(node.get("CaseId")) or fallback_cid
    return {
        "id": node.get("Id") or "",
        "case_id": str(cid) if cid else "",
        "created": v(node.get("CreatedDate")) or "",
        "old": _history_string(node.get("OldValue")),
        "new": _history_string(node.get("NewValue")),
    }


def fetch_case_histories(case_ids: Sequence[str]) -> Dict[str, List[dict]]:
    """Batch CaseHistory via CaseId in:[...], 10 ids, full pagination (Step 12)."""
    out: Dict[str, List[dict]] = defaultdict(list)
    ids = [cid for cid in case_ids if cid]
    batch_size = 10
    page_size = 100
    max_pages = 25
    for i in range(0, len(ids), batch_size):
        if i:
            time.sleep(BATCH_SLEEP)
        batch = ids[i : i + batch_size]
        id_list = ", ".join(f'"{cid}"' for cid in batch)
        fallback = batch[0] if len(batch) == 1 else ""
        cursor = None
        for page in range(max_pages):
            if page:
                time.sleep(PAGE_SLEEP)
            after = f'after: "{cursor}"' if cursor else ""
            query = f"""
query CaseHistories {{
  redhat_support_uiapi {{
    query {{
      RedHatSupportCaseHistory(
        first: {page_size}
        {after}
        where: {{ CaseId: {{ in: [{id_list}] }} }}
      ) {{
        pageInfo {{ hasNextPage endCursor }}
        edges {{ node {{
          Id
          CaseId {{ value }}
          CreatedDate {{ value }}
          OldValue {{ ... on RedHatSupportStringValue {{ value }} }}
          NewValue {{ ... on RedHatSupportStringValue {{ value }} }}
        }} }}
      }}
    }}
  }}
}}
"""
            try:
                data = gql(query, operation_name="CaseHistories")
            except Exception as exc:
                print(f"  histories batch: {exc}", file=sys.stderr)
                break
            payload = (
                ((data.get("data") or {}).get("redhat_support_uiapi") or {})
                .get("query", {})
                .get("RedHatSupportCaseHistory")
                or {}
            )
            edges = payload.get("edges") or []
            for edge in edges:
                row = _history_node_row(edge.get("node") or {}, fallback)
                if row["case_id"]:
                    out[row["case_id"]].append(row)
            page_info = payload.get("pageInfo") or {}
            if not page_info.get("hasNextPage") or not edges:
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break
    return dict(out)


def fetch_case_history(case_id: str) -> List[dict]:
    if not case_id:
        return []
    return fetch_case_histories([case_id]).get(case_id, [])


def fetch_case_history_actors_batch(case_ids: Sequence[str]) -> Dict[str, str]:
    """CreatedBy-only query — never mixed with Old/New (zero-row quirk)."""
    out: Dict[str, str] = {}
    ids = [cid for cid in case_ids if cid]
    batch_size = 10
    page_size = 100
    max_pages = 25
    for i in range(0, len(ids), batch_size):
        if i:
            time.sleep(BATCH_SLEEP)
        batch = ids[i : i + batch_size]
        id_list = ", ".join(f'"{cid}"' for cid in batch)
        cursor = None
        for page in range(max_pages):
            if page:
                time.sleep(PAGE_SLEEP)
            after = f'after: "{cursor}"' if cursor else ""
            query = f"""
query CaseHistoryActors {{
  redhat_support_uiapi {{
    query {{
      RedHatSupportCaseHistory(
        first: {page_size}
        {after}
        where: {{ CaseId: {{ in: [{id_list}] }} }}
      ) {{
        pageInfo {{ hasNextPage endCursor }}
        edges {{ node {{
          Id
          CreatedBy {{ Name {{ value }} }}
        }} }}
      }}
    }}
  }}
}}
"""
            try:
                data = gql(query, operation_name="CaseHistoryActors")
            except Exception:
                break
            payload = (
                ((data.get("data") or {}).get("redhat_support_uiapi") or {})
                .get("query", {})
                .get("RedHatSupportCaseHistory")
                or {}
            )
            edges = payload.get("edges") or []
            for edge in edges:
                node = edge.get("node") or {}
                hid = node.get("Id")
                name = v((node.get("CreatedBy") or {}).get("Name"))
                if hid and name:
                    out[hid] = str(name)
            page_info = payload.get("pageInfo") or {}
            if not page_info.get("hasNextPage") or not edges:
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break
    return out


def fetch_case_history_actors(case_id: str) -> Dict[str, str]:
    return fetch_case_history_actors_batch([case_id]) if case_id else {}


def fetch_case_comment_timelines(case_ids: Sequence[str]) -> Dict[str, List[dict]]:
    """Same 10-id batch + pagination as comment activity (Step 12)."""
    out: Dict[str, List[dict]] = defaultdict(list)
    ids = [cid for cid in case_ids if cid]
    batch_size = 10
    page_size = 100
    max_pages = 25
    for i in range(0, len(ids), batch_size):
        if i:
            time.sleep(BATCH_SLEEP)
        batch = ids[i : i + batch_size]
        id_list = ", ".join(f'"{cid}"' for cid in batch)
        fallback = batch[0] if len(batch) == 1 else ""
        cursor = None
        for page in range(max_pages):
            if page:
                time.sleep(PAGE_SLEEP)
            after = f'after: "{cursor}"' if cursor else ""
            query = f"""
query CaseCommentTimelines {{
  redhat_support_uiapi {{
    query {{
      RedHatSupportCaseComment__c(
        first: {page_size}
        {after}
        where: {{ Case__c: {{ in: [{id_list}] }} }}
      ) {{
        pageInfo {{ hasNextPage endCursor }}
        edges {{ node {{
          Case__c {{ value }}
          CreatedDate {{ value }}
          LastModifiedDate {{ value }}
        }} }}
      }}
    }}
  }}
}}
"""
            try:
                data = gql(query, operation_name="CaseCommentTimelines")
            except Exception:
                break
            payload = (
                ((data.get("data") or {}).get("redhat_support_uiapi") or {})
                .get("query", {})
                .get("RedHatSupportCaseComment__c")
                or {}
            )
            edges = payload.get("edges") or []
            for edge in edges:
                node = edge.get("node") or {}
                cid = v(node.get("Case__c")) or fallback
                if not cid:
                    continue
                out[str(cid)].append(
                    {
                        "created": v(node.get("CreatedDate")) or "",
                        "modified": v(node.get("LastModifiedDate")) or "",
                    }
                )
            page_info = payload.get("pageInfo") or {}
            if not page_info.get("hasNextPage") or not edges:
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break
    return dict(out)


def fetch_case_comment_timeline(case_id: str) -> List[dict]:
    if not case_id:
        return []
    return fetch_case_comment_timelines([case_id]).get(case_id, [])


def _classify_case_history(rows: Sequence[dict]) -> List[dict]:
    classified = []
    for row in rows:
        old, new = row.get("old") or "", row.get("new") or ""
        if _is_sf_id(old) or _is_sf_id(new):
            kind = "id"
        elif old.lower() in _STATUS_VOCAB or new.lower() in _STATUS_VOCAB:
            kind = "status"
        else:
            kind = "owner"
        classified.append({**row, "kind": kind})
    return classified


def _is_system_actor(name: str) -> bool:
    n = (name or "").lower()
    return any(token in n for token in ("migration", "workato", "integration"))


def is_rfe_subject(subject: str) -> bool:
    return "[rfe]" in (subject or "").lower()


def detect_owner_reversion(rows: Sequence[dict]) -> List[dict]:
    classified = _classify_case_history(rows)
    by_ts: Dict[str, List[dict]] = defaultdict(list)
    for row in classified:
        by_ts[row.get("created") or ""].append(row)
    flags = []
    for ts, group in by_ts.items():
        if any(r["kind"] == "status" for r in group):
            continue
        id_rows = [r for r in group if r["kind"] == "id"]
        name_rows = [r for r in group if r["kind"] == "owner"]
        for id_row in id_rows:
            was_user = (id_row.get("old") or "").startswith("005")
            now_group = (id_row.get("new") or "").startswith("00G")
            if was_user and now_group:
                name_row = name_rows[0] if name_rows else id_row
                flags.append({**name_row, "kind": "owner_reversion", "created": ts})
    return flags


def _hours_apart(a: str, b: str) -> Optional[float]:
    try:
        da = datetime.fromisoformat(a.replace("Z", "+00:00"))
        db = datetime.fromisoformat(b.replace("Z", "+00:00"))
        return abs((da - db).total_seconds()) / 3600.0
    except Exception:
        return None


def detect_status_gaming(rows: Sequence[dict], comments: Sequence[dict], subject: str = "") -> List[dict]:
    if is_rfe_subject(subject):
        return []
    classified = [r for r in _classify_case_history(rows) if r["kind"] == "status"]
    flags = []
    waiting = lambda s: (s or "").lower().startswith("waiting on")

    def _near_created(ts: str) -> bool:
        for comment in comments:
            gap = _hours_apart(ts, comment.get("created") or "")
            if gap is not None and gap <= CASE_HISTORY_WINDOW_HOURS:
                return True
        return False

    def _near_edited(ts: str) -> bool:
        for comment in comments:
            created_gap = _hours_apart(ts, comment.get("created") or "")
            modified_gap = _hours_apart(ts, comment.get("modified") or "")
            if modified_gap is not None and modified_gap <= CASE_HISTORY_WINDOW_HOURS:
                if created_gap is None or created_gap > CASE_HISTORY_WINDOW_HOURS:
                    return True
        return False

    for row in classified:
        ts = row.get("created") or ""
        if (row.get("new") or "").lower() == "in progress":
            continue
        if _near_created(ts):
            continue
        if _near_edited(ts):
            flags.append({**row, "kind": "edited_comment"})
        else:
            flags.append({**row, "kind": "no_comment"})

    for i, row in enumerate(classified):
        left = (row.get("old") or "")
        if not waiting(left) or left.lower() == "in progress":
            continue
        for later in classified[i + 1 :]:
            gap = _hours_apart(row.get("created") or "", later.get("created") or "")
            if gap is None or gap > CASE_ROUNDTRIP_HOURS:
                continue
            if (later.get("new") or "").lower() == left.lower():
                if not _near_created(row.get("created") or "") and not _near_created(later.get("created") or ""):
                    flags.append({**later, "kind": "roundtrip", "old": left, "new": later.get("new")})
                break
    return flags


def ansi_to_rich(code: str) -> str:
    return {
        RED: "red",
        YELLOW: "yellow",
        GREEN: "green",
        GREY: "#555555",
        DIM: "#555555",
        CYAN: "cyan",
        MAGENTA: "magenta",
        WHITE: "white",
    }.get(code, "white")


def sev_num(priority: str) -> int:
    m = re.match(r"\s*([1-4])", priority or "")
    return int(m.group(1)) if m else 0


def open_portal(case: Case) -> None:
    webbrowser.open(case.portal_url())


def open_sfdc(case: Case) -> None:
    webbrowser.open(case.sfdc_url())


def main() -> None:
    parser = build_parser("TAM case dashboard (terminal)")
    args = parser.parse_args()
    webhook_url, webhook_type = apply_saved_webhook(args)

    previous: Dict[str, Case] = {}
    while True:
        started = time.time()
        try:
            cases = load_cases(args)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            if not args.watch:
                sys.exit(1)
            time.sleep(max(30, args.interval))
            continue
        debug_uniques(cases, args)
        previous = detect_and_alert(previous, cases, webhook_url, webhook_type)
        stamp = datetime.now().strftime("%H:%M:%S")
        extra = f"refreshed {stamp}  interval {args.interval}s" if args.watch else f"refreshed {stamp}"
        render_terminal(cases, extra=extra)
        if not args.watch:
            return
        elapsed = time.time() - started
        time.sleep(max(1, args.interval - elapsed))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        sys.exit(0)
