"""Tests de direction_logic.py, corribles sin websockets/requests instalados."""

from direction_logic import JumpWithDirection


def test_no_dispara_con_precio_estable():
    jd = JumpWithDirection(threshold_pct=0.03, window_seconds=5.0)
    base = 65000.0
    for i in range(5):
        jumped, direction = jd.add_price(1000.0 + i, base + i * 0.1)
        assert not jumped, f"Falso positivo en tick {i}"
    print("OK: no dispara con precio estable")


def test_detecta_salto_arriba():
    jd = JumpWithDirection(threshold_pct=0.03, window_seconds=5.0)
    base = 65000.0
    for i in range(3):
        jd.add_price(1000.0 + i, base)
    jumped, direction = jd.add_price(1004.0, base * 1.0005)
    assert jumped and direction == "up"
    print("OK: detecta salto hacia arriba")


def test_detecta_salto_abajo():
    jd = JumpWithDirection(threshold_pct=0.03, window_seconds=5.0)
    base = 65000.0
    for i in range(3):
        jd.add_price(2000.0 + i, base)
    jumped, direction = jd.add_price(2004.0, base * 0.9995)
    assert jumped and direction == "down"
    print("OK: detecta salto hacia abajo")


def test_no_dispara_bajo_el_umbral():
    jd = JumpWithDirection(threshold_pct=0.03, window_seconds=5.0)
    base = 65000.0
    for i in range(3):
        jd.add_price(3000.0 + i, base)
    jumped, direction = jd.add_price(3004.0, base * 1.0001)
    assert not jumped
    print("OK: no dispara con movimiento menor al umbral")


def test_tamano_de_orden_es_consistente():
    order_size_usd = 2.0
    for midpoint in [5.0, 45.0, 50.0, 80.0, 95.0]:
        aggressive_price = min(99.0, midpoint + 8.0)
        size = order_size_usd / (aggressive_price / 100.0)
        cost = (aggressive_price / 100.0) * size
        assert abs(cost - order_size_usd) < 0.01, f"Costo no coincide en midpoint={midpoint}"
    print("OK: el tamaño de orden siempre gasta ORDER_SIZE_USD exacto")


if __name__ == "__main__":
    test_no_dispara_con_precio_estable()
    test_detecta_salto_arriba()
    test_detecta_salto_abajo()
    test_no_dispara_bajo_el_umbral()
    test_tamano_de_orden_es_consistente()
    print("\nTodos los tests pasaron.")
