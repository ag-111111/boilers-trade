"""Boilers Trade: a prediction market for Purdue.
Run:  streamlit run app.py
All money logic lives in engine.py. This file is UI only."""
import os
import re
from datetime import datetime, time as dtime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import engine as eng
import seed
from engine import MarketError

st.set_page_config(page_title="Boilers Trade", page_icon="🚂", layout="wide")

GOLD = "#CFB991"
LINE_COLORS = [GOLD, "#FFFFFF", "#8E6F3E", "#6F727B", "#DAAA00", "#C4BFC0"]
STATUS_BADGE = {"OPEN": ("Open", "green"), "CLOSED": ("Awaiting result", "orange"),
                "RESOLVED": ("Resolved", "gray"), "CANCELLED": ("Cancelled", "red")}

st.markdown("""
<style>
:root {--gold: #CFB991; --gold-hi: #E6D3A3; --gold-dim: #8E6F3E; --ink: #000000; --panel: #0B0B0B;}
.stApp {background: var(--ink); border-top: 4px solid var(--gold);}
header[data-testid="stHeader"] {background: transparent;}
.block-container {padding-top: 2.2rem; max-width: 1200px;}
h1, h2, h3, h4, h5 {color: var(--gold) !important; letter-spacing: -0.01em;}
hr {border-color: var(--gold-dim) !important;}
a {color: var(--gold) !important;}

/* cards */
div[data-testid="stVerticalBlockBorderWrapper"] {border-color: rgba(207,185,145,0.38) !important;
    background: var(--panel); border-radius: 10px;}
div[data-testid="stExpander"] details {border-color: rgba(207,185,145,0.38) !important; background: var(--panel);}

/* buttons: gold with black text, or black with gold outline */
button[data-testid^="stBaseButton-primary"] {background: var(--gold) !important; color: #000 !important;
    border: 1px solid var(--gold) !important; font-weight: 800 !important;}
button[data-testid^="stBaseButton-primary"]:hover {background: var(--gold-hi) !important;}
button[data-testid^="stBaseButton-primary"] p {color: #000 !important; font-weight: 800 !important;}
button[data-testid^="stBaseButton-primary"]:disabled {opacity: 0.35;}
button[data-testid^="stBaseButton-secondary"] {background: #000 !important; color: var(--gold) !important;
    border: 1px solid var(--gold-dim) !important;}
button[data-testid^="stBaseButton-secondary"]:hover {border-color: var(--gold) !important;}
button[data-testid^="stBaseButton-tertiary"] {color: var(--gold) !important;}

/* tabs, metrics, inputs */
button[data-baseweb="tab"] p {font-weight: 700;}
button[data-baseweb="tab"][aria-selected="true"] p {color: var(--gold) !important;}
div[data-testid="stMetricValue"] {color: var(--gold);}
div[data-testid="stMetricLabel"] {color: #B8AE98;}
div[data-baseweb="input"], div[data-baseweb="textarea"], div[data-baseweb="select"] > div {
    border-color: rgba(207,185,145,0.35) !important; background: #0B0B0B !important;}

div[data-testid="stAlertContainer"] {background: #15120B !important; border: 1px solid var(--gold-dim);
    color: var(--gold-hi) !important;}
div[data-testid="stAlertContainer"] p {color: var(--gold-hi) !important;}

.bt-brand {font-size: 1.9rem; font-weight: 800; letter-spacing: -0.02em; color: #FFFFFF;}
.bt-brand span {color: var(--gold);}
.bt-sub {color: #B8AE98; font-size: 0.9rem; margin-top: -0.4rem;}
.bt-q {font-size: 1.08rem; font-weight: 700; line-height: 1.3; margin-bottom: 0.2rem; color: #FFFFFF;}
.bt-meta {color: #B8AE98; font-size: 0.8rem;}
.bt-row {display: flex; justify-content: space-between; font-size: 0.95rem; margin: 0.35rem 0 0.1rem 0;}
.bt-row b {color: var(--gold);}
.bt-bar {height: 6px; border-radius: 3px; background: #1F1B14; overflow: hidden;}
.bt-bar div {height: 100%; background: linear-gradient(90deg, var(--gold-dim), var(--gold));}
.bt-tape {font-size: 0.85rem; padding: 0.25rem 0; border-bottom: 1px solid #1F1B14;}
.bt-foot {margin-top: 2.5rem; padding-top: 0.9rem; border-top: 1px solid var(--gold-dim); color: #8F8674;
    font-size: 0.75rem; line-height: 1.5; text-align: center;}
</style>
""", unsafe_allow_html=True)

