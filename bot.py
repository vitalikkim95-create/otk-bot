import os
import json
import base64
import logging
import threading
import time
import requests
from flask import Flask, request
import anthropic

from calc import build_report, parse_articles

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Список разрешённых chat_id через запятую, например: "590441036,-5225713707"
# Пусто/не задано = бот отвечает всем (небезопасно, но удобно для первого теста).
_allowed_raw = os.environ.get("ALLOWED_CHAT_IDS", "").strip()
ALLOWED_CHAT_IDS = {int(x) for x in _allowed_raw.split(",") if x.strip()} if _allowed_raw else None

# Username бота без @, например "otkinformbot" — нужен, чтобы понимать,
# когда его явно упомянули в группе.
BOT_USERNAME = os.environ.get("BOT_USERNAME", "").lstrip("@").lower()

# Скрины, присланные файлом (без сжатия), тоже считаем. 5 МБ — лимит Claude на картинку.
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024

# Сколько секунд ждать остальные фото из одного альбома, прежде чем считать
MEDIA_GROUP_WAIT_SECONDS = 3

if ALLOWED_CHAT_IDS is None:
    logger.warning("ALLOWED_CHAT_IDS не задан — бот отвечает в любом чате и тратит деньги API")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
app = Flask(__name__)

# Справочник артикулов общий с qc_bot: файл articles.txt в репозитории qc_bot.
# Новые артикулы добавляйте только туда — сюда они подтянутся сами.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
ARTICLES_URL = "https://api.github.com/repos/vitalikkim95-create/qc_bot/contents/articles.txt"
ARTICLES_CACHE_SECONDS = 600

# ---------------------------------------------------------------------------
# Claude только переписывает таблицу с фото. Вся методика расчёта — в calc.py.
# ---------------------------------------------------------------------------
EXTRACT_PROMPT = """
Ты переписываешь в JSON таблицы с фото китайских накладных ОТК.
Ничего не считай и не исправляй — перепиши строки точно так, как они в таблице.

Колонки таблицы: 分点 (склад), 产品 (товар), 数量 (кол-во), 单价 (цена),
金额 (сумма), 账单预估运输费 (доставка), 款号 (код модели),
账单金额 (сумма счёта), 待付款合计 (итого к оплате).

Перепиши КАЖДУЮ строку таблицы, включая строки сборов (货拉拉运费差额,
增值服务（抽检费） и подобные):
- invoice: номер счёта 1, 2, 3… Счёт — блок строк с одной датой 账单日期 и одной
  ячейкой 账单金额. Номера сквозные по всем фото.
- product: 产品 как написано.
- code: 款号 как написано; если в ячейке несколько кодов — как есть, через /.
- qty: 数量 числом, со знаком минус, если он есть; "/" или пусто — null.
- unit_price: 单价 числом; "/" или пусто — null.
- amount: 金额 числом, со знаком; пусто — null.
- delivery_group и delivery_amount: если одна ячейка 账单预估运输费 объединена на
  несколько строк — у всех этих строк одинаковый delivery_group (например "1-a")
  и delivery_amount = число из этой ячейки. Если у строки своя ячейка с числом —
  свой отдельный delivery_group. Если в ячейке "/" или пусто — оба null.
- paused: true, если товар — юбка (裙), кардиган (开衫) или футболка с коротким
  рукавом (短袖T恤, 短袖); иначе false. 长袖 (длинный рукав) — это false.

grand_total: сумма всех чисел 待付款合计 на фото. Если 待付款合计 нет — сумма всех
账单金额. Если нет и их — null.

Если несколько фото — это части одной таблицы или разные накладные. Строку,
которая видна сразу на двух фото, переписывай один раз.
"""

_NUMBER_OR_NULL = {"anyOf": [{"type": "number"}, {"type": "null"}]}
_STRING_OR_NULL = {"anyOf": [{"type": "string"}, {"type": "null"}]}
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "invoice": {"type": "integer"},
                    "product": {"type": "string"},
                    "code": {"type": "string"},
                    "qty": _NUMBER_OR_NULL,
                    "unit_price": _NUMBER_OR_NULL,
                    "amount": _NUMBER_OR_NULL,
                    "delivery_group": _STRING_OR_NULL,
                    "delivery_amount": _NUMBER_OR_NULL,
                    "paused": {"type": "boolean"},
                },
                "required": [
                    "invoice", "product", "code", "qty", "unit_price", "amount",
                    "delivery_group", "delivery_amount", "paused",
                ],
                "additionalProperties": False,
            },
        },
        "grand_total": _NUMBER_OR_NULL,
    },
    "required": ["rows", "grand_total"],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# Буфер для альбомов (несколько фото, присланных одним сообщением-группой)
