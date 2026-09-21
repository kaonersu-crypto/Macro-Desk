#!/usr/bin/env python3
"""Macro bias engine (prototype) for gold and the Nasdaq.

  python engine.py --snapshot snapshot.json   score a hand-filled snapshot
  python engine.py                            live run: needs FRED_API_KEY and AV_API_KEY

Writes docs/bias.json (read by docs/index.html) and appends to docs/history.json.
Each driver scores from -1 (bearish for the asset) to +1 (bullish). Weights are judgment
calls, not fitted to data. Backtest them against docs/history.json before trusting them.
"""
import argparse, json, os, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone

MINUS = "\u2212"


def clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def sgn(x, spec=",.0f"):
    s = format(abs(x), spec)
    return (MINUS if x < 0 else "+") + s


def bp(x):
    return sgn(x) + "bp"


# ---------------- scorers: each takes the full input dict, returns (score, reading) ----------------
def s_real_yield(i):
    d = i["real_yield"]
    score = -(0.5 * clamp((d["level"] - 1.0) / 2.0) + 0.5 * clamp(d["chg_2w_bp"] / 25))
    return score, f"10-year TIPS yield {d['level']:.2f}%, {bp(d['chg_2w_bp'])} in two weeks."


def s_fed(i):
    d = i["fed"]
    tight = clamp(clamp(d["last_move_bp"] / 50) + (0.5 if d.get("more_hikes_signalled") else 0))
    score = -(0.5 * tight + 0.5 * clamp(d["two_year_chg_2w_bp"] / 40))
    move = "unchanged" if d["last_move_bp"] == 0 else bp(d["last_move_bp"])
    return score, (f"Fed funds {d['lower']:.2f}\u2013{d['upper']:.2f}%, last move {move}. "
                   f"2-year yield {d['two_year']:.2f}%, {bp(d['two_year_chg_2w_bp'])} in two weeks.")


def s_dollar(i):
    d = i["dollar"]
    return -clamp(d["chg_2w_pct"] / 2), f"{d['label']} {d['index']:.2f}, {sgn(d['chg_2w_pct'], '.1f')}% in two weeks."


def s_breakevens(i):
    d = i["breakeven"]
    return clamp(d["chg_2w_bp"] / 15), f"10-year breakeven {d['level']:.2f}%, {bp(d['chg_2w_bp'])} in two weeks."


def s_geo_gold(i):
    d = i["geopolitics"]
    return clamp(d["score"]), d["note"]


def s_price_gold(i):
    g = i["gold"]
    dd = g["price"] / g["ref_high"] - 1
    return clamp(dd / 0.15), f"Gold ${g['price']:,.0f}, {abs(dd)*100:.1f}% below its {g['ref_high_label']}."


def s_price_nasdaq(i):
    g = i["nasdaq"]
    dd = g["price"] / g["ref_high"] - 1
    return clamp((dd + 0.05) / 0.05), f"Nasdaq Composite {g['price']:,.0f}, {abs(dd)*100:.1f}% below its {g['ref_high_label']}."


def s_cot(key):
    def f(i):
        d = i[key]
        crowd = clamp((d["index"] - 50) / 40)
        score = crowd if d.get("mode") == "trend" else -crowd
        side = "long" if d["net"] >= 0 else "short"
        ch = d["changes"][0]
        return score, (f"{d['group']} net {side} {abs(d['net']):,} contracts, COT index {d['index']:.0f} of 100 "
                       f"(positions as of {d['as_of']}), {sgn(ch['value'])} over {ch['label']}.")
    return f


def s_risk(i):
    d = i["risk"]
    vix = 0.5 * -clamp((d["vix"] - 20) / 10) + 0.5 * -clamp(d["vix_chg_2w"] / 5)
    hy = 0.5 * -clamp((d["hy"] - 3.5) / 1.5) + 0.5 * -clamp(d["hy_chg_2w_bp"] / 40)
    return 0.5 * vix + 0.5 * hy, (f"VIX {d['vix']:.1f} ({sgn(d['vix_chg_2w'], '.1f')}), "
                                  f"high-yield spread {d['hy']:.2f}% ({bp(d['hy_chg_2w_bp'])}).")


