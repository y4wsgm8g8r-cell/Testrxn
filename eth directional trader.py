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
import json
import os
import signal
import sys
import time

import requests
import websockets

from direction_logic import JumpWithDirection

SCRIPT_VERSION = "v14-decimales-correctos-market-order"

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

# ---------------- Proxy (mismo patrón que tu bot Flask que funciona) ----------------
# CORRECCIÓN: antes intentamos setear HTTP_PROXY/HTTPS_PROXY como
# variables de entorno (globales o acotadas con un context manager) --
# pero confirmamos en vivo que py-clob-client-v2 NO las respeta para sus
# llamadas internas (la orden se rechazó igual, aunque la variable
# estaba activa). Ahora usamos exactamente el mismo mecanismo que tu
# bot Flask, que sí funciona: un diccionario `proxies=` pasado
# explícitamente en cada llamada a `requests`.
PROXY_URL = os.environ.get("PROXY_URL")
PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

if PROXIES:
    print("[DEBUG] PROXY_URL configurada -- se usará en las llamadas al "
          "servicio de órdenes en Rust (mismo patrón que el bot que ya funciona).")
else:
    print("[DEBUG] PROXY_URL no configurada.")


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
    """
    REEMPLAZADO: en vez de hablar directo con Polymarket vía
    py-clob-client-v2 (que chocaba con el geo-bloqueo pese al proxy),
    esto delega la ejecución real a un servicio en Rust que ya tenés
    corriendo en Railway (RUST_ORDER_SERVICE_URL) y que confirmaste que
    sí manda órdenes bien. Mismo patrón que tu bot Flask que funciona:
    `proxies=PROXIES` explícito en cada request, no una variable de
    entorno global (que es lo que fallaba antes).
    """

    def __init__(self):
        self._sim_order_counter = 0
        self.rust_url = (os.environ.get("RUST_ORDER_SERVICE_URL") or "").strip()
        if not SIMULATION_MODE:
            if not self.rust_url:
                raise RuntimeError(
                    "Definí RUST_ORDER_SERVICE_URL como variable de entorno en "
                    "Railway para operar en real (la URL de tu servicio Rust, "
                    "ej. https://rustclaude-production.up.railway.app)."
                )
            print(f"[DEBUG] RUST_ORDER_SERVICE_URL limpia: [{self.rust_url}] "
                  f"(entre corchetes, para detectar espacios/saltos de línea de más)")

    def get_available_balance_usd(self) -> float:
        if SIMULATION_MODE:
            return 9999.0  # en simulación, "capital infinito" para pruebas

        r = requests.get(
            self.rust_url.rstrip("/") + "/balance",
            timeout=15,
            proxies=PROXIES,
        )
        print(f"[BALANCE] status={r.status_code} body={r.text[:300]}")
        if r.status_code != 200:
            return 0.0
        data = r.json()
        if "error" in data:
            return 0.0
        raw_balance = data.get("balance")
        if raw_balance is None:
            return 0.0
        value = float(raw_balance)
        # Igual que en tu bot Flask: si viene en unidades "crudas"
        # (ej. 50000000 en vez de 50.0), se divide por 1e6.
        if value > 1_000_000:
            value = value / 1_000_000
        return value

    def place_aggressive_order(self, token_id: str, price_cents: float, size: float) -> str:
        """
        Manda la orden al servicio Rust -- mismo contrato que
        send_order_with_retry() de tu bot Flask que ya funciona.
        """
        if SIMULATION_MODE:
            self._sim_order_counter += 1
            order_id = f"sim-{self._sim_order_counter}"
            print(f"[SIM] Orden colocada: token={token_id[:16]}... "
                  f"precio={price_cents:.1f}c size={size:.2f} (id={order_id})")
            return order_id

        payload = {
            "token_id": token_id,
            # Regla específica de Polymarket para órdenes de mercado
            # (confirmada por el error real): price máx. 2 decimales,
            # size máx. 4 decimales -- distinto del límite genérico de
            # 6 decimales que asumí antes.
            "price": f"{price_cents / 100.0:.2f}",
            "size": f"{size:.4f}",
            "side": "buy",
            "type": "market",
        }
        r = requests.post(
            self.rust_url.rstrip("/") + "/order",
            json=payload,
            timeout=30,
            proxies=PROXIES,
        )
        print(f"[REAL] Status servicio Rust: {r.status_code} | body: {r.text[:500]}")
        resp = r.json()
        if r.status_code != 200 or "error" in resp:
            raise RuntimeError(f"Orden rechazada por el servicio Rust: {resp}")
        return resp.get("orderID", "sin-id")


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
    stats = {"btc_msgs": 0, "eth_msgs": 0, "last_btc_price": None, "last_eth_update_ts": time.time()}

    async def debug_loop():
        while True:
            await asyncio.sleep(30)
            secs_since_eth = time.time() - stats["last_eth_update_ts"]
            # Si pasaron más de 60s sin actualización de ETH, es señal de
            # que la conexión quedó trabada (no de que el mercado esté
            # tranquilo nomás) -- un mercado activo normalmente actualiza
            # al menos cada tanto, aunque sea lento.
            estado_eth = "‼️ POSIBLE TRABADA" if secs_since_eth > 60 else "OK"
            eth_raw = stats.get("eth_raw_msgs", 0)
            eth_types = stats.get("eth_event_types", {})
            print(f"[DEBUG] mensajes BTC recibidos: {stats['btc_msgs']} | "
                  f"último precio BTC: {stats['last_btc_price']} | "
                  f"mensajes ETH recibidos (con precio): {stats['eth_msgs']} | "
                  f"mensajes ETH crudos (todos): {eth_raw} | "
                  f"tipos vistos: {eth_types} | "
                  f"midpoint ETH actual: {eth_midpoint['value']:.1f}c | "
                  f"segundos desde última act. ETH: {secs_since_eth:.0f}s [{estado_eth}] | "
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

        size = round(ORDER_SIZE_USD / (aggressive_price / 100.0), 4)

        print(f"[TRADE] Salto de BTC ({btc_price:.2f}, dirección={direction}) -> "
              f"comprando {direction.upper()} en {market['slug']} "
              f"@ {aggressive_price:.1f}c, size={size:.2f} (${ORDER_SIZE_USD})")

        try:
            exchange.place_aggressive_order(token_id, aggressive_price, size)
        except Exception as e:
            print(f"[ERROR] Falló la colocación de la orden: {e}")

    async def eth_odds_loop():
        while True:
            current_slug = state["market"]["slug"]
            current_token = state["market"]["token_id_up"]
            try:
                ws = await asyncio.wait_for(websockets.connect(MARKET_WS_URL), timeout=15)
                await ws.send(json.dumps({
                    "assets_ids": [current_token],
                    "type": "market",
                    "custom_feature_enabled": True,
                }))
                asyncio.create_task(_ping_loop(ws, "PING", 10))

                async def watch_for_rotation():
                    """
                    BUG que encontramos: sin esto, si el mercado rota
                    mientras estamos conectados, nunca nos enteramos --
                    Polymarket no cierra la conexión sola cuando un
                    mercado de 5min resuelve, simplemente deja de mandar
                    actualizaciones para ese token, y nos quedábamos
                    escuchando un mercado ya muerto para siempre.
                    Este chequeo activo fuerza el cierre de la conexión
                    apenas detecta la rotación, para reconectar con el
                    token nuevo.
                    """
                    while True:
                        await asyncio.sleep(5)
                        if state["market"]["slug"] != current_slug:
                            print(f"[DEBUG] Mercado rotó de {current_slug} a "
                                  f"{state['market']['slug']} -- reconectando "
                                  f"canal de cuotas ETH")
                            await ws.close()
                            return

                watcher = asyncio.create_task(watch_for_rotation())

                try:
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

                            # Contador CRUDO: cualquier mensaje que llegue,
                            # aunque no logre sacarle un precio. Si esto
                            # sube pero "eth_msgs" no, significa que sí
                            # llegan datos (conexión viva) pero el parseo
                            # de ese tipo de evento tiene un bug -- distinto
                            # de una conexión realmente muerta.
                            stats["eth_raw_msgs"] = stats.get("eth_raw_msgs", 0) + 1
                            stats["eth_event_types"] = stats.get("eth_event_types", {})
                            stats["eth_event_types"][ev_type] = stats["eth_event_types"].get(ev_type, 0) + 1

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
                                stats["last_eth_update_ts"] = time.time()
                finally:
                    watcher.cancel()
            except Exception as e:
                print(f"[ERROR] eth_odds_loop: {e} -- reconectando en 5s")
                await asyncio.sleep(5)

    await asyncio.gather(btc_price_loop(), eth_odds_loop(), market_refresh_loop(), debug_loop())


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nDetenido por el usuario.")
