# -*- coding: utf-8 -*-
import time
import requests
import feedparser
import concurrent.futures
from bs4 import BeautifulSoup
from openai import OpenAI
from telegram import Bot
from PIL import Image
from io import BytesIO
import sqlite3
import logging
from datetime import datetime, timedelta
import os  # <--- Добавляем os
from dotenv import load_dotenv  # <--- Добавляем dotenv

# --- Загрузка переменных окружения ---
load_dotenv()

# --- 1. Настройки из .env ---
DB_FILE = os.getenv("DB_FILE_PATH")  # Укажи путь по умолчанию, если нужно

# Список RSS-лент (читаем строку, разделяем по запятой)
rss_urls_str = os.getenv("RSS_FEEDS_URLS", "")
RSS_FEEDS = [url.strip() for url in rss_urls_str.split(',') if url.strip()]
if not RSS_FEEDS:
    logging.warning("Переменная RSS_FEEDS_URLS не найдена или пуста в .env файле!")
    exit(1)

# Ключ OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL_NAME = os.getenv("OPENAI_MODEL_NAME", "gpt-4.1-mini")  # Модель по умолчанию

# Ключи Telegram
TELEGRAM_BOT_API_KEY = os.getenv("TELEGRAM_BOT_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID")

# Проверка наличия ключей
if not OPENAI_API_KEY:
    logging.error("Ключ OpenAI API (OPENAI_API_KEY) не найден в окружении или .env файле!")
    exit(1) # Раскомментируй, если хочешь завершать скрипт при отсутствии ключа
if not TELEGRAM_BOT_API_KEY:
    logging.error("Ключ Telegram Bot API (TELEGRAM_BOT_API_KEY) не найден!")
    exit(1)
if not CHANNEL_ID:
    logging.error("ID Канала Telegram (CHANNEL_ID) не найден!")
    exit(1)

# --- 2. Инициализация ---

# Настройка логгирования (можно имя файла тоже вынести в .env)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("telegram_poster.log", encoding='utf-8'),  # Пример имени лог-файла
        logging.StreamHandler()
    ]
)

# Инициализация клиентов (теперь используют переменные)
openai_client = None
if OPENAI_API_KEY:
    try:
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
        logging.info("Клиент OpenAI успешно инициализирован.")
    except Exception as e:
        logging.error(f"Ошибка инициализации OpenAI: {e}")
else:
    logging.warning("Пропуск инициализации OpenAI - ключ не найден.")

telegram_bot = None
if TELEGRAM_BOT_API_KEY:
    try:
        telegram_bot = Bot(token=TELEGRAM_BOT_API_KEY)
        logging.info("Клиент Telegram Bot успешно инициализирован.")
    except Exception as e:
        logging.error(f"Ошибка инициализации Telegram Bot: {e}")
else:
    logging.warning("Пропуск инициализации Telegram Bot - ключ не найден.")


def ensure_database_exists():
    """
    Проверяет наличие таблицы articles и всех необходимых колонок.
    Добавляет недостающие колонки без удаления существующих данных.
    Создаёт таблицу, если она не существует.
    """
    # Сначала вызываем setup_database(), чтобы гарантировать существование таблицы
    # setup_database() использует CREATE TABLE IF NOT EXISTS, так что это безопасно
    setup_database()

    # Теперь проверяем и добавляем недостающие колонки в СУЩЕСТВУЮЩУЮ таблицу
    logging.info("Проверка и добавление недостающих колонок в таблицу articles...")
    required_columns = {
        # Старые колонки (id и datetime добавляются автоматически или через DEFAULT)
        'link', 'title', 'keywords', 'telegram_link',
        # Новые колонки
        'cleaned_text', 'image_url', 'status_wp', 'wordpress_link'
    }

    columns_to_add = {
        'cleaned_text': 'TEXT',
        'image_url': 'TEXT',
        'status_wp': 'TEXT DEFAULT \'pending\'',  # Добавляем DEFAULT значение сразу
        'wordpress_link': 'TEXT'
    }

    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        cursor = conn.cursor()

        # Получаем текущие колонки
        cursor.execute("PRAGMA table_info(articles);")
        current_columns = {row[1] for row in cursor.fetchall()}

        added_count = 0
        for col_name, col_type in columns_to_add.items():
            if col_name not in current_columns:
                try:
                    alter_query = f'ALTER TABLE articles ADD COLUMN {col_name} {col_type}'
                    cursor.execute(alter_query)
                    logging.info(f"Успешно добавлена колонка: {col_name}")
                    added_count += 1
                except sqlite3.OperationalError as e:
                    # Ловим ошибку, если колонка уже существует (хотя проверка выше должна это предотвращать)
                    # или другие проблемы ALTER TABLE
                    logging.error(f"Не удалось добавить колонку {col_name}: {e}")
            else:  # Можно раскомментировать для отладки
                logging.debug(f"Колонка {col_name} уже существует.")

        if added_count > 0:
            conn.commit()  # Сохраняем изменения только если что-то добавили
            logging.info(f"Структура таблицы обновлена. Добавлено колонок: {added_count}")
        else:
            logging.info("Структура таблицы `articles` уже содержит все необходимые колонки.")

    logging.info("Проверка структуры базы данных завершена.")