# ---------------------------------------------------------------------------
_media_groups_lock = threading.Lock()
_media_groups = {}  # media_group_id -> {"chat_id": ..., "images": [(file_id, тип)], "mentioned": bool}


_articles_lock = threading.Lock()
_articles_cache = {"articles": {}, "loaded_at": 0.0}


def load_articles():
    """Справочник с GitHub, кэш на 10 минут; при ошибке — последняя удачная версия."""
    with _articles_lock:
        if time.time() - _articles_cache["loaded_at"] < ARTICLES_CACHE_SECONDS:
            return _articles_cache["articles"]
        try:
            r = requests.get(
                ARTICLES_URL,
                headers={
                    "Authorization": f"Bearer {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github.raw",
                },
                timeout=15,
            )
            r.raise_for_status()
            _articles_cache["articles"] = parse_articles(r.text)
            _articles_cache["loaded_at"] = time.time()
            logger.info("Справочник артикулов загружен: %s моделей",
                        len(set(_articles_cache["articles"].values())))
        except Exception:
            logger.exception("Не удалось загрузить справочник артикулов с GitHub")
        return _articles_cache["articles"]


def is_allowed(chat_id):
    if ALLOWED_CHAT_IDS is None:
        return True
    return chat_id in ALLOWED_CHAT_IDS


def bot_username():
    """Username бота: из BOT_USERNAME, а если не задан — у самого Telegram."""
    global BOT_USERNAME
    if not BOT_USERNAME:
        try:
            r = requests.get(f"{TELEGRAM_API}/getMe", timeout=10).json()
            BOT_USERNAME = r["result"]["username"].lower()
        except Exception:
            logger.exception("Не удалось узнать username бота")
    return BOT_USERNAME


def is_mentioned(message):
    """В личке — всегда True. В группе — только если бота явно упомянули
    в подписи/тексте (@username)."""
    if message["chat"].get("type") not in ("group", "supergroup"):
        return True

    # Reply на сообщение бота
    username = bot_username()
    if not username:
        return False

    # Ответ (reply) на сообщение бота упоминанием не считается: в ответ на отчёт
    # присылают, например, скрин оплаты, и считать его не нужно.
    # Упоминание в подписи к фото или в тексте сообщения
    text = (message.get("caption") or message.get("text") or "").lower()
    return f"@{username}" in text


def send_chat_action(chat_id, action="typing"):
    try:
        requests.post(
            f"{TELEGRAM_API}/sendChatAction",
            json={"chat_id": chat_id, "action": action},
            timeout=10,
        )
    except Exception:
        logger.exception("Не удалось отправить chat action")


def send_message(chat_id, text):
    """Отправка сообщения с разбивкой на части (лимит Telegram — 4096 символов)."""
    if not text:
        text = "Не получилось ничего посчитать — проверьте фото."
    for i in range(0, len(text), 4000):
        try:
            r = requests.post(
                f"{TELEGRAM_API}/sendMessage",
                json={"chat_id": chat_id, "text": text[i:i + 4000]},
                timeout=30,
            )
            if not r.ok:
                logger.error("Telegram sendMessage failed: %s %s", r.status_code, r.text)
        except Exception:
            logger.exception("Не удалось отправить сообщение в Telegram")


def get_file_bytes(file_id):
    r = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}, timeout=30).json()
    file_path = r["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    return requests.get(file_url, timeout=30).content


def extract_rows(content):
    """Claude переписывает таблицу с фото в JSON. При сбое — одна повторная попытка."""
    for attempt in (1, 2):
        started = time.time()
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": EXTRACT_SCHEMA},
            },
            system=EXTRACT_PROMPT,
            messages=[{"role": "user", "content": content}],
        ) as stream:
            response = stream.get_final_message()
        text = "".join(b.text for b in response.content if b.type == "text")
        data = None
        if response.stop_reason == "end_turn":
            try:
                data = json.loads(text)
            except ValueError:
                pass
        logger.info(
            "Claude, попытка %s: stop_reason=%s, output_tokens=%s, строк=%s, %.0f сек",
            attempt, response.stop_reason, response.usage.output_tokens,
            len(data["rows"]) if data else "-", time.time() - started,
        )
        if data is not None:
            # Пустой список строк — честный ответ «таблицы на фото нет», повтор не нужен.
            return data
    return None


