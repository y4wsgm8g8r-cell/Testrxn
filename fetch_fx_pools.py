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
        borrowApy
        netBorrowApy
        borrowAssetsUsd
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
        apy
        netApy
        avgNetApy
        weeklyNetApy
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
      netApy
      avgNetApy
      rewards {
        supplyApr
        asset { symbol }
      }
    }
  }
}
"""

VAULT_V2_BY_ADDRESS_QUERY = """
query RockawayXVault($address: String!, $chainId: Int!) {
  vaultV2ByAddress(address: $address, chainId: $chainId) {
    address
    name
    asset { symbol }
    totalAssetsUsd
    apy
    netApy
    avgNetApy
    avgNetApyExcludingRewards
    performanceFee
    managementFee
    rewards {
      supplyApr
      asset { symbol }
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


HYDREX_STRATEGIES_URL = "https://api.hydrex.fi/strategies"
HYDREX_POOLS_URL = "https://www.hydrex.fi/pools?search=Fxusd"


def collect_hydrex_pools() -> list[dict]:
    """Fetches fxUSD pools directly from Hydrex's own public API (Base
    chain). DeFiLlama doesn't track this DEX's fxUSD pools at all, so we
    go straight to the source instead of guessing project slugs."""
    try:
        resp = requests.get(HYDREX_STRATEGIES_URL, timeout=30)
        resp.raise_for_status()
        strategies = resp.json()
        results = []
        for s in strategies:
            title = s.get("title") or ""
            if "FXUSD" not in title.upper():
                continue
            gauge = s.get("gauge") or {}
            apr = gauge.get("dayFarmingApr")
            tvl = s.get("tvlUsd") if s.get("tvlUsd") is not None else gauge.get("tvl")
            if not tvl or tvl < 1:
                continue
            results.append(
                {
                    "symbol": title,
                    "chain": "Base",
                    "tvl_usd": round(tvl or 0, 2),
                    "apy_pct": round(apr or 0, 2),
                    "apy_reward_pct": None,
                    "apy_change_24h": None,
                    "url": HYDREX_POOLS_URL,
                }
            )
        return results
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not fetch Hydrex pools: {exc}", file=sys.stderr)
        return []


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
                return {
                    "mcap": round(mcap or 0, 2),
                    "price": round(price, 4) if price else None,
                    "id": s.get("id"),
                }
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not fetch fxUSD market cap: {exc}", file=sys.stderr)
    return None


FXUSD_STABLECOIN_ID = 168
FXUSD_MCAP_CHART_URL = "https://stablecoins.llama.fi/stablecoincharts/all?stablecoin=168"

COMPARE_STABLES = [
    {"key": "fxUSD", "label": "fxUSD", "id": 168, "symbol": "FXUSD", "color": "#39ff14"},
    {"key": "GHO", "label": "GHO", "id": 118, "symbol": "GHO", "color": "#7aedcf"},
    {"key": "crvUSD", "label": "crvUSD", "id": 110, "symbol": "CRVUSD", "color": "#4da3ff"},
    {"key": "FRAX", "label": "FRAX", "id": 6, "symbol": "FRAX", "color": "#c77dff"},
    {"key": "DOLA", "label": "DOLA", "id": 15, "symbol": "DOLA", "color": "#ff8c42"},
    {"key": "frxUSD", "label": "frxUSD", "id": 235, "symbol": "FRXUSD", "color": "#ff4d8d"},
    {"key": "REUSD", "label": "REUSD (Resupply)", "id": 256, "symbol": "REUSD", "color": "#ffd166"},
    {"key": "ZCHF", "label": "Frankencoin", "id": 226, "symbol": "ZCHF", "color": "#e8e8e8"},
]


def collect_fxusd_mcap_history() -> list[dict] | None:
    """Daily fxUSD circulating market cap from DeFiLlama (same series as
    the Market Cap chart on defillama.com/stablecoin/fxusd)."""
    try:
        resp = requests.get(FXUSD_MCAP_CHART_URL, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        history = []
        for row in payload or []:
            ts = row.get("date")
            usd = ((row.get("totalCirculatingUSD") or {}).get("peggedUSD")
                   or (row.get("totalCirculating") or {}).get("peggedUSD"))
            try:
                ts = int(ts)
            except (TypeError, ValueError):
                continue
            if not isinstance(usd, (int, float)):
                continue
            history.append({"timestamp": ts, "mcap": float(usd)})
        if len(history) < 2:
            print("[warn] fxUSD mcap history: not enough points", file=sys.stderr)
            return None
        return history
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not fetch fxUSD mcap history: {exc}", file=sys.stderr)
        return None


def collect_compare_histories() -> dict[str, list[dict]]:
    """Daily circulating USD on Ethereum for the overlay comparison chart."""
    out: dict[str, list[dict]] = {}
    for spec in COMPARE_STABLES:
        url = f"https://stablecoins.llama.fi/stablecoincharts/ethereum?stablecoin={spec['id']}"
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
            history = []
            for row in payload or []:
                try:
                    ts = int(row.get("date"))
                except (TypeError, ValueError):
                    continue
                usd = row.get("totalCirculatingUSD") or {}
                val = None
                for k in ("peggedUSD", "peggedCHF", "peggedEUR", "peggedVAR"):
                    if isinstance(usd.get(k), (int, float)):
                        val = float(usd[k])
                        break
                if val is None:
                    circ = row.get("totalCirculating") or {}
                    for k, v in circ.items():
                        if isinstance(v, (int, float)):
                            val = float(v)
                            break
                if val is None:
                    continue
                history.append({"timestamp": ts, "mcap": val})
            if len(history) >= 2:
                out[spec["key"]] = history
            else:
                print(f"[warn] compare chart {spec['key']}: not enough points", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] could not fetch {spec['key']} history: {exc}", file=sys.stderr)
    return out


def collect_fxusd_peg_history(days: int = 60) -> list[dict] | None:
    """Fetches fxUSD's daily price history (with dates) via DeFiLlama's
    coins API, using the token's contract address. Returns a list of
    {"timestamp": int, "price": float} dicts."""
    try:
        import time as _time
        span_seconds = days * 86400
        start = int(_time.time()) - span_seconds
        url = (
            f"https://coins.llama.fi/chart/ethereum:{FXUSD_CONTRACT}"
            f"?start={start}&span={days}&period=1d"
        )
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        coin_data = (payload.get("coins") or {}).get(f"ethereum:{FXUSD_CONTRACT}")
        if not coin_data:
            print(f"[warn] fxUSD peg history: no data for token address in response, keys: {list(payload.get('coins', {}).keys())}", file=sys.stderr)
            return None
        points = coin_data.get("prices") or []
        history = [
            {"timestamp": p["timestamp"], "price": p["price"]}
            for p in points
            if isinstance(p.get("price"), (int, float)) and isinstance(p.get("timestamp"), (int, float))
        ]
        if not history:
            print(f"[warn] fxUSD peg history: no price points, sample: {points[0] if points else 'N/A'}", file=sys.stderr)
            return None
        return history[-days:]
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not fetch fxUSD peg history: {exc}", file=sys.stderr)
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


def _pack_market(m: dict, url: str | None = None) -> dict:
    loan_sym = (m.get("loanAsset") or {}).get("symbol") or ""
    coll_sym = (m.get("collateralAsset") or {}).get("symbol") or ""
    state = m.get("state") or {}
    return {
        "id": m.get("marketId"),
        "loan": loan_sym,
        "collateral": coll_sym,
        "lltv_pct": round(float(m.get("lltv") or 0) / 1e16, 2),
        "supply_apy_pct": round((state.get("netSupplyApy") or state.get("supplyApy") or 0) * 100, 2),
        "borrow_apy_pct": round((state.get("netBorrowApy") or state.get("borrowApy") or 0) * 100, 2),
        "borrow_usd": round(state.get("borrowAssetsUsd") or 0, 2),
        "supply_usd": round(state.get("supplyAssetsUsd") or 0, 2),
        "liquidity_usd": round(state.get("liquidityAssetsUsd") or 0, 2),
        "utilization_pct": round((state.get("utilization") or 0) * 100, 2),
        "url": url or FXSAVE_USDC_MARKET_URL,
    }


def collect_direct_market() -> dict | None:
    """USDC / fxSAVE Morpho variable market (LEND)."""
    try:
        data = graphql(
            MARKET_BY_ID_QUERY,
            {"marketId": FXSAVE_USDC_MARKET_ID, "chainId": 1},
        )
        m = data.get("marketById")
        if m:
            return _pack_market(m, FXSAVE_USDC_MARKET_URL)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] marketById failed, falling back to scan: {exc}", file=sys.stderr)

    for page in range(MAX_PAGES):
        data = graphql(MARKETS_QUERY, {"skip": page * 200})
        items = data["markets"]["items"]
        if not items:
            break
        for m in items:
            loan_sym = (m.get("loanAsset") or {}).get("symbol") or ""
            coll_sym = (m.get("collateralAsset") or {}).get("symbol") or ""
            if coll_sym.upper() == "FXSAVE" and loan_sym.upper() == "USDC":
                return _pack_market(m, FXSAVE_USDC_MARKET_URL)
    return None


FXSAVE_USDC_MARKET_ID = "0x17f7ae1b52670010976b3fe41324cbb2b1eb7dd8f492e51764b2828371b86b84"
FXSAVE_USDC_MARKET_URL = (
    "https://app.morpho.org/ethereum/variable/"
    "0x17f7ae1b52670010976b3fe41324cbb2b1eb7dd8f492e51764b2828371b86b84/usdc-fxsave?tab=market#market"
)

MARKET_BY_ID_QUERY = """
query MarketById($marketId: String!, $chainId: Int!) {
  marketById(marketId: $marketId, chainId: $chainId) {
    marketId
    loanAsset { symbol }
    collateralAsset { symbol }
    lltv
    state {
      supplyApy
      netSupplyApy
      borrowApy
      netBorrowApy
      avgBorrowApy
      avgNetBorrowApy
      dailyBorrowApy
      borrowAssetsUsd
      supplyAssetsUsd
      liquidityAssetsUsd
      utilization
    }
  }
}
"""

YIELDZ_URL = "https://yieldz.io/leverage?q=Fxsave"
YIELDZ_MARKETS = [
    {
        "id": "0x17f7ae1b52670010976b3fe41324cbb2b1eb7dd8f492e51764b2828371b86b84",
        "risk": "Med.",
    },
    {
        "id": "0x43e925e52d7873fa8acac90dd5f246087d55b3a34c344b71884a6352491ff459",
        "risk": "Med.",
    },
]


def _yieldz_max_apy(deposit_pct, borrow_pct, lltv_pct):
    if deposit_pct is None or borrow_pct is None or not lltv_pct:
        return None
    lltv = float(lltv_pct) / 100
    if lltv <= 0 or lltv >= 0.99:
        return None
    return round((float(deposit_pct) - float(borrow_pct) * lltv) / (1 - lltv), 2)


def collect_yieldz_pools() -> list[dict]:
    """Two fxSAVE/USDC markets using Yieldz Live Max APY formula."""
    yz_by_id: dict[str, dict] = {}
    try:
        resp = requests.get("https://yieldz.io/api/markets", timeout=30)
        resp.raise_for_status()
        for row in (resp.json().get("data") or []):
            rid = (row.get("id") or "").lower()
            if rid:
                yz_by_id[rid] = row
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] Yieldz /api/markets: {exc}", file=sys.stderr)

    out = []
    for spec in YIELDZ_MARKETS:
        try:
            data = graphql(
                MARKET_BY_ID_QUERY,
                {"marketId": spec["id"], "chainId": 1},
            )
            m = data.get("marketById")
            if not m:
                continue
            packed = _pack_market(m, YIELDZ_URL)
            packed["risk"] = spec["risk"]
            state = m.get("state") or {}
            live_borrow = state.get("avgNetBorrowApy") or state.get("avgBorrowApy")
            if live_borrow is not None:
                packed["borrow_apy_pct"] = round(float(live_borrow) * 100, 2)
            yz = yz_by_id.get(spec["id"].lower()) or {}
            pdata = yz.get("protocol_data") or {}
            deposit = pdata.get("collateral_intrinsic_apy_percent")
            packed["deposit_apy_pct"] = round(float(deposit), 4) if deposit is not None else None
            packed["to_util_usd"] = pdata.get("to_utilization_usd")
            if yz.get("liquidity_usd") is not None:
                packed["liquidity_usd"] = round(float(yz["liquidity_usd"]), 2)
            if yz.get("utilization") is not None:
                packed["utilization_pct"] = round(float(yz["utilization"]) * 100, 2)
            packed["max_apy_pct"] = _yieldz_max_apy(
                packed.get("deposit_apy_pct"),
                packed.get("borrow_apy_pct"),
                packed.get("lltv_pct"),
            )
            out.append(packed)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] Yieldz market {spec['id'][:10]}: {exc}", file=sys.stderr)
    return out


ROCKAWAYX_VAULT_ADDRESS = "0x2cA22cb25558fa2018ecb1CE4eD8AF92Ee7ea423"
ROCKAWAYX_VAULT_URL = (
    "https://app.morpho.org/ethereum/vault/"
    "0x2cA22cb25558fa2018ecb1CE4eD8AF92Ee7ea423/rockawayx-fx-protocol-ecosystem-usdc"
)


def _vault_rewards_pct(rewards: list | None) -> list[dict]:
    out = []
    for r in rewards or []:
        apr = r.get("supplyApr")
        symbol = ((r.get("asset") or {}).get("symbol")) or "?"
        if apr is None:
            continue
        out.append({"symbol": symbol, "apr_pct": round(float(apr) * 100, 2)})
    return out


def _pack_v2_vault(v: dict) -> dict:
    rewards = _vault_rewards_pct(v.get("rewards"))
    # Morpho redondea cada línea del breakdown y las suma:
    # 3.72% vault + 7.27% FXN = 10.99% (no 11.00% de redondear netApy).
    apy_raw = v.get("netApy")
    if apy_raw is None:
        apy_raw = v.get("avgNetApy") or 0
    return {
        "address": v["address"],
        "name": v.get("name") or "",
        "asset": (v.get("asset") or {}).get("symbol"),
        "total_assets_usd": round(v.get("totalAssetsUsd") or 0, 2),
        "net_apy_pct": round(float(apy_raw) * 100, 2),
        "rewards": rewards,
        "url": ROCKAWAYX_VAULT_URL if v["address"].lower() == ROCKAWAYX_VAULT_ADDRESS.lower()
        else f"https://app.morpho.org/ethereum/vault/{v['address']}",
    }


def collect_rockawayx_vault() -> dict | None:
    data = graphql(
        VAULT_V2_BY_ADDRESS_QUERY,
        {"address": ROCKAWAYX_VAULT_ADDRESS, "chainId": 1},
    )
    v = data.get("vaultV2ByAddress")
    if v:
        print(
            f"[debug] RockawayX V2 -- netApy={v.get('netApy')} "
            f"avgNetApy={v.get('avgNetApy')} rewards={v.get('rewards')}",
            file=sys.stderr,
        )
        return _pack_v2_vault(v)
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
  background: #e9fbe9; padding: 10px 18px; border-radius: 12px;
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
.live-heart {
  display: inline-block;
  color: #39ff14;
  font-size: 1.05rem;
  line-height: 1;
  animation: heart-blink 1.2s ease-in-out infinite;
  filter: drop-shadow(0 0 6px rgba(57, 255, 20, 0.75));
}
@keyframes heart-blink {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.25; transform: scale(0.88); }
}
.subtitle {
  color: #0b3d91; font-weight: 700; font-size: 1.1rem; margin-bottom: 2rem;
  background: #e9fbe9; padding: 8px 14px; border-radius: 10px;
  display: inline-block;
}
.section-title {
  font-size: 1.7rem; font-weight: 900; margin: 2.5rem 0 0.85rem;
  color: #0b3d91; text-transform: uppercase; letter-spacing: 0.03em;
  background: #e9fbe9; padding: 8px 14px; border-radius: 10px;
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
.lend-badge {
  display: inline-block;
  font-size: 0.72rem;
  font-weight: 900;
  letter-spacing: 0.08em;
  background: #39ff14;
  color: #05210a;
  padding: 3px 9px;
  border-radius: 6px;
  margin-right: 6px;
  vertical-align: middle;
}
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
.peg-chart {
  background: var(--card);
  border: 1px solid var(--card-border);
  border-radius: 14px;
  padding: 1rem 1.1rem;
  margin-bottom: 2rem;
}
.peg-title { font-size: 0.8rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin: 0 0 4px; font-weight: 700; }
.peg-value { font-size: 1.1rem; color: #fff; font-weight: 700; margin: 0 0 8px; font-variant-numeric: tabular-nums; }
.cmp-legend { display: flex; flex-wrap: wrap; gap: 8px 14px; margin: 0 0 10px; }
.cmp-item { font-size: 0.75rem; color: #c8d0d8; display: inline-flex; align-items: center; gap: 6px; }
.cmp-item i { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.cmp-item b { color: #fff; font-variant-numeric: tabular-nums; }
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


def render_peg_chart(history: list[dict] | None) -> str:
    if not history or len(history) < 2:
        return ""

    prices = [h["price"] for h in history]
    width, height = 700, 190
    pad = 10
    chart_bottom = height - 30  # leave room for date labels below the line
    lo, hi = min(prices + [0.994]), max(prices + [1.006])
    if hi == lo:
        hi = lo + 0.001
    span_y = hi - lo

    def x_at(i: int) -> float:
        return pad + (i / (len(prices) - 1)) * (width - 2 * pad)

    def y_at(v: float) -> float:
        return pad + (1 - (v - lo) / span_y) * (chart_bottom - pad)

    points = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, v in enumerate(prices))
    peg_y = y_at(1.0)
    current = prices[-1]
    deviation_bps = round((current - 1.0) * 10000, 1)

    # Date labels: show roughly 4 evenly-spaced month markers along the axis.
    num_labels = min(4, len(history))
    label_svg = ""
    if num_labels >= 2:
        for k in range(num_labels):
            idx = round(k * (len(history) - 1) / (num_labels - 1))
            ts = history[idx]["timestamp"]
            label = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b '%y")
            anchor = "start" if idx == 0 else ("end" if idx == len(history) - 1 else "middle")
            label_svg += f'<text x="{x_at(idx):.1f}" y="{height - 8}" font-size="11" fill="#8a94a3" text-anchor="{anchor}">{label}</text>'

    return f"""
    <div class="peg-chart">
      <p class="peg-title">Peg Deviation (fxUSD)</p>
      <p class="peg-value"><span class="live-heart">♥</span> $<span id="peg-current-price">{current:.4f}</span> &middot; <span id="peg-deviation">{deviation_bps:+.1f}</span> bps</p>
      <svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" style="width: 100%; height: 140px;">
        <line x1="{pad}" y1="{peg_y:.1f}" x2="{width - pad}" y2="{peg_y:.1f}" stroke="#3a4658" stroke-width="1" stroke-dasharray="4,3" />
        <polyline points="{points}" fill="none" stroke="#4ade80" stroke-width="2" />
        {label_svg}
      </svg>
    </div>
    """


def _fmt_compact_usd(n: float) -> str:
    if n >= 1_000_000_000:
        return f"${n / 1_000_000_000:.2f}b"
    if n >= 1_000_000:
        return f"${n / 1_000_000:.2f}m"
    if n >= 1_000:
        return f"${n / 1_000:.1f}k"
    return f"${n:,.0f}"


def render_mcap_chart(history: list[dict] | None) -> str:
    if not history or len(history) < 2:
        return ""

    values = [h["mcap"] for h in history]
    width, height = 700, 210
    pad_l, pad_r, pad_t, pad_b = 44, 10, 10, 32
    lo, hi = 0.0, max(values)
    if hi <= 0:
        hi = 1.0
    span_y = hi - lo

    def x_at(i: int) -> float:
        return pad_l + (i / (len(values) - 1)) * (width - pad_l - pad_r)

    def y_at(v: float) -> float:
        return pad_t + (1 - (v - lo) / span_y) * (height - pad_t - pad_b)

    points = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, v in enumerate(values))
    # Area fill under the line
    area = f"{pad_l:.1f},{height - pad_b:.1f} " + points + f" {width - pad_r:.1f},{height - pad_b:.1f}"
    current = values[-1]

    ticks = [0, hi * 0.2, hi * 0.4, hi * 0.6, hi * 0.8, hi]
    tick_svg = ""
    for t in ticks:
        y = y_at(t)
        label = _fmt_compact_usd(t).replace("$", "")
        tick_svg += (
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
            f'stroke="#243044" stroke-width="1" />'
            f'<text x="{pad_l - 6}" y="{y + 3:.1f}" font-size="10" fill="#8a94a3" text-anchor="end">{label}</text>'
        )

    num_labels = min(6, len(history))
    label_svg = ""
    if num_labels >= 2:
        for k in range(num_labels):
            idx = round(k * (len(history) - 1) / (num_labels - 1))
            ts = history[idx]["timestamp"]
            label = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %Y")
            anchor = "start" if idx == 0 else ("end" if idx == len(history) - 1 else "middle")
            label_svg += f'<text x="{x_at(idx):.1f}" y="{height - 8}" font-size="11" fill="#8a94a3" text-anchor="{anchor}">{label}</text>'

    return f"""
    <div class="peg-chart">
      <p class="peg-title">fxUSD Market Cap</p>
      <svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" style="width: 100%; height: 180px;">
        {tick_svg}
        <polygon points="{area}" fill="rgba(57, 255, 20, 0.22)" />
        <polyline points="{points}" fill="none" stroke="#39ff14" stroke-width="2.5" />
        {label_svg}
      </svg>
    </div>
    """


def render_compare_chart(histories: dict[str, list[dict]] | None) -> str:
    if not histories:
        return ""

    fx_hist = histories.get("fxUSD") or []
    if len(fx_hist) < 2:
        return ""

    start_ts = fx_hist[0]["timestamp"]
    all_ts = set()
    series_map: dict[str, dict[int, float]] = {}
    for spec in COMPARE_STABLES:
        hist = histories.get(spec["key"]) or []
        mp: dict[int, float] = {}
        for row in hist:
            if row["timestamp"] >= start_ts:
                mp[row["timestamp"]] = row["mcap"]
                all_ts.add(row["timestamp"])
        if mp:
            series_map[spec["key"]] = mp
    if not series_map:
        return ""

    times = sorted(all_ts)
    if len(times) > 280:
        step = max(1, len(times) // 280)
        times = times[::step]
        if times[-1] != sorted(all_ts)[-1]:
            times.append(sorted(all_ts)[-1])

    hi = 0.0
    last_vals: dict[str, float] = {}
    aligned: dict[str, list[tuple[int, float]]] = {}
    for key, mp in series_map.items():
        pts = []
        last = None
        for ts in times:
            if ts in mp:
                last = mp[ts]
            if last is None:
                continue
            pts.append((ts, last))
            if last > hi:
                hi = last
        if pts:
            aligned[key] = pts
            last_vals[key] = pts[-1][1]
    if hi <= 0:
        hi = 1.0

    width, height = 700, 280
    pad_l, pad_r, pad_t, pad_b = 48, 10, 10, 32
    t0, t1 = times[0], times[-1]
    span_t = max(t1 - t0, 1)
    span_y = hi

    def x_at_ts(ts: int) -> float:
        return pad_l + ((ts - t0) / span_t) * (width - pad_l - pad_r)

    def y_at(v: float) -> float:
        return pad_t + (1 - v / span_y) * (height - pad_t - pad_b)

    polylines = ""
    glow = ""
    for spec in COMPARE_STABLES:
        pts = aligned.get(spec["key"])
        if not pts:
            continue
        points = " ".join(f"{x_at_ts(ts):.1f},{y_at(val):.1f}" for ts, val in pts)
        stroke = spec["color"]
        sw = 2.6 if spec["key"] == "fxUSD" else 1.6
        if spec["key"] == "fxUSD":
            glow = f'<polyline points="{points}" fill="none" stroke="{stroke}" stroke-width="8" opacity="0.18" />'
        polylines += f'<polyline id="cmp-line-{spec["key"]}" points="{points}" fill="none" stroke="{stroke}" stroke-width="{sw}" />'

    ticks = [0, hi * 0.2, hi * 0.4, hi * 0.6, hi * 0.8, hi]
    tick_svg = ""
    for t in ticks:
        y = y_at(t)
        label = _fmt_compact_usd(t).replace("$", "")
        tick_svg += (
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
            f'stroke="#243044" stroke-width="1" />'
            f'<text x="{pad_l - 6}" y="{y + 3:.1f}" font-size="10" fill="#8a94a3" text-anchor="end">{label}</text>'
        )

    num_labels = min(6, len(times))
    label_svg = ""
    if num_labels >= 2:
        for k in range(num_labels):
            idx = round(k * (len(times) - 1) / (num_labels - 1))
            ts = times[idx]
            label = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %Y")
            anchor = "start" if idx == 0 else ("end" if idx == len(times) - 1 else "middle")
            label_svg += f'<text x="{x_at_ts(ts):.1f}" y="{height - 8}" font-size="11" fill="#8a94a3" text-anchor="{anchor}">{label}</text>'

    legend_items = ""
    for spec in COMPARE_STABLES:
        if spec["key"] not in last_vals:
            continue
        val = last_vals[spec["key"]]
        legend_items += (
            f'<span class="cmp-item">'
            f'<i style="background:{spec["color"]}"></i>'
            f'{spec["label"]} '
            f'<b id="cmp-val-{spec["key"]}">{_fmt_compact_usd(val)}</b>'
            f'</span>'
        )

    spec_json = json.dumps(
        [{"key": s["key"], "id": s["id"], "symbol": s["symbol"]} for s in COMPARE_STABLES],
        separators=(",", ":"),
    )

    return f"""
    <div class="peg-chart">
      <p class="peg-title">Ethereum supply · fxUSD vs peers</p>
      <div class="cmp-legend">{legend_items}</div>
      <svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" style="width: 100%; height: 240px;">
        {tick_svg}
        {glow}
        {polylines}
        {label_svg}
      </svg>
      <script type="application/json" id="cmp-spec">{spec_json}</script>
    </div>
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


def render_yieldz_card(m: dict, fxsave_apy: float | None = None) -> str:
    max_apy = m.get("max_apy_pct")
    if max_apy is None:
        max_apy = _yieldz_max_apy(m.get("deposit_apy_pct") or fxsave_apy, m.get("borrow_apy_pct"), m.get("lltv_pct"))
    max_html = f"{max_apy}%" if max_apy is not None else "—"
    sid = (m.get("id") or "")[:10]
    to_util = m.get("to_util_usd")
    to_util_html = "—"
    if isinstance(to_util, (int, float)):
        sign = "-" if to_util < 0 else ""
        to_util_html = f"{sign}${abs(to_util):,.0f}"
    return f"""
    <a class="card" href="{YIELDZ_URL}" target="_blank" rel="noopener" data-yz-id="{m.get('id') or ''}" data-yz-lltv="{m.get('lltv_pct') or 0}" data-yz-deposit="{m.get('deposit_apy_pct') or ''}">
      <p class="card-title">{m['collateral']} / {m['loan']}</p>
      <p class="card-sub">Yieldz &middot; Morpho &middot; {m.get('risk', 'Med.')} &middot; Ethereum</p>
      <div class="stat-row">
        <div class="stat">
          <p class="stat-label">Max APY</p>
          <p class="stat-value apy"><span id="yz-{sid}-apy">{max_html}</span></p>
        </div>
        <div class="stat">
          <p class="stat-label">Avail. Liq.</p>
          <p class="stat-value">$<span id="yz-{sid}-liq">{m.get('liquidity_usd', 0):,.0f}</span></p>
        </div>
      </div>
      <div class="stat-row">
        <div class="stat">
          <p class="stat-label">To Util.</p>
          <p class="stat-value"><span id="yz-{sid}-toutil">{to_util_html}</span></p>
        </div>
        <div class="stat">
          <p class="stat-label">Utilization</p>
          <p class="stat-value"><span id="yz-{sid}-util">{m['utilization_pct']}</span>%</p>
        </div>
      </div>
    </a>
    """


def render_lend_card(m: dict) -> str:
    lend = m.get("supply_apy_pct")
    borrow = m.get("borrow_apy_pct")
    return f"""
    <a class="card" href="{FXSAVE_USDC_MARKET_URL}" target="_blank" rel="noopener">
      <p class="card-title"><span class="lend-badge">LEND</span>USDC / fxSAVE</p>
      <p class="card-sub">Morpho Variable &middot; Lend Rate (incl. rewards)</p>
      <div class="stat-row">
        <div class="stat">
          <p class="stat-label">Lend Rate</p>
          <p class="stat-value apy"><span id="lend-rate">{lend if lend is not None else '—'}</span>{'%' if lend is not None else ''}</p>
        </div>
        <div class="stat">
          <p class="stat-label">Borrow Rate</p>
          <p class="stat-value"><span id="lend-borrow">{borrow if borrow is not None else '—'}</span>{'%' if borrow is not None else ''}</p>
        </div>
      </div>
      <div class="stat-row">
        <div class="stat">
          <p class="stat-label">Total Liquidity</p>
          <p class="stat-value">${m.get('liquidity_usd', 0):,.0f}</p>
        </div>
        <div class="stat">
          <p class="stat-label">Prestado</p>
          <p class="stat-value">${m.get('borrow_usd', 0):,.0f}</p>
        </div>
      </div>
    </a>
    """


def render_vault_card(v: dict) -> str:
    reward_note = ""
    rewards = v.get("rewards") or []
    if rewards:
        parts = " + ".join(f"{r['apr_pct']}% {r['symbol']}" for r in rewards)
        reward_note = f' <span class="pill">incl. {parts}</span>'
    fxn_pct = next((r["apr_pct"] for r in rewards if r["symbol"].upper() == "FXN"), None)
    fxn_html = (
        f' <span class="pill">incl. <span id="rockawayx-fxn">{fxn_pct}</span>% FXN</span>'
        if fxn_pct is not None
        else reward_note
    )
    return f"""
    <a class="card" href="{v['url']}" target="_blank" rel="noopener">
      <p class="card-title">{v['name']}</p>
      <p class="card-sub">Morpho &middot; vault curado por RockawayX &middot; activo {v['asset']}</p>
      <div class="stat-row">
        <div class="stat">
          <p class="stat-label">Net APY</p>
          <p class="stat-value apy"><span id="rockawayx-apy">{v['net_apy_pct']}</span>%</p>
        </div>
        <div class="stat">
          <p class="stat-label">Depositos totales</p>
          <p class="stat-value">$<span id="rockawayx-tvl">{v['total_assets_usd']:,.0f}</span></p>
        </div>
      </div>
      {fxn_html}
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
    fxusd_peg_history: list[float] | None = None,
    hydrex_pools: list[dict] | None = None,
    fxusd_mcap_history: list[dict] | None = None,
    compare_histories: dict[str, list[dict]] | None = None,
    yieldz_pools: list[dict] | None = None,
) -> str:
    peg_chart_html = render_peg_chart(fxusd_peg_history)
    mcap_chart_html = render_mcap_chart(fxusd_mcap_history)
    compare_chart_html = render_compare_chart(compare_histories)

    fxusd_mcap_val = (fxusd_mcap or {}).get("mcap") or 0
    fxusd_price_val = (fxusd_mcap or {}).get("price") or 0
    fxsave_mcap_val = (fxsave_mcap or {}).get("mcap") or 0
    fxsave_price_val = (fxsave_mcap or {}).get("price") or 0
    fxsave_apy = (fxsave_apy_data or {}).get("apy")
    fxsave_apy_change = (fxsave_apy_data or {}).get("change_24h")

    market_html = render_market_card(market) if market else '<p class="empty">Mercado no disponible en este momento.</p>'
    lend_html = render_lend_card(market) if market else ""
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
        render_section("Morpho", (lend_html or market_html) + vault_html),
        render_section(
            "YIELDZ",
            "".join(render_yieldz_card(p, fxsave_apy) for p in (yieldz_pools or []))
            or '<p class="empty">Sin pools activos en este momento.</p>',
        ),
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
        pt_change_badge = render_apy_change_badge(pendle_rockawayx.get("pt_apy_change_24h"))
        lp_change_badge = render_apy_change_badge(pendle_rockawayx.get("lp_apy_change_24h"))
        pendle_html = f"""
    <a class="card" href="{PENDLE_ROCKAWAYX_URL}" target="_blank" rel="noopener">
      <p class="card-title">PT-fxSAVE</p>
      <p class="card-sub">f(x) USD Saving &middot; Pendle V2</p>
      <div class="stat-row">
        <div class="stat">
          <p class="stat-label">APY</p>
          <p class="stat-value apy">{pt_apy if pt_apy is not None else 'no disponible'}{'%' if pt_apy is not None else ''} {pt_change_badge}</p>
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
          <p class="stat-value apy">{lp_apy if lp_apy is not None else 'no disponible'}{'%' if lp_apy is not None else ''} {lp_change_badge}</p>
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

    hydrex_html = "".join(render_defillama_card(p) for p in (hydrex_pools or []))
    sections.append(render_section("Hydrex", hydrex_html if hydrex_html else '<p class="empty">Sin pools activos en este momento.</p>'))

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

    {peg_chart_html}

    {mcap_chart_html}

    {compare_chart_html}

    <h1><span class="live-dot"></span>DASHBOARD FXUSD &amp; FXSAVE POOLS LIVE</h1>

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

      function fmtCompactUsd(n) {{
        if (n >= 1e9) return '$' + (n / 1e9).toFixed(2) + 'b';
        if (n >= 1e6) return '$' + (n / 1e6).toFixed(2) + 'm';
        if (n >= 1e3) return '$' + (n / 1e3).toFixed(1) + 'k';
        return '$' + Math.round(n);
      }}

      function assetUsd(asset) {{
        var circ = asset.circulating || {{}};
        if (typeof circ.peggedUSD === 'number') return circ.peggedUSD;
        var price = asset.price;
        if (typeof circ.peggedCHF === 'number') return circ.peggedCHF * (price || 1);
        if (typeof circ.peggedEUR === 'number') return circ.peggedEUR * (price || 1);
        return null;
      }}

      function refreshAll() {{
        fetch('https://stablecoins.llama.fi/stablecoins?includePrices=true')
          .then(function(r) {{ return r.json(); }})
          .then(function(data) {{
            var list = data.peggedAssets || [];
            var specEl = document.getElementById('cmp-spec');
            var spec = [];
            try {{ spec = specEl ? JSON.parse(specEl.textContent || '[]') : []; }} catch (e) {{}}
            var byId = {{}};
            for (var s = 0; s < spec.length; s++) byId[String(spec[s].id)] = spec[s].key;
            for (var i = 0; i < list.length; i++) {{
              var asset = list[i];
              var usd = assetUsd(asset);
              var key = byId[String(asset.id)];
              if (key && typeof usd === 'number') {{
                var el = document.getElementById('cmp-val-' + key);
                if (el) el.textContent = fmtCompactUsd(usd);
              }}
              if ((asset.symbol || '').toUpperCase() === 'FXUSD') {{
                setValue('fxusd-mcap', (asset.circulating || {{}}).peggedUSD);
                setPrice('fxusd-price', asset.price);
                if (asset.price) {{
                  setPrice('peg-current-price', asset.price);
                  var pegEl = document.getElementById('peg-deviation');
                  if (pegEl) {{
                    var bps = (asset.price - 1.0) * 10000;
                    pegEl.textContent = (bps >= 0 ? '+' : '') + bps.toFixed(1);
                  }}
                }}
                if (typeof whalePrices !== 'undefined' && asset.price) {{
                  whalePrices.FXUSD = asset.price;
                }}
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

        fetch('https://api.morpho.org/graphql', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            query: 'query {{ vaultV2ByAddress(address: "0x2cA22cb25558fa2018ecb1CE4eD8AF92Ee7ea423", chainId: 1) {{ totalAssetsUsd netApy rewards {{ supplyApr asset {{ symbol }} }} }} }}'
          }})
        }})
          .then(function(r) {{ return r.json(); }})
          .then(function(payload) {{
            var v = payload && payload.data && payload.data.vaultV2ByAddress;
            if (!v) return;
            if (typeof v.netApy === 'number') {{
              var apyEl = document.getElementById('rockawayx-apy');
              if (apyEl) apyEl.textContent = (v.netApy * 100).toFixed(2);
            }}
            if (typeof v.totalAssetsUsd === 'number') {{
              setValue('rockawayx-tvl', v.totalAssetsUsd);
            }}
            var rewards = v.rewards || [];
            for (var i = 0; i < rewards.length; i++) {{
              var sym = ((rewards[i].asset || {{}}).symbol || '').toUpperCase();
              if (sym === 'FXN' && typeof rewards[i].supplyApr === 'number') {{
                var fxnEl = document.getElementById('rockawayx-fxn');
                if (fxnEl) fxnEl.textContent = (rewards[i].supplyApr * 100).toFixed(2);
              }}
            }}
          }})
          .catch(function() {{ /* keep last known value on failure */ }});

        var yzIds = {json.dumps([s["id"] for s in YIELDZ_MARKETS])};
        fetch('https://api.morpho.org/graphql', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            query: 'query($ids: [String!]!) {{ markets(where: {{ uniqueKey_in: $ids, chainId_in: [1] }}) {{ items {{ marketId lltv state {{ netSupplyApy netBorrowApy avgNetBorrowApy avgBorrowApy liquidityAssetsUsd utilization }} }} }} }}',
            variables: {{ ids: yzIds }}
          }})
        }})
          .then(function(r) {{ return r.json(); }})
          .then(function(payload) {{
            var items = payload && payload.data && payload.data.markets && payload.data.markets.items;
            if (!items || !items.length) {{
              return Promise.all(yzIds.map(function(id) {{
                return fetch('https://api.morpho.org/graphql', {{
                  method: 'POST',
                  headers: {{ 'Content-Type': 'application/json' }},
                  body: JSON.stringify({{
                    query: 'query($marketId: String!, $chainId: Int!) {{ marketById(marketId: $marketId, chainId: $chainId) {{ marketId lltv state {{ netSupplyApy netBorrowApy avgNetBorrowApy avgBorrowApy liquidityAssetsUsd utilization }} }} }}',
                    variables: {{ marketId: id, chainId: 1 }}
                  }})
                }}).then(function(r) {{ return r.json(); }});
              }})).then(function(results) {{
                results.forEach(function(res) {{
                  var m = res && res.data && res.data.marketById;
                  if (m) applyYzMarket(m);
                }});
              }});
            }}
            items.forEach(applyYzMarket);
          }})
          .catch(function() {{ /* keep last known value on failure */ }});

        fetch('https://yieldz.io/api/markets')
          .then(function(r) {{ return r.json(); }})
          .then(function(payload) {{
            var rows = (payload && payload.data) || [];
            rows.forEach(function(row) {{
              var card = document.querySelector('[data-yz-id="' + row.id + '"]');
              if (!card) return;
              var pdata = row.protocol_data || {{}};
              if (typeof pdata.collateral_intrinsic_apy_percent === 'number') {{
                card.setAttribute('data-yz-deposit', String(pdata.collateral_intrinsic_apy_percent));
              }}
              if (typeof row.liquidity_usd === 'number') {{
                var sid = String(row.id).slice(0, 10);
                var liqEl = document.getElementById('yz-' + sid + '-liq');
                if (liqEl) liqEl.textContent = Math.round(row.liquidity_usd).toLocaleString('en-US');
                var utilEl = document.getElementById('yz-' + sid + '-util');
                if (utilEl && typeof row.utilization === 'number') utilEl.textContent = (row.utilization * 100).toFixed(2);
                var toEl = document.getElementById('yz-' + sid + '-toutil');
                if (toEl && typeof pdata.to_utilization_usd === 'number') {{
                  var v = pdata.to_utilization_usd;
                  toEl.textContent = (v < 0 ? '-' : '') + '$' + Math.round(Math.abs(v)).toLocaleString('en-US');
                }}
              }}
              refreshYzApy(card);
            }});
          }})
          .catch(function() {{}});
      }}

      function refreshYzApy(card) {{
        var sid = (card.getAttribute('data-yz-id') || '').slice(0, 10);
        var apyEl = document.getElementById('yz-' + sid + '-apy');
        var deposit = parseFloat(card.getAttribute('data-yz-deposit'));
        var borrow = parseFloat(card.getAttribute('data-yz-borrow'));
        var lltv = parseFloat(card.getAttribute('data-yz-lltv') || '0') / 100;
        if (!apyEl || isNaN(deposit) || isNaN(borrow) || !(lltv > 0 && lltv < 0.99)) return;
        apyEl.textContent = ((deposit - borrow * lltv) / (1 - lltv)).toFixed(2) + '%';
      }}

      function applyYzMarket(m) {{
        if (!m || !m.marketId) return;
        var sid = String(m.marketId).slice(0, 10);
        var st = m.state || {{}};
        var liqEl = document.getElementById('yz-' + sid + '-liq');
        var utilEl = document.getElementById('yz-' + sid + '-util');
        var card = document.querySelector('[data-yz-id="' + m.marketId + '"]');
        if (typeof st.liquidityAssetsUsd === 'number' && liqEl) liqEl.textContent = Math.round(st.liquidityAssetsUsd).toLocaleString('en-US');
        if (typeof st.utilization === 'number' && utilEl) utilEl.textContent = (st.utilization * 100).toFixed(2);
        var liveBorrow = (typeof st.avgNetBorrowApy === 'number') ? st.avgNetBorrowApy : st.avgBorrowApy;
        if (card && typeof liveBorrow === 'number') card.setAttribute('data-yz-borrow', String(liveBorrow * 100));
        if (card && typeof m.lltv !== 'undefined') {{
          var lltvPct = Number(m.lltv) > 10 ? Number(m.lltv) / 1e16 : Number(m.lltv);
          card.setAttribute('data-yz-lltv', String(lltvPct));
        }}
        if (card) refreshYzApy(card);
        if (m.marketId === '{FXSAVE_USDC_MARKET_ID}') {{
          if (typeof st.netSupplyApy === 'number') {{
            var lr = document.getElementById('lend-rate');
            if (lr) lr.textContent = (st.netSupplyApy * 100).toFixed(2);
          }}
          if (typeof st.netBorrowApy === 'number') {{
            var br = document.getElementById('lend-borrow');
            if (br) br.textContent = (st.netBorrowApy * 100).toFixed(2);
          }}
        }}
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


APY_HISTORY_FILE = "apy_history.json"
APY_HISTORY_MAX_ENTRIES = 30  # ~7.5 days at 6h cadence, plenty to find a ~24h-old snapshot


def load_apy_history() -> list[dict]:
    try:
        with open(APY_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return []


def find_snapshot_near_24h(history: list[dict]) -> dict | None:
    """Finds the stored snapshot closest to 24h old (within a +/-4h window),
    for computing genuine 24h changes ourselves since Morpho/Pendle/Hydrex
    don't expose a 24h-change field via their own APIs."""
    if not history:
        return None
    now = datetime.now(timezone.utc).timestamp()
    target = now - 24 * 3600
    best, best_diff = None, None
    for snap in history:
        diff = abs(snap.get("ts", 0) - target)
        if diff <= 4 * 3600 and (best_diff is None or diff < best_diff):
            best, best_diff = snap, diff
    return best


def _apy_as_pct(value: float | None) -> float | None:
    """History used to mix fractions (0.11) and percents (10.99)."""
    if value is None:
        return None
    return round(value * 100, 2) if abs(value) <= 1 else float(value)


def pct_point_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    cur = _apy_as_pct(current)
    prev = _apy_as_pct(previous)
    if cur is None or prev is None:
        return None
    change = round(cur - prev, 2)
    # A 9pp "24h" move here is almost always bad history, not a real rate jump.
    if abs(change) > 4:
        return None
    return change


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
    fxusd_peg_history = collect_fxusd_peg_history()
    fxusd_mcap_history = collect_fxusd_mcap_history()
    compare_histories = collect_compare_histories()
    hydrex_pools = collect_hydrex_pools()
    fxsave_mcap = collect_fxsave_mcap()
    fxsave_apy = collect_fxsave_apy()
    pendle_rockawayx = collect_pendle_fxsave_market()
    try:
        yieldz_pools = collect_yieldz_pools()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not fetch Yieldz pools: {exc}", file=sys.stderr)
        yieldz_pools = []

    # Compute real 24h changes for Morpho/Pendle/Hydrex ourselves, since
    # none of those APIs expose a 24h-change field like DeFiLlama does.
    history = load_apy_history()
    prev = find_snapshot_near_24h(history)

    vault_apy_change = pct_point_change(
        (vault or {}).get("net_apy_pct"),
        (prev or {}).get("vault_apy") if prev else None,
    )
    pendle_pt_change = pct_point_change(
        (pendle_rockawayx or {}).get("pt_apy"),
        (prev or {}).get("pendle_pt_apy") if prev else None,
    )
    pendle_lp_change = pct_point_change(
        (pendle_rockawayx or {}).get("lp_apy"),
        (prev or {}).get("pendle_lp_apy") if prev else None,
    )
    prev_hydrex = (prev or {}).get("hydrex") or {}
    for pool in hydrex_pools:
        pool["apy_change_24h"] = pct_point_change(pool.get("apy_pct"), prev_hydrex.get(pool["symbol"]))

    if vault is not None:
        vault["apy_change_24h"] = vault_apy_change
    if pendle_rockawayx is not None:
        pendle_rockawayx["pt_apy_change_24h"] = pendle_pt_change
        pendle_rockawayx["lp_apy_change_24h"] = pendle_lp_change

    history.append(
        {
            "ts": datetime.now(timezone.utc).timestamp(),
            "vault_apy": (vault or {}).get("net_apy_pct"),
            "pendle_pt_apy": (pendle_rockawayx or {}).get("pt_apy"),
            "pendle_lp_apy": (pendle_rockawayx or {}).get("lp_apy"),
            "hydrex": {p["symbol"]: p["apy_pct"] for p in hydrex_pools},
        }
    )
    history = history[-APY_HISTORY_MAX_ENTRIES:]
    with open(APY_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)

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
                "fxusd_peg_history_points": len(fxusd_peg_history) if fxusd_peg_history else 0,
                "fxusd_mcap_history_points": len(fxusd_mcap_history) if fxusd_mcap_history else 0,
                "hydrex_pools": hydrex_pools,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )

    html = render_html(market, vault, defillama, fxusd_mcap, fxsave_mcap, fxsave_apy, pendle_rockawayx, fxusd_peg_history, hydrex_pools, fxusd_mcap_history, compare_histories, yieldz_pools)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    counts = {k: len(v) for k, v in defillama.items()}
    print(
        f"Listo: market={'ok' if market else 'missing'}, vault={'ok' if vault else 'missing'}, "
        f"fxusd_mcap={'ok' if fxusd_mcap else 'missing'}, fxsave_mcap={'ok' if fxsave_mcap else 'missing'}, "
        f"fxsave_apy={(fxsave_apy or {}).get('apy', 'missing')}, "
        f"pendle_pt_apy={(pendle_rockawayx or {}).get('pt_apy', 'missing')}, "
        f"pendle_lp_apy={(pendle_rockawayx or {}).get('lp_apy', 'missing')}, "
        f"defillama={counts}, hydrex_pools={len(hydrex_pools)}"
    )


if __name__ == "__main__":
    main()
