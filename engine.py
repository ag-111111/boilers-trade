"""
Boilers Trade engine.

Everything that touches money lives here: the LMSR market maker, accounts,
trading, settlement, and the leaderboard. No Streamlit imports, so it can be
unit tested and reused behind a different frontend later.

Pricing: Hanson's LMSR.
    C(q)   = b * ln(sum_i exp(q_i / b))
    p_i(q) = exp(q_i / b) / sum_j exp(q_j / b)
A trade costs C(q_after) - C(q_before). That makes pricing exact, path
independent (no chunking exploit), and prices always sum to 1.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import os
import re
import secrets
import smtplib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

DB_PATH = os.environ.get("BOILERS_DB", "boilers_market.db")
ALLOWED_DOMAIN = os.environ.get("BOILERS_EMAIL_DOMAIN", "purdue.edu").lower()
ET = ZoneInfo("America/Indiana/Indianapolis")

START_CASH = 1000.0      # play money every new account gets
DAILY_BONUS = 50.0
FEE_RATE = 0.02          # charged on top of buys, taken out of sells
DEFAULT_B = 200.0        # LMSR liquidity. Higher = prices move slower
MIN_SPEND = 1.0
MAX_SPEND = 50_000.0
MIN_PROB = 0.005
DUST = 1e-6

# Admin account. Username is "house". Only the scrypt hash of the default
# password lives in the code, so the repo can be public without leaking it.
# To use a different password, set BOILERS_ADMIN_PASSWORD (env var or Streamlit secret).
ADMIN_USERNAME = "house"
DEFAULT_ADMIN_SALT = "619feb3ba463709b8aa53585d199a4ed"
DEFAULT_ADMIN_HASH = "c0d0f5b081d7a73278dbcd42efb10f31b900bbb1fffcaeefc0731b60c8c4ee07"

# Signup invite code. Only its hash is stored here, so a public repo does not
# reveal it. Not case sensitive. Override with BOILERS_INVITE_CODE (env var or
# Streamlit secret). Set INVITE_HASH = "" to turn the invite requirement off.
INVITE_HASH = "617ef1b4a99703bdff4ed5571a9917af307b16a666944c4b7cb9ee1af77b1884"

LOGIN_WINDOW_MIN = 15
LOGIN_MAX_FAILS = 8
VERIFY_MAX_TRIES = 5


class MarketError(Exception):
    """Raised with a message that is safe to show to the user."""


# ----------------------------------------------------------------------------
# Time helpers
# ----------------------------------------------------------------------------
def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


# ----------------------------------------------------------------------------
# LMSR math (pure functions)
# ----------------------------------------------------------------------------
def lmsr_cost(q: list[float], b: float) -> float:
    m = max(q)
    return m + b * math.log(sum(math.exp((x - m) / b) for x in q))


def lmsr_prices(q: list[float], b: float) -> list[float]:
    m = max(q)
    e = [math.exp((x - m) / b) for x in q]
    s = sum(e)
    return [x / s for x in e]


def shares_for_spend(q: list[float], b: float, i: int, net: float) -> float:
    """Shares of outcome i you get for `net` dollars (closed form)."""
    c1 = lmsr_cost(q, b) + net
    m = max(q)
    x = (c1 - m) / b
    if x > 40:                       # other outcomes are negligible out here
        new_qi = c1
    else:
        others = sum(math.exp((qj - m) / b) for j, qj in enumerate(q) if j != i)
        new_qi = m + b * math.log(math.exp(x) - others)
    return new_qi - q[i]


def buy_math(q: list[float], b: float, i: int, spend: float) -> dict:
    fee = spend - spend / (1 + FEE_RATE)
    net = spend - fee
    shares = shares_for_spend(q, b, i, net)
    q2 = list(q)
    q2[i] += shares
    return {"shares": shares, "fee": fee, "net": net, "spend": spend,
            "avg_price": spend / shares if shares > 0 else 0.0,
            "q_after": q2, "prices_after": lmsr_prices(q2, b)}


def sell_math(q: list[float], b: float, i: int, shares: float) -> dict:
    q2 = list(q)
    q2[i] -= shares
    gross = lmsr_cost(q, b) - lmsr_cost(q2, b)
    fee = gross * FEE_RATE
    return {"shares": shares, "gross": gross, "fee": fee, "net": gross - fee,
            "avg_price": (gross - fee) / shares if shares > 0 else 0.0,
            "q_after": q2, "prices_after": lmsr_prices(q2, b)}


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    cash REAL NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 0,
    verify_hash TEXT,
    verify_tries INTEGER NOT NULL DEFAULT 0,
    last_claim TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS markets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    rules TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'General',
    b REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN, CLOSED, RESOLVED, CANCELLED
    closes_at TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    winner_id INTEGER,
    volume REAL NOT NULL DEFAULT 0,
    fees REAL NOT NULL DEFAULT 0,
    amm_cash REAL NOT NULL DEFAULT 0,      -- net cash the market maker has taken in
    payouts REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id INTEGER NOT NULL REFERENCES markets(id),
    label TEXT NOT NULL,
    q REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    user_id INTEGER NOT NULL REFERENCES users(id),
    outcome_id INTEGER NOT NULL REFERENCES outcomes(id),
    shares REAL NOT NULL,
    cost REAL NOT NULL,
    PRIMARY KEY (user_id, outcome_id)
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    market_id INTEGER NOT NULL,
    outcome_id INTEGER,
    kind TEXT NOT NULL,                    -- BUY, SELL, PAYOUT, REFUND
    shares REAL NOT NULL DEFAULT 0,
    amount REAL NOT NULL,
    fee REAL NOT NULL DEFAULT 0,
    price_after REAL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id INTEGER NOT NULL,
    outcome_id INTEGER NOT NULL,
    price REAL NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_attempts (
    ident TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outcomes_market ON outcomes(market_id);
CREATE INDEX IF NOT EXISTS ix_hist_market ON price_history(market_id, id);
CREATE INDEX IF NOT EXISTS ix_trades_market ON trades(market_id, id);
CREATE INDEX IF NOT EXISTS ix_trades_user ON trades(user_id, id);
CREATE INDEX IF NOT EXISTS ix_auth ON auth_attempts(ident, ts);
"""