def s_liquidity(i):
    d = i["liquidity"]
    score = 0.5 * clamp((d["pct"] - 50) / 40) + 0.5 * clamp(d["chg_4w_bn"] / 100)
    return score, (f"Net liquidity ${d['net_bn']:,.0f}B, {ordinal(d['pct'])} percentile of five years, "
                   f"{sgn(d['chg_4w_bn'])}B over four weeks.")


def s_oil(i):
    d, g = i["oil"], i["geopolitics"]
    score = 0.7 * -clamp((d["price"] - 80) / 40) + 0.3 * -clamp(d["chg_2w_pct"] / 10)
    return score, f"WTI ${d['price']:.0f} a barrel, {sgn(d['chg_2w_pct'], '.1f')}% in two weeks. {g.get('nasdaq_note', '')}".strip()


def s_manual(key):
    def f(i):
        return clamp(i[key]["score"]), i[key]["note"]
    return f


def s_rate_gap(i):
    d, f = i["policy_rates"], i["fed"]
    w = {"ecb": 0.576, "boj": 0.136, "boe": 0.119}
    mid = (f["upper"] + f["lower"]) / 2
    gap = sum(w[k] * (mid - d[k]["rate"]) for k in w) / sum(w.values())
    foreign_move = sum(w[k] * d[k]["chg_4w"] for k in w) / sum(w.values())
    gap_chg = f["last_move_bp"] / 100 - foreign_move
    score = 0.5 * clamp((gap - 0.5) / 2.0) + 0.5 * clamp(gap_chg / 0.25)
    return score, (f"Fed funds mid {mid:.2f}% vs ECB {d['ecb']['rate']:.2f}%, BoJ {d['boj']['rate']:.2f}%, BoE {d['boe']['rate']:.2f}%. "
                   f"Weighted by index share, the US rate gap is {gap:.2f} points, {sgn(gap_chg, '.2f')} points over four weeks.")


def s_policy_path(i):
    d, f = i["policy_path"], i["fed"]
    score = 0.5 * clamp(f["two_year_chg_2w_bp"] / 40) + 0.5 * clamp(d["edge"])
    return score, f"US 2-year yield {bp(f['two_year_chg_2w_bp'])} in two weeks. {d['note']}"


def s_real_yield_usd(i):
    s, r = s_real_yield(i)
    return -s, r + " Higher US real yields draw in foreign money."


def s_risk_usd(i):
    s, r = s_risk(i)
    return -0.5 * s, r + " Calm markets mean less safe-haven demand for the dollar."


def s_oil_usd(i):
    d, g = i["oil"], i["geopolitics"]
    score = 0.5 * clamp((d["price"] - 70) / 40) + 0.5 * clamp(d["chg_2w_pct"] / 10)
    return score, f"WTI ${d['price']:.0f} a barrel, {sgn(d['chg_2w_pct'], '.1f')}% in two weeks. {g.get('dxy_note', '')}".strip()


def s_price_dxy(i):
    g = i["dxy"]
    dd = g["price"] / g["ref_high"] - 1
    score = 0.5 * clamp(g["chg_pct"] / 2) + 0.5 * clamp((dd + 0.03) / 0.03)
    return score, (f"DXY {g['price']:.2f}, {abs(dd)*100:.1f}% below its {g['ref_high_label']}, "
                   f"{sgn(g['chg_pct'], '.1f')}% over the {g['chg_label']}.")


# ---------------- asset definitions ----------------
GOLD_COT_WHY = {
    "contrarian": "Hedge funds already heavily long leave few buyers left, so crowded longs raise downside risk. Crowded shorts raise squeeze risk. Set to contrarian in manual.json.",
    "trend": "Funds adding to longs tends to confirm an uptrend, so heavy long positioning counts as bullish. Set to trend in manual.json.",
}
NQ_COT_WHY = {
    "contrarian": "Leveraged funds are often hedging, so this is a noisy signal. Contrarian mode treats a crowded long as a risk and a crowded short as squeeze fuel. Set in manual.json.",
    "trend": "Leveraged funds buying back shorts tends to go with rising prices. Set to trend in manual.json.",
}

DXY_COT_WHY = {
    "contrarian": "Dollar index futures are a thin market, so this is a noisy signal. Contrarian mode treats a crowded long as a risk. Set in manual.json.",
    "trend": "Funds adding to dollar longs tends to go with a rising dollar. Set to trend in manual.json.",
}

