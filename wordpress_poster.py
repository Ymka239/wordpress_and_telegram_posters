import requests
import sqlite3
import google.generativeai as genai
import logging
import os
import time
import json
from base64 import b64encode
from io import BytesIO
from pathlib import Path
import urllib.parse
import sys
from dotenv import load_dotenv # <--- Добавляем dotenv

# --- Загрузка переменных окружения ---
load_dotenv()

# --- 1. Настройки из .env ---
DB_FILE = os.getenv("DB_FILE_PATH")

# Настройки WordPress
WP_BASE_URL = os.getenv("WP_BASE_URL")
WP_POSTS_URL = f"{WP_BASE_URL}/posts"
WP_MEDIA_URL = f"{WP_BASE_URL}/media"
WP_CATEGORIES_URL = f"{WP_BASE_URL}/categories"
WP_TAGS_URL = f"{WP_BASE_URL}/tags"
WP_USER = os.getenv("WP_USER")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD")

# Настройки Google API
GOOGLE_GEMINI_API_KEY = os.getenv("GOOGLE_GEMINI_API_KEY")
GOOGLE_SEARCH_API_KEY = os.getenv("GOOGLE_SEARCH_API_KEY")
GOOGLE_CSE_ID = os.getenv("GOOGLE_CSE_ID")

# Модель Gemini
# Используем модель, которая точно работала - 1.5 flash. Можно изменить в .env
GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL_NAME", 'models/gemini-1.5-flash-latest')

# Настройки скрипта
POST_STATUS = os.getenv("POST_STATUS", "publish")
ARTICLES_PER_RUN = int(os.getenv("ARTICLES_PER_RUN", 1)) # Читаем и конвертируем
WAIT_TIME_SECONDS = int(os.getenv("WAIT_TIME_SECONDS", 3600))
LOG_FILE = os.getenv("LOG_FILE_PATH", "wordpress_poster.log")
MAX_SUPPLEMENTARY_URLS = int(os.getenv("MAX_SUPPLEMENTARY_URLS", 3))
SEARCH_ENGINE_BASE_URL = "https://www.googleapis.com/customsearch/v1"

# --- 2. Инициализация ---