@contextmanager
def db(write: bool = False):
    """One connection per call. Writes run inside BEGIN IMMEDIATE so two trades
    can never read the same stale state."""
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if write:
            conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init_db(admin_password: str | None = None) -> None:
    """Creates tables and the house (admin) account. The admin password is re-synced
    on every boot: BOILERS_ADMIN_PASSWORD if set, otherwise the built-in default."""
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
    pw = admin_password or os.environ.get("BOILERS_ADMIN_PASSWORD")
    if pw:
        salt = secrets.token_hex(16)
        pw_hash = _hash_pw(pw, salt)
    else:
        salt, pw_hash = DEFAULT_ADMIN_SALT, DEFAULT_ADMIN_HASH
    with db(write=True) as c:
        admin = c.execute("SELECT id FROM users WHERE is_admin=1").fetchone()
        if admin:
            c.execute("UPDATE users SET pw_hash=?, salt=? WHERE id=?", (pw_hash, salt, admin["id"]))
            return
        c.execute(
            "INSERT INTO users (email, username, pw_hash, salt, cash, is_admin, verified, created_at) "
            "VALUES (?,?,?,?,0,1,1,?)",
            ("house@local", ADMIN_USERNAME, pw_hash, salt, iso(now())))


# ----------------------------------------------------------------------------
# Accounts
# ----------------------------------------------------------------------------
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$")


def _hash_pw(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                          n=2 ** 14, r=8, p=1, dklen=32).hex()


def _hash_code(code: str, salt: str) -> str:
    return hashlib.sha256((salt + code).encode()).hexdigest()


