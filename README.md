# 🐕 Corgi Markets

A private prediction market for you and your friends. You're the admin and the bank: you create markets, take people's money (Venmo, cash, whatever), credit their balances, resolve the bets and pay out withdrawals. The app keeps the books.

## Run it

Double-click `start.bat`, or:

```
.venv\Scripts\python serve.py
```

Open http://localhost:8000. The first visit asks you to create the **admin account**.

All data lives in `data/market.db` (SQLite). **Back this file up.** It's the record of who owes what.

## Letting friends in

Your friends need a way to reach your computer. The easiest private option:

- **Tailscale** (recommended): install it on your PC and invite friends to your tailnet (or share the machine). They browse to `http://<your-pc-name>:8000`. Nothing is exposed to the public internet.
- **Cloudflare Tunnel**: `cloudflared tunnel --url http://localhost:8000` gives you a public https URL. Anyone with the link can see the login page, so use strong passwords.
- **A small cloud VM** (Fly.io, Railway, a $5 VPS): copy the folder, `pip install -r requirements.txt`, run `python serve.py`. Use a persistent volume and set `CORGI_DATA` to point at it.

Then go to **Admin → Invite friends**, create a link, and send it. Each link works once. Open the admin page through the same address your friends use before copying, so the link has the right host.

## Money flow

1. A friend sends you money → **Admin → Players → Money → Deposit** credits their balance.
2. They trade. Balances can't go negative from trading.
3. They request a withdrawal on their Portfolio page. The amount is held from their balance and shows up under **Pending withdrawals**. Send them the money, then click **Mark paid** (or **Reject** to return it to their balance).
   You can also cash someone out directly with **Money → Cash out**.
4. **Adjust** handles bonuses or corrections that don't involve real cash.

The admin dashboard shows the cash you're holding, what you owe players, and the house's result if all open markets resolve now (at current odds and in the worst case).

## Markets

**Admin → New market**. Two types:

| Type | Use it for | How it resolves |
|---|---|---|
| **Yes / No** | A single question (leave the contracts box empty), or a list of separate yes/no contracts under one heading, e.g. "Who shows up to game night?" with one line per person | Each contract resolves YES / NO / Cancel on its own, whenever you want |
| **Pick one** | Exactly one outcome wins, e.g. "Who wins the league?" Prices always add up to 100% | You pick the winner. That contract pays YES, all the others pay NO |

Contracts go one per line. You can set a starting chance with `|`:

```
Alice | 40%
Bob | 25
Carol
```

You can **add contracts later** from the market page, along with renaming them, editing the title/description/close time, halting and reopening trading, resolving, or cancelling with full refunds.

### How pricing works

There's no order book, so nobody has to wait for someone to take the other side. An automated market maker (LMSR) always quotes a price:

- A share pays **$1** if it's right and $0 if it's wrong. The price is the market's probability.
- Buying pushes the price up. **Liquidity (b)** controls how much: higher b means prices move less per dollar and more of your money is at risk. The new-market form shows the maximum subsidy as you type. At b=20, a yes/no market risks at most about $14.
- People can buy YES or NO on any contract, including "Pick one" outcomes, and sell back at any time before resolution.
- **Cancel** reverses everyone's net spend on that contract exactly.

## Notes

- Real-money betting among friends may still count as gambling where you live. Check your local rules.
- The app never touches real payment rails. Money moves outside the app and you record it, which keeps things simple and avoids storing anyone's payment details.