# Настройка логгирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# Инициализация клиента Google Gemini API
gemini_model = None
try:
    if not GOOGLE_GEMINI_API_KEY:
         raise ValueError("Ключ Google Gemini API (GOOGLE_GEMINI_API_KEY) не найден в окружении или .env файле.")
    genai.configure(api_key=GOOGLE_GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel(GEMINI_MODEL_NAME)
    logging.info(f"Клиент Google Gemini API ({GEMINI_MODEL_NAME}) успешно инициализирован.")
except Exception as e:
    logging.error(f"Критическая ошибка инициализации Google Gemini API: {e}")


# --- 3. Вспомогательные функции ---
def get_auth_header(user, password):
    """Создает заголовок Basic Authentication для WordPress API."""
    credentials = f"{user}:{password}"
    token = b64encode(credentials.encode('utf-8')).decode('utf-8')
    return {'Authorization': f'Basic {token}'}


def fetch_all_paginated_items(url, headers):
    """Получает все элементы (категории/теги) с учетом пагинации WordPress."""
    all_items = {}  # Используем словарь {id: name}
    page = 1
    per_page = 100  # Максимальное количество за запрос
    while True:
        try:
            params = {'per_page': per_page, 'page': page, '_fields': 'id,name'}  # Запрашиваем только нужные поля
            response = requests.get(url, headers=headers, params=params, timeout=20)
            response.raise_for_status()  # Проверка на HTTP ошибки (4xx, 5xx)
            items = response.json()

            if not items:  # Если страница пуста, значит все загрузили
                break

            for item in items:
                if 'id' in item and 'name' in item:
                    all_items[item['id']] = item['name']

            if len(items) < per_page:  # Если получили меньше, чем запрашивали, это последняя страница
                break

            page += 1
            time.sleep(0.5)  # Небольшая пауза между запросами пагинации

        except requests.exceptions.RequestException as e:
            logging.error(f"Ошибка при получении данных с {url} (страница {page}): {e}")
            return None  # Возвращаем None в случае ошибки сети/API
        except json.JSONDecodeError as e:
            logging.error(f"Ошибка декодирования JSON с {url} (страница {page}): {e}")
            return None
    return all_items

# --- 4. Основные функции ---
def connect_db():
    """Подключается к базе данных SQLite."""
    try:
        # Используем detect_types для автоматического преобразования TIMESTAMP
        conn = sqlite3.connect(DB_FILE, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES, timeout=10)
        # Устанавливаем row_factory для получения результатов в виде словарей
        conn.row_factory = sqlite3.Row
        logging.info(f"Успешное подключение к базе данных: {DB_FILE}")
        return conn
    except sqlite3.Error as e:
        logging.error(f"Ошибка подключения к базе данных {DB_FILE}: {e}")
        return None


def fetch_pending_articles(conn, limit):
    """Получает статьи из БД со статусом 'pending', включая заголовок."""
    if not conn:
        return []
    try:
        cursor = conn.cursor()
        # Добавляем 'title' в SELECT
        cursor.execute(
            """SELECT id, link, title, cleaned_text, image_url 
               FROM articles 
               WHERE status_wp = 'pending' 
               ORDER BY datetime ASC 
               LIMIT ?""",
            (limit,)
        )
        articles = cursor.fetchall()
        logging.info(f"Найдено {len(articles)} статей со статусом 'pending'.")
        return [dict(article) for article in articles]
    except sqlite3.Error as e:
        logging.error(f"Ошибка получения статей из БД: {e}")
        return []


def fetch_wordpress_taxonomies(auth_header):
    """Получает все категории и теги из WordPress."""
    logging.info("Получение списка категорий из WordPress...")
    categories = fetch_all_paginated_items(WP_CATEGORIES_URL, auth_header)
    if categories is None:
        logging.error("Не удалось получить категории.")
        return None, None  # Возвращаем None, если ошибка

    logging.info(f"Получено {len(categories)} категорий.")

    logging.info("Получение списка тегов из WordPress...")
    tags = fetch_all_paginated_items(WP_TAGS_URL, auth_header)
    if tags is None:
        logging.error("Не удалось получить теги.")
        return categories, None  # Возвращаем категории, если теги не получены

    logging.info(f"Получено {len(tags)} тегов.")

    return categories, tags  # Возвращаем словари {id: name}


# --- НОВАЯ ФУНКЦИЯ: Поиск дополнительных статей ---
def find_supplementary_articles(query, api_key, cse_id, exclude_url=None, num_results=MAX_SUPPLEMENTARY_URLS):
    """Ищет дополнительные статьи в Google Custom Search API."""
    logging.info(f"Поиск дополнительных источников для запроса: '{query}'")
    params = {
        'key': api_key,
        'cx': cse_id,
        'q': query,
        'num': num_results + 2  # Запрашиваем чуть больше, чтобы было из чего фильтровать
        # Можно добавить другие параметры, например, 'dateRestrict': 'd7' (за последнюю неделю)
    }
    supplementary_urls = []
    try:
        response = requests.get(SEARCH_ENGINE_BASE_URL, params=params, timeout=15)
        response.raise_for_status()
        search_results = response.json()

        items = search_results.get('items', [])
        logging.info(f"Найдено {len(items)} результатов в Google Search.")

        # Получаем домен из exclude_url для фильтрации
        excluded_domain = None
        if exclude_url:
            try:
                excluded_domain = urllib.parse.urlparse(exclude_url).netloc.replace("www.", "")
            except Exception:
                logging.warning(f"Не удалось извлечь домен из exclude_url: {exclude_url}")

        # Известные агрегаторы или нежелательные домены для исключения
        known_aggregators = {"techmeme.com", "feed.informer.com", "feedproxy.google.com"}  # Добавь свои, если нужно
        if excluded_domain:
            known_aggregators.add(excluded_domain)  # Добавляем исходный домен статьи

        count = 0
        added_domains = set()  # Чтобы не добавлять несколько ссылок с одного домена

        for item in items:
            url = item.get('link')
            if not url: continue

            try:
                domain = urllib.parse.urlparse(url).netloc.replace("www.", "")
                # Проверяем, не является ли ссылка исходной, агрегатором или с уже добавленного домена
                if domain not in known_aggregators and domain not in added_domains:
                    supplementary_urls.append(url)
                    added_domains.add(domain)
                    count += 1
                    logging.info(f"Добавлена доп. ссылка ({count}/{num_results}): {url}")
                    if count >= num_results:
                        break  # Набрали нужное количество
            except Exception as e:
                logging.warning(f"Ошибка парсинга URL {url} из поиска: {e}")
                continue

        if not supplementary_urls:
            logging.warning("Не найдено подходящих дополнительных источников.")
        else:
            logging.info(f"Итоговый список дополнительных URL: {supplementary_urls}")

    except requests.exceptions.RequestException as e:
        logging.error(f"Ошибка запроса к Google Search API: {e}")
        if response is not None:
            logging.error(f"Ответ Google Search API ({response.status_code}): {response.text}")
    except json.JSONDecodeError as e:
        logging.error(f"Ошибка декодирования JSON ответа Google Search API: {e}")
        if response is not None:
            logging.error(f"Ответ Google Search API: {response.text}")
    except Exception as e:
        logging.exception(f"Неожиданная ошибка в find_supplementary_articles: {e}")

    return supplementary_urls


def generate_wp_content_and_suggestions(cleaned_text, original_link, article_title, supplementary_urls, category_map, tag_map):
    """Генерирует контент с помощью Gemini API, используя доп. ссылки."""

    if cleaned_text is None:
        logging.error(
            f"Значение cleaned_text для статьи {original_link} (ID из БД) равно None. Невозможно сгенерировать контент.")
        return None  # Возвращаем None, чтобы главный цикл пометил статью как 'failed'
        # Убедимся, что это строка, даже если пустая (на всякий случай)
    processed_text = cleaned_text if isinstance(cleaned_text, str) else ""

    # Формируем часть промпта с доп. ссылками
    supplementary_prompt_part = ""
    if supplementary_urls:
         supplementary_prompt_part = "\n4.  **Supplementary Source URLs:**\n" + "\n".join([f"    - {url}" for url in supplementary_urls]) + "\n"
    else:
         supplementary_prompt_part = "\n4.  **Supplementary Source URLs:** None provided.\n"

    # --- ЗДЕСЬ БУДЕТ ТВОЙ ДЕТАЛЬНЫЙ ПРОМПТ ---
    # Промпт должен учитывать правила Rank Math (как ты и говорила)
    # и просить результат в формате JSON.
    prompt = f"""
You are an expert SEO copywriter and WordPress editor, highly proficient in optimizing content according to Rank Math SEO guidelines for API automation using models like Gemini 2.5 Flash. Your specialty is generating engaging, comprehensive, and well-sourced tech news articles ready for publishing.

**Task:** Write a NEW, UNIQUE, and IN-DEPTH WordPress post in HTML format based on the provided information.

**Input Data:**

1.  **Main Article Title:** "{article_title}"
2.  **Main Article Cleaned Text:**
    ```
    {processed_text[:8000]} 
    ```
    *(Note: Text might be truncated)*
3.  **Main Source URL:** {original_link}{supplementary_prompt_part}
5.  **Available WordPress Categories:** `{', '.join(category_map) if category_map else 'None'}` # <-- Используем category_map
6.  **Available WordPress Tags:** `{', '.join(tag_map) if tag_map else 'None'}` # <-- Используем tag_map

**Instructions (Adhere STRICTLY):**

1.  **Analyze All Input:** Understand the core topic from the main text and title. Consider the context provided by the supplementary source URLs.
2.  **Synthesize Information:** Write a comprehensive article using the main text as the primary source. **Enrich the content** with unique details, facts, quotes, or different perspectives found in similar articles represented by the supplementary URLs. Do NOT just list information from each source; integrate it naturally into a cohesive narrative.
3.  **Focus Keyword:** Determine the best **Primary Focus Keyword** (2-5 words) for Rank Math based on the synthesized content. Remember this keyword for the next steps.
4.  **SEO Title (approx. 50-60 chars):**
    *   Create a compelling title that includes the **Primary Focus Keyword**, preferably near the beginning.
    *   Include a **Power Word** (e.g., essential, ultimate, proven, surprising, critical, easy, best).
    *   Evoke **positive or negative sentiment**.
5.  **HTML Body Content:**
    *   **Length:** Target **at least 1000 words**. Focus on depth and quality.
    *   **Uniqueness:** Ensure the generated content is original and provides value beyond the source text.
    *   **Structure:** Use `<p>`, `<h2>`, `<h3>`. Ensure logical flow.
    *   **Readability:** Use short paragraphs and clear sentences.
    *   **Keyword Integration:**
        *   Ensure the **exact phrase** 'determined Primary Focus Keyword' appears naturally within the first 10% of the content.
        *   Integrate the **exact phrase** 'determined Primary Focus Keyword' naturally multiple times (aim for ~1% density, e.g., 8-10 times for 1000 words). Use related variations too.
        *   Include the **exact phrase** 'determined Primary Focus Keyword' or a very close variation in at least one `<h2>` or `<h3>` subheading.
    *   **Source Citing & Linking:**
        *   When using information primarily from the **Main Source URL** ({original_link}), cite it appropriately using an HTML link like: `<a href="{original_link}">[Inferred Source Name or 'original source']</a> reports...`
        *   When incorporating significant information inspired by a **Supplementary Source URL**, cite *that specific supplementary URL* using an HTML link: `<a href="[Supplementary URL]">[Inferred Source Name or 'another source']</a> adds that...`
        *   Infer source names from URLs where possible (e.g., The Verge, TechCrunch). Use generic terms if unsure.
        *   Integrate these citations contextually 1-3 times throughout the body. **Do NOT add a separate "Additional Context" paragraph at the end.**
        *   Ensure all `<a>` tags are correctly formed.
6.  **Main Image Alt Text Suggestion:** Generate a relevant and SEO-optimized **Alt Text suggestion** including the **Primary Focus Keyword**.
7.  **Taxonomies (Prefer Existing, Suggest New if Needed):**
    *   Suggest **1-2 Categories** from the AVAILABLE list, or suggest new relevant names if none fit.
    *   Suggest **3-5 Tags** from the AVAILABLE list, or suggest new relevant names if none fit.
    *   Return the suggested names (existing or new).

**Output Format:**

Return ONLY the JSON object below. Ensure the `body` is valid HTML and includes appropriate `<a>` tags for source links.

```json
{{
  "primary_focus_keyword": "Example Primary Keyword",
  "seo_title": "Example SEO Title with 5 Essential Facts",
  "suggested_alt_text_main_image": "Alt text including Example Primary Keyword",
  "body": "<p>Start of the unique HTML content including Example Primary Keyword...</p><h2>Subheading with Keyword</h2><p>More content synthesized from sources... According to <a href=\"{original_link}\">Original Source Name</a>, the main point is... However, <a href=\"supplementary_url_1\">Another Source</a> adds that...</p><h3>Details</h3><p>...Further details from <a href=\"supplementary_url_2\">Yet Another Source</a> indicate...</p>",
  "suggested_categories": ["Chosen Category 1", "New Category Suggestion"],
  "suggested_tags": ["Existing Tag 1", "New Tag Suggestion 1", "New Tag Suggestion 2"]
}}
"""
    # --- КОНЕЦ ПРОМПТА ---

    logging.info(f"Запрос к Google Gemini API ({GEMINI_MODEL_NAME}) для генерации контента по ссылке: {original_link}")

    if not gemini_model:  # Проверяем, инициализирована ли модель
        logging.error("Клиент Gemini API не был инициализирован. Пропуск генерации.")
        return None

    try:
        # Настройки безопасности
        safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_ONLY_HIGH"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_ONLY_HIGH"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_ONLY_HIGH"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_ONLY_HIGH"},
        ]
        generation_config = genai.types.GenerationConfig(
            response_mime_type="application/json"
        )

        response = gemini_model.generate_content(
            prompt,
            generation_config=generation_config,
            safety_settings=safety_settings
        )

        if not response.parts:
            logging.error("Ответ Gemini API пуст (возможно, заблокирован по соображениям безопасности).")
            if response.prompt_feedback:
                logging.error(f"Gemini prompt feedback: {response.prompt_feedback}")
            return None

        response_content = response.text
        generated_data = json.loads(response_content)

        required_keys = ["primary_focus_keyword", "seo_title", "suggested_alt_text_main_image", "body",
                         "suggested_categories", "suggested_tags"]
        if not all(key in generated_data for key in required_keys):
            missing_keys = [key for key in required_keys if key not in generated_data]
            logging.error(f"Ответ Gemini API не содержит всех необходимых ключей. Отсутствующие: {missing_keys}")
            logging.error(f"Полученный JSON от Gemini: {response_content}")
            return None

        logging.info(f"Контент успешно сгенерирован Gemini API для: {original_link}")
        return generated_data

    except json.JSONDecodeError as e:
        logging.error(f"Ошибка декодирования JSON ответа Gemini API: {response_content} \nОшибка: {e}")
    except Exception as e:
        logging.error(f"Неожиданная ошибка при генерации контента Gemini API: {e}")
        try:
            if response and response.prompt_feedback:
                logging.error(f"Gemini prompt feedback: {response.prompt_feedback}")
        except Exception:
            pass

    return None