def register(email: str, username: str, password: str, require_verify: bool) -> str | None:
    """Creates the account. Returns a 6 digit code to email when verification
    is required, otherwise None (account is active immediately)."""
    email = (email or "").strip().lower()
    username = (username or "").strip()
    if not EMAIL_RE.match(email) or not email.endswith("@" + ALLOWED_DOMAIN):
        raise MarketError(f"Use your @{ALLOWED_DOMAIN} email.")
    if not USERNAME_RE.match(username):
        raise MarketError("Username: 3 to 20 letters, numbers, or underscores.")
    if username.lower() in {"house", "admin"}:
        raise MarketError("That username is reserved.")
    if len(password or "") < 8:
        raise MarketError("Password needs at least 8 characters.")

    salt = secrets.token_hex(16)
    code = f"{secrets.randbelow(10 ** 6):06d}" if require_verify else None
    vhash = _hash_code(code, salt) if code else None
    with db(write=True) as c:
        existing = c.execute("SELECT id, verified FROM users WHERE email=?", (email,)).fetchone()
        taken = c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if existing and existing["verified"]:
            raise MarketError("That email already has an account. Log in instead.")
        if taken and (not existing or taken["id"] != existing["id"]):
            raise MarketError("Username taken.")
        if existing:   # unverified signup being retried
            c.execute("UPDATE users SET username=?, pw_hash=?, salt=?, verify_hash=?, verify_tries=0 WHERE id=?",
                      (username, _hash_pw(password, salt), salt, vhash, existing["id"]))
        else:
            c.execute(
                "INSERT INTO users (email, username, pw_hash, salt, cash, verified, verify_hash, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (email, username, _hash_pw(password, salt), salt, START_CASH,
                 0 if require_verify else 1, vhash, iso(now())))
    return code


def invite_required(override: str | None = None) -> bool:
    return bool(override or INVITE_HASH)


def check_invite(code: str, override: str | None = None) -> None:
    """Raises MarketError unless the invite code matches (or none is required)."""
    if not invite_required(override):
        return
    given = (code or "").strip().lower()
    want = hashlib.sha256(override.strip().lower().encode()).hexdigest() if override else INVITE_HASH
    if not hmac.compare_digest(hashlib.sha256(given.encode()).hexdigest(), want):
        raise MarketError("Wrong invite code. Ask whoever sent you the link.")


def verify_email(email: str, code: str) -> int:
    email = (email or "").strip().lower()
    with db(write=True) as c:
        u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not u or u["verified"] or not u["verify_hash"]:
            raise MarketError("Nothing to verify for that email.")
        if u["verify_tries"] >= VERIFY_MAX_TRIES:
            raise MarketError("Too many wrong codes. Sign up again to get a new one.")
        if not hmac.compare_digest(u["verify_hash"], _hash_code((code or "").strip(), u["salt"])):
            c.execute("UPDATE users SET verify_tries = verify_tries + 1 WHERE id=?", (u["id"],))
            c.execute("COMMIT")
            c.execute("BEGIN IMMEDIATE")
            raise MarketError("Wrong code.")
        c.execute("UPDATE users SET verified=1, verify_hash=NULL WHERE id=?", (u["id"],))
        return u["id"]


def login(ident: str, password: str) -> int:
    """ident is an email or username. Returns the user id."""
    ident = (ident or "").strip().lower()
    cutoff = iso(now() - timedelta(minutes=LOGIN_WINDOW_MIN))
    with db(write=True) as c:
        c.execute("DELETE FROM auth_attempts WHERE ts < ?", (cutoff,))
        fails = c.execute("SELECT COUNT(*) FROM auth_attempts WHERE ident=?", (ident,)).fetchone()[0]
        if fails >= LOGIN_MAX_FAILS:
            raise MarketError(f"Too many attempts. Try again in {LOGIN_WINDOW_MIN} minutes.")
        u = c.execute("SELECT * FROM users WHERE email=? OR lower(username)=?", (ident, ident)).fetchone()
        ok = bool(u) and hmac.compare_digest(u["pw_hash"], _hash_pw(password or "", u["salt"]))
        if not ok:
            c.execute("INSERT INTO auth_attempts (ident, ts) VALUES (?,?)", (ident, iso(now())))
            c.execute("COMMIT")
            c.execute("BEGIN IMMEDIATE")
            raise MarketError("Wrong login or password.")
        if not u["verified"]:
            raise MarketError("Verify your email first (Verify tab).")
        return u["id"]


