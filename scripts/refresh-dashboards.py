#!/usr/bin/env python3
"""
Refresh all client dashboards from Monday.com data.
Reads clients.yaml, queries Monday GraphQL API, renders Jinja2 templates,
and writes static HTML files. Designed to run in a GitHub Action on a schedule.

Usage:
    MONDAY_API_TOKEN=... python scripts/refresh-dashboards.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
import yaml
from jinja2 import Environment, FileSystemLoader

# ── Config ────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "_template"
CLIENTS_FILE = REPO_ROOT / "clients.yaml"

MONDAY_API_URL = "https://api.monday.com/v2"
MONDAY_BOARD_ID = 18397531209

# Column IDs (validated against live board)
COL_SUBITEM_DATE = "timerange_mm00qh4t"
COL_VISIBLE = "boolean_mm18mjmm"
COL_MILESTONE = "boolean_mm00qyep"
COL_TYPE = "dropdown_mm0ekqzg"
COL_SUBITEM_NOTES = "long_text_mm26ejw9"
COL_PHASE = "color_mkzzgws0"
COL_ITEM_NOTES = "long_text_mm285qea"
COL_SOW_HOURS = "numeric_mm0qrgth"


# ── Monday API ────────────────────────────────────────────────────

def monday_query(token: str, query: str, variables: dict | None = None) -> dict:
    """Execute a Monday.com GraphQL query."""
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(
        MONDAY_API_URL,
        json=payload,
        headers={
            "Authorization": token,
            "Content-Type": "application/json",
            "API-Version": "2024-01",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Monday API error: {data['errors']}")
    return data["data"]


def fetch_group_data(token: str, group_id: str) -> list[dict]:
    """Fetch all items + subitems for a Monday group on the board."""
    # Monday's items_page supports filtering by group via query_params
    query = """
    query ($boardId: [ID!]!, $groupIds: [String!]) {
        boards(ids: $boardId) {
            groups(ids: $groupIds) {
                items_page(limit: 500) {
                    items {
                        id
                        name
                        column_values {
                            id
                            text
                            value
                        }
                        subitems {
                            id
                            name
                            column_values {
                                id
                                text
                                value
                            }
                        }
                    }
                }
            }
        }
    }
    """
    data = monday_query(token, query, {
        "boardId": [str(MONDAY_BOARD_ID)],
        "groupIds": [group_id],
    })
    boards = data.get("boards", [])
    if not boards:
        return []
    groups = boards[0].get("groups", [])
    if not groups:
        return []
    return groups[0].get("items_page", {}).get("items", [])


# ── Data parsing ──────────────────────────────────────────────────

def get_col(item: dict, col_id: str) -> dict | None:
    """Get a column_values entry by ID."""
    for col in item.get("column_values", []):
        if col["id"] == col_id:
            return col
    return None


def get_col_text(item: dict, col_id: str) -> str:
    """Get column text value, empty string if missing."""
    col = get_col(item, col_id)
    return (col.get("text") or "").strip() if col else ""


def get_col_value_parsed(item: dict, col_id: str) -> dict | None:
    """Parse JSON value field from a column."""
    col = get_col(item, col_id)
    if not col or not col.get("value"):
        return None
    try:
        return json.loads(col["value"])
    except (json.JSONDecodeError, TypeError):
        return None


def is_boolean_true(item: dict, col_id: str) -> bool:
    """Check if a boolean/checkbox column is checked."""
    val = get_col_value_parsed(item, col_id)
    if val is None:
        return False
    # Monday boolean columns: {"checked": "true"} or {"checked": true}
    checked = val.get("checked", "")
    return str(checked).lower() == "true"


def is_done(item: dict) -> bool:
    """Check if item/subitem is done via the default status column."""
    # Monday's default status column has id "status" on subitems
    # Look for common done indicators
    for col in item.get("column_values", []):
        if col["id"] in ("status", "status_1"):
            text = (col.get("text") or "").strip().lower()
            if text in ("done", "complete", "completed"):
                return True
            # Also check the color index — green (1) often means done
            val = get_col_value_parsed(item, col["id"])
            if val and val.get("index") in (1,):
                return True
    return False


def parse_date(item: dict, col_id: str) -> date | None:
    """Parse a date or timeline column, returning the 'from' date."""
    val = get_col_value_parsed(item, col_id)
    if val:
        date_str = val.get("from") or val.get("date")
        if date_str:
            try:
                return date.fromisoformat(date_str[:10])
            except ValueError:
                pass
    # Fallback: try text
    text = get_col_text(item, col_id)
    if text:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            pass
    return None


def format_date(d: date | None) -> str:
    """Format a date as 'Mon DD' or 'Mon DD, YYYY' if not current year."""
    if not d:
        return "–"
    if d.year == date.today().year:
        return d.strftime("%b %-d")
    return d.strftime("%b %-d, %Y")


def format_date_long(d: date | None) -> str:
    """Format a date as 'Month DD, YYYY'."""
    if not d:
        return "–"
    return d.strftime("%B %-d, %Y")


def get_subitem_type(subitem: dict) -> str:
    """Get the Type dropdown value for a subitem."""
    text = get_col_text(subitem, COL_TYPE)
    return text


# ── Build workstream + milestone data ─────────────────────────────

class Subitem:
    def __init__(self, raw: dict):
        self.id = raw["id"]
        self.name = raw["name"]
        self.visible = is_boolean_true(raw, COL_VISIBLE)
        self.is_milestone = is_boolean_true(raw, COL_MILESTONE)
        self.done = is_done(raw)
        self.date = parse_date(raw, COL_SUBITEM_DATE)
        self.type = get_subitem_type(raw)
        self.notes = get_col_text(raw, COL_SUBITEM_NOTES)
        # Store raw status text for in-progress detection
        self._status_text = ""
        for col in raw.get("column_values", []):
            if col["id"] in ("status", "status_1"):
                self._status_text = (col.get("text") or "").strip()


class Workstream:
    def __init__(self, raw: dict):
        self.id = raw["id"]
        self.name = raw["name"]
        self.phase = get_col_text(raw, COL_PHASE)
        self.notes = get_col_text(raw, COL_ITEM_NOTES)
        self.sow_hours = get_col_text(raw, COL_SOW_HOURS)

        all_subs = [Subitem(s) for s in raw.get("subitems", [])]
        self.all_subitems = all_subs
        self.visible_subitems = [s for s in all_subs if s.visible]

    @property
    def status(self) -> str:
        """Derive workstream status from ALL subitems (not just visible).
        If any subitem is done or in-progress, the workstream is active."""
        subs = self.all_subitems
        if not subs:
            return "upcoming"
        done = [s for s in subs if s.done]
        if len(done) == len(subs):
            return "complete"
        if done:
            return "active"
        # Also check for in-progress via status column text
        for s_raw in subs:
            if not s_raw.done and s_raw._status_text in ("Working on it",):
                return "active"
        return "upcoming"

    @property
    def status_label(self) -> str:
        labels = {"complete": "Complete", "active": "In Progress", "upcoming": "Upcoming"}
        return labels[self.status]

    @property
    def window(self) -> str:
        """Derive a timeline window from subitem dates."""
        dates = sorted([s.date for s in self.all_subitems if s.date])
        if not dates:
            return ""
        first, last = dates[0], dates[-1]
        if first.month == last.month and first.year == last.year:
            return first.strftime("%b %Y")
        f = first.strftime("%b")
        l = last.strftime("%b %Y") if last.year != first.year else last.strftime("%b")
        return f"{f} – {l}"


# ── Phase / Deliverable tracker ───────────────────────────────────

def _auto_derive_phases(workstreams: list[Workstream]) -> list[str]:
    """Auto-derive phase order from workstreams by sorting on earliest subitem date."""
    SKIP_PHASES = {"N/A", "Project Management", "Consulting", ""}
    phase_earliest: dict[str, date | None] = {}

    for ws in workstreams:
        p = ws.phase
        if not p or p in SKIP_PHASES or not ws.visible_subitems:
            continue
        dates = sorted([s.date for s in ws.visible_subitems if s.date])
        earliest = dates[0] if dates else None
        if p not in phase_earliest or (earliest and (phase_earliest[p] is None or earliest < phase_earliest[p])):
            phase_earliest[p] = earliest

    # Sort by earliest date, nulls last
    return [p for p, _ in sorted(phase_earliest.items(), key=lambda x: x[1] or date.max)]


def compute_linear_phases(phase_names: list[str] | None, workstreams: list[Workstream],
                          is_closed: bool) -> list[dict]:
    """Compute phase states for a linear project.
    If phase_names is None/empty, auto-derive from Monday's Phase column."""
    if not phase_names:
        phase_names = _auto_derive_phases(workstreams)
        if phase_names:
            print(f"  Auto-derived phases: {' → '.join(phase_names)}")

    steps = []
    active_found = False

    for phase_name in phase_names:
        # Find workstreams tagged with this phase
        phase_ws = [ws for ws in workstreams if ws.phase == phase_name]
        # Only consider workstreams that have visible subitems
        phase_ws_with_subs = [ws for ws in phase_ws if ws.visible_subitems]

        if not phase_ws_with_subs:
            state = "complete" if is_closed else "upcoming"
            steps.append({"name": phase_name, "state": state, "window": ""})
            continue

        all_subs = [s for ws in phase_ws_with_subs for s in ws.visible_subitems]
        done_count = sum(1 for s in all_subs if s.done)
        total = len(all_subs)
        incomplete = total - done_count

        if done_count >= 1 and incomplete == 0:
            steps.append({"name": phase_name, "state": "complete", "window": ""})
        elif not active_found and incomplete > 0 and not is_closed:
            steps.append({"name": phase_name, "state": "active", "window": ""})
            active_found = True
        else:
            state = "complete" if is_closed else "upcoming"
            steps.append({"name": phase_name, "state": state, "window": ""})

    return steps