def setup_database():
    """Создаёт таблицу со всеми нужными полями, если её ещё нет."""
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        cursor = conn.cursor()

        # Создаём таблицу articles сразу со всеми полями
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY,
                link TEXT UNIQUE,
                title TEXT,
                keywords TEXT,
                telegram_link TEXT,
                datetime TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                cleaned_text TEXT,
                image_url TEXT,
                status_wp TEXT DEFAULT 'pending',
                wordpress_link TEXT
            )
        ''')
        conn.commit()
        logging.info("База данных и таблица `articles` успешно настроены.")


def cleanup_old_articles():
    """
    Удаляет статьи старше недели из базы данных.
    """
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        cursor = conn.cursor()

        # Считаем дату отсечения
        cutoff_date = datetime.now() - timedelta(days=7)  # 7 дней = 1 неделя

        # Удаляем старые статьи
        cursor.execute('DELETE FROM articles WHERE datetime < ?', (cutoff_date,))
        conn.commit()
    logging.info("Старые статьи (старше 1 недели) успешно удалены.")


def save_article_to_db(link, title, cleaned_text, image_url, telegram_link=None):
    """
    Сохраняет данные статьи в базу данных, включая очищенный текст и URL изображения.
    Устанавливает статус для WordPress в 'pending'.
    """
    # Убрали 'keywords' из параметров и запроса
    sql_query = '''
        INSERT INTO articles (
            link, 
            title, 
            telegram_link, 
            datetime, 
            cleaned_text,       -- Новое
            image_url,          -- Новое
            status_wp           -- Новое (устанавливаем 'pending' по умолчанию)
            -- Убрали keywords, wordpress_link (он будет заполняться позже)
        ) 
        VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?, 'pending') 
    '''
    # Параметры для запроса (порядок важен!)
    params = (link, title, telegram_link, cleaned_text, image_url)

    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(sql_query, params)
            conn.commit()
            logging.info(f"Статья успешно добавлена в базу для обработки WP: {link}")
        except sqlite3.IntegrityError:
            # Эта ошибка возникает, если link уже существует (UNIQUE constraint)
            logging.warning(f"Статья уже существует в базе данных (попытка дублирования): {link}")
            # Здесь можно добавить логику обновления записи, если нужно, но пока просто пропускаем
            pass  # Просто игнорируем дубликат, т.к. он уже должен быть обработан или в процессе
        except Exception as e:
            conn.rollback()  # Откатываем транзакцию при других ошибках
            logging.error(f"Ошибка сохранения статьи {link} в БД: {e}")


def clean_html(html):
    """Cleans HTML code by removing unnecessary tags.

    Args:
        html (str): The raw HTML content.

    Returns:
        str: Cleaned plain text from the HTML.
    """

    def parse_html(html):
        try:
            # Используем "lxml" вместо менее оптимального "html.parser"
            soup = BeautifulSoup(html, 'lxml')
            for tag in soup(["script", "style", "meta", "noscript"]):
                tag.extract()
            text = soup.get_text(separator="\n")
            return "\n".join(line.strip() for line in text.splitlines() if line.strip())
        except Exception as e:
            logging.info(f"Ошибка обработки HTML: {e}")
            return ""

    # Ограничиваем время выполнения — тайм-аут в 5 секунд.
    with (concurrent.futures.ThreadPoolExecutor() as executor):
        future = executor.submit(parse_html, html)
        try:
            return future.result(timeout=5)
        except concurrent.futures.TimeoutError:
            logging.info("Обработка HTML заняла слишком много времени!")
            return ""


def filter_article(cleaned_text, link):

    if not openai_client:
        logging.error("Клиент OpenAI не инициализирован, пропуск filter_article.")
        return None

    prompt = f"""
    Based on the text below, answer the following question:
    Does this article meet the listed requirements?

    Requirements:
    - It is not an advertisement or promotional content, and not tips for wordgames
    - It is related to technology, gadgets, or software.

    Text:
    {cleaned_text[:3000]}

    Answer "Yes" if the article meets all requirements, otherwise answer "No".
    """
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": prompt}]
        )
        answer = response.choices[0].message.content.strip().lower()

        logging.info(f"Processing link: {link}")
        logging.info(f"ChatGPT desicion: {answer}")

        return answer.lower() == "yes"

    except Exception as e:
        logging.error(f"Ошибка принятия решения на прохождение фильтра через OpenAI: {e}")
        return None


def is_title_similar_with_chatgpt(new_title, existing_titles):
    """
    Проверяет схожесть нового заголовка с уже существующими заголовками
    в базе данных, используя GPT-опрос.
    """
    # Соединяем существующие заголовки в читаемом формате
    formatted_existing_titles = "\n".join(f"- {title}" for title in existing_titles)

    # Создаём общий промпт
    prompt = f"""
    Check if the following new title is too similar to any of the existing titles.

    New title:
    "{new_title}"

    Existing titles:
    {formatted_existing_titles}

    Answer "Yes" if the new title is too similar to any of the existing titles. 
    Otherwise, answer "No".

    ONLY reply with "Yes" or "No".
    """
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": prompt}]
        )
        decision = response.choices[0].message.content.strip().lower()

        logging.info(f"GPT decision for title similarity check: {decision}")

        return decision == "yes"

    except Exception as e:
        logging.error(f"Ошибка сравнения заголовков через OpenAI: {e}")
        return None


def extract_main_image(html):
    """
    Извлекает главное изображение статьи на основе OpenGraph (<meta property="og:image">),
    проверяет его доступность и разрешение.
    """
    try:
        soup = BeautifulSoup(html, 'lxml')

        # Находим картинку через OpenGraph
        og_image = soup.find("meta", property="og:image")
        if og_image and og_image.get("content"):
            image_url = og_image["content"]

            # Проверяем доступность изображения и его размеры
            response = requests.get(image_url, timeout=5)
            if response.status_code == 200:
                # Проверяем MIME-тип (например, поддерживать только изображения)
                content_type = response.headers.get('Content-Type', '')
                if content_type.startswith('image/'):
                    # Проверка разрешения изображения
                    image = Image.open(BytesIO(response.content))
                    width, height = image.size
                    if width >= 300 and height >= 300:  # Минимальное разрешение
                        return image_url
                    else:
                        logging.info(f"Изображение слишком маленькое: {width}x{height}px")
            else:
                logging.error(f"Не удалось загрузить изображение: {image_url}")
    except Exception as e:
        logging.error(f"Ошибка извлечения изображения: {e}")
    return None


def generate_content(cleaned_text, link):
    if not cleaned_text.strip():
        logging.info(f"Пустой текст для контента: {link} — пропускаю.")
        return None  # Пропускаем пустой текст
    prompt = f"""
You are a highly experienced and popular journalist who writes concise and engaging posts that hold the audience’s 
attention. You believe that the most important quality of any post is the absence of fluff and pointless conclusions 
(why bother, if the post is just two paragraphs long anyway?).It’s better to leave the juiciest bits for your beloved 
readers. Your favorite and most well - studied topics for publication include all kinds of gadgets, system 
vulnerabilities, technologies that improve our lives, operating system updates, programming languages, and so on. In 
short, you’re a geek.

This is very important for me and my family; my survival literally depends on this.I need to grow my audience of 
readers, and I am really counting on you. Of course, I will pay you 1 bitcoin for each post 🙏🏻

Task:
1. Read the text of the article at this link: {cleaned_text} This will be your source of information.
2. Write a concise, captivating, but information - packed post in English based solely on the data from the source. 
Write it as the author—this is your article, not a retelling. However, do not make anything up. You can use HTML 
formatting to make the text even more attractive. Don't use MARKDOWN. Don't use ** at all! Ensure the post is concise 
and contains no redundant information. Does not exceed 1024 characters (including HTML tags)
3. Write an interesting title that grabs attention and makes even the most skeptical critic take a look. The title must 
be written in HTML format using the <b> tag to make it bold.
4. After the “body” of the post, on a separate line, write one to three hashtags relevant to the text. 
5. At the end of the post, after the hashtags and on a separate line, write the word “Source” in plain text (not bold or 
italic), embedding this link: {link} using a < a >  tag in HTML. Please end the post with the word 'Source' as a 
hyperlink containing the link. Do not add any extra text, punctuation, or formatting before or after 'Source'. Only the 
word 'Source' should be hyperlinked.

Answer ONLY in the specified format:

<b>Engaging Title Goes Here</b>

Main body (up to 1024 characters). Add <i>italics</i> or <b>bold</b> text for emphasis if necessary.

#Hashtag1 #Hashtag2 #Hashtag3

<a href = "{link}"> Source </a>
    """

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[{"role": "user", "content": prompt}]
        )
        gpt_article = response.choices[0].message.content.strip()
        return gpt_article  # Успешно завершили — возвращаем результат
    except Exception as e:
        logging.error(f"Ошибка генерации контента через OpenAI: {e}")
        return None  # Если произошла ошибка, возвращаем None


def publish_to_telegram(post, photo_url=None):

    if not telegram_bot:
        logging.error("Клиент Telegram Bot не инициализирован, пропуск публикации.")
        return None

    for attempt in range(3):  # Пробуем трижды
        try:
            if photo_url:
                message = telegram_bot.send_photo(chat_id=CHANNEL_ID, photo=photo_url, caption=post, parse_mode="HTML",
                                                  timeout=10)
            else:
                message = telegram_bot.send_message(chat_id=CHANNEL_ID, text=post, parse_mode="HTML", timeout=10)
            telegram_message_link = f"https://t.me/{CHANNEL_ID.replace('-', '')}/{message.message_id}"
            logging.info("Статья опубликована в Telegram. Ссылка на пост: {}".format(telegram_message_link))
            return telegram_message_link
        except Exception as e:
            logging.error(f"Ошибка при публикации в Telegram на попытке {attempt + 1}: {e}")
            time.sleep(5)  # Ждём перед следующей попыткой
    return None


def process_rss_feed(feed_url):
    """
    Обрабатывает RSS-канал, выбирает статьи, проверяет их и публикует в Telegram.
    """
    feed = feedparser.parse(feed_url)
    logging.info(f"Загружено статей из RSS-ленты {feed_url}: {len(feed.entries)}")

    # Один раз загружаем существующие заголовки из базы
    with sqlite3.connect(DB_FILE, timeout=10) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT title FROM articles ORDER BY datetime DESC LIMIT 50')
        existing_titles = [row[0] for row in cursor.fetchall()]

    for entry in feed.entries:
        try:
            link = entry.link
            new_title = entry.title

            # Проверка на дубль по ссылке
            with sqlite3.connect(DB_FILE, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT 1 FROM articles WHERE link = ?', (link,))
                already_exists = cursor.fetchone()

            if already_exists:
                logging.info(f"Статья уже обработана и есть в базе, пропускаю: {link}")
                continue

            # Проверка уникальности нового заголовка
            if is_title_similar_with_chatgpt(new_title, existing_titles):
                logging.info(f"Заголовок слишком похож на существующие заголовки. Пропускаю: {new_title}")
                continue

            # Основной процесс: обработка статьи
            response = requests.get(link, timeout=10)
            html = response.text
            cleaned_text = clean_html(html)

            if not filter_article(cleaned_text, link):  # Фильтруем статью
                logging.info(f"Фильтр отклонил статью: {link}")
                continue

            post = generate_content(cleaned_text, link)
            if not post:
                logging.info(f"Ошибка генерации контента для статьи: {link}")
                continue

            post_cleaned_for_telegram = post.replace("**", "")

            # Проверка основного изображения
            photo_url = extract_main_image(html)

            # Используем очищенный текст для публикации
            telegram_link = publish_to_telegram(post_cleaned_for_telegram, photo_url)

            if telegram_link:  # Только если публикация в Telegram успешна
                # Вызываем обновленную функцию сохранения, передавая нужные данные
                # Убрали 'keywords', добавили cleaned_text и image_url
                save_article_to_db(
                    link=link,
                    title=new_title,
                    cleaned_text=cleaned_text,  # Передаем очищенный текст
                    image_url=photo_url,  # Передаем URL картинки
                    telegram_link=telegram_link
                )

        except Exception as e:
            logging.info(f"Ошибка обработки статьи {entry.link}: {e}")


def wait_until_next_hour():
    """Ожидает до ближайших 00 минут следующего часа."""
    now = datetime.now()
    # Время следующего часа
    next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    # Вычисляем, сколько секунд ждать
    wait_time = (next_hour - now).total_seconds()
    logging.info(f"Ожидание {wait_time} секунд до следующего полного часа ({next_hour}).")
    time.sleep(wait_time)


def main():
    # Ждём до 00 минут следующего часа
    wait_until_next_hour()

    while True:
        try:
            logging.info("Начинаем обработку RSS-лент...")
            for feed_url in RSS_FEEDS:
                process_rss_feed(feed_url)

            cleanup_old_articles()  # Очищаем старые данные перед ожиданием

            logging.info("Ожидание следующего полного часа...")
            wait_until_next_hour()  # Ждём до начала следующего часа
        except Exception as e:
            logging.info(f"Ошибка в основном цикле: {e}")


if __name__ == "__main__":
    ensure_database_exists()  # Убеждаемся, что база данных существует
    main()