def get_user(uid: int) -> dict | None:
    with db() as c:
        u = c.execute("SELECT id, email, username, cash, is_admin, last_claim FROM users WHERE id=?",
                      (uid,)).fetchone()
        return dict(u) if u else None


def claim_daily(uid: int) -> float:
    today = now().astimezone(ET).strftime("%Y-%m-%d")
    with db(write=True) as c:
        u = c.execute("SELECT last_claim, is_admin FROM users WHERE id=?", (uid,)).fetchone()
        if u["is_admin"]:
            raise MarketError("The house does not claim bonuses.")
        if u["last_claim"] == today:
            raise MarketError("Already claimed today. Back tomorrow.")
        c.execute("UPDATE users SET cash = cash + ?, last_claim=? WHERE id=?", (DAILY_BONUS, today, uid))
    return DAILY_BONUS


def send_code_email(to: str, code: str, cfg: dict) -> None:
    msg = EmailMessage()
    msg["Subject"] = f"{code} is your Boilers Trade code"
    msg["From"] = cfg.get("SMTP_FROM") or cfg["SMTP_USER"]
    msg["To"] = to
    msg.set_content(f"Your Boilers Trade verification code is {code}.\n\n"
                    "If you did not sign up, ignore this email.")
    with smtplib.SMTP(cfg["SMTP_HOST"], int(cfg.get("SMTP_PORT", 587)), timeout=15) as s:
        s.starttls()
        s.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
        s.send_message(msg)


# ----------------------------------------------------------------------------
# Markets: reads
# ----------------------------------------------------------------------------
def _load(c: sqlite3.Connection, market_id: int):
    m = c.execute("SELECT * FROM markets WHERE id=?", (int(market_id),)).fetchone()
    if not m:
        raise MarketError("Market not found.")
    outs = c.execute("SELECT * FROM outcomes WHERE market_id=? ORDER BY id", (m["id"],)).fetchall()
    return m, outs


def effective_status(m) -> str:
    """OPEN markets past their close time count as CLOSED (awaiting resolution)."""
    if m["status"] == "OPEN" and m["closes_at"] and now() >= parse(m["closes_at"]):
        return "CLOSED"
    return m["status"]


def _pack(m, outs) -> dict:
    d = dict(m)
    d["status"] = effective_status(m)
    q = [o["q"] for o in outs]
    if m["status"] == "RESOLVED":
        prices = [1.0 if o["id"] == m["winner_id"] else 0.0 for o in outs]
    else:
        prices = lmsr_prices(q, m["b"])
    d["outcomes"] = [{"id": o["id"], "label": o["label"], "q": o["q"], "price": p}
                     for o, p in zip(outs, prices)]
    return d


def get_market(market_id: int) -> dict:
    with db() as c:
        return _pack(*_load(c, market_id))


def list_markets() -> list[dict]:
    with db() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM markets ORDER BY (closes_at IS NULL), closes_at, id")]
        return [_pack(*_load(c, i)) for i in ids]


def quote_buy(outcome_id: int, spend: float) -> dict:
    with db() as c:
        m, outs, i = _locate(c, outcome_id)
        return buy_math([o["q"] for o in outs], m["b"], i, float(spend))


def quote_sell(outcome_id: int, shares: float) -> dict:
    with db() as c:
        m, outs, i = _locate(c, outcome_id)
        return sell_math([o["q"] for o in outs], m["b"], i, float(shares))


def _locate(c, outcome_id: int):
    o = c.execute("SELECT market_id FROM outcomes WHERE id=?", (int(outcome_id),)).fetchone()
    if not o:
        raise MarketError("Outcome not found.")
    m, outs = _load(c, o["market_id"])
    i = next(k for k, x in enumerate(outs) if x["id"] == int(outcome_id))
    return m, outs, i


def price_history(market_id: int) -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT h.outcome_id, o.label, h.price, h.ts FROM price_history h "
            "JOIN outcomes o ON o.id = h.outcome_id WHERE h.market_id=? ORDER BY h.id", (int(market_id),))]


