"""
FicBook Downloader
"""

import requests
from bs4 import BeautifulSoup
import time
import os
import re
import json
import subprocess
import platform
import shutil
from urllib.parse import urljoin
import tkinter as tk
from tkinter import filedialog, scrolledtext, messagebox, ttk
from threading import Thread, Event

# ─── Опциональные зависимости ─────────────────────────────────────────────────
try:
    from ebooklib import epub as ebooklib_epub
    HAS_EPUB = True
except ImportError:
    HAS_EPUB = False

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    HAS_PDF = True
except ImportError:
    HAS_PDF = False

# ─── Файл настроек ────────────────────────────────────────────────────────────
SETTINGS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'fbd_settings.json')
_settings: dict = {}


def load_settings() -> dict:
    global _settings
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            _settings = json.load(f)
    except Exception:
        _settings = {}
    return _settings


def save_settings():
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(_settings, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ─── Файл индекса (автодополнение) ────────────────────────────────────────────
INDEX_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'fbd_index.json')
_index_cache: dict = {}


def load_index() -> dict:
    global _index_cache
    try:
        with open(INDEX_FILE, 'r', encoding='utf-8') as f:
            _index_cache = json.load(f)
    except Exception:
        _index_cache = {
            'titles': [], 'authors': [],
            'fandoms': [], 'tags': [], 'pairings': []
        }
    return _index_cache


def save_index(index: dict):
    global _index_cache
    _index_cache = index
    try:
        with open(INDEX_FILE, 'w', encoding='utf-8') as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def update_index(meta: dict):
    """Добавляет данные книги в глобальный поисковый индекс."""
    index = load_index()

    def add_unique(lst: list, value):
        if value and str(value).strip() and value not in lst:
            lst.append(value)

    add_unique(index['titles'],  meta.get('title'))
    add_unique(index['authors'], meta.get('author'))
    for f in (meta.get('fandom') or []):
        add_unique(index['fandoms'], f)
    for t in (meta.get('tags') or []):
        add_unique(index['tags'], t)
    for p in (meta.get('pairing') or []):
        add_unique(index['pairings'], p)

    save_index(index)


def rank_suggestions(query: str, candidates: list) -> list:
    """Ранжирует кандидатов по степени похожести на запрос (макс. 10)."""
    q = query.lower()
    scored = []
    for c in candidates:
        cl = c.lower()
        if cl == q:
            score = 0
        elif cl.startswith(q):
            score = 1
        elif f' {q}' in cl:
            score = 2
        elif q in cl:
            score = 3
        else:
            continue
        scored.append((score, c))
    scored.sort(key=lambda x: (x[0], x[1].lower()))
    return [c for _, c in scored[:10]]


# ─── Глобальное состояние ─────────────────────────────────────────────────────
stop_event = Event()
load_settings()
output_folder = _settings.get('output_folder', os.getcwd())

# ─── Утилиты ──────────────────────────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\/:*?"<>|]', '', name)
    name = name.strip('. ')
    return name or 'untitled'


def count_words(text: str) -> int:
    return len(text.split())


def count_chars(text: str) -> int:
    return len(text)


def folder_size_kb(folder_path: str) -> float:
    total = 0
    try:
        for f in os.scandir(folder_path):
            if f.is_file():
                total += f.stat().st_size
    except Exception:
        pass
    return round(total / 1024, 1)


BOOK_FILE_PREFIX = '[BOOK]'


def is_chapter_file(fname: str) -> bool:
    return (fname.endswith('.txt')
            and not fname.startswith(BOOK_FILE_PREFIX)
            and bool(re.match(r'^\d+_', fname)))


def sorted_chapter_files(book_folder: str) -> list:
    files = []
    try:
        for fname in os.listdir(book_folder):
            if is_chapter_file(fname):
                m = re.match(r'^(\d+)_', fname)
                if m:
                    files.append((int(m.group(1)), fname))
    except Exception:
        pass
    files.sort(key=lambda x: x[0])
    return [os.path.join(book_folder, f[1]) for f in files]


# ─── Парсинг метаданных ───────────────────────────────────────────────────────

def parse_metadata(soup, fic_id) -> dict:
    meta = {
        'fic_id':            str(fic_id),
        'title':             None,
        'author':            None,
        'author_url':        None,
        'universe':          None,
        'fandom':            [],
        'pairing':           [],   # Пэйринги И персонажи (единое поле)
        'size_raw':          None,
        'pages':             None,
        'words':             None,
        'chapters_count':    None,
        'tags':              [],
        'description':       None,
        'notes':             None,
        'dedication':        None,
        'rating':            None,
        'direction':         None,
        'status':            None,
        'chapter_titles':    [],
        'chapter_urls':      [],
        'total_words':       None,
        'total_chars':       None,
        'size_kb':           None,
        'converted_formats': {},
    }

    # Название
    try:
        h1 = (soup.find('h1', itemprop='name')
              or soup.find('h1', class_='heading'))
        if h1:
            meta['title'] = h1.get_text(strip=True)
    except Exception:
        pass

    # Автор
    try:
        author_tag = (soup.find('a', itemprop='author')
                      or soup.find('a', class_='creator-username'))
        if author_tag:
            meta['author'] = author_tag.get_text(strip=True)
            meta['author_url'] = author_tag.get('href', '')
    except Exception:
        pass

    # Блоки .mb-10
    try:
        for div in soup.find_all('div', class_='mb-10'):
            strong = div.find('strong')
            if not strong:
                continue
            label = strong.get_text(strip=True)

            if 'Вселенная' in label:
                try:
                    links = div.find_all('a')
                    meta['universe'] = (
                        ', '.join(a.get_text(strip=True) for a in links) or None)
                except Exception:
                    pass

            elif 'Фэндом' in label:
                try:
                    meta['fandom'] = [
                        a.get_text(strip=True) for a in div.find_all('a')]
                except Exception:
                    pass

            elif 'Пэйринг' in label:
                # Сайт объединяет пэйринги и персонажей в одну группу
                try:
                    meta['pairing'] = [
                        a.get_text(strip=True) for a in div.find_all('a')]
                except Exception:
                    pass

            # Персонажей как отдельной группы больше нет — всё в pairing

            elif 'Размер' in label:
                try:
                    content_div = strong.find_next_sibling('div') or div.find('div')
                    size_text = (content_div.get_text(separator=' ', strip=True)
                                 if content_div else '')
                    size_text = size_text.replace('\xa0', ' ')
                    meta['size_raw'] = size_text
                    for pat, key in [(r'([\d\s]+)\s*страниц', 'pages'),
                                     (r'([\d\s]+)\s*слов',    'words'),
                                     (r'([\d\s]+)\s*част',    'chapters_count')]:
                        m2 = re.search(pat, size_text)
                        if m2:
                            meta[key] = int(m2.group(1).replace(' ', ''))
                except Exception:
                    pass

            elif 'Метки' in label:
                try:
                    meta['tags'] = [
                        a.get_text(strip=True)
                        for a in div.find_all('a', class_='tag')]
                except Exception:
                    pass

            elif 'Описание' in label:
                try:
                    desc_div = (
                        div.find('div', itemprop='description')
                        or div.find('div', class_='urlize-links')
                        or div.find('div', class_=re.compile(
                            'js-public-beta-description')))
                    meta['description'] = (
                        desc_div.get_text(strip=True) if desc_div else None)
                except Exception:
                    pass

            elif 'Примечани' in label:
                try:
                    notes_div = (
                        div.find('div', class_='urlize-links')
                        or div.find('div', class_=re.compile(
                            'js-public-beta-author-comment')))
                    meta['notes'] = (
                        notes_div.get_text(strip=True) if notes_div else None)
                except Exception:
                    pass

            elif 'Посвящение' in label:
                try:
                    ded_div = (
                        div.find('div', class_='urlize-links')
                        or div.find('div', class_=re.compile(
                            'js-public-beta-dedication')))
                    meta['dedication'] = (
                        ded_div.get_text(strip=True) if ded_div else None)
                except Exception:
                    pass
    except Exception:
        pass

    # Бейджи
    try:
        badges_section = soup.find('section', class_='fanfic-badges')
        if badges_section:
            for badge in badges_section.find_all(class_='badge-with-icon'):
                text = badge.get_text(strip=True)
                if any(d in text for d in
                       ['Гет', 'Слэш', 'Фемслэш', 'Джен', 'Смешанный', 'Другой']):
                    meta['direction'] = text
                elif re.match(r'^(G|PG|PG-13|R|NC-17|NC-21)$', text):
                    meta['rating'] = text
                elif any(s in text for s in
                         ['Завершён', 'В процессе', 'Заморожен', 'Приостановлен']):
                    meta['status'] = text
    except Exception:
        pass

    # Список глав
    try:
        for part in soup.find_all('li', class_='part'):
            link = part.find('a', class_='part-link')
            if link:
                h3 = link.find('h3')
                chapter_title = h3.get_text(strip=True) if h3 else None
                href = link.get('href', '')
                meta['chapter_titles'].append(chapter_title)
                meta['chapter_urls'].append(href)
    except Exception:
        pass

    return meta