ASSETS = {
    "gold": {
        "title": "Gold",
        "weights": {"real_yield": 27, "fed_path": 18, "dollar": 13, "breakevens": 9,
                    "geopolitics": 13, "price": 10, "cot": 10},
        "scorers": {"real_yield": s_real_yield, "fed_path": s_fed, "dollar": s_dollar,
                    "breakevens": s_breakevens, "geopolitics": s_geo_gold, "price": s_price_gold,
                    "cot": s_cot("cot")},
        "meta": {
            "real_yield": ("Real yields", "real yields", "Gold pays no interest, so higher inflation-adjusted yields raise the cost of holding it."),
            "fed_path": ("Fed path", "the Fed path", "A tightening Fed and rising 2-year yields pull money toward cash and bonds."),
            "dollar": ("US dollar", "the dollar", "Gold is priced in dollars. A stronger dollar makes it dearer for buyers in other currencies."),
            "breakevens": ("Inflation expectations", "inflation expectations", "Rising breakevens support gold as an inflation hedge. Steady ones mean yields are rising in real terms, not on inflation fears."),
            "geopolitics": ("War and fiscal risk", "war and fiscal risk", "Conflict and government borrowing worries drive demand for gold as a store of value. This score is entered by hand."),
            "price": ("Price vs recent high", "price action", "Checks whether the market is agreeing with the fundamentals."),
            "cot": ("Fund positioning", "fund positioning", GOLD_COT_WHY),
        },
        "cot_key": "cot",
    },
    "nasdaq": {
        "title": "Nasdaq",
        "weights": {"real_yield": 22, "fed_path": 14, "risk": 14, "liquidity": 9, "geopolitics": 10,
                    "earnings_ai": 12, "breadth": 8, "price": 6, "cot": 5},
        "scorers": {"real_yield": s_real_yield, "fed_path": s_fed, "risk": s_risk, "liquidity": s_liquidity,
                    "geopolitics": s_oil, "earnings_ai": s_manual("earnings_ai"), "breadth": s_manual("breadth"),
                    "price": s_price_nasdaq, "cot": s_cot("cot_nq")},
        "meta": {
            "real_yield": ("Real yields", "real yields", "Growth stocks are valued on distant earnings. Higher real yields discount those earnings harder."),
            "fed_path": ("Fed path", "the Fed path", "Tighter policy raises borrowing costs and competes with stocks for money."),
            "risk": ("Risk appetite", "risk appetite", "Low volatility and tight credit spreads mean investors are comfortable owning risk."),
            "liquidity": ("Net liquidity", "liquidity", "Fed assets minus the Treasury cash account and reverse repos. More liquidity has tended to lift risk assets."),
            "geopolitics": ("Oil and war shock", "the oil and war shock", "Expensive oil feeds inflation, which keeps the Fed hiking and hurts growth stocks. Gold reads the same war as support."),
            "earnings_ai": ("Earnings and AI spending", "AI earnings momentum", "The largest stocks are about half the index, so their earnings and AI spending set the tone. This score is entered by hand."),
            "breadth": ("Market breadth", "narrow breadth", "Broad participation makes rallies sturdier. Narrow leadership makes them fragile. This score is entered by hand."),
            "price": ("Price vs record high", "price action", "Trend confirmation. Trading within a few percent of the record counts as bullish."),
            "cot": ("Fund positioning", "fund positioning", NQ_COT_WHY),
        },
        "cot_key": "cot_nq",
    },
    "dxy": {
        "title": "Dollar (DXY)",
        "weights": {"rate_gap": 20, "policy_path": 20, "real_yield": 10, "risk": 10, "geopolitics": 10,
                    "fiscal": 10, "growth": 8, "price": 6, "cot": 6},
        "scorers": {"rate_gap": s_rate_gap, "policy_path": s_policy_path, "real_yield": s_real_yield_usd,
                    "risk": s_risk_usd, "geopolitics": s_oil_usd, "fiscal": s_manual("fiscal"),
                    "growth": s_manual("growth"), "price": s_price_dxy, "cot": s_cot("cot_dxy")},
        "meta": {
            "rate_gap": ("Rate gap vs Europe and Japan", "the rate gap", "The index is 57.6% euro and 13.6% yen. A wider gap between US rates and theirs pulls money into dollars."),
            "policy_path": ("Expected policy path", "the policy path", "Markets trade where central banks are heading, not just where rates are. A Fed priced for more hikes than the ECB and BoJ supports the dollar. The edge is entered by hand."),
            "real_yield": ("US real yields", "US real yields", "Higher inflation-adjusted returns on Treasuries draw in foreign money."),
            "risk": ("Safe-haven demand", "safe-haven demand", "The dollar tends to catch a bid when volatility and credit spreads jump. Calm markets take that support away."),
            "geopolitics": ("Oil shock", "the oil shock", "The US is a net energy exporter. Expensive oil hurts importers like Europe and Japan and helps the dollar against them."),
            "fiscal": ("Fiscal and Fed independence", "fiscal worries", "Heavy borrowing and pressure on the Fed can push yields up while the dollar falls. This score is entered by hand."),
            "growth": ("Growth and data", "US growth", "Stronger US data than abroad supports the dollar. This score is entered by hand."),
            "price": ("Price trend", "price trend", "Trend confirmation. A rising index close to its recent high counts as bullish."),
            "cot": ("Fund positioning", "fund positioning", DXY_COT_WHY),
        },
        "cot_key": "cot_dxy",
    },
}
SHARED = [("real_yield", "real_yield", "Real yields", "real yields"),
          ("fed_path", "policy_path", "Fed path", "the Fed path"),
          ("geopolitics", "geopolitics", "War and oil", "war and oil")]