def md(text: str) -> str:
    """Escape bare dollar signs so Streamlit never reads $...$ as math."""
    return re.sub(r"(?<!\\)\$", r"\\$", text)


DISCLAIMER_SHORT = ("Fake money, no cash value, nothing to cash out. Boilers Trade is a student project and "
                    "isn't affiliated with Purdue, the Big Ten, or the NCAA.")

RULES_MD = md(f"""
#### The basics
Everyone starts with **${eng.START_CASH:,.0f}** of fake cash. Buy shares in what you think will happen. A share
pays $1 if you're right and $0 if you're wrong, and its price is the crowd's odds, so a 70¢ share means about a
70% chance. Every trade has a {eng.FEE_RATE * 100:.0f}% fee.

#### It's fake money
Nobody can deposit, withdraw, or cash out, so this is a game, not gambling. Your balance is a score. It can't be
bought, sold, or traded for anything, and nothing here is betting or financial advice.

#### Not affiliated
Boilers Trade is a student project. It has nothing to do with Purdue, Purdue Athletics, the Big Ten, the NCAA, or
any team or person named in a market.

#### How markets settle
Every market says what counts and when trading stops. The admin settles from public results and the call is
final. If a game gets postponed or the result is unclear, the market is cancelled and everyone is refunded.

#### Ground rules
- One account each.
- Keep chirps clean: no harassment, hate, or anyone's private info.
- No markets on anyone's private life, health, or grades.
- Athletes and staff: check your own team or department rules before you play.
- Accounts and balances can be wiped or reset at any time, bugs included. No guarantees.

#### Your info
We keep your email, username, and a hashed password. Your username, trades, and chirps are visible to other
players. Don't reuse a password you care about.
""")


def footer():
    st.markdown(f'<div class="bt-foot">{DISCLAIMER_SHORT}</div>', unsafe_allow_html=True)


# ----------------------------------------------------------------------------
# Boot + config
# ----------------------------------------------------------------------------
def secret(key: str) -> str | None:
    """Env var first, then Streamlit secrets."""
    if os.environ.get(key):
        return os.environ[key]
    try:
        return str(st.secrets[key]) if key in st.secrets else None
    except Exception:
        return None


@st.cache_resource
def boot():
    eng.init_db(admin_password=secret("BOILERS_ADMIN_PASSWORD"))
    seed.ensure_markets()
    return True


def cfg() -> dict:
    keys = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM"]
    return {k: v for k in keys if (v := secret(k))}


