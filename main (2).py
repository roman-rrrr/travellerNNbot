import os
import logging
import threading
import time
import json
import uuid
import re
from typing import Dict, Any, Optional
import base64

import requests
import telebot
from telebot import types
from dotenv import load_dotenv

load_dotenv()

# ---------------- Configuration ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
DGIS_API_KEY = os.getenv("DGIS_API_KEY")
GIGACHAT_CLIENT_ID = os.getenv("GIGACHAT_CLIENT_ID")
GIGACHAT_CLIENT_SECRET = os.getenv("GIGACHAT_CLIENT_SECRET")
RADIUS = int(os.getenv("RADIUS", "1500"))
REQUEST_TIMEOUT = 10
GIGACHAT_TIMEOUT = 30
MAX_RESULTS = 50
MAX_GIGACHAT_PLACES = 20
RETRY_ATTEMPTS = 3
BACKOFF_FACTOR = 0.5
TG_MSG_LIMIT = 3000

if not BOT_TOKEN or not DGIS_API_KEY or not GIGACHAT_CLIENT_ID or not GIGACHAT_CLIENT_SECRET:
    raise SystemExit(
        "Missing environment variables: set BOT_TOKEN, DGIS_API_KEY, GIGACHAT_CLIENT_ID, and GIGACHAT_CLIENT_SECRET")

auth_string = f"{GIGACHAT_CLIENT_ID}:{GIGACHAT_CLIENT_SECRET}"
auth_b64 = base64.b64encode(auth_string.encode()).decode()

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
logger.info("🤖 TravelerNN bot started. Waiting for commands...")

# ---------------- Bot and State ----------------
bot = telebot.TeleBot(BOT_TOKEN)
user_data: Dict[int, Dict[str, Any]] = {}
user_lock = threading.Lock()

gigachat_token: Optional[str] = None
token_expiry: float = 0
token_lock = threading.Lock()


# ---------------- GigaChat Auth ----------------
def get_gigachat_token() -> Optional[str]:
    global gigachat_token, token_expiry
    with token_lock:
        if gigachat_token and time.time() < token_expiry:
            return gigachat_token

        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'application/json',
            'RqUID': str(uuid.uuid4()),
            'Authorization': f'Basic {auth_b64}'
        }
        data = {'scope': 'GIGACHAT_API_PERS'}

        try:
            resp = requests.post(
                'https://ngw.devices.sberbank.ru:9443/api/v2/oauth',
                headers=headers,
                data=data,
                timeout=REQUEST_TIMEOUT,
                verify=False
            )
            resp.raise_for_status()
            token_data = resp.json()
            gigachat_token = token_data.get('access_token')
            expires_in = token_data.get('expires_in', 1800)
            token_expiry = time.time() + expires_in - 60
            return gigachat_token
        except Exception as e:
            logger.error("❌ Failed to get GigaChat token: %s", e)
            return None


# ---------------- Helper Functions ----------------
def fetch_with_retries(session: requests.Session, url: str, params: dict) -> Optional[dict]:
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.Timeout:
            logger.warning("⏱ Timeout on attempt %s for params=%s", attempt, params)
        except requests.RequestException as e:
            logger.warning("⚠️ HTTP error on attempt %s: %s", attempt, e)
        time.sleep(BACKOFF_FACTOR * attempt)
    return None


def call_gigachat(prompt: str, max_tokens: int = 1500) -> Optional[str]:
    token = get_gigachat_token()
    if not token:
        return None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    data = {
        "model": "GigaChat-2-Max",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens
    }
    try:
        resp = requests.post(
            "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            json=data,
            headers=headers,
            timeout=GIGACHAT_TIMEOUT,
            verify=False
        )
        resp.raise_for_status()
        result = resp.json()
        return result.get("choices", [{}])[0].get("message", {}).get("content")
    except Exception as e:
        logger.error("❌ GigaChat API error: %s", e)
        return None


