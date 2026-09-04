#!/usr/bin/env python3
"""
fetch_fx_pools.py

Live dashboard for fxSAVE / fxUSD, covering exactly these sources:
    - f(x) Protocol native (fxSAVE vault / stability pool)
    - Morpho: the direct fxSAVE/USDC market
    - Morpho: the RockawayX-curated "f(x) Protocol Ecosystem USDC" vault
    - Pendle: fxSAVE markets (PT/YT)
    - Curve: fxUSD/fxSAVE pools
    - Convex: fxUSD/fxSAVE pools (the same Curve pools, boosted)

Data sources (both public, no API key needed):
    - DeFiLlama Yields API : https://yields.llama.fi/pools
      (covers native f(x), Pendle, Curve, Convex -- each reports into this feed)
    - Morpho GraphQL API   : https://api.morpho.org/graphql
      (covers the direct market + the RockawayX vault)

Usage:
    pip install -r requirements.txt
    python fetch_fx_pools.py

Output:
    index.html   -> the dashboard
    data.json    -> raw snapshot, for debugging
"""

import json
import sys
from datetime import datetime, timezone

import requests

DEFILLAMA_POOLS_URL = "https://yields.llama.fi/pools"
MORPHO_GRAPHQL_URL = "https://api.morpho.org/graphql"

TARGET_SYMBOLS = ["FXUSD", "FXSAVE"]
DEFILLAMA_PROJECTS = {
    "fx-protocol": "f(x) Protocol (nativo)",
    "pendle": "Pendle",
    "curve-dex": "Curve",
    "convex-finance": "Convex",
    "concentrator": "Concentrator",
    "aerodrome-slipstream": "Aerodrome",
}

MARKETS_QUERY = """
query FxMarkets($skip: Int!) {
  markets(first: 200, skip: $skip, where: { chainId_in: [1] }) {
    items {
      marketId
      loanAsset { symbol }
      collateralAsset { symbol }
      lltv
      state {
        supplyApy
        netSupplyApy
        supplyAssetsUsd
        liquidityAssetsUsd
        utilization
      }
    }
  }
}
"""

VAULTS_QUERY = """
query AllVaults($skip: Int!) {
  vaults(first: 200, skip: $skip, where: { chainId_in: [1] }) {
    items {
      address
      name
      asset { symbol }
      state {
        totalAssetsUsd
        netApy
      }
    }
  }
}
"""

VAULTS_V2_QUERY = """
query AllVaultsV2($skip: Int!) {
  vaultV2s(first: 200, skip: $skip, where: { chainId_in: [1] }) {
    items {
      address
      name
      asset { symbol }
      totalAssetsUsd
      avgNetApy
    }
  }
}
"""

MAX_PAGES = 6  # up to 1200 markets/vaults, in safe 200-item chunks

# Aerodrome's real Emission APR depends on the concentrated-liquidity range
# you pick in the app, so it can't be pulled automatically -- edit this by
# hand whenever you check the current number in the Aerodrome app.
AERODROME_EMISSIONS_NOTE = "> 6% emisiones"

# fxSAVE's real APY is tracked by DeFiLlama under the Concentrator project
# (this is the same underlying vault fx.aladdin.club shows), with a stable
# pool ID we can query directly -- much simpler and more reliable than
# reading on-chain data or scraping a JS-rendered page ourselves.
FXSAVE_APP_URL = "https://fx.aladdin.club/v2/fxsave"
FXSAVE_DEFILLAMA_POOL_ID = "ee0b7069-f8f3-4aa2-a415-728f13e6cc3d"