boot()
CFG = cfg()
VERIFY_ON = all(CFG.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS"))
INVITE_OVERRIDE = secret("BOILERS_INVITE_CODE")
INVITE_ON = eng.invite_required(INVITE_OVERRIDE)


# ----------------------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------------------
def money(x: float) -> str:
    return f"${x:,.2f}"


def pct(p: float) -> str:
    if 0 < p < 0.01:
        return "<1%"
    if 0.99 < p < 1:
        return ">99%"
    return f"{p * 100:.0f}%"


def esc(text: str) -> str:
    """Neutralize markdown and HTML in user-supplied text."""
    for ch in "\\`*_{}[]()#+-!|<>~$:":
        text = text.replace(ch, "\\" + ch)
    return text


def html_esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def when(ts: str | None) -> str:
    if not ts:
        return ""
    return eng.parse(ts).astimezone(eng.ET).strftime("%a %b %d, %I:%M %p ET").replace(" 0", " ")


def closes_label(m: dict) -> str:
    if m["status"] != "OPEN":
        return STATUS_BADGE[m["status"]][0]
    if not m["closes_at"]:
        return "No close time"
    left = eng.parse(m["closes_at"]) - eng.now()
    mins = int(left.total_seconds() // 60)
    if mins < 60:
        return f"Closes in {max(mins, 1)} min"
    if mins < 60 * 24:
        return f"Closes in {mins // 60}h {mins % 60}m"
    return f"Closes {when(m['closes_at'])}"


def flash(msg: str, icon: str = "✅"):
    st.session_state["flash"] = (msg, icon)


def show_flash():
    if "flash" in st.session_state:
        msg, icon = st.session_state.pop("flash")
        st.toast(msg, icon=icon)


def prob_bars(m: dict) -> str:
    rows = []
    for o in m["outcomes"]:
        mark = " ✓" if m["status"] == "RESOLVED" and o["id"] == m["winner_id"] else ""
        rows.append(f'<div class="bt-row"><span>{html_esc(o["label"])}{mark}</span><b>{pct(o["price"])}</b></div>'
                    f'<div class="bt-bar"><div style="width:{o["price"] * 100:.1f}%"></div></div>')
    return "".join(rows)


# ----------------------------------------------------------------------------
# Logged-out view
# ----------------------------------------------------------------------------
def auth_view():
    st.markdown('<div class="bt-brand">🚂 Boilers <span>Trade</span></div>'
                '<div class="bt-sub">Trade on Purdue. Prove you called it.</div>',
                unsafe_allow_html=True)
    st.write("")
    left, right = st.columns([1, 1.2], gap="large")

    with left:
        names = ["Log in", "Sign up"] + (["Verify"] if VERIFY_ON else [])
        tabs = st.tabs(names)
        with tabs[0]:
            with st.form("login"):
                ident = st.text_input("Purdue email or username")
                pw = st.text_input("Password", type="password")
                if st.form_submit_button("Log in", type="primary", width="stretch"):
                    try:
                        st.session_state["uid"] = eng.login(ident, pw)
                        st.rerun()
                    except MarketError as e:
                        st.error(str(e))
        with tabs[1]:
            with st.form("signup"):
                email = st.text_input(f"Purdue email (@{eng.ALLOWED_DOMAIN})")
                uname = st.text_input("Username (shown on the leaderboard)")
                pw1 = st.text_input("Password (8+ characters)", type="password")
                invite = st.text_input("Invite code") if INVITE_ON else ""
                st.caption(md(f"You start with {money(eng.START_CASH)} of fake cash."))
                agree = st.checkbox("I get that the money here is fake and can't be cashed out, and I'm "
                                    "good with the rules.")
                if st.form_submit_button("Create account", type="primary", width="stretch"):
                    try:
                        if not agree:
                            raise MarketError("Tick the box first.")
                        eng.check_invite(invite, INVITE_OVERRIDE)
                        code = eng.register(email, uname, pw1, require_verify=VERIFY_ON)
                        if code:
                            eng.send_code_email(email.strip().lower(), code, CFG)
                            st.session_state["pending_email"] = email.strip().lower()
                            st.success("Code sent to your Purdue inbox. Enter it in the Verify tab.")
                        else:
                            st.session_state["uid"] = eng.login(email, pw1)
                            st.rerun()
                    except MarketError as e:
                        st.error(str(e))
                    except Exception:
                        st.error("Could not send the verification email. Try again in a minute.")
        if VERIFY_ON:
            with tabs[2]:
                with st.form("verify"):
                    v_email = st.text_input("Purdue email", value=st.session_state.get("pending_email", ""))
                    v_code = st.text_input("6 digit code")
                    if st.form_submit_button("Verify", type="primary", width="stretch"):
                        try:
                            st.session_state["uid"] = eng.verify_email(v_email, v_code)
                            st.rerun()
                        except MarketError as e:
                            st.error(str(e))

    with right:
        st.markdown("#### Live markets")
        live = [m for m in eng.list_markets() if m["status"] in ("OPEN", "CLOSED")][:6]
        if not live:
            st.caption("No markets yet.")
        for m in live:
            with st.container(border=True):
                st.markdown(f'<div class="bt-q">{html_esc(m["question"])}</div>'
                            f'<div class="bt-meta">{html_esc(m["category"])} · {closes_label(m)} · '
                            f'{money(m["volume"])} traded</div>{prob_bars(m)}', unsafe_allow_html=True)
    with st.expander("Rules"):
        st.markdown(RULES_MD)
    footer()


# ----------------------------------------------------------------------------
# Markets
# ----------------------------------------------------------------------------
def market_card(m: dict, key: str):
    with st.container(border=True):
        st.markdown(f'<div class="bt-q">{html_esc(m["question"])}</div>'
                    f'<div class="bt-meta">{html_esc(m["category"])} · {closes_label(m)} · '
                    f'{money(m["volume"])} traded</div>{prob_bars(m)}', unsafe_allow_html=True)
        st.write("")
        label = "Trade" if m["status"] == "OPEN" else "View"
        if st.button(label, key=f"open_{key}_{m['id']}", width="stretch",
                     type="primary" if m["status"] == "OPEN" else "secondary"):
            st.session_state["market_id"] = m["id"]
            st.rerun()


def markets_view(user: dict):
    if st.session_state.get("market_id"):
        try:
            market_detail(user, st.session_state["market_id"])
            return
        except MarketError:
            st.session_state.pop("market_id", None)

    markets = eng.list_markets()
    cats = sorted({m["category"] for m in markets})
    c1, c2 = st.columns([2, 1])
    cat = c1.pills("Category", ["All"] + cats, default="All", label_visibility="collapsed") or "All"
    show = c2.segmented_control("Show", ["Live", "Settled"], default="Live", label_visibility="collapsed") or "Live"
    wanted = ("OPEN", "CLOSED") if show == "Live" else ("RESOLVED", "CANCELLED")
    rows = [m for m in markets if m["status"] in wanted and (cat == "All" or m["category"] == cat)]
    if show == "Settled":
        rows.sort(key=lambda m: m["resolved_at"] or "", reverse=True)
    else:
        rows.sort(key=lambda m: m["status"] != "OPEN")   # list is already in close-time order
    if not rows:
        st.info("Nothing here yet.")
        return
    cols = st.columns(2)
    for k, m in enumerate(rows):
        with cols[k % 2]:
            market_card(m, "list")


def price_chart(m: dict):
    hist = pd.DataFrame(eng.price_history(m["id"]))
    fig = go.Figure()
    for k, o in enumerate(m["outcomes"]):
        h = hist[hist["outcome_id"] == o["id"]]
        xs = [eng.parse(t).astimezone(eng.ET) for t in h["ts"]]
        ys = [p * 100 for p in h["price"]]
        if m["status"] in ("OPEN", "CLOSED") and xs:
            xs.append(eng.now().astimezone(eng.ET))
            ys.append(ys[-1])
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", name=o["label"], line_shape="hv",
                                 line=dict(color=LINE_COLORS[k % len(LINE_COLORS)], width=2.5),
                                 hovertemplate="%{y:.1f}%<extra>" + html_esc(o["label"]) + "</extra>"))
    fig.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0), template="plotly_dark",
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      yaxis=dict(range=[0, 100], ticksuffix="%", gridcolor="#1F1B14"),
                      xaxis=dict(gridcolor="#1F1B14"), hovermode="x unified",
                      legend=dict(orientation="h", y=1.12, x=0))
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})


