"""
Bot direccional: detecta saltos de precio de BTC (Coinbase) y, cuando
supera el umbral, coloca UNA orden real en el mercado de ETH 5min de
Polymarket en la dirección del salto -- y la sostiene hasta que la vela
cierra (sin salida anticipada, tal como se pidió).

ARRANCA EN MODO SIMULACIÓN por defecto (SIMULATION_MODE=true). El
código de colocación de órdenes reales (create_order/post_order de
py-clob-client-v2) nunca pudo probarse contra la API en vivo en este
entorno de generación (sin acceso a red) -- por eso el default es
seguro. Para operar con plata real, hay que poner explícitamente
SIMULATION_MODE=false como variable de entorno en Railway.

Variables de entorno:
  SIMULATION_MODE       "true" (default) o "false"
  ORDER_SIZE_USD         Tamaño de cada orden en USD (default 2.0)
  POLYMARKET_PRIVATE_KEY Solo si SIMULATION_MODE=false
  POLYMARKET_FUNDER_ADDRESS Solo si SIMULATION_MODE=false

Requiere: pip install websockets requests
En real, además: pip install py-clob-client-v2 python-dotenv
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import time

import requests
import websockets

from direction_logic import JumpWithDirection

SCRIPT_VERSION = "v4-con-debug-periodico"

# ---------------- Reinicio automático (mismo truco que en el monitor) ----------------
def _handle_sigterm(signum, frame):
    print("[SIGTERM] Railway cortó el contenedor -- saliendo con código de "
          "error a propósito para forzar el reinicio automático.")
    sys.exit(1)

signal.signal(signal.SIGTERM, _handle_sigterm)

# ---------------- Configuración ----------------
SIMULATION_MODE = os.environ.get("SIMULATION_MODE", "true").lower() != "false"
ORDER_SIZE_USD = float(os.environ.get("ORDER_SIZE_USD", "2.0"))
JUMP_THRESHOLD_PCT = 0.03
JUMP_WINDOW_SECONDS = 5.0
MARKET_WINDOW_MINUTES = 5

COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_HOST = "https://gamma-api.polymarket.com"

# ---------------- Proxy SOLO para el trading real ----------------
# CORRECCIÓN: al principio seteábamos el proxy a nivel de TODO el
# proceso, pero eso hizo que la librería de WebSockets lo detectara
# automáticamente también para la conexión a Coinbase y a las cuotas de
# ETH -- que ya sabíamos que funcionaban bien SIN proxy, y el proxy en
# sí rechazó esas conexiones (HTTP 403, el proxy no las dejó pasar).
#
# Ahora el proxy se activa solo puntualmente, alrededor de las llamadas
# de trading real (ClobClient), usando este context manager -- así el
# resto de las conexiones (Coinbase, cuotas de ETH) siguen yendo
# directo, como ya confirmamos que funciona.
_PROXY_URL = os.environ.get("PROXY_URL")
if _PROXY_URL:
    print("[DEBUG] PROXY_URL configurada -- se usará SOLO para las "
          "llamadas de trading (ClobClient), no para Coinbase ni las "
          "cuotas de ETH.")
else:
    print("[DEBUG] PROXY_URL no configurada -- si el trading real falla "
          "por bloqueo geográfico, configurala en Railway.")


@contextlib.contextmanager
def _proxy_scope():
    """Activa HTTP_PROXY/HTTPS_PROXY solo durante el bloque `with`,
    y los restaura al salir -- para no afectar conexiones concurrentes
    (Coinbase, WebSocket de ETH) que corren en el mismo proceso."""
    if not _PROXY_URL:
        yield
        return
    prev_http = os.environ.get("HTTP_PROXY")
    prev_https = os.environ.get("HTTPS_PROXY")
    os.environ["HTTP_PROXY"] = _PROXY_URL
    os.environ["HTTPS_PROXY"] = _PROXY_URL
    try:
        yield
    finally:
        if prev_http is None:
            os.environ.pop("HTTP_PROXY", None)
        else:
            os.environ["HTTP_PROXY"] = prev_http
        if prev_https is None:
            os.environ.pop("HTTPS_PROXY", None)
        else:
            os.environ["HTTPS_PROXY"] = prev_https


# ============================================================
# Descubrimiento del mercado ETH 5min activo
# ============================================================

def find_active_eth_5m_market() -> dict:
    """Devuelve dict con token_id_up, token_id_down, slug, end_ts."""
    resp = requests.get(
        f"{GAMMA_HOST}/markets",
        params={
            "active": "true", "closed": "false", "tag_id": 21,
            "limit": 200, "order": "startDate", "ascending": "false",
        },
        timeout=10,
    )
    resp.raise_for_status()
    markets = resp.json()

    for m in markets:
        slug = m.get("slug", "")
        if "eth" in slug.lower() and "5m" in slug.lower() and "15m" not in slug.lower():
            token_ids = m["clobTokenIds"]
            if isinstance(token_ids, str):
                token_ids = json.loads(token_ids)
            end_date_str = m.get("endDate")
            end_ts = None
            if end_date_str:
                from datetime import datetime
                end_ts = datetime.fromisoformat(end_date_str.replace("Z", "+00:00")).timestamp()
            return {
                "token_id_up": token_ids[0],
                "token_id_down": token_ids[1],
                "slug": slug,
                "end_ts": end_ts,
            }

    sample_slugs = [m.get("slug", "?") for m in markets[:15]]
    raise RuntimeError(
        f"No se encontró mercado ETH 5min activo. Primeros slugs vistos: {sample_slugs}"
    )


# ============================================================
# Cliente de exchange (colocación de órdenes)
# ============================================================

class ExchangeClient:
    def __init__(self):
        self._sim_order_counter = 0
        if not SIMULATION_MODE:
            self._init_real_client()

    def _init_real_client(self):
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        try:
            from py_clob_client_v2 import ClobClient
        except ImportError as e:
            raise ImportError(
                "Falta instalar: pip install py-clob-client-v2"
            ) from e

        private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
        funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS")
        if not private_key or not funder:
            raise RuntimeError(
                "Definí POLYMARKET_PRIVATE_KEY y POLYMARKET_FUNDER_ADDRESS "
                "como variables de entorno en Railway para operar en real."
            )

        # Tipo de firma según cómo esté armada tu wallet:
        #   0 = EOA directa (MetaMask firmando y operando directamente, sin proxy)
        #   1 = Email/Magic (wallet integrada de Polymarket)
        #   2 = Wallet externa conectada vía el proxy de Polymarket (caso
        #       más común al conectar MetaMask desde polymarket.com -- la
        #       plataforma crea una wallet proxy fondeada desde tu MetaMask,
        #       y las operaciones salen de esa proxy, no de tu EOA directo)
        # Default = 2 porque es el flujo estándar para la mayoría de
        # usuarios que conectan una wallet externa vía la web de
        # Polymarket. Si tu caso es distinto, ajustá con la variable de
        # entorno POLYMARKET_SIGNATURE_TYPE.
        signature_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "2"))

        with _proxy_scope():
            self.client = ClobClient(
                "https://clob.polymarket.com",
                key=private_key,
                chain_id=137,
                funder=funder,
                signature_type=signature_type,
            )
            self.client.set_api_creds(self.client.create_or_derive_api_creds())

    def get_available_balance_usd(self) -> float:
        """
        Confirmado contra múltiples fuentes independientes (a diferencia
        de create_order/post_order más abajo, que NO están verificados
        contra la API real).
        """
        if SIMULATION_MODE:
            return 9999.0  # en simulación, "capital infinito" para pruebas

        from py_clob_client_v2 import AssetType, BalanceAllowanceParams
        with _proxy_scope():
            result = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
        return int(result["balance"]) / 1_000_000

    def place_aggressive_order(self, token_id: str, price_cents: float, size: float) -> str:
        """
        Coloca una orden agresiva (cruzando el spread) para maximizar
        probabilidad de fill rápido, ya que la ventana de oportunidad
        tras un salto es corta.

        ADVERTENCIA: en modo real, create_order/post_order NUNCA se
        probaron contra la API en vivo en este entorno de generación.
        Siguen el patrón general documentado, pero no están confirmados
        con la misma certeza que get_balance_allowance. Probar primero
        con SIMULATION_MODE=true y, al pasar a real, con tamaños chicos.
        """
        if SIMULATION_MODE:
            self._sim_order_counter += 1
            order_id = f"sim-{self._sim_order_counter}"
            print(f"[SIM] Orden colocada: token={token_id[:16]}... "
                  f"precio={price_cents:.1f}c size={size:.2f} (id={order_id})")
            return order_id

        from py_clob_client_v2 import OrderArgs, OrderType, Side

        args = OrderArgs(
            token_id=token_id,
            price=price_cents / 100.0,
            size=size,
            side=Side.BUY,
        )
        with _proxy_scope():
            signed = self.client.create_order(args)
            resp = self.client.post_order(signed, OrderType.GTC)
        print(f"[REAL] Orden colocada: {resp}")
        return resp["orderID"]


# ============================================================
# Loop principal
# ============================================================

async def _ping_loop(ws, ping_text: str, interval: float):
    while True:
        await asyncio.sleep(interval)
        try:
            await ws.send(ping_text)
        except Exception:
            return


async def run():
    print(f"[VERSION] Corriendo {SCRIPT_VERSION} | SIMULATION_MODE={SIMULATION_MODE} "
          f"| ORDER_SIZE_USD={ORDER_SIZE_USD}")

    if not SIMULATION_MODE:
        print("[AVISO] Modo REAL activo. Se van a colocar órdenes con dinero real.")

    exchange = ExchangeClient()
    balance = exchange.get_available_balance_usd()
    print(f"[BALANCE] Disponible: ${balance:.2f}")
    if not SIMULATION_MODE and balance < ORDER_SIZE_USD * 2:
        print(f"[AVISO] Balance bajo (${balance:.2f}) para ORDER_SIZE_USD=${ORDER_SIZE_USD:.2f} "
              f"por lado -- las órdenes podrían fallar por fondos insuficientes.")

    market = find_active_eth_5m_market()
    print(f"[MERCADO] {market['slug']} | up={market['token_id_up'][:16]}... "
          f"down={market['token_id_down'][:16]}...")

    jump_detector = JumpWithDirection(JUMP_THRESHOLD_PCT, JUMP_WINDOW_SECONDS)
    eth_midpoint = {"value": 0.5}
    state = {"market": market, "traded_this_window": False}

    # ---- Contadores de diagnóstico, para confirmar que sigue vivo aunque no haya saltado nada ----
    stats = {"btc_msgs": 0, "eth_msgs": 0, "last_btc_price": None}

    async def debug_loop():
        while True:
            await asyncio.sleep(30)
            print(f"[DEBUG] mensajes BTC recibidos: {stats['btc_msgs']} | "
                  f"último precio BTC: {stats['last_btc_price']} | "
                  f"mensajes ETH recibidos: {stats['eth_msgs']} | "
                  f"midpoint ETH actual: {eth_midpoint['value']:.1f}c | "
                  f"mercado: {state['market']['slug']} | "
                  f"ya operé esta vela: {state['traded_this_window']}")

    async def refresh_market_if_needed():
        """Si la ventana actual ya cerró (o está por cerrar), busca la
        siguiente y resetea el flag de 'ya operé esta vela'."""
        end_ts = state["market"].get("end_ts")
        if end_ts and time.time() >= end_ts:
            print("[MERCADO] Ventana cerrada, buscando la siguiente...")
            try:
                new_market = find_active_eth_5m_market()
                if new_market["slug"] != state["market"]["slug"]:
                    state["market"] = new_market
                    state["traded_this_window"] = False
                    print(f"[MERCADO] Nueva ventana: {new_market['slug']}")
            except Exception as e:
                print(f"[ERROR] No se pudo refrescar el mercado: {e}")

    async def market_refresh_loop():
        while True:
            await asyncio.sleep(15)
            await refresh_market_if_needed()

    async def btc_price_loop():
        try:
            print("[DEBUG] Conectando a Coinbase (BTC)...")
            ws = await asyncio.wait_for(websockets.connect(COINBASE_WS_URL), timeout=15)
            print("[DEBUG] Conexión a Coinbase abierta")
            try:
                await ws.send(json.dumps({
                    "type": "subscribe",
                    "product_ids": ["BTC-USD"],
                    "channels": ["ticker"],
                }))
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(msg, dict) or msg.get("type") != "ticker":
                        continue
                    price_str = msg.get("price")
                    if price_str is None:
                        continue

                    price = float(price_str)
                    ts = time.time()
                    stats["btc_msgs"] += 1
                    stats["last_btc_price"] = price

                    jumped, direction = jump_detector.add_price(ts, price)
                    if jumped and not state["traded_this_window"]:
                        await execute_trade(direction, price)
            finally:
                await ws.close()
        except Exception as e:
            print(f"[ERROR] btc_price_loop falló: {type(e).__name__}: {e}")
            raise

    async def execute_trade(direction: str, btc_price: float):
        state["traded_this_window"] = True  # marcar YA, antes de operar,
        # para no disparar dos veces si llegan varios precios juntos

        market = state["market"]
        if direction == "up":
            token_id = market["token_id_up"]
        else:
            token_id = market["token_id_down"]

        # Precio agresivo: cruza el spread para maximizar probabilidad
        # de fill rápido (la ventana de oportunidad es corta).
        midpoint = eth_midpoint["value"]
        aggressive_price = min(99.0, midpoint + 8.0) if direction == "up" else min(99.0, midpoint + 8.0)

        size = ORDER_SIZE_USD / (aggressive_price / 100.0)

        print(f"[TRADE] Salto de BTC ({btc_price:.2f}, dirección={direction}) -> "
              f"comprando {direction.upper()} en {market['slug']} "
              f"@ {aggressive_price:.1f}c, size={size:.2f} (${ORDER_SIZE_USD})")

        try:
            exchange.place_aggressive_order(token_id, aggressive_price, size)
        except Exception as e:
            print(f"[ERROR] Falló la colocación de la orden: {e}")

    async def eth_odds_loop():
        market = state["market"]
        while True:
            try:
                ws = await asyncio.wait_for(websockets.connect(MARKET_WS_URL), timeout=15)
                await ws.send(json.dumps({
                    "assets_ids": [state["market"]["token_id_up"]],
                    "type": "market",
                    "custom_feature_enabled": True,
                }))
                asyncio.create_task(_ping_loop(ws, "PING", 10))

                async for raw in ws:
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    messages = data if isinstance(data, list) else [data]
                    for msg in messages:
                        if not isinstance(msg, dict):
                            continue
                        ev_type = msg.get("event_type")
                        midpoint = None
                        if ev_type == "book":
                            bids = msg.get("bids") or []
                            asks = msg.get("asks") or []
                            if bids and asks:
                                midpoint = (float(bids[0]["price"]) + float(asks[0]["price"])) / 2 * 100
                        elif ev_type == "price_change":
                            for ch in (msg.get("price_changes") or msg.get("priceChanges") or []):
                                bb = ch.get("best_bid") or ch.get("bestBid")
                                ba = ch.get("best_ask") or ch.get("bestAsk")
                                if bb is not None and ba is not None:
                                    midpoint = (float(bb) + float(ba)) / 2 * 100
                        elif ev_type == "best_bid_ask":
                            bb = msg.get("bestBid")
                            ba = msg.get("bestAsk")
                            if bb is not None and ba is not None:
                                midpoint = (float(bb) + float(ba)) / 2 * 100

                        if midpoint is not None:
                            eth_midpoint["value"] = midpoint
                            stats["eth_msgs"] += 1

                # Si el mercado cambió mientras estábamos en el loop, se
                # reconecta con el nuevo token al volver a empezar el while.
                if state["market"]["slug"] != market["slug"]:
                    market = state["market"]
            except Exception as e:
                print(f"[ERROR] eth_odds_loop: {e} -- reconectando en 5s")
                await asyncio.sleep(5)

    await asyncio.gather(btc_price_loop(), eth_odds_loop(), market_refresh_loop(), debug_loop())


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nDetenido por el usuario.")