def recent_trades(limit: int = 20, market_id: int | None = None, user_id: int | None = None) -> list[dict]:
    sql = ("SELECT t.*, u.username, o.label, m.question FROM trades t "
           "JOIN users u ON u.id = t.user_id JOIN markets m ON m.id = t.market_id "
           "LEFT JOIN outcomes o ON o.id = t.outcome_id WHERE 1=1")
    args: list = []
    if market_id is not None:
        sql += " AND t.market_id=?"
        args.append(int(market_id))
    if user_id is not None:
        sql += " AND t.user_id=?"
        args.append(int(user_id))
    sql += " ORDER BY t.id DESC LIMIT ?"
    args.append(int(limit))
    with db() as c:
        return [dict(r) for r in c.execute(sql, args)]


# ----------------------------------------------------------------------------
# Trading
# ----------------------------------------------------------------------------
def _record_prices(c, market_id: int, outs, prices, ts: str) -> None:
    c.executemany("INSERT INTO price_history (market_id, outcome_id, price, ts) VALUES (?,?,?,?)",
                  [(market_id, o["id"], p, ts) for o, p in zip(outs, prices)])


def _trader(c, uid: int):
    u = c.execute("SELECT id, cash, is_admin FROM users WHERE id=?", (int(uid),)).fetchone()
    if not u:
        raise MarketError("Account not found.")
    if u["is_admin"]:
        raise MarketError("The house account resolves markets, so it cannot trade.")
    return u


