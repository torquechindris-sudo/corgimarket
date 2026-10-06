# 🐕 Corgi Markets

A private prediction market for you and your friends. You're the admin and the bank: you create markets, take people's money (Venmo, cash, whatever), credit their balances, resolve the bets and pay out withdrawals. The app keeps the books.

## Hosting on Vercel

Vercel's disk resets between requests, so the hosted app stores its data in Postgres (Neon, free tier) instead of a file.

1. **Vercel → Add New → Project →** import this GitHub repo. Leave the defaults and deploy. The first deploy shows an error page saying no database is connected. That's expected.
2. **Project → Storage → Create Database → Neon (Serverless Postgres)**. Pick the region closest to the project's functions (US East by default) and connect it to the project. This sets `DATABASE_URL`.
3. **Deployments → ⋯ → Redeploy**.
4. Open the site **right away** and create the admin account. The first person to visit claims it.

Optional environment variables (Project → Settings → Environment Variables):

| Variable | Default | Purpose |
|---|---|---|
| `TIMEZONE` | `America/New_York` | Timezone for close times and timestamps |
| `CORGI_SITE_NAME` | `Corgi Markets` | Name shown in the header |

Tables and the login-signing key are created automatically on first start. Every push to `main` redeploys. Neon keeps point-in-time backups; you can also export the data from the Neon console.

## Running locally

Double-click `start.bat`, or:

```
.venv\Scripts\python serve.py
```

Open http://localhost:8000. Without `DATABASE_URL` set, local data lives in `data/market.db` (SQLite), completely separate from the hosted site. To run locally against the hosted database instead, set `DATABASE_URL` to the Neon connection string.

## Letting friends in

Go to **Admin → Invite friends**, create a link, and send it. Each link works once. Copy links from the hosted site, not localhost, so they point at the right address.

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
