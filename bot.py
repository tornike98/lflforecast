import os
import re
import logging
import psycopg2
from contextlib import closing
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Updater, CommandHandler, CallbackQueryHandler,
                          MessageHandler, Filters, CallbackContext)
from dotenv import load_dotenv

# Загружаем переменные окружения из .env
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")  # Пример: postgresql://username:password@host:port/dbname
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

# Настройка логирования
logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)

# Функция подключения к базе данных PostgreSQL
def connect_db():
    return psycopg2.connect(DATABASE_URL)

# Инициализация таблиц (вызывается один раз при старте)
def init_db():
    with closing(connect_db()) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    points INT DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS matches (
                    match_id SERIAL PRIMARY KEY,
                    match_name TEXT,
                    result TEXT DEFAULT NULL,
                    is_active BOOLEAN DEFAULT TRUE
                );
                CREATE TABLE IF NOT EXISTS predictions (
                    user_id BIGINT REFERENCES users(user_id),
                    match_id INT REFERENCES matches(match_id),
                    score TEXT,
                    PRIMARY KEY (user_id, match_id)
                );
            """)
            conn.commit()

# Главное меню – 4 кнопки
def show_main_menu(update: Update, context: CallbackContext):
    keyboard = [
        [InlineKeyboardButton("Мой профиль", callback_data='profile')],
        [InlineKeyboardButton("Просмотр матчей", callback_data='view_matches')],
        [InlineKeyboardButton("Просмотр моего прогноза", callback_data='view_my_prediction')],
        [InlineKeyboardButton("Таблица лидеров", callback_data='leaderboard')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text("Выберите действие:", reply_markup=reply_markup)

# Обработка команды /start
def start(update: Update, context: CallbackContext):
    user = update.message.from_user
    user_id = user.id
    # Добавляем пользователя, если его ещё нет
    with closing(connect_db()) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING;", (user_id,))
        conn.commit()
    # Проверяем, задано ли имя
    with closing(connect_db()) as conn, conn.cursor() as cur:
        cur.execute("SELECT username FROM users WHERE user_id = %s;", (user_id,))
        result = cur.fetchone()
    if result is None or result[0] is None:
        update.message.reply_text("Добро пожаловать! Пожалуйста, введите ваше имя:")
        context.user_data['awaiting_name'] = True
    else:
        show_main_menu(update, context)

# Обработка ввода текста (для ввода имени или прогнозов)
def handle_text(update: Update, context: CallbackContext):
    if context.user_data.get('awaiting_name'):
        name = update.message.text.strip()
        user_id = update.message.from_user.id
        with closing(connect_db()) as conn, conn.cursor() as cur:
            cur.execute("UPDATE users SET username = %s WHERE user_id = %s;", (name, user_id))
            conn.commit()
        update.message.reply_text(f"Спасибо, {name}!")
        context.user_data['awaiting_name'] = False
        show_main_menu(update, context)
        return
    if context.user_data.get('awaiting_prediction'):
        process_prediction_input(update, context)
        return
    update.message.reply_text("Пожалуйста, используйте кнопки для навигации.")

# Обработка нажатий кнопок (callback_query)
def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    data = query.data
    if data == 'profile':
        show_profile(query, context)
    elif data == 'view_matches':
        start_prediction(query, context)
    elif data == 'view_my_prediction':
        show_my_prediction(query, context)
    elif data == 'leaderboard':
        show_leaderboard(query, context)

    # Функция показа профиля (с позицией в таблице лидеров)
    def show_profile(query, context: CallbackContext):
        user_id = query.from_user.id
        with closing(connect_db()) as conn, conn.cursor() as cur:
            cur.execute("SELECT username, points FROM users WHERE user_id = %s;", (user_id,))
            user_info = cur.fetchone()
            if not user_info:
                query.message.reply_text("Профиль не найден.")
                return
            username, points = user_info
            cur.execute("SELECT user_id FROM users ORDER BY points DESC;")
            all_users = cur.fetchall()
        rank = 1
        for row in all_users:
            if row[0] == user_id:
                break
            rank += 1
        message = (f"Профиль:\nИмя: {username}\nID: {user_id}\nОчки: {points}\n"
                   f"Место в таблице лидеров: {rank}")
        query.message.reply_text(message)

    # Функция начала последовательного ввода прогнозов для матчей
    def start_prediction(query, context: CallbackContext):
        user_id = query.from_user.id
        with closing(connect_db()) as conn, conn.cursor() as cur:
            cur.execute("SELECT match_id, match_name FROM matches WHERE is_active = TRUE ORDER BY match_id;")
            matches = cur.fetchall()
        if not matches:
            query.message.reply_text("Нет доступных матчей для прогнозов.")
            return
        context.user_data['matches'] = matches
        context.user_data['current_match_index'] = 0
        context.user_data['awaiting_prediction'] = True
        first_match = matches[0]
        query.message.reply_text(f"Введите прогноз для матча: {first_match[1]}\nФормат: X-Y (например, 2-1)")

    # Функция обработки ввода прогноза для текущего матча
    def process_prediction_input(update: Update, context: CallbackContext):
        user_id = update.message.from_user.id
        text = update.message.text.strip()
        if not text.replace("-", "").isdigit() or "-" not in text:
            update.message.reply_text("Некорректный счёт, введите в формате 2-1.")
            return
        if 'matches' not in context.user_data or 'current_match_index' not in context.user_data:
            update.message.reply_text("Ошибка: нет активного списка матчей.")
            return
        matches = context.user_data['matches']
        idx = context.user_data['current_match_index']
        current_match = matches[idx]
        match_id, match_name = current_match
        with closing(connect_db()) as conn, conn.cursor() as cur:
            cur.execute("SELECT score FROM predictions WHERE user_id = %s AND match_id = %s;", (user_id, match_id))
            if cur.fetchone():
                update.message.reply_text(
                    "Вы уже сделали прогноз на этот матч. Если хотите посмотреть его, нажмите кнопку «Просмотр моего прогноза».")
                context.user_data['awaiting_prediction'] = False
                return
            cur.execute("INSERT INTO predictions (user_id, match_id, score) VALUES (%s, %s, %s);",
                        (user_id, match_id, text))
            conn.commit()
        idx += 1
        if idx < len(matches):
            context.user_data['current_match_index'] = idx
            next_match = matches[idx]
            update.message.reply_text(f"Теперь введите прогноз для матча: {next_match[1]}\nФормат: X-Y (например, 2-1)")
        else:
            update.message.reply_text("Прогноз принят, желаем удачи!")
            context.user_data.pop('matches', None)
            context.user_data.pop('current_match_index', None)
            context.user_data['awaiting_prediction'] = False

    # Функция просмотра прогнозов пользователя
    def show_my_prediction(query, context: CallbackContext):
        user_id = query.from_user.id
        with closing(connect_db()) as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT m.match_name, p.score
                FROM predictions p
                JOIN matches m ON p.match_id = m.match_id
                WHERE p.user_id = %s
                ORDER BY m.match_id;
            """, (user_id,))
            predictions = cur.fetchall()
            if predictions:
                message = "Ваши прогнозы:\n"
                for match_name, score in predictions:
                    message += f"{match_name}: {score}\n"
                query.message.reply_text(message)
            else:
                query.message.reply_text("Вы ещё не сделали прогнозы на текущую неделю.")

        # Функция просмотра таблицы лидеров (топ-10)
        def show_leaderboard(query, context: CallbackContext):
            with closing(connect_db()) as conn, conn.cursor() as cur:
                cur.execute("SELECT username, points FROM users ORDER BY points DESC LIMIT 10;")
                top_users = cur.fetchall()
            if top_users:
                message = "Топ-10 пользователей:\n"
                for idx, (username, points) in enumerate(top_users, start=1):
                    message += f"{idx}. {username} - {points} очков\n"
                query.message.reply_text(message)
            else:
                query.message.reply_text("Нет данных для таблицы лидеров.")

        def main():
            init_db()
            updater = Updater(TOKEN, use_context=True)
            dp = updater.dispatcher

            dp.add_handler(CommandHandler("start", start))
            dp.add_handler(CallbackQueryHandler(button_handler))
            dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))

            updater.start_polling()
            updater.idle()

        if name == "__main__":
            main()