def process_photos(chat_id, images):
    """Считает одну или несколько фотографий вместе и присылает готовый отчёт."""
    # Держим индикатор "печатает" живым (сам статус живёт ~5 сек в Telegram)
    stop_typing = threading.Event()

    def typing_loop():
        while not stop_typing.is_set():
            send_chat_action(chat_id, "typing")
            stop_typing.wait(4)

    threading.Thread(target=typing_loop, daemon=True).start()
    try:
        content = []
        for file_id, media_type in images:
            img_bytes = get_file_bytes(file_id)
            if len(img_bytes) > MAX_IMAGE_BYTES:
                stop_typing.set()
                send_message(chat_id, "Картинка больше 5 МБ — пришлите скрин как фото, а не файлом.")
                return
            img_b64 = base64.b64encode(img_bytes).decode()
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": img_b64},
            })

        content.append({"type": "text", "text": "Перепиши таблицу с фото в JSON."})

        data = extract_rows(content)
        if data is None:
            result_text = "Не получилось посчитать с двух попыток — отправьте фото ещё раз."
        else:
            result_text = (build_report(data, load_articles())
                           or "На фото не нашёл таблицу накладной — нечего считать.")
        stop_typing.set()
        send_message(chat_id, result_text)
    except Exception:
        logger.exception("Ошибка при обработке фото")
        stop_typing.set()
        send_message(chat_id, "Не получилось обработать фото, попробуйте прислать ещё раз.")
    finally:
        stop_typing.set()


def schedule_media_group(media_group_id):
    """Ждём немного, собираем все фото альбома, затем считаем разом."""
    time.sleep(MEDIA_GROUP_WAIT_SECONDS)
    with _media_groups_lock:
        group = _media_groups.pop(media_group_id, None)
    if not group or not group["mentioned"]:
        return
    process_photos(group["chat_id"], group["images"])


@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}
    message = update.get("message") or update.get("channel_post")
    if not message:
        return "ok"

    chat_id = message["chat"]["id"]
    logger.info("Incoming message: chat_id=%s chat_type=%s has_photo=%s has_document=%s",
                chat_id, message["chat"].get("type"), "photo" in message, "document" in message)

    if not is_allowed(chat_id):
        logger.info("Chat %s не в списке разрешённых — игнорирую", chat_id)
        return "ok"

    mentioned = is_mentioned(message)

    # Текстовые команды
    if "text" in message:
        if not mentioned:
            return "ok"
        text = message["text"].strip()
        if text.startswith("/start") or text.startswith("/help"):
            send_message(
                chat_id,
                "Привет! Пришлите фото/скрин таблицы ОТК (можно сразу несколько) — "
                "посчитаю по нашей методике и пришлю готовый отчёт списком.",
            )
        return "ok"

    photos = message.get("photo")
    document = message.get("document") or {}
    if photos:
        # Telegram присылает несколько размеров, берём самый большой
        image = (photos[-1]["file_id"], "image/jpeg")
    elif document.get("mime_type") in IMAGE_TYPES:
        image = (document["file_id"], document["mime_type"])
    else:
        return "ok"

    media_group_id = message.get("media_group_id")

    if media_group_id:
        # Часть альбома — копим фото и запускаем таймер один раз на группу.
        # Подпись с упоминанием обычно есть только у одного фото из альбома,
        # поэтому достаточно, чтобы упоминание нашлось хотя бы у одного.
        with _media_groups_lock:
            group = _media_groups.get(media_group_id)
            if group is None:
                group = {"chat_id": chat_id, "images": [], "mentioned": False}
                _media_groups[media_group_id] = group
                threading.Thread(
                    target=schedule_media_group, args=(media_group_id,), daemon=True
                ).start()
            group["images"].append(image)
            if mentioned:
                group["mentioned"] = True
        return "ok"

    if not mentioned:
        return "ok"

    # Одиночное фото — считаем сразу в фоновом потоке, чтобы не держать вебхук
    threading.Thread(target=process_photos, args=(chat_id, [image]), daemon=True).start()
    return "ok"


@app.route("/", methods=["GET"])
def health():
    return "OTK bot is running"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
