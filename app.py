import functools
import math
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import lmsr

BASE = os.path.dirname(os.path.abspath(__file__))
# Hosted (Vercel): DATABASE_URL points at Postgres. Local: data lives in data/market.db.
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
if os.environ.get("VERCEL") and not DATABASE_URL:
    raise RuntimeError("No database connected. In the Vercel dashboard: Storage -> Create Database -> Neon, "
                       "connect it to this project, then redeploy.")
DATA = os.environ.get("CORGI_DATA", os.path.join(BASE, "data"))
DB_PATH = os.path.join(DATA, "market.db")
TZ = ZoneInfo(os.environ.get("TIMEZONE", "America/New_York"))
EPS = 1e-6
SITE_NAME = os.environ.get("CORGI_SITE_NAME", "Corgi Markets")

app = Flask(__name__, static_folder="public/static", static_url_path="/static")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")),
                  PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  balance INTEGER NOT NULL DEFAULT 0,          -- cents
  created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_lower ON users(lower(username));
CREATE TABLE IF NOT EXISTS invites(
  code TEXT PRIMARY KEY, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
  used_by INTEGER REFERENCES users(id), used_at TEXT);
CREATE TABLE IF NOT EXISTS markets(
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL CHECK(kind IN ('exclusive','independent')),
  b REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed','resolved')),
  close_at TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  resolution_note TEXT);
