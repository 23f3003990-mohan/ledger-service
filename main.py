import calendar
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

BASE = Path(__file__).parent
IST = timezone(timedelta(hours=5, minutes=30))  # India has no DST

# ---------------------------------------------------------------- data
RATES = {k: Decimal(str(v)) for k, v in json.loads((BASE / "rates.json").read_text())["usd_per_unit"].items()}


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_rows():
    latest = {}
    for line in (BASE / "data.ndjson").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        r["_upd"] = parse_ts(r["updated_at"])
        cur = latest.get(r["id"])
        if cur is None or r["_upd"] >= cur["_upd"]:
            latest[r["id"]] = r
    rows = []
    for r in latest.values():
        r["_date"] = parse_ts(r["created_at"]).astimezone(IST).date()
        r["_usd"] = Decimal(str(r["amount"])) * RATES[r["currency"]]
        rows.append(r)
    return rows


ROWS = load_rows()
REGIONS = sorted({r["region"] for r in ROWS})
PRODUCTS = sorted({r["product"] for r in ROWS})

# ---------------------------------------------------------------- execution
METRICS = {"sum", "count", "units", "avg", "distinct_customers"}
STATUSES = {"paid", "refunded", "void", "all"}
GROUPS = {"product", "customer", "region"}


def money(d: Decimal) -> float:
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def execute(spec: dict):
    status = spec.get("status") or "paid"
    d0 = date.fromisoformat(spec["date_from"]) if spec.get("date_from") else None
    d1 = date.fromisoformat(spec["date_to"]) if spec.get("date_to") else None
    sel = []
    for r in ROWS:
        if status != "all" and r["status"] != status:
            continue
        if spec.get("region") and r["region"] != spec["region"]:
            continue
        if spec.get("product") and r["product"] != spec["product"]:
            continue
        if spec.get("customer") and r["customer"] != spec["customer"]:
            continue
        if spec.get("currency") and r["currency"] != spec["currency"]:
            continue
        if d0 and r["_date"] < d0:
            continue
        if d1 and r["_date"] > d1:
            continue
        sel.append(r)

    metric = spec.get("metric") or "sum"

    def value(rs):
        if metric == "sum":
            return sum((r["_usd"] for r in rs), Decimal(0))
        if metric == "count":
            return len(rs)
        if metric == "units":
            return sum(r["qty"] for r in rs)
        if metric == "avg":
            return (sum((r["_usd"] for r in rs), Decimal(0)) / len(rs)) if rs else Decimal(0)
        if metric == "distinct_customers":
            mo = int(spec.get("min_orders") or 1)
            counts = {}
            for r in rs:
                counts[r["customer"]] = counts.get(r["customer"], 0) + 1
            return sum(1 for c in counts.values() if c >= mo)

    def fmt(v):
        return money(v) if isinstance(v, Decimal) else v

    group = spec.get("group_by")
    if group:
        buckets = {}
        for r in sel:
            buckets.setdefault(r[group], []).append(r)
        scored = [(k, value(v)) for k, v in buckets.items()]
        reverse = (spec.get("order") or "max") == "max"
        scored.sort(key=lambda kv: (-kv[1] if reverse else kv[1], kv[0]))
        n = int(spec.get("limit") or 1)
        names = [k for k, _ in scored[:n]]
        if not names:
            return None
        return names[0] if n == 1 else names
    return fmt(value(sel))


# ---------------------------------------------------------------- rule-based parser
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS["sept"] = 9
MON_RE = "|".join(sorted(MONTHS, key=len, reverse=True))
DEFAULT_YEAR = 2026


def month_span(y, m):
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def find_dates(q: str):
    """Return (date_from, date_to) as ISO strings or (None, None)."""
    text = q.lower()
    spans = []

    def take(pattern, fn):
        nonlocal text
        for m in list(re.finditer(pattern, text)):
            res = fn(m)
            if res:
                spans.append((m.start(), res))
        text = re.sub(pattern, lambda m: " " * len(m.group(0)), text)

    take(r"\b(\d{4})-(\d{2})-(\d{2})\b",
         lambda m: (date(int(m[1]), int(m[2]), int(m[3])),) * 2)
    take(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + MON_RE + r")\.?,?\s*(\d{4})?\b",
         lambda m: (date(int(m[3] or DEFAULT_YEAR), MONTHS[m[2]], int(m[1])),) * 2)
    take(r"\b(" + MON_RE + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?!\d)(?:,?\s*(\d{4}))?",
         lambda m: (date(int(m[3] or DEFAULT_YEAR), MONTHS[m[1]], int(m[2])),) * 2
         if 1 <= int(m[2]) <= 31 else None)
    take(r"\bq([1-4])\s*(?:of\s+)?(\d{4})?\b",
         lambda m: (month_span(int(m[2] or DEFAULT_YEAR), 3 * int(m[1]) - 2)[0],
                    month_span(int(m[2] or DEFAULT_YEAR), 3 * int(m[1]))[1]))
    take(r"\bh([12])\s*(\d{4})?\b",
         lambda m: (month_span(int(m[2] or DEFAULT_YEAR), 1 if m[1] == "1" else 7)[0],
                    month_span(int(m[2] or DEFAULT_YEAR), 6 if m[1] == "1" else 12)[1]))
    take(r"\b(" + MON_RE + r")\b\.?,?\s*(\d{4})?",
         lambda m: month_span(int(m[2] or DEFAULT_YEAR), MONTHS[m[1]]))
    take(r"\b(20\d{2})\b", lambda m: (date(int(m[1]), 1, 1), date(int(m[1]), 12, 31)))

    if not spans:
        return None, None
    spans.sort(key=lambda s: s[0])
    lo = min(s[1][0] for s in spans)
    hi = max(s[1][1] for s in spans)
    return lo.isoformat(), hi.isoformat()


