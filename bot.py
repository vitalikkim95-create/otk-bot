import os
import re
import base64
import logging
import threading
import time
import requests
from flask import Flask, request
import anthropic

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

# Сколько секунд ждать остальные фото из одного альбома, прежде чем считать
MEDIA_GROUP_WAIT_SECONDS = 3

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
app = Flask(__name__)

# Справочник артикулов общий с qc_bot: файл articles.txt в репозитории qc_bot.
# Новые артикулы добавляйте только туда — сюда они подтянутся сами.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
ARTICLES_URL = "https://api.github.com/repos/vitalikkim95-create/qc_bot/contents/articles.txt"
ARTICLES_CACHE_SECONDS = 600
ARTICLE_LINE = re.compile(r"^(.+?)\s+[—–-]\s+(.+)$")

# ---------------------------------------------------------------------------
# Методика расчёта. Отредактируйте этот блок, если правила изменятся.
# {ARTICLES} подставляется из общего справочника.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """
Ты помогаешь считать китайские накладные/счета ОТК (таблицы с колонками
数量 (кол-во), 单价 (цена за единицу), 账单预估运输费 (доставка), 款号 (код модели)
и, возможно, 货拉拉运费差额 или другими мелкими сборами).

Тебе может прийти ОДНА или НЕСКОЛЬКО фотографий (например, если таблица не
влезла в один скрин и человек прислал 2-3 части одной таблицы, или несколько
разных накладных сразу). Считай их все вместе как единый набор данных:
объединяй одинаковые коды моделей между фотографиями точно так же, как
объединяешь повторы внутри одной таблицы.

МЕТОДИКА РАСЧЁТА (строго следуй этим правилам):

1. Для каждой строки считаешь: 数量 × 单价. НЕ используй готовое значение из
   столбца 金额, если оно отличается от 数量×单价 — всегда пересчитывай сам.

2. Доставка (账单预估运输费) обычно указана одной ячейкой на группу строк
   (объединённая ячейка в таблице). Определи, какие строки относятся к
   какой группе доставки, и прибавь сумму доставки к строке с МАКСИМАЛЬНЫМ
   数量 внутри этой группы. Остальные строки группы — без доставки.

3. Мелкие дополнительные сборы (货拉拉运费差额, 抽检费 "проверка" и подобные,
   если у них нет своего 数量) — прибавляй к доставке любой крупной позиции
   того же счёта (неважно, к какой именно — сумма всегда небольшая).

4. Строки с ОТРИЦАТЕЛЬНЫМ 数量 (обычно помечены как "预估") в основной расчёт
   НЕ включаются. Их нужно вынести ОТДЕЛЬНЫМ списком в самом конце отчёта,
   каждую строку с пояснением: "вычет с прошлых оплат в связи с браком".

5. Если один и тот же код модели (款号) встречается в таблице (или на разных
   фотографиях) несколько раз — объединяй все эти строки в одну: складывай
   количество, сумму и доставку.

6. Код модели заменяй на нормальное название товара по справочнику ниже.
   Если код в справочнике не найден — не выдумывай название, пиши только код.

СПРАВОЧНИК КОДОВ (款号 → название):
{ARTICLES}

Категории на паузе (если встретятся — не включай в отчёт и не запоминай):
юбки, кардиганы, футболка.

Коды 8862 и 806 — названия пока не заданы, если встретятся, пиши просто код.

ФОРМАТ ИТОГОВОГО ОТВЕТА (строго):

Пронумерованный список, каждая позиция в формате:
Название (код) — X шт: Y ю, доставка Z ю

— если доставка равна 0, эту часть строки не пиши вообще (просто "X шт: Y ю")
— никаких лишних слов и пояснений внутри позиций

После списка положительных позиций — строка:
Общий итог: N ю

Если есть отрицательные строки (брак) — после общего итога добавь отдельный
список:
Название/код — X шт: −Y ю (вычет с прошлых оплат в связи с браком)

КРИТИЧЕСКИ ВАЖНО ПРО ФОРМАТ ОТВЕТА — читай внимательно, это не рекомендация,
а жёсткое требование:

Твой ответ пользователю должен состоять ТОЛЬКО из:
1. Пронумерованного списка позиций в формате "Название (код) — X шт: Y ю[, доставка Z ю]"
2. Строки "Общий итог: N ю"
3. (если есть брак) списка вычетов

И БОЛЬШЕ НИЧЕГО. Даже одного лишнего слова быть не должно.

ЗАПРЕЩЕНО применять эти правила прямо в ответе — весь разбор, группировку по
分点/городам (Дунгуань, Гуанчжоу и т.п.), восстановление строк, промежуточные
списки "- код: X×Y=Z" делай молча, про себя, не выводя их в текст ответа.
Пользователь должен увидеть только финальный чистый список, как будто ты
сразу знал ответ — без слов "сначала", "разберу", "восстановлю", "замечу",
без markdown-заголовков и звёздочек, без построчного проговаривания таблицы.

