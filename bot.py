import os
import sqlite3
from datetime import datetime, timedelta, UTC
import telebot
from telebot import types
from dotenv import dotenv_values
import requests

config = dotenv_values(".env")

# ID администратора
ADMIN_ID = int(config.get("ADMIN_ID") or config.get("ADMIN_ID_SECRET") or 0)

# Токен бота
TOKEN = config["TELEGRAM_BOT_TOKEN"]

if not TOKEN:
    raise RuntimeError("Не задан токен бота в .env (ключ TELEGRAM_BOT_TOKEN).")

if not ADMIN_ID:
    raise RuntimeError("Не задан ID администратора в .env (ключ ADMIN_ID или ADMIN_ID_SECRET).")

bot = telebot.TeleBot(TOKEN)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bot_stats.db")



# YandexGPT API
YANDEX_FOLDER_ID = (config.get("YANDEX_FOLDER_ID") or os.environ.get("YANDEX_FOLDER_ID") or "").strip()
YANDEX_API_KEY = (config.get("YANDEX_API_KEY") or os.environ.get("YANDEX_API_KEY") or "").strip()
# Исправленный endpoint для YandexGPT
YANDEXGPT_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"


# --- База данных ---

def init_db() -> None:
    """Создаёт таблицы для статистики, если их ещё нет."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id        INTEGER PRIMARY KEY,
                username       TEXT,
                first_name     TEXT,
                last_name      TEXT,
                first_seen     TEXT,
                last_seen      TEXT,
                total_messages INTEGER DEFAULT 0,
                about_clicks   INTEGER DEFAULT 0,
                cases_clicks   INTEGER DEFAULT 0
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS interactions (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                button  TEXT,
                ts      TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS applications (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                username    TEXT,
                first_name  TEXT,
                last_name   TEXT,
                phone       TEXT NOT NULL,
                created_at  TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS monthly_stats_saves (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                year        INTEGER NOT NULL,
                month       INTEGER NOT NULL,
                saved_at    TEXT NOT NULL,
                UNIQUE(year, month)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

def _now_iso() -> str:
    """Текущее время в ISO-формате (UTC)."""
    return datetime.now(UTC).isoformat()


def track_user_interaction(message, button: str | None = None) -> None:
    """
    Сохраняет/обновляет информацию о пользователе и считает нажатия кнопок.

    button:
        - "about"  — нажата кнопка «О нас»
        - "cases"  — нажата кнопка «Кейсы»
        - None     — любое другое сообщение (включая /start)
    """
    user = message.from_user

    about_inc = 1 if button == "about" else 0
    cases_inc = 1 if button == "cases" else 0
    button_label = button or "other"

    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        now = _now_iso()

        # Логируем каждое взаимодействие
        cur.execute(
            "INSERT INTO interactions (user_id, button, ts) VALUES (?, ?, ?)",
            (user.id, button_label, now),
        )

        # Обновляем агрегированную таблицу пользователей
        cur.execute(
            """
            INSERT INTO users (
                user_id, username, first_name, last_name,
                first_seen, last_seen,
                total_messages, about_clicks, cases_clicks
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username       = excluded.username,
                first_name     = excluded.first_name,
                last_name      = excluded.last_name,
                last_seen      = excluded.last_seen,
                total_messages = users.total_messages + 1,
                about_clicks   = users.about_clicks + excluded.about_clicks,
                cases_clicks   = users.cases_clicks + excluded.cases_clicks
            """,
            (
                user.id,
                user.username,
                user.first_name,
                user.last_name,
                now,
                now,
                about_inc,
                cases_inc,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_stats() -> tuple[int, int, int, int]:
    """
    Возвращает агрегированную статистику за всё время:
    (кол-во пользователей, нажатий «О нас», нажатий «Кейсы», общее кол-во сообщений).
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        total_users = cur.fetchone()[0] or 0

        cur.execute(
            "SELECT SUM(about_clicks), SUM(cases_clicks), SUM(total_messages) FROM users"
        )
        row = cur.fetchone()
        about_clicks = row[0] or 0
        cases_clicks = row[1] or 0
        total_messages = row[2] or 0

        return int(total_users), int(about_clicks), int(cases_clicks), int(total_messages)
    finally:
        conn.close()


def get_month_stats(days: int = 30) -> tuple[int, int, int, int]:
    """
    Статистика за последние `days` дней (по умолчанию 30):
    (кол-во пользователей, нажатий «О нас», нажатий «Кейсы», общее кол-во сообщений).
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    cutoff_iso = cutoff.isoformat()

    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()

        # Сколько уникальных пользователей взаимодействовали за период
        cur.execute(
            "SELECT COUNT(DISTINCT user_id) FROM interactions WHERE ts >= ?",
            (cutoff_iso,),
        )
        total_users = cur.fetchone()[0] or 0

        # Клики по кнопкам и общее количество событий
        cur.execute(
            """
            SELECT
                SUM(CASE WHEN button = 'about' THEN 1 ELSE 0 END) AS about_clicks,
                SUM(CASE WHEN button = 'cases' THEN 1 ELSE 0 END) AS cases_clicks,
                COUNT(*) AS total_messages
            FROM interactions
            WHERE ts >= ?
            """,
            (cutoff_iso,),
        )
        row = cur.fetchone()
        about_clicks = row[0] or 0
        cases_clicks = row[1] or 0
        total_messages = row[2] or 0

        return int(total_users), int(about_clicks), int(cases_clicks), int(total_messages)
    finally:
        conn.close()


def save_application(user, phone: str) -> None:
    """
    Сохраняет заявку в БД и отправляет уведомление администратору.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        now = _now_iso()
        cur.execute(
            """
            INSERT INTO applications (user_id, username, first_name, last_name, phone, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user.id,
                user.username,
                user.first_name,
                user.last_name,
                phone,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # Отправляем уведомление администратору
    admin_message = (
        "🔔 *Новая заявка*\n\n"
        f"Пользователь оставил заявку на получение продукта.\n\n"
        f"👤 *Информация о пользователе:*\n"
        f"ID: `{user.id}`\n"
        f"Имя: {user.first_name or 'не указано'}\n"
        f"Фамилия: {user.last_name or 'не указано'}\n"
        f"Username: @{user.username or 'не указан'}\n\n"
        f"📞 *Телефон:* `{phone}`\n\n"
        f"⏰ Время заявки: {now}\n\n"
        f"Пожалуйста, свяжитесь с клиентом как можно скорее!"
    )
    bot.send_message(ADMIN_ID, admin_message, parse_mode="Markdown")


def is_phone_number(text: str) -> bool:
    """
    Проверяет, похож ли текст на номер телефона.
    """
    # Убираем все пробелы, дефисы, скобки и плюсы
    cleaned = "".join(c for c in text if c.isdigit() or c == "+")
    # Проверяем, что осталось достаточно цифр (минимум 10)
    digits = "".join(c for c in cleaned if c.isdigit())
    return len(digits) >= 10


# --- Исправленная функция для YandexGPT ---
def ask_yandexgpt(user_message: str) -> tuple[str | None, str | None]:
    """
    Отправляет вопрос пользователя в YandexGPT и возвращает (ответ, ошибка).
    """
    if not YANDEX_FOLDER_ID or not YANDEX_API_KEY:
        return None, None

    system_prompt = (
        "Ты дружелюбный помощник в телеграм-боте компании. "
        "Отвечай кратко и по делу на русском языке. "
        "Если вопрос не по теме компании или продукта, вежливо ответь и предложи вернуться к кнопкам бота (О нас, Кейсы)."
    )

    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "Content-Type": "application/json",
    }

    # Исправленная структура payload
    payload = {
        "modelUri": f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/latest",
        "messages": [
            {"role": "system", "text": system_prompt},
            {"role": "user", "text": user_message},
        ],
        "generationOptions": {
            "maxTokens": 1000,
            "temperature": 0.6,
        },
    }

    try:
        r = requests.post(YANDEXGPT_URL, headers=headers, json=payload, timeout=30)
        if r.status_code != 200:
            err_body = r.text
            try:
                err_body = r.json()
            except Exception:
                pass
            print(f"[YandexGPT] Ошибка HTTP {r.status_code}: {err_body}", flush=True)
            if r.status_code == 401:
                return None, "Неверный API-ключ Yandex Cloud. Проверьте YANDEX_API_KEY в .env."
            if r.status_code == 403:
                return None, "Доступ к YandexGPT запрещён. Проверьте права ключа и folder_id в консоли Yandex Cloud."
            if r.status_code == 429:
                return None, "Слишком много запросов к YandexGPT. Подождите и попробуйте снова."
            if r.status_code == 404:
                return None, "Сервис YandexGPT временно недоступен (404). Проверьте доступность API в консоли Yandex Cloud."
            return None, None

        data = r.json()
        # Исправленная обработка ответа
        reply = data.get("result", {}).get("alternatives", [])
        if reply and len(reply) > 0:
            text = reply[0].get("message", {}).get("text", "").strip()
            return text or None, None
        return None, None

    except requests.exceptions.RequestException as e:
        print(f"[YandexGPT] Ошибка запроса: {e}", flush=True)
        return None, None


def get_month_stats_for_period(year: int, month: int) -> tuple[int, int, int, int]:
    """
    Получает статистику за конкретный месяц и год.
    Возвращает: (кол-во пользователей, нажатий «О нас», нажатий «Кейсы», общее кол-во сообщений).
    """
    # Определяем начало и конец месяца
    if month == 12:
        start_date = datetime(year, month, 1)
        end_date = datetime(year + 1, 1, 1)
    else:
        start_date = datetime(year, month, 1)
        end_date = datetime(year, month + 1, 1)
    
    start_iso = start_date.isoformat()
    end_iso = end_date.isoformat()

    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()

        # Сколько уникальных пользователей взаимодействовали за период
        cur.execute(
            "SELECT COUNT(DISTINCT user_id) FROM interactions WHERE ts >= ? AND ts < ?",
            (start_iso, end_iso),
        )
        total_users = cur.fetchone()[0] or 0

        # Клики по кнопкам и общее количество событий
        cur.execute(
            """
            SELECT
                SUM(CASE WHEN button = 'about' THEN 1 ELSE 0 END) AS about_clicks,
                SUM(CASE WHEN button = 'cases' THEN 1 ELSE 0 END) AS cases_clicks,
                COUNT(*) AS total_messages
            FROM interactions
            WHERE ts >= ? AND ts < ?
            """,
            (start_iso, end_iso),
        )
        row = cur.fetchone()
        about_clicks = row[0] or 0
        cases_clicks = row[1] or 0
        total_messages = row[2] or 0

        return int(total_users), int(about_clicks), int(cases_clicks), int(total_messages)
    finally:
        conn.close()


def save_monthly_stats_to_file(year: int, month: int) -> bool:
    """
    Сохраняет статистику за указанный месяц в файл statistic.txt.
    Возвращает True, если сохранение успешно, False если уже было сохранено ранее.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        # Проверяем, не сохранялась ли уже статистика за этот месяц
        cur.execute(
            "SELECT id FROM monthly_stats_saves WHERE year = ? AND month = ?",
            (year, month),
        )
        if cur.fetchone():
            return False  # Уже сохранено ранее
        
        # Получаем статистику за месяц
        total_users, about_clicks, cases_clicks, total_messages = get_month_stats_for_period(
            year, month
        )
        
        # Формируем текст для сохранения
        month_names = [
            "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
            "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"
        ]
        month_name = month_names[month - 1]
        
        stats_text = (
            f"Статистика за {month_name} {year} года\n"
            f"{'=' * 50}\n"
            f"Пользователей взаимодействовало: {total_users}\n"
            f"Нажатий «О нас»: {about_clicks}\n"
            f"Нажатий «Кейсы»: {cases_clicks}\n"
            f"Всего сообщений: {total_messages}\n"
            f"{'=' * 50}\n\n"
        )
        
        # Сохраняем в файл
        stats_file_path = os.path.join(BASE_DIR, "statistic.txt")
        with open(stats_file_path, "a", encoding="utf-8") as f:
            f.write(stats_text)
        
        # Отмечаем, что статистика сохранена
        now = _now_iso()
        cur.execute(
            "INSERT INTO monthly_stats_saves (year, month, saved_at) VALUES (?, ?, ?)",
            (year, month, now),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def check_and_save_monthly_stats() -> None:
    """
    Проверяет, является ли сегодня 1-е число месяца, и если да,
    сохраняет статистику за предыдущий месяц в файл.
    """
    today = datetime.utcnow()
    
    # Проверяем, является ли сегодня 1-е число месяца
    if today.day != 1:
        return
    
    # Определяем предыдущий месяц
    if today.month == 1:
        prev_month = 12
        prev_year = today.year - 1
    else:
        prev_month = today.month - 1
        prev_year = today.year
    
    # Сохраняем статистику за предыдущий месяц
    save_monthly_stats_to_file(prev_year, prev_month)


@bot.message_handler(commands=["start"])
def send_welcome(message):
    """Обработчик команды /start: приветствие и показ клавиатуры."""
    track_user_interaction(message, button=None)

    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn_about = types.KeyboardButton("О нас")
    btn_cases = types.KeyboardButton("Кейсы")

    # Кнопка "Статистика" доступна только администратору
    if message.from_user.id == ADMIN_ID:
        btn_stats = types.KeyboardButton("Статистика")
        keyboard.row(btn_about, btn_cases, btn_stats)
    else:
        keyboard.add(btn_about, btn_cases)

    bot.send_message(
        message.chat.id,
        "Привет! Я бот компании.\nВыбери нужный раздел на клавиатуре.",
        reply_markup=keyboard,
    )


@bot.message_handler(commands=["stats"])
def send_stats(message):
    """Показывает простую статистику по пользователям и нажатиям кнопок."""
    total_users, about_clicks, cases_clicks, total_messages = get_stats()

    text = (
        "📊 *Статистика бота*\n\n"
        f"Всего пользователей: *{total_users}*\n"
        f"Нажатий кнопки «О нас»: *{about_clicks}*\n"
        f"Нажатий кнопки «Кейсы»: *{cases_clicks}*\n"
        f"Всего сообщений: *{total_messages}*"
    )

    bot.send_message(message.chat.id, text, parse_mode="Markdown")


@bot.message_handler(content_types=["contact"])
def handle_contact(message):
    """Обработчик отправки контакта (номера телефона)."""
    if message.contact and message.contact.phone_number:
        phone = message.contact.phone_number
        user = message.from_user
        
        # Сохраняем заявку
        save_application(user, phone)
        
        # Благодарим пользователя
        bot.send_message(
            message.chat.id,
            "✅ Спасибо за ваше обращение!\n\n"
            "Мы получили вашу заявку и свяжемся с вами в ближайшее время.",
        )
        
        # Возвращаем обычную клавиатуру
        keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
        btn_about = types.KeyboardButton("О нас")
        btn_cases = types.KeyboardButton("Кейсы")
        if message.from_user.id == ADMIN_ID:
            btn_stats = types.KeyboardButton("Статистика")
            keyboard.row(btn_about, btn_cases, btn_stats)
        else:
            keyboard.add(btn_about, btn_cases)
        
        bot.send_message(
            message.chat.id,
            "Выберите нужный раздел на клавиатуре.",
            reply_markup=keyboard,
        )


@bot.message_handler(content_types=["text"])
def handle_text(message):
    """Обработка нажатий на кнопки и текстовых сообщений."""
    text = message.text.strip()

    if text == "О нас":
        track_user_interaction(message, button="about")
        bot.send_message(
            message.chat.id,
            "🧾 *О нас*\n\n"
            "Мы создаём телеграм-ботов и автоматизируем бизнес-процессы.\n"
            "Помогаем компаниям экономить время и увеличивать продажи.",
            parse_mode="Markdown",
        )
    elif text == "Кейсы":
        track_user_interaction(message, button="cases")
        # Показываем информацию о кейсах и предлагаем оставить заявку
        cases_info = (
            "📌 *Кейсы*\n\n"
            "1. Бот для поддержки клиентов — сократил нагрузку на операторов на 40%.\n"
            "2. Бот для заявок в отдел продаж — ускорил обработку лидов в 2 раза.\n"
            "3. Внутренний бот-комбайн — автоматизировал рутинные задачи в команде.\n\n"
            "💡 *Хотите получить наш продукт?*\n\n"
            "Это очень просто! Оставьте заявку, указав ваш номер телефона, "
            "и мы свяжемся с вами в ближайшее время."
        )
        bot.send_message(
            message.chat.id,
            cases_info,
            parse_mode="Markdown",
        )
        
        # Предлагаем отправить контакт или ввести телефон вручную
        keyboard_phone = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        btn_contact = types.KeyboardButton("📞 Отправить контакт", request_contact=True)
        keyboard_phone.add(btn_contact)
        
        bot.send_message(
            message.chat.id,
            "Пожалуйста, отправьте ваш номер телефона для связи.\n"
            "Вы можете нажать кнопку ниже или ввести номер вручную.",
            reply_markup=keyboard_phone,
        )
    elif text == "Статистика":
        # Кнопка видна только администратору, но на всякий случай ещё раз проверяем
        if message.from_user.id != ADMIN_ID:
            track_user_interaction(message, button=None)
            bot.send_message(message.chat.id, "Эта функция доступна только админу.")
            return

        track_user_interaction(message, button=None)
        total_users, about_clicks, cases_clicks, total_messages = get_month_stats(
            days=30
        )
        text_stats = (
            "📊 *Статистика за последние 30 дней*\n\n"
            f"Пользователей взаимодействовало: *{total_users}*\n"
            f"Нажатий «О нас»: *{about_clicks}*\n"
            f"Нажатий «Кейсы»: *{cases_clicks}*\n"
            f"Всего сообщений: *{total_messages}*"
        )
        bot.send_message(message.chat.id, text_stats, parse_mode="Markdown")
    else:
        # Проверяем, не является ли текст номером телефона
        if is_phone_number(text):
            # Пользователь ввёл номер телефона
            user = message.from_user
            save_application(user, text)
            
            # Благодарим пользователя
            bot.send_message(
                message.chat.id,
                "✅ Спасибо за ваше обращение!\n\n"
                "Мы получили вашу заявку и свяжемся с вами в ближайшее время.",
            )
            
            # Возвращаем обычную клавиатуру
            keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
            btn_about = types.KeyboardButton("О нас")
            btn_cases = types.KeyboardButton("Кейсы")
            if message.from_user.id == ADMIN_ID:
                btn_stats = types.KeyboardButton("Статистика")
                keyboard.row(btn_about, btn_cases, btn_stats)
            else:
                keyboard.add(btn_about, btn_cases)
            
            bot.send_message(
                message.chat.id,
                "Выберите нужный раздел:",
                reply_markup=keyboard,
            )
        else:
            # Произвольный текст: если подключён YandexGPT — отвечаем через него
            track_user_interaction(message, button=None)
            if YANDEX_FOLDER_ID and YANDEX_API_KEY:
                bot.send_chat_action(message.chat.id, "typing")
                reply, err = ask_yandexgpt(text)
                if reply:
                    if len(reply) > 4000:
                        reply = reply[:3997] + "..."
                    bot.send_message(message.chat.id, reply)
                else:
                    msg = err or "Сейчас не удалось получить ответ. Попробуйте позже или выберите кнопку: «О нас» или «Кейсы»."
                    bot.send_message(message.chat.id, msg)
            else:
                bot.send_message(
                    message.chat.id,
                    "Я тебя не понял. Пожалуйста, выбери одну из кнопок: «О нас» или «Кейсы».",
                )


if __name__ == "__main__":
    init_db()
    # Проверяем и сохраняем статистику за предыдущий месяц, если сегодня 1-е число
    check_and_save_monthly_stats()
    print("Бот запущен. Нажми Ctrl+C, чтобы остановить.")
    # Не обрабатывать сообщения, отправленные пока бот был выключен
    bot.infinity_polling(skip_pending=True)