def ordinal(n):
    n = int(round(n))
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def label_for(bias):
    if bias <= 35: return "bearish"
    if bias <= 45: return "leans bearish"
    if bias < 55: return "neutral"
    if bias < 65: return "leans bullish"
    return "bullish"


def price_display(name, i):
    g = i[name]
    dd = g["price"] / g["ref_high"] - 1
    value = {"gold": f"${g['price']:,.0f}", "nasdaq": f"{g['price']:,.0f}", "dxy": f"{g['price']:.2f}"}[name]
    return dd, {"value": value, "note": f"{abs(dd)*100:.1f}% below the {g['ref_high_label']}"}


def score_asset(name, inp):
    cfg = ASSETS[name]
    drivers = []
    for key, w in cfg["weights"].items():
        if key == "cot" and not inp.get(cfg["cot_key"]):
            continue  # COT feed missing: drop the driver and re-normalise
        s, reading = cfg["scorers"][key](inp)
        nm, short, why = cfg["meta"][key]
        if key == "cot":
            why = why["trend" if inp[cfg["cot_key"]].get("mode") == "trend" else "contrarian"]
        src = inp.get("sources", {}).get(name, {}).get(key, {})
        drivers.append({"key": key, "name": nm, "short": short, "weight": w,
                        "score": round(s, 3) + 0.0, "points": round(s * w, 1) + 0.0,
                        "reading": reading, "why": why,
                        "source": src.get("source", ""), "status": src.get("status", "")})
    total_w = sum(d["weight"] for d in drivers)
    net = sum(d["score"] * d["weight"] for d in drivers) / total_w
    bias = round(50 + 50 * net)
    label = label_for(bias)
    conviction = "strong" if abs(net) >= 0.5 else "moderate" if abs(net) >= 0.25 else "weak"

    side = -1 if net < 0 else 1
    same = sorted([d for d in drivers if d["points"] * side > 0], key=lambda d: -abs(d["points"]))
    opp = sorted([d for d in drivers if d["points"] * side < 0], key=lambda d: -abs(d["points"]))
    if label == "neutral" or len(same) < 2:
        headline = f"{cfg['title']} reads neutral: the drivers roughly cancel out."
    else:
        headline = f"{cfg['title']} reads {label}: {same[0]['short']} and {same[1]['short']}"
        headline += f" outweigh {opp[0]['short']}." if opp else " point the same way."

    by = {d["key"]: d for d in drivers}
    dd, pdisp = price_display(name, inp)
    flags = []
    if name == "gold" and by["real_yield"]["score"] <= -0.6 and by["price"]["score"] > -0.6:
        flags.append(f"Real yields are at {inp['real_yield']['level']:.2f}%, yet gold is only {abs(dd)*100:.1f}% below its "
                     f"recent high. Something besides rates is holding the price up. The war and fiscal risk driver is the "
                     f"likely candidate, and it is scored by hand.")
    if name == "nasdaq" and bias <= 45 and by["price"]["score"] >= 0.4:
        flags.append(f"The macro drivers lean bearish, yet the Nasdaq is only {abs(dd)*100:.1f}% below its record. "
                     f"The market is looking through higher rates, most likely on AI earnings. That gap can close either way.")
    if name == "dxy" and bias >= 55 and by["fiscal"]["score"] <= -0.15:
        flags.append("Rates and central bank paths favour the dollar, but fiscal worries and pressure on the Fed pull the other way. "
                     "A sell-America episode would show up as the dollar falling while US yields rise.")
    c = inp.get(cfg["cot_key"])
    if c and (c["index"] >= 80 or c["index"] <= 20):
        flags.append(f"Fund positioning is stretched (COT index {c['index']:.0f} of 100). "
                     "Crowded positioning makes sharp reversals more likely.")

    macro = [d for d in drivers if d["key"] != "price"]
    macro_net = sum(d["score"] * d["weight"] for d in macro) / sum(d["weight"] for d in macro)
    trend = by["price"]["score"] if "price" in by else 0.0
    macro_word = label_for(round(50 + 50 * macro_net))
    trend_word = "bullish" if trend >= 0.3 else "bearish" if trend <= -0.3 else "flat"
    if abs(macro_net) >= 0.15 and abs(trend) >= 0.3:
        state = "conflicted" if macro_net * trend < 0 else "aligned"
    else:
        state = "unclear"
    text = {"conflicted": f"Conflicted: the macro drivers read {macro_word}, but the price trend is {trend_word}. "
                          "When the two disagree, treat the bias with extra caution.",
            "aligned": f"Macro and price trend agree: both are {trend_word}.",
            "unclear": "Price trend is flat or the macro reading is weak, so the two neither confirm nor contradict each other."}[state]
    conflict = {"state": state, "macro": macro_word, "trend": trend_word, "text": text,
                "macro_net": round(macro_net, 3), "trend_score": round(trend, 3)}

    return {
        "title": cfg["title"],
        "conflict": conflict,
        "price": pdisp,
        "bias": {"score": bias, "net": round(net, 3), "label": label, "conviction": conviction,
                 "bearish_pts": round(sum(-d["points"] for d in drivers if d["points"] < 0), 1),
                 "bullish_pts": round(sum(d["points"] for d in drivers if d["points"] > 0), 1)},
        "headline": headline, "drivers": drivers, "flags": flags, "cot": c,
        "drawdown_pct": round(dd * 100, 1),
        "_price": inp[name]["price"],
    }