Если засомневался в формате — вспомни: хороший ответ короткий, состоит
только из пронумерованных строк "Название (код) — ..." и итога.
"""

# ---------------------------------------------------------------------------
# Буфер для альбомов (несколько фото, присланных одним сообщением-группой)
# ---------------------------------------------------------------------------
_media_groups_lock = threading.Lock()
_media_groups = {}  # media_group_id -> {"chat_id": ..., "file_ids": [...], "timer": Timer}


_articles_lock = threading.Lock()
_articles_cache = {"text": "", "loaded_at": 0.0}


def format_articles(text):
    """articles.txt -> строки справочника для промпта."""
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = ARTICLE_LINE.match(line)
        if not match:
            logger.warning("Строка справочника не разобрана: %s", line)
            continue
        codes = [c.strip() for c in match.group(1).split(",") if c.strip()]
        name = match.group(2).strip()
        name = name[:1].upper() + name[1:]
        variants = f" (может писаться {', '.join(codes[1:])})" if len(codes) > 1 else ""
        lines.append(f"{codes[0]}{variants} — {name}")
    return "\n".join(lines)


def load_articles():
    """Справочник с GitHub, кэш на 10 минут; при ошибке — последняя удачная версия."""
    with _articles_lock:
        if time.time() - _articles_cache["loaded_at"] < ARTICLES_CACHE_SECONDS:
            return _articles_cache["text"]
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
            _articles_cache["text"] = format_articles(r.text)
            _articles_cache["loaded_at"] = time.time()
            logger.info("Справочник артикулов загружен: %s моделей",
                        len(_articles_cache["text"].splitlines()))
        except Exception:
            logger.exception("Не удалось загрузить справочник артикулов с GitHub")
        return _articles_cache["text"]


def build_system_prompt():
    articles = load_articles() or "(справочник сейчас недоступен — пиши только коды)"
    return SYSTEM_PROMPT.replace("{ARTICLES}", articles)


def is_allowed(chat_id):
    if ALLOWED_CHAT_IDS is None:
        return True
    return chat_id in ALLOWED_CHAT_IDS


def is_mentioned(message):
    """В личке — всегда True. В группе — только если бота явно упомянули
    в подписи/тексте (@username) или ответили (reply) на его сообщение."""
    if message["chat"].get("type") not in ("group", "supergroup"):
        return True

    # Reply на сообщение бота
    reply = message.get("reply_to_message")
    if reply and reply.get("from", {}).get("username", "").lower() == BOT_USERNAME:
        return True

    if not BOT_USERNAME:
        # Username не задан — не можем проверить упоминание, пропускаем всё
        # (чтобы не сломать работу бота, если переменную забыли задать)
        return True

    # Упоминание в подписи к фото или в тексте сообщения
    text = (message.get("caption") or message.get("text") or "").lower()
    return f"@{BOT_USERNAME}" in text


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
        r = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text[i:i + 4000]},
            timeout=30,
        )
        if not r.ok:
            logger.error("Telegram sendMessage failed: %s %s", r.status_code, r.text)


def get_file_bytes(file_id):
    r = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}, timeout=30).json()
    file_path = r["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    return requests.get(file_url, timeout=30).content


def process_photos(chat_id, file_ids):
    """Считает одну или несколько фотографий вместе и присылает готовый отчёт."""
    try:
        # Держим индикатор "печатает" живым (сам статус живёт ~5 сек в Telegram)
        stop_typing = threading.Event()

        def typing_loop():
            while not stop_typing.is_set():
                send_chat_action(chat_id, "typing")
                stop_typing.wait(4)

        t = threading.Thread(target=typing_loop, daemon=True)
        t.start()

        content = []
        for file_id in file_ids:
            img_bytes = get_file_bytes(file_id)
            img_b64 = base64.b64encode(img_bytes).decode()
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64},
            })

        prompt_text = (
            "Посчитай эту таблицу по методике и выдай готовый отчёт."
            if len(content) == 1
            else f"Это {len(content)} фото одного набора данных (части одной таблицы или "
                 f"несколько накладных). Посчитай их вместе по методике и выдай один "
                 f"общий готовый отчёт."
        )
        content.append({"type": "text", "text": prompt_text})

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8000,
            thinking={"type": "enabled", "budget_tokens": 4000},
            system=build_system_prompt(),
            messages=[{"role": "user", "content": content}],
        )
        result_text = "".join(block.text for block in response.content if block.type == "text")

        stop_typing.set()
        send_message(chat_id, result_text)
    except Exception:
        logger.exception("Ошибка при обработке фото")
        send_message(chat_id, "Не получилось обработать фото, попробуйте прислать ещё раз.")


def schedule_media_group(media_group_id):
    """Ждём немного, собираем все фото альбома, затем считаем разом."""
    time.sleep(MEDIA_GROUP_WAIT_SECONDS)
    with _media_groups_lock:
        group = _media_groups.pop(media_group_id, None)
    if not group or not group["mentioned"]:
        return
    process_photos(group["chat_id"], group["file_ids"])


@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}
    message = update.get("message") or update.get("channel_post")
    if not message:
        return "ok"

    chat_id = message["chat"]["id"]
    logger.info("Incoming message: chat_id=%s chat_type=%s has_photo=%s",
                chat_id, message["chat"].get("type"), "photo" in message)

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
    if not photos:
        return "ok"

    largest = photos[-1]  # Telegram присылает несколько размеров, берём самый большой
    media_group_id = message.get("media_group_id")

    if media_group_id:
        # Часть альбома — копим фото и запускаем таймер один раз на группу.
        # Подпись с упоминанием обычно есть только у одного фото из альбома,
        # поэтому достаточно, чтобы упоминание нашлось хотя бы у одного.
        with _media_groups_lock:
            group = _media_groups.get(media_group_id)
            if group is None:
                group = {"chat_id": chat_id, "file_ids": [], "mentioned": False}
                _media_groups[media_group_id] = group
                threading.Thread(
                    target=schedule_media_group, args=(media_group_id,), daemon=True
                ).start()
            group["file_ids"].append(largest["file_id"])
            if mentioned:
                group["mentioned"] = True
        return "ok"

    if not mentioned:
        return "ok"

    # Одиночное фото — считаем сразу в фоновом потоке, чтобы не держать вебхук
    threading.Thread(target=process_photos, args=(chat_id, [largest["file_id"]]), daemon=True).start()
    return "ok"


@app.route("/", methods=["GET"])
def health():
    return "OTK bot is running"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
