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
    return f"{round(value, 2):.2f}".rstrip("0").rstrip(".").replace("-", "−")


def qty_str(value):
    return f"{value:g}"


def calculate(rows, articles):
    """Применяет правила 1-6 методики. rows — строки таблицы как их переписал Claude."""
    rows = [r for r in rows if not r.get("paused")]
    goods = [r for r in rows if r.get("qty") is not None]
    fees = [r for r in rows if r.get("qty") is None and r.get("amount")]

    for r in goods:
        # Правило 1: всегда qty × цена, готовый 金额 не используем (в нём уже сидит доставка).
        price = r.get("unit_price")
        r["line"] = r["qty"] * price if price is not None else (r.get("amount") or 0)
        r["delivery"] = 0.0

    positive = [r for r in goods if r["qty"] > 0]

    # Правило 2: доставка группы — к строке с максимальным количеством в группе.
    groups = {}
    for r in positive:
        if r.get("delivery_group") and r.get("delivery_amount"):
            groups.setdefault((r["invoice"], r["delivery_group"]), []).append(r)
    for members in groups.values():
        biggest = max(members, key=lambda r: r["qty"])
        biggest["delivery"] += members[0]["delivery_amount"]

    # Правило 3: мелкие сборы — к доставке самой крупной позиции того же счёта.
    for fee in fees:
        same_invoice = [r for r in positive if r["invoice"] == fee["invoice"]] or positive
        if same_invoice:
            max(same_invoice, key=lambda r: r["qty"])["delivery"] += fee["amount"]

    def resolve(code):
        found = articles.get(str(code).strip().upper())
        return found if found else (str(code).strip(), None)

    # Правила 5 и 6: объединяем по коду модели, названия — из справочника.
    positions = {}
    for r in positive:
        code, name = resolve(r["code"])
        pos = positions.setdefault(code, {"code": code, "name": name, "qty": 0, "sum": 0.0, "delivery": 0.0})
        pos["qty"] += r["qty"]
        pos["sum"] += r["line"]
        pos["delivery"] += r["delivery"]

    # Правило 4: вычет — только строка с отрицательным количеством.
    deductions = []
    for r in goods:
        if r["qty"] < 0:
            code, name = resolve(r["code"])
            deductions.append({"code": code, "name": name, "qty": -r["qty"], "sum": r["line"]})

    return list(positions.values()), deductions


def label(item):
    return f"{item['name']} ({item['code']})" if item["name"] else item["code"]


def build_report(data, articles):
    positions, deductions = calculate(data["rows"], articles)
    if not positions and not deductions:
        return ""

    lines = []
    for i, pos in enumerate(positions, start=1):
        line = f"{i}. {label(pos)} — {qty_str(pos['qty'])} шт: {money(pos['sum'])} ю"
        if round(pos["delivery"], 2):
            line += f", доставка {money(pos['delivery'])} ю"
        lines.append(line)

    total = sum(p["sum"] + p["delivery"] for p in positions)
    lines += ["", f"Общий итог: {money(total)} ю"]

    if deductions:
        lines.append("")
        for d in deductions:
            lines.append(
                f"{label(d)} — {qty_str(d['qty'])} шт: {money(d['sum'])} ю "
                "(вычет с прошлых оплат в связи с браком)"
            )

    # Правило 7: сверка с итогом накладной.
    invoice_total = data.get("grand_total")
    if invoice_total is not None:
        calculated = total + sum(d["sum"] for d in deductions)
        if abs(calculated - invoice_total) > 1:
            lines += [
                "",
                f"Внимание: итог накладной {money(invoice_total)} ю, "
                f"по расчёту {money(calculated)} ю — проверьте вручную",
            ]

    return "\n".join(lines)