def get_search_plan(prompt: str) -> Optional[Dict[str, Any]]:
    plan_prompt = f"""
Проанализируй запрос пользователя: '{prompt}'.
Составь список поисковых запросов для 2GIS API, подходящих для туристической прогулки.
Выбирай только интересные туристические места: парки, музеи, площади, театры, исторические и архитектурные достопримечательности.
Не включай школы, офисы, магазины, автомобили, мемориальные доски без туристического интереса.
Верни JSON: {{"radius": "{RADIUS}", "queries": ["запрос1","запрос2"]}}
"""
    response = call_gigachat(plan_prompt)
    if response:
        try:
            json_start = response.find('{')
            json_end = response.rfind('}') + 1
            if json_start != -1 and json_end != 0:
                return json.loads(response[json_start:json_end])
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("⚠️ Failed to parse GigaChat response: %s, error: %s", response, e)
    return None


def split_message(msg: str) -> list:
    parts = []
    while len(msg) > TG_MSG_LIMIT:
        split_idx = msg.rfind("\n", 0, TG_MSG_LIMIT)
        if split_idx == -1:
            split_idx = TG_MSG_LIMIT
        parts.append(msg[:split_idx])
        msg = msg[split_idx:]
    if msg:
        parts.append(msg)
    return parts


# ---------------- Bot Handlers ----------------
@bot.message_handler(commands=['start'])
def start_cmd(message: types.Message):
    bot.send_message(message.chat.id,
                     "Привет! Чем ты увлекаешься? (например: история, архитектура, парки, музеи, панорамы и т.д.)")


@bot.message_handler(func=lambda m: m.chat.id not in user_data or 'prompt' not in user_data.get(m.chat.id, {}),
                     content_types=['text'])
def receive_prompt(message: types.Message):
    chat_id = message.chat.id
    prompt = message.text.strip()
    if not prompt or prompt.startswith('/'):
        bot.send_message(chat_id, "Некорректный запрос. Попробуйте снова.")
        return

    with user_lock:
        user_data[chat_id] = {'prompt': prompt}

    markup = types.ReplyKeyboardMarkup(row_width=2, one_time_keyboard=True, resize_keyboard=True)
    markup.add(
        types.KeyboardButton("30 минут"),
        types.KeyboardButton("1 час"),
        types.KeyboardButton("2 часа"),
        types.KeyboardButton("3 часа"),
        types.KeyboardButton("Ввести своё время")
    )
    bot.send_message(chat_id, "Сколько времени вы планируете потратить на прогулку?", reply_markup=markup)


@bot.message_handler(func=lambda m: m.chat.id in user_data and 'duration' not in user_data.get(m.chat.id, {}),
                     content_types=['text'])
def receive_duration(message: types.Message):
    chat_id = message.chat.id
    text = message.text.strip().lower()

    predefined_times = {"30 минут": 0.5, "1 час": 1, "2 часа": 2, "3 часа": 3}

    if text in predefined_times:
        duration_hours = predefined_times[text]
    else:
        match = re.search(r"[\d,.]+", text)
        if match:
            duration_hours = float(match.group().replace(',', '.'))
        else:
            duration_hours = 1

    with user_lock:
        user_data[chat_id]['duration'] = duration_hours

    bot.send_message(
        chat_id,
        f"Отлично! Выделено {duration_hours} ч. Теперь отправьте геопозицию 📍",
        reply_markup=types.ReplyKeyboardRemove()
    )


