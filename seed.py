"""Launch markets. Safe to run on every boot: adds any market that is missing and
refreshes the rules text and close time on ones nobody has traded yet.
Edit freely, or create markets from the Admin tab instead."""
from datetime import datetime

import engine as eng

SEED_MARKETS = [
    {
        "question": "Who wins the Big Ten Women's Soccer Tournament title tonight?",
        "category": "Soccer",
        "rules": ("No. 3 seed Purdue hosts No. 4 seed Michigan State at Folk Field, Fri Nov 13 at 6:00 PM ET (BTN). "
                  "Goes to whoever wins the Big Ten Tournament final, extra time and penalty kicks included. "
                  "Purdue won the regular season meeting 2-1. Trading closes at kickoff. If the match isn't "
                  "finished by Sunday, the market is cancelled and refunded."),
        "outcomes": [("Purdue", 0.58), ("Michigan State", 0.42)],
        "closes_at": datetime(2026, 11, 13, 18, 0, tzinfo=eng.ET),
        "b": 250.0,
    },
    {
        "question": "Does Purdue MBB beat Ohio tonight at Mackey?",
        "category": "Basketball",
        "rules": ("Purdue (1-2) hosts Ohio (2-0, the Bobcats) at Mackey Arena, Fri Nov 13 at 7:00 PM ET. Yes if "
                  "Purdue wins, overtime included. Trading closes at tip. If the game isn't played by Sunday, "
                  "the market is cancelled and refunded."),
        "outcomes": [("Yes", 0.92), ("No", 0.08)],
        "closes_at": datetime(2026, 11, 13, 19, 0, tzinfo=eng.ET),
        "b": 250.0,
    },
    {
        "question": "Does Purdue MBB beat Ohio by 16 or more?",
        "category": "Basketball",
        "rules": ("Same game: Purdue vs Ohio, Fri Nov 13 at 7:00 PM ET. Yes if Purdue wins by 16 or more, "
                  "overtime included. Otherwise No. Trading closes at tip. If the game isn't played by Sunday, "
                  "the market is cancelled and refunded."),
        "outcomes": [("Yes", 0.50), ("No", 0.50)],
        "closes_at": datetime(2026, 11, 13, 19, 0, tzinfo=eng.ET),
        "b": 250.0,
    },
    {
        "question": "Who wins Purdue at Iowa?",
        "category": "Football",
        "rules": ("Purdue (3-6) at No. 16 Iowa (7-2), Kinnick Stadium, Sat Nov 14 at 7:30 PM ET (NBC). Goes to "
                  "whoever wins, overtime included. Trading closes at kickoff. If the game isn't played by "
                  "Monday, the market is cancelled and refunded."),
        "outcomes": [("Purdue", 0.12), ("Iowa", 0.88)],
        "closes_at": datetime(2026, 11, 14, 19, 30, tzinfo=eng.ET),
        "b": 250.0,
    },
    {
        "question": "When does West Lafayette get its first measurable snow of the season?",
        "category": "Weather",
        "rules": ("Goes to the window that contains the first day with 0.1 inch or more of snow at West Lafayette in "
                  "the National Weather Service daily climate records (NWS Indianapolis). Flurries and a trace "
                  "don't count. If that data is missing, the admin uses NWS Indianapolis snowfall reports for "
                  "the area. Eastern time. Trading stops as soon as the admin confirms qualifying snow. No "
                  "measurable snow yet as of Nov 13. The long-run average first measurable snow here is Dec 3."),
        "outcomes": [("Nov 13 to Nov 19", 0.06), ("Nov 20 to Nov 30", 0.22), ("Dec 1 to Dec 10", 0.27),
                     ("Dec 11 to Dec 20", 0.20), ("Dec 21 to Dec 31", 0.13), ("Jan 1 or later", 0.12)],
        "closes_at": datetime(2026, 12, 31, 23, 59, tzinfo=eng.ET),
        "b": 250.0,
    },
]


def ensure_markets() -> int:
    """Adds missing seed markets. Existing ones with no trades get fresh rules text and close time."""
    existing = {m["question"]: m for m in eng.list_markets()}
    added = 0
    for m in SEED_MARKETS:
        have = existing.get(m["question"])
        if have:
            eng.update_market_meta(have["id"], m["rules"], m["category"], m["closes_at"])
            continue
        eng.create_market(m["question"], m["outcomes"], m["rules"], m["category"], m["b"], m["closes_at"])
        added += 1
    return added


run_if_empty = ensure_markets   # old name, kept so nothing breaks


if __name__ == "__main__":
    eng.init_db()
    print(f"Added {ensure_markets()} markets")