def buy(uid: int, outcome_id: int, spend: float, min_shares: float = 0.0) -> dict:
    """Spend `spend` dollars (fee included) on one outcome.
    min_shares is slippage protection: the trade fails if the price moved."""
    spend = round(float(spend), 2)
    if not (MIN_SPEND <= spend <= MAX_SPEND):
        raise MarketError(f"Trade size must be ${MIN_SPEND:.0f} to ${MAX_SPEND:,.0f}.")
    with db(write=True) as c:
        m, outs, i = _locate(c, outcome_id)
        if effective_status(m) != "OPEN":
            raise MarketError("This market is closed.")
        u = _trader(c, uid)
        if u["cash"] + 1e-9 < spend:
            raise MarketError(f"Not enough cash. You have ${u['cash']:,.2f}.")
        r = buy_math([o["q"] for o in outs], m["b"], i, spend)
        if r["shares"] < min_shares:
            raise MarketError("Price moved before your order landed. Check the new quote.")
        ts = iso(now())
        c.execute("UPDATE users SET cash = cash - ? WHERE id=?", (spend, u["id"]))
        c.execute("UPDATE outcomes SET q=? WHERE id=?", (r["q_after"][i], outs[i]["id"]))
        c.execute("INSERT INTO positions (user_id, outcome_id, shares, cost) VALUES (?,?,?,?) "
                  "ON CONFLICT(user_id, outcome_id) DO UPDATE SET "
                  "shares = shares + excluded.shares, cost = cost + excluded.cost",
                  (u["id"], outs[i]["id"], r["shares"], spend))
        c.execute("UPDATE markets SET volume = volume + ?, fees = fees + ?, amm_cash = amm_cash + ? WHERE id=?",
                  (spend, r["fee"], r["net"], m["id"]))
        c.execute("INSERT INTO trades (user_id, market_id, outcome_id, kind, shares, amount, fee, price_after, ts) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (u["id"], m["id"], outs[i]["id"], "BUY", r["shares"], spend, r["fee"], r["prices_after"][i], ts))
        _record_prices(c, m["id"], outs, r["prices_after"], ts)
        return r


def sell(uid: int, outcome_id: int, shares: float, min_proceeds: float = 0.0) -> dict:
    shares = float(shares)
    if shares <= 0:
        raise MarketError("Enter a share amount.")
    with db(write=True) as c:
        m, outs, i = _locate(c, outcome_id)
        if effective_status(m) != "OPEN":
            raise MarketError("This market is closed.")
        u = _trader(c, uid)
        pos = c.execute("SELECT shares, cost FROM positions WHERE user_id=? AND outcome_id=?",
                        (u["id"], outs[i]["id"])).fetchone()
        held = pos["shares"] if pos else 0.0
        if shares > held + DUST:
            raise MarketError(f"You only hold {held:,.2f} shares.")
        shares = min(shares, held)
        r = sell_math([o["q"] for o in outs], m["b"], i, shares)
        if r["net"] < min_proceeds:
            raise MarketError("Price moved before your order landed. Check the new quote.")
        ts = iso(now())
        left = held - shares
        if left <= DUST:
            c.execute("DELETE FROM positions WHERE user_id=? AND outcome_id=?", (u["id"], outs[i]["id"]))
        else:
            c.execute("UPDATE positions SET shares=?, cost=? WHERE user_id=? AND outcome_id=?",
                      (left, pos["cost"] * left / held, u["id"], outs[i]["id"]))
        c.execute("UPDATE users SET cash = cash + ? WHERE id=?", (r["net"], u["id"]))
        c.execute("UPDATE outcomes SET q=? WHERE id=?", (r["q_after"][i], outs[i]["id"]))
        c.execute("UPDATE markets SET volume = volume + ?, fees = fees + ?, amm_cash = amm_cash - ? WHERE id=?",
                  (r["gross"], r["fee"], r["gross"], m["id"]))
        c.execute("INSERT INTO trades (user_id, market_id, outcome_id, kind, shares, amount, fee, price_after, ts) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (u["id"], m["id"], outs[i]["id"], "SELL", shares, r["net"], r["fee"], r["prices_after"][i], ts))
        _record_prices(c, m["id"], outs, r["prices_after"], ts)
        return r


# ----------------------------------------------------------------------------
# Portfolio and leaderboard (everything marked at what you could sell it for)
# ----------------------------------------------------------------------------
def positions(uid: int) -> list[dict]:
    out = []
    with db() as c:
        rows = c.execute(
            "SELECT p.*, o.label, o.market_id FROM positions p JOIN outcomes o ON o.id = p.outcome_id "
            "WHERE p.user_id=? ORDER BY o.market_id DESC", (int(uid),)).fetchall()
        cache: dict = {}
        for p in rows:
            if p["market_id"] not in cache:
                cache[p["market_id"]] = _load(c, p["market_id"])
            m, outs = cache[p["market_id"]]
            q = [o["q"] for o in outs]
            i = next(k for k, x in enumerate(outs) if x["id"] == p["outcome_id"])
            value = sell_math(q, m["b"], i, p["shares"])["net"]
            out.append({"market_id": m["id"], "question": m["question"], "status": effective_status(m),
                        "outcome_id": p["outcome_id"], "label": p["label"], "shares": p["shares"],
                        "cost": p["cost"], "avg_cost": p["cost"] / p["shares"],
                        "price": lmsr_prices(q, m["b"])[i], "value": value, "pnl": value - p["cost"],
                        "payout_if_win": p["shares"]})
    return out


def leaderboard() -> list[dict]:
    with db() as c:
        users = c.execute("SELECT id, username, cash FROM users WHERE is_admin=0 AND verified=1").fetchall()
        pos = c.execute("SELECT p.user_id, p.outcome_id, p.shares, o.market_id FROM positions p "
                        "JOIN outcomes o ON o.id = p.outcome_id").fetchall()
        cache: dict = {}
        held: dict[int, float] = {}
        for p in pos:
            if p["market_id"] not in cache:
                cache[p["market_id"]] = _load(c, p["market_id"])
            m, outs = cache[p["market_id"]]
            i = next(k for k, x in enumerate(outs) if x["id"] == p["outcome_id"])
            held[p["user_id"]] = held.get(p["user_id"], 0.0) + sell_math(
                [o["q"] for o in outs], m["b"], i, p["shares"])["net"]
    board = [{"user_id": u["id"], "username": u["username"], "cash": u["cash"],
              "positions": held.get(u["id"], 0.0), "net_worth": u["cash"] + held.get(u["id"], 0.0)}
             for u in users]
    board.sort(key=lambda r: r["net_worth"], reverse=True)
    for rank, r in enumerate(board, 1):
        r["rank"] = rank
    return board


# ----------------------------------------------------------------------------
# Comments
# ----------------------------------------------------------------------------
def add_comment(uid: int, market_id: int, body: str) -> None:
    body = " ".join((body or "").split())[:280]
    if not body:
        raise MarketError("Say something first.")
    with db(write=True) as c:
        last = c.execute("SELECT ts FROM comments WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        if last and now() - parse(last["ts"]) < timedelta(seconds=10):
            raise MarketError("Slow down a sec.")
        c.execute("INSERT INTO comments (market_id, user_id, body, ts) VALUES (?,?,?,?)",
                  (int(market_id), int(uid), body, iso(now())))


def list_comments(market_id: int, limit: int = 30) -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT cm.body, cm.ts, u.username FROM comments cm JOIN users u ON u.id = cm.user_id "
            "WHERE cm.market_id=? ORDER BY cm.id DESC LIMIT ?", (int(market_id), int(limit)))]


# ----------------------------------------------------------------------------
# Admin
# ----------------------------------------------------------------------------
def _require_admin(c, uid: int) -> None:
    u = c.execute("SELECT is_admin FROM users WHERE id=?", (int(uid),)).fetchone()
    if not u or not u["is_admin"]:
        raise MarketError("Admins only.")


def create_market(question: str, outcomes: list[tuple[str, float | None]], rules: str = "",
                  category: str = "General", b: float = DEFAULT_B,
                  closes_at: datetime | None = None) -> int:
    """outcomes: [(label, starting probability or None)]. Probabilities are
    normalized. Starting share counts are q_i = b * ln(p_i)."""
    question = (question or "").strip()
    labels = [(l or "").strip() for l, _ in outcomes]
    if not question or len(labels) < 2 or any(not l for l in labels) or len(set(labels)) != len(labels):
        raise MarketError("Need a question and at least two distinct outcomes.")
    if not (10 <= float(b) <= 100_000):
        raise MarketError("Liquidity b must be between 10 and 100,000.")
    probs = [p for _, p in outcomes]
    if any(p is None for p in probs):
        probs = [1.0 / len(labels)] * len(labels)
    probs = [max(MIN_PROB, float(p)) for p in probs]
    total = sum(probs)
    probs = [p / total for p in probs]
    ts = iso(now())
    with db(write=True) as c:
        cur = c.execute("INSERT INTO markets (question, rules, category, b, closes_at, created_at) VALUES (?,?,?,?,?,?)",
                        (question, (rules or "").strip(), (category or "General").strip(), float(b),
                         iso(closes_at) if closes_at else None, ts))
        mid = cur.lastrowid
        for label, p in zip(labels, probs):
            oc = c.execute("INSERT INTO outcomes (market_id, label, q) VALUES (?,?,?)",
                           (mid, label, float(b) * math.log(p)))
            c.execute("INSERT INTO price_history (market_id, outcome_id, price, ts) VALUES (?,?,?,?)",
                      (mid, oc.lastrowid, p, ts))
        return mid


def update_market_meta(market_id: int, rules: str, category: str, closes_at: datetime | None) -> bool:
    """Refreshes the rules text, category, and close time. Skipped once anyone has traded."""
    with db(write=True) as c:
        m = c.execute("SELECT volume, status FROM markets WHERE id=?", (int(market_id),)).fetchone()
        if not m or m["volume"] > 0 or m["status"] != "OPEN":
            return False
        c.execute("UPDATE markets SET rules=?, category=?, closes_at=? WHERE id=?",
                  ((rules or "").strip(), (category or "General").strip(),
                   iso(closes_at) if closes_at else None, int(market_id)))
        return True


def close_market(admin_id: int, market_id: int) -> None:
    with db(write=True) as c:
        _require_admin(c, admin_id)
        c.execute("UPDATE markets SET status='CLOSED' WHERE id=? AND status='OPEN'", (int(market_id),))


def resolve_market(admin_id: int, market_id: int, winner_id: int) -> float:
    """Pays $1 per winning share, wipes positions, locks the market.
    Returns total paid out."""
    winner_id = int(winner_id)
    with db(write=True) as c:
        _require_admin(c, admin_id)
        m, outs = _load(c, market_id)
        if m["status"] not in ("OPEN", "CLOSED"):
            raise MarketError("Market is already settled.")
        if winner_id not in [o["id"] for o in outs]:
            raise MarketError("That outcome is not in this market.")
        ts = iso(now())
        paid = 0.0
        for p in c.execute("SELECT * FROM positions WHERE outcome_id=?", (winner_id,)).fetchall():
            c.execute("UPDATE users SET cash = cash + ? WHERE id=?", (p["shares"], p["user_id"]))
            c.execute("INSERT INTO trades (user_id, market_id, outcome_id, kind, shares, amount, ts) "
                      "VALUES (?,?,?,?,?,?,?)", (p["user_id"], m["id"], winner_id, "PAYOUT", p["shares"], p["shares"], ts))
            paid += p["shares"]
        c.execute("DELETE FROM positions WHERE outcome_id IN (SELECT id FROM outcomes WHERE market_id=?)", (m["id"],))
        c.execute("UPDATE markets SET status='RESOLVED', winner_id=?, resolved_at=?, payouts=? WHERE id=?",
                  (winner_id, ts, paid, m["id"]))
        _record_prices(c, m["id"], outs, [1.0 if o["id"] == winner_id else 0.0 for o in outs], ts)
        return paid


def cancel_market(admin_id: int, market_id: int) -> float:
    """Voids the market: everyone gets back what they put in, net of what they
    already took out. Returns total refunded."""
    with db(write=True) as c:
        _require_admin(c, admin_id)
        m, _ = _load(c, market_id)
        if m["status"] not in ("OPEN", "CLOSED"):
            raise MarketError("Market is already settled.")
        ts = iso(now())
        rows = c.execute(
            "SELECT user_id, SUM(CASE kind WHEN 'BUY' THEN amount WHEN 'SELL' THEN -amount ELSE 0 END) AS net "
            "FROM trades WHERE market_id=? GROUP BY user_id", (m["id"],)).fetchall()
        refunded = 0.0
        for r in rows:
            net = r["net"] or 0.0
            if abs(net) < 1e-9:
                continue
            c.execute("UPDATE users SET cash = MAX(0, cash + ?) WHERE id=?", (net, r["user_id"]))
            c.execute("INSERT INTO trades (user_id, market_id, kind, amount, ts) VALUES (?,?,?,?,?)",
                      (r["user_id"], m["id"], "REFUND", net, ts))
            refunded += net
        c.execute("DELETE FROM positions WHERE outcome_id IN (SELECT id FROM outcomes WHERE market_id=?)", (m["id"],))
        c.execute("UPDATE markets SET status='CANCELLED', resolved_at=? WHERE id=?", (ts, m["id"]))
        return refunded


def treasury() -> dict:
    with db() as c:
        r = c.execute(
            "SELECT COALESCE(SUM(fees),0) AS fees, COALESCE(SUM(volume),0) AS volume, "
            "COALESCE(SUM(CASE WHEN status='RESOLVED' THEN amm_cash - payouts ELSE 0 END),0) AS amm_pnl "
            "FROM markets").fetchone()
        n = c.execute("SELECT COUNT(*) FROM users WHERE is_admin=0 AND verified=1").fetchone()[0]
        return {"fees": r["fees"], "volume": r["volume"], "amm_pnl": r["amm_pnl"], "players": n}


def list_users() -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT username, email, cash, verified, created_at FROM users WHERE is_admin=0 ORDER BY id DESC")]


def set_password(uid: int, new_password: str) -> None:
    if len(new_password or "") < 8:
        raise MarketError("Password needs at least 8 characters.")
    salt = secrets.token_hex(16)
    with db(write=True) as c:
        c.execute("UPDATE users SET pw_hash=?, salt=? WHERE id=?", (_hash_pw(new_password, salt), salt, int(uid)))