def trade_panel(user: dict, m: dict):
    with st.container(border=True):
        st.markdown("**Trade**")
        if m["status"] != "OPEN":
            st.info("Trading is closed on this market.")
            return
        if user["is_admin"]:
            st.info("Admin can't trade.")
            return
        by_id = {o["id"]: o for o in m["outcomes"]}
        oid = st.radio("Outcome", list(by_id), horizontal=True, key=f"oc_{m['id']}",
                       format_func=lambda i: f"{by_id[i]['label']} · {pct(by_id[i]['price'])}")
        held = next((p for p in eng.positions(user["id"]) if p["outcome_id"] == oid), None)
        side = st.segmented_control("Side", ["Buy", "Sell"], default="Buy", key=f"side_{m['id']}",
                                    label_visibility="collapsed") or "Buy"

        if side == "Buy":
            if user["cash"] < eng.MIN_SPEND:
                st.warning("You're out of cash. Grab your daily bonus up top.")
                return
            spend = st.number_input("Amount ($)", min_value=eng.MIN_SPEND, max_value=float(int(user["cash"])),
                                    value=float(min(25, int(user["cash"]))), step=5.0, key=f"amt_{m['id']}")
            q = eng.quote_buy(oid, spend)
            a, b = st.columns(2)
            a.metric("Shares", f"{q['shares']:,.1f}")
            b.metric("Avg price", f"{q['avg_price'] * 100:.1f}¢")
            st.caption(md(f"Pays **{money(q['shares'])}** if {esc(by_id[oid]['label'])} wins "
                          f"({(q['shares'] / spend - 1) * 100:+.0f}%). Price goes to "
                          f"{pct(q['prices_after'][list(by_id).index(oid)])}. Fee: {money(q['fee'])}."))
            if st.button(f"Buy {by_id[oid]['label']}", type="primary", width="stretch", key=f"buy_{m['id']}"):
                try:
                    r = eng.buy(user["id"], oid, spend, min_shares=q["shares"] * 0.97)
                    flash(f"Bought {r['shares']:,.1f} {by_id[oid]['label']} for {money(spend)}")
                    st.rerun()
                except MarketError as e:
                    st.error(str(e))
        else:
            if not held:
                st.caption("You don't own any of this one.")
                return
            n = st.number_input(f"Shares to sell (you hold {held['shares']:,.2f})", min_value=0.01,
                                max_value=float(held["shares"]), value=float(held["shares"]),
                                key=f"sh_{m['id']}_{oid}")
            q = eng.quote_sell(oid, n)
            a, b = st.columns(2)
            a.metric("You get", money(q["net"]))
            b.metric("Avg price", f"{q['avg_price'] * 100:.1f}¢")
            st.caption(md(f"Price goes to {pct(q['prices_after'][list(by_id).index(oid)])}. "
                          f"Fee: {money(q['fee'])}."))
            if st.button(f"Sell {by_id[oid]['label']}", type="primary", width="stretch", key=f"sell_{m['id']}"):
                try:
                    r = eng.sell(user["id"], oid, n, min_proceeds=q["net"] * 0.97)
                    flash(f"Sold {n:,.1f} {by_id[oid]['label']} for {money(r['net'])}")
                    st.rerun()
                except MarketError as e:
                    st.error(str(e))


