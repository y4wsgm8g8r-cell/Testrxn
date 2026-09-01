#!/usr/bin/env python3
"""
fetch_fx_pools.py

Live dashboard for fxSAVE / fxUSD, limited to exactly these sources:
    - Morpho: the direct fxSAVE/USDC market
    - Pendle: fxSAVE markets (PT/YT)
    - RockawayX: the curated "f(x) Protocol Ecosystem USDC" vault on Morpho
    - Curve: fxUSD/fxSAVE pools
    - Convex: fxUSD/fxSAVE pools (Curve pools boosted through Convex)

Data sources (both public, no API key needed):
    - DeFiLlama Yields API : https://yields.llama.fi/pools
      (covers Pendle, Curve, Convex -- each protocol reports into this feed)
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

TARGET_SYMBOLS = ["FXUSD", "FXSAVE", "FXN"]
DEFILLAMA_PROJECTS = {
    "pendle": "Pendle",
    "curve-dex": "Curve",
    "convex-finance": "Convex",
}

# The direct fxSAVE/USDC market on Morpho.
FX_MARKET_ID = "0x43e925e52d7873fa8acac90dd5f246087d55b3a34c344b71884a6352491ff459"

MARKET_QUERY = """
query FxMarket($id: String!) {
  marketByUniqueKey(uniqueKey: $id, chainId: 1) {
    uniqueKey
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
"""

VAULTS_QUERY = """
query RockawayXVaults {
  vaults(first: 1000, where: { chainId_in: [1] }) {
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


def touches_fx(text: str) -> bool:
    text = (text or "").upper()
    return any(sym in text for sym in TARGET_SYMBOLS)


# ---------------------------------------------------------------------------
# DeFiLlama: Pendle, Curve, Convex pools for fxUSD/fxSAVE/FXN
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
                "apy_reward_pct": round(p.get("apyReward") or 0, 2) if p.get("apyReward") else None,
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
    resp.raise_for_status()
    payload = resp.json()
    if "errors" in payload:
        raise RuntimeError(f"Morpho API returned errors: {payload['errors']}")
    return payload["data"]


def collect_direct_market() -> dict | None:
    data = graphql(MARKET_QUERY, {"id": FX_MARKET_ID})
    m = data.get("marketByUniqueKey")
    if not m:
        return None
    state = m.get("state") or {}
    return {
        "id": m["uniqueKey"],
        "loan": (m.get("loanAsset") or {}).get("symbol"),
        "collateral": (m.get("collateralAsset") or {}).get("symbol"),
        "lltv_pct": round(float(m.get("lltv") or 0) / 1e16, 2),
        "supply_apy_pct": round((state.get("netSupplyApy") or state.get("supplyApy") or 0) * 100, 2),
        "supply_usd": round(state.get("supplyAssetsUsd") or 0, 2),
        "liquidity_usd": round(state.get("liquidityAssetsUsd") or 0, 2),
        "utilization_pct": round((state.get("utilization") or 0) * 100, 2),
        "url": f"https://app.morpho.org/ethereum/market/{m['uniqueKey']}",
    }


def collect_rockawayx_vault() -> dict | None:
    data = graphql(VAULTS_QUERY)
    for v in data["vaults"]["items"]:
        name = v.get("name") or ""
        if "rockawayx" in name.lower() and touches_fx(name):
            state = v.get("state") or {}
            return {
                "address": v["address"],
                "name": name,
                "asset": (v.get("asset") or {}).get("symbol"),
                "total_assets_usd": round(state.get("totalAssetsUsd") or 0, 2),
                "net_apy_pct": round((state.get("netApy") or 0) * 100, 2),
                "url": f"https://app.morpho.org/ethereum/vault/{v['address']}",
            }
    return None


CARD_CSS = """
:root {
  color-scheme: dark;
  --bg: #0b0b0d;
  --card: #16161a;
  --card-border: #26262c;
  --text: #f2f2f0;
  --muted: #9a9aa2;
  --accent: #7f77dd;
  --accent-text: #d6d3fb;
  --green: #5dcaa5;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 2.5rem 1.25rem 4rem;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
.wrap { max-width: 900px; margin: 0 auto; }
h1 { font-size: 1.7rem; font-weight: 600; margin-bottom: 0.25rem; }
.subtitle { color: var(--muted); font-size: 0.95rem; margin-bottom: 2rem; }
.section-title {
  font-size: 1rem; font-weight: 600; margin: 2.25rem 0 0.75rem;
  color: var(--accent-text);
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
.card:hover { border-color: var(--accent); transform: translateY(-2px); }
.card-title { font-weight: 600; font-size: 0.95rem; margin: 0 0 2px; }
.card-sub { color: var(--muted); font-size: 0.8rem; margin: 0 0 10px; }
.stat-row { display: flex; gap: 10px; margin-top: 6px; }
.stat { flex: 1; background: #1e1e24; border-radius: 10px; padding: 8px 10px; }
.stat-label { font-size: 0.7rem; color: var(--muted); margin: 0; }
.stat-value { font-size: 1.15rem; font-weight: 600; margin: 2px 0 0; }
.apy { color: var(--green); }
.pill {
  display: inline-block; font-size: 0.7rem; background: #262138; color: var(--accent-text);
  padding: 2px 8px; border-radius: 6px; margin-top: 8px;
}
.empty { color: var(--muted); font-size: 0.9rem; padding: 1rem 0; }
footer { color: var(--muted); font-size: 0.75rem; margin-top: 3rem; text-align: center; }
footer a { color: var(--accent-text); }
"""


def render_defillama_card(p: dict) -> str:
    reward_note = ""
    if p["apy_reward_pct"]:
        reward_note = f' <span class="pill">incl. {p["apy_reward_pct"]}% rewards</span>'
    return f"""
    <a class="card" href="{p['url']}" target="_blank" rel="noopener">
      <p class="card-title">{p['symbol']}</p>
      <p class="card-sub">{p['chain']}</p>
      <div class="stat-row">
        <div class="stat">
          <p class="stat-label">APY</p>
          <p class="stat-value apy">{p['apy_pct']}%</p>
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


def render_html(market: dict | None, vault: dict | None, defillama: dict[str, list[dict]]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    market_html = render_market_card(market) if market else '<p class="empty">Mercado no disponible en este momento.</p>'
    vault_html = render_vault_card(vault) if vault else '<p class="empty">No se encontro el vault de RockawayX en este momento.</p>'

    sections = [
        render_section("Morpho -- mercado directo fxSAVE/USDC", market_html),
        render_section("Morpho -- vault RockawayX", vault_html),
    ]
    for label in ("Pendle", "Curve", "Convex"):
        pools = defillama.get(label, [])
        html = "".join(render_defillama_card(p) for p in pools) if pools else '<p class="empty">Sin pools activos en este momento.</p>'
        sections.append(render_section(label, html))

    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>fxSAVE / fxUSD en vivo</title>
  <style>{CARD_CSS}</style>
</head>
<body>
  <div class="wrap">
    <h1>fxSAVE / fxUSD en vivo</h1>
    <p class="subtitle">Morpho (mercado directo + vault RockawayX), Pendle, Curve y Convex. Ultima actualizacion: {now}</p>

    {''.join(sections)}

    <footer>
      Generado automaticamente con <code>fetch_fx_pools.py</code> &middot;
      Fuentes: <a href="https://yields.llama.fi/pools" target="_blank" rel="noopener">DeFiLlama Yields API</a>
      y <a href="https://api.morpho.org/graphql" target="_blank" rel="noopener">Morpho GraphQL API</a>.
      Verifica siempre las cifras antes de operar.
    </footer>
  </div>
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

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "morpho_direct_market": market,
                "morpho_rockawayx_vault": vault,
                "defillama": defillama,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )

    html = render_html(market, vault, defillama)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    counts = {k: len(v) for k, v in defillama.items()}
    print(f"Listo: market={'ok' if market else 'missing'}, vault={'ok' if vault else 'missing'}, defillama={counts}")


if __name__ == "__main__":
    main()