def compute_ongoing_deliverables(deliverable_names: list[str],
                                  workstreams: list[Workstream],
                                  is_closed: bool) -> list[dict]:
    """Compute deliverable states for an ongoing project."""
    ws_by_name = {ws.name: ws for ws in workstreams}
    steps = []

    for name in deliverable_names:
        ws = ws_by_name.get(name)
        if not ws:
            print(f"  WARNING: Deliverable '{name}' not found as a Monday workstream")
            steps.append({"name": name, "state": "upcoming", "window": ""})
            continue

        state = "complete" if is_closed else ws.status
        steps.append({"name": name, "state": state, "window": ws.window})

    return steps


# ── Build milestone list ──────────────────────────────────────────

MONTH_NAMES = {
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
}


def _is_recurring_workstream(ws: Workstream) -> bool:
    """Detect recurring-meeting workstreams (e.g. 'Monthly Meetings + Status Updates')."""
    if len(ws.visible_subitems) < 5:
        return False
    name_lower = ws.name.lower()
    if "meeting" in name_lower or "recurring" in name_lower:
        # Check if most subitem names start with a month name
        month_hits = sum(
            1 for s in ws.visible_subitems
            if s.name.split()[0].lower().rstrip(",") in MONTH_NAMES
            or any(m in s.name.lower() for m in MONTH_NAMES)
        )
        return month_hits >= len(ws.visible_subitems) * 0.6
    return False