CREATE TABLE IF NOT EXISTS contracts(
  id INTEGER PRIMARY KEY,
  market_id INTEGER NOT NULL REFERENCES markets(id),
  name TEXT NOT NULL,
  q REAL NOT NULL DEFAULT 0,
  outcome TEXT CHECK(outcome IN ('YES','NO','CANCEL')),
  resolved_at TEXT,
  sort INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS positions(
  user_id INTEGER NOT NULL REFERENCES users(id),
  contract_id INTEGER NOT NULL REFERENCES contracts(id),
  side TEXT NOT NULL CHECK(side IN ('YES','NO')),
  shares REAL NOT NULL DEFAULT 0,
  basis INTEGER NOT NULL DEFAULT 0,   -- cost basis of shares still held (cents)
  net INTEGER NOT NULL DEFAULT 0,     -- total paid minus total received (cents); refunded on cancel
  PRIMARY KEY(user_id, contract_id, side));
CREATE TABLE IF NOT EXISTS trades(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  contract_id INTEGER NOT NULL REFERENCES contracts(id),
  side TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN ('buy','sell')),
  shares REAL NOT NULL, cents INTEGER NOT NULL,
  price_before REAL NOT NULL, price_after REAL NOT NULL,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS price_history(
  id INTEGER PRIMARY KEY,
  contract_id INTEGER NOT NULL REFERENCES contracts(id),
  price REAL NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  kind TEXT NOT NULL,   -- deposit, withdrawal, withdrawal_refund, trade, payout, refund, adjust
  cents INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS withdrawals(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  cents INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','paid','rejected')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, handled_at TEXT);
CREATE INDEX IF NOT EXISTS ix_contracts_market ON contracts(market_id);
CREATE INDEX IF NOT EXISTS ix_positions_contract ON positions(contract_id);
CREATE INDEX IF NOT EXISTS ix_trades_contract ON trades(contract_id);
CREATE INDEX IF NOT EXISTS ix_history_contract ON price_history(contract_id);
CREATE INDEX IF NOT EXISTS ix_ledger_user ON ledger(user_id);
"""
# Same schema with Postgres types (REAL is only 4 bytes there; prices need 8)
PG_SCHEMA = (SCHEMA.replace("id INTEGER PRIMARY KEY", "id SERIAL PRIMARY KEY")
             .replace(" REAL ", " DOUBLE PRECISION "))


# ---------------------------------------------------------------- db helpers
# Both backends share one tiny interface: execute(sql, args) with "?" placeholders,
# and rows readable by column name or by index.

class Row(dict):
    def __init__(self, cols, vals):
        super().__init__(zip(cols, vals))
        self._vals = vals

    def __getitem__(self, key):
        return self._vals[key] if isinstance(key, int) else super().__getitem__(key)


def _pg_rows(cursor):
    cols = [c.name for c in cursor.description or []]
    return lambda values: Row(cols, values)


class PgConn:
    def __init__(self):
        import psycopg
        # prepare_threshold=None: server-side prepared statements break behind Neon's connection pooler
        self.conn = psycopg.connect(DATABASE_URL, autocommit=True, prepare_threshold=None, row_factory=_pg_rows)

    def execute(self, sql, args=()):
        return self.conn.execute(sql.replace("?", "%s"), args)

    def executemany(self, sql, seq):
        with self.conn.cursor() as cur:
            cur.executemany(sql.replace("?", "%s"), seq)

    def close(self):
        self.conn.close()


def db():
    if "db" not in g:
        if DATABASE_URL:
            g.db = PgConn()
        else:
            g.db = sqlite3.connect(DB_PATH, isolation_level=None, timeout=15)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def _close_db(exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    """Create missing tables; return the session-signing secret (generated once, kept in the db)."""
    new_secret = secrets.token_hex(32)
    if DATABASE_URL:
        import psycopg
        with psycopg.connect(DATABASE_URL, autocommit=True, prepare_threshold=None) as conn:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(7231)")  # concurrent cold starts
                conn.execute(PG_SCHEMA)
                conn.execute("INSERT INTO settings(key, value) VALUES ('secret_key', %s) ON CONFLICT DO NOTHING",
                             (new_secret,))
            return conn.execute("SELECT value FROM settings WHERE key = 'secret_key'").fetchone()[0]
    os.makedirs(DATA, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(price_history)")]
    old_history = bool(cols) and "id" not in cols  # created before price_history had an id column
    if old_history:
        conn.execute("ALTER TABLE price_history RENAME TO price_history_old")
    conn.executescript(SCHEMA)
    if old_history:
        conn.executescript("INSERT INTO price_history(id, contract_id, price, created_at) "
                           "SELECT rowid, contract_id, price, created_at FROM price_history_old; "
                           "DROP TABLE price_history_old;")
    conn.execute("INSERT INTO settings(key, value) VALUES ('secret_key', ?) ON CONFLICT DO NOTHING", (new_secret,))
    conn.commit()
    secret = conn.execute("SELECT value FROM settings WHERE key = 'secret_key'").fetchone()[0]
    conn.close()
    return secret


_stored_secret = init_db()
app.secret_key = os.environ.get("SECRET_KEY") or _stored_secret


class UserError(Exception):
    pass


@contextmanager
def tx():
    """Serialized write transaction; any exception rolls everything back."""
    d = db()
    if DATABASE_URL:
        d.execute("BEGIN")
        d.execute("SELECT pg_advisory_xact_lock(7232)")  # one writer at a time, like SQLite
    else:
        d.execute("BEGIN IMMEDIATE")
    try:
        yield d
        d.execute("COMMIT")
    except BaseException:
        d.execute("ROLLBACK")
        raise


def now():
    return datetime.now(TZ).replace(microsecond=0, tzinfo=None).isoformat(sep=" ")


def one(sql, *args):
    return db().execute(sql, args).fetchone()


def all_(sql, *args):
    return db().execute(sql, args).fetchall()


def credit(d, user_id, cents, kind, note):
    d.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (cents, user_id))
    d.execute("INSERT INTO ledger(user_id, kind, cents, note, created_at) VALUES (?,?,?,?,?)",
              (user_id, kind, cents, note, now()))


def parse_cents(value, allow_negative=False):
    s = (value or "").strip().replace("$", "").replace(",", "")
    if not re.fullmatch(r"-?\d+(\.\d{0,2})?|-?\.\d{1,2}", s):
        raise UserError("Enter a dollar amount like 10 or 12.50")
    cents = round(float(s) * 100)
    if cents == 0 or (cents < 0 and not allow_negative):
        raise UserError("Amount must be greater than zero")
    return cents


# ---------------------------------------------------------------- auth / csrf

def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


@app.before_request
def _before():
    if request.method == "POST":
        token = request.form.get("_csrf") or request.headers.get("X-CSRF")
        if not token or token != session.get("_csrf"):
            abort(400, "Form expired - go back, refresh, and try again.")
    g.user = None
    if "uid" in session:
        g.user = one("SELECT * FROM users WHERE id = ?", session["uid"])
    if request.endpoint not in ("setup", "static") and not one("SELECT 1 FROM users LIMIT 1"):
        return redirect(url_for("setup"))


def login_required(f):
    @functools.wraps(f)
    def wrapper(*a, **kw):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)
    return wrapper


def admin_required(f):
    @functools.wraps(f)
    @login_required
    def wrapper(*a, **kw):
        if not g.user["is_admin"]:
            abort(403)
        return f(*a, **kw)
    return wrapper


def valid_username(name):
    return re.fullmatch(r"[A-Za-z0-9_.\-]{2,24}", name or "")


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if one("SELECT 1 FROM users LIMIT 1"):
        return redirect(url_for("index"))
    if request.method == "POST":
        name, pw = request.form.get("username", "").strip(), request.form.get("password", "")
        if not valid_username(name):
            flash("Username: 2-24 letters, numbers, _ . -", "error")
        elif len(pw) < 8:
            flash("Password must be at least 8 characters", "error")
        else:
            with tx() as d:
                if d.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                    return redirect(url_for("login"))
                uid = d.execute("INSERT INTO users(username, password_hash, is_admin, created_at) VALUES (?,?,1,?) "
                                "RETURNING id", (name, generate_password_hash(pw), now())).fetchone()[0]
            session.clear()
            session.permanent = True
            session["uid"] = uid
            flash("Admin account created. Invite your friends from the Admin page.", "ok")
            return redirect(url_for("admin"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = one("SELECT * FROM users WHERE lower(username) = lower(?)", request.form.get("username", "").strip())
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            session.clear()
            session.permanent = True
            session["uid"] = u["id"]
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("index"))
        flash("Wrong username or password", "error")
    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/join/<code>", methods=["GET", "POST"])
def join(code):
    inv = one("SELECT * FROM invites WHERE code = ? AND used_by IS NULL", code)
    if not inv:
        return render_template("message.html", title="Invite not valid",
                               body="This invite link was already used or revoked. Ask for a new one.")
    if request.method == "POST":
        name, pw = request.form.get("username", "").strip(), request.form.get("password", "")
        try:
            if not valid_username(name):
                raise UserError("Username: 2-24 letters, numbers, _ . -")
            if len(pw) < 8:
                raise UserError("Password must be at least 8 characters")
            with tx() as d:
                if d.execute("SELECT 1 FROM users WHERE lower(username) = lower(?)", (name,)).fetchone():
                    raise UserError("That username is taken")
                if not d.execute("SELECT 1 FROM invites WHERE code = ? AND used_by IS NULL", (code,)).fetchone():
                    raise UserError("This invite was just used")
                uid = d.execute("INSERT INTO users(username, password_hash, created_at) VALUES (?,?,?) RETURNING id",
                                (name, generate_password_hash(pw), now())).fetchone()[0]
                d.execute("UPDATE invites SET used_by = ?, used_at = ? WHERE code = ?", (uid, now(), code))
            session.clear()
            session.permanent = True
            session["uid"] = uid
            flash(f"Welcome, {name}! Send money to the admin to get your balance funded.", "ok")
            return redirect(url_for("index"))
        except UserError as e:
            flash(str(e), "error")
    return render_template("join.html", invite=inv)


# ---------------------------------------------------------------- market math helpers

def load_market(mid):
    m = one("SELECT * FROM markets WHERE id = ?", mid)
    if not m:
        abort(404)
    cs = all_("SELECT * FROM contracts WHERE market_id = ? ORDER BY sort, id", mid)
    return m, cs


def yes_prices(m, cs, override=None):
    """YES price for each contract id. override=(contract_id, new_q) for what-if quotes."""
    qmap = {c["id"]: c["q"] for c in cs}
    if override:
        qmap[override[0]] = override[1]
    out = {}
    if m["kind"] == "exclusive":
        if cs:
            out = dict(zip(qmap.keys(), lmsr.prices(list(qmap.values()), m["b"])))
    else:
        for cid, q in qmap.items():
            out[cid] = lmsr.prices([q, 0.0], m["b"])[0]
    for c in cs:
        if c["outcome"] == "YES":
            out[c["id"]] = 1.0
        elif c["outcome"] == "NO":
            out[c["id"]] = 0.0
    return out


def market_open(m):
    return m["status"] == "open" and not (m["close_at"] and now() >= m["close_at"])


def contract_tradable(m, c):
    return market_open(m) and c["outcome"] is None


def compute_trade(m, cs, c, side, action, amount_cents=None, shares=None):
    b = m["b"]
    p = yes_prices(m, cs)[c["id"]]
    if action == "buy":
        n = lmsr.shares_for_amount(p, b, amount_cents / 100, side)
        cents = -amount_cents
        new_q = c["q"] + (n if side == "YES" else -n)
    else:
        n = shares
        cents = int(math.floor(lmsr.proceeds_for_shares(p, b, n, side) * 100 + 1e-9))
        new_q = c["q"] - (n if side == "YES" else -n)
    after = yes_prices(m, cs, (c["id"], new_q))
    return dict(shares=n, cents=cents, new_q=new_q, price_before=p,
                price_after=after[c["id"]], all_after=after)


def record_prices(d, prices_by_contract):
    t = now()
    d.executemany("INSERT INTO price_history(contract_id, price, created_at) VALUES (?,?,?)",
                  [(cid, p, t) for cid, p in prices_by_contract.items()])


def parse_contract_lines(text):
    """'Name' or 'Name | 30' / 'Name | 30%' per line -> [(name, prob or None)]."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        name, prob = line, None
        if "|" in line:
            name, raw = line.rsplit("|", 1)
            name = name.strip()
            raw = raw.strip().rstrip("%").strip()
            try:
                prob = min(max(float(raw) / 100, 0.01), 0.99)
            except ValueError:
                raise UserError(f"Couldn't read the probability in: {line}")
        if not name:
            raise UserError(f"Missing contract name in: {line}")
        if len(name) > 120:
            raise UserError("Contract names must be 120 characters or fewer")
        out.append((name, prob))
    return out


def add_contracts(d, m, lines):
    existing = d.execute("SELECT * FROM contracts WHERE market_id = ? ORDER BY sort, id", (m["id"],)).fetchall()
    sort = max([c["sort"] for c in existing], default=-1) + 1
    b = m["b"]
    if m["kind"] == "independent":
        for name, p in lines:
            d.execute("INSERT INTO contracts(market_id, name, q, sort) VALUES (?,?,?,?)",
                      (m["id"], name, lmsr.q_for_binary(p or 0.5, b), sort))
            sort += 1
    elif not existing:
        given = sum(p for _, p in lines if p is not None)
        blanks = sum(1 for _, p in lines if p is None)
        fill = max(1 - given, 0.01 * blanks) / blanks if blanks else 0
        probs = [p if p is not None else fill for _, p in lines]
        total = sum(probs)
        for (name, _), p in zip(lines, probs):
            d.execute("INSERT INTO contracts(market_id, name, q, sort) VALUES (?,?,?,?)",
                      (m["id"], name, b * math.log(p / total), sort))
            sort += 1
    else:
        for name, p in lines:
            qs = [r["q"] for r in d.execute("SELECT q FROM contracts WHERE market_id = ?", (m["id"],))]
            p = p or 1 / (len(qs) + 1)
            d.execute("INSERT INTO contracts(market_id, name, q, sort) VALUES (?,?,?,?)",
                      (m["id"], name, lmsr.q_for_new_outcome(p, b, qs), sort))
            sort += 1
    cs = d.execute("SELECT * FROM contracts WHERE market_id = ? ORDER BY sort, id", (m["id"],)).fetchall()
    record_prices(d, {cid: p for cid, p in yes_prices(m, cs).items()
                      if not any(c["id"] == cid and c["outcome"] for c in cs)})


def settle_contract(d, m, c, outcome):
    """Pay out (or refund, on CANCEL) every position in one contract."""
    label = f"{c['name']}" if m["title"] == c["name"] else f"{m['title']} - {c['name']}"
    for pos in d.execute("SELECT * FROM positions WHERE contract_id = ?", (c["id"],)).fetchall():
        if outcome == "CANCEL":
            if pos["net"]:
                credit(d, pos["user_id"], pos["net"], "refund", f"Cancelled: {label} ({pos['side']})")
        elif pos["side"] == outcome and pos["shares"] > EPS:
            amt = int(math.floor(pos["shares"] * 100 + 1e-6))
            if amt:
                credit(d, pos["user_id"], amt, "payout", f"{label} resolved {outcome}")
    d.execute("UPDATE contracts SET outcome = ?, resolved_at = ? WHERE id = ?", (outcome, now(), c["id"]))


def maybe_finish_market(d, m):
    left = d.execute("SELECT COUNT(*) FROM contracts WHERE market_id = ? AND outcome IS NULL", (m["id"],)).fetchone()[0]
    if left == 0:
        d.execute("UPDATE markets SET status = 'resolved', resolved_at = ? WHERE id = ?", (now(), m["id"]))


# ---------------------------------------------------------------- template helpers

@app.template_filter("money")
def money(cents):
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


@app.template_filter("pct")
def pct(p):
    v = p * 100
    if 0 < v < 1:
        return "<1%"
    if 99 < v < 100:
        return ">99%"
    return f"{v:.0f}%"


@app.template_filter("cents_price")
def cents_price(p):
    v = p * 100
    return f"{v:.1f}¢" if v < 1 or v > 99 else f"{v:.0f}¢"


@app.template_filter("shares")
def fmt_shares(x):
    return f"{x:,.2f}"


@app.template_filter("when")
def when(s):
    if not s:
        return ""
    dt = datetime.fromisoformat(s)
    fmt = "%b %d, %I:%M %p" if dt.year == datetime.now().year else "%b %d %Y, %I:%M %p"
    return dt.strftime(fmt).replace(" 0", " ")


@app.context_processor
def _globals():
    return dict(csrf_token=csrf_token, site_name=SITE_NAME, market_open=market_open)


# ---------------------------------------------------------------- pages

def market_summaries(where, *args):
    rows = []
    for m in all_(f"SELECT * FROM markets WHERE {where} ORDER BY created_at DESC", *args):
        cs = all_("SELECT * FROM contracts WHERE market_id = ? ORDER BY sort, id", m["id"])
        ps = yes_prices(m, cs)
        vol = one("SELECT COALESCE(SUM(ABS(t.cents)),0) FROM trades t JOIN contracts c ON c.id = t.contract_id "
                  "WHERE c.market_id = ?", m["id"])[0]
        ranked = sorted(cs, key=lambda c: -ps[c["id"]]) if m["kind"] == "exclusive" else list(cs)
        rows.append(dict(m=m, contracts=[(c, ps[c["id"]]) for c in ranked], volume=vol))
    return rows


@app.route("/")
@login_required
def index():
    return render_template(
        "index.html",
        open_markets=[r for r in market_summaries("status != 'resolved'") if market_open(r["m"])],
        closed_markets=[r for r in market_summaries("status != 'resolved'") if not market_open(r["m"])],
        resolved_markets=market_summaries("status = 'resolved'"),
    )


@app.route("/m/<int:mid>")
@login_required
def market(mid):
    m, cs = load_market(mid)
    ps = yes_prices(m, cs)
    mine = {}
    for pos in all_("SELECT p.* FROM positions p JOIN contracts c ON c.id = p.contract_id "
                    "WHERE p.user_id = ? AND c.market_id = ? AND p.shares > ?", g.user["id"], mid, EPS):
        mine.setdefault(pos["contract_id"], []).append(pos)
    history = {}
    for c in cs:
        pts = all_("SELECT price, created_at FROM price_history WHERE contract_id = ? ORDER BY id", c["id"])
        history[c["id"]] = [[r["created_at"], round(r["price"], 4)] for r in pts][-300:]
    trades = all_("SELECT t.*, u.username, c.name AS cname FROM trades t JOIN users u ON u.id = t.user_id "
                  "JOIN contracts c ON c.id = t.contract_id WHERE c.market_id = ? ORDER BY t.id DESC LIMIT 40", mid)
    holders = {}
    if g.user["is_admin"]:
        for r in all_("SELECT p.contract_id, p.side, SUM(p.shares) s FROM positions p JOIN contracts c ON c.id = p.contract_id "
                      "WHERE c.market_id = ? GROUP BY p.contract_id, p.side", mid):
            holders[(r["contract_id"], r["side"])] = r["s"]
    return render_template("market.html", m=m, contracts=cs, prices=ps, mine=mine, history=history,
                           trades=trades, holders=holders, is_open=market_open(m))


@app.get("/api/quote")
@login_required
def api_quote():
    try:
        c = one("SELECT * FROM contracts WHERE id = ?", int(request.args["cid"]))
        if not c:
            raise UserError("No such contract")
        m, cs = load_market(c["market_id"])
        side, action = request.args.get("side"), request.args.get("action", "buy")
        if side not in ("YES", "NO") or action not in ("buy", "sell"):
            raise UserError("Bad request")
        if action == "buy":
            amount = parse_cents(request.args.get("amount"))
            r = compute_trade(m, cs, c, side, "buy", amount_cents=amount)
            return jsonify(ok=True, shares=r["shares"], cost=amount / 100, avg=amount / 100 / r["shares"],
                           payout=math.floor(r["shares"] * 100 + 1e-6) / 100,
                           price_before=r["price_before"], price_after=r["price_after"],
                           over_balance=amount > g.user["balance"])
        n = float(request.args.get("shares", "0"))
        if not n > 0:
            raise UserError("Enter a number of shares")
        r = compute_trade(m, cs, c, side, "sell", shares=n)
        return jsonify(ok=True, shares=n, proceeds=r["cents"] / 100, avg=r["cents"] / 100 / n,
                       price_before=r["price_before"], price_after=r["price_after"])
    except (UserError, ValueError, KeyError) as e:
        return jsonify(ok=False, error=str(e) if isinstance(e, UserError) else "Invalid input")


@app.post("/trade")
@login_required
def trade():
    try:
        cid = int(request.form.get("contract_id", 0))
        side, action = request.form.get("side"), request.form.get("action")
        if side not in ("YES", "NO") or action not in ("buy", "sell"):
            raise UserError("Bad request")
        if action == "buy":
            amount = parse_cents(request.form.get("amount"))
        else:
            try:
                req_shares = float(request.form.get("shares", "0"))
            except ValueError:
                raise UserError("Enter a number of shares")
            if not req_shares > 0:
                raise UserError("Enter a number of shares")
        with tx() as d:
            c = d.execute("SELECT * FROM contracts WHERE id = ?", (cid,)).fetchone()
            if not c:
                raise UserError("No such contract")
            m = d.execute("SELECT * FROM markets WHERE id = ?", (c["market_id"],)).fetchone()
            cs = d.execute("SELECT * FROM contracts WHERE market_id = ? ORDER BY sort, id", (m["id"],)).fetchall()
            if not contract_tradable(m, c):
                raise UserError("Trading is closed for this contract")
            uid = g.user["id"]
            balance = d.execute("SELECT balance FROM users WHERE id = ?", (uid,)).fetchone()[0]
            pos = d.execute("SELECT * FROM positions WHERE user_id = ? AND contract_id = ? AND side = ?",
                            (uid, cid, side)).fetchone()
            if action == "buy":
                if amount > balance:
                    raise UserError(f"Not enough balance ({money(balance)} available)")
                r = compute_trade(m, cs, c, side, "buy", amount_cents=amount)
                d.execute("INSERT INTO positions(user_id, contract_id, side, shares, basis, net) VALUES (?,?,?,?,?,?) "
                          "ON CONFLICT(user_id, contract_id, side) DO UPDATE SET shares = positions.shares + excluded.shares, "
                          "basis = positions.basis + excluded.basis, net = positions.net + excluded.net",
                          (uid, cid, side, r["shares"], amount, amount))
            else:
                held = pos["shares"] if pos else 0
                if req_shares > held + EPS:
                    raise UserError(f"You only have {held:,.2f} {side} shares")
                n = held if req_shares >= held - 1e-4 else req_shares
                r = compute_trade(m, cs, c, side, "sell", shares=n)
                left = held - n
                basis_out = pos["basis"] if left < EPS else round(pos["basis"] * n / held)
                d.execute("UPDATE positions SET shares = ?, basis = basis - ?, net = net - ? "
                          "WHERE user_id = ? AND contract_id = ? AND side = ?",
                          (0 if left < EPS else left, basis_out, r["cents"], uid, cid, side))
            d.execute("UPDATE contracts SET q = ? WHERE id = ?", (r["new_q"], cid))
            label = c["name"] if c["name"] == m["title"] else f"{m['title']} - {c['name']}"
            verb = "Bought" if action == "buy" else "Sold"
            credit(d, uid, r["cents"], "trade", f"{verb} {r['shares']:,.2f} {side} · {label}")
            d.execute("INSERT INTO trades(user_id, contract_id, side, action, shares, cents, price_before, price_after, created_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (uid, cid, side, action, r["shares"], r["cents"], r["price_before"], r["price_after"], now()))
            record_prices(d, r["all_after"] if m["kind"] == "exclusive" else {cid: r["price_after"]})
        flash(f"{verb} {r['shares']:,.2f} {side} shares of “{c['name']}” for {money(abs(r['cents']))}", "ok")
        return redirect(url_for("market", mid=m["id"]))
    except UserError as e:
        flash(str(e), "error")
        return redirect(request.referrer or url_for("index"))


def open_position_rows(user_id):
    rows = []
    for pos in all_("SELECT p.*, c.name cname, c.market_id, c.q, c.outcome FROM positions p "
                    "JOIN contracts c ON c.id = p.contract_id WHERE p.user_id = ? AND p.shares > ? "
                    "AND c.outcome IS NULL", user_id, EPS):
        m, cs = load_market(pos["market_id"])
        p = yes_prices(m, cs)[pos["contract_id"]]
        sp = lmsr.side_price(p, pos["side"])
        value = pos["shares"] * sp * 100
        rows.append(dict(pos=pos, m=m, price=sp, value=value, pnl=value - pos["basis"],
                         avg=pos["basis"] / 100 / pos["shares"]))
    return rows


@app.route("/portfolio")
@login_required
def portfolio():
    uid = g.user["id"]
    positions = open_position_rows(uid)
    return render_template(
        "portfolio.html", positions=positions,
        positions_value=sum(r["value"] for r in positions),
        ledger=all_("SELECT * FROM ledger WHERE user_id = ? ORDER BY id DESC LIMIT 200", uid),
        withdrawals=all_("SELECT * FROM withdrawals WHERE user_id = ? ORDER BY id DESC LIMIT 20", uid))


@app.post("/withdraw")
@login_required
def request_withdrawal():
    try:
        cents = parse_cents(request.form.get("amount"))
        with tx() as d:
            bal = d.execute("SELECT balance FROM users WHERE id = ?", (g.user["id"],)).fetchone()[0]
            if cents > bal:
                raise UserError(f"You can withdraw at most {money(bal)}")
            credit(d, g.user["id"], -cents, "withdrawal", "Withdrawal requested")
            d.execute("INSERT INTO withdrawals(user_id, cents, note, created_at) VALUES (?,?,?,?)",
                      (g.user["id"], cents, request.form.get("note", "")[:200], now()))
        flash(f"Withdrawal of {money(cents)} requested. The admin will send it and mark it paid.", "ok")
    except UserError as e:
        flash(str(e), "error")
    return redirect(url_for("portfolio"))


@app.post("/password")
@login_required
def change_password():
    if not check_password_hash(g.user["password_hash"], request.form.get("current", "")):
        flash("Current password is wrong", "error")
    elif len(request.form.get("new", "")) < 8:
        flash("New password must be at least 8 characters", "error")
    else:
        with tx() as d:
            d.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                      (generate_password_hash(request.form["new"]), g.user["id"]))
        flash("Password changed", "ok")
    return redirect(url_for("portfolio"))


def user_stats():
    stats = []
    for u in all_("SELECT * FROM users ORDER BY username"):
        sums = {r["kind"]: r["s"] for r in all_("SELECT kind, SUM(cents) s FROM ledger WHERE user_id = ? GROUP BY kind", u["id"])}
        pending = one("SELECT COALESCE(SUM(cents),0) FROM withdrawals WHERE user_id = ? AND status = 'pending'", u["id"])[0]
        paid = one("SELECT COALESCE(SUM(cents),0) FROM withdrawals WHERE user_id = ? AND status = 'paid'", u["id"])[0]
        value = sum(r["value"] for r in open_position_rows(u["id"]))
        funded = sums.get("deposit", 0) + sums.get("adjust", 0)
        profit = u["balance"] + pending + paid + value - funded
        stats.append(dict(u=u, deposits=sums.get("deposit", 0), adjust=sums.get("adjust", 0), pending=pending,
                          paid=paid, value=value, profit=profit, equity=u["balance"] + value))
    return stats


@app.route("/leaderboard")
@login_required
def leaderboard():
    return render_template("leaderboard.html", stats=sorted(user_stats(), key=lambda s: -s["profit"]))


# ---------------------------------------------------------------- admin

def house_exposure():
    """Payouts still owed on unresolved contracts: worst case and at current prices (cents)."""
    worst = expected = 0.0
    for m in all_("SELECT * FROM markets WHERE status != 'resolved'"):
        cs = all_("SELECT * FROM contracts WHERE market_id = ? ORDER BY sort, id", m["id"])
        ps = yes_prices(m, cs)
        held = {(r["contract_id"], r["side"]): r["s"] for r in all_(
            "SELECT p.contract_id, p.side, SUM(p.shares) s FROM positions p JOIN contracts c ON c.id = p.contract_id "
            "WHERE c.market_id = ? AND c.outcome IS NULL GROUP BY p.contract_id, p.side", m["id"])}
        live = [c for c in cs if c["outcome"] is None]
        if m["kind"] == "exclusive" and live:
            total_no = sum(held.get((c["id"], "NO"), 0) for c in live)
            scen = {c["id"]: held.get((c["id"], "YES"), 0) + total_no - held.get((c["id"], "NO"), 0) for c in live}
            worst += max(scen.values())
            expected += sum(ps[cid] * v for cid, v in scen.items())
        else:
            for c in live:
                y, n = held.get((c["id"], "YES"), 0), held.get((c["id"], "NO"), 0)
                worst += max(y, n)
                expected += ps[c["id"]] * y + (1 - ps[c["id"]]) * n
    return worst * 100, expected * 100


@app.route("/admin")
@admin_required
def admin():
    deposits = one("SELECT COALESCE(SUM(cents),0) FROM ledger WHERE kind = 'deposit'")[0]
    paid_out = one("SELECT COALESCE(SUM(cents),0) FROM withdrawals WHERE status = 'paid'")[0]
    balances = one("SELECT COALESCE(SUM(balance),0) FROM users")[0]
    pending = one("SELECT COALESCE(SUM(cents),0) FROM withdrawals WHERE status = 'pending'")[0]
    worst, expected = house_exposure()
    cash = deposits - paid_out
    owed = balances + pending
    summary = dict(cash=cash, owed=owed, house_now=cash - owed, worst=worst, expected=expected,
                   house_worst=cash - owed - worst, house_expected=cash - owed - expected)
    return render_template(
        "admin.html", summary=summary, stats=user_stats(),
        withdrawals=all_("SELECT w.*, u.username FROM withdrawals w JOIN users u ON u.id = w.user_id "
                         "WHERE w.status = 'pending' ORDER BY w.id"),
        invites=all_("SELECT * FROM invites WHERE used_by IS NULL ORDER BY created_at DESC"),
        ledger=all_("SELECT l.*, u.username FROM ledger l JOIN users u ON u.id = l.user_id "
                    "WHERE l.kind != 'trade' ORDER BY l.id DESC LIMIT 100"))


@app.post("/admin/invite")
@admin_required
def admin_invite():
    with tx() as d:
        d.execute("INSERT INTO invites(code, note, created_at) VALUES (?,?,?)",
                  (secrets.token_urlsafe(9), request.form.get("note", "")[:80], now()))
    flash("Invite created - copy the link and send it to your friend.", "ok")
    return redirect(url_for("admin") + "#invites")


@app.post("/admin/invite/<code>/revoke")
@admin_required
def admin_revoke_invite(code):
    with tx() as d:
        d.execute("DELETE FROM invites WHERE code = ? AND used_by IS NULL", (code,))
    return redirect(url_for("admin") + "#invites")


@app.post("/admin/user/<int:uid>/money")
@admin_required
def admin_money(uid):
    kind = request.form.get("kind")
    note = request.form.get("note", "").strip()[:200]
    try:
        with tx() as d:
            u = d.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
            if not u:
                abort(404)
            if kind == "deposit":
                cents = parse_cents(request.form.get("amount"))
                credit(d, uid, cents, "deposit", note or "Deposit")
            elif kind == "withdraw":
                cents = parse_cents(request.form.get("amount"))
                if cents > u["balance"]:
                    raise UserError(f"{u['username']} only has {money(u['balance'])}")
                credit(d, uid, -cents, "withdrawal", note or "Cash out")
                d.execute("INSERT INTO withdrawals(user_id, cents, status, note, created_at, handled_at) "
                          "VALUES (?,?,'paid',?,?,?)", (uid, cents, note, now(), now()))
            elif kind == "adjust":
                cents = parse_cents(request.form.get("amount"), allow_negative=True)
                credit(d, uid, cents, "adjust", note or "Adjustment")
            else:
                raise UserError("Unknown action")
        flash(f"{kind.title()} of {money(abs(cents))} recorded for {u['username']}", "ok")
    except UserError as e:
        flash(str(e), "error")
    return redirect(url_for("admin") + "#players")


@app.post("/admin/user/<int:uid>/password")
@admin_required
def admin_reset_password(uid):
    pw = request.form.get("password", "")
    if len(pw) < 8:
        flash("Temporary password must be at least 8 characters", "error")
    else:
        with tx() as d:
            d.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(pw), uid))
        flash("Password reset. Tell them to change it from their Portfolio page.", "ok")
    return redirect(url_for("admin") + "#players")


@app.post("/admin/withdrawal/<int:wid>/<action>")
@admin_required
def admin_withdrawal(wid, action):
    with tx() as d:
        w = d.execute("SELECT * FROM withdrawals WHERE id = ? AND status = 'pending'", (wid,)).fetchone()
        if not w:
            abort(404)
        if action == "paid":
            d.execute("UPDATE withdrawals SET status = 'paid', handled_at = ? WHERE id = ?", (now(), wid))
        elif action == "reject":
            d.execute("UPDATE withdrawals SET status = 'rejected', handled_at = ? WHERE id = ?", (now(), wid))
            credit(d, w["user_id"], w["cents"], "withdrawal_refund", "Withdrawal request rejected")
        else:
            abort(400)
    flash(f"Withdrawal of {money(w['cents'])} marked {'paid' if action == 'paid' else 'rejected (refunded)'}", "ok")
    return redirect(url_for("admin"))


def parse_close_at(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).replace(second=0, microsecond=0).isoformat(sep=" ")
    except ValueError:
        raise UserError("Couldn't read the closing time")


@app.route("/admin/new", methods=["GET", "POST"])
@admin_required
def admin_new_market():
    form = request.form
    if request.method == "POST":
        try:
            title = form.get("title", "").strip()
            kind = form.get("kind")
            if not title:
                raise UserError("Give the market a question/title")
            if kind not in ("exclusive", "independent"):
                raise UserError("Pick a market type")
            try:
                b = float(form.get("b") or 20)
            except ValueError:
                raise UserError("Liquidity must be a number")
            if not 1 <= b <= 100000:
                raise UserError("Liquidity must be between 1 and 100000")
            lines = parse_contract_lines(form.get("contracts"))
            if not lines:
                if kind == "exclusive":
                    raise UserError("An exclusive market needs at least two outcomes")
                lines = [(title, None)]
            if kind == "exclusive" and len(lines) < 2:
                raise UserError("An exclusive market needs at least two outcomes")
            with tx() as d:
                m = d.execute("INSERT INTO markets(title, description, kind, b, close_at, created_at) VALUES (?,?,?,?,?,?) "
                              "RETURNING *", (title, form.get("description", "").strip(), kind, b,
                                              parse_close_at(form.get("close_at")), now())).fetchone()
                add_contracts(d, m, lines)
            flash("Market created", "ok")
            return redirect(url_for("market", mid=m["id"]))
        except UserError as e:
            flash(str(e), "error")
    return render_template("new_market.html", form=form)


@app.post("/admin/m/<int:mid>/<action>")
@admin_required
def admin_market_action(mid, action):
    form = request.form
    try:
        with tx() as d:
            m = d.execute("SELECT * FROM markets WHERE id = ?", (mid,)).fetchone()
            if not m:
                abort(404)
            if m["status"] == "resolved" and action not in ("edit",):
                raise UserError("This market is already resolved")
            if action == "edit":
                title = form.get("title", "").strip()
                if not title:
                    raise UserError("Title can't be empty")
                d.execute("UPDATE markets SET title = ?, description = ? WHERE id = ?",
                          (title, form.get("description", "").strip(), mid))
                if m["status"] != "resolved":
                    d.execute("UPDATE markets SET close_at = ? WHERE id = ?", (parse_close_at(form.get("close_at")), mid))
                msg = "Market updated"
            elif action == "add":
                lines = parse_contract_lines(form.get("contracts"))
                if not lines:
                    raise UserError("Enter at least one contract")
                add_contracts(d, m, lines)
                msg = f"Added {len(lines)} contract(s)"
            elif action == "rename":
                name = form.get("name", "").strip()
                if not name:
                    raise UserError("Name can't be empty")
                d.execute("UPDATE contracts SET name = ? WHERE id = ? AND market_id = ?",
                          (name[:120], int(form.get("contract_id", 0)), mid))
                msg = "Contract renamed"
            elif action == "close":
                d.execute("UPDATE markets SET status = 'closed' WHERE id = ?", (mid,))
                msg = "Trading halted"
            elif action == "reopen":
                d.execute("UPDATE markets SET status = 'open' WHERE id = ?", (mid,))
                if m["close_at"] and now() >= m["close_at"]:
                    d.execute("UPDATE markets SET close_at = NULL WHERE id = ?", (mid,))
                msg = "Trading reopened"
            elif action == "resolve_winner":
                cs = d.execute("SELECT * FROM contracts WHERE market_id = ?", (mid,)).fetchall()
                winner = int(form.get("winner", 0))
                if m["kind"] != "exclusive" or winner not in [c["id"] for c in cs]:
                    raise UserError("Pick the winning outcome")
                for c in cs:
                    settle_contract(d, m, c, "YES" if c["id"] == winner else "NO")
                name = next(c["name"] for c in cs if c["id"] == winner)
                d.execute("UPDATE markets SET resolution_note = ? WHERE id = ?", (f"Winner: {name}", mid))
                maybe_finish_market(d, m)
                msg = f"Resolved - {name} wins. Payouts sent."
            elif action == "resolve_contract":
                outcome = form.get("outcome")
                c = d.execute("SELECT * FROM contracts WHERE id = ? AND market_id = ? AND outcome IS NULL",
                              (int(form.get("contract_id", 0)), mid)).fetchone()
                if not c or outcome not in ("YES", "NO", "CANCEL") or m["kind"] != "independent":
                    raise UserError("Can't resolve that contract")
                settle_contract(d, m, c, outcome)
                maybe_finish_market(d, m)
                msg = f"“{c['name']}” resolved {outcome}"
            elif action == "cancel":
                for c in d.execute("SELECT * FROM contracts WHERE market_id = ? AND outcome IS NULL", (mid,)).fetchall():
                    settle_contract(d, m, c, "CANCEL")
                d.execute("UPDATE markets SET resolution_note = 'Cancelled - all bets refunded' WHERE id = ?", (mid,))
                maybe_finish_market(d, m)
                msg = "Market cancelled and all bets refunded"
            else:
                abort(400)
        flash(msg, "ok")
    except UserError as e:
        flash(str(e), "error")
    return redirect(url_for("market", mid=mid))


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def _err(e):
    return render_template("message.html", title=f"{e.code} - {e.name}", body=e.description), e.code


if __name__ == "__main__":
    app.run(debug=True, port=5000)