def market_detail(user: dict, market_id: int):
    m = eng.get_market(market_id)
    if st.button("← All markets", type="tertiary"):
        st.session_state.pop("market_id", None)
        st.rerun()
    text, color = STATUS_BADGE[m["status"]]
    st.markdown(f"### {esc(m['question'])}")
    st.badge(text, color=color)
    st.caption(md(f"{esc(m['category'])} · {closes_label(m)} · {money(m['volume'])} traded"))

    left, right = st.columns([1.7, 1], gap="large")
    with left:
        price_chart(m)
        if m["rules"]:
            with st.expander("Resolution rules", expanded=True):
                st.write(esc(m["rules"]))

        st.markdown("**Chirp box**")
        if not user["is_admin"]:
            with st.form(f"cm_{m['id']}", clear_on_submit=True, border=False):
                c1, c2 = st.columns([5, 1])
                body = c1.text_input("Comment", max_chars=280, placeholder="Say something...",
                                     label_visibility="collapsed")
                if c2.form_submit_button("Post", width="stretch"):
                    try:
                        eng.add_comment(user["id"], m["id"], body)
                        st.rerun()
                    except MarketError as e:
                        st.error(str(e))
        comments = eng.list_comments(m["id"])
        if not comments:
            st.caption("Nobody's chirped yet.")
        for cm in comments:
            st.markdown(f"**{esc(cm['username'])}** · {esc(cm['body'])}")

    with right:
        trade_panel(user, m)
        mine = [p for p in eng.positions(user["id"]) if p["market_id"] == m["id"]]
        if mine:
            with st.container(border=True):
                st.markdown("**Your position**")
                for p in mine:
                    color = "green" if p["pnl"] >= 0 else "red"
                    st.markdown(md(f"{esc(p['label'])}: **{p['shares']:,.1f}** shares @ {p['avg_cost'] * 100:.1f}¢  \n"
                                f"Worth {money(p['value'])} now · :{color}[{p['pnl']:+,.2f}] · "
                                f"pays {money(p['payout_if_win'])} if it wins"))
        with st.container(border=True):
            st.markdown("**Recent trades**")
            tape(eng.recent_trades(8, market_id=m["id"]), show_market=False)


