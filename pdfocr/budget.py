"""Spending guard for Claude API calls: a ledger and a hard monthly cap.

The Claude API has no call that reports a prepaid credit balance to an ordinary key (usage and cost
reports need an organisation Admin key, which individual accounts don't have), so this app keeps its own
books and refuses to send a request that could take the period's spend over the limit you set:

    spent this period  +  worst case of every request in flight  +  worst case of the next request
        <=  monthly limit  -  safety margin

Worst case = input tokens (counted by the API before sending) at the input price, plus `max_tokens` at the
output price; a response can't cost more than that. After each response the real cost, from the response's
own token usage, replaces the reservation. Requests whose outcome is unknown (the connection dropped
mid-flight) stay on the books at worst case until you clear them.

This only knows about spending made through this app. The independent backstop is on Anthropic's side:
with no card and auto-reload off, the API stops answering when the credits run out.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

# The ledger lives in the app's own folder, not under %LOCALAPPDATA%: sandboxed tools (the Claude desktop
# app runs commands that way) get a private copy of files they write under AppData, so a ledger kept
# there splits in two and each side undercounts. One file in the app folder is seen by everything.
APP_DIR = Path(__file__).resolve().parent.parent / "data"
OLD_APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PDF OCR"   # where it was until 2026-11-10
LEDGER_PATH = APP_DIR / "spend.json"
PRICES_PATH = APP_DIR / "prices.json"   # optional overrides, same shape as PRICES

# USD per million tokens. Source: platform.claude.com/docs/en/about-claude/pricing, read 2026-11-10.
# Haiku 5.5's row is for prompts up to 100,000 tokens (a page here is a few thousand).
PRICES: dict[str, dict[str, float]] = {
    "claude-opus-5-5":   {"in": 4.00, "cache_write": 5.00,  "cache_read": 0.20, "out": 20.00},
    "claude-sonnet-5-5": {"in": 2.00, "cache_write": 2.50,  "cache_read": 0.10, "out": 10.00},
    "claude-haiku-5-5":  {"in": 0.10, "cache_write": 0.125, "cache_read": 0.01, "out": 0.50},
}
MODEL_LABELS = {
    "claude-opus-5-5": "Claude Opus 5.5 (best quality)",
    "claude-sonnet-5-5": "Claude Sonnet 5.5 (half the price)",
    "claude-haiku-5-5": "Claude Haiku 5.5 (cheapest)",
}
PENDING_STALE_S = 3600   # a pending entry older than this belongs to a run that died; it stays counted


class BudgetNotSet(Exception):
    """No monthly limit has been entered yet; nothing may be sent."""


class BudgetExceeded(Exception):
    """Sending this request could take spending over the limit."""


def prices() -> dict[str, dict[str, float]]:
    table = {k: dict(v) for k, v in PRICES.items()}
    try:
        for model, row in json.loads(PRICES_PATH.read_text(encoding="utf-8")).items():
            table.setdefault(model, {}).update({k: float(v) for k, v in row.items()})
    except (OSError, ValueError):
        pass
    return table


def price_row(model: str) -> dict[str, float]:
    """Prices for a model; an unknown model is charged at the dearest known rates so the guard errs high."""
    table = prices()
    if model in table:
        return table[model]
    return {k: max(row[k] for row in table.values()) for k in ("in", "cache_write", "cache_read", "out")}


def cost_of_usage(model: str, usage) -> float:
    """Dollar cost of one response from its `usage` object (or dict)."""
    get = (lambda k: usage.get(k) or 0) if isinstance(usage, dict) else (lambda k: getattr(usage, k, 0) or 0)
    p = price_row(model)
    return (get("input_tokens") * p["in"] + get("cache_creation_input_tokens") * p["cache_write"]
            + get("cache_read_input_tokens") * p["cache_read"] + get("output_tokens") * p["out"]) / 1e6


def worst_case_cost(model: str, input_tokens: int, max_tokens: int) -> float:
    """The most one request can cost: all input billed as a cache write, and the full output allowance used."""
    p = price_row(model)
    return (input_tokens * max(p["in"], p["cache_write"]) + max_tokens * p["out"]) / 1e6


@dataclass
class BudgetStatus:
    limit: float | None       # monthly limit in USD, None until the user sets one
    margin: float             # kept unspent
    spent: float              # confirmed cost this period
    pending: float            # reservations: requests in flight plus unconfirmed ones
    unconfirmed: float        # the part of `pending` left by requests whose outcome is unknown
    period_start: datetime
    period_end: datetime

    @property
    def available(self) -> float:
        if self.limit is None:
            return 0.0
        return max(0.0, self.limit - self.margin - self.spent - self.pending)


class Budget:
    """Thread-safe ledger on disk. Every public method re-reads the file, so two app windows share one book."""

    def __init__(self, path: Path = LEDGER_PATH):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path == LEDGER_PATH:
            self._import_old(OLD_APP_DIR / "spend.json")

    def _import_old(self, old: Path):
        """Bring in the ledger from its old place under AppData: every entry not seen before, and the
        limit if none is set here. Safe to repeat; each old entry is imported once."""
        try:
            src = json.loads(old.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        with self._lock:
            data = self._load()
            seen = set(data.get("imported_ids", [])) | {e["id"] for e in data["entries"]}
            fresh = [e for e in src.get("entries", []) if e.get("id") and e["id"] not in seen]
            changed = bool(fresh)
            data["entries"].extend(fresh)
            data["entries"].sort(key=lambda e: e["ts"])
            data["imported_ids"] = sorted(set(data.get("imported_ids", [])) | {e["id"] for e in src.get("entries", []) if e.get("id")})
            old_settings = src.get("settings") or {}
            if data["settings"]["monthly_limit_usd"] is None and old_settings.get("monthly_limit_usd") is not None:
                data["settings"].update({k: old_settings[k] for k in ("monthly_limit_usd", "margin_usd", "period_start_day")
                                         if k in old_settings})
                changed = True
            if changed or not self.path.exists():
                self._save(data)

    # ---- storage ------------------------------------------------------------------
    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data.setdefault("settings", {})
        data["settings"].setdefault("monthly_limit_usd", None)
        data["settings"].setdefault("margin_usd", 5.0)
        data["settings"].setdefault("period_start_day", 1)
        data.setdefault("entries", [])
        return data

    def _save(self, data: dict):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---- settings -----------------------------------------------------------------
    def settings(self) -> dict:
        with self._lock:
            return dict(self._load()["settings"])

    def set_settings(self, monthly_limit_usd: float | None = ..., margin_usd: float = ..., period_start_day: int = ...):
        with self._lock:
            data = self._load()
            s = data["settings"]
            if monthly_limit_usd is not ...:
                s["monthly_limit_usd"] = None if monthly_limit_usd is None else max(0.0, float(monthly_limit_usd))
            if margin_usd is not ...:
                s["margin_usd"] = max(0.0, float(margin_usd))
            if period_start_day is not ...:
                s["period_start_day"] = min(28, max(1, int(period_start_day)))
            self._save(data)

    # ---- period -------------------------------------------------------------------
    @staticmethod
    def period_bounds(start_day: int, now: datetime | None = None) -> tuple[datetime, datetime]:
        """The billing period containing `now`: from `start_day` of one month to the same day of the next."""
        now = now or datetime.now()
        start = now.replace(day=start_day, hour=0, minute=0, second=0, microsecond=0)
        if start > now:
            prev = (start.replace(day=1) - timedelta(days=1))
            start = prev.replace(day=start_day)
        nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=start_day)
        return start, nxt

    def _status(self, data: dict) -> BudgetStatus:
        s = data["settings"]
        start, end = self.period_bounds(s["period_start_day"])
        t0, t1 = start.timestamp(), end.timestamp()
        now = time.time()
        spent = pending = unconfirmed = 0.0
        for e in data["entries"]:
            if not (t0 <= e["ts"] < t1):
                continue
            if e["state"] == "final":
                spent += e["cost"]
            else:
                pending += e["worst"]
                if e["state"] == "unconfirmed" or now - e["ts"] > PENDING_STALE_S:
                    unconfirmed += e["worst"]
        return BudgetStatus(s["monthly_limit_usd"], s["margin_usd"], spent, pending, unconfirmed, start, end)

    def status(self) -> BudgetStatus:
        with self._lock:
            return self._status(self._load())

    # ---- the guard ----------------------------------------------------------------
    def reserve(self, worst: float, model: str, note: str = "") -> str:
        """Book the worst-case cost of a request about to be sent. Raises instead of letting it overrun."""
        with self._lock:
            data = self._load()
            st = self._status(data)
            if st.limit is None:
                raise BudgetNotSet("Set a monthly API limit before translating.")
            if worst > st.available + 1e-9:
                raise BudgetExceeded(
                    f"Limit reached: ${st.spent:.2f} spent and ${st.pending:.2f} reserved of ${st.limit:.2f} "
                    f"(keeping ${st.margin:.2f} unspent); the next request could cost up to ${worst:.2f}.")
            rid = uuid.uuid4().hex
            data["entries"].append({"id": rid, "ts": time.time(), "state": "pending", "worst": round(worst, 6),
                                    "cost": 0.0, "model": model, "note": note})
            self._save(data)
            return rid

    def settle(self, rid: str, cost: float, usage: dict | None = None):
        """Replace a reservation with what the request really cost."""
        with self._lock:
            data = self._load()
            for e in data["entries"]:
                if e["id"] == rid:
                    e.update(state="final", cost=round(cost, 6), usage=usage or {})
                    break
            self._save(data)

    def release(self, rid: str):
        """Drop a reservation for a request that was certainly not billed (rejected before running)."""
        with self._lock:
            data = self._load()
            data["entries"] = [e for e in data["entries"] if e["id"] != rid]
            self._save(data)

    def mark_unconfirmed(self, rid: str, why: str):
        """The request's fate is unknown (connection lost): keep it counted at worst case."""
        with self._lock:
            data = self._load()
            for e in data["entries"]:
                if e["id"] == rid:
                    e.update(state="unconfirmed", note=(e.get("note", "") + " | " + why).strip(" |"))
            self._save(data)

    def clear_unconfirmed(self) -> float:
        """Forget unconfirmed and stale reservations (after checking the real figure in the Console). Returns USD freed."""
        with self._lock:
            data = self._load()
            now = time.time()
            gone = [e for e in data["entries"] if e["state"] == "unconfirmed"
                    or (e["state"] == "pending" and now - e["ts"] > PENDING_STALE_S)]
            data["entries"] = [e for e in data["entries"] if e not in gone]
            self._save(data)
            return sum(e["worst"] for e in gone)

    # ---- reporting ----------------------------------------------------------------
    def entries(self, this_period_only: bool = True) -> list[dict]:
        with self._lock:
            data = self._load()
            if not this_period_only:
                return list(data["entries"])
            start, end = self.period_bounds(data["settings"]["period_start_day"])
            return [e for e in data["entries"] if start.timestamp() <= e["ts"] < end.timestamp()]

    def average_cost(self, model: str, last: int = 40) -> float | None:
        """Mean cost of recent confirmed requests on a model, for estimates. None with fewer than three."""
        with self._lock:
            costs = [e["cost"] for e in self._load()["entries"]
                     if e["state"] == "final" and e["model"] == model and e["cost"] > 0][-last:]
        return sum(costs) / len(costs) if len(costs) >= 3 else None
