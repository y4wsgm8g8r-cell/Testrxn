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
import os
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
PENDLE_ROCKAWAYX_YT_URL = f"https://app.pendle.finance/trade/markets/{PENDLE_ROCKAWAYX_MARKET_ADDRESS}/swap?view=yt&chain=ethereum"



SPIRAL_FXSAVE_ID = "0x17f7ae1b52670010976b3fe41324cbb2b1eb7dd8f492e51764b2828371b86b84"
SPIRAL_FXSAVE_URL = "https://app.spiralstake.xyz/1/strategies/0x17f7ae1b52670010976b3fe41324cbb2b1eb7dd8f492e51764b2828371b86b84/fxSAVE-USDC?profile=yield"


LAGOON_FXLOOP_ADDRESS = "0x4Ef75e307f7EDbf0462C679192C1FF7E911c389a"
LAGOON_FXLOOP_URL = "https://app.lagoon.finance/vault/1/0x4Ef75e307f7EDbf0462C679192C1FF7E911c389a"

def collect_lagoon_fxloop() -> dict | None:
    """Fetches the fxLOOP vault from Lagoon Finance GraphQL API."""
    try:
        query = """
        query {
          vaultByAddress(address: "%s", chainId: 1) {
            name
            symbol
            state {
              yearlyApr { linearNetApr }
              totalAssetsUsd
            }
          }
        }
        """ % LAGOON_FXLOOP_ADDRESS
        resp = requests.post(
            "https://api.lagoon.finance/query",
            json={"query": query},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()["data"]["vaultByAddress"]
        state = data["state"]
        return {
            "name": data.get("name") or "fxLOOP",
            "symbol": data.get("symbol") or "fxLOOP",
            "apy": float(state["yearlyApr"]["linearNetApr"] or 0),
            "tvl": float(state["totalAssetsUsd"] or 0),
            "url": LAGOON_FXLOOP_URL,
        }
    except Exception as exc:
        print(f"[warn] Lagoon fxLOOP fetch failed: {exc}", file=sys.stderr)
        return None

def collect_spiral_fxsave() -> dict | None:
    """Fetches the fxSAVE/USDC leveraged strategy from SpiralStake public API."""
    try:
        resp = requests.get(f"https://api.spiralstake.xyz/v1/strategies/{SPIRAL_FXSAVE_ID}", timeout=30)
        resp.raise_for_status()
        d = resp.json()
        return {
            "name": "fxSAVE / USDC",
            "platform": "SpiralStake",
            "apy": float(d.get("defaultLeverage", {}).get("leverageApyPct") or 0),
            "base_apy": float(d.get("collateralApyPct") or 0),
            "tvl": d.get("supplyUsd") or 0,
            "liquidity": d.get("liquidityUsd") or 0,
            "leverage": d.get("defaultLeverage", {}).get("leverage", "10.5"),
            "url": SPIRAL_FXSAVE_URL,
        }
    except Exception as exc:
        print(f"[warn] SpiralStake fetch failed: {exc}", file=sys.stderr)
        return None

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
        # En la UI de Pendle (vista YT) el número grande no es ytFloatingApy
        # (~1177%). Es Underlying APY + Campaign Boost: protocol yield +
        # incentivos exclusivos de YT (p.ej. 6.72% + 5.20% = 11.91%).
        yt_apy_raw = None
        try:
            all_url = (
                "https://api-v2.pendle.finance/core/v2/markets/all"
                "?chainId=1&limit=100&isActive=true"
            )
            all_resp = requests.get(all_url, timeout=30)
            all_resp.raise_for_status()
            want = PENDLE_ROCKAWAYX_MARKET_ADDRESS.lower()
            for m in (all_resp.json().get("results") or []):
                if (m.get("address") or "").lower() != want:
                    continue
                cats = ((m.get("ytApyBreakdown") or {}).get("categories")) or []
                if cats:
                    yt_apy_raw = sum(
                        float(c["apy"]) for c in cats
                        if isinstance(c.get("apy"), (int, float))
                    )
                break
        except Exception as yt_exc:  # noqa: BLE001
            print(f"[warn] Pendle YT breakdown: {yt_exc}", file=sys.stderr)
        if yt_apy_raw is None:
            # Fallback: underlying + reward fields from /data
            und = find_apy(("underlyingApy", "underlyingInterestApy"))
            rew = find_apy(("underlyingRewardApy",))
            if und is not None:
                yt_apy_raw = und + (rew or 0)

        # Pendle publica dos cifras distintas:
        #   totalTvl  -> todo el mercado (PT + YT + LP) ≈ $2.18M
        #   liquidity -> solo el AMM (lo que ves como "AMM Liquidity") ≈ $898k
        # En las cards usamos Total TVL, como la ficha de PT.
        tvl = None
        total_tvl = data.get("totalTvl")
        if isinstance(total_tvl, dict):
            tvl = total_tvl.get("usd")
        elif isinstance(total_tvl, (int, float)):
            tvl = total_tvl
        if tvl is None:
            liquidity = data.get("liquidity")
            if isinstance(liquidity, dict):
                tvl = liquidity.get("usd")
            elif isinstance(data.get("tvl"), (int, float)):
                tvl = data.get("tvl")

        if pt_apy_raw is None and lp_apy_raw is None and yt_apy_raw is None:
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
            "yt_apy": to_pct(yt_apy_raw),
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
    {"key": "fxUSD", "label": "fxUSD", "id": 168, "symbol": "FXUSD", "color": "#b9e63a"},
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


# ---------------------------------------------------------------------------
# Stability Pool funding (f(x) Protocol)
# Funding nivel I se activa cuando el USDC del Stability Pool baja del 5%
# del total (USDC + fxUSD). Fuente: docs de f(x), "Advanced Peg Protection
# Mechanisms". Si la gobernanza cambia el umbral, edita FUNDING_USDC_THRESHOLD_PCT.
#
# Formula (U = USDC del pool, F = fxUSD del pool, t = umbral):
#     U / (U + F) = t   ->   U_necesario = F * t / (1 - t)
#     con t = 5%        ->   U_necesario = F / 19
#     USDC que falta    =    max(U_necesario - U, 0)
# ---------------------------------------------------------------------------
STABILITY_POOL_ADDRESS = "0x65C9A641afCEB9C0E6034e558A319488FA0FA3be"
USDC_CONTRACT = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"

FUNDING_USDC_THRESHOLD_PCT = 5.0
# Funding Level I = Aave V3 Ethereum USDC variable borrow rate (charged to xPOSITIONs when USDC share < 5%)
# Live rate is fetched client-side; this is only a fallback note.
AAVE_USDC_BORROW_NOTE = "Aave USDC borrow rate (Level I)"

# Contrato que calcula el funding (lectura en vivo del Rate).
# getLongPoolFundingRatio(pool) devuelve la tasa anual x 1e18 del pool long de wstETH.
POOL_CONFIGURATION_ADDRESS = "0x16b334f2644cc00b85DB1A1efF0C2C395e00C28d"
WSTETH_LONG_POOL_ADDRESS = "0x6Ecfa38FeE8a5277B91eFdA204c235814F0122E8"
FUNDING_RATIO_SELECTOR = "0xeacb05f0"  # getLongPoolFundingRatio(address)

STABILITY_POOL_URL = f"https://etherscan.io/address/{STABILITY_POOL_ADDRESS}"


def _stability_pool_balances() -> tuple[float, float] | None:
    """Returns (usdc, fxusd) held by the Stability Pool contract.

    Prefers Etherscan (more up-to-date). Falls back to Blockscout.
    Matches by contract address to ignore fake spam tokens."""
    # Prefer Etherscan
    try:
        def token_balance(contract: str, decimals: int) -> float:
            r = requests.get(
                "https://api.etherscan.io/v2/api",
                params={
                    "chainid": 1,
                    "module": "account",
                    "action": "tokenbalance",
                    "contractaddress": contract,
                    "address": STABILITY_POOL_ADDRESS,
                    "tag": "latest",
                    "apikey": ETHERSCAN_API_KEY,
                },
                timeout=30,
            )
            r.raise_for_status()
            return int(r.json()["result"]) / (10 ** decimals)

        return token_balance(USDC_CONTRACT, 6), token_balance(FXUSD_CONTRACT, 18)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] Etherscan stability pool balances failed: {exc}", file=sys.stderr)

    # Fallback: Blockscout
    try:
        resp = requests.get(
            f"https://eth.blockscout.com/api/v2/addresses/{STABILITY_POOL_ADDRESS}/token-balances",
            timeout=30,
        )
        resp.raise_for_status()
        usdc = fxusd = None
        for row in resp.json():
            token = row.get("token") or {}
            addr = (token.get("address_hash") or token.get("address") or "").lower()
            decimals = int(token.get("decimals") or 0)
            value = int(row.get("value") or 0) / (10 ** decimals)
            if addr == USDC_CONTRACT.lower():
                usdc = value
            elif addr == FXUSD_CONTRACT.lower():
                fxusd = value
        if fxusd is not None:
            return (usdc or 0.0), fxusd
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] Blockscout stability pool balances failed: {exc}", file=sys.stderr)
    return None


def compute_funding_status(usdc: float, fxusd: float, threshold_pct: float = FUNDING_USDC_THRESHOLD_PCT) -> dict:
    t = threshold_pct / 100
    total = usdc + fxusd
    share_pct = (usdc / total * 100) if total > 0 else 0.0
    usdc_needed = fxusd * t / (1 - t)          # con t=5%: fxusd / 19
    return {
        "usdc": round(usdc, 2),
        "fxusd": round(fxusd, 2),
        "threshold_pct": threshold_pct,
        "usdc_share_pct": round(share_pct, 4),
        "usdc_needed": round(usdc_needed, 2),
        "usdc_missing": round(max(usdc_needed - usdc, 0.0), 2),
        "usdc_surplus": round(max(usdc - usdc_needed, 0.0), 2),
        "pct_missing": round(max(threshold_pct - share_pct, 0.0), 4),
        "divisor": round((1 - t) / t, 4),
        "funding_on": share_pct < threshold_pct,
    }


