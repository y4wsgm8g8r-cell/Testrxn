"""
Lógica pura (sin dependencias de red) para detectar saltos de precio
CON dirección -- a diferencia del monitor de rezago, acá necesitamos
saber hacia dónde apostar, no solo que hubo un salto.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass
class JumpWithDirection:
    threshold_pct: float
    window_seconds: float
    _prices: deque = field(default_factory=deque)

    def add_price(self, ts: float, price: float) -> tuple[bool, str | None]:
        self._prices.append((ts, price))
        cutoff = ts - self.window_seconds
        while self._prices and self._prices[0][0] < cutoff:
            self._prices.popleft()

        if len(self._prices) < 2:
            return False, None

        oldest_price = self._prices[0][1]
        pct_change = (price - oldest_price) / oldest_price * 100
        if abs(pct_change) >= self.threshold_pct:
            direction = "up" if pct_change > 0 else "down"
            return True, direction
        return False, None