def create_wp_term(name, taxonomy_endpoint, auth_header):
    """Создает новый терм (категорию или тег) в WordPress."""
    logging.info(f"Попытка создать терм '{name}' в {taxonomy_endpoint}...")
    headers = auth_header.copy()
    headers['Content-Type'] = 'application/json'
    payload = json.dumps({"name": name})  # Передаем имя в JSON

    try:
        response = requests.post(taxonomy_endpoint, headers=headers, data=payload, timeout=15)

        # Проверка на ошибку "term_exists" (если вдруг гонка запросов)
        if response.status_code == 400:
            try:
                error_data = response.json()
                if error_data.get("code") == "term_exists":
                    term_id = error_data.get("data", {}).get("term_id")
                    if term_id:
                        logging.warning(f"Терм '{name}' уже существует (код term_exists). ID: {term_id}")
                        return term_id  # Возвращаем ID существующего терма
                    else:
                        logging.error(f"Терм '{name}' уже существует, но не удалось получить ID.")
                        return None
            except json.JSONDecodeError:
                pass  # Ошибка не 'term_exists', обрабатываем ниже

        response.raise_for_status()  # Проверка на другие HTTP ошибки

        term_data = response.json()
        term_id = term_data.get('id')
        if term_id:
            logging.info(f"Терм '{name}' успешно создан. ID: {term_id}")
            return term_id
        else:
            logging.error(f"Не удалось получить ID для созданного терма '{name}': {term_data}")
            return None

    except requests.exceptions.RequestException as e:
        logging.error(f"Ошибка создания терма '{name}' в {taxonomy_endpoint}: {e}")
        if response is not None:
            logging.error(f"Ответ WP ({response.status_code}): {response.text}")
        return None
    except json.JSONDecodeError as e:
        logging.error(f"Ошибка декодирования JSON при создании терма '{name}': {e}")
        if response is not None:
            logging.error(f"Ответ WP: {response.text}")
        return None