def build_milestones(workstreams: list[Workstream], client_cfg: dict) -> list[dict]:
    """Build sorted milestone list from all visible subitems across workstreams."""
    milestones = []
    kickoff = client_cfg.get("kickoff_date", "")
    close = client_cfg.get("close_date", "")

    for ws in workstreams:
        # Consolidate recurring-meeting workstreams into one summary row
        if _is_recurring_workstream(ws):
            # Build a date range string from config dates
            try:
                k = date.fromisoformat(kickoff) if kickoff else None
                c = date.fromisoformat(close) if close else None
            except ValueError:
                k, c = None, None
            range_str = ""
            if k and c:
                range_str = f"{k.strftime('%b %Y')} – {c.strftime('%b %Y')}"

            milestones.append({
                "name": "All-Team Status Meetings",
                "workstream": ws.name,
                "date": None,
                "date_display": "Monthly",
                "done": False,
                "is_milestone": True,
                "is_approval": False,
                "type": "Recurring",
                "notes": f"Monthly cadence {range_str}" if range_str else "Monthly cadence",
                "row_class": "",
            })
            continue

        for sub in ws.visible_subitems:
            is_approval = sub.type in ("Approval Needed", "Client Meeting")
            row_classes = []
            if sub.done:
                row_classes.append("row-done")
            elif is_approval:
                row_classes.append("row-approval")
            elif not sub.is_milestone:
                row_classes.append("row-task")

            milestones.append({
                "name": sub.name,
                "workstream": ws.name,
                "date": sub.date,
                "date_display": format_date(sub.date),
                "done": sub.done,
                "is_milestone": sub.is_milestone,
                "is_approval": is_approval,
                "type": sub.type,
                "notes": sub.notes,
                "row_class": " ".join(row_classes),
                "on_hold": sub._status_text == "On Hold",
            })

    # Sort: done items first (by date), then upcoming (by date), nulls last
    def sort_key(m):
        d = m["date"] or date.max
        return (0 if m["done"] else 1, d)

    milestones.sort(key=sort_key)
    return milestones


# ── Render + write ────────────────────────────────────────────────

TIMESTAMP_PATTERN = re.compile(r"<!-- LAST-UPDATED: .* -->")