def compare(assets):
    def find(asset, key):
        return next((d for d in assets[asset]["drivers"] if d["key"] == key), None)
    rows, mirror = [], []
    for gk, dk, name, phrase in SHARED:
        g, n, u = find("gold", gk), find("nasdaq", gk), find("dxy", dk)
        if not (g and n):
            continue
        both = abs(g["score"]) >= 0.15 and abs(n["score"]) >= 0.15
        verdict = "mixed" if not both else ("agree" if g["score"] * n["score"] > 0 else "split")
        if u and both and abs(u["score"]) >= 0.15 and u["score"] * g["score"] < 0 and u["score"] * n["score"] < 0:
            mirror.append(phrase)
        rows.append({"key": gk, "name": name, "phrase": phrase, "gold": g["score"], "nasdaq": n["score"],
                     "dxy": u["score"] if u else None, "verdict": verdict})
    agree = [r["phrase"] for r in rows if r["verdict"] == "agree"]
    split = [r["phrase"] for r in rows if r["verdict"] == "split"]
    parts = []
    if agree:
        parts.append("Gold and the Nasdaq read the same way on " + " and ".join(agree) + ".")
    if split:
        parts.append("They split on " + " and ".join(split) + ": what supports gold weighs on the Nasdaq.")
    if mirror:
        parts.append("The dollar is the mirror image on " + " and ".join(mirror) + ": it gains where both of them lose.")
    return rows, " ".join(parts) or "No strong agreement or split between the assets right now."


