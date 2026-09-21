# Macro desk: gold, Nasdaq and the dollar

Daily macro bias for XAUUSD, the Nasdaq and the DXY. `engine.py` scores each asset's drivers and writes `docs/bias.json`; `docs/index.html` shows it with a tab per asset and a side-by-side view of the shared drivers.

Setup
1. Push this folder to a new GitHub repo.
2. Get a free FRED key (fred.stlouisfed.org) and a free Alpha Vantage key. Add them as repo secrets `FRED_API_KEY` and `AV_API_KEY`.
3. Settings > Pages: deploy from branch `main`, folder `/docs`.
4. Edit `manual.json` when the hand-scored inputs change: `geopolitics` (gold), its `nasdaq_note`, `earnings_ai`, `breadth`, `more_hikes_signalled`, `policy_rates` (ECB, BoJ and BoE rates and their four-week changes, updated after each meeting), `policy_path`, `fiscal`, `growth`, and `cot_mode` (`contrarian` or `trend`).
5. Actions > Daily macro bias > Run workflow to test. It then runs every weekday at 22:30 UTC.

`python engine.py --snapshot snapshot.json` scores a hand-filled snapshot with no API keys.

Drivers
- Gold: real yields, Fed path, dollar, breakevens, war and fiscal risk (manual), price vs recent high, fund positioning.
- Dollar (DXY): rate gap vs Europe and Japan, expected policy path (manual edge), US real yields, safe-haven demand, oil shock, fiscal and Fed independence (manual), growth (manual), price trend, fund positioning. The index is rebuilt from six Alpha Vantage FX pairs (6 calls a day, 7 with gold, inside the free 25-a-day limit).
- Nasdaq: real yields, Fed path, risk appetite (VIX and high-yield spread), net liquidity, oil and war shock, earnings and AI spending (manual), breadth (manual), price vs record high, fund positioning.

COT: pulled from the CFTC public reporting API. Gold uses the Disaggregated Futures Only dataset (`72hh-3qpy`, code 088691). The Nasdaq (code 209742) and the dollar index (code 098662) use Traders in Financial Futures (`gpe5-46if`, leveraged funds). Dollar index futures are a thin market, so that driver only has a 6% weight. If a fetch fails, that driver is dropped and the other weights re-normalise. Confirm the dataset IDs and field names on publicreporting.cftc.gov if one fails.

The live fetchers (FRED, Alpha Vantage, CFTC) have not been run end to end yet, so expect to fix small parsing issues on the first run.
`docs/history.json` grows by one row per asset per day. Use it to backtest the weights.
