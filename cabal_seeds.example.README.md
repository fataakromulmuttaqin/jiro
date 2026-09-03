# Jiro Sniper Net — Cabal Seed Database

This file tracks **known cabal/funding wallets** you've identified from
external sources (GMGN, Frontrun.pro, KOL chats, Twitter, etc).

When `cabal_detector.py` finds a cluster whose shared_funder address
matches one in this file, the cluster gets **+0.3 score boost** and gets
labeled with your cabal name on the website.

## How to add a cabal

1. Find the funder address (master wallet that sends SOL to many children).
   Sources:
   - **GMGN** → token page → "Top Traders" tab → click a top trader → "Funded by" → that's the funder
   - **Frontrun.pro** → wallet profile → "Funding Source" panel
   - **Birdeye/Nansen** (if you have a paid sub)
   - **Twitter/X KOL alpha** (people post screenshots of fund flows)

2. Open `cabal_seeds.json` (copy from `cabal_seeds.example.json` if it
   doesn't exist yet).

3. Add an entry:
   ```json
   {
     "5Q544fKrR8F2wKnWAZLzWqGvKfYJ8v9x2F3aBcDeFgHi": "CashCartel",
     "9Fk2aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890AbCd": "MevSquad"
   }
   ```

4. Run `python3 sync_website_data.py` to copy → website/public/data/
   and `vercel --prod --yes` (or push to master for auto-deploy).

## Cold start

If `cabal_seeds.json` doesn't exist, the detector still works — it just
won't boost scores for known cabals. You'll see CABAL labels but no
names until you start adding entries.

## Privacy

This file is **not** gitignored by default (it's just addresses + names,
no private keys). If you want to keep your watchlist private, add it to
`.gitignore` and use a local-only workflow.

## Scoring impact

```
cabal_score =
  +0.5  shared funder detected (any)
  +0.3  shared funder is in seeds DB
  +0.3  co-funding within 10 min
  +0.2  co-buy within 5 min, per extra member (capped at 2)
  +0.2  all winners
  -0.2  all losers
```

So a cabal you know about with 2 winners and same-funder timing = 0.5 +
0.3 + 0.3 + 0.2 + 0.2 = **1.0 (max, definitely CABAL)**.

## Related modules

- `cabal_detector.py` — reads seeds from `CABAL_SEED_PATH` env var
  (defaults to `~/ruangkerja/jiro/cabal_seeds.json`)
- `sync_website_data.py` — copies `cabal_seeds.json` →
  `website/public/data/cabal_seeds.json` (bundled in the deploy)