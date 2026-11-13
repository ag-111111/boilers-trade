# Boilers Trade

Trade on Purdue. Prove you called it.

Boilers Trade is a prediction market for Purdue students. Everyone starts with fake cash, buys shares in what they think will happen (games, weather, whatever), and climbs the leaderboard if they're right.

**Play it:** https://boilers-trade.streamlit.app

## How it works

- You start with $1,000 of fake cash and get a small bonus each day.
- A share pays $1 if you're right and $0 if you're wrong.
- The price of a share is the crowd's odds. A 70¢ share means the market thinks it's about 70% likely.
- Buy when you think the odds are too low, sell when they're too high. Your rank is your cash plus what your shares would sell for right now.

## Fake money, for real

Nobody can deposit, withdraw, or cash out. Balances are a score, not currency, and nothing here is betting or financial advice. This is a student project and is not affiliated with Purdue University, Purdue Athletics, the Big Ten, or the NCAA.

## Under the hood

Prices come from an LMSR market maker (logarithmic market scoring rule), the same idea used by academic prediction markets. Trades are path independent, prices always add up to 100%, and the house's worst case loss per market is capped.

Built with Python, Streamlit, SQLite, and Plotly. Passwords are hashed with scrypt, and every trade is a single atomic database transaction.

## Run it yourself

```
pip install -r requirements.txt
streamlit run app.py
```

Optional environment variables:

| Var | What it does |
| --- | --- |
| `BOILERS_ADMIN_PASSWORD` | Sets the admin password |
| `BOILERS_INVITE_CODE` | Sets the signup invite code |
| `BOILERS_DB` | Database file path |

Engine tests: `python test_engine.py`. Headless app test: `python smoke_test.py`.

## Contact

Found a bug or want a market added? Message the creator.