def find_entities(q: str):
    ql = q.lower()
    out = {}
    for rg in REGIONS:
        if re.search(r"\b" + rg.lower() + r"\b", ql):
            out["region"] = rg
    for p in PRODUCTS:
        pat = r"\b" + r"[\s\-]*".join(re.escape(w) for w in p.lower().split()) + r"s?\b"
        if re.search(pat, ql):
            out["product"] = p
    m = re.search(r"\bc(\d{3,5})\b", ql)
    if m:
        out["customer"] = "C" + m.group(1)
    return out


SUPER = r"highest|most|top|best|largest|biggest|greatest|lowest|least|worst|smallest|fewest|bottom|leading"
WORDNUM = {"two": 2, "three": 3, "four": 4, "five": 5, "ten": 10}


def rule_parse(q: str) -> dict:
    ql = q.lower()
    spec = {}
    spec.update(find_entities(q))
    spec["date_from"], spec["date_to"] = find_dates(q)
    for cur in ("usd", "eur", "inr"):
        if re.search(r"\b(?:paid|placed|made|ordered|priced)\s+in\s+" + cur + r"\b", ql):
            spec["currency"] = cur.upper()

    if "refund" in ql:
        spec["status"] = "refunded"
    elif re.search(r"\b(void|voided|cancel)", ql):
        spec["status"] = "void"
    elif re.search(r"\ball orders\b|\bregardless\b|\bincluding (?:void|refund)", ql):
        spec["status"] = "all"
    else:
        spec["status"] = "paid"

    revenue_words = re.search(r"revenue|sales|spent|spend|income|turnover|worth|value|amount|money", ql)
    countish = re.search(r"how many|number of|count|\bno\. of\b", ql)

    if re.search(r"\bunits?\b|quantit|items|pieces", ql):
        spec["metric"] = "units"
    elif re.search(r"\baverage\b|\bavg\b|\bmean\b", ql):
        spec["metric"] = "avg"
    elif re.search(r"\b(distinct|unique|different|separate)\s+(?:\w+\s+)?customers?\b", ql) or re.search(
            r"\b(how many|number of|count of|count)\s+(?:\w+\s+)?(?:(?:distinct|unique|different)\s+)?customers\b", ql) or re.search(
            r"\bcustomers\s+(?:have|has|who|that|bought|purchased|ordered|placed|made)\b", ql) and countish:
        spec["metric"] = "distinct_customers"
    elif countish:
        spec["metric"] = "count"
    else:
        spec["metric"] = "sum"

    th = re.search(r"\bat least (\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b[^.?]*?\b(?:paid |refunded |void )?(?:orders?|purchases?|refunds?)\b", ql)
    mt = re.search(r"\b(?:more than|over|greater than)\s+(\d+|one|two|three|four|five)\b[^.?]*?\b(?:orders?|purchases?|refunds?)\b", ql)
    NUMW = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
    if spec["metric"] in ("distinct_customers", "count") and re.search(r"customers?", ql) and (th or mt):
        if th:
            v = th.group(1); n = int(v) if v.isdigit() else NUMW[v]
        else:
            v = mt.group(1); n = (int(v) if v.isdigit() else NUMW[v]) + 1
        if re.search(r"\bhow many (?:different |distinct |unique )?customers\b|\bnumber of (?:different |distinct |unique )?customers\b", ql):
            spec["metric"] = "distinct_customers"
            if n > 1:
                spec["min_orders"] = n
    ql_rank = re.sub(r"\bat (?:least|most)\b|\bleast one\b|\bnot least\b", " ", ql)
    grp = re.search(r"\b(customer|product|region)s?\b", ql_rank)
    sup = re.search(r"\b(" + SUPER + r")\b", ql_rank)
    if spec["metric"] == "distinct_customers":
        sup = None  # "how many customers..." is a count, never a ranking
    if grp and sup:
        g = grp.group(1)
        # a named entity of the same kind makes it a filter, not a ranking
        if g not in spec:
            spec["group_by"] = g
            low = re.search(r"lowest|least|worst|smallest|fewest|bottom", ql_rank)
            spec["order"] = "min" if low else "max"
            n = re.search(r"\b(?:top|bottom|best|worst)\s+(\d+|two|three|four|five|ten)\b", ql)
            if n:
                v = n.group(1)
                spec["limit"] = int(v) if v.isdigit() else WORDNUM[v]
            if spec["metric"] == "sum" and re.search(r"orders|purchases", ql) and not revenue_words:
                spec["metric"] = "count"
    return spec