@bot.message_handler(content_types=['location'])
def receive_location(message: types.Message):
    chat_id = message.chat.id
    lat = message.location.latitude
    lon = message.location.longitude

    with user_lock:
        state = user_data.get(chat_id)

    if not state or 'prompt' not in state or 'duration' not in state:
        bot.send_message(chat_id, "Сначала отправьте интерес и время прогулки.")
        return

    prompt = state['prompt']
    duration_hours = state['duration']

    # 🕓 Сообщение о генерации маршрута
    generating_msg = bot.send_message(chat_id, "⏳ Идёт генерация маршрута, подождите немного...")

    try:
        plan = get_search_plan(prompt)
        if not plan or not plan.get('queries'):
            queries = [word for word in prompt.split() if len(word) > 3] or [prompt]
            plan = {'radius': RADIUS, 'queries': queries}

        all_items = []
        url = "https://catalog.api.2gis.com/3.0/items"
        base_params = {"point": f"{lon},{lat}", "radius": plan['radius'], "key": DGIS_API_KEY}

        with requests.Session() as session:
            for query in plan['queries']:
                params = base_params.copy()
                params["q"] = query
                params["page_size"] = 10
                params["page"] = 1
                while True:
                    data = fetch_with_retries(session, url, params)
                    if not data:
                        break
                    items = data.get("result", {}).get("items", [])
                    if not items:
                        break
                    all_items.extend(items)
                    if len(items) < 10:
                        break
                    params["page"] += 1

        SKIP_KEYWORDS = [
            "школа", "дворец бракосочетания", "автомобиль", "магазин",
            "комиссионный", "пианино", "куб", "офис", "банк", "салон",
            "аптека", "ремонт", "ритуал", "похорон", "гроб", "надгроб", "кладбищ", "кремаци",
            "офис", "магазин", "услуги", "салон", "агентств", "нотариус", "юрист", "страхов", "строй", "ветеринар",
            "аптек", "тату", "медицин", "клиник", "авто", "аренда", "оптика",
            "фастфуд", "строй", "атракцион", "лабиринт", "музей иллюзий", "побег из тюрьмы"
        ]

        all_items = [item for item in all_items if not any(skip in item.get("name", "").lower() for skip in SKIP_KEYWORDS)]

        if not all_items:
            bot.edit_message_text("😔 Не удалось найти подходящие туристические места рядом.", chat_id, generating_msg.message_id)
            return

        seen = set()
        unique_items = []
        for i in all_items:
            name = i.get("name", "")
            if name not in seen:
                seen.add(name)
                unique_items.append(i)
            if len(unique_items) >= MAX_RESULTS:
                break


        names = [p.get("name", "Без названия") for p in unique_items[:MAX_GIGACHAT_PLACES]]

        route_prompt = f"""
        Ты — эксперт по пешеходным маршрутам по Нижнему Новгороду.
        Пользователь хочет прогулку по туристическим местам города.
        Он находится на координатах {lat},{lon}, приехал из другого города и хочет провести прогулку длительностью {duration_hours} часов.
        Его запрос: '{prompt}'
        Найденные места для маршрута: {', '.join(names)}

        Твоя задача — составить подробный, логичный и интересный пешеходный маршрут.

        ОБЯЗАТЕЛЬНЫЕ УСЛОВИЯ:
        - Если среди найденных мест встречаются разные категории (например, музеи, парки, театры, памятники, храмы и т.п.), маршрут обязан содержать минимум по одному месту из каждой категории. 
          Например, если пользователь указал «музеи и парки», маршрут должен включать и музеи, и парки, а не что-то одно.
        - Используй только те объекты, которые реально существуют и указаны в списке найденных мест.
        - Не выдумывай названия, описания или объекты.
        - Исключай неинтересные, ритуальные или закрытые места.

        ТРЕБОВАНИЯ К СТРУКТУРЕ МАРШРУТА:
        1. Определи оптимальный порядок посещения всех выбранных мест — чтобы переходы были логичными и маршрут выглядел естественно.
        2. Укажи:
           - время перехода между точками (учитывай спокойный темп прогулки),
           - время пребывания на каждом месте,
           - краткое описание,
           - почему это место может быть интересно пользователю.
        3. Делай текст живым и приятным: создавай ощущение увлекательной прогулки.
        Ответ дай на русском языке.
        """

        route = None
        for attempt in range(3):
            route = call_gigachat(route_prompt, max_tokens=2500)
            if route:
                break
            time.sleep(2)

        # 🧹 Удаляем сообщение “идет генерация” после завершения
        bot.delete_message(chat_id, generating_msg.message_id)

        if route:
            for part in split_message(route):
                bot.send_message(chat_id, part)
        else:
            bot.send_message(chat_id, "❌ Не удалось составить маршрут. Попробуйте снова позже.")

    finally:
        with user_lock:
            user_data.pop(chat_id, None)


# ---------------- Run ----------------
if __name__ == "__main__":
    bot.remove_webhook()  # очистить webhook перед polling
    time.sleep(1)
    bot.infinity_polling()