def tape(rows: list[dict], show_market: bool = True):
    if not rows:
        st.caption("No trades yet.")
        return
    verbs = {"BUY": "bought", "SELL": "sold", "PAYOUT": "was paid on", "REFUND": "was refunded"}
    lines = []
    for t in rows:
        what = f"{verbs[t['kind']]} {html_esc(t['label'] or '')}".strip()
        where = f" · {html_esc(t['question'])}" if show_market else ""
        lines.append(f'<div class="bt-tape"><b>{html_esc(t["username"])}</b> {what} '
                     f'<b style="color:{GOLD}">{money(abs(t["amount"]))}</b>'
                     f'<span class="bt-meta">{where} · {when(t["ts"])}</span></div>')
    st.markdown("".join(lines), unsafe_allow_html=True)


# ----------------------------------------------------------------------------
# Portfolio, leaderboard, activity
# ----------------------------------------------------------------------------
def portfolio_view(user: dict):
    pos = eng.positions(user["id"])
    value = sum(p["value"] for p in pos)
    cost = sum(p["cost"] for p in pos)
    a, b, c = st.columns(3)
    a.metric("Cash", money(user["cash"]))
    b.metric("Positions (sell value)", money(value), delta=f"{value - cost:+,.2f}" if pos else None)
    c.metric("Net worth", money(user["cash"] + value))
    st.markdown("#### Open positions")
    if pos:
        df = pd.DataFrame([{"Market": p["question"], "Outcome": p["label"], "Shares": p["shares"],
                            "Avg cost": p["avg_cost"], "Price": p["price"], "Sell value": p["value"],
                            "P&L": p["pnl"], "Pays if wins": p["payout_if_win"]} for p in pos])
        st.dataframe(df, hide_index=True, column_config={
            "Shares": st.column_config.NumberColumn(format="%.1f"),
            "Avg cost": st.column_config.NumberColumn(format="$%.3f"),
            "Price": st.column_config.NumberColumn(format="$%.3f"),
            "Sell value": st.column_config.NumberColumn(format="$%.2f"),
            "P&L": st.column_config.NumberColumn(format="$%.2f"),
            "Pays if wins": st.column_config.NumberColumn(format="$%.2f")})
    else:
        st.caption("No positions yet.")
    st.markdown("#### Your history")
    hist = eng.recent_trades(50, user_id=user["id"])
    if hist:
        st.dataframe(pd.DataFrame([{"When": when(t["ts"]), "Type": t["kind"], "Market": t["question"],
                                    "Outcome": t["label"] or "", "Shares": t["shares"], "Cash": t["amount"],
                                    "Fee": t["fee"]} for t in hist]),
                     hide_index=True, column_config={
                         "Shares": st.column_config.NumberColumn(format="%.1f"),
                         "Cash": st.column_config.NumberColumn(format="$%.2f"),
                         "Fee": st.column_config.NumberColumn(format="$%.2f")})
    else:
        st.caption("Nothing yet.")


