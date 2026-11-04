"""Расчёт накладной ОТК по нашей методике. Claude только переписывает строки с фото."""

import re

ARTICLE_LINE = re.compile(r"^(.+?)\s+[—–-]\s+(.+)$")


def parse_articles(text):
    """articles.txt -> {вариант кода в верхнем регистре: (основной код, Название)}."""
    articles = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = ARTICLE_LINE.match(line)
        if not match:
            continue
        codes = [c.strip() for c in match.group(1).split(",") if c.strip()]
        name = match.group(2).strip()
        for code in codes:
            articles[code.upper()] = (codes[0], name[:1].upper() + name[1:])
    return articles


def money(value):
    # + 0.0 убирает «−0» у сумм, округлившихся до нуля
    return f"{round(value, 2) + 0.0:.2f}".rstrip("0").rstrip(".").replace("-", "−")


def qty_str(value):
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def calculate(rows, articles):
    """Применяет правила 1-6 методики. rows — строки таблицы как их переписал Claude."""
    # Индексы считаем по исходному порядку строк: от них зависят ячейки доставки.
    for i, r in enumerate(rows):
        r["_pos"] = i
    all_rows = rows
    rows = [r for r in rows if not r.get("paused")]
    goods = [r for r in rows if r.get("qty") is not None]
    fees = [r for r in rows if r.get("qty") is None and r.get("amount")]

    for r in goods:
        # Правило 1: всегда qty × цена, готовый 金额 не используем (в нём уже сидит доставка).
        price = r.get("unit_price")
        r["line"] = r["qty"] * price if price is not None else (r.get("amount") or 0)
        r["delivery"] = 0.0

    positive = [r for r in goods if r["qty"] > 0]

    def biggest_in_invoice(invoice):
        same = [r for r in positive if r["invoice"] == invoice] or positive
        return max(same, key=lambda r: r["qty"]) if same else None

    # Правило 2: доставка ячейки — к строке с максимальным количеством среди строк,
    # которые эта ячейка накрывает. Каждая ячейка учитывается ровно один раз.
    for start in all_rows:
        amount = start.get("delivery_amount")
        if not amount or start.get("paused"):
            continue
        span = max(start.get("delivery_rows") or 1, 1)
        covered = {start["_pos"] + k for k in range(span)}
        members = [r for r in positive if r["_pos"] in covered]
        target = (max(members, key=lambda r: r["qty"]) if members
                  else biggest_in_invoice(start["invoice"]))
        if target:
            target["delivery"] += amount

    # Правило 3: мелкие сборы — к доставке самой крупной позиции того же счёта.
    for fee in fees:
        target = biggest_in_invoice(fee["invoice"])
        if target:
            target["delivery"] += fee["amount"]

    def resolve(code):
        found = articles.get(str(code).strip().upper())
        return found if found else (str(code).strip(), None)

    # Правила 4-6: объединяем по коду модели вместе с минусовыми строками, поэтому
    # сумма всех строк отчёта равна сумме к оплате. Названия — из справочника.
    positions = {}
    for r in goods:
        code, name = resolve(r["code"])
        pos = positions.setdefault(code, {"code": code, "name": name, "qty": 0, "sum": 0.0, "delivery": 0.0})
        pos["qty"] += r["qty"]
        pos["sum"] += r["line"]
        pos["delivery"] += r["delivery"]

    return [p for p in positions.values() if p["qty"] or round(p["sum"], 2) or round(p["delivery"], 2)]


def label(item):
    if item["name"]:
        return f"{item['name']} ({item['code']})"
    return item["code"] or "без кода"


def build_report(data, articles):
    """Возвращает (отчёт, предупреждение). В отчёте после итога ничего нет."""
    positions = calculate(data["rows"], articles)
    if not positions:
        return "", None

    lines = []
    for i, pos in enumerate(positions, start=1):
        qty = qty_str(pos["qty"]).replace("-", "−")
        line = f"{i}. {label(pos)} — {qty} шт: {money(pos['sum'])} ю"
        if round(pos["delivery"], 2):
            line += f", доставка {money(pos['delivery'])} ю"
        lines.append(line)

    total = sum(p["sum"] + p["delivery"] for p in positions)
    lines += ["", f"К оплате: {money(total)} ю"]

    # Правило 7: сверка с итогом накладной. Предупреждение — отдельным сообщением.
    warning = None
    invoice_total = data.get("grand_total")
    if invoice_total is not None and abs(total - invoice_total) > 1:
        warning = (f"Внимание: итог накладной {money(invoice_total)} ю, "
                   f"по расчёту {money(total)} ю — проверьте вручную")

    return "\n".join(lines), warning