def collect_stability_pool_funding() -> dict | None:
    balances = _stability_pool_balances()
    if balances is None:
        return None
    return compute_funding_status(*balances)


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

    deposit_7d = None
    try:
        hist = requests.get(
            "https://yieldz.io/api/intrinsic-apy/history"
            "?asset_address=0x7743e50F534a7f9F1791DdE7dCD89F7783Eefc39"
            "&asset_symbol=fxSAVE&days=7",
            timeout=30,
        )
        hist.raise_for_status()
        pts = hist.json().get("data") or []
        vals = [float(p["total_apy"]) for p in pts if p.get("total_apy") is not None]
        if vals:
            deposit_7d = sum(vals) / len(vals)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] Yieldz fxSAVE 7d APY: {exc}", file=sys.stderr)

    out = []
    for spec in YIELDZ_MARKETS:
        try:
            yz = yz_by_id.get(spec["id"].lower()) or {}
            packed = {
                "id": spec["id"],
                "loan": ((yz.get("loan_asset") or {}).get("symbol")) or "USDC",
                "collateral": ((yz.get("collateral_asset") or {}).get("symbol")) or "fxSAVE",
                "lltv_pct": round(float(yz.get("lltv") or 0) / 100, 2) if yz.get("lltv") else None,
                "risk": spec["risk"],
                "url": YIELDZ_URL,
            }
            if packed["lltv_pct"] is None:
                data = graphql(MARKET_BY_ID_QUERY, {"marketId": spec["id"], "chainId": 1})
                m = data.get("marketById")
                if not m:
                    continue
                packed = _pack_market(m, YIELDZ_URL)
                packed["risk"] = spec["risk"]
            pdata = yz.get("protocol_data") or {}
            # Live list: 7d avg fxSAVE deposit + current market borrow.
            if yz.get("borrow_apy") is not None:
                packed["borrow_apy_pct"] = round(float(yz["borrow_apy"]), 4)
            packed["deposit_apy_pct"] = round(float(deposit_7d), 4) if deposit_7d is not None else packed.get("deposit_apy_pct")
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
            if packed.get("max_apy_pct") is not None and packed["max_apy_pct"] < 0:
                continue
            if (packed.get("liquidity_usd") or 0) <= 0:
                continue
            out.append(packed)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] Yieldz market {spec['id'][:10]}: {exc}", file=sys.stderr)
    return out


TANGENT_URL = "https://app.tangent.finance/"
TANGENT_API = "https://api.tangent.finance"
TANGENT_MARKETS = [
    {
        "id": "0x849cf82e0ebcfeab8270cc5a3ea3b26cd481b754",
        "name": "USDC/fxUSD",
        "ltv": 0.90,
    },
    {
        "id": "0xa63ded87df22ee573567ad1dd87d0a71c8fcd380",
        "name": "fxUSD/reUSD",
        "ltv": 0.84,
    },
]


def _sum_apr_components(apr_obj) -> float:
    if not isinstance(apr_obj, dict):
        return 0.0
    return sum(float(v) for v in apr_obj.values() if isinstance(v, (int, float)))


def collect_tangent_pools() -> list[dict]:
    aprs_by_id: dict[str, dict] = {}
    try:
        rows = requests.get(f"{TANGENT_API}/aprs", timeout=30).json()
        for row in rows or []:
            mid = (row.get("marketAddress") or "").lower()
            if mid:
                aprs_by_id[mid] = row
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] Tangent /aprs: {exc}", file=sys.stderr)

    out = []
    for spec in TANGENT_MARKETS:
        mid = spec["id"]
        vapr = None
        borrow = 0.0
        tvl = 0.0
        row = aprs_by_id.get(mid.lower()) or {}
        current = row.get("currentAPR") or {}
        if current:
            vapr = round(_sum_apr_components(current), 2)
        try:
            hist = requests.get(
                f"{TANGENT_API}/markets/{mid}/dateFrom/2026-09-09T00:00:00.000Z",
                timeout=30,
            ).json()
            if hist:
                last = hist[-1]
                tvl = float(last.get("tvl_usd") or 0)
                borrow = float(last.get("ir_apy") or 0)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] Tangent market hist {mid[:10]}: {exc}", file=sys.stderr)
        ltv = spec["ltv"]
        max_lev = None
        if vapr is not None and 0 < ltv < 0.99:
            max_lev = round((vapr - borrow * ltv) / (1 - ltv), 2)
        out.append(
            {
                "id": mid,
                "name": spec["name"],
                "vapr": vapr,
                "borrow_apy": round(borrow, 2),
                "max_lev_vapr": max_lev,
                "tvl_usd": round(tvl, 2),
                "ltv": ltv,
                "url": TANGENT_URL,
            }
        )
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
  /* Dos verdes del billete: oscuro (cabecera, tarjetas, menu) y lima (fondo) */
  --deep: #064e06;
  --vivid: #8aba06;
  --lime: #b9e63a;
  --bg: var(--vivid);
  --card: var(--deep);
  --card-border: rgba(255, 255, 255, 0.16);
  --text: #ffffff;
  --muted: rgba(255, 255, 255, 0.62);
  --accent-text: #d6ee8c;
  --highlight: var(--lime);
  --green: var(--lime);
  --font-ui: "Space Grotesk", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  --font-num: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 0 0 calc(104px + env(safe-area-inset-bottom));
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-ui);
}
.wrap { max-width: 900px; margin: 0 auto; padding: 28px 16px 24px; }

/* ---------- Cabecera ---------- */
.topbar {
  position: sticky; top: 0; z-index: 50;
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 16px;
  background: var(--deep);
  border-bottom: 1px solid var(--card-border);
}
.brand { display: flex; align-items: center; gap: 12px; }
.brand img { height: 40px; width: auto; display: block; }
.brand-name {
  font-family: var(--font-ui); font-weight: 700; font-size: 1.3rem;
  letter-spacing: 0.08em; color: #fff;
}
.brand-text { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.brand-by { display: flex; align-items: center; gap: 6px; font-family: var(--font-ui); font-size: 0.72rem; color: var(--accent-text); letter-spacing: 0.04em; }
.brand-by img { height: 22px; width: 22px; border-radius: 50%; object-fit: cover; border: 1px solid rgba(255,255,255,0.35); }
.brand-name span { font-weight: 400; letter-spacing: 0.04em; color: var(--accent-text); }
.live-pill {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 8px 14px; border-radius: 999px;
  background: rgba(185, 230, 58, 0.12); border: 1px solid rgba(185, 230, 58, 0.45);
  color: var(--lime); font-weight: 700; font-size: 0.78rem; letter-spacing: 0.12em;
}
.live-dot {
  width: 10px; height: 10px; border-radius: 50%;
  background: var(--lime);
  box-shadow: 0 0 0 0 rgba(185, 230, 58, 0.7);
  animation: live-pulse 1.6s infinite;
  flex-shrink: 0; display: inline-block;
}
@keyframes live-pulse {
  0% { box-shadow: 0 0 0 0 rgba(185, 230, 58, 0.7); }
  70% { box-shadow: 0 0 0 10px rgba(185, 230, 58, 0); }
  100% { box-shadow: 0 0 0 0 rgba(185, 230, 58, 0); }
}
.live-heart {
  display: inline-block; color: var(--lime); font-size: 1.05rem; line-height: 1;
  animation: heart-blink 1.2s ease-in-out infinite;
  filter: drop-shadow(0 0 6px rgba(185, 230, 58, 0.75));
}
@keyframes heart-blink {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.25; transform: scale(0.88); }
}

/* ---------- Titulos ---------- */
h1 {
  font-size: 2.4rem; font-weight: 700; line-height: 1.1; letter-spacing: -0.01em;
  margin: 0 0 0.6rem; color: var(--deep);
}
.subtitle {
  color: var(--deep); font-weight: 500; font-size: 1.05rem; line-height: 1.5;
  margin: 0 0 1.6rem;
}
.section-title {
  display: block; margin: 2rem 0 0.8rem; padding: 0;
  font-size: 0.85rem; font-weight: 700; letter-spacing: 0.16em; text-transform: uppercase;
  color: var(--deep);
}