def leaderboard_view(user: dict):
    board = eng.leaderboard()
    me = next((r for r in board if r["user_id"] == user["id"]), None)
    if me:
        st.markdown(md(f"You are **#{me['rank']}** of {len(board)} with **{money(me['net_worth'])}**."))
    st.caption("Net worth = cash + what your shares would sell for right now.")
    if not board:
        st.caption("No players yet.")
        return
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    df = pd.DataFrame([{"Rank": medals.get(r["rank"], str(r["rank"])),
                        "Player": r["username"] + ("  (you)" if r["user_id"] == user["id"] else ""),
                        "Net worth": r["net_worth"], "Cash": r["cash"], "Positions": r["positions"]}
                       for r in board[:100]])
    st.dataframe(df, hide_index=True, column_config={
        "Net worth": st.column_config.NumberColumn(format="$%.2f"),
        "Cash": st.column_config.NumberColumn(format="$%.2f"),
        "Positions": st.column_config.NumberColumn(format="$%.2f")})


@st.fragment(run_every="15s")
def activity_view():
    st.caption("Updates every 15 seconds.")
    tape(eng.recent_trades(40))


# ----------------------------------------------------------------------------
# Admin
# ----------------------------------------------------------------------------
def admin_view(user: dict):
    t = eng.treasury()
    a, b, c, d = st.columns(4)
    a.metric("Players", t["players"])
    b.metric("Volume", money(t["volume"]))
    c.metric("Fees collected", money(t["fees"]))
    d.metric("Market maker P&L (settled)", money(t["amm_pnl"]))
    if not VERIFY_ON:
        st.caption(f"Email verification is off, so anyone can sign up with any @{eng.ALLOWED_DOMAIN} address. "
                   "Fine for a soft launch.")

    with st.expander("Create market", expanded=False):
        with st.form("new_market", clear_on_submit=True):
            q = st.text_input("Question", placeholder="Does Purdue beat IU by 10+?")
            rules = st.text_area("Resolution rules (source, what counts, what happens on a postponement)")
            c1, c2, c3 = st.columns(3)
            cat = c1.text_input("Category", value="General")
            d_close = c2.date_input("Closes (ET date)", value=eng.now().astimezone(eng.ET).date())
            t_close = c3.time_input("Closes (ET time)", value=dtime(19, 0))
            opts = st.text_area("Outcomes, one per line. Optional starting odds after a colon.",
                                placeholder="Yes: 0.6\nNo: 0.4")
            liq = st.number_input("Liquidity b (higher = prices move slower)", min_value=10.0,
                                  value=eng.DEFAULT_B, step=25.0)
            st.caption(md("Worst case the house loses b × ln(1 / lowest starting odds) on a market. "
                          "At b = 200 with 50/50 odds that's about $139."))
            if st.form_submit_button("Launch market", type="primary"):
                try:
                    outcomes = []
                    for line in opts.splitlines():
                        if not line.strip():
                            continue
                        label, _, p = line.partition(":")
                        outcomes.append((label.strip(), float(p) if p.strip() else None))
                    closes = datetime.combine(d_close, t_close, tzinfo=eng.ET)
                    if closes <= eng.now():
                        raise MarketError("Close time is in the past.")
                    eng.create_market(q, outcomes, rules, cat, liq, closes)
                    flash("Market launched")
                    st.rerun()
                except ValueError:
                    st.error("Starting odds must be numbers, like 0.6")
                except MarketError as e:
                    st.error(str(e))

    st.markdown("#### Settle markets")
    pending = [m for m in eng.list_markets() if m["status"] in ("OPEN", "CLOSED")]
    if not pending:
        st.caption("Nothing to settle.")
    for m in pending:
        with st.container(border=True):
            st.markdown(f"**{esc(m['question'])}**")
            st.caption(md(f"{STATUS_BADGE[m['status']][0]} · {closes_label(m)} · {money(m['volume'])} traded · "
                          + " / ".join(f"{esc(o['label'])} {pct(o['price'])}" for o in m["outcomes"])))
            by_id = {o["id"]: o["label"] for o in m["outcomes"]}
            c1, c2, c3, c4 = st.columns([2, 1.2, 1, 1])
            winner = c1.selectbox("Winner", list(by_id), format_func=by_id.get, key=f"w_{m['id']}",
                                  index=None, placeholder="Pick the winner", label_visibility="collapsed")
            sure = c2.checkbox("Confirm, this is final", key=f"sure_{m['id']}")
            if c3.button("Resolve", key=f"res_{m['id']}", type="primary", disabled=not (winner and sure),
                         width="stretch"):
                paid = eng.resolve_market(user["id"], m["id"], winner)
                flash(f"Resolved. Paid out {money(paid)}")
                st.rerun()
            if c4.button("Cancel + refund", key=f"can_{m['id']}", disabled=not sure, width="stretch"):
                eng.cancel_market(user["id"], m["id"])
                flash("Cancelled and refunded", "↩️")
                st.rerun()
            if m["status"] == "OPEN" and st.button("Stop trading now", key=f"cl_{m['id']}", type="tertiary"):
                eng.close_market(user["id"], m["id"])
                st.rerun()

    with st.expander("Players"):
        users = eng.list_users()
        if users:
            st.dataframe(pd.DataFrame(users), hide_index=True,
                         column_config={"cash": st.column_config.NumberColumn(format="$%.2f")})


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    uid = st.session_state.get("uid")
    user = eng.get_user(uid) if uid else None
    if not user:
        st.session_state.pop("uid", None)
        auth_view()
        return

    show_flash()
    h1, h2, h3, h4 = st.columns([3, 1.3, 1.1, 0.9], vertical_alignment="center")
    h1.markdown('<div class="bt-brand">🚂 Boilers <span>Trade</span></div>', unsafe_allow_html=True)
    h2.markdown(md(f"**{esc(user['username'])}** · {money(user['cash'])}"))
    if not user["is_admin"]:
        if h3.button(f"Claim ${eng.DAILY_BONUS:.0f}", width="stretch"):
            try:
                eng.claim_daily(user["id"])
                flash(f"+{money(eng.DAILY_BONUS)} daily bonus", "💰")
            except MarketError as e:
                flash(str(e), "⏳")
            st.rerun()
    if h4.button("Log out", width="stretch"):
        st.session_state.clear()
        st.rerun()

    names = ["Markets", "Portfolio", "Leaderboard", "Activity", "Rules"] + (["Admin"] if user["is_admin"] else [])
    tabs = st.tabs(names)
    with tabs[0]:
        markets_view(user)
    with tabs[1]:
        portfolio_view(user)
    with tabs[2]:
        leaderboard_view(user)
    with tabs[3]:
        activity_view()
    with tabs[4]:
        st.markdown(RULES_MD)
    if user["is_admin"]:
        with tabs[5]:
            admin_view(user)
    footer()


main()