def collect_fxsave_apy() -> dict | None:
    """Looks up fxSAVE's real Supply APY (plus its 24h change) from
    DeFiLlama's yields dataset, by its known pool ID
    (project: Concentrator, symbol: fxSAVE)."""
    try:
        resp = requests.get(DEFILLAMA_POOLS_URL, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        for p in payload.get("data", []):
            if p.get("pool") == FXSAVE_DEFILLAMA_POOL_ID:
                apy = p.get("apy")
                if apy is None:
                    return None
                change = p.get("apyPct1D")
                return {
                    "apy": round(apy, 2),
                    "change_24h": round(change, 2) if change is not None else None,
                }
        print(f"[warn] fxSAVE APY: pool id {FXSAVE_DEFILLAMA_POOL_ID} not found in DeFiLlama data", file=sys.stderr)
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not fetch fxSAVE APY from DeFiLlama: {exc}", file=sys.stderr)
        return None


# The RockawayX-curated PT-fxSAVE Pendle market. DeFiLlama sometimes has
# gaps in its Pendle coverage for this pool, so we pull its APY straight
# from Pendle's own public API instead of depending on DeFiLlama for it.
PENDLE_ROCKAWAYX_MARKET_ADDRESS = "0x8308e53f584a7e5f0c581059d9ba971c0bec9454"
PENDLE_ROCKAWAYX_URL = f"https://app.pendle.finance/trade/markets/{PENDLE_ROCKAWAYX_MARKET_ADDRESS}/swap?view=pt&chain=ethereum"


def collect_pendle_fxsave_market() -> dict | None:
    """Fetches live market data directly from Pendle's own public backend
    API for the fxSAVE market, returning both the PT (fixed/implied) APY
    and the LP (liquidity provision) APY from a single call, plus TVL."""
    try:
        url = f"https://api-v2.pendle.finance/core/v2/1/markets/{PENDLE_ROCKAWAYX_MARKET_ADDRESS}/data"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        def find_apy(candidates: tuple[str, ...]) -> float | None:
            for key in candidates:
                val = data.get(key)
                if isinstance(val, (int, float)):
                    return val
            return None

        pt_apy_raw = find_apy(("impliedApy", "underlyingApy"))
        lp_apy_raw = find_apy(("lpApy", "aggregatedApy", "apy"))

        tvl = None
        liquidity = data.get("liquidity")
        if isinstance(liquidity, dict):
            tvl = liquidity.get("usd")
        elif isinstance(data.get("tvl"), (int, float)):
            tvl = data.get("tvl")

        if pt_apy_raw is None and lp_apy_raw is None:
            print(f"[warn] Pendle fxSAVE market: no known APY field in response, raw keys: {list(data.keys())}", file=sys.stderr)
            return None

        # Pendle's docs don't specify whether these fields are fractions
        # (0.0715) or already percentages (7.15) -- treat small values as
        # fractions needing *100, larger ones as already percentages.
        def to_pct(raw: float | None) -> float | None:
            if raw is None:
                return None
            return round(raw * 100 if abs(raw) < 1 else raw, 2)

        return {
            "pt_apy": to_pct(pt_apy_raw),
            "lp_apy": to_pct(lp_apy_raw),
            "tvl": round(tvl or 0, 2),
        }
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not fetch Pendle fxSAVE market data: {exc}", file=sys.stderr)
        return None

# Whale-watch feed: shows live transfers over $10k for fxUSD/fxSAVE.
# This runs client-side in the browser, so this key is publicly visible in
# the page source -- that's expected/fine for a free-tier Etherscan key,
# which is read-only and rate-limited.
ETHERSCAN_API_KEY = "FQF5W8F6ZYMPQUJRV1ZBF1ASZC7IF9EBRM"
FXUSD_CONTRACT = "0x085780639cC2cAcd35E474e71f4d000e2405D8f6"
FXSAVE_CONTRACT = "0x7743e50F534a7f9F1791DdE7dCD89F7783Eefc39"

STABLECOINS_URL = "https://stablecoins.llama.fi/stablecoins?includePrices=true"
COINGECKO_FXSAVE_URL = "https://api.coingecko.com/api/v3/coins/fx-usd-saving?localization=false&tickers=false&market_data=true&community_data=false&developer_data=false"


def collect_fxusd_mcap() -> dict | None:
    """fxUSD is tracked as a pegged stablecoin on DeFiLlama."""
    try:
        resp = requests.get(STABLECOINS_URL, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        for s in payload.get("peggedAssets", []):
            if (s.get("symbol") or "").upper() == "FXUSD":
                mcap = (s.get("circulating") or {}).get("peggedUSD")
                price = s.get("price")
                return {"mcap": round(mcap or 0, 2), "price": round(price, 4) if price else None}
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not fetch fxUSD market cap: {exc}", file=sys.stderr)
    return None


def collect_fxsave_mcap() -> dict | None:
    """fxSAVE is NOT indexed as a pegged asset on DeFiLlama's stablecoins
    endpoint (confirmed empty), so pull its market cap from CoinGecko."""
    try:
        resp = requests.get(COINGECKO_FXSAVE_URL, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        market_data = payload.get("market_data") or {}
        mcap = (market_data.get("market_cap") or {}).get("usd")
        price = (market_data.get("current_price") or {}).get("usd")
        return {"mcap": round(mcap or 0, 2), "price": round(price, 4) if price else None}
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not fetch fxSAVE market cap: {exc}", file=sys.stderr)
    return None


def touches_fx(text: str) -> bool:
    text = (text or "").upper()
    return any(sym in text for sym in TARGET_SYMBOLS)


# ---------------------------------------------------------------------------
# DeFiLlama: native f(x), Pendle, Curve, Convex pools for fxUSD/fxSAVE/FXN
# ---------------------------------------------------------------------------
def collect_defillama_pools() -> dict[str, list[dict]]:
    resp = requests.get(DEFILLAMA_POOLS_URL, timeout=60)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != "success":
        raise RuntimeError("DeFiLlama API did not return status=success")

    buckets: dict[str, list[dict]] = {label: [] for label in DEFILLAMA_PROJECTS.values()}
    for p in payload.get("data", []):
        project = p.get("project")
        if project not in DEFILLAMA_PROJECTS:
            continue
        if not touches_fx(p.get("symbol")):
            continue
        label = DEFILLAMA_PROJECTS[project]
        buckets[label].append(
            {
                "symbol": p.get("symbol"),
                "chain": p.get("chain"),
                "tvl_usd": round(p.get("tvlUsd") or 0, 2),
                "apy_pct": round(p.get("apy") or 0, 2),
                "apy_base_pct": round(p.get("apyBase") or 0, 2) if p.get("apyBase") is not None else None,
                "apy_reward_pct": round(p.get("apyReward") or 0, 2) if p.get("apyReward") else None,
                "apy_change_24h": round(p.get("apyPct1D"), 2) if p.get("apyPct1D") is not None else None,
                "url": f"https://defillama.com/yields/pool/{p.get('pool')}",
            }
        )
    for label in buckets:
        buckets[label].sort(key=lambda x: x["apy_pct"], reverse=True)
    return buckets


# ---------------------------------------------------------------------------
# Morpho: the direct fxSAVE/USDC market + the RockawayX vault
# ---------------------------------------------------------------------------
def graphql(query: str, variables: dict | None = None) -> dict:
    resp = requests.post(
        MORPHO_GRAPHQL_URL,
        json={"query": query, "variables": variables or {}},
        timeout=30,
    )
    if not resp.ok:
        raise RuntimeError(f"Morpho API HTTP {resp.status_code}: {resp.text[:500]}")
    payload = resp.json()
    if "errors" in payload:
        raise RuntimeError(f"Morpho API returned errors: {payload['errors']}")
    return payload["data"]


def collect_direct_market() -> dict | None:
    """Page through markets (200 at a time) looking for the fxSAVE/USDC one.

    We deliberately don't hardcode a market's marketId: those can change,
    and a stale ID would silently return nothing. We also don't rely on a
    single first:N request -- with no explicit ordering, the target market
    could simply fall outside whatever window the API returns first.
    """
    for page in range(MAX_PAGES):
        data = graphql(MARKETS_QUERY, {"skip": page * 200})
        items = data["markets"]["items"]
        if not items:
            break
        for m in items:
            loan_sym = (m.get("loanAsset") or {}).get("symbol") or ""
            coll_sym = (m.get("collateralAsset") or {}).get("symbol") or ""
            if coll_sym.upper() == "FXSAVE" and loan_sym.upper() == "USDC":
                state = m.get("state") or {}
                return {
                    "id": m["marketId"],
                    "loan": loan_sym,
                    "collateral": coll_sym,
                    "lltv_pct": round(float(m.get("lltv") or 0) / 1e16, 2),
                    "supply_apy_pct": round((state.get("netSupplyApy") or state.get("supplyApy") or 0) * 100, 2),
                    "supply_usd": round(state.get("supplyAssetsUsd") or 0, 2),
                    "liquidity_usd": round(state.get("liquidityAssetsUsd") or 0, 2),
                    "utilization_pct": round((state.get("utilization") or 0) * 100, 2),
                    "url": f"https://app.morpho.org/ethereum/market/{m['marketId']}",
                }
    return None


def collect_rockawayx_vault() -> dict | None:
    """Page through vaults (200 at a time), matching on curator name -- the
    vault name won't necessarily contain the literal strings
    FXUSD/FXSAVE/FXN, so don't filter on that. Checks both Vault V1
    ("vaults") and Vault V2 ("vaultV2s"), since newer curator vaults may
    only exist under V2.
    """
    for page in range(MAX_PAGES):
        data = graphql(VAULTS_QUERY, {"skip": page * 200})
        items = data["vaults"]["items"]
        if not items:
            break
        for v in items:
            name = v.get("name") or ""
            if "rockawayx" in name.lower():
                state = v.get("state") or {}
                return {
                    "address": v["address"],
                    "name": name,
                    "asset": (v.get("asset") or {}).get("symbol"),
                    "total_assets_usd": round(state.get("totalAssetsUsd") or 0, 2),
                    "net_apy_pct": round((state.get("netApy") or 0) * 100, 2),
                    "url": f"https://app.morpho.org/ethereum/vault/{v['address']}",
                }

    for page in range(MAX_PAGES):
        data = graphql(VAULTS_V2_QUERY, {"skip": page * 200})
        items = data["vaultV2s"]["items"]
        if not items:
            break
        for v in items:
            name = v.get("name") or ""
            if "rockawayx" in name.lower():
                return {
                    "address": v["address"],
                    "name": name,
                    "asset": (v.get("asset") or {}).get("symbol"),
                    "total_assets_usd": round(v.get("totalAssetsUsd") or 0, 2),
                    "net_apy_pct": round((v.get("avgNetApy") or 0) * 100, 2),
                    "url": f"https://app.morpho.org/ethereum/vault/{v['address']}",
                }
    return None


CARD_CSS = """
:root {
  color-scheme: dark;
  --bg: #0d1f15;
  --card: #10151d;
  --card-border: #1e2733;
  --text: #f2f3f5;
  --muted: #8a94a3;
  --accent: #0b3d91;
  --accent-text: #7fb4ec;
  --highlight: #ef4444;
  --green: #4ade80;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 2.5rem 1.25rem 4rem;
  background: url("background.jpg") repeat;
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
.wrap { max-width: 900px; margin: 0 auto; }
h1 {
  font-size: 2.3rem; font-weight: 900; margin-bottom: 0.25rem;
  display: flex; align-items: center; gap: 10px; color: #0b3d91;
  background: #fff; padding: 10px 18px; border-radius: 12px;
  width: fit-content;
}
.live-dot {
  width: 12px; height: 12px; border-radius: 50%;
  background: var(--green);
  box-shadow: 0 0 0 0 rgba(74, 222, 128, 0.7);
  animation: live-pulse 1.6s infinite;
  flex-shrink: 0;
}
@keyframes live-pulse {
  0% { box-shadow: 0 0 0 0 rgba(74, 222, 128, 0.7); }
  70% { box-shadow: 0 0 0 10px rgba(74, 222, 128, 0); }
  100% { box-shadow: 0 0 0 0 rgba(74, 222, 128, 0); }
}
.subtitle {
  color: #0b3d91; font-weight: 700; font-size: 1.1rem; margin-bottom: 2rem;
  background: #fff; padding: 8px 14px; border-radius: 10px;
  display: inline-block;
}
.section-title {
  font-size: 1.7rem; font-weight: 900; margin: 2.5rem 0 0.85rem;
  color: #0b3d91; text-transform: uppercase; letter-spacing: 0.03em;
  background: #fff; padding: 8px 14px; border-radius: 10px;
  display: inline-block;
}
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }
.card {
  background: var(--card);
  border: 1px solid var(--card-border);
  border-radius: 14px;
  padding: 1.1rem 1.25rem;
  text-decoration: none;
  color: inherit;
  display: block;
  transition: border-color 0.15s ease, transform 0.15s ease;
}
.card:hover { border-color: var(--highlight); transform: translateY(-2px); }
.card-title { font-weight: 600; font-size: 0.95rem; margin: 0 0 2px; }
.card-sub { color: var(--muted); font-size: 0.8rem; margin: 0 0 10px; }
.stat-row { display: flex; gap: 10px; margin-top: 6px; }
.stat { flex: 1; background: #161d28; border-radius: 10px; padding: 8px 10px; }
.stat-label { font-size: 0.7rem; color: var(--muted); margin: 0; }
.stat-value { font-size: 1.15rem; font-weight: 600; margin: 2px 0 0; }
.apy { color: var(--green); }
.pill {
  display: inline-block; font-size: 0.7rem; background: #1a2536; color: var(--accent-text);
  padding: 2px 8px; border-radius: 6px; margin-top: 8px;
}
.apy-change {
  display: inline-block; font-size: 0.7rem; font-weight: 700;
  padding: 2px 7px; border-radius: 6px; margin-left: 4px; vertical-align: middle;
}
.apy-up { background: rgba(74, 222, 128, 0.15); color: #4ade80; }
.apy-down { background: rgba(239, 68, 68, 0.15); color: #ef4444; }
.apy-flat { background: rgba(154, 154, 162, 0.15); color: var(--muted); }
.empty { color: var(--muted); font-size: 0.9rem; padding: 1rem 0; }
footer { color: #000; font-size: 0.75rem; margin-top: 3rem; text-align: center; }
footer a { color: #000; }
.mcap-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-bottom: 2rem; }
.mcap-card {
  background: var(--card);
  border: 1px solid var(--card-border);
  border-radius: 14px;
  padding: 1.1rem 1.25rem;
}
.mcap-label { font-size: 0.75rem; color: #ffffff; text-transform: uppercase; letter-spacing: 0.05em; margin: 0 0 6px; font-weight: 600; }
.mcap-value { font-size: 1.9rem; font-weight: 700; margin: 0; color: #39ff14; font-variant-numeric: tabular-nums; display: flex; align-items: center; gap: 8px; }
.mcap-price {
  font-size: 0.85rem; color: #ffffff; margin: 4px 0 0; font-variant-numeric: tabular-nums;
  animation: price-blink 1.4s infinite;
}
@keyframes price-blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}
.whale-box {
  background: var(--card);
  border: 1px solid var(--card-border);
  border-radius: 14px;
  padding: 1rem 1.1rem;
  margin-bottom: 2rem;
  height: 220px;
  overflow: hidden;
  position: relative;
}
.whale-title {
  font-size: 0.8rem; color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.05em; margin: 0 0 8px; font-weight: 700;
}
.whale-list { display: flex; flex-direction: column; gap: 8px; }
.whale-item {
  display: flex; justify-content: space-between; align-items: center;
  background: #161d28; border-radius: 8px; padding: 8px 10px;
  font-size: 0.85rem; animation: whale-in 0.5s ease;
  text-decoration: none; color: inherit;
}
.whale-item .whale-symbol { font-weight: 700; color: var(--accent-text); }
.whale-item .whale-amount { color: #39ff14; font-weight: 700; }
@keyframes whale-in {
  from { opacity: 0; transform: translateY(-14px); }
  to { opacity: 1; transform: translateY(0); }
}
"""


def render_apy_change_badge(change: float | None) -> str:
    if change is None:
        return ""
    if change > 0:
        return f'<span class="apy-change apy-up">+{change}% (24h)</span>'
    if change < 0:
        return f'<span class="apy-change apy-down">{change}% (24h)</span>'
    return '<span class="apy-change apy-flat">0% (24h)</span>'


def render_aerodrome_card(p: dict) -> str:
    change_badge = render_apy_change_badge(p.get("apy_change_24h"))
    return f"""
    <a class="card" href="https://aerodrome.finance/liquidity?query=Fxusd" target="_blank" rel="noopener">
      <p class="card-title">{p['symbol']}</p>
      <p class="card-sub">{p['chain']}</p>
      <div class="stat-row">
        <div class="stat">
          <p class="stat-label">APY total</p>
          <p class="stat-value apy">{p['apy_pct']}% <span class="pill">{AERODROME_EMISSIONS_NOTE}</span> {change_badge}</p>
        </div>
        <div class="stat">
          <p class="stat-label">TVL</p>
          <p class="stat-value">${p['tvl_usd']:,.0f}</p>
        </div>
      </div>
    </a>
    """


def render_defillama_card(p: dict) -> str:
    reward_note = ""
    if p["apy_reward_pct"]:
        reward_note = f' <span class="pill">incl. {p["apy_reward_pct"]}% rewards</span>'
    change_badge = render_apy_change_badge(p.get("apy_change_24h"))
    return f"""
    <a class="card" href="{p['url']}" target="_blank" rel="noopener">
      <p class="card-title">{p['symbol']}</p>
      <p class="card-sub">{p['chain']}</p>
      <div class="stat-row">
        <div class="stat">
          <p class="stat-label">APY</p>
          <p class="stat-value apy">{p['apy_pct']}% {change_badge}</p>
        </div>
        <div class="stat">
          <p class="stat-label">TVL</p>
          <p class="stat-value">${p['tvl_usd']:,.0f}</p>
        </div>
      </div>
      {reward_note}
    </a>
    """


def render_market_card(m: dict) -> str:
    return f"""
    <a class="card" href="{m['url']}" target="_blank" rel="noopener">
      <p class="card-title">{m['collateral']} / {m['loan']}</p>
      <p class="card-sub">Morpho &middot; mercado directo &middot; LLTV {m['lltv_pct']}%</p>
      <div class="stat-row">
        <div class="stat">
          <p class="stat-label">Supply APY</p>
          <p class="stat-value apy">{m['supply_apy_pct']}%</p>
        </div>
        <div class="stat">
          <p class="stat-label">TVL suministrado</p>
          <p class="stat-value">${m['supply_usd']:,.0f}</p>
        </div>
      </div>
      <div class="stat-row">
        <div class="stat">
          <p class="stat-label">Utilizacion</p>
          <p class="stat-value">{m['utilization_pct']}%</p>
        </div>
        <div class="stat">
          <p class="stat-label">Liquidez libre</p>
          <p class="stat-value">${m['liquidity_usd']:,.0f}</p>
        </div>
      </div>
    </a>
    """


def render_vault_card(v: dict) -> str:
    return f"""
    <a class="card" href="{v['url']}" target="_blank" rel="noopener">
      <p class="card-title">{v['name']}</p>
      <p class="card-sub">Morpho &middot; vault curado por RockawayX &middot; activo {v['asset']}</p>
      <div class="stat-row">
        <div class="stat">
          <p class="stat-label">Net APY</p>
          <p class="stat-value apy">{v['net_apy_pct']}%</p>
        </div>
        <div class="stat">
          <p class="stat-label">Depositos totales</p>
          <p class="stat-value">${v['total_assets_usd']:,.0f}</p>
        </div>
      </div>
    </a>
    """


def render_section(title: str, html: str) -> str:
    return f"""
    <p class="section-title">{title}</p>
    <div class="grid">
      {html}
    </div>
    """


def render_html(
    market: dict | None,
    vault: dict | None,
    defillama: dict[str, list[dict]],
    fxusd_mcap: dict | None,
    fxsave_mcap: dict | None,
    fxsave_apy_data: dict | None = None,
    pendle_rockawayx: dict | None = None,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    fxusd_mcap_val = (fxusd_mcap or {}).get("mcap") or 0
    fxusd_price_val = (fxusd_mcap or {}).get("price") or 0
    fxsave_mcap_val = (fxsave_mcap or {}).get("mcap") or 0
    fxsave_price_val = (fxsave_mcap or {}).get("price") or 0
    fxsave_apy = (fxsave_apy_data or {}).get("apy")
    fxsave_apy_change = (fxsave_apy_data or {}).get("change_24h")

    market_html = render_market_card(market) if market else '<p class="empty">Mercado no disponible en este momento.</p>'
    vault_html = render_vault_card(vault) if vault else '<p class="empty">No se encontro el vault de RockawayX en este momento.</p>'

    apy_display = f"{fxsave_apy}%" if fxsave_apy is not None else "no disponible"
    fxsave_change_badge = render_apy_change_badge(fxsave_apy_change)
    fxsave_apy_card = f"""
    <a class="card" href="{FXSAVE_APP_URL}" target="_blank" rel="noopener">
      <p class="card-title">FXSAVE</p>
      <p class="card-sub">f(x) Protocol app &middot; APY calculado on-chain (24h)</p>
      <div class="stat-row">
        <div class="stat">
          <p class="stat-label">APY</p>
          <p class="stat-value apy">{apy_display} {fxsave_change_badge}</p>
        </div>
        <div class="stat">
          <p class="stat-label">TVL</p>
          <p class="stat-value">${fxsave_mcap_val:,.0f}</p>
        </div>
      </div>
    </a>
    """

    fx_native_pools = [
        {**p, "url": "https://fx.aladdin.club/v2/earn"}
        for p in defillama.get("f(x) Protocol (nativo)", [])
    ]

    sections = [
        render_section("f(x) Protocol", fxsave_apy_card + "".join(
            render_defillama_card(p) for p in fx_native_pools
        ) or '<p class="empty">Sin pools activos en este momento.</p>'),
        render_section("Morpho -- vault RockawayX", vault_html),
    ]
    # Manual link overrides for the Pendle section: the first two cards
    # Pendle section is fully independent of DeFiLlama now -- just these
    # two fixed cards (PT and PLP for the fxSAVE market), both sourced
    # directly from Pendle's own API.
    PENDLE_ZAP_URL = "https://app.pendle.finance/trade/pools/0x8308e53f584a7e5f0c581059d9ba971c0bec9454/zap/in?chain=ethereum"

    if pendle_rockawayx is not None:
        pt_apy = pendle_rockawayx.get("pt_apy")
        lp_apy = pendle_rockawayx.get("lp_apy")
        tvl = pendle_rockawayx.get("tvl") or 0
        pendle_html = f"""
    <a class="card" href="{PENDLE_ROCKAWAYX_URL}" target="_blank" rel="noopener">
      <p class="card-title">PT-fxSAVE</p>
      <p class="card-sub">f(x) USD Saving &middot; Pendle V2</p>
      <div class="stat-row">
        <div class="stat">
          <p class="stat-label">APY</p>
          <p class="stat-value apy">{pt_apy if pt_apy is not None else 'no disponible'}{'%' if pt_apy is not None else ''}</p>
        </div>
        <div class="stat">
          <p class="stat-label">TVL</p>
          <p class="stat-value">${tvl:,.0f}</p>
        </div>
      </div>
    </a>
    <a class="card" href="{PENDLE_ZAP_URL}" target="_blank" rel="noopener">
      <p class="card-title">PLP-fxSAVE</p>
      <p class="card-sub">f(x) USD Saving &middot; Pendle V2</p>
      <div class="stat-row">
        <div class="stat">
          <p class="stat-label">APY</p>
          <p class="stat-value apy">{lp_apy if lp_apy is not None else 'no disponible'}{'%' if lp_apy is not None else ''}</p>
        </div>
        <div class="stat">
          <p class="stat-label">TVL</p>
          <p class="stat-value">${tvl:,.0f}</p>
        </div>
      </div>
    </a>
    """
    else:
        pendle_html = '<p class="empty">No se pudo obtener el dato de Pendle en este momento.</p>'

    sections.append(render_section("Pendle", pendle_html))

    CURVE_POOLS_URL = "https://www.curve.finance/#/ethereum/pools"
    CONVEX_STAKE_URL = "https://curve.convexfinance.com/stake"
    CONCENTRATOR_VAULT_URL = "https://concentrator.aladdin.club/#/vault"

    for label in ("Curve", "Convex", "Concentrator", "Aerodrome"):
        pools = defillama.get(label, [])
        if label == "Curve":
            pools = [{**p, "url": CURVE_POOLS_URL} for p in pools]
        if label == "Convex":
            pools = [{**p, "url": CONVEX_STAKE_URL} for p in pools]
        if label == "Concentrator":
            pools = [{**p, "url": CONCENTRATOR_VAULT_URL} for p in pools]
        card_fn = render_aerodrome_card if label == "Aerodrome" else render_defillama_card
        pool_html = "".join(card_fn(p) for p in pools)
        html = pool_html if pool_html else '<p class="empty">Sin pools activos en este momento.</p>'
        sections.append(render_section(label, html))

    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>DASHBOARD FXUSD &amp; FXSAVE POOLS LIVE</title>
  <style>{CARD_CSS}</style>
</head>
<body>
  <div class="wrap">
    <div class="mcap-row">
      <div class="mcap-card">
        <p class="mcap-label">fxUSD Market Cap</p>
        <p class="mcap-value"><span class="live-dot"></span>$<span id="fxusd-mcap">{fxusd_mcap_val:,.0f}</span></p>
        <p class="mcap-price">Precio: $<span id="fxusd-price">{fxusd_price_val:.4f}</span></p>
      </div>
      <div class="mcap-card">
        <p class="mcap-label">fxSAVE Market Cap</p>
        <p class="mcap-value"><span class="live-dot"></span>$<span id="fxsave-mcap">{fxsave_mcap_val:,.0f}</span></p>
        <p class="mcap-price">Precio: $<span id="fxsave-price">{fxsave_price_val:.4f}</span></p>
      </div>
    </div>

    <div class="whale-box">
      <p class="whale-title">Posiciones &gt; $10K en vivo</p>
      <div class="whale-list" id="whale-list"></div>
    </div>

    <h1><span class="live-dot"></span>DASHBOARD FXUSD &amp; FXSAVE POOLS LIVE</h1>
    <p class="subtitle">f(x) Protocol nativo, Morpho (mercado directo + vault RockawayX), Pendle, Curve y Convex. Ultima actualizacion: {now}</p>

    {''.join(sections)}

    <footer>
      <img src="logo.png" alt="METAFXN" style="width: 140px; height: 140px; border-radius: 50%; display: block; margin: 0 auto;" />
    </footer>
  </div>

  <img id="mascot" src="mascot.png" alt="" style="position: fixed; top: 0; left: 0; width: 130px; z-index: 999; pointer-events: none;" />

  <script>
    (function() {{
      function fmt(n) {{
        return Math.round(n).toLocaleString('en-US');
      }}

      function setValue(id, value) {{
        var el = document.getElementById(id);
        if (el && typeof value === 'number' && !isNaN(value)) {{
          el.textContent = fmt(value);
        }}
      }}

      function setPrice(id, value) {{
        var el = document.getElementById(id);
        if (el && typeof value === 'number' && !isNaN(value)) {{
          el.textContent = value.toFixed(4);
        }}
      }}

      function refreshAll() {{
        fetch('https://stablecoins.llama.fi/stablecoins?includePrices=true')
          .then(function(r) {{ return r.json(); }})
          .then(function(data) {{
            var list = data.peggedAssets || [];
            for (var i = 0; i < list.length; i++) {{
              if ((list[i].symbol || '').toUpperCase() === 'FXUSD') {{
                setValue('fxusd-mcap', (list[i].circulating || {{}}).peggedUSD);
                setPrice('fxusd-price', list[i].price);
                if (typeof whalePrices !== 'undefined' && list[i].price) {{
                  whalePrices.FXUSD = list[i].price;
                }}
                return;
              }}
            }}
          }})
          .catch(function() {{ /* keep last known value on failure */ }});

        fetch('https://api.coingecko.com/api/v3/coins/fx-usd-saving?localization=false&tickers=false&market_data=true&community_data=false&developer_data=false')
          .then(function(r) {{ return r.json(); }})
          .then(function(data) {{
            var md = data.market_data || {{}};
            setValue('fxsave-mcap', (md.market_cap || {{}}).usd);
            var price = (md.current_price || {{}}).usd;
            setPrice('fxsave-price', price);
            if (typeof whalePrices !== 'undefined' && price) {{
              whalePrices.FXSAVE = price;
            }}
          }})
          .catch(function() {{ /* keep last known value on failure */ }});
      }}

      refreshAll();
      setInterval(refreshAll, 20000);

      // --- Whale watch: live transfers over $10k for fxUSD / fxSAVE ---
      var whalePrices = {{ FXUSD: {fxusd_price_val}, FXSAVE: {fxsave_price_val} }};
      var whaleSeen = {{}};
      var whaleLastShown = {{}}; // symbol -> amount/time pair, for fuzzy dedup
      var WHALE_MAX_ITEMS = 8;
      var WHALE_MIN_USD = 10000;
      var ETHERSCAN_KEY = "{ETHERSCAN_API_KEY}";

      function whaleUrl(contract) {{
        return 'https://api.etherscan.io/v2/api?chainid=1&module=account&action=tokentx'
          + '&contractaddress=' + contract
          + '&page=1&offset=25&sort=desc&apikey=' + ETHERSCAN_KEY;
      }}

      function addWhaleItem(symbol, usdValue, txHash) {{
        var list = document.getElementById('whale-list');
        if (!list) return;
        var item = document.createElement('a');
        item.className = 'whale-item';
        item.href = 'https://etherscan.io/tx/' + txHash;
        item.target = '_blank';
        item.rel = 'noopener';
        item.innerHTML =
          '<span class="whale-symbol">' + symbol + '</span>' +
          '<span class="whale-amount">$' + Math.round(usdValue).toLocaleString('en-US') + '</span>';
        list.insertBefore(item, list.firstChild);
        while (list.children.length > WHALE_MAX_ITEMS) {{
          list.removeChild(list.lastChild);
        }}
      }}

      function checkWhales(contract, symbol) {{
        fetch(whaleUrl(contract))
          .then(function(r) {{ return r.json(); }})
          .then(function(data) {{
            var results = data.result;
            if (!Array.isArray(results)) return;
            var price = whalePrices[symbol] || 0;
            if (!price) return;
            // Process oldest-to-newest so the feed reads top-to-bottom chronologically
            for (var i = results.length - 1; i >= 0; i--) {{
              var t = results[i];
              if (whaleSeen[t.hash]) continue;
              whaleSeen[t.hash] = true;
              var decimals = parseInt(t.tokenDecimal, 10) || 18;
              var amount = parseFloat(t.value) / Math.pow(10, decimals);
              var usdValue = amount * price;
              if (usdValue >= WHALE_MIN_USD) {{
                var last = whaleLastShown[symbol];
                var now = Date.now();
                var isDuplicateLeg = last
                  && (now - last.time) < 90000
                  && Math.abs(usdValue - last.amount) / last.amount < 0.02;
                if (!isDuplicateLeg) {{
                  addWhaleItem(symbol, usdValue, t.hash);
                  whaleLastShown[symbol] = {{ amount: usdValue, time: now }};
                }}
              }}
            }}
          }})
          .catch(function() {{ /* ignore failures, try again next cycle */ }});
      }}

      function checkAllWhales() {{
        checkWhales('{FXUSD_CONTRACT}', 'FXUSD');
        checkWhales('{FXSAVE_CONTRACT}', 'FXSAVE');
      }}

      checkAllWhales();
      setInterval(checkAllWhales, 25000);

      // --- Mascot: bounces around the screen like the old DVD logo ---
      var mascot = document.getElementById('mascot');
      if (mascot) {{
        var x = Math.random() * (window.innerWidth - 130);
        var y = Math.random() * (window.innerHeight - 130);
        var vx = 2.2, vy = 2.2;

        function stepMascot() {{
          var w = mascot.offsetWidth || 130;
          var h = mascot.offsetHeight || 73;
          x += vx;
          y += vy;
          if (x <= 0 || x + w >= window.innerWidth) {{ vx = -vx; x = Math.max(0, Math.min(x, window.innerWidth - w)); }}
          if (y <= 0 || y + h >= window.innerHeight) {{ vy = -vy; y = Math.max(0, Math.min(y, window.innerHeight - h)); }}
          mascot.style.transform = 'translate(' + x + 'px,' + y + 'px)';
          requestAnimationFrame(stepMascot);
        }}
        requestAnimationFrame(stepMascot);
      }}
    }})();
  </script>
</body>
</html>
"""


def main() -> None:
    try:
        market = collect_direct_market()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not fetch Morpho market: {exc}", file=sys.stderr)
        market = None

    try:
        vault = collect_rockawayx_vault()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not fetch RockawayX vault: {exc}", file=sys.stderr)
        vault = None

    try:
        defillama = collect_defillama_pools()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not fetch DeFiLlama pools: {exc}", file=sys.stderr)
        defillama = {label: [] for label in DEFILLAMA_PROJECTS.values()}

    fxusd_mcap = collect_fxusd_mcap()
    fxsave_mcap = collect_fxsave_mcap()
    fxsave_apy = collect_fxsave_apy()
    pendle_rockawayx = collect_pendle_fxsave_market()

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "morpho_direct_market": market,
                "morpho_rockawayx_vault": vault,
                "defillama": defillama,
                "fxusd_mcap_usd": fxusd_mcap,
                "fxsave_mcap_usd": fxsave_mcap,
                "fxsave_apy_pct": fxsave_apy,
                "pendle_rockawayx": pendle_rockawayx,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )

    html = render_html(market, vault, defillama, fxusd_mcap, fxsave_mcap, fxsave_apy, pendle_rockawayx)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    counts = {k: len(v) for k, v in defillama.items()}
    print(
        f"Listo: market={'ok' if market else 'missing'}, vault={'ok' if vault else 'missing'}, "
        f"fxusd_mcap={'ok' if fxusd_mcap else 'missing'}, fxsave_mcap={'ok' if fxsave_mcap else 'missing'}, "
        f"fxsave_apy={(fxsave_apy or {}).get('apy', 'missing')}, "
        f"pendle_pt_apy={(pendle_rockawayx or {}).get('pt_apy', 'missing')}, "
        f"pendle_lp_apy={(pendle_rockawayx or {}).get('lp_apy', 'missing')}, "
        f"defillama={counts}"
    )


if __name__ == "__main__":
    main()
