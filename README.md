# fxSAVE / fxUSD dashboard

Web estática enfocada en cinco fuentes, en vivo, sin números inventados:

1. **Morpho** — mercado directo fxSAVE/USDC
2. **Morpho** — vault curado por RockawayX ("f(x) Protocol Ecosystem USDC")
3. **Pendle** — mercados de fxSAVE (PT/YT)
4. **Curve** — pools de fxUSD/fxSAVE
5. **Convex** — pools de fxUSD/fxSAVE (los mismos de Curve, boosteados)

## Fuentes de datos

- **Morpho GraphQL API** (`https://api.morpho.org/graphql`) — mercado directo
  y el vault de RockawayX, con enlace directo a `app.morpho.org`.
- **DeFiLlama Yields API** (`https://yields.llama.fi/pools`) — Pendle, Curve
  y Convex reportan sus pools ahí, filtrado por símbolo `FXUSD`/`FXSAVE`/`FXN`.
  Cada card enlaza a `defillama.com/yields/pool/...`.

Ninguna requiere API key.

## Correrlo en local

```bash
pip install -r requirements.txt
python fetch_fx_pools.py
```

Genera `index.html` (el dashboard) y `data.json` (snapshot crudo, para debug).

## Publicarlo en GitHub Pages

1. Subí esta carpeta a un repo nuevo en GitHub.
2. **Settings → Pages → Source: Deploy from a branch → `main` / root**.
3. Tu dashboard queda en `https://tu-usuario.github.io/tu-repo/`.

## Actualización automática

`.github/workflows/update.yml` corre el script cada 6 horas (y también a mano
desde **Actions → Run workflow**), commitea `index.html` / `data.json` y
GitHub Pages los sirve solos.

## Nota importante

Si RockawayX renombra su vault o Morpho cambia el `uniqueKey` del mercado
fxSAVE/USDC, el script lo va a reportar como "no disponible" en vez de
inventar un número — revisá la salida en consola (`market=missing` /
`vault=missing`) si eso pasa.