# ---------------------------------------------------------------- optional LLM parser
SCHEMA = {
    "type": "object",
    "properties": {
        "metric": {"type": "string", "enum": sorted(METRICS)},
        "status": {"type": "string", "enum": sorted(STATUSES)},
        "region": {"type": ["string", "null"]},
        "product": {"type": ["string", "null"]},
        "customer": {"type": ["string", "null"]},
        "currency": {"type": ["string", "null"], "enum": ["USD", "EUR", "INR", None]},
        "date_from": {"type": ["string", "null"]},
        "date_to": {"type": ["string", "null"]},
        "group_by": {"type": ["string", "null"], "enum": ["product", "customer", "region", None]},
        "order": {"type": ["string", "null"], "enum": ["max", "min", None]},
        "limit": {"type": ["integer", "null"]},
        "min_orders": {"type": ["integer", "null"], "description": "for distinct_customers: only customers with at least this many matching orders"},
    },
    "required": ["metric", "status"],
}

LLM_PROMPT = f"""Convert a question about the Acme Appliances order ledger into a query spec by calling the tool.
Data: orders with fields region {REGIONS}, product {PRODUCTS}, customer ids like C1174, status paid/refunded/void.
Semantics:
- metric "sum" = money in USD (default for revenue, sales, spend, refund amount); "count" = number of orders; "units" = total quantity; "avg" = average order value in USD; "distinct_customers" = number of different customers.
- status: "paid" for revenue/sales/orders; "refunded" for anything about refunds; "void" for void/cancelled; "all" only if asked for all orders regardless of status.
- Dates are inclusive calendar dates in YYYY-MM-DD. A month means its first to last day; a quarter or year likewise. If no year is given assume 2026. Leave null when the question has no date range.
- Use region/product/customer only when the question restricts to one; spell them exactly as listed.
- "How many customers placed at least N orders": metric distinct_customers with min_orders N. Words like "at least one" are NOT a ranking.
- For 'which product/customer/region had the highest/lowest ...' set group_by, order (max/min) and limit (default 1); the metric says what is ranked.
Return only the tool call."""


def llm_parse(q: str):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or os.environ.get("USE_LLM", "1") == "0":
        return None
    import httpx
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={
            "model": os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5"),
            "max_tokens": 400,
            "system": LLM_PROMPT,
            "tools": [{"name": "query", "description": "Structured ledger query", "input_schema": SCHEMA}],
            "tool_choice": {"type": "tool", "name": "query"},
            "messages": [{"role": "user", "content": q}],
        },
        timeout=float(os.environ.get("LLM_TIMEOUT", "8")),
    )
    resp.raise_for_status()
    for block in resp.json().get("content", []):
        if block.get("type") == "tool_use":
            return clean_spec(block["input"])
    return None


def clean_spec(s: dict):
    """Validate an LLM spec against real values; return None if anything is off."""
    out = {"metric": s.get("metric"), "status": s.get("status") or "paid"}
    if out["metric"] not in METRICS or out["status"] not in STATUSES:
        return None
    for key, allowed in (("region", REGIONS), ("product", PRODUCTS)):
        v = s.get(key)
        if v:
            match = [a for a in allowed if a.lower() == str(v).lower()]
            if not match:
                return None
            out[key] = match[0]
    if s.get("customer"):
        c = str(s["customer"]).upper()
        if not re.fullmatch(r"C\d{3,5}", c):
            return None
        out["customer"] = c
    if s.get("currency"):
        out["currency"] = s["currency"]
    for key in ("date_from", "date_to"):
        if s.get(key):
            try:
                date.fromisoformat(s[key])
            except ValueError:
                return None
            out[key] = s[key]
    if s.get("min_orders"):
        out["min_orders"] = int(s["min_orders"])
    if s.get("group_by"):
        if s["group_by"] not in GROUPS:
            return None
        out["group_by"] = s["group_by"]
        out["order"] = s.get("order") or "max"
        out["limit"] = s.get("limit") or 1
    return out


# ---------------------------------------------------------------- API
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/")
def health():
    return {"ok": True, "orders": len(ROWS)}


@app.post("/")
@app.post("/ask")
async def ask(request: Request):
    body = await request.json()
    q = str(body.get("question", ""))
    t0 = time.time()
    spec = None
    try:
        spec = llm_parse(q)
    except Exception as e:  # network, timeout, bad key... fall back to rules
        print("llm failed:", repr(e))
    if spec is None:
        spec = rule_parse(q)
    print(f"{time.time() - t0:.2f}s Q={q!r} SPEC={spec}", flush=True)
    return {"answer": execute(spec)}