# Можно создать обертки для удобства
def create_wp_category(name, auth_header):
    return create_wp_term(name, WP_CATEGORIES_URL, auth_header)


def create_wp_tag(name, auth_header):
    return create_wp_term(name, WP_TAGS_URL, auth_header)


def get_or_create_term_ids(suggested_names, existing_terms_map, taxonomy_type, auth_header):
    """
    Получает ID для предложенных имен таксономий.
    Если терм не существует, создает его.

    Args:
        suggested_names (list): Список имен, предложенных ИИ.
        existing_terms_map (dict): Словарь {id: name} существующих термов.
        taxonomy_type (str): 'category' или 'tag'.
        auth_header (dict): Заголовок аутентификации.

    Returns:
        list: Список ID существующих или только что созданных термов.
    """
    if not suggested_names:
        return []

    final_ids = []
    # Создаем обратную карту {name_lowercase: id} для быстрого поиска существующих
    # Сразу приводим имена к нижнему регистру для сравнения
    name_to_id_map = {name.strip().lower(): id for id, name in existing_terms_map.items()}

    for name in suggested_names:
        if not isinstance(name, str) or not name.strip():
            continue  # Пропускаем нестроковые или пустые имена

        clean_name = name.strip()
        lower_name = clean_name.lower()

        term_id = name_to_id_map.get(lower_name)  # Ищем существующий ID

        if term_id:
            logging.info(f"Найден существующий {taxonomy_type} ID={term_id} для '{clean_name}'")
            final_ids.append(term_id)
        else:
            logging.info(f"{taxonomy_type.capitalize()} '{clean_name}' не найден. Попытка создания...")
            # Создаем новый терм
            if taxonomy_type == 'category':
                new_id = create_wp_category(clean_name, auth_header)
            elif taxonomy_type == 'tag':
                new_id = create_wp_tag(clean_name, auth_header)
            else:
                logging.error(f"Неизвестный тип таксономии: {taxonomy_type}")
                new_id = None

            if new_id:
                final_ids.append(new_id)
                # Добавляем только что созданный терм в нашу карту,
                # чтобы не создавать его повторно в этом же цикле, если ИИ предложил его дважды
                name_to_id_map[lower_name] = new_id
                existing_terms_map[new_id] = clean_name  # Обновляем и основную карту для полноты
            else:
                logging.error(f"Не удалось создать {taxonomy_type} '{clean_name}'.")

        # Небольшая пауза после потенциального создания терма
        if not term_id:
            time.sleep(1)

    return final_ids