def save_metadata(meta: dict, book_folder: str):
    try:
        path = os.path.join(book_folder, 'metadata.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as e:
        write_log(f"Ошибка сохранения метаданных: {e}")


def load_metadata(book_folder: str) -> dict | None:
    path = os.path.join(book_folder, 'metadata.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        data.setdefault('converted_formats', {})
        data.setdefault('fandom', [])
        data.setdefault('pairing', [])
        # Обратная совместимость: если были characters — добавляем в pairing
        old_chars = data.pop('characters', [])
        if old_chars:
            existing = set(data['pairing'])
            for c in old_chars:
                if c not in existing:
                    data['pairing'].append(c)
        return data
    except Exception:
        return None


def update_file_stats(meta: dict, book_folder: str):
    total_words = total_chars = 0
    try:
        for fpath in sorted_chapter_files(book_folder):
            with open(fpath, 'r', encoding='utf-8') as f:
                text = f.read()
            total_words += count_words(text)
            total_chars += count_chars(text)
        meta['total_words'] = total_words
        meta['total_chars'] = total_chars
        meta['size_kb'] = folder_size_kb(book_folder)
    except Exception:
        pass
    save_metadata(meta, book_folder)
    update_index(meta)  # обновляем глобальный индекс


# ─── Скачивание ───────────────────────────────────────────────────────────────

HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) '
                   'Chrome/124.0.0.0 Safari/537.36')
}


def save_text(content: str, filename: str):
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(content)


def write_log(message: str):
    msg = f"{time.strftime('%H:%M:%S')} — {message}"
    try:
        log_area.insert(tk.END, msg + '\n')
        log_area.yview(tk.END)
    except Exception:
        print(msg)


def _get_download_delay() -> float:
    try:
        return float(_settings.get('download_delay', 0.4))
    except Exception:
        return 0.4


def _make_book_folder(safe_title: str, fic_id) -> tuple:
    """Возвращает (folder_name, book_folder). Без ID в названии папки."""
    folder_name = safe_title
    book_folder = os.path.join(output_folder, folder_name)

    if os.path.exists(book_folder):
        # Проверяем: та же книга или другая с совпадающим названием?
        try:
            meta_path = os.path.join(book_folder, 'metadata.json')
            with open(meta_path, 'r', encoding='utf-8') as f:
                existing = json.load(f)
            if str(existing.get('fic_id')) == str(fic_id):
                return folder_name, book_folder  # та же книга — перезаписываем
        except Exception:
            pass
        # Другая книга — добавляем суффикс
        i = 2
        while os.path.exists(os.path.join(output_folder, f"{folder_name}_{i}")):
            i += 1
        folder_name = f"{folder_name}_{i}"
        book_folder = os.path.join(output_folder, folder_name)

    return folder_name, book_folder


def extract_content_from_url(url, fic_id, chapter_index,
                              passed_title=None, passed_meta=None,
                              book_folder_ref=None):
    retries = 0
    while retries < 5:
        if stop_event.is_set():
            write_log("Скачивание остановлено пользователем.")
            return

        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)

            if resp.status_code == 429:
                retries += 1
                write_log(f"429 — слишком много запросов. Пауза 60 с "
                          f"(попытка {retries}/5)")
                time.sleep(60)
                continue

            if resp.status_code != 200:
                write_log(f"Ошибка {resp.status_code} при загрузке {url}")
                return

            soup = BeautifulSoup(resp.text, 'html.parser')

            # ── Первый вход (chapter_index == 0) ──────────────────────────────
            if chapter_index == 0:
                meta = parse_metadata(soup, fic_id)
                title = meta.get('title') or f"book_{fic_id}"
                safe_title = sanitize_filename(title)

                # Папка: только название (без ID-префикса)
                folder_name, book_folder = _make_book_folder(safe_title, fic_id)
                os.makedirs(book_folder, exist_ok=True)

                passed_title = safe_title
                passed_meta  = meta

                if book_folder_ref is not None:
                    book_folder_ref[0] = book_folder
                else:
                    book_folder_ref = [book_folder]

                chapter_links = soup.find_all('li', class_='part')

                if chapter_links:
                    write_log(f"[{fic_id}] «{title}» — "
                              f"найдено {len(chapter_links)} глав")
                    for idx, part in enumerate(chapter_links):
                        link_tag = part.find('a', class_='part-link')
                        if link_tag:
                            full_url = urljoin(url, link_tag['href'])
                            extract_content_from_url(
                                full_url, fic_id, idx + 1,
                                passed_title=passed_title,
                                passed_meta=meta,
                                book_folder_ref=book_folder_ref)
                    update_file_stats(meta, book_folder)
                    return

                # Одноглавник
                content_div = soup.find('div', id='content')
                if content_div:
                    text = content_div.get_text()
                    ch_title = (meta['chapter_titles'][0]
                                if meta['chapter_titles'] else 'Глава 1')
                    safe_ch = sanitize_filename(ch_title) if ch_title else '1'
                    fname = os.path.join(book_folder, f"1_{safe_ch}.txt")
                    save_text(text, fname)
                    write_log(f"  Глава сохранена: {fname}")
                    update_file_stats(meta, book_folder)
                else:
                    write_log(f"[{fic_id}] Не найден ни список глав, "
                              f"ни контент: {url}")
                return

            # ── Скачивание конкретной главы (chapter_index > 0) ───────────────
            else:
                book_folder = book_folder_ref[0] if book_folder_ref else output_folder

                content_div = (soup.find('div', id='content')
                               or soup.find('article', class_='article'))

                if content_div:
                    text = content_div.get_text()
                    meta = passed_meta or {}
                    titles = meta.get('chapter_titles', [])
                    if chapter_index - 1 < len(titles) and titles[chapter_index - 1]:
                        ch_title = titles[chapter_index - 1]
                    else:
                        h2 = (soup.find('h2', class_=re.compile('part-title|chapter'))
                              or soup.find('h2'))
                        ch_title = (h2.get_text(strip=True) if h2
                                    else f"Глава {chapter_index}")

                    safe_ch = sanitize_filename(ch_title)
                    fname = os.path.join(book_folder,
                                        f"{chapter_index}_{safe_ch}.txt")
                    save_text(text, fname)
                    write_log(f"  [{chapter_index}] {ch_title}")
                    time.sleep(_get_download_delay())
                else:
                    write_log(f"  Не найден контент для главы "
                              f"{chapter_index}: {url}")
                return

        except requests.exceptions.ConnectTimeout:
            retries += 1
            write_log(f"Таймаут {url} — попытка {retries}/5")
            time.sleep(30)
        except Exception as e:
            write_log(f"Ошибка при обработке {url}: {e}")
            return


# ─── Скачивание коллекции ─────────────────────────────────────────────────────

def extract_books_from_collection_page(collection_url: str, page: int) -> list:
    url = f"{collection_url}{page}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            write_log(f"Не удалось загрузить коллекцию {url}: {resp.status_code}")
            return []
        soup = BeautifulSoup(resp.text, 'html.parser')
        book_links = soup.find_all('a', class_='visit-link')
        return [urljoin(url, bl['href']) for bl in book_links]
    except Exception as e:
        write_log(f"Ошибка при загрузке страницы коллекции: {e}")
        return []


def process_collection(collection_url: str):
    page = 1
    last_books: set = set()
    saved = 0
    while True:
        if stop_event.is_set():
            write_log("Скачивание остановлено.")
            break
        current = set(extract_books_from_collection_page(collection_url, page))
        if not current or current == last_books:
            write_log(f"Больше страниц нет — остановка на стр. {page}")
            break
        for book_url in current:
            m = re.search(r'/readfic/([^/?#]+)', book_url)
            fic_id = m.group(1) if m else book_url.split('/')[-1]
            extract_content_from_url(book_url, fic_id, 0)
            saved += 1
        last_books = current
        page += 1
        time.sleep(1)
    messagebox.showinfo("Завершено", f"Сохранено книг: {saved}")
    root.after(0, refresh_library)


def set_output_folder() -> bool:
    global output_folder
    base = path_label.cget('text')
    folder_name = folder_name_entry.get().strip()
    if create_folder_var.get():
        if not folder_name:
            messagebox.showerror("Ошибка", "Введите название папки.")
            return False
        output_folder = os.path.join(base, folder_name)
        os.makedirs(output_folder, exist_ok=True)
    else:
        output_folder = base
    _settings['output_folder'] = output_folder
    _settings['base_path'] = base
    _settings['folder_name'] = folder_name
    _settings['create_subfolder'] = int(create_folder_var.get())
    save_settings()
    return True


# ─── Конвертация ──────────────────────────────────────────────────────────────

