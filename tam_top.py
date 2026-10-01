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
TOKEN_PATH = Path.home() / ".rh_offline_token"
TOKEN_CACHE_PATH = Path.home() / ".rh_access_token_cache"

SSO_TOKEN_URL = (
    "https://sso.redhat.com/auth/realms/redhat-external"
    "/protocol/openid-connect/token"
)
GRAPHQL_URL = "https://graphql.redhat.com"
PORTAL_CASE = "https://access.redhat.com/support/cases/#/case/{number}"
SFDC_CASE = "https://redhatsupport.lightning.force.com/lightning/r/Case/{id}/view"

UNCLAIMED_DAYS = 14
PAGE_SIZE = 200
MAX_CASE_PAGES = 5
HTTP_TIMEOUT = 60
API_CALLS = 0
LAST_FETCH: Dict[str, Any] = {"calls": 0, "seconds": 0.0, "records": 0, "breach": 0}
LAST_WOC: Dict[str, Dict[str, Any]] = {}

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


def _http_json(url: str, payload: dict, token: str) -> dict:
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
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
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
    data = _http_json(GRAPHQL_URL, payload, token)
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


def fetch_comment_activity(case_ids: Sequence[str]) -> Tuple[Set[str], Dict[str, Tuple[str, str]]]:
    """Return (commented_by_me, last_comment) keyed by Salesforce Id.

    Batches of 10 + full cursor pagination so a chatty case cannot starve a quiet one.
    """
    case_ids = [cid for cid in case_ids if cid]
    commented_by_me: Set[str] = set()
    last_comment: Dict[str, Tuple[str, str]] = {}
    batch_size = 10
    page_size = 200
    max_pages = 25
    token = get_access_token()

    for i in range(0, len(case_ids), batch_size):
        batch = case_ids[i : i + batch_size]
        id_list = ", ".join(f'"{cid}"' for cid in batch)
        cursor = None
        for _page in range(max_pages):
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
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    token = get_access_token(force=True)
                    data = graphql(query, token, operation_name="CommentActivity")
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
                if isinstance(cid, str) is False:
                    cid = str(cid)
                if author and MY_NAME.lower() in str(author).lower():
                    commented_by_me.add(cid)
                created_s = str(created) if created else ""
                author_s = str(author) if author else ""
                if created_s and (cid not in last_comment or created_s > last_comment[cid][0]):
                    last_comment[cid] = (created_s, author_s)
            page_info = payload.get("pageInfo") or {}
            if not page_info.get("hasNextPage") or not edges:
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break
    return commented_by_me, last_comment


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
    for term in accounts:
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
    for term, label in source.items():
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
        if mine_only and not (case.mine or case.unclaimed or case.is_contact):
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


def apply_comment_activity(cases: Sequence[Case]) -> Tuple[Set[str], Dict[str, Tuple[str, str]]]:
    commented, last_comment = fetch_comment_activity([c.id for c in cases if c.id])
    for case in cases:
        if case.id in commented:
            case.mine = True
            case.marker = "●"
            case.unclaimed = False
        info = last_comment.get(case.id)
        if info:
            case.last_reply = parse_dt(info[0])
            case.last_reply_author = info[1] or ""
    return commented, last_comment


def apply_escalation_marks(cases: Sequence[Case], esc_ids: Set[str]) -> None:
    for case in cases:
        case.escalated = bool(case.id and case.id in esc_ids)


def is_my_case(case: Case, commented_ids: Set[str], contacts: Dict[str, List[str]]) -> bool:
    if case.id in commented_ids or case.mine:
        return True
    if is_tam_contact(case.contact, case.account_label, contacts):
        return True
    if name_is_mine(case.owner):
        return True
    return False


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


def grouped_cases(cases: Sequence[Case]) -> List[Tuple[str, List[Case]]]:
    buckets: Dict[str, List[Case]] = {}
    for case in cases:
        buckets.setdefault(case.account_label, []).append(case)
    for label in dict.fromkeys(ACCOUNTS.values()):
        info = LAST_WOC.get(label) or {}
        if label not in buckets and info.get("count"):
            buckets[label] = []
    return [(label, buckets[label]) for label in sorted(buckets, key=str.lower)]


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
    subject = case.subject or ""
    if case.escalated:
        subject = "▲ " + subject
        subject = paint(subject, RED, BOLD)
    prefix = f"{caseno} {sev} {st} {sbt} {owner} {reply} {contact} "
    remain = max(20, width - len(re.sub(r"\033\[[0-9;]*m", "", prefix)) - 1)
    if case.escalated:
        line = prefix + subject[: remain + 20]
    else:
        line = prefix + (case.subject or "")[:remain]
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
    print(paint("● replied  ◆ TAM contact  ○ other  grey = new unclaimed  red CASE# = SBT breached  ▲ = escalated", DIM))


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
        help="Only ● cases, ◆ TAM contacts, and new unclaimed",
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
    apply_comment_activity(cases)
    try:
        esc, esc_failed = fetch_escalations(debug=args.debug)
    except Exception:
        esc, esc_failed = [], ["escalations"]
    apply_escalation_marks(cases, escalation_parent_ids(esc))
    if args.mine:
        cases = [c for c in cases if c.mine or c.unclaimed or c.is_contact]
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
    commented, _last = apply_comment_activity(all_open)
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
    apply_comment_activity(parent_cases)
    apply_escalation_marks(parent_cases, esc_ids)
    contacts = load_contacts()
    extra = [c for c in parent_cases if c.id not in known_ids]
    mine_parents = [c for c in extra if is_my_case(c, commented, contacts)]
    other_parents = [c for c in extra if not is_my_case(c, commented, contacts)]
    if args.mine:
        keep = lambda c: c.mine or c.unclaimed or c.is_contact
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
        "failed": LAST_FETCH["failed"],
    }


def ansi_to_rich(code: str) -> str:
    return {
        RED: "red",
        YELLOW: "yellow",
        GREEN: "green",
        GREY: "#555555",
        DIM: "#555555",
        CYAN: "cyan",
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