def download_image(image_url):
    """Скачивает изображение по URL."""
    if not image_url:
        logging.warning("URL изображения отсутствует, пропуск скачивания.")
        return None, None

    logging.info(f"Попытка скачивания изображения: {image_url}")
    try:
        response = requests.get(image_url, stream=True, timeout=20)
        response.raise_for_status()

        # Проверяем тип контента
        content_type = response.headers.get('content-type')
        if not content_type or not content_type.startswith('image/'):
            logging.warning(f"URL указывает не на изображение ({content_type}): {image_url}")
            return None, None

        # Получаем имя файла из URL или генерируем
        try:
            filename = os.path.basename(image_url.split('?')[0])  # Убираем параметры запроса
            if not filename:  # Если URL заканчивается на /
                filename = f"image_{int(time.time())}.{content_type.split('/')[-1]}"
        except Exception:
            filename = f"image_{int(time.time())}.{content_type.split('/')[-1]}"  # Запасной вариант

        # Читаем данные изображения в память
        image_data = BytesIO(response.content)
        logging.info(f"Изображение успешно скачано: {filename}")
        return image_data, filename

    except requests.exceptions.RequestException as e:
        logging.error(f"Ошибка скачивания изображения {image_url}: {e}")
        return None, None


def upload_image_to_wp(image_data, filename, alt_text, auth_header): # <-- Добавлен alt_text
    """Загружает изображение в медиатеку WordPress."""
    if not image_data or not filename:
        return None

    logging.info(f"Загрузка изображения '{filename}' в WordPress с Alt текстом: '{alt_text}'...")

    # Сбрасываем указатель в BytesIO перед чтением
    image_data.seek(0)

    # Устанавливаем правильные заголовки для файла
    headers = auth_header.copy()
    headers['Content-Disposition'] = f'attachment; filename="{filename}"'

    files = {'file': (filename, image_data)}
    # 2. Добавить данные для POST-запроса, включая alt_text:
    post_data = {'alt_text': alt_text}

    try:
        # 3. Передать post_data в вызов requests.post:
        response = requests.post(
            WP_MEDIA_URL,
            headers=headers,
            files=files,
            data=post_data, # <-- Передаем alt_text здесь
            timeout=30
        )
        response.raise_for_status()

        media_data = response.json()
        media_id = media_data.get('id')

        if media_id:
            logging.info(f"Изображение успешно загружено в WP. Media ID: {media_id}")
            return media_id
        else:
            logging.error(f"Не удалось получить Media ID из ответа WP: {media_data}")
            return None

    except requests.exceptions.RequestException as e:
        logging.error(f"Ошибка загрузки изображения в WP: {e}")
        if response is not None:
            logging.error(f"Ответ WP: {response.text}")
        return None
    except json.JSONDecodeError as e:
        logging.error(f"Ошибка декодирования JSON при загрузке изображения: {e}")
        if response is not None:
            logging.error(f"Ответ WP: {response.text}")
        return None


def update_post_meta(post_id, meta_data, auth_header):
    """Обновляет метаданные поста отдельным запросом."""
    if not post_id or not meta_data:
        logging.error("Не передан ID поста или метаданные для обновления.")
        return False

    update_url = f"{WP_POSTS_URL}/{post_id}"  # URL для обновления конкретного поста
    logging.info(f"Попытка обновления метаданных для поста ID {post_id}...")

    # Payload содержит только поле meta
    payload = {"meta": meta_data}

    headers = auth_header.copy()
    headers['Content-Type'] = 'application/json'

    # --- ЛОГГИРОВАНИЕ PAYLOAD ОБНОВЛЕНИЯ ---
    try:
        payload_string = json.dumps(payload, indent=2, ensure_ascii=False)
        logging.info(f"Payload для обновления мета:\n{payload_string}")
    except Exception as e:
        logging.error(f"Ошибка при форматировании meta payload для логгирования: {e}")
        logging.info(f"Meta Payload (сырой вид): {payload}")
    # --- КОНЕЦ ЛОГА PAYLOAD ОБНОВЛЕНИЯ ---

    response = None
    try:
        # Используем POST для обновления (WP REST API рекомендует POST для частичных обновлений)
        response = requests.post(update_url, headers=headers, json=payload, timeout=20)
        response.raise_for_status()  # Проверка на ошибки 4xx/5xx

        updated_data = response.json()

        # --- ЛОГГИРОВАНИЕ ОТВЕТА WP ОБНОВЛЕНИЯ ---
        try:
            response_string = json.dumps(updated_data, indent=2, ensure_ascii=False)
            logging.info(f"Ответ WP при обновлении мета для поста ID {post_id}:\n{response_string}")
        except Exception as e:
            logging.error(f"Ошибка при форматировании ответа WP (мета) для логгирования: {e}")
            logging.info(f"Ответ WP (мета, сырой вид): {updated_data}")
        # --- КОНЕЦ ЛОГА ОТВЕТА WP ОБНОВЛЕНИЯ ---

        # Проверяем, появилось ли наше мета-поле в ответе (хотя бы одно из переданных)
        meta_keys_in_response = updated_data.get("meta", {}).keys()
        meta_keys_sent = meta_data.keys()
        if any(key in meta_keys_in_response for key in meta_keys_sent):
            logging.info(f"Метаданные для поста ID {post_id}, похоже, успешно обновлены (ключ найден в ответе).")
            return True
        else:
            # Иногда WP может обновить, но не вернуть поле в ответе, если оно было пустое до этого.
            # Считаем запрос успешным, если не было HTTP ошибки.
            logging.warning(
                f"Запрос на обновление мета для поста ID {post_id} прошел успешно (код {response.status_code}), но ключи метаданных не найдены в ответе. Возможно, они все равно сохранились.")
            return True  # Возвращаем True, т.к. запрос прошел без ошибок

    except requests.exceptions.RequestException as e:
        logging.error(f"Ошибка обновления метаданных поста ID {post_id}: {e}")
        if response is not None:
            try:
                logging.error(f"Ответ WP ({response.status_code}): {response.text}")
            except Exception as log_e:
                logging.error(f"Не удалось прочитать текст ответа WP: {log_e}")
        return False
    except json.JSONDecodeError as e:
        logging.error(f"Ошибка декодирования JSON при обновлении мета (пост ID {post_id}): {e}")
        if response is not None:
            try:
                logging.error(f"Ответ WP ({response.status_code}): {response.text}")
            except Exception as log_e:
                logging.error(f"Не удалось прочитать текст ответа WP: {log_e}")
        return False
    except Exception as e:
        logging.exception(f"Непредвиденная ошибка в update_post_meta (пост ID {post_id}): {e}")
        return False