def get_chapter_texts(book_folder: str) -> list:
    result = []
    for fpath in sorted_chapter_files(book_folder):
        fname = os.path.basename(fpath)
        m = re.match(r'^(\d+)_(.+)\.txt$', fname)
        num  = int(m.group(1)) if m else 0
        name = m.group(2)      if m else fname
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                text = f.read()
            result.append((num, name, text))
        except Exception:
            pass
    return result


def convert_to_txt(meta: dict, book_folder: str):
    title = sanitize_filename(meta.get('title') or 'book')
    out_name = f"{BOOK_FILE_PREFIX}_{title}.txt"
    out_path = os.path.join(book_folder, out_name)
    chapters = get_chapter_texts(book_folder)
    if not chapters:
        return None, "Нет глав для конвертации"
    try:
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write("##FICBOOK_CONVERTED_BOOK##\n")
            f.write(f"Название: {meta.get('title') or '—'}\n")
            f.write(f"Автор: {meta.get('author') or '—'}\n")
            f.write(f"ID: {meta.get('fic_id') or '—'}\n")
            fandoms = ', '.join(meta.get('fandom') or [])
            if fandoms:
                f.write(f"Фэндом: {fandoms}\n")
            f.write("=" * 60 + "\n\n")
            for num, name, text in chapters:
                f.write("=" * 60 + "\n")
                f.write(f"Глава {num}: {name}\n")
                f.write("=" * 60 + "\n\n")
                f.write(text)
                f.write("\n\n")
        return out_path, None
    except Exception as e:
        return None, str(e)


def convert_to_epub(meta: dict, book_folder: str):
    if not HAS_EPUB:
        return None, ("Библиотека ebooklib не установлена.\n"
                      "Выполните: pip install ebooklib")
    title = sanitize_filename(meta.get('title') or 'book')
    out_name = f"{BOOK_FILE_PREFIX}_{title}.epub"
    out_path = os.path.join(book_folder, out_name)
    chapters = get_chapter_texts(book_folder)
    if not chapters:
        return None, "Нет глав для конвертации"
    try:
        book = ebooklib_epub.EpubBook()
        book.set_identifier(f"ficbook_{meta.get('fic_id', 'unknown')}")
        book.set_title(meta.get('title') or 'Без названия')
        book.set_language('ru')
        if meta.get('author'):
            book.add_author(meta['author'])

        epub_chapters = []
        for num, name, text in chapters:
            ch = ebooklib_epub.EpubHtml(
                title=f"Глава {num}: {name}",
                file_name=f"chapter_{num}.xhtml",
                lang='ru')
            paragraphs = ''.join(
                f'<p>{p.strip()}</p>'
                for p in text.split('\n') if p.strip())
            ch.content = (
                f'<html><body>'
                f'<h2>Глава {num}: {name}</h2>'
                f'{paragraphs}'
                f'</body></html>')
            book.add_item(ch)
            epub_chapters.append(ch)

        book.toc = tuple(epub_chapters)
        book.add_item(ebooklib_epub.EpubNcx())
        book.add_item(ebooklib_epub.EpubNav())
        book.spine = ['nav'] + epub_chapters
        ebooklib_epub.write_epub(out_path, book)
        return out_path, None
    except Exception as e:
        return None, str(e)


def convert_to_fb2(meta: dict, book_folder: str):
    title = sanitize_filename(meta.get('title') or 'book')
    out_name = f"{BOOK_FILE_PREFIX}_{title}.fb2"
    out_path = os.path.join(book_folder, out_name)
    chapters = get_chapter_texts(book_folder)
    if not chapters:
        return None, "Нет глав для конвертации"
    try:
        def esc(s):
            return (s.replace('&', '&amp;')
                     .replace('<', '&lt;')
                     .replace('>', '&gt;')
                     .replace('"', '&quot;'))

        lines = ['<?xml version="1.0" encoding="UTF-8"?>']
        lines.append(
            '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" '
            'xmlns:l="http://www.w3.org/1999/xlink">')
        lines.append('<description><title-info>')
        lines.append(f'<book-title>{esc(meta.get("title") or "Без названия")}'
                     f'</book-title>')
        if meta.get('author'):
            parts = meta['author'].split()
            if len(parts) >= 2:
                lines.append(f'<author>'
                             f'<first-name>{esc(parts[0])}</first-name>'
                             f'<last-name>{esc(" ".join(parts[1:]))}</last-name>'
                             f'</author>')
            else:
                lines.append(f'<author>'
                             f'<nickname>{esc(meta["author"])}</nickname>'
                             f'</author>')
        lines.append('<lang>ru</lang>')
        for fandom in (meta.get('fandom') or []):
            lines.append(f'<genre>{esc(fandom)}</genre>')
        lines.append('</title-info>')
        lines.append('<document-info>')
        lines.append(f'<id>ficbook_{meta.get("fic_id", "unknown")}</id>')
        lines.append('</document-info>')
        lines.append('</description><body>')

        for num, name, text in chapters:
            lines.append(
                f'<section><title><p>Глава {num}: {esc(name)}</p></title>')
            for para in text.split('\n'):
                para = para.strip()
                if para:
                    lines.append(f'<p>{esc(para)}</p>')
            lines.append('</section>')

        lines.append('</body></FictionBook>')
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        return out_path, None
    except Exception as e:
        return None, str(e)


def convert_to_pdf(meta: dict, book_folder: str):
    if not HAS_PDF:
        return None, ("Библиотека reportlab не установлена.\n"
                      "Выполните: pip install reportlab")
    title = sanitize_filename(meta.get('title') or 'book')
    out_name = f"{BOOK_FILE_PREFIX}_{title}.pdf"
    out_path = os.path.join(book_folder, out_name)
    chapters = get_chapter_texts(book_folder)
    if not chapters:
        return None, "Нет глав для конвертации"
    try:
        doc = SimpleDocTemplate(out_path, pagesize=A4,
                                rightMargin=2 * cm, leftMargin=2 * cm,
                                topMargin=2 * cm, bottomMargin=2 * cm)
        styles = getSampleStyleSheet()
        title_style   = ParagraphStyle('Ttl', parent=styles['Title'],
                                       fontSize=18, spaceAfter=12)
        chapter_style = ParagraphStyle('Ch', parent=styles['Heading1'],
                                       fontSize=14, spaceAfter=8, spaceBefore=16)
        body_style    = ParagraphStyle('Bd', parent=styles['Normal'],
                                       fontSize=11, leading=14, spaceAfter=6)

        story = [Paragraph(meta.get('title') or 'Без названия', title_style)]
        if meta.get('author'):
            story.append(Paragraph(f"Автор: {meta['author']}", styles['Normal']))
        story.append(Spacer(1, 0.5 * cm))

        for num, name, text in chapters:
            story.append(PageBreak())
            story.append(Paragraph(f"Глава {num}: {name}", chapter_style))
            for para in text.split('\n'):
                para = para.strip()
                if para:
                    para_esc = (para.replace('&', '&amp;')
                                    .replace('<', '&lt;')
                                    .replace('>', '&gt;'))
                    story.append(Paragraph(para_esc, body_style))

        doc.build(story)
        return out_path, None
    except Exception as e:
        return None, str(e)