/* ---------- Tarjetas ---------- */
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }
.card {
  background: var(--card);
  border: 1px solid var(--card-border);
  border-radius: 20px;
  padding: 1.2rem 1.3rem;
  text-decoration: none; color: inherit; display: block;
  transition: border-color 0.15s ease, transform 0.15s ease;
}
.card:hover { border-color: var(--lime); transform: translateY(-2px); }
.card-title { font-weight: 700; font-size: 1.05rem; margin: 0 0 2px; }
.card-sub { color: var(--muted); font-size: 0.8rem; margin: 0 0 12px; }
.stat-row { display: flex; gap: 10px; margin-top: 6px; }
.stat {
  flex: 1; background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 12px; padding: 10px 12px;
}
.stat-label {
  font-size: 0.68rem; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--muted); margin: 0;
}
.stat-value { font-family: var(--font-num); font-size: 1.2rem; font-weight: 700; margin: 4px 0 0; }
.apy { color: var(--lime); }
.pill {
  display: inline-block; font-size: 0.7rem; background: rgba(255, 255, 255, 0.12); color: #fff;
  padding: 2px 8px; border-radius: 6px; margin-top: 8px;
}
.apy-change {
  display: inline-block; font-size: 0.7rem; font-weight: 700;
  padding: 2px 7px; border-radius: 6px; margin-left: 4px; vertical-align: middle;
}
.apy-up { background: rgba(185, 230, 58, 0.15); color: var(--lime); }
.apy-down { background: rgba(255, 99, 99, 0.18); color: #ff7b7b; }
.apy-flat { background: rgba(255, 255, 255, 0.12); color: var(--muted); }
.lend-badge {
  display: inline-block; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.1em;
  background: var(--lime); color: var(--deep);
  padding: 3px 9px; border-radius: 6px; margin-right: 6px; vertical-align: middle;
}
.empty { color: var(--deep); font-size: 0.9rem; padding: 1rem 0; }
footer { color: #000; font-size: 0.75rem; margin-top: 3rem; text-align: center; }
footer a { color: #000; }

/* ---------- Market caps ---------- */
.mcap-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-bottom: 1.5rem; }
.mcap-card {
  background: var(--card); border: 1px solid var(--card-border);
  border-radius: 20px; padding: 1.2rem 1.3rem;
}
.mcap-label {
  font-size: 0.72rem; color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.14em; margin: 0 0 8px; font-weight: 700;
}
.mcap-value {
  font-family: var(--font-num); font-size: 1.8rem; font-weight: 700; margin: 0;
  color: #fff; font-variant-numeric: tabular-nums; display: flex; align-items: center; gap: 8px;
}
.mcap-price {
  font-family: var(--font-num); font-size: 0.85rem; color: var(--lime); margin: 6px 0 0;
  font-variant-numeric: tabular-nums; animation: price-blink 1.4s infinite;
}
@keyframes price-blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }

/* ---------- Graficos ---------- */
.peg-chart {
  background: var(--card); border: 1px solid var(--card-border);
  border-radius: 20px; padding: 1.1rem 1.2rem; margin-bottom: 1.5rem;
}
.peg-title { font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.14em; margin: 0 0 6px; font-weight: 700; }
.peg-value { font-family: var(--font-num); font-size: 1.1rem; color: #fff; font-weight: 700; margin: 0 0 8px; font-variant-numeric: tabular-nums; }
.cmp-legend { display: flex; flex-wrap: wrap; gap: 8px 14px; margin: 0 0 10px; }
.cmp-item { font-size: 0.75rem; color: rgba(255, 255, 255, 0.78); display: inline-flex; align-items: center; gap: 6px; }
.cmp-item i { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.cmp-item b { color: #fff; font-family: var(--font-num); font-variant-numeric: tabular-nums; }

/* ---------- Whale feed ---------- */
.whale-box {
  background: var(--card); border: 1px solid var(--card-border);
  border-radius: 20px; padding: 1rem 1.1rem; margin-bottom: 1.5rem;
  height: 220px; overflow: hidden; position: relative;
}
.whale-title { font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.14em; margin: 0 0 8px; font-weight: 700; }
.whale-list { display: flex; flex-direction: column; gap: 8px; }
.whale-item {
  display: flex; justify-content: space-between; align-items: center;
  background: rgba(255, 255, 255, 0.05); border-radius: 10px; padding: 8px 10px;
  font-size: 0.85rem; animation: whale-in 0.5s ease; text-decoration: none; color: inherit;
}
.whale-item .whale-symbol { font-weight: 700; color: var(--accent-text); }
.whale-item .whale-amount { color: var(--lime); font-weight: 700; font-family: var(--font-num); }
@keyframes whale-in {
  from { opacity: 0; transform: translateY(-14px); }
  to { opacity: 1; transform: translateY(0); }
}

/* ---------- Funding ---------- */
.funding-box {
  background: var(--card); border: 1px solid var(--card-border);
  border-radius: 20px; padding: 1.1rem 1.2rem; margin-bottom: 1.5rem;
}
.funding-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin: 0 0 12px; }
.funding-title { font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.14em; margin: 0; font-weight: 700; }
.funding-tag { font-size: 1rem; font-weight: 700; letter-spacing: 0.08em; padding: 4px 12px; border-radius: 8px; }
.funding-on { background: rgba(255, 99, 99, 0.18); color: #ff7b7b; }
.funding-off { background: rgba(185, 230, 58, 0.15); color: var(--lime); }
.funding-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; }
.funding-formula {
  font-family: var(--font-num); font-size: 0.8rem; color: rgba(255, 255, 255, 0.78);
  background: rgba(255, 255, 255, 0.05); border-radius: 8px; padding: 8px 10px; margin: 10px 0 0; overflow-x: auto;
}
.funding-note { font-size: 0.72rem; color: var(--muted); margin: 8px 0 0; }
.funding-note a { color: var(--accent-text); }

/* ---------- Menu inferior ---------- */
.bottom-nav {
  position: fixed; left: 0; right: 0; bottom: 0; z-index: 60;
  display: grid; grid-template-columns: repeat(6, 1fr);
  padding: 10px 2px calc(10px + env(safe-area-inset-bottom));
  background: var(--deep); border-top: 1px solid var(--card-border);
}
.nav-item {
  appearance: none; background: none; border: 0; cursor: pointer; position: relative;
  display: flex; flex-direction: column; align-items: center; gap: 5px;
  padding: 8px 0 4px; color: var(--muted);
  font-family: var(--font-ui); font-size: 0.6rem; font-weight: 500; white-space: nowrap;
}
.nav-item svg { width: 24px; height: 24px; stroke: currentColor; fill: none; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
.nav-item.active { color: #fff; }
.nav-item.active svg { stroke: var(--lime); }
.nav-item.active::before {
  content: ""; position: absolute; top: -11px; left: 22%; right: 22%; height: 3px;
  background: var(--lime); border-radius: 0 0 3px 3px;
}

@media (max-width: 480px) {
  h1 { font-size: 2rem; }
  .brand-name { font-size: 1.15rem; }
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
        <polyline points="{points}" fill="none" stroke="#b9e63a" stroke-width="2" />
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
        <polygon points="{area}" fill="rgba(185, 230, 58, 0.22)" />
        <polyline points="{points}" fill="none" stroke="#b9e63a" stroke-width="2.5" />
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
    <a class="card" href="{YIELDZ_URL}" target="_blank" rel="noopener" data-yz-id="{m.get('id') or ''}" data-yz-lltv="{m.get('lltv_pct') or 0}" data-yz-deposit="{m.get('deposit_apy_pct') or ''}" data-yz-borrow="{m.get('borrow_apy_pct') or ''}">
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


def render_tangent_card(p: dict) -> str:
    sid = (p.get("id") or "")[:10]
    vapr = p.get("vapr")
    max_lev = p.get("max_lev_vapr")
    return f"""
    <a class="card" href="{TANGENT_URL}" target="_blank" rel="noopener" data-tg-id="{p.get('id') or ''}" data-tg-ltv="{p.get('ltv') or 0}">
      <p class="card-title">{p['name']}</p>
      <p class="card-sub">Tangent &middot; HEC &middot; Ethereum</p>
      <div class="stat-row">
        <div class="stat">
          <p class="stat-label">vAPR</p>
          <p class="stat-value apy"><span id="tg-{sid}-vapr">{vapr if vapr is not None else '—'}</span>{'%' if vapr is not None else ''}</p>
        </div>
        <div class="stat">
          <p class="stat-label">Max lev. vAPR</p>
          <p class="stat-value apy"><span id="tg-{sid}-max">{max_lev if max_lev is not None else '—'}</span>{'%' if max_lev is not None else ''}</p>
        </div>
      </div>
      <div class="stat-row">
        <div class="stat">
          <p class="stat-label">Borrow APY</p>
          <p class="stat-value"><span id="tg-{sid}-bor">{p.get('borrow_apy', 0)}</span>%</p>
        </div>
        <div class="stat">
          <p class="stat-label">TVL</p>
          <p class="stat-value">$<span id="tg-{sid}-tvl">{p.get('tvl_usd', 0):,.0f}</span></p>
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


# Pestaña del menu inferior a la que pertenece cada seccion.
SECTION_TABS = {
    # FX Pool: etiquetas de f(x) Protocol
    "f(x) Protocol": "pool",
    # Loop fxSAVE
    "YIELDZ": "loop",
    "SpiralStake": "loop",
    "Lagoon": "loop",
    # Todo lo demas (Morpho, Tangent, Pendle, Curve, Convex, Concentrator...) -> Yield
}


def render_section(title: str, html: str) -> str:
    tab = SECTION_TABS.get(title, "yield")
    return f"""
    <section data-tab="{tab}">
      <p class="section-title">{title}</p>
      <div class="grid">
        {html}
      </div>
    </section>
    """


# Menu inferior: cambia de pestaña sin recargar. Los bloques de la pagina llevan
# data-tab="mcap|funding|pool|loop|yield"; solo se ve el de la pestaña activa.
TABS_JS = """
(function() {
  var TABS = ['mcap', 'funding', 'xs', 'pool', 'loop', 'yield'];
  var nodes = document.querySelectorAll('[data-tab]');
  var btns = document.querySelectorAll('.nav-item');
  function show(name, scroll) {
    if (TABS.indexOf(name) < 0) name = 'mcap';
    Array.prototype.forEach.call(nodes, function(n) {
      n.style.display = (n.getAttribute('data-tab') === name) ? '' : 'none';
    });
    Array.prototype.forEach.call(btns, function(b) {
      b.classList.toggle('active', b.getAttribute('data-go') === name);
    });
    if (scroll) window.scrollTo(0, 0);
    try { history.replaceState(null, '', '#' + name); } catch (e) {}
  }
  Array.prototype.forEach.call(btns, function(b) {
    b.addEventListener('click', function() { show(b.getAttribute('data-go'), true); });
  });
  show((location.hash || '').replace('#', '') || 'mcap', false);
})();
"""

NAV_ICONS = {
    "mcap": '<svg viewBox="0 0 24 24"><path d="M3 3v18h18"/><path d="M7 15l4-5 3 3 5-7"/></svg>',
    "funding": '<svg viewBox="0 0 24 24"><path d="M13 2L4 14h7l-1 8 9-12h-7l1-8z"/></svg>',
    "xs": '<svg viewBox="0 0 24 24"><path d="M3 12h4l3-8 4 16 3-8h4"/></svg>',
    "pool": '<svg viewBox="0 0 24 24"><path d="M12 3l9 5-9 5-9-5 9-5z"/><path d="M3 13l9 5 9-5"/></svg>',
    "loop": '<svg viewBox="0 0 24 24"><path d="M17 2l4 4-4 4"/><path d="M3 11V9a3 3 0 0 1 3-3h15"/><path d="M7 22l-4-4 4-4"/><path d="M21 13v2a3 3 0 0 1-3 3H3"/></svg>',
    "yield": '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6"/><circle cx="9.5" cy="9.5" r="1"/><circle cx="14.5" cy="14.5" r="1"/></svg>',
}
NAV_LABELS = [("mcap", "MCAP"), ("funding", "Funding"), ("xs", "X&amp;S Live"), ("pool", "FX Pool"), ("loop", "Loop fxSAVE"), ("yield", "Yield")]
# Titulo + descripcion corta de cada pestaña (solo se ve el de la pestaña activa).
TAB_HEADS = [
    ("mcap", "Dashboard", "fxUSD &amp; fxSAVE: pools, yields, funding and peg, live."),
    ("funding", "Funding", "Live Stability Pool funding status and current rate."),
    ("xs", "X&amp;S Live", "Latest xPosition and sPosition orders above $10k, with 24h and 7-day volume."),
    ("pool", "FX Pool", "f(x) Protocol pools and their current yields."),
    ("loop", "Loop fxSAVE", "Leveraged fxSAVE strategies: max APY and borrow cost."),
    ("yield", "Yield", "More ways to earn on fxUSD and fxSAVE."),
]
TAB_HEADS_HTML = "".join(
    f'<div data-tab="{k}"><h1>{title}</h1><p class="subtitle">{desc}</p></div>'
    for k, title, desc in TAB_HEADS
)
BOTTOM_NAV_HTML = (
    '<nav class="bottom-nav">'
    + "".join(
        f'<button class="nav-item" data-go="{k}">{NAV_ICONS[k]}<span>{label}</span></button>'
        for k, label in NAV_LABELS
    )
    + "</nav>"
)


FUNDING_REFRESH_MS = 5000  # se actualiza al entrar y despues cada 5 s

# Se ejecuta en el navegador: lee los balances del contrato del Stability Pool
# en vivo (Blockscout, con Etherscan como respaldo) y recalcula ON/OFF.
FUNDING_JS = """
(function() {
  var ADDR = '__ADDR__', USDC = '__USDC__', FXUSD = '__FXUSD__', KEY = '__KEY__', THR = __THR__;
  var CFG = '__CFG__', POOL = '__POOL__', SEL = '__SEL__';
  var fundingOn = null; // null = aun no sabemos; true/false tras leer el pool
  function el(id) { return document.getElementById(id); }
  function setText(id, t) { var e = el(id); if (e) e.textContent = t; }
  function money(n) { if (n == null || isNaN(n)) return '$0'; return '$' + Math.round(n).toLocaleString('en-US'); }

  // Parse large token balances without losing precision (fxUSD raw value is ~26 digits)
  function parseTokenBalance(valueStr, decimals) {
    if (!valueStr || valueStr === '0') return 0;
    try {
      // Clean the string in case of extra characters
      var clean = String(valueStr).replace(/[^0-9]/g, '');
      if (!clean) return 0;
      var raw = BigInt(clean);
      var divisor = BigInt(10) ** BigInt(decimals);
      var intPart = raw / divisor;
      var fracPart = raw % divisor;
      var fracStr = fracPart.toString().padStart(decimals, '0').slice(0, 6);
      var result = Number(intPart) + Number('0.' + fracStr);
      return isNaN(result) ? 0 : result;
    } catch (e) {
      var fallback = parseFloat(valueStr) / Math.pow(10, decimals);
      return isNaN(fallback) ? 0 : fallback;
    }
  }

  function fromBlockscout() {
    return fetch('https://eth.blockscout.com/api/v2/addresses/' + ADDR + '/token-balances')
      .then(function(r) { if (!r.ok) throw new Error('blockscout ' + r.status); return r.json(); })
      .then(function(rows) {
        var u = null, f = null;
        rows.forEach(function(row) {
          var tok = row.token || {};
          var a = String(tok.address_hash || tok.address || '').toLowerCase();
          var decimals = parseInt(tok.decimals || '0', 10);
          var v = parseTokenBalance(row.value, decimals);
          if (a === USDC.toLowerCase()) u = v;
          else if (a === FXUSD.toLowerCase()) f = v;
        });
        if (f === null || isNaN(f)) throw new Error('fxUSD not found');
        return { usdc: u || 0, fxusd: f };
      });
  }

  function etherscanBal(contract, dec) {
    return fetch('https://api.etherscan.io/v2/api?chainid=1&module=account&action=tokenbalance'
      + '&contractaddress=' + contract + '&address=' + ADDR + '&tag=latest&apikey=' + KEY)
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (!d || !d.result || d.result === '0' && d.message !== 'OK') throw new Error('etherscan no data');
        var v = parseTokenBalance(d.result, dec);
        if (isNaN(v)) throw new Error('bad parse');
        return v;
      });
  }

  function fromEtherscan() {
    return Promise.all([etherscanBal(USDC, 6), etherscanBal(FXUSD, 18)])
      .then(function(a) {
        if (!a[1] || a[1] <= 0) throw new Error('fxUSD zero');
        return { usdc: a[0] || 0, fxusd: a[1] };
      });
  }

  function render(b) {
    // Ignore bad / incomplete responses so the UI doesn't flash wrong state
    if (!b || isNaN(b.fxusd) || isNaN(b.usdc) || b.fxusd <= 0 || b.usdc < 0) {
      return; // keep previous values on screen
    }
    // Extra guard: if USDC is exactly 0 but we previously had a real value, skip
    var usdcEl = document.getElementById('fd-usdc');
    var prevText = usdcEl ? usdcEl.textContent : '';
    var prevUsdc = parseFloat(prevText.replace(/[^0-9.]/g, '')) || 0;
    if (b.usdc === 0 && prevUsdc > 1000) {
      return; // don't overwrite a real USDC balance with zero
    }
    var t = THR / 100, total = b.usdc + b.fxusd;
    var share = total > 0 ? b.usdc / total * 100 : 0;
    var needed = b.fxusd * t / (1 - t);
    var on = share < THR;
    fundingOn = on;
    var tag = el('fd-tag');
    if (tag) { tag.textContent = on ? 'FUNDING ON' : 'FUNDING OFF'; tag.className = 'funding-tag ' + (on ? 'funding-on' : 'funding-off'); }
    setText('fd-usdc', money(b.usdc));
    setText('fd-fxusd', money(b.fxusd));
    setText('fd-share', share.toFixed(2) + '%');
    setText('fd-gap-label', on ? 'USDC missing' : 'USDC above minimum');
    setText('fd-gap-usdc', money(on ? Math.max(needed - b.usdc, 0) : Math.max(b.usdc - needed, 0)));
    var g = el('fd-gap-usdc'); if (g) g.className = 'stat-value' + (on ? '' : ' apy');

    // Funding rate: valor del contrato cuando esta ON, 0% cuando esta OFF
    if (on) {
      fetchContractRate();
    } else {
      setText('fd-rate', '0%');
    }
  }

  // Rate exacto: se lee del propio contrato (PoolConfiguration.getLongPoolFundingRatio),
  // que ya incluye el escalar de gobernanza x tipo medio de Aave. Devuelve una tasa
  // anual x 1e18; se divide entre 1095 (365 dias x 3) para mostrarla por periodo de 8 horas,
  // que es la escala que usa la web de f(x).
  function fetchContractRate() {
    if (fundingOn !== true) return; // OFF -> 0%, no hace falta consultar
    var data = SEL + POOL.toLowerCase().replace('0x', '').padStart(64, '0');
    fetch('https://api.etherscan.io/v2/api?chainid=1&module=proxy&action=eth_call'
      + '&to=' + CFG + '&data=' + data + '&tag=latest&apikey=' + KEY)
      .then(function(r) { return r.json(); })
      .then(function(d) {
        var res = d && d.result;
        if (typeof res !== 'string' || !/^0x[0-9a-fA-F]+$/.test(res)) { return; }
        var annualPct = Number(BigInt(res) / BigInt(10) ** BigInt(12)) / 1e6 * 100; // % anual
        if (fundingOn === true) setText('fd-rate', (annualPct / 1095).toFixed(4) + '%');
      })
      .catch(function() { /* sin datos: se deja el valor anterior */ });
  }

  function refresh() {
    // Only Etherscan — Blockscout is often stale for fxUSD
    fromEtherscan().then(render).catch(function() {
      // On failure keep the last good values on screen
    });
  }

  refresh();
  setInterval(refresh, __INTERVAL__);
})();
"""


def render_funding_box(f: dict | None) -> str:
    thr = (f or {}).get("threshold_pct", FUNDING_USDC_THRESHOLD_PCT)
    if f:
        on = f["funding_on"]
        tag_cls, tag_txt = ("funding-on", "FUNDING ON") if on else ("funding-off", "FUNDING OFF")
        usdc, fxusd = f"${f['usdc']:,.0f}", f"${f['fxusd']:,.0f}"
        share = f"{f['usdc_share_pct']:.2f}%"
        if on:
            gap_label, gap_usdc = "USDC missing", f"${f['usdc_missing']:,.0f}"
        else:
            gap_label, gap_usdc = "USDC above minimum", f"${f['usdc_surplus']:,.0f}"
        val_cls = "" if on else " apy"
    else:
        tag_cls, tag_txt = "", "LOADING..."
        usdc = fxusd = share = gap_usdc = "..."
        gap_label, val_cls = "USDC missing", ""
    rate_init = "0%" if (f and not f["funding_on"]) else "..."
    return f"""
    <div class="funding-box">
      <div class="funding-head">
        <p class="funding-title">Stability Pool Funding</p>
        <span class="funding-tag {tag_cls}" id="fd-tag">{tag_txt}</span>
      </div>
      <div class="funding-stats">
        <div class="stat">
          <p class="stat-label">USDC in pool</p>
          <p class="stat-value" id="fd-usdc">...</p>
        </div>
        <div class="stat">
          <p class="stat-label">fxUSD in pool</p>
          <p class="stat-value" id="fd-fxusd">...</p>
        </div>
        <div class="stat">
          <p class="stat-label">% USDC (threshold {thr:g}%)</p>
          <p class="stat-value" id="fd-share">...</p>
        </div>
        <div class="stat">
          <p class="stat-label" id="fd-gap-label">{gap_label}</p>
          <p class="stat-value{val_cls}" id="fd-gap-usdc">...</p>
        </div>
        <div class="stat" id="fd-rate-box">
          <p class="stat-label">Rate</p>
          <p class="stat-value" id="fd-rate">{rate_init}</p>
        </div>
      </div>
    </div>
    """


# ---------------------------------------------------------------------------
# X&S Live: ordenes grandes de xPosition (pools largos) y sPosition (pools cortos)
# ---------------------------------------------------------------------------
XS_MIN_USD = 10_000          # solo se listan ordenes de mas de este valor
XS_REFRESH_MS = 15_000       # refresco mientras la pestaña esta abierta
XS_OPERATE_TOPIC = "0x9a243f0f02273a4b80be965697988a178f95cd11863de2122e69f811445dff44"  # Operate(address,uint256,int256,int256,uint256)
XS_LONG_MANAGER = "0x250893CA4Ba5d05626C785e8da758026928FCD24"   # PoolManager (xPosition)
XS_SHORT_MANAGER = "0xaCDc0AB51178d0Ae8F70c1EAd7d3cF5421FDd66D"  # ShortPoolManager (sPosition)
XS_POOLS = [
    # asset, side, manager, pool
    ("wBTC", "x", XS_LONG_MANAGER, "0xAB709e26Fa6B0A30c119D8c55B887DeD24952473"),
    ("wBTC", "s", XS_SHORT_MANAGER, "0xA0cC8162c523998856D59065fAa254F87D20A5b0"),
    ("wstETH", "x", XS_LONG_MANAGER, "0x6Ecfa38FeE8a5277B91eFdA204c235814F0122E8"),
    ("wstETH", "s", XS_SHORT_MANAGER, "0x25707b9e6690B52C60aE6744d711cf9C1dFC1876"),
]
XS_ASSETS = {
    "wBTC": {"dec": 8, "token": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"},
    "wstETH": {"dec": 18, "token": "0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0"},
}

XS_CSS = """
html, body { overflow-x: hidden; }
.xs-status { font-size: 0.8rem; font-weight: 600; color: var(--deep); margin: 0 0 14px; display: flex; align-items: center; gap: 8px; }
.xs-card { background: var(--card); border: 1px solid var(--card-border); border-radius: 16px; padding: 16px; margin-bottom: 14px; }
.xs-info { background: rgba(6,78,6,0.12); border: 1px solid rgba(6,78,6,0.35); border-radius: 12px; padding: 10px 12px; margin: 0 0 14px; font-size: 0.8rem; line-height: 1.4; color: var(--deep); }
#xs-tab .funding-note { color: var(--deep); font-size: 0.78rem; }
#xs-tab .funding-note a { color: var(--deep); }
.xs-card h3 { margin: 0 0 10px; font-size: 1rem; color: #fff; }
.xs-win { font-size: 0.7rem; letter-spacing: 0.12em; text-transform: uppercase; color: var(--accent-text); margin: 12px 0 6px; font-weight: 700; }
.xs-row { display: flex; align-items: center; gap: 8px; padding: 4px 0; font-size: 0.86rem; color: #fff; }
.xs-row .xs-v { margin-left: auto; font-family: var(--font-mono, 'JetBrains Mono', monospace); }
.xs-sep { height: 1px; background: var(--card-border); margin: 4px 0; }
.xs-total { display: flex; padding-top: 6px; font-weight: 700; font-size: 0.9rem; color: #fff; }
.xs-total .xs-v { margin-left: auto; font-family: var(--font-mono, 'JetBrains Mono', monospace); color: var(--lime); }
.xs-dot { width: 9px; height: 9px; border-radius: 50%; flex: none; }
.xs-g { background: var(--lime); } .xs-r { background: #ff6b6b; }
.xs-feed-title { font-size: 0.8rem; color: var(--deep); text-transform: uppercase; letter-spacing: 0.14em; margin: 18px 0 10px; font-weight: 700; }
.xs-item { background: var(--card); border: 1px solid var(--card-border); border-radius: 14px; padding: 12px 14px; margin-bottom: 10px; }
.xs-item.xs-new { border-color: var(--lime); }
.xs-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.xs-pill { font-size: 0.74rem; font-weight: 700; padding: 3px 10px; border-radius: 8px; letter-spacing: 0.02em; }
.xs-pill.xs-g { background: rgba(185,230,58,0.15); color: var(--lime); }
.xs-pill.xs-r { background: rgba(255,99,99,0.18); color: #ff7b7b; }
.xs-usd { font-family: var(--font-mono, 'JetBrains Mono', monospace); font-weight: 700; color: #fff; }
.xs-sub { font-size: 0.8rem; color: var(--muted); margin: 6px 0 2px; }
.xs-meta { font-size: 0.72rem; color: var(--muted); display: flex; flex-wrap: wrap; gap: 4px 12px; margin-top: 4px; }
.xs-meta a { color: var(--accent-text); text-decoration: none; }
.xs-empty { color: var(--deep); font-size: 0.85rem; padding: 10px 0; }
"""

XS_JS = """
(function() {
  var KEY = '__KEY__', MIN_USD = __MIN__, TOPIC0 = '__TOPIC0__', REFRESH = __REFRESH__;
  var POOLS = __POOLS__, ASSETS = __ASSETS__;
  var WEEK = 7 * 86400, DAY = 86400, MAX_FEED = 5;
  var events = {}, lastBlock = 0, started = false, busy = false, newKeys = {}, froms = {}, fromQueued = {};
  var prices = {};
  var chain = Promise.resolve();
  function delay(ms) { return new Promise(function(r) { setTimeout(r, ms); }); }
  function call(url, tries) {
    return fetch(url).then(function(r) { return r.json(); }).then(function(j) {
      var txt = String(j.result || '') + ' ' + String(j.message || '');
      if (/rate limit/i.test(txt) && tries < 4) return delay(1200).then(function() { return call(url, tries + 1); });
      return j;
    });
  }
  // Cola global: maximo ~2.6 llamadas/s para respetar el limite de Etherscan (3/s).
  function es(qs) {
    var url = 'https://api.etherscan.io/v2/api?chainid=1&' + qs + '&apikey=' + KEY;
    var p = chain.then(function() { return delay(380); }).then(function() { return call(url, 0); });
    chain = p.catch(function() {});
    return p;
  }
  function pad(a) { return '0x' + '0'.repeat(24) + a.toLowerCase().replace('0x', ''); }
  function int256(hex) {
    var v = BigInt('0x' + hex);
    return v >= (BigInt(1) << BigInt(255)) ? v - (BigInt(1) << BigInt(256)) : v;
  }
  function fetchPool(p, fromBlock, page, acc) {
    var qs = 'module=logs&action=getLogs&address=' + p.mgr + '&fromBlock=' + fromBlock + '&toBlock=latest'
      + '&topic0=' + TOPIC0 + '&topic0_1_opr=and&topic1=' + pad(p.pool) + '&page=' + page + '&offset=1000';
    return es(qs).then(function(j) {
      var rows = Array.isArray(j.result) ? j.result : [];
      acc = acc.concat(rows);
      if (rows.length >= 1000 && page < 5) return fetchPool(p, fromBlock, page + 1, acc);
      return acc;
    });
  }
  function ingest(p, rows, markNew) {
    var dec = ASSETS[p.asset].dec, added = 0;
    rows.forEach(function(r) {
      var d = r.data.replace('0x', '');
      var dColl = int256(d.slice(0, 64)), dDebt = int256(d.slice(64, 128));
      var delta = p.side === 'x' ? dColl : dDebt, amt;
      // xPosition solo con cambio de deuda (mint/repay de fxUSD sin tocar colateral): se pasa a unidades del activo con el precio.
      var debtOnly = p.side === 'x' && dColl === BigInt(0) && dDebt !== BigInt(0);
      if (delta === BigInt(0) && !debtOnly) return;
      var key = r.transactionHash + ':' + r.logIndex;
      if (events[key]) return;
      if (debtOnly) {
        if (!prices[p.asset]) return;
        amt = (Number(dDebt) / 1e18) / prices[p.asset];
      } else {
        amt = Number(delta) / Math.pow(10, dec);
      }
      events[key] = { key: key, asset: p.asset, side: p.side, open: amt > 0, amt: Math.abs(amt),
        ts: parseInt(r.timeStamp, 16), tx: r.transactionHash, pos: BigInt(r.topics[2]).toString() };
      if (markNew) newKeys[key] = true;
      added++;
    });
    return added;
  }
  function loadPrices() {
    var ids = Object.keys(ASSETS).map(function(a) { return 'ethereum:' + ASSETS[a].token; });
    return fetch('https://coins.llama.fi/prices/current/' + ids.join(',')).then(function(r) { return r.json(); }).then(function(j) {
      Object.keys(ASSETS).forEach(function(a) {
        var c = j.coins && j.coins['ethereum:' + ASSETS[a].token];
        if (c && c.price) prices[a] = c.price;
      });
    }).catch(function() {});
  }
  function fmt(n, d) { return n.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }); }
  function kindOf(e) { return e.side === 'x' ? (e.open ? 'Open xPosition' : 'Close xPosition') : (e.open ? 'Open sPosition' : 'Close sPosition'); }
  function good(e) { return e.side === 'x' ? e.open : !e.open; } // x abierta / s cerrada = verde
  function statsHtml() {
    var now = Math.floor(Date.now() / 1000), html = '';
    Object.keys(ASSETS).forEach(function(a) {
      var out = '<div class="xs-card"><h3>' + a + ' Volume Report</h3>';
      [['24-Hour Volume', DAY], ['7-Day Volume', WEEK]].forEach(function(w) {
        var s = { ox: 0, cx: 0, os: 0, cs: 0 };
        Object.keys(events).forEach(function(k) {
          var e = events[k];
          if (e.asset !== a || now - e.ts > w[1]) return;
          s[(e.open ? 'o' : 'c') + e.side] += e.amt;
        });
        function row(c, label, v) { return '<div class="xs-row"><span class="xs-dot ' + c + '"></span>' + label + '<span class="xs-v">' + fmt(v, 2) + ' ' + a + '</span></div>'; }
        out += '<div class="xs-win">' + w[0] + '</div>'
          + row('xs-g', 'Open xPosition', s.ox) + row('xs-r', 'Close xPosition', s.cx)
          + '<div class="xs-sep"></div>'
          + row('xs-r', 'Open sPosition', s.os) + row('xs-g', 'Close sPosition', s.cs)
          + '<div class="xs-total">Total Volume<span class="xs-v">' + fmt(s.ox + s.cx + s.os + s.cs, 2) + ' ' + a + '</span></div>';
      });
      html += out + '</div>';
    });
    return html;
  }
  function feedHtml() {
    var list = Object.keys(events).map(function(k) { return events[k]; }).filter(function(e) {
      return prices[e.asset] && e.amt * prices[e.asset] >= MIN_USD;
    }).sort(function(a, b) { return b.ts - a.ts; }).slice(0, MAX_FEED);
    if (!list.length) return '<p class="xs-empty">No orders above $' + fmt(MIN_USD, 0) + ' in the last 7 days.</p>';
    list.forEach(queueFrom);
    return list.map(function(e) {
      var usd = e.amt * prices[e.asset], d = new Date(e.ts * 1000).toISOString();
      var f = froms[e.tx];
      var addr = f ? '<a href="https://etherscan.io/address/' + f + '" target="_blank" rel="noopener">' + f.slice(0, 6) + '…' + f.slice(-4) + '</a>' : '…';
      return '<div class="xs-item' + (newKeys[e.key] ? ' xs-new' : '') + '"><div class="xs-top">'
        + '<span class="xs-pill ' + (good(e) ? 'xs-g' : 'xs-r') + '">' + kindOf(e) + '</span>'
        + '<span class="xs-usd">$' + fmt(usd, 2) + '</span></div>'
        + '<div class="xs-sub">' + fmt(e.amt, 2) + ' ' + e.asset + ' · Price $' + fmt(prices[e.asset], 2) + '</div>'
        + '<div class="xs-meta"><span>' + addr + '</span><span>Position #' + e.pos + '</span><span>' + d.slice(5, 10) + ' ' + d.slice(11, 19) + ' UTC</span>'
        + '<a href="https://etherscan.io/tx/' + e.tx + '" target="_blank" rel="noopener">Etherscan</a></div></div>';
    }).join('');
  }
  function queueFrom(e) {
    if (froms[e.tx] || fromQueued[e.tx]) return;
    fromQueued[e.tx] = true;
    es('module=proxy&action=eth_getTransactionByHash&txhash=' + e.tx).then(function(j) {
      var f = j && j.result && j.result.from;
      if (f && /^0x[0-9a-fA-F]{40}$/.test(f)) { froms[e.tx] = f; render(); }
    }).catch(function() { delete fromQueued[e.tx]; });
  }
  function render() {
    var s = document.getElementById('xs-stats'), f = document.getElementById('xs-feed');
    if (s) s.innerHTML = statsHtml();
    if (f) f.innerHTML = feedHtml();
  }
  function status(msg) { var el = document.getElementById('xs-status-text'); if (el) el.textContent = msg; }
  function cycle() {
    if (busy) return Promise.resolve();
    busy = true;
    return loadPrices().then(function() { return es('module=proxy&action=eth_blockNumber'); }).then(function(j) {
      var head = parseInt(j.result, 16);
      if (!head) throw new Error('block');
      var first = !lastBlock, from = first ? head - 50400 : lastBlock + 1;
      var jobs = POOLS.reduce(function(pr, p) {
        return pr.then(function() { return fetchPool(p, from, 1, []); }).then(function(rows) { ingest(p, rows, !first); });
      }, Promise.resolve());
      return jobs.then(function() { lastBlock = head; });
    }).then(function() {
      var now = Math.floor(Date.now() / 1000);
      Object.keys(events).forEach(function(k) { if (now - events[k].ts > WEEK) delete events[k]; });
      render();
      status('Live · updated ' + new Date().toISOString().slice(11, 19) + ' UTC');
    }).catch(function() { status('Could not load on-chain data, retrying…'); })
      .then(function() { busy = false; });
  }
  function active() { var n = document.getElementById('xs-tab'); return n && n.style.display !== 'none'; }
  function start() { if (!started) { started = true; cycle(); } else { cycle(); } }
  document.addEventListener('click', function(ev) {
    var t = ev.target && ev.target.closest ? ev.target.closest('[data-go="xs"]') : null;
    if (t) start();
  });
  if ((location.hash || '') === '#xs') start();
  setInterval(function() { if (started && active() && !document.hidden) cycle(); }, REFRESH);
})();
"""


def render_xs_tab() -> tuple[str, str]:
    """Devuelve (html, js) de la pestaña X&S Live."""
    html = (
        '<div class="xs-status"><span class="live-dot"></span><span id="xs-status-text">Loading live orders…</span></div>'
        '<p class="xs-info"><b>Note:</b> minting fxUSD counts as xPosition. Mints that only change the debt (no collateral deposit) are included too, converted to wBTC / wstETH at spot price.</p>'
        '<div id="xs-stats"></div>'
        f'<p class="xs-feed-title">Last 5 orders above ${XS_MIN_USD:,.0f}</p>'
        '<div id="xs-feed"><p class="xs-empty">Loading…</p></div>'
        '<p class="funding-note">xPosition = long pools (wBTC, wstETH); sPosition = short pools. '
        'Open/Close is the sign of the change in each on-chain Operate event: collateral for xPosition (minting fxUSD counts as xPosition, including debt-only mints, converted at spot) and debt for sPosition; '
        'USD value uses the current spot price. Volume in wBTC / wstETH. '
        '<b>Why volumes may differ from other trackers:</b> every on-chain change counts as an order, including partial collateral adds/withdrawals and debt-only fxUSD mints/repayments, so Close xPosition and totals can be higher than reports that only count full opens and closes.</p>'
    )
    pools = [{"asset": a, "side": s, "mgr": m, "pool": p} for a, s, m, p in XS_POOLS]
    js = (
        XS_JS
        .replace("__KEY__", ETHERSCAN_API_KEY)
        .replace("__MIN__", str(XS_MIN_USD))
        .replace("__TOPIC0__", XS_OPERATE_TOPIC)
        .replace("__REFRESH__", str(XS_REFRESH_MS))
        .replace("__POOLS__", json.dumps(pools))
        .replace("__ASSETS__", json.dumps(XS_ASSETS))
    )
    return html, js


CARD_CSS = CARD_CSS + XS_CSS


# ---------------------------------------------------------------------------
# Historial del Rate: medias por dia / mes / año (empieza a guardar desde que se despliega)
# ---------------------------------------------------------------------------
# Para que NO se borre en cada deploy hay que guardarlo en un Volume de Railway:
# crea un Volume montado en /data y define la variable DATA_DIR=/data.
DATA_DIR = os.environ.get("DATA_DIR", "").strip()
RATE_HISTORY_FILE = os.path.join(DATA_DIR or ".", "rate_history.json")
RATE_PERIODS_PER_YEAR = 1095  # periodos de 8 h al año (365 x 3), igual que la web de f(x)


def fetch_current_rate(funding: dict | None) -> float | None:
    """Rate actual en % por 8 h (0 si el funding esta OFF). None si no se pudo leer."""
    if not funding:
        return None
    if not funding.get("funding_on"):
        return 0.0
    try:
        data = FUNDING_RATIO_SELECTOR + WSTETH_LONG_POOL_ADDRESS.lower().replace("0x", "").rjust(64, "0")
        r = requests.get(
            "https://api.etherscan.io/v2/api",
            params={
                "chainid": 1, "module": "proxy", "action": "eth_call",
                "to": POOL_CONFIGURATION_ADDRESS, "data": data, "tag": "latest",
                "apikey": ETHERSCAN_API_KEY,
            },
            timeout=30,
        )
        r.raise_for_status()
        res = r.json().get("result")
        if not isinstance(res, str) or not res.startswith("0x") or len(res) < 3:
            return None
        annual_pct = int(res, 16) / 1e18 * 100
        return round(annual_pct / RATE_PERIODS_PER_YEAR, 6)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not read funding rate: {exc}", file=sys.stderr)
        return None


def load_rate_history() -> list[dict]:
    try:
        with open(RATE_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def save_rate_history(history: list[dict]) -> None:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(RATE_HISTORY_FILE)), exist_ok=True)
        tmp = RATE_HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(history, f)
        os.replace(tmp, RATE_HISTORY_FILE)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not save rate history: {exc}", file=sys.stderr)


def record_rate_sample(history: list[dict], rate: float | None, now_ts: float | None = None) -> list[dict]:
    """Añade una muestra (como mucho una cada 5 minutos) y la guarda en disco."""
    if rate is None:
        return history
    now_ts = now_ts or datetime.now(timezone.utc).timestamp()
    if history and now_ts - history[-1].get("ts", 0) < 300:
        return history
    history = history + [{"ts": round(now_ts), "rate": rate}]
    save_rate_history(history)
    return history


def summarize_rate_history(history: list[dict]) -> dict[str, list[dict]]:
    """Medias por dia, mes y año (UTC). Cada fila: periodo, media, % de muestras con funding ON, muestras."""
    buckets: dict[str, dict[str, list[float]]] = {"day": {}, "month": {}, "year": {}}
    for s in history:
        try:
            d = datetime.fromtimestamp(float(s["ts"]), tz=timezone.utc)
            v = float(s["rate"])
        except Exception:  # noqa: BLE001
            continue
        for kind, key in (("day", d.strftime("%Y-%m-%d")), ("month", d.strftime("%Y-%m")), ("year", d.strftime("%Y"))):
            buckets[kind].setdefault(key, []).append(v)
    out: dict[str, list[dict]] = {}
    for kind, groups in buckets.items():
        rows = []
        for key in sorted(groups, reverse=True):
            vals = groups[key]
            avg = sum(vals) / len(vals)
            rows.append({
                "period": key, "avg": avg, "annual": avg * RATE_PERIODS_PER_YEAR,
                "on_pct": 100 * sum(1 for v in vals if v > 0) / len(vals), "n": len(vals),
            })
        out[kind] = rows
    return out


RH_CSS = """
.rh-box { background: var(--card); border: 1px solid var(--card-border); border-radius: 20px; padding: 1.1rem 1.2rem; margin-bottom: 1.5rem; }
.rh-tabs { display: flex; gap: 8px; margin: 10px 0 12px; }
.rh-tab { appearance: none; border: 1px solid var(--card-border); background: transparent; color: var(--muted); border-radius: 999px; padding: 5px 14px; font: inherit; font-size: 0.8rem; font-weight: 600; cursor: pointer; }
.rh-tab.active { background: var(--lime); color: var(--deep); border-color: var(--lime); }
.rh-table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
.rh-table th { text-align: right; font-size: 0.68rem; letter-spacing: 0.1em; text-transform: uppercase; color: var(--muted); padding: 6px 4px; border-bottom: 1px solid var(--card-border); }
.rh-table td { text-align: right; padding: 8px 4px; border-bottom: 1px solid rgba(255,255,255,0.08); color: #fff; font-family: var(--font-mono, 'JetBrains Mono', monospace); }
.rh-table th:first-child, .rh-table td:first-child { text-align: left; font-family: inherit; white-space: nowrap; }
.rh-table th { font-size: 0.6rem; letter-spacing: 0.06em; }
.rh-table td { font-size: 0.8rem; }
.rh-empty { color: var(--muted); font-size: 0.85rem; margin: 8px 0; }
"""
RH_JS = """
(function() {
  var tabs = document.querySelectorAll('.rh-tab'), panes = document.querySelectorAll('.rh-pane');
  Array.prototype.forEach.call(tabs, function(t) {
    t.addEventListener('click', function() {
      Array.prototype.forEach.call(tabs, function(x) { x.classList.toggle('active', x === t); });
      Array.prototype.forEach.call(panes, function(p) { p.style.display = (p.getAttribute('data-rh') === t.getAttribute('data-rh')) ? '' : 'none'; });
    });
  });
})();
"""
CARD_CSS = CARD_CSS + RH_CSS


def render_rate_history(history: list[dict]) -> str:
    summary = summarize_rate_history(history)
    limits = {"day": 30, "month": 24, "year": 10}
    labels = {"day": "Daily", "month": "Monthly", "year": "Yearly"}
    heads = {"day": "Day", "month": "Month", "year": "Year"}
    panes = []
    for kind in ("day", "month", "year"):
        rows = summary.get(kind, [])[: limits[kind]]
        if rows:
            body = "".join(
                f'<tr><td>{r["period"]}</td><td>{r["avg"]:.4f}%</td><td>{r["annual"]:.2f}%</td>'
                f'<td>{r["on_pct"]:.0f}%</td><td>{r["n"]}</td></tr>'
                for r in rows
            )
            inner = (
                f'<table class="rh-table"><thead><tr><th>{heads[kind]}</th><th>Avg rate</th><th>Annual</th>'
                f'<th>Time ON</th><th>Samples</th></tr></thead><tbody>{body}</tbody></table>'
            )
        else:
            inner = '<p class="rh-empty">No data yet.</p>'
        display = "" if kind == "day" else ' style="display:none"'
        panes.append(f'<div class="rh-pane" data-rh="{kind}"{display}>{inner}</div>')
    tabs = "".join(
        f'<button class="rh-tab{" active" if k == "day" else ""}" data-rh="{k}">{labels[k]}</button>'
        for k in ("day", "month", "year")
    )
    since = ""
    if history:
        first = datetime.fromtimestamp(float(history[0]["ts"]), tz=timezone.utc).strftime("%Y-%m-%d")
        since = f"Tracking since {first} (UTC), {len(history)} samples."
    storage = (
        ""
        if DATA_DIR
        else " <b>Storage is temporary:</b> set DATA_DIR to a Railway Volume path to keep this history across deploys."
    )
    return (
        '<div class="rh-box"><p class="funding-title">Rate history (average)</p>'
        f'<div class="rh-tabs">{tabs}</div>{"".join(panes)}'
        '<p class="funding-note">Average Rate per 8 h, same scale as the Rate above; funding OFF counts as 0%. '
        f'One sample each time the page is regenerated. {since}{storage}</p>'
        f'<script>{RH_JS}</script></div>'
    )


def render_html(
    market: dict | None,
    vault: dict | None,
    defillama: dict[str, list[dict]],
    fxusd_mcap: dict | None,
    fxsave_mcap: dict | None,
    fxsave_apy_data: dict | None = None,
    pendle_rockawayx: dict | None = None,
    fxusd_peg_history: list[float] | None = None,
    fxusd_mcap_history: list[dict] | None = None,
    compare_histories: dict[str, list[dict]] | None = None,
    yieldz_pools: list[dict] | None = None,
    tangent_pools: list[dict] | None = None,
    funding: dict | None = None,
    rate_history: list[dict] | None = None,
) -> str:
    funding_html = render_funding_box(funding) + render_rate_history(rate_history or [])
    xs_html, xs_js = render_xs_tab()
    funding_js = (
        FUNDING_JS
        .replace("__ADDR__", STABILITY_POOL_ADDRESS)
        .replace("__USDC__", USDC_CONTRACT)
        .replace("__FXUSD__", FXUSD_CONTRACT)
        .replace("__KEY__", ETHERSCAN_API_KEY)
        .replace("__THR__", f"{FUNDING_USDC_THRESHOLD_PCT:g}")
        .replace("__INTERVAL__", str(FUNDING_REFRESH_MS))
        .replace("__CFG__", POOL_CONFIGURATION_ADDRESS)
        .replace("__POOL__", WSTETH_LONG_POOL_ADDRESS)
        .replace("__SEL__", FUNDING_RATIO_SELECTOR)
    )
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
    # Use the known DeFiLlama Concentrator pool for fxSAVE, fallback to CoinGecko
    fxsave_tvl = 0
    for p in defillama.get("Concentrator", []):
        if p.get("pool") == FXSAVE_DEFILLAMA_POOL_ID:
            fxsave_tvl = p.get("tvl_usd") or 0
            break
    if not fxsave_tvl:
        fxsave_tvl = fxsave_mcap_val
    fxsave_apy_card = f"""
    <a class="card" href="https://fx.aladdin.club/v2/fxsave" target="_blank" rel="noopener">
      <p class="card-title">FXSAVE</p>
      <p class="card-sub">f(x) Protocol app &middot; APY calculado on-chain (24h)</p>
      <div class="stat-row">
        <div class="stat">
          <p class="stat-label">APY</p>
          <p class="stat-value apy">{apy_display} {fxsave_change_badge}</p>
        </div>
        <div class="stat">
          <p class="stat-label">TVL</p>
          <p class="stat-value">${fxsave_tvl:,.0f}</p>
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
            "".join(render_yieldz_card(p, fxsave_apy) for p in sorted((yieldz_pools or []), key=lambda x: x.get("max_apy_pct") or 0, reverse=True))
            or '<p class="empty">Sin pools activos en este momento.</p>',
        ),
        render_section(
            "TANGENT",
            "".join(render_tangent_card(p) for p in (tangent_pools or []))
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
        yt_apy = pendle_rockawayx.get("yt_apy")
        tvl = pendle_rockawayx.get("tvl") or 0
        pt_change_badge = render_apy_change_badge(pendle_rockawayx.get("pt_apy_change_24h"))
        lp_change_badge = render_apy_change_badge(pendle_rockawayx.get("lp_apy_change_24h"))
        yt_change_badge = render_apy_change_badge(pendle_rockawayx.get("yt_apy_change_24h"))
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
    <a class="card" href="{PENDLE_ROCKAWAYX_YT_URL}" target="_blank" rel="noopener">
      <p class="card-title">YT-fxSAVE</p>
      <p class="card-sub">f(x) USD Saving &middot; Pendle V2 &middot; Yield Token</p>
      <div class="stat-row">
        <div class="stat">
          <p class="stat-label">Underlying APY</p>
          <p class="stat-value apy">{yt_apy if yt_apy is not None else 'no disponible'}{'%' if yt_apy is not None else ''} {yt_change_badge}</p>
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

    # SpiralStake fxSAVE/USDC
    spiral = collect_spiral_fxsave()
    if spiral:
        spiral_html = f"""
        <a class="card" href="{spiral['url']}" target="_blank" rel="noopener">
          <p class="card-title">{spiral['name']}</p>
          <p class="card-sub">SpiralStake · {spiral['leverage']}x leverage</p>
          <div class="stat-row">
            <div class="stat">
              <p class="stat-label">Leveraged APY</p>
              <p class="stat-value apy">{spiral['apy']:.2f}%</p>
            </div>
            <div class="stat">
              <p class="stat-label">Base APY</p>
              <p class="stat-value">{spiral['base_apy']:.2f}%</p>
            </div>
          </div>
          <div class="stat-row">
            <div class="stat">
              <p class="stat-label">TVL Morpho</p>
              <p class="stat-value">${spiral['tvl']:,.0f}</p>
            </div>
            <div class="stat">
              <p class="stat-label">Available to Borrow</p>
              <p class="stat-value">${spiral['liquidity']:,.0f}</p>
            </div>
          </div>
        </a>
        """
        sections.append(render_section("SpiralStake", spiral_html))
    else:
        sections.append(render_section("SpiralStake", '<p class="empty">No se pudo obtener el dato de SpiralStake.</p>'))

    # Lagoon fxLOOP
    lagoon = collect_lagoon_fxloop()
    if lagoon:
        lagoon_html = f"""
        <a class="card" href="{lagoon['url']}" target="_blank" rel="noopener">
          <p class="card-title">{lagoon['symbol']}</p>
          <p class="card-sub">Lagoon · 9Summits · leveraged fxSAVE</p>
          <div class="stat-row">
            <div class="stat">
              <p class="stat-label">Net APR</p>
              <p class="stat-value apy">{lagoon['apy']:.2f}%</p>
            </div>
            <div class="stat">
              <p class="stat-label">TVL</p>
              <p class="stat-value">${lagoon['tvl']:,.0f}</p>
            </div>
          </div>
        </a>
        """
        sections.append(render_section("Lagoon", lagoon_html))
    else:
        sections.append(render_section("Lagoon", '<p class="empty">No se pudo obtener el dato de Lagoon.</p>'))

    CURVE_POOLS_URL = "https://www.curve.finance/#/ethereum/pools"
    CONVEX_STAKE_URL = "https://curve.convexfinance.com/stake"
    CONCENTRATOR_VAULT_URL = "https://concentrator.aladdin.club/#/vault"

    for label in ("Curve", "Convex", "Concentrator"):
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
  <title>fxusd.club</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@500;700&family=Space+Grotesk:wght@400;500;700&display=swap" rel="stylesheet" />
  <style>{CARD_CSS}</style>
</head>
<body>
  <header class="topbar">
    <div class="brand">
      <img src="IMG_2015.png" alt="FXN" />
      <div class="brand-text">
        <span class="brand-name">FXUSD<span>.club</span></span>
        <span class="brand-by"><img src="metafxn.jpg" alt="MetaFXN" />by MetaFXN</span>
      </div>
    </div>
    <span class="live-pill"><span class="live-dot"></span>LIVE</span>
  </header>

  <div class="wrap">

    {TAB_HEADS_HTML}

    <div data-tab="mcap">
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

    {mcap_chart_html}

    {peg_chart_html}

    {compare_chart_html}
    </div>

    <div data-tab="funding">
    {funding_html}
    </div>

    <div data-tab="xs" id="xs-tab">
    {xs_html}
    </div>

    {''.join(sections)}

  </div>


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

        Promise.all([
          fetch('https://yieldz.io/api/markets').then(function(r) {{ return r.json(); }}),
          fetch('https://yieldz.io/api/intrinsic-apy/history?asset_address=0x7743e50F534a7f9F1791DdE7dCD89F7783Eefc39&asset_symbol=fxSAVE&days=7').then(function(r) {{ return r.json(); }}).catch(function() {{ return null; }})
        ]).then(function(parts) {{
            var payload = parts[0] || {{}};
            var hist = parts[1] || {{}};
            var pts = hist.data || [];
            var depVals = pts.map(function(p) {{ return p.total_apy; }}).filter(function(v) {{ return typeof v === 'number'; }});
            var deposit7d = depVals.length ? depVals.reduce(function(a,b) {{ return a+b; }}, 0) / depVals.length : null;
            var rows = payload.data || [];
            rows.forEach(function(row) {{
              var card = document.querySelector('[data-yz-id="' + row.id + '"]');
              if (!card) return;
              var pdata = row.protocol_data || {{}};
              if (deposit7d != null) card.setAttribute('data-yz-deposit', String(deposit7d));
              if (typeof row.borrow_apy === 'number') card.setAttribute('data-yz-borrow', String(row.borrow_apy));
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

        fetch('https://api.tangent.finance/aprs')
          .then(function(r) {{ return r.json(); }})
          .then(function(rows) {{
            (rows || []).forEach(function(row) {{
              var id = row.marketAddress;
              if (!id) return;
              var card = document.querySelector('[data-tg-id="' + id + '"]');
              if (!card) return;
              var sid = String(id).slice(0, 10);
              var cur = row.currentAPR || {{}};
              var vapr = 0;
              Object.keys(cur).forEach(function(k) {{
                if (typeof cur[k] === 'number') vapr += cur[k];
              }});
              var vEl = document.getElementById('tg-' + sid + '-vapr');
              if (vEl) vEl.textContent = vapr.toFixed(2);
              var ltv = parseFloat(card.getAttribute('data-tg-ltv') || '0');
              var borEl = document.getElementById('tg-' + sid + '-bor');
              var borrow = borEl ? parseFloat(borEl.textContent) : 0;
              if (isNaN(borrow)) borrow = 0;
              var maxEl = document.getElementById('tg-' + sid + '-max');
              if (maxEl && ltv > 0 && ltv < 0.99) {{
                maxEl.textContent = ((vapr - borrow * ltv) / (1 - ltv)).toFixed(2);
              }}
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

    }})();
  </script>
  <script>{funding_js}</script>
  <script>{xs_js}</script>




  {BOTTOM_NAV_HTML}
  <script>{TABS_JS}</script>
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
    fxsave_mcap = collect_fxsave_mcap()
    fxsave_apy = collect_fxsave_apy()
    pendle_rockawayx = collect_pendle_fxsave_market()
    funding = collect_stability_pool_funding()
    rate_history = record_rate_sample(load_rate_history(), fetch_current_rate(funding))
    try:
        yieldz_pools = collect_yieldz_pools()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not fetch Yieldz pools: {exc}", file=sys.stderr)
        yieldz_pools = []
    try:
        tangent_pools = collect_tangent_pools()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not fetch Tangent pools: {exc}", file=sys.stderr)
        tangent_pools = []

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
    pendle_yt_change = pct_point_change(
        (pendle_rockawayx or {}).get("yt_apy"),
        (prev or {}).get("pendle_yt_apy") if prev else None,
    )

    if vault is not None:
        vault["apy_change_24h"] = vault_apy_change
    if pendle_rockawayx is not None:
        pendle_rockawayx["pt_apy_change_24h"] = pendle_pt_change
        pendle_rockawayx["lp_apy_change_24h"] = pendle_lp_change
        pendle_rockawayx["yt_apy_change_24h"] = pendle_yt_change

    history.append(
        {
            "ts": datetime.now(timezone.utc).timestamp(),
            "vault_apy": (vault or {}).get("net_apy_pct"),
            "pendle_pt_apy": (pendle_rockawayx or {}).get("pt_apy"),
            "pendle_lp_apy": (pendle_rockawayx or {}).get("lp_apy"),
            "pendle_yt_apy": (pendle_rockawayx or {}).get("yt_apy"),
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
                "stability_pool_funding": funding,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )

    html = render_html(market, vault, defillama, fxusd_mcap, fxsave_mcap, fxsave_apy, pendle_rockawayx, fxusd_peg_history, fxusd_mcap_history, compare_histories, yieldz_pools, tangent_pools, funding, rate_history)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    counts = {k: len(v) for k, v in defillama.items()}
    print(
        f"Listo: market={'ok' if market else 'missing'}, vault={'ok' if vault else 'missing'}, "
        f"fxusd_mcap={'ok' if fxusd_mcap else 'missing'}, fxsave_mcap={'ok' if fxsave_mcap else 'missing'}, "
        f"fxsave_apy={(fxsave_apy or {}).get('apy', 'missing')}, "
        f"pendle_pt_apy={(pendle_rockawayx or {}).get('pt_apy', 'missing')}, "
        f"pendle_lp_apy={(pendle_rockawayx or {}).get('lp_apy', 'missing')}, "
    )


if __name__ == "__main__":
    main()
