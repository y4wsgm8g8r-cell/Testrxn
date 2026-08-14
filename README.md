# Bot direccional ETH — opera real cuando BTC salta

Detecta saltos de precio de BTC (Coinbase, umbral 0.03% en 5s) y coloca
UNA orden real en el mercado de ETH 5min de Polymarket en la dirección
del salto, sosteniéndola hasta que la vela cierra — sin salida
anticipada, tal como se pidió.

## Arranca en modo simulación

`SIMULATION_MODE=true` es el default. **No opera con plata real hasta
que pongas explícitamente `SIMULATION_MODE=false`** como variable de
entorno en Railway. Esto es a propósito: el código que coloca órdenes
reales nunca se probó contra la API en vivo (ver checklist abajo).

## Qué SÍ está probado

- **Detección de dirección** (`direction_logic.py`): 4 tests, todos
  pasan — detecta salto arriba, abajo, y no dispara con precio estable
  ni con movimientos menores al umbral.
- **Matemática del tamaño de orden**: confirmado que siempre gasta
  exactamente `ORDER_SIZE_USD`, sin importar el precio, con el tope de
  99c respetado cerca de los extremos.
- **Conexión a Coinbase y al canal de mercado de Polymarket**: reutiliza
  el código exacto que ya confirmamos funcionando en el monitor
  (`eth_lag_monitor.py`), no es código nuevo sin probar.
- **`get_balance_allowance`**: confirmado contra múltiples fuentes
  independientes (mismo que en los bots anteriores).

## Qué NO está probado — leer antes de poner SIMULATION_MODE=false

- **`create_order` / `post_order`**: siguen el patrón general
  documentado de `py-clob-client-v2`, pero nunca se ejecutaron contra
  la API real. Podrían fallar por un nombre de parámetro mal, un tipo
  de firma (`signature_type`) sin configurar según tu wallet, o
  permisos on-chain (allowances) sin activar.
- **`find_active_eth_5m_market()`**: mismo patrón de búsqueda que ya
  funcionó para 15min, adaptado a 5min — debería andar, pero no se
  probó en vivo específicamente para mercados de 5 minutos.
- **El precio agresivo (`midpoint + 8 centavos`)** es una estimación
  razonable para garantizar fill rápido, no un valor optimizado — con
  mercados de spread ancho podría ser insuficiente (no cruza el ask
  real) o excesivo (paga de más).

## Variables de entorno necesarias (Railway → pestaña Variables)

```
SIMULATION_MODE=true          # cambiar a false para operar en real
ORDER_SIZE_USD=2.0
POLYMARKET_PRIVATE_KEY=...    # solo si SIMULATION_MODE=false
POLYMARKET_FUNDER_ADDRESS=... # solo si SIMULATION_MODE=false
```

## Cómo probarlo

1. Subí los 5 archivos a GitHub (mismo proceso que el monitor: borrar +
   subir de cero, no editar por partes).
2. Desplegá en Railway con `SIMULATION_MODE=true` (default).
3. Dejalo correr un rato y confirmá en los logs que aparecen líneas
   `[SIM] Orden colocada: ...` cuando detecta saltos — así sabés que la
   lógica de detección y dirección funciona antes de arriesgar plata.
4. **Recién ahí**, si querés pasar a real: agregá las credenciales,
   poné `SIMULATION_MODE=false`, y probá primero con `ORDER_SIZE_USD`
   bajo. Mirá de cerca los primeros logs por si `create_order` falla —
   si eso pasa, mandame el error exacto para ajustarlo.

## Una vez más, la advertencia de fondo

Sostener hasta el cierre sin salida = exposición binaria completa
(ganás todo o perdés todo lo apostado en esa operación). No hay
stop-loss ni toma de ganancias anticipada en este diseño — es
exactamente lo que pediste, pero quiero que quede escrito acá también,
no solo dicho en el chat.