def convert_to_mobi(meta: dict, book_folder: str):
    epub_path, err = convert_to_epub(meta, book_folder)
    if err:
        return None, f"Не удалось создать EPUB для конвертации в MOBI: {err}"
    title = sanitize_filename(meta.get('title') or 'book')
    out_name = f"{BOOK_FILE_PREFIX}_{title}.mobi"
    out_path = os.path.join(book_folder, out_name)
    try:
        result = subprocess.run(
            ['ebook-convert', epub_path, out_path],
            capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            return out_path, None
        return None, f"Ошибка ebook-convert: {result.stderr[:300]}"
    except FileNotFoundError:
        return None, ("Calibre не установлен.\n"
                      "Установите Calibre и убедитесь, что ebook-convert "
                      "доступен в PATH.")
    except Exception as e:
        return None, str(e)


CONVERTERS = {
    'TXT':  ('txt',  convert_to_txt),
    'EPUB': ('epub', convert_to_epub),
    'FB2':  ('fb2',  convert_to_fb2),
    'PDF':  ('pdf',  convert_to_pdf),
    'MOBI': ('mobi', convert_to_mobi),
}


def run_conversion(fmt_label: str, book_folder: str):
    meta = load_metadata(book_folder)
    if not meta:
        messagebox.showerror('Ошибка', 'metadata.json не найден.')
        return
    ext, converter = CONVERTERS[fmt_label]
    write_log(f"Конвертация в {fmt_label}…")
    out_path, err = converter(meta, book_folder)
    if err:
        write_log(f"Ошибка конвертации: {err}")
        messagebox.showerror('Ошибка конвертации', err)
        return
    meta.setdefault('converted_formats', {})[ext] = os.path.basename(out_path)
    save_metadata(meta, book_folder)
    write_log(f"  ✓ {fmt_label} сохранён: {out_path}")
    messagebox.showinfo('Готово', f'Файл сохранён:\n{out_path}')
    root.after(0, lambda: display_metadata(load_metadata(book_folder)))


# ─── Открыть в читалке ────────────────────────────────────────────────────────

def open_in_reader(book_folder: str):
    meta = load_metadata(book_folder)
    if not meta:
        messagebox.showerror('Ошибка', 'metadata.json не найден.')
        return
    converted = meta.get('converted_formats') or {}
    if not converted:
        messagebox.showwarning(
            'Книга не конвертирована',
            'Сначала конвертируйте книгу в один из форматов:\n'
            'TXT, EPUB, FB2, PDF или MOBI.')
        return
    if len(converted) == 1:
        ext, fname = next(iter(converted.items()))
        _open_file(os.path.join(book_folder, fname))
    else:
        _show_format_chooser(converted, book_folder)


def _open_file(path: str):
    if not os.path.exists(path):
        messagebox.showerror('Ошибка', f'Файл не найден:\n{path}')
        return
    try:
        if platform.system() == 'Windows':
            os.startfile(path)
        elif platform.system() == 'Darwin':
            subprocess.Popen(['open', path])
        else:
            subprocess.Popen(['xdg-open', path])
    except Exception as e:
        messagebox.showerror('Ошибка', f'Не удалось открыть файл:\n{e}')


def _show_format_chooser(converted: dict, book_folder: str):
    dlg = tk.Toplevel(root)
    dlg.title('Выберите формат')
    dlg.configure(bg='#1e1e2e')
    dlg.resizable(False, False)
    dlg.grab_set()
    ttk.Label(dlg,
              text='Книга конвертирована в несколько форматов.\n'
                   'Выберите, в каком открыть:',
              font=('Segoe UI', 9), justify='center').pack(padx=20, pady=(16, 8))
    for ext, fname in converted.items():
        fpath = os.path.join(book_folder, fname)
        ttk.Button(
            dlg, text=ext.upper(),
            command=lambda p=fpath, d=dlg: (d.destroy(), _open_file(p))
        ).pack(padx=20, pady=4, fill='x')
    ttk.Button(dlg, text='Отмена', command=dlg.destroy).pack(
        padx=20, pady=(4, 16), fill='x')


# ─── Диалоги удаления ─────────────────────────────────────────────────────────

def delete_selected_book(book_folder: str):
    """Удалить папку выбранной книги (и её метаданные)."""
    meta = load_metadata(book_folder)
    book_title = (meta.get('title') or os.path.basename(book_folder)) if meta else os.path.basename(book_folder)

    dlg = tk.Toplevel(root)
    dlg.title('Удалить книгу')
    dlg.configure(bg='#1e1e2e')
    dlg.resizable(False, False)
    dlg.grab_set()

    ttk.Label(
        dlg,
        text=f'⚠  Удалить книгу?\n\n«{book_title}»\n\n'
             'Папка и все файлы будут безвозвратно удалены.',
        font=('Segoe UI', 10), foreground='#f38ba8',
        justify='center').pack(padx=24, pady=(18, 8))

    btn_row = ttk.Frame(dlg)
    btn_row.pack(pady=(8, 18))

    def on_yes():
        dlg.destroy()
        try:
            shutil.rmtree(book_folder)
            write_log(f"Книга удалена: {book_title}")
        except Exception as e:
            messagebox.showwarning('Ошибка', f'Не удалось удалить папку:\n{e}')
        root.after(0, refresh_library)

    ttk.Button(btn_row, text='Да, удалить',
               style='Danger.TButton', command=on_yes).pack(side='left', padx=8)
    ttk.Button(btn_row, text='Отмена', command=dlg.destroy).pack(side='left', padx=8)


def delete_converted_files(book_folder: str):
    meta = load_metadata(book_folder)
    if not meta:
        messagebox.showerror('Ошибка', 'metadata.json не найден.')
        return
    converted = meta.get('converted_formats') or {}
    if not converted:
        messagebox.showinfo('Нет файлов',
                            'Нет конвертированных файлов для удаления.')
        return

    dlg = tk.Toplevel(root)
    dlg.title('Удалить конвертированные файлы')
    dlg.configure(bg='#1e1e2e')
    dlg.resizable(False, False)
    dlg.grab_set()

    ttk.Label(dlg, text='Выберите форматы для удаления:',
              font=('Segoe UI', 9)).pack(padx=20, pady=(16, 8))

    checks = {}
    for ext, fname in converted.items():
        var = tk.BooleanVar()
        ttk.Checkbutton(
            dlg, text=f"{ext.upper()} — {fname}",
            variable=var).pack(padx=20, pady=2, anchor='w')
        checks[ext] = (var, fname)

    btn_row = ttk.Frame(dlg)
    btn_row.pack(pady=(12, 16))

    def on_delete():
        to_del = [(e, fn) for e, (v, fn) in checks.items() if v.get()]
        if not to_del:
            messagebox.showinfo('Ничего не выбрано',
                                'Выберите хотя бы один формат.', parent=dlg)
            return
        for ext, fname in to_del:
            fpath = os.path.join(book_folder, fname)
            try:
                if os.path.exists(fpath):
                    os.remove(fpath)
                meta['converted_formats'].pop(ext, None)
                write_log(f"Удалён: {fname}")
            except Exception as e:
                write_log(f"Ошибка удаления {fname}: {e}")
        save_metadata(meta, book_folder)
        root.after(0, lambda: display_metadata(load_metadata(book_folder)))
        dlg.destroy()

    ttk.Button(btn_row, text='Удалить',
               style='Warn.TButton', command=on_delete).pack(side='left', padx=8)
    ttk.Button(btn_row, text='Отмена', command=dlg.destroy).pack(
        side='left', padx=8)


def delete_separate_chapters(book_folder: str):
    dlg = tk.Toplevel(root)
    dlg.title('Удалить главы')
    dlg.configure(bg='#1e1e2e')
    dlg.resizable(False, False)
    dlg.grab_set()

    ttk.Label(
        dlg,
        text='⚠  Внимание!\n\n'
             'После удаления отдельных файлов глав\n'
             'книгу нельзя будет конвертировать в другие форматы.\n'
             'Для повторной конвертации придётся скачивать книгу заново.\n\n'
             'metadata.json и конвертированные файлы останутся нетронутыми.',
        font=('Segoe UI', 9), foreground='#fab387',
        justify='center').pack(padx=24, pady=(18, 8))

    btn_row = ttk.Frame(dlg)
    btn_row.pack(pady=(8, 18))

    def on_confirm():
        chapter_files = sorted_chapter_files(book_folder)
        deleted = 0
        for fpath in chapter_files:
            try:
                os.remove(fpath)
                deleted += 1
            except Exception as e:
                write_log(f"Ошибка удаления {fpath}: {e}")
        write_log(f"Удалено файлов глав: {deleted}")
        dlg.destroy()
        messagebox.showinfo('Готово', f'Удалено файлов глав: {deleted}')

    ttk.Button(btn_row, text='Удалить главы',
               style='Warn.TButton', command=on_confirm).pack(side='left', padx=8)
    ttk.Button(btn_row, text='Отмена', command=dlg.destroy).pack(
        side='left', padx=8)


# ─── GUI ──────────────────────────────────────────────────────────────────────

root = tk.Tk()
root.title("FicBook Downloader")
root.geometry("1080x780")
root.configure(bg='#1e1e2e')

style = ttk.Style()
style.theme_use('clam')
style.configure('TNotebook',     background='#1e1e2e', borderwidth=0)
style.configure('TNotebook.Tab', background='#313244', foreground='#cdd6f4',
                padding=[10, 4], font=('Segoe UI', 9))
style.map('TNotebook.Tab',
          background=[('selected', '#89b4fa')],
          foreground=[('selected', '#1e1e2e')])
style.configure('TFrame',      background='#1e1e2e')
style.configure('TLabel',      background='#1e1e2e', foreground='#cdd6f4',
                font=('Segoe UI', 9))
style.configure('TEntry',      fieldbackground='#313244', foreground='#cdd6f4')
style.configure('TButton',     background='#89b4fa', foreground='#1e1e2e',
                font=('Segoe UI', 9, 'bold'), padding=[6, 3])
style.map('TButton',           background=[('active', '#b4befe')])
style.configure('TCheckbutton', background='#1e1e2e', foreground='#cdd6f4')
style.configure('Danger.TButton', background='#f38ba8', foreground='#1e1e2e',
                font=('Segoe UI', 9, 'bold'), padding=[6, 3])
style.map('Danger.TButton',    background=[('active', '#eba0ac')])
style.configure('Warn.TButton',   background='#fab387', foreground='#1e1e2e',
                font=('Segoe UI', 9, 'bold'), padding=[6, 3])
style.map('Warn.TButton',      background=[('active', '#f9e2af')])
style.configure('Green.TButton',  background='#a6e3a1', foreground='#1e1e2e',
                font=('Segoe UI', 9, 'bold'), padding=[6, 3])
style.map('Green.TButton',     background=[('active', '#94e2d5')])

notebook = ttk.Notebook(root)
notebook.pack(expand=True, fill='both', padx=8, pady=8)

# ── Вкладка: Настройки ────────────────────────────────────────────────────────
tab_settings = ttk.Frame(notebook)
notebook.add(tab_settings, text='⚙ Настройки')

settings_inner = ttk.Frame(tab_settings)
settings_inner.pack(padx=20, pady=20, fill='x')

ttk.Label(settings_inner, text='Путь сохранения:').grid(
    row=0, column=0, sticky='w', pady=4)

saved_path = _settings.get('base_path', os.getcwd())
path_label = ttk.Label(settings_inner, text=saved_path,
                        relief='sunken', width=55, anchor='w',
                        background='#313244', foreground='#a6e3a1')
path_label.grid(row=0, column=1, padx=8, sticky='w', pady=4)


def select_path():
    p = filedialog.askdirectory(initialdir=path_label.cget('text'),
                                title='Выберите путь сохранения')
    if p:
        path_label.config(text=p)
        _settings['base_path'] = p
        save_settings()


ttk.Button(settings_inner, text='Выбрать…', command=select_path).grid(
    row=0, column=2, padx=4, pady=4)

ttk.Label(settings_inner, text='Название папки:').grid(
    row=1, column=0, sticky='w', pady=4)
folder_name_entry = ttk.Entry(settings_inner, width=30)
folder_name_entry.insert(0, _settings.get('folder_name', 'books'))
folder_name_entry.grid(row=1, column=1, sticky='w', padx=8, pady=4)


def _on_folder_name_change(*_):
    _settings['folder_name'] = folder_name_entry.get().strip()
    save_settings()


folder_name_entry.bind('<FocusOut>', _on_folder_name_change)

create_folder_var = tk.IntVar(value=_settings.get('create_subfolder', 1))


def _on_create_folder_change():
    _settings['create_subfolder'] = int(create_folder_var.get())
    save_settings()


ttk.Checkbutton(settings_inner, text='Создать подпапку',
                variable=create_folder_var,
                command=_on_create_folder_change).grid(
    row=2, column=1, sticky='w', padx=8)

# ── Задержка между скачиванием ────────────────────────────────────────────────
ttk.Label(settings_inner, text='Задержка между главами (сек):').grid(
    row=3, column=0, sticky='w', pady=4)
delay_entry = ttk.Entry(settings_inner, width=8)
delay_entry.insert(0, str(_settings.get('download_delay', 0.4)))
delay_entry.grid(row=3, column=1, sticky='w', padx=8, pady=4)
ttk.Label(settings_inner, text='(напр. 0.4, 1.0, 2.0)',
          foreground='#6c7086', font=('Segoe UI', 8)).grid(
    row=3, column=2, sticky='w')


def _on_delay_change(*_):
    try:
        v = float(delay_entry.get().strip())
        if v < 0:
            v = 0.0
        _settings['download_delay'] = v
        save_settings()
    except ValueError:
        pass


delay_entry.bind('<FocusOut>', _on_delay_change)
delay_entry.bind('<Return>',   _on_delay_change)

# ── Автодополнение ────────────────────────────────────────────────────────────
autocomplete_var = tk.BooleanVar(
    value=bool(_settings.get('autocomplete_enabled', True)))


def _on_autocomplete_change():
    _settings['autocomplete_enabled'] = autocomplete_var.get()
    save_settings()


ttk.Checkbutton(settings_inner,
                text='Автодополнение при поиске (из индекса fbd_index.json)',
                variable=autocomplete_var,
                command=_on_autocomplete_change).grid(
    row=4, column=0, columnspan=3, sticky='w', padx=8, pady=4)

ttk.Label(settings_inner,
          text='Для конвертации в EPUB: pip install ebooklib\n'
               'Для конвертации в PDF: pip install reportlab\n'
               'Для конвертации в MOBI: установите Calibre',
          font=('Segoe UI', 8), foreground='#6c7086').grid(
    row=5, column=0, columnspan=3, sticky='w', padx=8, pady=(16, 0))

# ── Вкладка: Коллекция ────────────────────────────────────────────────────────
tab_collection = ttk.Frame(notebook)
notebook.add(tab_collection, text='📚 Коллекция')

coll_inner = ttk.Frame(tab_collection)
coll_inner.pack(padx=20, pady=20, fill='x')

ttk.Label(coll_inner, text='ID коллекции:').pack(anchor='w')
collection_id_entry = ttk.Entry(coll_inner, width=30)
collection_id_entry.pack(pady=4, anchor='w')

btn_row_c = ttk.Frame(coll_inner)
btn_row_c.pack(anchor='w', pady=6)


def start_scraping():
    if not set_output_folder():
        return
    stop_event.clear()
    cid = collection_id_entry.get().strip()
    if not cid:
        messagebox.showerror('Ошибка', 'Введите ID коллекции.')
        return
    url = f"https://ficbook.net/collections/{cid}?p="
    Thread(target=process_collection, args=(url,), daemon=True).start()


ttk.Button(btn_row_c, text='▶ Начать', command=start_scraping).pack(
    side='left', padx=4)
ttk.Button(btn_row_c, text='⏹ Стоп',
           command=lambda: stop_event.set()).pack(side='left', padx=4)

# ── Вкладка: Конкретные книги ─────────────────────────────────────────────────
tab_books = ttk.Frame(notebook)
notebook.add(tab_books, text='📖 Книги по ID')

books_inner = ttk.Frame(tab_books)
books_inner.pack(padx=20, pady=20, fill='x')

ttk.Label(books_inner, text='ID книг (через запятую):').pack(anchor='w')
book_ids_entry = ttk.Entry(books_inner, width=55)
book_ids_entry.pack(pady=4, anchor='w')

btn_row_b = ttk.Frame(books_inner)
btn_row_b.pack(anchor='w', pady=6)


def download_specific_books():
    if not set_output_folder():
        return
    stop_event.clear()
    ids = [i.strip() for i in book_ids_entry.get().split(',') if i.strip()]
    if not ids:
        messagebox.showerror('Ошибка', 'Введите хотя бы один ID.')
        return

    def run():
        for fic_id in ids:
            if stop_event.is_set():
                break
            url = f"https://ficbook.net/readfic/{fic_id}"
            extract_content_from_url(url, fic_id, 0)
        messagebox.showinfo('Готово', 'Книги загружены!')
        root.after(0, refresh_library)

    Thread(target=run, daemon=True).start()


ttk.Button(btn_row_b, text='▶ Скачать',
           command=download_specific_books).pack(side='left', padx=4)
ttk.Button(btn_row_b, text='⏹ Стоп',
           command=lambda: stop_event.set()).pack(side='left', padx=4)

# ── Вкладка: Интервал ─────────────────────────────────────────────────────────
tab_interval = ttk.Frame(notebook)
notebook.add(tab_interval, text='🔢 Интервал')

intv_inner = ttk.Frame(tab_interval)
intv_inner.pack(padx=20, pady=20, fill='x')

ttk.Label(intv_inner, text='Начальный ID:').pack(anchor='w')
start_id_entry = ttk.Entry(intv_inner, width=20)
start_id_entry.pack(pady=4, anchor='w')
ttk.Label(intv_inner, text='Конечный ID:').pack(anchor='w')
end_id_entry = ttk.Entry(intv_inner, width=20)
end_id_entry.pack(pady=4, anchor='w')

btn_row_i = ttk.Frame(intv_inner)
btn_row_i.pack(anchor='w', pady=6)


def download_interval_books():
    if not set_output_folder():
        return
    stop_event.clear()
    try:
        s = int(start_id_entry.get().strip())
        e = int(end_id_entry.get().strip())
    except ValueError:
        messagebox.showerror('Ошибка', 'Введите числовые ID.')
        return

    def run():
        for fic_id in range(s, e + 1):
            if stop_event.is_set():
                break
            url = f"https://ficbook.net/readfic/{fic_id}"
            extract_content_from_url(url, fic_id, 0)
        messagebox.showinfo('Готово', f'Книги {s}–{e} загружены!')
        root.after(0, refresh_library)

    Thread(target=run, daemon=True).start()


ttk.Button(btn_row_i, text='▶ Скачать',
           command=download_interval_books).pack(side='left', padx=4)
ttk.Button(btn_row_i, text='⏹ Стоп',
           command=lambda: stop_event.set()).pack(side='left', padx=4)


# ─── Виджеты поиска: автодополнение и тег-система ────────────────────────────

class AutocompleteEntry:
    """Поле ввода с выпадающим списком автодополнения."""

    def __init__(self, parent, index_key: str, width: int = 18):
        self.index_key = index_key
        self._dropdown_win = None
        self._listbox = None

        self.var = tk.StringVar()
        self.entry = ttk.Entry(parent, textvariable=self.var, width=width)
        self.var.trace_add('write', self._on_type)
        self.entry.bind('<Down>',     self._focus_dropdown)
        self.entry.bind('<Escape>',   lambda e: self._hide())
        self.entry.bind('<FocusOut>', lambda e: root.after(150, self._hide))

    def _on_type(self, *_):
        if not autocomplete_var.get():
            self._hide()
            return
        text = self.var.get().strip()
        if not text:
            self._hide()
            return
        idx = load_index()
        suggestions = rank_suggestions(text, idx.get(self.index_key, []))
        if suggestions:
            self._show(suggestions)
        else:
            self._hide()

    def _show(self, items: list):
        x = self.entry.winfo_rootx()
        y = self.entry.winfo_rooty() + self.entry.winfo_height()
        w = max(self.entry.winfo_width(), 220)
        h = min(len(items) * 22 + 4, 200)

        if self._dropdown_win is None:
            self._dropdown_win = tk.Toplevel(root)
            self._dropdown_win.wm_overrideredirect(True)
            self._dropdown_win.configure(bg='#45475a')
            self._listbox = tk.Listbox(
                self._dropdown_win,
                bg='#313244', fg='#cdd6f4',
                selectbackground='#89b4fa', selectforeground='#1e1e2e',
                font=('Segoe UI', 9), relief='flat', bd=1,
                highlightthickness=0, activestyle='none')
            self._listbox.pack(fill='both', expand=True, padx=1, pady=1)
            self._listbox.bind('<<ListboxSelect>>', self._on_select)
            self._listbox.bind('<Return>',          self._on_select)
            self._listbox.bind('<Escape>',          lambda e: self._hide())
            self._listbox.bind('<FocusOut>',        lambda e: root.after(150, self._hide))

        self._listbox.delete(0, 'end')
        for item in items:
            self._listbox.insert('end', item)

        self._dropdown_win.geometry(f"{w}x{h}+{x}+{y}")
        self._dropdown_win.lift()

    def _hide(self, *_):
        if self._dropdown_win:
            self._dropdown_win.destroy()
            self._dropdown_win = None
            self._listbox = None

    def _focus_dropdown(self, *_):
        if self._listbox:
            self._listbox.focus_set()
            if self._listbox.size() > 0:
                self._listbox.selection_set(0)

    def _on_select(self, *_):
        if self._listbox:
            sel = self._listbox.curselection()
            if sel:
                self.var.set(self._listbox.get(sel[0]))
        self._hide()
        self.entry.focus_set()

    def get(self) -> str:
        return self.var.get()

    def set(self, v: str):
        self.var.set(v)

    def delete(self, *_):
        self.var.set('')

    def pack(self, **kw):
        self.entry.pack(**kw)

    def grid(self, **kw):
        self.entry.grid(**kw)


class TagField:
    """
    Поле для выбора нескольких тегов с разделением на позитивные (зелёные)
    и негативные/исключающие (красные). Поддерживает автодополнение.
    """

    def __init__(self, parent, label: str, index_key: str):
        self.index_key = index_key
        self.positive: list = []   # включить
        self.negative: list = []   # исключить
        self._dropdown_win = None
        self._listbox = None

        # Внешний фрейм
        self.frame = ttk.Frame(parent)

        # Метка
        ttk.Label(self.frame, text=label,
                  font=('Segoe UI', 8, 'bold'),
                  foreground='#a6adc8').pack(anchor='w', pady=(2, 0))

        # Строка ввода
        input_row = ttk.Frame(self.frame)
        input_row.pack(fill='x')

        self.var = tk.StringVar()
        self.entry = ttk.Entry(input_row, textvariable=self.var, width=22)
        self.entry.pack(side='left')
        self.var.trace_add('write', self._on_type)
        self.entry.bind('<Return>',   lambda e: self.add_positive())
        self.entry.bind('<Down>',     self._focus_dropdown)
        self.entry.bind('<Escape>',   lambda e: self._hide_dropdown())
        self.entry.bind('<FocusOut>', lambda e: root.after(150, self._hide_dropdown))

        ttk.Button(input_row, text='+ Добавить',
                   command=self.add_positive).pack(side='left', padx=(4, 2))
        ttk.Button(input_row, text='− Исключить',
                   style='Warn.TButton',
                   command=self.add_negative).pack(side='left', padx=(0, 2))

        # Фрейм чипов
        self._chips_outer = ttk.Frame(self.frame)
        self._chips_outer.pack(fill='x', pady=(2, 0))

    # ── Автодополнение ─────────────────────────────────────────────────────

    def _on_type(self, *_):
        if not autocomplete_var.get():
            self._hide_dropdown()
            return
        text = self.var.get().strip()
        if not text:
            self._hide_dropdown()
            return
        idx = load_index()
        suggestions = rank_suggestions(text, idx.get(self.index_key, []))
        if suggestions:
            self._show_dropdown(suggestions)
        else:
            self._hide_dropdown()

    def _show_dropdown(self, items: list):
        x = self.entry.winfo_rootx()
        y = self.entry.winfo_rooty() + self.entry.winfo_height()
        w = max(self.entry.winfo_width() + 160, 280)
        h = min(len(items) * 22 + 4, 200)

        if self._dropdown_win is None:
            self._dropdown_win = tk.Toplevel(root)
            self._dropdown_win.wm_overrideredirect(True)
            self._dropdown_win.configure(bg='#45475a')
            self._listbox = tk.Listbox(
                self._dropdown_win,
                bg='#313244', fg='#cdd6f4',
                selectbackground='#89b4fa', selectforeground='#1e1e2e',
                font=('Segoe UI', 9), relief='flat', bd=1,
                highlightthickness=0, activestyle='none')
            self._listbox.pack(fill='both', expand=True, padx=1, pady=1)
            self._listbox.bind('<<ListboxSelect>>', self._on_dropdown_select)
            self._listbox.bind('<Return>',          self._on_dropdown_select)
            self._listbox.bind('<Escape>',          lambda e: self._hide_dropdown())
            self._listbox.bind('<FocusOut>',        lambda e: root.after(150, self._hide_dropdown))

        self._listbox.delete(0, 'end')
        for item in items:
            self._listbox.insert('end', item)

        self._dropdown_win.geometry(f"{w}x{h}+{x}+{y}")
        self._dropdown_win.lift()

    def _hide_dropdown(self, *_):
        if self._dropdown_win:
            self._dropdown_win.destroy()
            self._dropdown_win = None
            self._listbox = None

    def _focus_dropdown(self, *_):
        if self._listbox:
            self._listbox.focus_set()
            if self._listbox.size() > 0:
                self._listbox.selection_set(0)

    def _on_dropdown_select(self, *_):
        if self._listbox:
            sel = self._listbox.curselection()
            if sel:
                self.var.set(self._listbox.get(sel[0]))
        self._hide_dropdown()
        self.entry.focus_set()

    # ── Управление тегами ─────────────────────────────────────────────────

    def add_positive(self):
        val = self.var.get().strip()
        if not val:
            return
        if val not in self.positive and val not in self.negative:
            self.positive.append(val)
            self.var.set('')
            self._refresh_chips()

    def add_negative(self):
        val = self.var.get().strip()
        if not val:
            return
        if val not in self.negative and val not in self.positive:
            self.negative.append(val)
            self.var.set('')
            self._refresh_chips()

    def _refresh_chips(self):
        for w in self._chips_outer.winfo_children():
            w.destroy()

        row = tk.Frame(self._chips_outer, bg='#1e1e2e')
        row.pack(fill='x', anchor='w')

        for tag in self.positive:
            self._make_chip(row, tag, '#1a3a28', '#a6e3a1', True)
        for tag in self.negative:
            self._make_chip(row, tag, '#3a1a1a', '#f38ba8', False)

    def _make_chip(self, parent, tag: str, bg: str, fg: str, positive: bool):
        chip = tk.Frame(parent, bg=bg, bd=0, padx=5, pady=2)
        chip.pack(side='left', padx=2, pady=1)

        tk.Label(chip, text=tag, bg=bg, fg=fg,
                 font=('Segoe UI', 8)).pack(side='left')
        tk.Button(chip, text=' ×', bg=bg, fg=fg,
                  font=('Segoe UI', 8, 'bold'),
                  relief='flat', bd=0, cursor='hand2',
                  activebackground=bg, activeforeground='#cdd6f4',
                  command=lambda t=tag, p=positive: self._remove(t, p)
                  ).pack(side='left', padx=(2, 0))

    def _remove(self, tag: str, positive: bool):
        if positive:
            self.positive = [t for t in self.positive if t != tag]
        else:
            self.negative = [t for t in self.negative if t != tag]
        self._refresh_chips()

    def clear(self):
        self.positive.clear()
        self.negative.clear()
        self.var.set('')
        self._hide_dropdown()
        self._refresh_chips()

    def get_positive(self) -> list:
        return [t.lower() for t in self.positive]

    def get_negative(self) -> list:
        return [t.lower() for t in self.negative]

    def pack(self, **kw):
        self.frame.pack(**kw)

    def grid(self, **kw):
        self.frame.grid(**kw)


# ── Вкладка: Библиотека ───────────────────────────────────────────────────────
tab_library = ttk.Frame(notebook)
notebook.add(tab_library, text='🗂 Библиотека')

# ── Панель поиска ─────────────────────────────────────────────────────────────
search_outer = ttk.Frame(tab_library)
search_outer.pack(fill='x', padx=8, pady=(8, 0))

# Строка 1: простые поля
row1 = ttk.Frame(search_outer)
row1.pack(fill='x', pady=(0, 2))

ttk.Label(row1, text='🔍', font=('Segoe UI', 11)).pack(side='left', padx=(0, 6))

# Название с автодополнением
ttk.Label(row1, text='Название:', font=('Segoe UI', 8)).pack(side='left')
title_search = AutocompleteEntry(row1, 'titles', width=20)
title_search.pack(side='left', padx=(2, 8))

# Автор с автодополнением
ttk.Label(row1, text='Автор:', font=('Segoe UI', 8)).pack(side='left')
author_search = AutocompleteEntry(row1, 'authors', width=16)
author_search.pack(side='left', padx=(2, 8))

# Глав
ttk.Label(row1, text='Глав:', font=('Segoe UI', 8)).pack(side='left')
chapters_entry = ttk.Entry(row1, width=10)
chapters_entry.pack(side='left', padx=(2, 8))
ttk.Label(row1, text='(5 или 5-20)', font=('Segoe UI', 7),
          foreground='#6c7086').pack(side='left')

# Строки 2-4: тег-системы
tags_row = ttk.Frame(search_outer)
tags_row.pack(fill='x', pady=(2, 0))

tag_fandom  = TagField(tags_row, '🌐 Фэндомы',                  'fandoms')
tag_pairing = TagField(tags_row, '💞 Пэйринги и персонажи',     'pairings')
tag_tags    = TagField(tags_row, '🏷 Теги',                      'tags')

tag_fandom.grid( row=0, column=0, padx=(0, 16), sticky='nw')
tag_pairing.grid(row=0, column=1, padx=(0, 16), sticky='nw')
tag_tags.grid(   row=0, column=2, sticky='nw')

# Кнопки поиска
btn_search_row = ttk.Frame(tab_library)
btn_search_row.pack(fill='x', padx=8, pady=4)

ttk.Button(btn_search_row, text='🔍 Искать',
           command=lambda: search_books()).pack(side='left', padx=4)
ttk.Button(btn_search_row, text='↺ Сбросить',
           command=lambda: reset_search()).pack(side='left', padx=4)
ttk.Button(btn_search_row, text='🔄 Обновить',
           command=lambda: refresh_library()).pack(side='left', padx=4)

# ── Основная область: список слева + детали справа ────────────────────────────
paned = ttk.PanedWindow(tab_library, orient='horizontal')
paned.pack(fill='both', expand=True, padx=8, pady=4)

# -- Левая панель
left_frame = ttk.Frame(paned)
paned.add(left_frame, weight=1)

ttk.Label(left_frame, text='Книги:',
          font=('Segoe UI', 9, 'bold')).pack(anchor='w', padx=4, pady=(4, 0))

list_frame = ttk.Frame(left_frame)
list_frame.pack(fill='both', expand=True, padx=4, pady=4)

books_listbox = tk.Listbox(
    list_frame, selectmode='single',
    bg='#313244', fg='#cdd6f4',
    selectbackground='#89b4fa', selectforeground='#1e1e2e',
    font=('Segoe UI', 9), relief='flat', bd=0, highlightthickness=0)
books_listbox.pack(side='left', fill='both', expand=True)

list_scrollbar = ttk.Scrollbar(list_frame, orient='vertical',
                                command=books_listbox.yview)
list_scrollbar.pack(side='right', fill='y')
books_listbox.config(yscrollcommand=list_scrollbar.set)

books_count_label = ttk.Label(left_frame, text='',
                               font=('Segoe UI', 8), foreground='#6c7086')
books_count_label.pack(anchor='w', padx=4, pady=2)

# -- Правая панель
right_frame = ttk.Frame(paned)
paned.add(right_frame, weight=2)

ttk.Label(right_frame, text='Метаданные:',
          font=('Segoe UI', 9, 'bold')).pack(anchor='w', padx=4, pady=(4, 0))

meta_text = scrolledtext.ScrolledText(
    right_frame, wrap='word', width=50,
    bg='#313244', fg='#cdd6f4', font=('Segoe UI', 9),
    relief='flat', bd=0, padx=8, pady=8, state='disabled')
meta_text.pack(fill='both', expand=True, padx=4, pady=4)

for tag, color, font_extra in [
    ('header',    '#89b4fa', ('Segoe UI', 10, 'bold')),
    ('key',       '#a6e3a1', ('Segoe UI', 9, 'bold')),
    ('value',     '#cdd6f4', ('Segoe UI', 9)),
    ('muted',     '#6c7086', ('Segoe UI', 8)),
    ('tag_item',  '#f38ba8', ('Segoe UI', 9)),
    ('converted', '#a6e3a1', ('Segoe UI', 9, 'bold')),
]:
    meta_text.tag_configure(tag, foreground=color, font=font_extra)

# ── Кнопки действий ───────────────────────────────────────────────────────────
action_frame = ttk.Frame(right_frame)
action_frame.pack(fill='x', padx=4, pady=(0, 4))

conv_frame = ttk.Frame(action_frame)
conv_frame.pack(fill='x', pady=2)
ttk.Label(conv_frame, text='Конвертировать:',
          font=('Segoe UI', 8, 'bold')).pack(side='left', padx=4)
for fmt_label in ['TXT', 'EPUB', 'FB2', 'PDF', 'MOBI']:
    ttk.Button(conv_frame, text=fmt_label,
               command=lambda f=fmt_label: _on_convert(f)).pack(
        side='left', padx=2)

misc_frame = ttk.Frame(action_frame)
misc_frame.pack(fill='x', pady=2)
ttk.Button(misc_frame, text='📖 Открыть в читалке',
           style='Green.TButton',
           command=lambda: _on_open_reader()).pack(side='left', padx=2)
ttk.Button(misc_frame, text='🗑 Удалить конвертированные',
           style='Warn.TButton',
           command=lambda: _on_delete_converted()).pack(side='left', padx=2)
ttk.Button(misc_frame, text='📄 Удалить главы',
           style='Warn.TButton',
           command=lambda: _on_delete_chapters()).pack(side='left', padx=2)
ttk.Button(misc_frame, text='🗑 Удалить книгу',
           style='Danger.TButton',
           command=lambda: _on_delete_book()).pack(side='right', padx=2)

# ─── Хранилище: имя_папки → полный путь ──────────────────────────────────────
_library_folders: dict[str, str] = {}


def _get_selected_folder() -> str | None:
    sel = books_listbox.curselection()
    if not sel:
        messagebox.showinfo('Выберите книгу', 'Сначала выберите книгу из списка.')
        return None
    return _library_folders.get(books_listbox.get(sel[0]))


def _on_convert(fmt: str):
    folder = _get_selected_folder()
    if folder:
        Thread(target=run_conversion, args=(fmt, folder), daemon=True).start()


def _on_open_reader():
    folder = _get_selected_folder()
    if folder:
        open_in_reader(folder)


def _on_delete_converted():
    folder = _get_selected_folder()
    if folder:
        delete_converted_files(folder)


def _on_delete_chapters():
    folder = _get_selected_folder()
    if folder:
        delete_separate_chapters(folder)


def _on_delete_book():
    folder = _get_selected_folder()
    if folder:
        delete_selected_book(folder)


# ─── Вспомогательные функции отображения ─────────────────────────────────────

def _set_meta_text(content_fn):
    meta_text.config(state='normal')
    meta_text.delete('1.0', 'end')
    content_fn()
    meta_text.config(state='disabled')


def _append(text: str, tag: str = 'value'):
    meta_text.insert('end', text, tag)


def display_metadata(meta: dict | None):
    if not meta:
        _set_meta_text(lambda: _append('Метаданные не найдены.\n', 'muted'))
        return

    def build():
        _append(f"  {meta.get('title') or '—'}\n", 'header')
        _append('\n')

        rows = [
            ('ID',                     meta.get('fic_id')),
            ('Автор',                  meta.get('author')),
            ('Статус',                 meta.get('status')),
            ('Рейтинг',                meta.get('rating')),
            ('Направление',            meta.get('direction')),
            ('Вселенная',              meta.get('universe')),
            ('Фэндом',                 ', '.join(meta.get('fandom') or []) or None),
            ('Пэйринги и персонажи',   ', '.join(meta.get('pairing') or []) or None),
            ('Размер',                 meta.get('size_raw')),
            ('Страниц',                meta.get('pages')),
            ('Слов (сайт)',            meta.get('words')),
            ('Глав',                   meta.get('chapters_count')),
            ('Слов (файлы)',           meta.get('total_words')),
            ('Символов',               meta.get('total_chars')),
            ('Размер папки',           (f"{meta.get('size_kb')} КБ"
                                        if meta.get('size_kb') is not None else None)),
        ]
        for k, v in rows:
            if v is not None:
                _append(f"{k}: ", 'key')
                _append(f"{v}\n", 'value')

        converted = meta.get('converted_formats') or {}
        if converted:
            _append('\nКонвертировано: ', 'key')
            _append(', '.join(ext.upper() for ext in converted) + '\n', 'converted')

        tags = meta.get('tags') or []
        if tags:
            _append('\nМетки:\n', 'key')
            _append('  ' + '  •  '.join(tags) + '\n', 'tag_item')

        chapters = meta.get('chapter_titles') or []
        if chapters:
            _append(f'\nГлавы ({len(chapters)}):\n', 'key')
            for i, ch in enumerate(chapters, 1):
                _append(f"  {i}. {ch or '—'}\n", 'muted')

        for label, field in [('Описание', 'description'),
                              ('Примечания', 'notes'),
                              ('Посвящение', 'dedication')]:
            v = meta.get(field)
            if v:
                _append(f'\n{label}:\n', 'key')
                snippet = v if len(v) <= 400 else v[:400] + '…'
                _append(f"  {snippet}\n", 'muted')

    _set_meta_text(build)


def on_book_select(event=None):
    sel = books_listbox.curselection()
    if not sel:
        return
    display_name = books_listbox.get(sel[0])
    folder = _library_folders.get(display_name)
    if not folder:
        return

    def load():
        _set_meta_text(lambda: _append('Загрузка…\n', 'muted'))
        meta = load_metadata(folder)
        if meta:
            display_metadata(meta)
        else:
            _set_meta_text(lambda: _append(
                f'metadata.json не найден.\nПапка: {folder}\n', 'muted'))

    Thread(target=load, daemon=True).start()


books_listbox.bind('<<ListboxSelect>>', on_book_select)


def _resolve_output_folder() -> bool:
    global output_folder

    dlg = tk.Toplevel(root)
    dlg.title('Папка не найдена')
    dlg.configure(bg='#1e1e2e')
    dlg.grab_set()
    result = [False]

    ttk.Label(
        dlg,
        text=f'Папка библиотеки не найдена:\n{output_folder}\n\n'
             'Введите путь вручную или выберите папку:',
        font=('Segoe UI', 9), justify='left').pack(padx=20, pady=(16, 8))

    path_var = tk.StringVar(value=output_folder)
    entry = ttk.Entry(dlg, textvariable=path_var, width=52)
    entry.pack(padx=20, pady=4)

    def browse():
        p = filedialog.askdirectory(title='Выберите папку библиотеки')
        if p:
            path_var.set(p)

    def confirm():
        p = path_var.get().strip()
        if os.path.isdir(p):
            global output_folder
            output_folder = p
            _settings['output_folder'] = p
            save_settings()
            result[0] = True
            dlg.destroy()
        else:
            messagebox.showerror('Ошибка',
                                 f'Папка не существует:\n{p}', parent=dlg)

    btn_row = ttk.Frame(dlg)
    btn_row.pack(pady=8)
    ttk.Button(btn_row, text='Обзор…',      command=browse).pack(side='left', padx=4)
    ttk.Button(btn_row, text='Подтвердить', command=confirm).pack(side='left', padx=4)
    ttk.Button(btn_row, text='Отмена',      command=dlg.destroy).pack(
        side='left', padx=4)

    dlg.wait_window()
    return result[0]


def refresh_library():
    global output_folder
    _library_folders.clear()
    books_listbox.delete(0, 'end')
    _set_meta_text(lambda: _append('Выберите книгу слева.\n', 'muted'))

    if not os.path.isdir(output_folder):
        if not _resolve_output_folder():
            books_count_label.config(text='Папка не найдена')
            return

    entries = []
    try:
        for entry in os.scandir(output_folder):
            if entry.is_dir():
                entries.append(entry.name)
    except Exception as e:
        write_log(f"Ошибка сканирования библиотеки: {e}")
        return

    entries.sort(key=str.lower)
    for name in entries:
        full = os.path.join(output_folder, name)
        _library_folders[name] = full
        books_listbox.insert('end', name)

    books_count_label.config(text=f'{len(entries)} книг')


def search_books():
    q_title  = title_search.get().strip().lower()
    q_author = author_search.get().strip().lower()

    pos_fandoms  = tag_fandom.get_positive()
    neg_fandoms  = tag_fandom.get_negative()
    pos_pairings = tag_pairing.get_positive()
    neg_pairings = tag_pairing.get_negative()
    pos_tags     = tag_tags.get_positive()
    neg_tags     = tag_tags.get_negative()

    q_chapters_raw = chapters_entry.get().strip()
    ch_min = ch_max = None
    if q_chapters_raw:
        mm = re.match(r'(\d+)\s*[-–]\s*(\d+)', q_chapters_raw)
        if mm:
            ch_min, ch_max = int(mm.group(1)), int(mm.group(2))
        elif q_chapters_raw.isdigit():
            ch_min = ch_max = int(q_chapters_raw)

    has_query = any([
        q_title, q_author,
        pos_fandoms, neg_fandoms,
        pos_pairings, neg_pairings,
        pos_tags, neg_tags,
        ch_min is not None,
    ])

    if not has_query:
        refresh_library()
        return

    _set_meta_text(lambda: _append('Поиск…\n', 'muted'))

    def run_search():
        matches = []
        folder = output_folder
        if not os.path.isdir(folder):
            return

        for entry in os.scandir(folder):
            if not entry.is_dir():
                continue
            meta = load_metadata(entry.path)
            if not meta:
                continue

            # Название и автор — подстрочный поиск
            if q_title and q_title not in (meta.get('title') or '').lower():
                continue
            if q_author and q_author not in (meta.get('author') or '').lower():
                continue

            # Фэндомы
            book_fandoms_str = ' | '.join(
                f.lower() for f in (meta.get('fandom') or []))
            if pos_fandoms and not all(pf in book_fandoms_str for pf in pos_fandoms):
                continue
            if neg_fandoms and any(nf in book_fandoms_str for nf in neg_fandoms):
                continue

            # Пэйринги и персонажи
            book_pairings_str = ' | '.join(
                p.lower() for p in (meta.get('pairing') or []))
            if pos_pairings and not all(pp in book_pairings_str for pp in pos_pairings):
                continue
            if neg_pairings and any(np in book_pairings_str for np in neg_pairings):
                continue

            # Теги
            book_tags_list = [t.lower() for t in (meta.get('tags') or [])]
            book_tags_str  = ' | '.join(book_tags_list)
            if pos_tags and not all(pt in book_tags_str for pt in pos_tags):
                continue
            if neg_tags and any(nt in book_tags_str for nt in neg_tags):
                continue

            # Количество глав
            if ch_min is not None:
                cc = meta.get('chapters_count')
                try:
                    cc = int(cc) if cc is not None else 0
                except Exception:
                    cc = 0
                if not (ch_min <= cc <= (ch_max or 99999)):
                    continue

            matches.append(entry.name)

        root.after(0, lambda: _update_search_results(matches))

    Thread(target=run_search, daemon=True).start()


def _update_search_results(matches: list):
    books_listbox.delete(0, 'end')
    _library_folders.clear()
    folder = output_folder
    for name in sorted(matches, key=str.lower):
        full = os.path.join(folder, name)
        _library_folders[name] = full
        books_listbox.insert('end', name)
    books_count_label.config(text=f'Найдено: {len(matches)}')
    _set_meta_text(lambda: _append(
        f'Найдено {len(matches)} книг.\nВыберите книгу слева.\n', 'muted'))


def reset_search():
    title_search.delete()
    author_search.delete()
    chapters_entry.delete(0, 'end')
    tag_fandom.clear()
    tag_pairing.clear()
    tag_tags.clear()
    refresh_library()


# ── Лог ───────────────────────────────────────────────────────────────────────
log_frame = ttk.Frame(root)
log_frame.pack(fill='x', padx=8, pady=(0, 8))
ttk.Label(log_frame, text='Лог:', font=('Segoe UI', 8, 'bold'),
          foreground='#6c7086').pack(anchor='w')
log_area = scrolledtext.ScrolledText(
    log_frame, wrap='word', height=5,
    bg='#181825', fg='#a6adc8', font=('Consolas', 8),
    relief='flat', bd=0)
log_area.pack(fill='x')


def on_tab_change(event):
    tab = notebook.tab(notebook.select(), 'text')
    if '🗂' in tab:
        refresh_library()


notebook.bind('<<NotebookTabChanged>>', on_tab_change)

root.mainloop()