def score(inp):
    assets = {n: score_asset(n, inp) for n in ASSETS}
    rows, note = compare(assets)
    return {"as_of": inp["as_of"], "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
            "assets": assets, "compare": rows, "compare_note": note, "watch": inp.get("watch", [])}


# ---------------- live data ----------------
def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:300]
        raise RuntimeError(f"HTTP {e.code} from {url.split('?')[0]}: {body}") from None


AV_GAP = 13  # seconds between Alpha Vantage calls: the free key allows 5 a minute
_last_av = [0.0]


def av_get(params, ak):
    wait = AV_GAP - (time.time() - _last_av[0])
    if wait > 0:
        time.sleep(wait)
    data = _get("https://www.alphavantage.co/query?" + urllib.parse.urlencode({**params, "apikey": ak}))
    _last_av[0] = time.time()
    for k in ("Note", "Information", "Error Message"):
        if k in data:
            raise RuntimeError(f"Alpha Vantage said: {data[k]}")
    return data


def fred(series, key, n=90):
    q = urllib.parse.urlencode({"series_id": series, "api_key": key, "file_type": "json",
                                "sort_order": "desc", "limit": n})
    obs = _get("https://api.stlouisfed.org/fred/series/observations?" + q)["observations"]
    return [(o["date"], float(o["value"])) for o in obs if o["value"] not in (".", "")]


def gold_history(key):
    try:
        rows = av_get({"function": "GOLD_SILVER_HISTORY", "symbol": "XAU", "interval": "daily"}, key)["data"]
        out = []
        for r in rows:
            try:
                out.append((r["date"], float(r.get("price") or r.get("value") or r.get("close"))))
            except (TypeError, ValueError, KeyError):
                pass
        if out:
            return sorted(out, reverse=True)
    except Exception as e:
        print("Gold history endpoint failed, trying XAU/USD FX instead:", e)
    return sorted(fx_daily("XAU", "USD", key).items(), reverse=True)


def cot_live(dataset, code, long_f, short_f, ol_f, os_f, group, other_label, market, mode):
    """Positioning from the CFTC public reporting API. Returns None on any failure so the run still finishes."""
    try:
        q = urllib.parse.urlencode({"$where": f"cftc_contract_market_code='{code}'",
                                    "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": 160})
        rows = _get(f"https://publicreporting.cftc.gov/resource/{dataset}.json?{q}")

        def g(r, f):
            return int(r[f] if r.get(f) is not None else r[f + "_all"])
        net = [g(r, long_f) - g(r, short_f) for r in rows]
        lo, hi = min(net), max(net)
        return {"group": group, "market": market, "as_of": rows[0]["report_date_as_yyyy_mm_dd"][:10], "net": net[0],
                "index": round(100 * (net[0] - lo) / (hi - lo)) if hi > lo else 50,
                "changes": [{"label": "four weeks", "value": net[0] - net[4]},
                            {"label": "thirteen weeks", "value": net[0] - net[13]}],
                "other_label": other_label, "other_net": g(rows[0], ol_f) - g(rows[0], os_f), "mode": mode}
    except Exception as e:
        print("COT fetch failed:", market, e)
        return None


def fx_daily(base, quote, ak):
    ts = av_get({"function": "FX_DAILY", "from_symbol": base, "to_symbol": quote, "outputsize": "compact"}, ak)["Time Series FX (Daily)"]
    return {d: float(v["4. close"]) for d, v in ts.items()}


def dxy_series(ak):
    """Rebuild the ICE Dollar Index from its six currency pairs and official weights (about 0.1 off the real thing)."""
    eur, jpy, gbp = fx_daily("EUR", "USD", ak), fx_daily("USD", "JPY", ak), fx_daily("GBP", "USD", ak)
    cad, sek, chf = fx_daily("USD", "CAD", ak), fx_daily("USD", "SEK", ak), fx_daily("USD", "CHF", ak)
    dates = sorted(set(eur) & set(jpy) & set(gbp) & set(cad) & set(sek) & set(chf), reverse=True)
    return [(d, 50.14348112 * eur[d] ** -0.576 * jpy[d] ** 0.136 * gbp[d] ** -0.119
             * cad[d] ** 0.091 * sek[d] ** 0.042 * chf[d] ** 0.036) for d in dates]