def update_article_status(conn, article_id, status, wordpress_link=None):
    """Обновляет статус статьи в БД."""
    if not conn:
        return False
    logging.info(f"Обновление статуса статьи ID {article_id} на '{status}'...")
    try:
        cursor = conn.cursor()
        if status == "published":
            cursor.execute(
                "UPDATE articles SET status_wp = ?, wordpress_link = ? WHERE id = ?",
                (status, wordpress_link, article_id)
            )
        elif status == "failed":
            cursor.execute(
                "UPDATE articles SET status_wp = ? WHERE id = ?",
                (status, article_id)
            )
        else:  # Можно добавить обработку других статусов, если нужно
            logging.warning(f"Попытка установить неизвестный статус '{status}' для статьи ID {article_id}")
            return False

        conn.commit()
        logging.info(f"Статус статьи ID {article_id} успешно обновлен на '{status}'.")
        return True
    except sqlite3.Error as e:
        conn.rollback()
        logging.error(f"Ошибка обновления статуса статьи ID {article_id} в БД: {e}")
        return False

# --- 5. Основной цикл ---
def main_loop():
    """Главный цикл обработки статей."""
    logging.info("Запуск основного цикла обработки статей для WordPress...")

    # Проверяем инициализацию Gemini перед началом цикла
    if not gemini_model:
        logging.error("Клиент Gemini API не инициализирован. Завершение работы.")
        return  # Выходим из main_loop, если Gemini недоступен

    auth_header = get_auth_header(WP_USER, WP_APP_PASSWORD)

    while True:
        conn = connect_db()
        if not conn:
            logging.error("Не удалось подключиться к БД. Повторная попытка через 60 сек.")
            time.sleep(60)
            continue

        try:
            pending_articles = fetch_pending_articles(conn, ARTICLES_PER_RUN)

            if not pending_articles:
                logging.info("Нет статей для обработки в этой итерации.")
                # Переходим к finally для ожидания
            else:
                categories_map, tags_map = fetch_wordpress_taxonomies(auth_header)
                if categories_map is None or tags_map is None:
                    logging.error("Не удалось получить таксономии из WP. Пропуск этого цикла обработки.")
                    # Переходим к finally для ожидания
                else:
                    logging.info(f"Начинаем обработку {len(pending_articles)} статей...")
                    success_count = 0
                    fail_count = 0

                    for article in pending_articles:
                        # Отступы верные
                        article_id = article['id']
                        article_link = article['link']
                        article_title = article.get('title',
                                                    f'Article ID {article_id}')  # Используем ID, если title пустой
                        cleaned_text = article['cleaned_text']
                        image_url = article['image_url']

                        logging.info(f"--- Обработка статьи ID: {article_id}, Link: {article_link} ---")

                        # ---> ШАГ 0: Поиск дополнительных URL <---
                        supplementary_urls = []
                        if article_title != f'Article ID {article_id}' and GOOGLE_SEARCH_API_KEY and GOOGLE_CSE_ID:
                            try:
                                supplementary_urls = find_supplementary_articles(
                                    query=article_title,
                                    api_key=GOOGLE_SEARCH_API_KEY,
                                    cse_id=GOOGLE_CSE_ID,
                                    exclude_url=article_link,
                                    num_results=MAX_SUPPLEMENTARY_URLS
                                )
                            except Exception as search_e:
                                logging.error(
                                    f"Ошибка во время поиска доп. источников для статьи ID {article_id}: {search_e}")
                        else:
                            logging.warning(
                                f"Пропуск поиска доп. источников для статьи ID {article_id} (нет заголовка или ключей API/CSE ID).")

                        # 1. Генерируем контент с помощью Gemini
                        # Передаем имена категорий/тегов для промпта
                        category_names_list = list(categories_map.values()) if categories_map else []
                        tag_names_list = list(tags_map.values()) if tags_map else []
                        generated_data = generate_wp_content_and_suggestions(
                            cleaned_text,
                            article_link,
                            article_title,
                            supplementary_urls,
                            category_names_list,  # Передаем список имен
                            tag_names_list  # Передаем список имен
                        )
                        if not generated_data:
                            logging.error(
                                f"Не удалось сгенерировать контент для статьи ID {article_id}. Помечаем как 'failed'.")
                            update_article_status(conn, article_id, "failed")
                            fail_count += 1
                            continue

                            # 2. Получаем или создаем ID таксономий
                        category_ids = get_or_create_term_ids(
                            generated_data.get("suggested_categories", []),
                            categories_map, 'category', auth_header
                        )
                        tag_ids = get_or_create_term_ids(
                            generated_data.get("suggested_tags", []),
                            tags_map, 'tag', auth_header
                        )

                        # 3. Обрабатываем изображение
                        featured_media_id = None
                        image_data, filename = download_image(image_url)
                        if image_data and filename:
                            suggested_alt = generated_data.get("suggested_alt_text_main_image", Path(filename).stem)
                            featured_media_id = upload_image_to_wp(
                                image_data, filename, suggested_alt, auth_header
                            )
                            if not featured_media_id:
                                logging.warning(
                                    f"Не удалось загрузить изображение для статьи ID {article_id}, пост будет создан без картинки.")

                        # 4. Формируем данные для поста (БЕЗ 'meta')
                        post_payload = {
                            "title": generated_data.get("seo_title", article_title),
                            "content": generated_data.get("body", ""),
                            "status": POST_STATUS,
                            "categories": category_ids,
                            "tags": tag_ids
                        }
                        if featured_media_id:
                            post_payload["featured_media"] = featured_media_id

                        # 5. Создаем пост
                        new_post_id = None
                        new_post_link = None
                        logging.info(f"Попытка СОЗДАНИЯ поста в WordPress: '{post_payload.get('title')}'")
                        headers_create = auth_header.copy()
                        headers_create['Content-Type'] = 'application/json'
                        response_create = None
                        try:
                            response_create = requests.post(WP_POSTS_URL, headers=headers_create, json=post_payload,
                                                            timeout=30)
                            response_create.raise_for_status()
                            created_post_data = response_create.json()
                            # Можно вернуть лог ответа WP при создании для отладки
                            # logging.info(f"Ответ WP при создании поста:\n{json.dumps(created_post_data, indent=2, ensure_ascii=False)}")
                            new_post_id = created_post_data.get('id')
                            new_post_link = created_post_data.get('link')
                            if new_post_id and new_post_link:
                                logging.info(
                                    f"Пост успешно создан (Статус: {post_payload.get('status', 'N/A')}). ID: {new_post_id}, Ссылка: {new_post_link}")
                            else:
                                logging.error(
                                    f"Не удалось получить ссылку или ID из ответа WP при создании поста: {created_post_data}")
                        except Exception as create_e:
                            logging.error(f"Ошибка создания поста в WP: {create_e}")
                            if response_create is not None:
                                try:
                                    logging.error(f"Ответ WP ({response_create.status_code}): {response_create.text}")
                                except Exception:
                                    pass

                        # 5.5 Обновляем метаданные, ЕСЛИ пост был создан
                        meta_updated_successfully = False
                        if new_post_id:
                            meta_payload_update = {}  # Собираем только то, что есть
                            primary_keyword = generated_data.get("primary_focus_keyword")
                            if primary_keyword:
                                meta_payload_update["rank_math_focus_keyword"] = primary_keyword

                            # Добавляем другие поля, если они зарегистрированы и нужны
                            # if generated_data.get("rank_math_description"):
                            #     meta_payload_update["rank_math_description"] = generated_data["rank_math_description"]
                            # if generated_data.get("rank_math_title"):
                            #      meta_payload_update["rank_math_title"] = generated_data["rank_math_title"]

                            if meta_payload_update:  # Обновляем только если есть что обновлять
                                meta_updated_successfully = update_post_meta(new_post_id, meta_payload_update,
                                                                             auth_header)
                                if not meta_updated_successfully:
                                    logging.warning(
                                        f"Не удалось ОБНОВИТЬ метаданные для поста ID {new_post_id}, но сам пост был СОЗДАН.")
                            else:
                                logging.info(f"Нет метаданных Rank Math для обновления для поста ID {new_post_id}.")
                        elif generated_data:
                            logging.error(
                                f"Пост не был создан, обновление метаданных для статьи ID {article_id} не будет выполнено.")

                        # 6. Обновляем статус в БД
                        if new_post_link:
                            update_article_status(conn, article_id, "published", new_post_link)
                            success_count += 1
                        else:
                            logging.error(f"Пост не был создан для статьи ID {article_id}. Помечаем как 'failed'.")
                            update_article_status(conn, article_id, "failed")
                            fail_count += 1

                        time.sleep(2)

                    logging.info(f"--- Пакет из {len(pending_articles)} статей обработан ---")
                    logging.info(f"Успешно: {success_count}, Ошибок: {fail_count}")

        except Exception as e:
            logging.exception(f"Ошибка в основном цикле обработки пакета: {e}")
        finally:
            if conn:
                try:
                    conn.close()
                    logging.debug("Соединение с БД закрыто.")
                except Exception as db_close_e:
                    logging.error(f"Ошибка при закрытии соединения с БД: {db_close_e}")

            logging.info(f"Ожидание {WAIT_TIME_SECONDS} секунд перед следующим циклом...")
            time.sleep(WAIT_TIME_SECONDS)

