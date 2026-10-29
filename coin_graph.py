"""График цены коина: чёрный фон, красные/зелёные полоски.

Строится по coin_price_history (цена по времени).
Полоска зелёная, если цена выросла относительно предыдущей точки,
красная — если упала, серая — если не изменилась.
"""
import io
import logging
from datetime import datetime, timezone
from typing import List, Tuple

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

W = 800
H = 400
PAD_L = 60
PAD_R = 20
PAD_T = 50
PAD_B = 60

BG = (0, 0, 0)
GRID = (40, 40, 40)
GREEN = (60, 200, 100)
RED = (220, 70, 70)
GRAY = (120, 120, 120)
WHITE = (230, 230, 230)


def _font(size: int):
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _fmt_price(p: int) -> str:
    return f"{p}💰"


def render_price_chart(
    coin_name: str,
    history: List[Tuple[str, int, int]],
    current_price: int,
    yield_per_day: int,
) -> bytes:
    """history: список (ts_str, price, sold). Возвращает PNG bytes.

    Если истории < 2 точек — возвращает None (вызывающий отправит «нет данных»).
    """
    if not history or len(history) < 2:
        return None

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    prices = [p for _, p, _ in history]
    pmin, pmax = min(prices), max(prices)
    if pmin == pmax:
        pmin -= 1
        pmax += 1

    # сетка
    for i in range(5):
        y = PAD_T + (H - PAD_T - PAD_B) * i // 4
        d.line([(PAD_L, y), (W - PAD_R, y)], fill=GRID)

    n = len(history)
    chart_w = W - PAD_L - PAD_R
    chart_h = H - PAD_T - PAD_B
    bar_w = max(2, chart_w // n - 2)
    gap = max(1, (chart_w - bar_w * n) // max(1, n - 1))

    prev_price = prices[0]
    for i, (ts, price, sold) in enumerate(history):
        x = PAD_L + i * (bar_w + gap)
        # высота относительно min..max
        frac = (price - pmin) / (pmax - pmin) if pmax > pmin else 0.5
        bar_h = max(4, int(chart_h * frac))
        y1 = H - PAD_B - bar_h
        y2 = H - PAD_B
        if i == 0:
            color = GRAY
        elif price > prev_price:
            color = GREEN
        elif price < prev_price:
            color = RED
        else:
            color = GRAY
        d.rectangle([x, y1, x + bar_w, y2], fill=color)
        prev_price = price

    # подписи
    f = _font(14)
    d.text((PAD_L, 10), f"{coin_name} — цена", fill=WHITE, font=f)
    d.text((PAD_L, 28), f"доход: {yield_per_day}💰/сутки", fill=WHITE, font=f)
    d.text((PAD_L, H - 40), f"мин {pmin}", fill=WHITE, font=f)
    d.text((W - PAD_R - 80, H - 40), f"макс {pmax}", fill=WHITE, font=f)
    d.text((W - PAD_R - 120, 10), f"сейчас: {current_price}💰", fill=WHITE, font=f)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
