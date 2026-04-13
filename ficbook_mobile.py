"""
FicBook Downloader — мобильная версия
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
    _index_cache = {'titles': [], 'authors': [], 'fandoms': [], 'tags': [], 'pairings': []}
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
  index = load_index()

  def add_unique(lst, value):
    if value and str(value).strip() and value not in lst:
      lst.append(value)

  add_unique(index['titles'], meta.get('title'))
  add_unique(index['authors'], meta.get('author'))
  for f in (meta.get('fandom') or []):
    add_unique(index['fandoms'], f)
  for t in (meta.get('tags') or []):
    add_unique(index['tags'], t)
  for p in (meta.get('pairing') or []):
    add_unique(index['pairings'], p)
  save_index(index)


def rank_suggestions(query: str, candidates: list) -> list:
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
    'fic_id':      str(fic_id),
    'title':       None,
    'author':      None,
    'author_url':    None,
    'universe':     None,
    'fandom':      [],
    'pairing':      [],
    'size_raw':     None,
    'pages':       None,
    'words':       None,
    'chapters_count':  None,
    'tags':       [],
    'description':    None,
    'notes':       None,
    'dedication':    None,
    'rating':      None,
    'direction':     None,
    'status':      None,
    'chapter_titles':  [],
    'chapter_urls':   [],
    'total_words':    None,
    'total_chars':    None,
    'size_kb':      None,
    'converted_formats': {},
  }

  try:
    h1 = (soup.find('h1', itemprop='name') or soup.find('h1', class_='heading'))
    if h1:
      meta['title'] = h1.get_text(strip=True)
  except Exception:
    pass

  try:
    author_tag = (soup.find('a', itemprop='author') or soup.find('a', class_='creator-username'))
    if author_tag:
      meta['author'] = author_tag.get_text(strip=True)
      meta['author_url'] = author_tag.get('href', '')
  except Exception:
    pass

  try:
    for div in soup.find_all('div', class_='mb-10'):
      strong = div.find('strong')
      if not strong:
        continue
      label = strong.get_text(strip=True)

      if 'Вселенная' in label:
        try:
          links = div.find_all('a')
          meta['universe'] = (', '.join(a.get_text(strip=True) for a in links) or None)
        except Exception:
          pass
      elif 'Фэндом' in label:
        try:
          meta['fandom'] = [a.get_text(strip=True) for a in div.find_all('a')]
        except Exception:
          pass
      elif 'Пэйринг' in label:
        try:
          meta['pairing'] = [a.get_text(strip=True) for a in div.find_all('a')]
        except Exception:
          pass
      elif 'Размер' in label:
        try:
          content_div = strong.find_next_sibling('div') or div.find('div')
          size_text = (content_div.get_text(separator=' ', strip=True) if content_div else '')
          size_text = size_text.replace('\xa0', ' ')
          meta['size_raw'] = size_text
          for pat, key in [(r'([\d\s]+)\s*страниц', 'pages'),
                   (r'([\d\s]+)\s*слов',  'words'),
                   (r'([\d\s]+)\s*част',  'chapters_count')]:
            m2 = re.search(pat, size_text)
            if m2:
              meta[key] = int(m2.group(1).replace(' ', ''))
        except Exception:
          pass
      elif 'Метки' in label:
        try:
          meta['tags'] = [a.get_text(strip=True) for a in div.find_all('a', class_='tag')]
        except Exception:
          pass
      elif 'Описание' in label:
        try:
          desc_div = (
            div.find('div', itemprop='description')
            or div.find('div', class_='urlize-links')
            or div.find('div', class_=re.compile('js-public-beta-description')))
          meta['description'] = (desc_div.get_text(strip=True) if desc_div else None)
        except Exception:
          pass
      elif 'Примечани' in label:
        try:
          notes_div = (
            div.find('div', class_='urlize-links')
            or div.find('div', class_=re.compile('js-public-beta-author-comment')))
          meta['notes'] = (notes_div.get_text(strip=True) if notes_div else None)
        except Exception:
          pass
      elif 'Посвящение' in label:
        try:
          ded_div = (
            div.find('div', class_='urlize-links')
            or div.find('div', class_=re.compile('js-public-beta-dedication')))
          meta['dedication'] = (ded_div.get_text(strip=True) if ded_div else None)
        except Exception:
          pass
  except Exception:
    pass

  try:
    badges_section = soup.find('section', class_='fanfic-badges')
    if badges_section:
      for badge in badges_section.find_all(class_='badge-with-icon'):
        text = badge.get_text(strip=True)
        if any(d in text for d in ['Гет', 'Слэш', 'Фемслэш', 'Джен', 'Смешанный', 'Другой']):
          meta['direction'] = text
        elif re.match(r'^(G|PG|PG-13|R|NC-17|NC-21)$', text):
          meta['rating'] = text
        elif any(s in text for s in ['Завершён', 'В процессе', 'Заморожен', 'Приостановлен']):
          meta['status'] = text
  except Exception:
    pass

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


def load_metadata(book_folder: str):
  path = os.path.join(book_folder, 'metadata.json')
  try:
    with open(path, 'r', encoding='utf-8') as f:
      data = json.load(f)
    data.setdefault('converted_formats', {})
    data.setdefault('fandom', [])
    data.setdefault('pairing', [])
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
  update_index(meta)


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
  folder_name = safe_title
  book_folder = os.path.join(output_folder, folder_name)
  if os.path.exists(book_folder):
    try:
      meta_path = os.path.join(book_folder, 'metadata.json')
      with open(meta_path, 'r', encoding='utf-8') as f:
        existing = json.load(f)
      if str(existing.get('fic_id')) == str(fic_id):
        return folder_name, book_folder
    except Exception:
      pass
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
        write_log(f"429 — слишком много запросов. Пауза 60 с (попытка {retries}/5)")
        time.sleep(60)
        continue
      if resp.status_code != 200:
        write_log(f"Ошибка {resp.status_code} при загрузке {url}")
        return

      soup = BeautifulSoup(resp.text, 'html.parser')

      if chapter_index == 0:
        meta = parse_metadata(soup, fic_id)
        title = meta.get('title') or f"book_{fic_id}"
        safe_title = sanitize_filename(title)
        folder_name, book_folder = _make_book_folder(safe_title, fic_id)
        os.makedirs(book_folder, exist_ok=True)
        passed_title = safe_title
        passed_meta = meta
        if book_folder_ref is not None:
          book_folder_ref[0] = book_folder
        else:
          book_folder_ref = [book_folder]
        save_metadata(meta, book_folder)
        write_log(f"[{fic_id}] «{title}»")

        chapter_links = soup.find_all('li', class_='part')
        if chapter_links:
          write_log(f"[{fic_id}] «{title}» — найдено {len(chapter_links)} глав")
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

        content_div = soup.find('div', id='content')
        if content_div:
          text = content_div.get_text()
          ch_title = (meta['chapter_titles'][0] if meta['chapter_titles'] else 'Глава 1')
          safe_ch = sanitize_filename(ch_title) if ch_title else '1'
          fname = os.path.join(book_folder, f"1_{safe_ch}.txt")
          save_text(text, fname)
          write_log(f" Глава сохранена: {fname}")
          update_file_stats(meta, book_folder)
        else:
          write_log(f"[{fic_id}] Не найден контент: {url}")
        return

      else:
        book_folder = book_folder_ref[0] if book_folder_ref else output_folder
        content_div = (soup.find('div', id='content') or soup.find('article', class_='article'))
        if content_div:
          text = content_div.get_text()
          meta = passed_meta or {}
          titles = meta.get('chapter_titles', [])
          if chapter_index - 1 < len(titles) and titles[chapter_index - 1]:
            ch_title = titles[chapter_index - 1]
          else:
            h2 = (soup.find('h2', class_=re.compile('part-title|chapter')) or soup.find('h2'))
            ch_title = (h2.get_text(strip=True) if h2 else f"Глава {chapter_index}")
          safe_ch = sanitize_filename(ch_title)
          fname = os.path.join(book_folder, f"{chapter_index}_{safe_ch}.txt")
          save_text(text, fname)
          write_log(f" [{chapter_index}] {ch_title}")
          time.sleep(_get_download_delay())
        else:
          write_log(f" Не найден контент для главы {chapter_index}: {url}")
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
  # В мобильной версии base берётся из path_entry (редактируемое поле)
  base = path_var.get().strip()
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
    num = int(m.group(1)) if m else 0
    name = m.group(2)   if m else fname
    try:
      with open(fpath, 'r', encoding='utf-8') as f:
        text = f.read()
      result.append((num, name, text))
    except Exception:
      pass
  return result


def convert_to_txt(meta: dict, book_folder: str):
  title = sanitize_filename(meta.get('title') or 'book')
  out_path = os.path.join(book_folder, f"{BOOK_FILE_PREFIX}_{title}.txt")
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
    return None, "Библиотека ebooklib не установлена.\nВыполните: pip install ebooklib"
  title = sanitize_filename(meta.get('title') or 'book')
  out_path = os.path.join(book_folder, f"{BOOK_FILE_PREFIX}_{title}.epub")
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
        f'<p>{p.strip()}</p>' for p in text.split('\n') if p.strip())
      ch.content = (f'<html><body><h2>Глава {num}: {name}</h2>'
             f'{paragraphs}</body></html>')
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
  out_path = os.path.join(book_folder, f"{BOOK_FILE_PREFIX}_{title}.fb2")
  chapters = get_chapter_texts(book_folder)
  if not chapters:
    return None, "Нет глав для конвертации"
  try:
    def esc(s):
      return (s.replace('&', '&amp;').replace('<', '&lt;')
           .replace('>', '&gt;').replace('"', '&quot;'))
    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append('<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" '
           'xmlns:l="http://www.w3.org/1999/xlink">')
    lines.append('<description><title-info>')
    lines.append(f'<book-title>{esc(meta.get("title") or "Без названия")}</book-title>')
    if meta.get('author'):
      parts = meta['author'].split()
      if len(parts) >= 2:
        lines.append(f'<author><first-name>{esc(parts[0])}</first-name>'
               f'<last-name>{esc(" ".join(parts[1:]))}</last-name></author>')
      else:
        lines.append(f'<author><nickname>{esc(meta["author"])}</nickname></author>')
    lines.append('<lang>ru</lang>')
    for fandom in (meta.get('fandom') or []):
      lines.append(f'<genre>{esc(fandom)}</genre>')
    lines.append('</title-info><document-info>')
    lines.append(f'<id>ficbook_{meta.get("fic_id", "unknown")}</id>')
    lines.append('</document-info></description><body>')
    for num, name, text in chapters:
      lines.append(f'<section><title><p>Глава {num}: {esc(name)}</p></title>')
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
    return None, "Библиотека reportlab не установлена.\nВыполните: pip install reportlab"
  title = sanitize_filename(meta.get('title') or 'book')
  out_path = os.path.join(book_folder, f"{BOOK_FILE_PREFIX}_{title}.pdf")
  chapters = get_chapter_texts(book_folder)
  if not chapters:
    return None, "Нет глав для конвертации"
  try:
    doc = SimpleDocTemplate(out_path, pagesize=A4,
                rightMargin=2*cm, leftMargin=2*cm,
                topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    title_style  = ParagraphStyle('Ttl', parent=styles['Title'],  fontSize=18, spaceAfter=12)
    chapter_style = ParagraphStyle('Ch', parent=styles['Heading1'], fontSize=14, spaceAfter=8, spaceBefore=16)
    body_style  = ParagraphStyle('Bd', parent=styles['Normal'],  fontSize=11, leading=14, spaceAfter=6)
    story = [Paragraph(meta.get('title') or 'Без названия', title_style)]
    if meta.get('author'):
      story.append(Paragraph(f"Автор: {meta['author']}", styles['Normal']))
    story.append(Spacer(1, 0.5*cm))
    for num, name, text in chapters:
      story.append(PageBreak())
      story.append(Paragraph(f"Глава {num}: {name}", chapter_style))
      for para in text.split('\n'):
        para = para.strip()
        if para:
          para_esc = para.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
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
  out_path = os.path.join(book_folder, f"{BOOK_FILE_PREFIX}_{title}.mobi")
  try:
    result = subprocess.run(['ebook-convert', epub_path, out_path],
                capture_output=True, text=True, timeout=120)
    if result.returncode == 0:
      return out_path, None
    return None, f"Ошибка ebook-convert: {result.stderr[:300]}"
  except FileNotFoundError:
    return None, ("Calibre не установлен.\n"
           "Установите Calibre и убедитесь, что ebook-convert доступен в PATH.")
  except Exception as e:
    return None, str(e)


CONVERTERS = {
  'TXT': ('txt', convert_to_txt),
  'EPUB': ('epub', convert_to_epub),
  'FB2': ('fb2', convert_to_fb2),
  'PDF': ('pdf', convert_to_pdf),
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
  write_log(f" OK {fmt_label} сохранён: {out_path}")
  messagebox.showinfo('Готово', f'Файл сохранён:\n{out_path}')
  root.after(0, lambda: display_metadata(load_metadata(book_folder)))


# ─── Открытие файла (с Android-fallback) ─────────────────────────────────────

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
      # Linux desktop или Android
      try:
        subprocess.Popen(['xdg-open', path])
      except FileNotFoundError:
        # Android: intent через am
        try:
          subprocess.Popen([
            'am', 'start', '-a', 'android.intent.action.VIEW',
            '-d', f'file://{path}', '-t', '*/*'])
        except Exception:
          messagebox.showinfo('Путь к файлу',
                    f'Файл сохранён. Откройте вручную:\n\n{path}')
  except Exception as e:
    messagebox.showinfo('Путь к файлу', f'Откройте файл вручную:\n\n{path}')


def open_in_reader(book_folder: str):
  meta = load_metadata(book_folder)
  if not meta:
    messagebox.showerror('Ошибка', 'metadata.json не найден.')
    return
  converted = meta.get('converted_formats') or {}
  if not converted:
    messagebox.showwarning('Книга не конвертирована',
                'Сначала конвертируйте книгу в один из форматов:\n'
                'TXT, EPUB, FB2, PDF или MOBI.')
    return
  if len(converted) == 1:
    ext, fname = next(iter(converted.items()))
    _open_file(os.path.join(book_folder, fname))
  else:
    _show_format_chooser(converted, book_folder)


def _show_format_chooser(converted: dict, book_folder: str):
  dlg = tk.Toplevel(root)
  dlg.title('Выберите формат')
  dlg.configure(bg=BG)
  dlg.grab_set()
  ttk.Label(dlg, text='Выберите формат для открытия:',
       font=FONT_MAIN, wraplength=max(260, int(SCREEN_W * 0.9))).pack(padx=20, pady=(16, 8))
  for ext, fname in converted.items():
    fpath = os.path.join(book_folder, fname)
    ttk.Button(dlg, text=ext.upper(),
          command=lambda p=fpath, d=dlg: (d.destroy(), _open_file(p))
          ).pack(padx=20, pady=6, fill='x', ipady=6)
  ttk.Button(dlg, text='Отмена', command=dlg.destroy).pack(padx=20, pady=(4, 16), fill='x', ipady=6)


# ─── Диалоги удаления ─────────────────────────────────────────────────────────

def delete_selected_book(book_folder: str):
  meta = load_metadata(book_folder)
  book_title = (meta.get('title') or os.path.basename(book_folder)) if meta else os.path.basename(book_folder)

  dlg = tk.Toplevel(root)
  dlg.title('Удалить книгу')
  dlg.configure(bg=BG)
  dlg.grab_set()

  ttk.Label(dlg,
       text=f'Удалить книгу?\n\n«{book_title}»\n\n'
          'Папка и все файлы будут безвозвратно удалены.',
       font=FONT_MAIN, foreground='#f38ba8', justify='center', wraplength=max(260, int(SCREEN_W * 0.9))).pack(padx=24, pady=(18, 8))

  btn_row = ttk.Frame(dlg)
  btn_row.pack(fill='x', padx=20, pady=(8, 18))

  def on_yes():
    dlg.destroy()
    try:
      shutil.rmtree(book_folder)
      write_log(f"Книга удалена: {book_title}")
    except Exception as e:
      messagebox.showwarning('Ошибка', f'Не удалось удалить папку:\n{e}')
    root.after(0, refresh_library)

  ttk.Button(btn_row, text='Да, удалить',
        style='Danger.TButton', command=on_yes).pack(fill='x', ipady=6, pady=4)
  ttk.Button(btn_row, text='Отмена', command=dlg.destroy).pack(fill='x', ipady=6, pady=4)


def delete_converted_files(book_folder: str):
  meta = load_metadata(book_folder)
  if not meta:
    messagebox.showerror('Ошибка', 'metadata.json не найден.')
    return
  converted = meta.get('converted_formats') or {}
  if not converted:
    messagebox.showinfo('Нет файлов', 'Нет конвертированных файлов для удаления.')
    return

  dlg = tk.Toplevel(root)
  dlg.title('Удалить конвертированные файлы')
  dlg.configure(bg=BG)
  dlg.grab_set()

  ttk.Label(dlg, text='Выберите форматы для удаления:', font=FONT_MAIN).pack(padx=20, pady=(16, 8))

  checks = {}
  for ext, fname in converted.items():
    var = tk.BooleanVar()
    ttk.Checkbutton(dlg, text=f"{ext.upper()} — {fname}", variable=var).pack(padx=20, pady=4, anchor='w')
    checks[ext] = (var, fname)

  btn_row = ttk.Frame(dlg)
  btn_row.pack(fill='x', padx=20, pady=(12, 16))

  def on_delete():
    to_del = [(e, fn) for e, (v, fn) in checks.items() if v.get()]
    if not to_del:
      messagebox.showinfo('Ничего не выбрано', 'Выберите хотя бы один формат.', parent=dlg)
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

  ttk.Button(btn_row, text='Удалить', style='Warn.TButton',
        command=on_delete).pack(fill='x', ipady=6, pady=4)
  ttk.Button(btn_row, text='Отмена', command=dlg.destroy).pack(fill='x', ipady=6, pady=4)


def delete_separate_chapters(book_folder: str):
  dlg = tk.Toplevel(root)
  dlg.title('Удалить главы')
  dlg.configure(bg=BG)
  dlg.grab_set()

  ttk.Label(dlg,
       text='Внимание!\n\n'
          'После удаления отдельных файлов глав\n'
          'книгу нельзя будет конвертировать.\n'
          'metadata.json и конвертированные файлы\n'
          'останутся нетронутыми.',
       font=FONT_MAIN, foreground='#fab387', justify='center', wraplength=max(260, int(SCREEN_W * 0.9))).pack(padx=24, pady=(18, 8))

  btn_row = ttk.Frame(dlg)
  btn_row.pack(fill='x', padx=20, pady=(8, 18))

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

  ttk.Button(btn_row, text='Удалить главы', style='Warn.TButton',
        command=on_confirm).pack(fill='x', ipady=6, pady=4)
  ttk.Button(btn_row, text='Отмена', command=dlg.destroy).pack(fill='x', ipady=6, pady=4)


# ─── GUI — мобильная версия ───────────────────────────────────────────────────

# Цвета (Catppuccin Mocha)
BG    = '#1e1e2e'
BG2    = '#313244'
BG3    = '#45475a'
FG    = '#cdd6f4'
FG_MUTED = '#6c7086'
FG_DIM  = '#a6adc8'
BLUE   = '#89b4fa'
GREEN   = '#a6e3a1'
RED    = '#f38ba8'
ORANGE  = '#fab387'

# Шрифты (TkDefaultFont + TkFixedFont — работают на любой платформе)
root = tk.Tk()
root.title("FicBook Downloader")
root.geometry(f"{max(360, min(760, int(root.winfo_screenwidth() * 0.95)))}x{max(640, min(1100, int(root.winfo_screenheight() * 0.95)))}")
root.configure(bg=BG)
SCREEN_W = root.winfo_screenwidth()
SCREEN_H = root.winfo_screenheight()
# Масштаб под экран: на маленьких дисплеях чуть уменьшаем, на больших оставляем
SCALE = max(0.75, min(1.0, min(SCREEN_W, SCREEN_H) / 520))

def _fs(size: int) -> int:
    return max(8, int(round(size * SCALE)))

FONT_MAIN = ('TkDefaultFont', _fs(11))
FONT_BOLD = ('TkDefaultFont', _fs(11), 'bold')
FONT_SMALL = ('TkDefaultFont', _fs(10))
FONT_TINY = ('TkDefaultFont', _fs(9))
FONT_MONO = ('TkFixedFont', _fs(10))

style = ttk.Style()
style.theme_use('clam')
style.configure('TNotebook',   background=BG, borderwidth=0)
style.configure('TNotebook.Tab', background=BG2, foreground=FG,
        padding=[6, 4], font=FONT_TINY)
style.map('TNotebook.Tab',
     background=[('selected', BLUE)],
     foreground=[('selected', BG)])
style.configure('TFrame',    background=BG)
style.configure('TLabel',    background=BG, foreground=FG, font=FONT_MAIN)
style.configure('TEntry',    fieldbackground=BG2, foreground=FG, font=FONT_MAIN)
style.configure('TCheckbutton', background=BG, foreground=FG, font=FONT_MAIN)
style.configure('TButton',    background=BLUE, foreground=BG,
        font=FONT_BOLD, padding=[10, 6])
style.map('TButton',       background=[('active', '#b4befe')])
style.configure('Danger.TButton', background=RED, foreground=BG,
        font=FONT_BOLD, padding=[10, 6])
style.map('Danger.TButton',   background=[('active', '#eba0ac')])
style.configure('Warn.TButton',  background=ORANGE, foreground=BG,
        font=FONT_BOLD, padding=[10, 6])
style.map('Warn.TButton',    background=[('active', '#f9e2af')])
style.configure('Green.TButton', background=GREEN, foreground=BG,
        font=FONT_BOLD, padding=[10, 6])
style.map('Green.TButton',    background=[('active', '#94e2d5')])

# Верхняя область: высота меняется по вкладке
tabs_container = ttk.Frame(root, height=max(220, int(SCREEN_H * 0.48)))
tabs_container.pack(fill='x', padx=2, pady=(2, 0))
tabs_container.pack_propagate(False)

notebook = ttk.Notebook(tabs_container)
notebook.pack(expand=True, fill='both')

# ── Вкладка: Настройки ────────────────────────────────────────────────────────
tab_settings = ttk.Frame(notebook)
notebook.add(tab_settings, text='Настройки')

settings_canvas = tk.Canvas(tab_settings, bg=BG, highlightthickness=0)
settings_scroll = ttk.Scrollbar(tab_settings, orient='vertical', command=settings_canvas.yview)
settings_inner = ttk.Frame(settings_canvas)

settings_inner.bind('<Configure>',
  lambda e: settings_canvas.configure(scrollregion=settings_canvas.bbox('all')))
settings_canvas.create_window((0, 0), window=settings_inner, anchor='nw')
settings_canvas.configure(yscrollcommand=settings_scroll.set)
settings_canvas.pack(side='left', fill='both', expand=True)
settings_scroll.pack(side='right', fill='y')

# Путь сохранения
ttk.Label(settings_inner, text='Путь сохранения:', font=FONT_BOLD).pack(
  anchor='w', padx=12, pady=(12, 2))

saved_path = _settings.get('base_path', os.getcwd())
path_var = tk.StringVar(value=saved_path)
path_frame = ttk.Frame(settings_inner)
path_frame.pack(fill='x', padx=12, pady=2)

path_entry = ttk.Entry(path_frame, textvariable=path_var, font=FONT_SMALL)
path_entry.pack(side='left', fill='x', expand=True, ipady=4)


def select_path():
  p = filedialog.askdirectory(initialdir=path_var.get(), title='Выберите путь сохранения')
  if p:
    path_var.set(p)
    _settings['base_path'] = p
    save_settings()


ttk.Button(path_frame, text='Обзор', width=7, command=select_path).pack(side='left', padx=(4, 0))

def _on_path_change(*_):
  _settings['base_path'] = path_var.get().strip()
  save_settings()

path_var.trace_add('write', _on_path_change)

# Название подпапки
ttk.Label(settings_inner, text='Название подпапки:', font=FONT_BOLD).pack(
  anchor='w', padx=12, pady=(10, 2))
folder_name_entry = ttk.Entry(settings_inner, font=FONT_MAIN)
folder_name_entry.insert(0, _settings.get('folder_name', 'books'))
folder_name_entry.pack(fill='x', padx=12, pady=2, ipady=4)


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
        command=_on_create_folder_change).pack(
  anchor='w', padx=12, pady=(4, 8))

# Задержка
ttk.Label(settings_inner, text='Задержка между главами (сек):', font=FONT_BOLD).pack(
  anchor='w', padx=12, pady=(8, 2))

delay_frame = ttk.Frame(settings_inner)
delay_frame.pack(fill='x', padx=12, pady=2)
delay_entry = ttk.Entry(delay_frame, width=8, font=FONT_MAIN)
delay_entry.insert(0, str(_settings.get('download_delay', 0.4)))
delay_entry.pack(side='left', ipady=4)
ttk.Label(delay_frame, text='(напр. 0.4, 1.0, 2.0)',
     foreground=FG_MUTED, font=FONT_TINY).pack(side='left', padx=8)


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
delay_entry.bind('<Return>',  _on_delay_change)

# Автодополнение
autocomplete_var = tk.BooleanVar(value=bool(_settings.get('autocomplete_enabled', True)))


def _on_autocomplete_change():
  _settings['autocomplete_enabled'] = autocomplete_var.get()
  save_settings()


ttk.Checkbutton(settings_inner, text='Автодополнение при поиске',
        variable=autocomplete_var,
        command=_on_autocomplete_change).pack(
  anchor='w', padx=12, pady=(8, 4))

ttk.Label(settings_inner,
     text='Зависимости:\n'
        'EPUB: pip install ebooklib\n'
        'PDF: pip install reportlab\n'
        'MOBI: установите Calibre',
     font=FONT_TINY, foreground=FG_MUTED, justify='left', wraplength=max(240, int(SCREEN_W * 0.9))).pack(
  anchor='w', padx=12, pady=(12, 12))

# ── Вкладка: Коллекция ────────────────────────────────────────────────────────
tab_collection = ttk.Frame(notebook)
notebook.add(tab_collection, text='Коллекция')

coll_inner = ttk.Frame(tab_collection)
coll_inner.pack(fill='both', expand=True, padx=6, pady=4)

ttk.Label(coll_inner, text='Скачать коллекцию', font=FONT_SMALL).pack(anchor='w', pady=(0, 4))
ttk.Label(coll_inner, text='ID коллекции:', font=FONT_SMALL).pack(anchor='w')
collection_id_entry = ttk.Entry(coll_inner, font=FONT_SMALL)
collection_id_entry.pack(fill='x', pady=2, ipady=3)


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


ttk.Button(coll_inner, text='Начать скачивание',
      command=start_scraping).pack(fill='x', pady=3, ipady=max(2, int(3*SCALE)))
ttk.Button(coll_inner, text='Остановить',
      style='Warn.TButton',
      command=lambda: stop_event.set()).pack(fill='x', pady=1, ipady=max(2, int(3*SCALE)))

# ── Вкладка: Книги по ID ──────────────────────────────────────────────────────
tab_books = ttk.Frame(notebook)
notebook.add(tab_books, text='Книги')

books_inner = ttk.Frame(tab_books)
books_inner.pack(fill='both', expand=True, padx=6, pady=4)

ttk.Label(books_inner, text='Книги по ID', font=FONT_SMALL).pack(anchor='w', pady=(0, 4))
ttk.Label(books_inner, text='ID книг (через запятую):', font=FONT_SMALL).pack(anchor='w')
book_ids_entry = ttk.Entry(books_inner, font=FONT_SMALL)
book_ids_entry.pack(fill='x', pady=2, ipady=3)


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


ttk.Button(books_inner, text='Скачать',
      command=download_specific_books).pack(fill='x', pady=3, ipady=max(2, int(3*SCALE)))
ttk.Button(books_inner, text='Остановить',
      style='Warn.TButton',
      command=lambda: stop_event.set()).pack(fill='x', pady=1, ipady=max(2, int(3*SCALE)))

# ── Вкладка: Интервал ─────────────────────────────────────────────────────────
tab_interval = ttk.Frame(notebook)
notebook.add(tab_interval, text='Интервал')

intv_inner = ttk.Frame(tab_interval)
intv_inner.pack(fill='both', expand=True, padx=6, pady=4)

ttk.Label(intv_inner, text='Скачать интервал ID', font=FONT_SMALL).pack(anchor='w', pady=(0, 4))
ttk.Label(intv_inner, text='Начальный ID:', font=FONT_SMALL).pack(anchor='w')
start_id_entry = ttk.Entry(intv_inner, font=FONT_SMALL)
start_id_entry.pack(fill='x', pady=2, ipady=3)
ttk.Label(intv_inner, text='Конечный ID:', font=FONT_SMALL).pack(anchor='w', pady=(4, 0))
end_id_entry = ttk.Entry(intv_inner, font=FONT_SMALL)
end_id_entry.pack(fill='x', pady=2, ipady=3)


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


ttk.Button(intv_inner, text='Скачать',
      command=download_interval_books).pack(fill='x', pady=3, ipady=max(2, int(3*SCALE)))
ttk.Button(intv_inner, text='Остановить',
      style='Warn.TButton',
      command=lambda: stop_event.set()).pack(fill='x', pady=1, ipady=max(2, int(3*SCALE)))


# ─── Виджет автодополнения (inline-listbox, без Toplevel) ─────────────────────

class AutocompleteEntry:
  """
  Поле ввода с inline-раскрывающимся списком подсказок.
  Не использует Toplevel — совместимо с мобильными экранами.
  """

  def __init__(self, parent, index_key: str):
    self.index_key = index_key
    self.frame = ttk.Frame(parent)

    self.var = tk.StringVar()
    self.entry = ttk.Entry(self.frame, textvariable=self.var, font=FONT_MAIN)
    self.entry.pack(fill='x', ipady=4)
    self.var.trace_add('write', self._on_type)
    self.entry.bind('<FocusOut>', lambda e: self.frame.after(250, self._hide))

    self._sugg_frame = tk.Frame(self.frame, bg=BG3, bd=1, relief='solid')
    self._listbox = tk.Listbox(
      self._sugg_frame,
      bg=BG2, fg=FG,
      selectbackground=BLUE, selectforeground=BG,
      font=FONT_MAIN, relief='flat', bd=0,
      highlightthickness=0, activestyle='none',
      height=5)
    self._listbox.pack(fill='x', padx=1, pady=1)
    self._listbox.bind('<<ListboxSelect>>', self._on_select)
    self._visible = False

  def _on_type(self, *_):
    if not autocomplete_var.get():
      self._hide()
      return
    text = self.var.get().strip()
    if not text:
      self._hide()
      return
    suggestions = rank_suggestions(text, load_index().get(self.index_key, []))
    if suggestions:
      self._show(suggestions)
    else:
      self._hide()

  def _show(self, items: list):
    self._listbox.delete(0, 'end')
    for item in items:
      self._listbox.insert('end', item)
    if not self._visible:
      self._sugg_frame.pack(fill='x')
      self._visible = True

  def _hide(self, *_):
    if self._visible:
      self._sugg_frame.pack_forget()
      self._visible = False

  def _on_select(self, *_):
    sel = self._listbox.curselection()
    if sel:
      self.var.set(self._listbox.get(sel[0]))
    self._hide()

  def get(self) -> str:
    return self.var.get()

  def set(self, v: str):
    self.var.set(v)

  def delete(self, *_):
    self.var.set('')

  def pack(self, **kw):
    self.frame.pack(**kw)

  def grid(self, **kw):
    self.frame.grid(**kw)


# ─── Виджет тегов (мобильный, крупные кнопки) ────────────────────────────────

class TagField:
  """
  Поле для выбора тегов с разделением на позитивные (зелёные) и негативные (красные).
  Адаптировано для touch-управления: увеличенные кнопки, вертикальная компоновка.
  """

  def __init__(self, parent, label: str, index_key: str):
    self.index_key = index_key
    self.positive: list = []
    self.negative: list = []

    self.frame = ttk.Frame(parent)

    ttk.Label(self.frame, text=label, font=FONT_BOLD,
         foreground=FG_DIM, wraplength=max(220, int(SCREEN_W * 0.88))).pack(anchor='w', pady=(8, 2))

    input_row = ttk.Frame(self.frame)
    input_row.pack(fill='x')

    self.var = tk.StringVar()
    self.entry = ttk.Entry(input_row, textvariable=self.var, font=FONT_MAIN)
    self.entry.pack(side='left', fill='x', expand=True, ipady=5)
    self.entry.bind('<Return>', lambda e: self.add_positive())

    ttk.Button(input_row, text='+', width=3,
          command=self.add_positive).pack(side='left', padx=(4, 2))
    ttk.Button(input_row, text='−', width=3,
          style='Warn.TButton',
          command=self.add_negative).pack(side='left', padx=(0, 2))

    # Inline autocomplete
    self._sugg_frame = tk.Frame(self.frame, bg=BG3, bd=1, relief='solid')
    self._listbox_ac = tk.Listbox(
      self._sugg_frame, bg=BG2, fg=FG,
      selectbackground=BLUE, selectforeground=BG,
      font=FONT_MAIN, relief='flat', bd=0,
      highlightthickness=0, activestyle='none', height=4)
    self._listbox_ac.pack(fill='x', padx=1, pady=1)
    self._listbox_ac.bind('<<ListboxSelect>>', self._on_ac_select)
    self._ac_visible = False

    self.var.trace_add('write', self._on_type)
    self.entry.bind('<FocusOut>', lambda e: self.frame.after(250, self._hide_ac))

    self._chips_outer = ttk.Frame(self.frame)
    self._chips_outer.pack(fill='x', pady=(2, 0))

  # ── Автодополнение ─────────────────────────────────────────────────────

  def _on_type(self, *_):
    if not autocomplete_var.get():
      self._hide_ac()
      return
    text = self.var.get().strip()
    if not text:
      self._hide_ac()
      return
    suggestions = rank_suggestions(text, load_index().get(self.index_key, []))
    if suggestions:
      self._listbox_ac.delete(0, 'end')
      for item in suggestions:
        self._listbox_ac.insert('end', item)
      if not self._ac_visible:
        self._sugg_frame.pack(fill='x', pady=(2, 0))
        self._ac_visible = True
    else:
      self._hide_ac()

  def _hide_ac(self, *_):
    if self._ac_visible:
      self._sugg_frame.pack_forget()
      self._ac_visible = False

  def _on_ac_select(self, *_):
    sel = self._listbox_ac.curselection()
    if sel:
      self.var.set(self._listbox_ac.get(sel[0]))
    self._hide_ac()

  # ── Управление тегами ─────────────────────────────────────────────────

  def add_positive(self):
    val = self.var.get().strip()
    if val and val not in self.positive and val not in self.negative:
      self.positive.append(val)
      self.var.set('')
      self._hide_ac()
      self._refresh_chips()

  def add_negative(self):
    val = self.var.get().strip()
    if val and val not in self.negative and val not in self.positive:
      self.negative.append(val)
      self.var.set('')
      self._hide_ac()
      self._refresh_chips()

  def _refresh_chips(self):
    for w in self._chips_outer.winfo_children():
      w.destroy()
    for tag in self.positive:
      self._make_chip(tag, '#1a3a28', GREEN, True)
    for tag in self.negative:
      self._make_chip(tag, '#3a1a1a', RED, False)

  def _make_chip(self, tag: str, bg: str, fg: str, positive: bool):
    chip = tk.Frame(self._chips_outer, bg=bg, padx=6, pady=3)
    chip.pack(anchor='w', pady=1, fill='x')
    lbl_text = ('+ ' if positive else '- ') + tag
    tk.Label(chip, text=lbl_text, bg=bg, fg=fg,
         font=FONT_SMALL).pack(side='left', fill='x', expand=True)
    tk.Button(chip, text='×', bg=bg, fg=fg,
         font=FONT_BOLD, relief='flat', bd=0,
         activebackground=bg, activeforeground=FG,
         command=lambda t=tag, p=positive: self._remove(t, p)
         ).pack(side='right', padx=(4, 0))

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
    self._hide_ac()
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
notebook.add(tab_library, text='Библиотека')

# Тулбар библиотеки
lib_toolbar = ttk.Frame(tab_library)
lib_toolbar.pack(fill='x', padx=6, pady=(6, 2))

books_count_label = ttk.Label(lib_toolbar, text='', font=FONT_SMALL, foreground=FG_MUTED)
books_count_label.pack(side='right', padx=4)

ttk.Button(lib_toolbar, text='Обновить',
      command=lambda: refresh_library()).pack(side='right', padx=4)
ttk.Button(lib_toolbar, text='Поиск',
      command=lambda: open_search_dialog()).pack(side='left', padx=4)

# Основной PanedWindow (вертикальный): список вверху, детали внизу
lib_paned = ttk.PanedWindow(tab_library, orient='vertical')
lib_paned.pack(fill='both', expand=True, padx=6, pady=4)

# ── Левая (верхняя) панель — список книг
list_outer = ttk.Frame(lib_paned)
lib_paned.add(list_outer, weight=1)

list_wrap = ttk.Frame(list_outer)
list_wrap.pack(fill='both', expand=True)

list_frame = ttk.Frame(list_wrap)
list_frame.pack(fill='both', expand=True)

books_listbox = tk.Listbox(
  list_frame, selectmode='single',
  bg=BG2, fg=FG,
  selectbackground=BLUE, selectforeground=BG,
  font=FONT_SMALL, relief='flat', bd=0,
  highlightthickness=0, exportselection=False)
books_listbox.pack(side='left', fill='both', expand=True)

list_scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=books_listbox.yview)
list_scrollbar.pack(side='right', fill='y')

h_scrollbar = ttk.Scrollbar(list_wrap, orient='horizontal', command=books_listbox.xview)
h_scrollbar.pack(side='bottom', fill='x')
books_listbox.config(yscrollcommand=list_scrollbar.set, xscrollcommand=h_scrollbar.set)

# ── Правая (нижняя) панель — метаданные + действия
right_outer = ttk.Frame(lib_paned)
lib_paned.add(right_outer, weight=2)

# Метаданные
meta_text = scrolledtext.ScrolledText(
  right_outer, wrap='word',
  bg=BG2, fg=FG, font=FONT_SMALL,
  relief='flat', bd=0, padx=8, pady=8,
  state='disabled', height=max(18, int(18 * SCALE)))
meta_text.pack(fill='both', expand=True)

for tag, color, font_extra in [
  ('header',  BLUE,   ('TkDefaultFont', 13, 'bold')),
  ('key',    GREEN,  ('TkDefaultFont', 12, 'bold')),
  ('value',   FG,    ('TkDefaultFont', 12)),
  ('muted',   FG_MUTED, ('TkDefaultFont', 11)),
  ('tag_item', RED,   ('TkDefaultFont', 12)),
  ('converted', GREEN,  ('TkDefaultFont', 12, 'bold')),
]:
  meta_text.tag_configure(tag, foreground=color, font=font_extra)

# Кнопки действий
action_frame = ttk.Frame(right_outer)
action_frame.pack(fill='x', padx=2, pady=(2, 4), before=meta_text)

# Конвертация: 5 кнопок в одну строку
conv_frame = ttk.Frame(action_frame)
conv_frame.pack(fill='x', pady=2)
for fmt_label in ['TXT', 'EPUB', 'FB2', 'PDF', 'MOBI']:
  ttk.Button(conv_frame, text=fmt_label, width=6,
        command=lambda f=fmt_label: _on_convert(f)).pack(
    side='left', expand=True, fill='x', padx=1)

# Открыть + удалить конвертированные
row2 = ttk.Frame(action_frame)
row2.pack(fill='x', pady=2)
ttk.Button(row2, text='Читалка',
      style='Green.TButton',
      command=lambda: _on_open_reader()).pack(side='left', expand=True, fill='x', padx=(0, 2))
ttk.Button(row2, text='Удал. конв.',
      style='Warn.TButton',
      command=lambda: _on_delete_converted()).pack(side='left', expand=True, fill='x', padx=(2, 0))

# Удалить главы + удалить книгу
row3 = ttk.Frame(action_frame)
row3.pack(fill='x', pady=2)
ttk.Button(row3, text='Удал. главы',
      style='Warn.TButton',
      command=lambda: _on_delete_chapters()).pack(side='left', expand=True, fill='x', padx=(0, 2))
ttk.Button(row3, text='Удалить книгу',
      style='Danger.TButton',
      command=lambda: _on_delete_book()).pack(side='left', expand=True, fill='x', padx=(2, 0))

# ─── Хранилище: имя_папки → полный путь ──────────────────────────────────────
_library_folders: dict = {}


def _get_selected_folder():
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


# ─── Отображение метаданных ───────────────────────────────────────────────────

def _set_meta_text(content_fn):
  meta_text.config(state='normal')
  meta_text.delete('1.0', 'end')
  content_fn()
  meta_text.config(state='disabled')


def _append(text: str, tag: str = 'value'):
  meta_text.insert('end', text, tag)


def display_metadata(meta):
  if not meta:
    _set_meta_text(lambda: _append('Метаданные не найдены.\n', 'muted'))
    return

  def build():
    _append(f" {meta.get('title') or '—'}\n", 'header')
    _append('\n')

    rows = [
      ('ID',          meta.get('fic_id')),
      ('Автор',         meta.get('author')),
      ('Статус',        meta.get('status')),
      ('Рейтинг',        meta.get('rating')),
      ('Направление',      meta.get('direction')),
      ('Вселенная',       meta.get('universe')),
      ('Фэндом',        ', '.join(meta.get('fandom') or []) or None),
      ('Пэйринги и персонажи', ', '.join(meta.get('pairing') or []) or None),
      ('Размер',        meta.get('size_raw')),
      ('Страниц',        meta.get('pages')),
      ('Слов (сайт)',      meta.get('words')),
      ('Глав',         meta.get('chapters_count')),
      ('Слов (файлы)',     meta.get('total_words')),
      ('Символов',       meta.get('total_chars')),
      ('Размер папки',     (f"{meta.get('size_kb')} КБ"
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
      _append(' ' + ' • '.join(tags) + '\n', 'tag_item')

    chapters = meta.get('chapter_titles') or []
    if chapters:
      _append(f'\nГлавы ({len(chapters)}):\n', 'key')
      for i, ch in enumerate(chapters, 1):
        _append(f" {i}. {ch or '—'}\n", 'muted')

    for label, field in [('Описание', 'description'),
               ('Примечания', 'notes'),
               ('Посвящение', 'dedication')]:
      v = meta.get(field)
      if v:
        _append(f'\n{label}:\n', 'key')
        snippet = v if len(v) <= 400 else v[:400] + '…'
        _append(f" {snippet}\n", 'muted')

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


# ─── Диалог поиска (persistent Toplevel) ─────────────────────────────────────

_search_dlg = None
title_search = None
author_search = None
chapters_entry = None
tag_fandom = None
tag_pairing = None
tag_tags  = None


def open_search_dialog():
  global _search_dlg, title_search, author_search, chapters_entry
  global tag_fandom, tag_pairing, tag_tags

  # Если диалог уже открыт — просто поднять
  if _search_dlg and _search_dlg.winfo_exists():
    _search_dlg.lift()
    _search_dlg.deiconify()
    return

  _search_dlg = tk.Toplevel(root)
  _search_dlg.title('Поиск по библиотеке')
  _search_dlg.configure(bg=BG)
  _search_dlg.geometry(f'{SCREEN_W}x{SCREEN_H}+0+0')

  # Scrollable area для полей поиска
  canvas = tk.Canvas(_search_dlg, bg=BG, highlightthickness=0)
  vsb = ttk.Scrollbar(_search_dlg, orient='vertical', command=canvas.yview)
  inner = ttk.Frame(canvas)
  inner.bind('<Configure>',
        lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
  canvas.create_window((0, 0), window=inner, anchor='nw')
  canvas.configure(yscrollcommand=vsb.set)
  canvas.pack(side='left', fill='both', expand=True)
  vsb.pack(side='right', fill='y')

  ttk.Label(inner, text=' Поиск', font=FONT_BOLD).pack(
    anchor='w', padx=12, pady=(12, 8))

  # Название
  ttk.Label(inner, text='Название:', font=FONT_BOLD).pack(anchor='w', padx=12)
  title_search = AutocompleteEntry(inner, 'titles')
  title_search.pack(fill='x', padx=12, pady=(2, 8))

  # Автор
  ttk.Label(inner, text='Автор:', font=FONT_BOLD).pack(anchor='w', padx=12)
  author_search = AutocompleteEntry(inner, 'authors')
  author_search.pack(fill='x', padx=12, pady=(2, 8))

  # Количество глав
  ttk.Label(inner, text='Глав (напр. 5 или 5–20):', font=FONT_BOLD).pack(anchor='w', padx=12)
  chapters_entry = ttk.Entry(inner, font=FONT_MAIN)
  chapters_entry.pack(fill='x', padx=12, pady=(2, 8), ipady=5)

  ttk.Separator(inner, orient='horizontal').pack(fill='x', padx=12, pady=6)

  # Тег-поля
  tag_fandom = TagField(inner, 'Фэндомы',        'fandoms')
  tag_pairing = TagField(inner, 'Пэйринги и персонажи', 'pairings')
  tag_tags  = TagField(inner, 'Теги',          'tags')

  tag_fandom.pack( fill='x', padx=12, pady=(0, 4))
  tag_pairing.pack(fill='x', padx=12, pady=(0, 4))
  tag_tags.pack(  fill='x', padx=12, pady=(0, 8))

  ttk.Separator(inner, orient='horizontal').pack(fill='x', padx=12, pady=6)

  # Кнопки
  btn_frame = ttk.Frame(inner)
  btn_frame.pack(fill='x', padx=12, pady=(4, 16))

  def do_search():
    search_books()
    _search_dlg.withdraw()

  def do_reset():
    reset_search()

  ttk.Button(btn_frame, text=' Найти',
        command=do_search).pack(fill='x', pady=4, ipady=6)
  ttk.Button(btn_frame, text='↺ Сбросить',
        command=do_reset).pack(fill='x', pady=4, ipady=6)
  ttk.Button(btn_frame, text='Закрыть',
        command=_search_dlg.withdraw).pack(fill='x', pady=4, ipady=6)


# ─── Поиск и обновление библиотеки ───────────────────────────────────────────

def _resolve_output_folder() -> bool:
  global output_folder

  dlg = tk.Toplevel(root)
  dlg.title('Папка не найдена')
  dlg.configure(bg=BG)
  dlg.grab_set()
  result = [False]

  ttk.Label(dlg,
       text=f'Папка библиотеки не найдена:\n{output_folder}\n\n'
          'Введите путь вручную или выберите папку:',
       font=FONT_MAIN, justify='left').pack(padx=16, pady=(16, 8))

  p_var = tk.StringVar(value=output_folder)
  p_entry = ttk.Entry(dlg, textvariable=p_var, font=FONT_MAIN)
  p_entry.pack(fill='x', padx=16, pady=4, ipady=5)

  def browse():
    p = filedialog.askdirectory(title='Выберите папку библиотеки')
    if p:
      p_var.set(p)

  def confirm():
    p = p_var.get().strip()
    if os.path.isdir(p):
      global output_folder
      output_folder = p
      _settings['output_folder'] = p
      save_settings()
      result[0] = True
      dlg.destroy()
    else:
      messagebox.showerror('Ошибка', f'Папка не существует:\n{p}', parent=dlg)

  ttk.Button(dlg, text='Обзор…',   command=browse).pack(fill='x', padx=16, pady=4, ipady=6)
  ttk.Button(dlg, text='Подтвердить', command=confirm).pack(fill='x', padx=16, pady=4, ipady=6)
  ttk.Button(dlg, text='Отмена',     command=dlg.destroy).pack(fill='x', padx=16, pady=(4, 16), ipady=6)

  dlg.wait_window()
  return result[0]


def refresh_library():
  global output_folder
  _library_folders.clear()
  books_listbox.delete(0, 'end')
  _set_meta_text(lambda: _append('Выберите книгу из списка.\n', 'muted'))

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
  q_title = title_search.get().strip().lower() if title_search else ''
  q_author = author_search.get().strip().lower() if author_search else ''

  pos_fandoms = tag_fandom.get_positive() if tag_fandom else []
  neg_fandoms = tag_fandom.get_negative() if tag_fandom else []
  pos_pairings = tag_pairing.get_positive() if tag_pairing else []
  neg_pairings = tag_pairing.get_negative() if tag_pairing else []
  pos_tags   = tag_tags.get_positive()  if tag_tags  else []
  neg_tags   = tag_tags.get_negative()  if tag_tags  else []

  q_chapters_raw = chapters_entry.get().strip() if chapters_entry else ''
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

      if q_title and q_title not in (meta.get('title') or '').lower():
        continue
      if q_author and q_author not in (meta.get('author') or '').lower():
        continue

      book_fandoms_str = ' | '.join(f.lower() for f in (meta.get('fandom') or []))
      if pos_fandoms and not all(pf in book_fandoms_str for pf in pos_fandoms):
        continue
      if neg_fandoms and any(nf in book_fandoms_str for nf in neg_fandoms):
        continue

      book_pairings_str = ' | '.join(p.lower() for p in (meta.get('pairing') or []))
      if pos_pairings and not all(pp in book_pairings_str for pp in pos_pairings):
        continue
      if neg_pairings and any(np in book_pairings_str for np in neg_pairings):
        continue

      book_tags_str = ' | '.join(t.lower() for t in (meta.get('tags') or []))
      if pos_tags and not all(pt in book_tags_str for pt in pos_tags):
        continue
      if neg_tags and any(nt in book_tags_str for nt in neg_tags):
        continue

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
    f'Найдено {len(matches)} книг.\nВыберите книгу из списка.\n', 'muted'))


def reset_search():
  if title_search:
    title_search.delete()
  if author_search:
    author_search.delete()
  if chapters_entry:
    chapters_entry.delete(0, 'end')
  if tag_fandom:
    tag_fandom.clear()
  if tag_pairing:
    tag_pairing.clear()
  if tag_tags:
    tag_tags.clear()
  refresh_library()


# ── Лог ───────────────────────────────────────────────────────────────────────
log_frame = ttk.Frame(root)
log_frame.pack(fill='x', padx=6, pady=(2, 4))
ttk.Label(log_frame, text='Лог:', font=FONT_TINY,
     foreground=FG_MUTED).pack(anchor='w')
log_area = scrolledtext.ScrolledText(
  log_frame, wrap='word', height=9,
  bg='#181825', fg=FG_DIM, font=FONT_MONO,
  relief='flat', bd=0)
log_area.pack(fill='x')

_DOWNLOAD_TABS = ('Коллекция', 'Книги', 'Интервал')


def _apply_tab_layout(tab: str):
  if tab in _DOWNLOAD_TABS:
    tabs_container.configure(height=max(220, int(SCREEN_H * 0.42)))
    if not log_frame.winfo_ismapped():
      log_frame.pack(fill='x', padx=6, pady=(2, 4))
    try:
      log_area.configure(height=9)
    except Exception:
      pass
  else:
    tabs_container.configure(height=max(520, int(SCREEN_H * 0.90)))
    if log_frame.winfo_ismapped():
      log_frame.pack_forget()
  try:
    tabs_container.pack_propagate(False)
  except Exception:
    pass
  root.update_idletasks()


def on_tab_change(event=None):
  tab = notebook.tab(notebook.select(), 'text')
  if tab == 'Библиотека':
    refresh_library()
  _apply_tab_layout(tab)


notebook.bind('<<NotebookTabChanged>>', on_tab_change)

# Лог показываем только на вкладках скачивания
current_tab = notebook.tab(notebook.select(), 'text')
_apply_tab_layout(current_tab)

root.mainloop()