# --- 6. Запуск ---
if __name__ == "__main__":
    # САМЫЙ ПЕРВЫЙ PRINT В БЛОКЕ MAIN
    try:
        print("--- PRINT: СКРИПТ ВОШЕЛ В БЛОК __main__ ---", file=sys.stderr)
        logging.info("--- LOG: СКРИПТ ВОШЕЛ В БЛОК __main__ ---")
    except Exception as e:
        print(f"Критическая ошибка при первой попытке логирования/print в main: {e}", file=sys.stderr)
        exit(1)

    # Проверка Настроек с print
    try:
        print("--- PRINT: Проверка настроек...", file=sys.stderr)
        logging.info("Проверка настроек...")
        # ... (все проверки if not DB_FILE:...) ...
        if not all(
                [DB_FILE, WP_BASE_URL, WP_USER, WP_APP_PASSWORD, GOOGLE_GEMINI_API_KEY, GOOGLE_SEARCH_API_KEY,
                 GOOGLE_CSE_ID]):
            # Используем print перед exit
            error_msg = "ОШИБКА: Не все обязательные настройки указаны! Проверьте переменные DB_FILE, WP_*, GOOGLE_*."
            print(error_msg, file=sys.stderr)
            logging.error(error_msg)
            exit(1)
        print("--- PRINT: Все обязательные настройки присутствуют.", file=sys.stderr)
        logging.info("Все обязательные настройки присутствуют.")

        # Проверка плейсхолдеров (оставляем как было, они тоже вызовут exit)
        # ... (код проверки плейсхолдеров ключей и CSE ID) ...
        if GOOGLE_GEMINI_API_KEY.startswith("YOUR_") or GOOGLE_GEMINI_API_KEY.startswith("AIzaSyB1_jJpd"):
            if "YOUR_GEMINI_API_KEY" in GOOGLE_GEMINI_API_KEY:
                error_msg = "ОШИБКА: Плейсхолдер для GOOGLE_GEMINI_API_KEY не заменен!"
                print(error_msg, file=sys.stderr)
                logging.error(error_msg)
                exit(1)
        if GOOGLE_SEARCH_API_KEY.startswith("YOUR_") or GOOGLE_SEARCH_API_KEY.startswith("AIzaSyBWWFZl"):
            if "YOUR_SEARCH_API_KEY" in GOOGLE_SEARCH_API_KEY:
                error_msg = "ОШИБКА: Плейсхолдер для GOOGLE_SEARCH_API_KEY не заменен!"
                print(error_msg, file=sys.stderr)
                logging.error(error_msg)
                exit(1)
        if not GOOGLE_CSE_ID or GOOGLE_CSE_ID == "YOUR_CSE_ID":
            error_msg = "ОШИБКА: GOOGLE_CSE_ID не установлен или не заменен!"
            print(error_msg, file=sys.stderr)
            logging.error(error_msg)
            exit(1)
        print("--- PRINT: Плейсхолдеры API проверены.", file=sys.stderr)
        logging.info("Плейсхолдеры API проверены.")


    except ValueError as ve:
        print(f"ОШИБКА НАСТРОЙКИ (ValueError): {ve}", file=sys.stderr)
        logging.error(f"ОШИБКА НАСТРОЙКИ: {ve}")
        exit(1)
    except Exception as e_settings:
        print(f"Неожиданная ошибка при проверке настроек: {e_settings}", file=sys.stderr)
        logging.exception(f"Неожиданная ошибка при проверке настроек: {e_settings}")
        exit(1)

    # Проверка инициализации Gemini
    print("--- PRINT: Проверка инициализации Gemini...", file=sys.stderr)
    logging.info("Проверка инициализации Gemini...")
    if not gemini_model:
        error_msg = "ОШИБКА: Клиент Gemini API не был инициализирован (gemini_model is None). Завершение работы."
        print(error_msg, file=sys.stderr)
        logging.error(error_msg)
        exit(1)
    else:
        print("--- PRINT: Клиент Gemini API успешно проинициализирован (проверка в main).", file=sys.stderr)
        logging.info("Клиент Gemini API успешно проинициализирован (проверка в main).")

    # Проверка папки логов
    print(f"--- PRINT: Проверка папки для лога: {LOG_FILE}", file=sys.stderr)
    logging.info(f"Проверка папки для лога: {LOG_FILE}")
    try:
        log_dir = Path(LOG_FILE).parent
        print(f"--- PRINT: Путь к папке логов: {log_dir}", file=sys.stderr)
        logging.info(f"Путь к папке логов: {log_dir}")
        if not log_dir.exists():
            print(f"--- PRINT: Папка {log_dir} не существует, попытка создания...", file=sys.stderr)
            logging.info(f"Папка {log_dir} не существует, попытка создания...")
            log_dir.mkdir(parents=True, exist_ok=True)
            print(f"--- PRINT: Папка для логов {log_dir} успешно создана.", file=sys.stderr)
            logging.info(f"Папка для логов {log_dir} успешно создана.")
        else:
            print(f"--- PRINT: Папка для логов {log_dir} уже существует.", file=sys.stderr)
            logging.info(f"Папка для логов {log_dir} уже существует.")
    except Exception as e_logdir:
        error_msg = f"ОШИБКА при работе с папкой/файлом логов: {e_logdir}"
        print(error_msg, file=sys.stderr)
        logging.exception(error_msg)
        # exit(1) # Решаем, критично ли это

    # Запуск основного цикла
    print("--- PRINT: Попытка запуска main_loop...", file=sys.stderr)
    logging.info("Попытка запуска main_loop...")
    try:
        main_loop()
    except KeyboardInterrupt:
        print("--- PRINT: Скрипт остановлен вручную (KeyboardInterrupt).", file=sys.stderr)
        logging.info("Скрипт остановлен вручную.")
    except Exception as e_main:
        # Этот except ловит ошибки ТОЛЬКО внутри main_loop
        print(f"--- PRINT: Критическая ошибка в главном цикле (main_loop): {e_main}", file=sys.stderr)
        logging.exception(f"Критическая ошибка в главном цикле (main_loop): {e_main}")
        exit(1)

    print("--- PRINT: Скрипт нормально завершил работу (до сюда не должен дойти в режиме сервиса).",
          file=sys.stderr)
    logging.info("--- Скрипт нормально завершил работу ---")