def content_hash(html: str) -> str:
    """Hash HTML content excluding the timestamp line, for idempotent writes."""
    stripped = TIMESTAMP_PATTERN.sub("", html)
    return hashlib.sha256(stripped.encode()).hexdigest()


def render_client(client_cfg: dict, token: str, jinja_env: Environment) -> str | None:
    """Fetch data and render a single client dashboard. Returns HTML or None on error."""
    slug = client_cfg["slug"]
    group_id = client_cfg["monday_group_id"]
    print(f"\n{'='*60}")
    print(f"Processing: {slug} (group: {group_id})")

    try:
        raw_items = fetch_group_data(token, group_id)
    except Exception as e:
        print(f"  ERROR fetching Monday data: {e}")
        return None

    print(f"  Found {len(raw_items)} items in group")

    # Parse workstreams
    workstreams = [Workstream(item) for item in raw_items]
    # Filter out workstreams with zero visible subitems
    visible_workstreams = [ws for ws in workstreams if ws.visible_subitems]
    print(f"  {len(visible_workstreams)} workstreams with visible subitems "
          f"(of {len(workstreams)} total)")

    # Parse dates
    kickoff = None
    close = None
    if client_cfg.get("kickoff_date"):
        try:
            kickoff = date.fromisoformat(client_cfg["kickoff_date"])
        except ValueError:
            pass
    if client_cfg.get("close_date"):
        try:
            close = date.fromisoformat(client_cfg["close_date"])
        except ValueError:
            pass

    is_closed = close is not None and date.today() > close

    # Compute tracker
    project_type = client_cfg.get("project_type", "linear")
    tracker_steps = []
    if project_type == "linear":
        tracker_steps = compute_linear_phases(
            client_cfg.get("phases"), workstreams, is_closed
        )
    elif project_type == "ongoing" and client_cfg.get("deliverables"):
        tracker_steps = compute_ongoing_deliverables(
            client_cfg["deliverables"], workstreams, is_closed
        )

    # Build milestones
    milestones = build_milestones(visible_workstreams, client_cfg)
    print(f"  {len(milestones)} visible milestones/tasks")

    # Check-in end date (90 days after close)
    check_in_end = close + timedelta(days=90) if close else None

    # Template context
    now = datetime.now().strftime("%Y-%m-%d %H:%M CT")
    ctx = {
        "client": {
            "slug": slug,
            "display_name": client_cfg["display_name"],
            "subtitle": client_cfg.get("subtitle", ""),
            "project_type": project_type,
            "kickoff_date": kickoff,
            "kickoff_date_display": format_date_long(kickoff),
            "close_date": close,
            "close_date_display": format_date_long(close),
            "check_in_end_display": format_date_long(check_in_end),
            "is_closed": is_closed,
        },
        "tracker_steps": tracker_steps,
        "workstreams": visible_workstreams,
        "milestones": milestones,
        "generated_at": now,
    }

    template = jinja_env.get_template("index.html")
    return template.render(**ctx)


def write_if_changed(filepath: Path, new_html: str) -> bool:
    """Write file only if content (excluding timestamp) has changed. Returns True if written."""
    if filepath.exists():
        existing = filepath.read_text(encoding="utf-8")
        if content_hash(existing) == content_hash(new_html):
            return False
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(new_html, encoding="utf-8")
    return True


# ── Main ──────────────────────────────────────────────────────────

def main():
    token = os.environ.get("MONDAY_API_TOKEN")
    if not token:
        print("ERROR: MONDAY_API_TOKEN environment variable is required")
        sys.exit(1)

    if not CLIENTS_FILE.exists():
        print(f"ERROR: {CLIENTS_FILE} not found")
        sys.exit(1)

    with open(CLIENTS_FILE, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    clients = config.get("clients", [])
    if not clients:
        print("No clients configured in clients.yaml")
        sys.exit(0)

    jinja_env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=False,
        keep_trailing_newline=True,
    )

    changes = 0
    errors = 0

    for client_cfg in clients:
        slug = client_cfg["slug"]
        html = render_client(client_cfg, token, jinja_env)
        if html is None:
            errors += 1
            continue

        out_path = REPO_ROOT / slug / "index.html"
        if write_if_changed(out_path, html):
            print(f"  UPDATED: {out_path.relative_to(REPO_ROOT)}")
            changes += 1
        else:
            print(f"  No changes: {out_path.relative_to(REPO_ROOT)}")

    print(f"\n{'='*60}")
    print(f"Done. {changes} file(s) updated, {errors} error(s).")

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