def net_liquidity(fk):
    walcl, tga, rrp = fred("WALCL", fk, 270), dict(fred("WTREGEN", fk, 270)), dict(fred("RRPONTSYD", fk, 1400))
    nets = [w / 1000 - tga[d] / 1000 - rrp.get(d, 0) for d, w in walcl if d in tga]
    return {"net_bn": nets[0], "pct": 100 * sum(1 for n in nets if n <= nets[0]) / len(nets),
            "chg_4w_bn": nets[0] - nets[4]}


def from_live(manual):
    fk, ak = os.environ["FRED_API_KEY"].strip(), os.environ["AV_API_KEY"].strip()
    print("Fetching FRED series...")
    k = 10  # about two trading weeks
    ry, be, d2 = fred("DFII10", fk), fred("T10YIE", fk), fred("DGS2", fk)
    up, lo = fred("DFEDTARU", fk), fred("DFEDTARL", fk)
    vix, hy, oil, comp = fred("VIXCLS", fk), fred("BAMLH0A0HYM2", fk), fred("DCOILWTICO", fk), fred("NASDAQCOM", fk, 130)
    print("Fetching gold from Alpha Vantage (paced to stay under 5 calls a minute)...")
    gold = gold_history(ak)[:90]
    print("Fetching the six currency pairs for the dollar index (about 1 minute)...")
    dxy = dxy_series(ak)
    print("Fetching liquidity and CFTC positioning...")
    mode = manual.get("cot_mode", "contrarian")
    ok = {"source": "FRED", "status": "auto"}
    return {
        "as_of": max(ry[0][0], gold[0][0]),
        "gold": {"price": gold[0][1], "ref_high": max(p for _, p in gold), "ref_high_label": "90-day high"},
        "nasdaq": {"price": comp[0][1], "ref_high": max(p for _, p in comp), "ref_high_label": "recent high"},
        "real_yield": {"level": ry[0][1], "chg_2w_bp": (ry[0][1] - ry[k][1]) * 100},
        "fed": {"upper": up[0][1], "lower": lo[0][1],
                "last_move_bp": round((up[0][1] - up[min(60, len(up) - 1)][1]) * 100),
                "more_hikes_signalled": manual.get("more_hikes_signalled", False),
                "two_year": d2[0][1], "two_year_chg_2w_bp": (d2[0][1] - d2[k][1]) * 100},
        "dollar": {"label": "DXY (rebuilt)", "index": dxy[0][1], "chg_2w_pct": (dxy[0][1] / dxy[k][1] - 1) * 100},
        "dxy": {"price": dxy[0][1], "ref_high": max(p for _, p in dxy[:35]), "ref_high_label": "seven-week high",
                "chg_pct": (dxy[0][1] / dxy[5][1] - 1) * 100, "chg_label": "week"},
        "policy_rates": manual["policy_rates"], "policy_path": manual["policy_path"],
        "fiscal": manual["fiscal"], "growth": manual["growth"],
        "breakeven": {"level": be[0][1], "chg_2w_bp": (be[0][1] - be[k][1]) * 100},
        "risk": {"vix": vix[0][1], "vix_chg_2w": vix[0][1] - vix[k][1],
                 "hy": hy[0][1], "hy_chg_2w_bp": (hy[0][1] - hy[k][1]) * 100},
        "liquidity": net_liquidity(fk),
        "oil": {"price": oil[0][1], "chg_2w_pct": (oil[0][1] / oil[k][1] - 1) * 100},
        "geopolitics": manual["geopolitics"], "earnings_ai": manual["earnings_ai"], "breadth": manual["breadth"],
        "cot": cot_live("72hh-3qpy", "088691", "m_money_positions_long", "m_money_positions_short",
                        "prod_merc_positions_long", "prod_merc_positions_short",
                        "Managed money", "Producers and merchants", "COMEX gold futures", mode),
        "cot_nq": cot_live("gpe5-46if", "209742", "lev_money_positions_long", "lev_money_positions_short",
                           "asset_mgr_positions_long", "asset_mgr_positions_short",
                           "Leveraged funds", "Asset managers", "E-mini Nasdaq-100 futures", mode),
        "cot_dxy": cot_live("gpe5-46if", "098662", "lev_money_positions_long", "lev_money_positions_short",
                            "asset_mgr_positions_long", "asset_mgr_positions_short",
                            "Leveraged funds", "Asset managers", "ICE US Dollar Index futures", mode),
        "watch": manual.get("watch", []),
        "sources": {"gold": {"real_yield": {"source": "FRED DFII10", "status": "auto"},
                             "fed_path": {"source": "FRED DFEDTARU, DGS2; guidance from manual.json", "status": "auto"},
                             "dollar": {"source": "Alpha Vantage FX_DAILY, index rebuilt from six pairs", "status": "auto"},
                             "breakevens": {"source": "FRED T10YIE", "status": "auto"},
                             "geopolitics": {"source": "manual.json", "status": "manual"},
                             "price": {"source": "Alpha Vantage GOLD_SILVER_HISTORY", "status": "auto"},
                             "cot": {"source": "CFTC Disaggregated Futures Only, gold 088691", "status": "auto"}},
                    "nasdaq": {"real_yield": {"source": "FRED DFII10", "status": "auto"},
                               "fed_path": {"source": "FRED DFEDTARU, DGS2; guidance from manual.json", "status": "auto"},
                               "risk": {"source": "FRED VIXCLS, BAMLH0A0HYM2", "status": "auto"},
                               "liquidity": {"source": "FRED WALCL minus WTREGEN minus RRPONTSYD", "status": "auto"},
                               "geopolitics": {"source": "FRED DCOILWTICO; note from manual.json", "status": "auto"},
                               "earnings_ai": {"source": "manual.json", "status": "manual"},
                               "breadth": {"source": "manual.json", "status": "manual"},
                               "price": {"source": "FRED NASDAQCOM", "status": "auto"},
                               "cot": {"source": "CFTC Traders in Financial Futures, Nasdaq mini 209742", "status": "auto"}},
                    "dxy": {"rate_gap": {"source": "FRED DFEDTARU; ECB, BoJ and BoE rates from manual.json", "status": "manual"},
                            "policy_path": {"source": "FRED DGS2; edge from manual.json", "status": "manual"},
                            "real_yield": {"source": "FRED DFII10", "status": "auto"},
                            "risk": {"source": "FRED VIXCLS, BAMLH0A0HYM2", "status": "auto"},
                            "geopolitics": {"source": "FRED DCOILWTICO; note from manual.json", "status": "auto"},
                            "fiscal": {"source": "manual.json", "status": "manual"},
                            "growth": {"source": "manual.json", "status": "manual"},
                            "price": {"source": "Alpha Vantage FX_DAILY, index rebuilt from six pairs", "status": "auto"},
                            "cot": {"source": "CFTC Traders in Financial Futures, ICE dollar index 098662", "status": "auto"}}},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot")
    ap.add_argument("--manual", default="manual.json")
    ap.add_argument("--out", default="docs")
    a = ap.parse_args()
    inp = json.load(open(a.snapshot)) if a.snapshot else from_live(json.load(open(a.manual)))
    result = score(inp)
    os.makedirs(a.out, exist_ok=True)
    price = {n: result["assets"][n].pop("_price") for n in result["assets"]}
    json.dump(result, open(os.path.join(a.out, "bias.json"), "w"), indent=2, ensure_ascii=False)

    hp = os.path.join(a.out, "history.json")
    hist = json.load(open(hp)) if os.path.exists(hp) else []
    for n, r in result["assets"].items():
        hist = [h for h in hist if not (h["date"] == result["as_of"] and h.get("asset") == n)]
        hist.append({"date": result["as_of"], "asset": n, "bias": r["bias"]["score"], "net": r["bias"]["net"],
                     "price": price[n], "cot_index": (r["cot"] or {}).get("index"),
                     "macro_net": r["conflict"]["macro_net"], "trend": r["conflict"]["trend_score"],
                     "conflict": r["conflict"]["state"]})
    json.dump(sorted(hist, key=lambda h: (h["date"], h["asset"])), open(hp, "w"), indent=1)
    for n, r in result["assets"].items():
        print(r["headline"], "| bias", r["bias"]["score"], "|", r["bias"]["conviction"], "|", r["conflict"]["state"])
    print(result["compare_note"])


if __name__ == "__main__":
    main()
