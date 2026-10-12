"""Windows floating novel reader. Run with Python 3.11+ after installing requirements."""

from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import statistics
import sys
import tempfile
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, simpledialog
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import requests
import trafilatura
from bs4 import BeautifulSoup
from charset_normalizer import from_bytes
from PIL import ImageGrab


APP_NAME = "拾光阅读窗"
APP_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = Path(os.environ.get("LOCALAPPDATA", str(APP_DIR))) / "ShiguangReader" / "settings.json"
MAX_HTML_BYTES = 6 * 1024 * 1024
AD_LINE = re.compile(
    r"^(?:广告|最新网址|手机用户请|请记住本站|收藏本站|加入书签|推荐阅读|本章未完|扫码|备用网址|下载本站|最新网址|小说网).*",
    re.I,
)
CONTENT_HINT = re.compile(r"(?:^|[-_ ])(?:chapter|read|article|content|text|正文|章节)(?:$|[-_ ])", re.I)


@dataclass
class Article:
    title: str
    text: str
    url: str
    previous_url: str | None = None
    next_url: str | None = None
    book_key: str = ""
    book_name: str = ""


def infer_book(soup: BeautifulSoup, url: str, chapter_title: str) -> tuple[str, str]:
    """Use book metadata/breadcrumbs, then a conservative URL-path fallback."""
    parsed = urlparse(url)
    book_name = ""
    for selector in (
        'meta[property="og:novel:book_name"]', 'meta[property="og:book_name"]',
        'meta[name="book_name"]', 'meta[name="novel_name"]',
    ):
        meta = soup.select_one(selector)
        if meta and meta.get("content", "").strip():
            book_name = meta["content"].strip()
            break

    book_url = ""
    up = soup.find("link", rel=lambda rel: rel and "up" in rel)
    if up and up.get("href"):
        book_url = urljoin(url, up["href"])
    if not book_url:
        for node in soup.find_all(True, attrs={"class": re.compile(r"bread|crumb|position|path", re.I)}):
            links = [a for a in node.find_all("a", href=True)
                     if a.get_text(" ", strip=True) not in {"首页", "主页", "目录", "Home"}]
            for link in reversed(links):
                candidate = urljoin(url, link["href"])
                if urlparse(candidate).netloc == parsed.netloc and candidate != url:
                    book_url = candidate
                    book_name = book_name or link.get_text(" ", strip=True)
                    break
            if book_url:
                break

    if not book_url:
        parts = [p for p in parsed.path.split("/") if p]
        parent = parts[:-1]
        generic = {"chapter", "chapters", "read", "reader", "article", "page", "pages", "wiki"}
        if parent and parent[-1].lower() not in generic:
            book_url = urlunparse((parsed.scheme, parsed.netloc, "/" + "/".join(parent), "", "", ""))
        elif parent and parent[-1].lower() == "wiki" and len(parts) >= 3:
            book_url = urlunparse((parsed.scheme, parsed.netloc, "/" + "/".join(parent), "", "", ""))
        else:
            book_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

    book_url_parsed = urlparse(book_url)
    book_key = urlunparse((book_url_parsed.scheme.lower(), book_url_parsed.netloc.lower(),
                           book_url_parsed.path.rstrip("/"), "", "", ""))
    if not book_name:
        tail = unquote(book_url_parsed.path.rstrip("/").rsplit("/", 1)[-1]).strip()
        if tail and not tail.isdigit() and tail.lower() not in {"chapter", "read", "wiki"}:
            book_name = tail
        else:
            book_name = chapter_title
    return book_key, book_name[:80]


def normalize_text(raw: str) -> str:
    lines: list[str] = []
    for part in raw.replace("\u3000", " ").splitlines():
        line = re.sub(r"[\t\r ]+", " ", part).strip()
        if not line or AD_LINE.match(line) and len(line) < 100:
            continue
        if line in {"上一章", "下一章", "返回目录", "目录"}:
            continue
        lines.append(line)
    return "\n\n".join(lines)


def _chapter_body(soup: BeautifulSoup) -> str:
    candidates = []
    for tag in soup.find_all(["article", "section", "div"]):
        marker = " ".join([str(tag.get("id", "")), " ".join(tag.get("class", []))])
        if tag.name != "article" and not CONTENT_HINT.search(marker):
            continue
        if re.search(r"nav|menu|footer|header|comment|recommend|sidebar|list", marker, re.I):
            continue
        text = normalize_text(tag.get_text("\n", strip=True))
        if len(text) < 180:
            continue
        link_length = sum(len(a.get_text(strip=True)) for a in tag.find_all("a"))
        score = len(text) - 2 * link_length
        if re.search(r"chapter|read|正文|章节", marker, re.I):
            score += 250
        candidates.append((score, text))
    return max(candidates, default=(0, ""))[1]


def extract_article(html: bytes, url: str, encoding: str | None = None) -> Article:
    soup = BeautifulSoup(html, "html.parser", from_encoding=encoding)
    title_tag = soup.find("h1")
    title = title_tag.get_text(" ", strip=True) if title_tag else ""
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    title = title or urlparse(url).netloc
    title = re.split(r"\s+[-_|]\s+", title)[0].strip()
    book_key, book_name = infer_book(soup, url, title)

    previous_url = next_url = None
    for link in soup.find_all("a", href=True):
        label = link.get_text(" ", strip=True)
        rel = " ".join(link.get("rel", []))
        href = urljoin(url, link["href"])
        if urlparse(href).scheme not in {"http", "https"} or href == url:
            continue
        if not previous_url and (re.search(r"(?:上|前)一[章节回页頁]", label) or rel == "prev"):
            previous_url = href
        if not next_url and (re.search(r"(?:下|后)一[章节回页頁]", label) or rel == "next"):
            next_url = href

    for tag in soup(["script", "style", "noscript", "iframe", "svg", "form", "nav", "footer", "aside"]):
        tag.decompose()
    for tag in list(soup.find_all(True)):
        if tag.attrs is None:  # A parent may already have been removed.
            continue
        marker = " ".join([str(tag.get("id", "")), " ".join(tag.get("class", []))])
        if re.search(r"(?:^|[-_ ])(?:advert|ads?|sponsor|popup|comment)(?:$|[-_ ])", marker, re.I):
            tag.decompose()
    cleaned_html = str(soup)
    extracted = trafilatura.extract(
        cleaned_html,
        url=url,
        include_comments=False,
        include_tables=False,
        include_images=False,
        favor_precision=True,
    ) or ""
    extracted = normalize_text(extracted)
    chapter = _chapter_body(soup)
    body = chapter if len(chapter) >= 300 and len(chapter) > len(extracted) * 1.25 else extracted
    if len(body) < 100:
        body = chapter
    if len(body) < 100:
        raise ValueError("没有找到足够的正文。页面可能需要登录、依赖脚本加载，或不是文章页。")
    return Article(title=title, text=body, url=url, previous_url=previous_url,
                   next_url=next_url, book_key=book_key, book_name=book_name)


def fetch_article(url: str) -> Article:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("请输入完整的 http:// 或 https:// 网页地址。")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    }
    with requests.get(url, headers=headers, timeout=(8, 20), stream=True) as response:
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if content_type and not any(x in content_type.lower() for x in ("html", "text/plain", "xml")):
            raise ValueError("这个地址没有返回网页正文。")
        data = bytearray()
        for chunk in response.iter_content(64 * 1024):
            data.extend(chunk)
            if len(data) > MAX_HTML_BYTES:
                raise ValueError("网页超过 6 MB，已停止读取。")
        encoding = response.encoding
        if not encoding or encoding.lower() in {"iso-8859-1", "ascii"}:
            match = from_bytes(bytes(data[:131072])).best()
            encoding = match.encoding if match else None
        return extract_article(bytes(data), response.url, encoding)


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    channels = []
    for value in rgb:
        s = value / 255
        channels.append(s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def readable_palette(sample: tuple[int, int, int]) -> tuple[str, str, str]:
    # Keep the sampled surface while choosing the more legible of near-black/near-white text.
    bg = tuple(max(18, min(244, int(v))) for v in sample)
    luminance = _relative_luminance(bg)
    dark = (0, 0, 0)
    light = (255, 255, 255)
    dark_ratio = (luminance + 0.05) / (_relative_luminance(dark) + 0.05)
    light_ratio = (_relative_luminance(light) + 0.05) / (luminance + 0.05)
    fg = dark if dark_ratio >= light_ratio else light
    secondary = tuple(round(f * 0.68 + b * 0.32) for f, b in zip(fg, bg))
    to_hex = lambda c: "#%02x%02x%02x" % c
    return to_hex(bg), to_hex(fg), to_hex(secondary)


def render_with_letter_spacing(text: str, spacing: int) -> str:
    """Render only the view with hair spaces; the source article remains unchanged."""
    if spacing <= 0:
        return text
    separator = "\u200a" * spacing
    return "\n".join(
        "".join(separator.join(chunk) if not chunk.isspace() else chunk
                for chunk in re.split(r"(\s+)", line))
        for line in text.split("\n")
    )


def sample_surrounding(root: tk.Tk) -> tuple[int, int, int]:
    """Fallback for systems that cannot exclude this window from capture."""
    x, y = root.winfo_rootx(), root.winfo_rooty()
    w, h = root.winfo_width(), root.winfo_height()
    user32 = ctypes.windll.user32
    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    vr, vb = vx + user32.GetSystemMetrics(78), vy + user32.GetSystemMetrics(79)
    candidates = [
        (x - 34, y + 8, x - 6, y + h - 8),
        (x + w + 6, y + 8, x + w + 34, y + h - 8),
        (x + 8, y - 34, x + w - 8, y - 6),
        (x + 8, y + h + 6, x + w - 8, y + h + 34),
    ]
    samples: list[tuple[int, int, int]] = []
    for left, top, right, bottom in candidates:
        box = (max(vx, left), max(vy, top), min(vr, right), min(vb, bottom))
        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
            continue
        image = ImageGrab.grab(bbox=box, all_screens=True).convert("RGB").resize((12, 12))
        pixels = image.get_flattened_data() if hasattr(image, "get_flattened_data") else image.getdata()
        samples.extend(pixels)
    if not samples:
        raise RuntimeError("窗口四周没有可取样的屏幕区域")
    return tuple(int(statistics.median(pixel[i] for pixel in samples)) for i in range(3))


def sample_background(root: tk.Tk, affinity_set: bool) -> tuple[int, int, int]:
    """Capture the exact covered area without briefly hiding the reader."""
    if not affinity_set:
        return sample_surrounding(root)
    x, y = root.winfo_rootx(), root.winfo_rooty()
    w, h = root.winfo_width(), root.winfo_height()
    image = ImageGrab.grab(bbox=(x + 5, y + 5, x + w - 5, y + h - 5), all_screens=True)
    image = image.convert("RGB").resize((24, 24))
    pixels = image.get_flattened_data() if hasattr(image, "get_flattened_data") else image.getdata()
    return tuple(int(statistics.median(pixel[i] for pixel in pixels)) for i in range(3))


def set_capture_exclusion(root: tk.Tk, enabled: bool = True) -> bool:
    try:
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.SetWindowDisplayAffinity.restype = ctypes.c_int
        hwnd = user32.GetAncestor(root.winfo_id(), 2)
        return bool(hwnd and user32.SetWindowDisplayAffinity(hwnd, 0x11 if enabled else 0))
    except (AttributeError, OSError):
        return False


def alt_key_state() -> tuple[bool, bool]:
    """Return whether Alt is held and whether another physical key is held."""
    try:
        user32 = ctypes.windll.user32
        down = bool(user32.GetAsyncKeyState(0x12) & 0x8000)
        if not down:
            return False, False
        other = any(user32.GetAsyncKeyState(code) & 0x8000
                    for code in range(1, 255) if code not in (0x12, 0xA4, 0xA5))
        return True, other
    except (AttributeError, OSError):
        return False, False


def reader_is_foreground(root: tk.Tk) -> bool:
    try:
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        return user32.GetForegroundWindow() == user32.GetAncestor(root.winfo_id(), 2)
    except (AttributeError, OSError, tk.TclError):
        return False


class ReaderApp:
    def __init__(self) -> None:
        self.settings = self._load_settings()
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", self.settings.get("topmost", True))
        self.root.minsize(300, 240)
        width, height = 510, 390
        x = 26
        y = max(30, self.root.winfo_screenheight() - height - 78)
        geometry = self.settings.get("geometry")
        self.root.geometry(geometry if isinstance(geometry, str) and re.fullmatch(r"\d+x\d+[-+]\d+[-+]\d+", geometry) else f"{width}x{height}+{x}+{y}")
        self.font_size = max(10, min(24, int(self.settings.get("font_size", 12))))
        self.line_spacing = max(0, min(24, int(self.settings.get("line_spacing", 3))))
        self.paragraph_spacing = max(0, min(40, int(self.settings.get("paragraph_spacing", 10))))
        self.letter_spacing = max(0, min(3, int(self.settings.get("letter_spacing", 0))))
        self.current_url = self.settings.get("url", "")
        saved_books = self.settings.get("books", {})
        self.books: dict[str, dict] = saved_books if isinstance(saved_books, dict) else {}
        self.last_book_key = self.settings.get("last_book_key", "")
        self.current_book_key = ""
        self.pending_book_key = ""
        self.pending_restore = False
        self.restoring_position = False
        self.auto_background = bool(self.settings.get("auto_background", True))
        self.article: Article | None = None
        self.chapters: list[Article] = []
        self.chapter_starts: list[str] = []
        self.active_chapter_index = 0
        self.prefetch_queue: queue.Queue[tuple[int, str, Article | Exception]] = queue.Queue()
        self.prefetch_url = ""
        self.prefetch_attempted: set[str] = set()
        self.chapter_generation = 0
        self._closing = False
        self.loading = False
        self.result_queue: queue.Queue[Article | Exception] = queue.Queue()
        self.drag_start: tuple[int, int, int, int] | None = None
        self.resize_start: tuple[int, int, int, int] | None = None
        self._save_after: str | None = None
        self._capture_after: str | None = None
        self._background_after: str | None = None
        self._results_after: str | None = None
        self._resume_after: str | None = None
        self._alt_after: str | None = None
        self.hidden = False
        self._alt_was_down = False
        self._alt_chord = False
        self._alt_started_active = False
        self._shown_at = 0.0
        self.typography_panel: tk.Toplevel | None = None
        self.typography_values: dict[str, tk.IntVar] = {}
        stored_color = self.settings.get("background_rgb", [228, 231, 228])
        if not isinstance(stored_color, list) or len(stored_color) != 3 or not all(isinstance(v, int) and 0 <= v <= 255 for v in stored_color):
            stored_color = [228, 231, 228]
        self._last_color: tuple[int, int, int] | None = tuple(stored_color)

        self.header = tk.Frame(self.root, height=31, bd=0)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)
        self.title_label = tk.Label(self.header, text="  拾光阅读窗 · 双击输入网址", anchor="w", cursor="fleur", font=("Microsoft YaHei UI", 9))
        self.title_label.pack(side="left", fill="both", expand=True)
        self.title_label.bind("<ButtonPress-1>", self._start_drag)
        self.title_label.bind("<B1-Motion>", self._drag)
        self.title_label.bind("<ButtonRelease-1>", lambda _e: self._after_move())
        self.title_label.bind("<Double-Button-1>", lambda _e: self.show_url())
        self.prev_button = self._small_button("‹", self.open_previous)
        self.next_button = self._small_button("›", self.open_next)
        self.url_button = self._small_button("⌕", self.show_url)
        self.close_button = self._small_button("×", self.close)

        self.url_frame = tk.Frame(self.root)
        self.url_entry = tk.Entry(self.url_frame, relief="flat", font=("Microsoft YaHei UI", 9))
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(9, 2), pady=5)
        self.url_entry.insert(0, self.current_url)
        self.url_entry.bind("<Return>", lambda _e: self.open_url(self.url_entry.get()))
        self.go_button = tk.Button(self.url_frame, text="打开", relief="flat", bd=0, command=lambda: self.open_url(self.url_entry.get()))
        self.go_button.pack(side="right", padx=(0, 7))
        self.paste_button = tk.Button(self.url_frame, text="粘贴", relief="flat", bd=0, command=self.paste_url)
        self.paste_button.pack(side="right", padx=(0, 3))

        self.main = tk.Frame(self.root, bd=0)
        self.main.pack(fill="both", expand=True)
        self.scroll = tk.Scrollbar(self.main, width=8, bd=0)
        self.scroll.pack(side="right", fill="y")
        self.text = tk.Text(
            self.main, wrap="word", bd=0, relief="flat", padx=15, pady=11,
            font=("Microsoft YaHei UI", self.font_size), spacing1=0,
            spacing2=self.line_spacing, spacing3=self.paragraph_spacing,
            undo=False, cursor="arrow", yscrollcommand=self._on_text_scroll,
        )
        self.text.pack(side="left", fill="both", expand=True)
        self.scroll.config(command=self.text.yview)
        self.text.insert("1.0", "把小说章节的网址粘贴进来，就能开始阅读。\n\n点击顶栏的 ⌕ 或按 Ctrl+L 输入网址；拖动顶栏移动窗口，拖动右下角调整大小。\n\n右键可切换置顶、字号，或打开原网页。")
        self.text.config(state="disabled")

        self.footer = tk.Frame(self.root, height=28)
        self.main.pack_forget()
        self.footer.pack(side="bottom", fill="x")
        self.main.pack(fill="both", expand=True)
        self.footer.pack_propagate(False)
        self.status = tk.Label(self.footer, text="就绪", anchor="w", font=("Microsoft YaHei UI", 8))
        self.status.pack(side="left", fill="x", expand=True, padx=9)
        self.grip = tk.Label(self.footer, text="◢", width=2, cursor="size_nw_se", font=("Segoe UI", 9))
        self.grip.pack(side="right")
        self.grip.bind("<ButtonPress-1>", self._start_resize)
        self.grip.bind("<B1-Motion>", self._resize)
        self.grip.bind("<ButtonRelease-1>", lambda _e: self._after_move())
        self.library_button = self._footer_button("书架", self.show_library)
        self.typography_button = self._footer_button("排版", self.show_typography_panel)
        self.capture_button = self._footer_button("取色", self.manual_capture)
        self.auto_button = self._footer_button("自动开" if self.auto_background else "自动关", self.toggle_auto_background)

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="输入网址  Ctrl+L", command=self.show_url)
        self.menu.add_command(label="在浏览器打开原网页", command=self.open_original)
        self.menu.add_separator()
        self.menu.add_command(label="排版设置…", command=self.show_typography_panel)
        self.topmost_var = tk.BooleanVar(value=self.settings.get("topmost", True))
        self.menu.add_checkbutton(label="窗口置顶", variable=self.topmost_var, command=self.toggle_topmost)
        self.auto_var = tk.BooleanVar(value=self.auto_background)
        self.menu.add_checkbutton(label="自动调整背景色", variable=self.auto_var, command=self.toggle_auto_background)
        self.menu.add_command(label="立即抓取背景色", command=self.manual_capture)
        self.menu.add_command(label="书架 / 阅读进度", command=self.show_library)
        self.menu.add_separator()
        self.menu.add_command(label="退出  Ctrl+Q", command=self.close)
        for widget in (self.root, self.header, self.title_label, self.main, self.text, self.footer, self.status):
            widget.bind("<Button-3>", self._popup_menu)
        self.root.bind_all("<Control-l>", lambda _e: self.show_url())
        self.root.bind_all("<Control-q>", lambda _e: self.close())
        self.root.bind_all("<Control-plus>", lambda _e: self.change_font(1))
        self.root.bind_all("<Control-equal>", lambda _e: self.change_font(1))
        self.root.bind_all("<Control-minus>", lambda _e: self.change_font(-1))
        self.root.bind_all("<Control-b>", lambda _e: self.show_library())
        self.root.bind_all("<Alt-Left>", lambda _e: self.open_previous())
        self.root.bind_all("<Alt-Right>", lambda _e: self.open_next())
        self.root.bind("<space>", self.page_down)
        self.root.bind("<Right>", self.next_chapter_key)
        self.root.bind("<Left>", self.previous_chapter_key)
        for widget in (self.text, self.prev_button, self.next_button, self.url_button,
                       self.close_button, self.library_button, self.typography_button,
                       self.capture_button, self.auto_button):
            widget.bind("<space>", self.page_down)
            widget.bind("<Right>", self.next_chapter_key)
            widget.bind("<Left>", self.previous_chapter_key)
        self.root.bind_all("<KeyPress>", self._note_alt_chord, add="+")
        self.root.update_idletasks()
        self.affinity_set = set_capture_exclusion(self.root) if self.auto_background else False
        self.apply_palette(self._last_color)
        if self.auto_background:
            self._capture_after = self.root.after(350, self.update_background)
        self._background_after = self.root.after(2500, self._background_loop)
        self._results_after = self.root.after(80, self._poll_results)
        self._alt_after = self.root.after(25, self._poll_alt)
        if self.last_book_key in self.books or self.current_url:
            self._resume_after = self.root.after(500, self._resume_on_start)

    @staticmethod
    def _load_settings() -> dict:
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_settings(self) -> None:
        if self._save_after:
            self.root.after_cancel(self._save_after)
        self._save_after = None
        try:
            self._record_position()
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            content = {
                "geometry": self.root.geometry(), "font_size": self.font_size,
                "line_spacing": self.line_spacing, "paragraph_spacing": self.paragraph_spacing,
                "letter_spacing": self.letter_spacing,
                "url": "" if self.article and self.current_book_key not in self.books else self.current_url,
                "topmost": bool(self.topmost_var.get()),
                "auto_background": self.auto_background,
                "background_rgb": list(self._last_color or (228, 231, 228)),
                "last_book_key": self.last_book_key,
                "books": self.books,
            }
            temp_path = SETTINGS_PATH.with_suffix(".tmp")
            temp_path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(SETTINGS_PATH)
        except OSError:
            pass

    def _save_soon(self) -> None:
        if self._closing:
            return
        if self._save_after:
            self.root.after_cancel(self._save_after)
        self._save_after = self.root.after(500, self._save_settings)

    def _small_button(self, label: str, command) -> tk.Button:
        button = tk.Button(self.header, text=label, width=3, relief="flat", bd=0, command=command, font=("Segoe UI", 12))
        button.pack(side="left")
        return button

    def _footer_button(self, label: str, command) -> tk.Button:
        button = tk.Button(self.footer, text=label, relief="flat", bd=0, padx=3,
                           font=("Microsoft YaHei UI", 8), command=command)
        button.pack(side="right", padx=1)
        return button

    def _on_text_scroll(self, first: str, last: str) -> None:
        self.scroll.set(first, last)
        if self.chapters and not self.restoring_position:
            self._sync_active_chapter()
            self._maybe_prefetch_next()
            self._save_soon()

    def _record_position(self) -> None:
        if self.article and self.current_book_key in self.books:
            start = self.chapter_starts[self.active_chapter_index]
            top = self.text.index("@0,0")
            if self.text.compare(top, "<", start):
                top = start
            start_line = int(start.split(".")[0])
            line, column = map(int, top.split("."))
            self.books[self.current_book_key]["scroll_index"] = f"{line - start_line + 1}.{column}"

    def _activate_chapter(self, index: int) -> None:
        if index == self.active_chapter_index:
            return
        self.active_chapter_index = index
        article = self.chapters[index]
        self.article = article
        self.current_url = article.url
        self.title_label.config(text=f"  {article.title[:32]}")
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, article.url)
        if self.current_book_key in self.books:
            record = self.books[self.current_book_key]
            record["chapter"] = article.title
            record["url"] = article.url
            record["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self.last_book_key = self.current_book_key
        self.status.config(text=f"{article.title[:24]} · 进度已保存")

    def _sync_active_chapter(self) -> None:
        if not self.chapters:
            return
        top = self.text.index("@0,0")
        bottom = self.text.index(f"@0,{max(0, self.text.winfo_height() - 2)}")
        at_end = self.text.compare(bottom, ">=", "end-1c")
        index = 0
        for candidate in range(1, len(self.chapter_starts)):
            start = self.chapter_starts[candidate]
            if self.text.compare(top, ">=", start) or (at_end and self.text.compare(bottom, ">=", start)):
                index = candidate
        self._activate_chapter(index)

    def _maybe_prefetch_next(self) -> None:
        if not self.chapters or self.loading or self.prefetch_url:
            return
        latest = self.chapters[-1]
        url = latest.next_url
        if not url or url in self.prefetch_attempted or any(chapter.url == url for chapter in self.chapters):
            return
        start = self.chapter_starts[-1]
        end = self.text.index("end-1c")
        total = (self.text.count(start, end, "chars") or (0,))[0]
        if total <= 0:
            return
        visible_bottom = self.text.index(f"@0,{max(0, self.text.winfo_height() - 2)}")
        if self.text.compare(visible_bottom, "<", start):
            return
        read = (self.text.count(start, visible_bottom, "chars") or (0,))[0]
        if read / total < 0.75:
            return
        self.prefetch_url = url
        self.prefetch_attempted.add(url)
        generation = self.chapter_generation

        def worker() -> None:
            try:
                result: Article | Exception = fetch_article(url)
            except Exception as error:
                result = error
            self.prefetch_queue.put((generation, url, result))

        threading.Thread(target=worker, daemon=True).start()

    def _append_chapter(self, article: Article) -> None:
        top = self.text.index("@0,0")
        self.restoring_position = True
        self.text.config(state="normal")
        self.text.insert("end-1c", "\n\n\n")
        start = self.text.index("end-1c")
        body = render_with_letter_spacing(article.text, self.letter_spacing)
        self.text.insert("end-1c", article.title + "\n\n" + body)
        self.text.config(state="disabled")
        self.chapters.append(article)
        self.chapter_starts.append(start)
        self.text.yview(top)
        self.restoring_position = False
        self.status.config(text=f"已接续下一章：{article.title[:24]}")
        self._save_soon()

    def _resume_on_start(self) -> None:
        if self.article or self.loading:
            return
        if self.last_book_key in self.books:
            self.open_saved_book(self.last_book_key)
        elif self.current_url:
            self.open_url(self.current_url)

    def _start_drag(self, event) -> None:
        self.drag_start = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def _drag(self, event) -> None:
        if self.drag_start:
            sx, sy, x, y = self.drag_start
            self.root.geometry(f"+{x + event.x_root - sx}+{y + event.y_root - sy}")

    def _start_resize(self, event) -> None:
        self.resize_start = (event.x_root, event.y_root, self.root.winfo_width(), self.root.winfo_height())

    def _resize(self, event) -> None:
        if self.resize_start:
            sx, sy, w, h = self.resize_start
            self.root.geometry(f"{max(300, w + event.x_root - sx)}x{max(240, h + event.y_root - sy)}")

    def _after_move(self) -> None:
        if self.auto_background:
            self.update_background()
        self._save_soon()

    def _popup_menu(self, event) -> None:
        self.menu.tk_popup(event.x_root, event.y_root)

    def show_url(self) -> None:
        if not self.url_frame.winfo_manager():
            self.url_frame.pack(after=self.header, fill="x")
        self.url_entry.focus_set()
        self.url_entry.selection_range(0, "end")

    def paste_url(self) -> None:
        try:
            value = self.root.clipboard_get().strip()
        except tk.TclError:
            self.status.config(text="剪贴板中没有可粘贴的文字")
            return
        if not value:
            self.status.config(text="剪贴板中没有可粘贴的网址")
            return
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, value)
        self.url_entry.focus_set()
        self.url_entry.icursor("end")
        self.status.config(text="已粘贴网址，按回车或点击打开")

    def hide_url(self) -> None:
        self.url_frame.pack_forget()
        self.text.focus_set()

    def change_font(self, delta: int) -> None:
        self._set_typography("font_size", self.font_size + delta)

    def _render_article(self, view_fraction: float | None = None) -> None:
        if not self.chapters:
            return
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.chapter_starts = []
        for index, article in enumerate(self.chapters):
            if index:
                self.text.insert("end-1c", "\n\n\n")
            self.chapter_starts.append(self.text.index("end-1c"))
            rendered_body = render_with_letter_spacing(article.text, self.letter_spacing)
            self.text.insert("end-1c", article.title + "\n\n" + rendered_body)
        self.text.config(state="disabled")
        if view_fraction is not None:
            self.root.update_idletasks()
            self.text.yview_moveto(view_fraction)

    def _set_typography(self, key: str, value: int | float) -> None:
        limits = {
            "font_size": (10, 24), "line_spacing": (0, 24),
            "paragraph_spacing": (0, 40), "letter_spacing": (0, 3),
        }
        minimum, maximum = limits[key]
        value = max(minimum, min(maximum, int(round(float(value)))))
        if getattr(self, key) == value:
            return
        fraction = self.text.yview()[0]
        setattr(self, key, value)
        if key in self.typography_values:
            self.typography_values[key].set(value)
        self.restoring_position = True
        self.text.config(font=("Microsoft YaHei UI", self.font_size),
                         spacing2=self.line_spacing, spacing3=self.paragraph_spacing)
        self._render_article(fraction)
        self.restoring_position = False
        self._sync_active_chapter()
        labels = {"font_size": "字号", "line_spacing": "行间距",
                  "paragraph_spacing": "段间距", "letter_spacing": "字间距"}
        self.status.config(text=f"{labels[key]} {value}")
        self._save_soon()

    def show_typography_panel(self) -> None:
        if self.typography_panel and self.typography_panel.winfo_exists():
            self.typography_panel.lift()
            return
        panel = tk.Toplevel(self.root)
        self.typography_panel = panel
        panel.title("排版")
        panel.transient(self.root)
        panel.resizable(False, False)
        panel.geometry(f"305x240+{self.root.winfo_rootx() + 12}+{max(0, self.root.winfo_rooty() - 245)}")
        panel.protocol("WM_DELETE_WINDOW", self.close_typography_panel)
        frame = tk.Frame(panel, padx=12, pady=10)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="排版", anchor="w", font=("Microsoft YaHei UI", 10)).pack(fill="x", pady=(0, 6))
        specs = (
            ("font_size", "字号", 10, 24),
            ("line_spacing", "行间距", 0, 24),
            ("paragraph_spacing", "段间距", 0, 40),
            ("letter_spacing", "字间距", 0, 3),
        )
        self.typography_values = {}
        for key, label, minimum, maximum in specs:
            row = tk.Frame(frame)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, width=6, anchor="w", font=("Microsoft YaHei UI", 9)).pack(side="left")
            value = tk.IntVar(value=getattr(self, key))
            self.typography_values[key] = value
            scale = tk.Scale(row, from_=minimum, to=maximum, orient="horizontal", showvalue=False,
                             variable=value, resolution=1, length=172, highlightthickness=0,
                             command=lambda number, name=key: self._set_typography(name, number))
            scale.pack(side="left", fill="x", expand=True)
            tk.Label(row, textvariable=value, width=3, anchor="e", font=("Microsoft YaHei UI", 9)).pack(side="right")
        hint = "字距只改变显示效果，不修改章节原文"
        tk.Label(frame, text=hint, anchor="w", font=("Microsoft YaHei UI", 8)).pack(fill="x", pady=(5, 2))
        tk.Button(frame, text="关闭", command=self.close_typography_panel).pack(anchor="e", pady=(2, 0))
        self._style_typography_panel()

    def close_typography_panel(self) -> None:
        if self.typography_panel and self.typography_panel.winfo_exists():
            self.typography_panel.destroy()
        self.typography_panel = None
        self.typography_values = {}

    def _style_typography_panel(self) -> None:
        if not self.typography_panel or not self.typography_panel.winfo_exists() or self._last_color is None:
            return
        bg, fg, muted = readable_palette(self._last_color)
        self.typography_panel.config(bg=bg)

        def style(widget) -> None:
            try:
                if isinstance(widget, tk.Scale):
                    widget.config(bg=bg, fg=fg, troughcolor=muted, activebackground=fg,
                                  highlightbackground=bg)
                elif isinstance(widget, tk.Button):
                    widget.config(bg=bg, fg=fg, activebackground=bg, activeforeground=fg)
                else:
                    widget.config(bg=bg, fg=fg)
            except tk.TclError:
                pass
            for child in widget.winfo_children():
                style(child)

        style(self.typography_panel)

    def toggle_auto_background(self) -> None:
        self.auto_background = not self.auto_background
        self.auto_var.set(self.auto_background)
        self.auto_button.config(text="自动开" if self.auto_background else "自动关")
        if self.auto_background:
            self.affinity_set = set_capture_exclusion(self.root)
            self.update_background(force=True)
        else:
            if self._capture_after:
                self.root.after_cancel(self._capture_after)
                self._capture_after = None
            if self.affinity_set:
                set_capture_exclusion(self.root, enabled=False)
                self.affinity_set = False
        self.status.config(text="自动背景已开启" if self.auto_background else "自动背景已关闭，当前颜色已固定")
        self._save_soon()

    def manual_capture(self) -> None:
        temporary_exclusion = False
        if not self.auto_background:
            temporary_exclusion = set_capture_exclusion(self.root)
            self.affinity_set = temporary_exclusion
        try:
            success = self.update_background(force=True)
        finally:
            if temporary_exclusion:
                set_capture_exclusion(self.root, enabled=False)
                self.affinity_set = False
        self.status.config(text="已抓取当前位置的背景色" if success else "取色失败，已保留原颜色")
        self._save_soon()

    def page_down(self, event=None) -> str | None:
        if event is not None and event.widget == self.url_entry:
            return None
        self.text.yview_scroll(1, "pages")
        self._save_soon()
        return "break"

    def next_chapter_key(self, event=None) -> str | None:
        if event is not None and event.widget == self.url_entry:
            return None
        self.open_next()
        return "break"

    def previous_chapter_key(self, event=None) -> str | None:
        if event is not None and event.widget == self.url_entry:
            return None
        self.open_previous()
        return "break"

    def _note_alt_chord(self, event) -> None:
        if self._alt_was_down and event.keysym not in ("Alt_L", "Alt_R"):
            self._alt_chord = True

    def _poll_alt(self) -> None:
        down, other_key_down = alt_key_state()
        if down and not self._alt_was_down:
            self._alt_started_active = self.hidden or reader_is_foreground(self.root)
            self._alt_chord = other_key_down
        elif down and other_key_down:
            self._alt_chord = True
        elif not down and self._alt_was_down:
            if self._alt_started_active and not self._alt_chord:
                if self.hidden:
                    self.show_reader()
                elif reader_is_foreground(self.root):
                    self.hide_reader()
            self._alt_started_active = False
            self._alt_chord = False
        self._alt_was_down = down
        self._alt_after = self.root.after(25, self._poll_alt)

    def hide_reader(self, _event=None) -> str:
        if time.monotonic() - self._shown_at < 0.3:
            return "break"
        if not self.hidden:
            self._record_position()
            self._save_soon()
            self.hidden = True
            if self.affinity_set:
                set_capture_exclusion(self.root, enabled=False)
                self.affinity_set = False
            self.root.withdraw()
        return "break"

    def show_reader(self) -> None:
        if not self.hidden:
            return
        self.hidden = False
        self._shown_at = time.monotonic()
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", bool(self.topmost_var.get()))
        self.root.focus_force()
        self.text.focus_set()
        if self.auto_background:
            self.affinity_set = set_capture_exclusion(self.root)
            self.update_background(force=True)

    def toggle_topmost(self) -> None:
        self.root.attributes("-topmost", bool(self.topmost_var.get()))
        self._save_soon()

    def open_original(self) -> None:
        if self.current_url:
            import webbrowser
            webbrowser.open(self.current_url)

    def open_previous(self) -> None:
        if self.active_chapter_index > 0:
            index = self.active_chapter_index - 1
            self.text.yview(self.chapter_starts[index])
            self._activate_chapter(index)
            self._save_soon()
            return
        if self.article and self.article.previous_url:
            self.open_url(self.article.previous_url, book_key=self.current_book_key)

    def open_next(self) -> None:
        if self.active_chapter_index + 1 < len(self.chapters):
            index = self.active_chapter_index + 1
            self.text.yview(self.chapter_starts[index])
            self._activate_chapter(index)
            self._save_soon()
            return
        if self.article and self.article.next_url:
            self.open_url(self.article.next_url, book_key=self.current_book_key)

    def open_saved_book(self, book_key: str) -> None:
        record = self.books.get(book_key, {})
        if record.get("url"):
            self.open_url(record["url"], book_key=book_key, restore=True)

    def delete_books(self, book_keys: list[str]) -> int:
        """Remove selected local bookshelf records, without touching the source website."""
        keys = [key for key in dict.fromkeys(book_keys) if key in self.books]
        if not keys:
            return 0
        deleting_current = self.current_book_key in keys
        for key in keys:
            self.books.pop(key, None)
        if deleting_current:
            self.current_book_key = ""
        if self.last_book_key not in self.books:
            self.last_book_key = max(
                self.books,
                key=lambda key: self.books[key].get("updated_at", ""),
                default="",
            )
        self._save_settings()
        return len(keys)

    def show_library(self) -> None:
        if not self.books:
            messagebox.showinfo(APP_NAME, "书架还没有阅读记录。打开一个章节后会自动保存。", parent=self.root)
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("阅读书架")
        dialog.transient(self.root)
        dialog.geometry("480x310")
        dialog.minsize(380, 240)
        dialog.grab_set()
        frame = tk.Frame(dialog, padx=12, pady=12)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="最近阅读 · 双击继续", anchor="w", font=("Microsoft YaHei UI", 10)).pack(fill="x", pady=(0, 8))
        listbox = tk.Listbox(frame, font=("Microsoft YaHei UI", 9), activestyle="none", selectmode="extended")
        listbox.pack(fill="both", expand=True)
        keys: list[str] = []

        def refresh() -> None:
            keys[:] = sorted(self.books, key=lambda key: self.books[key].get("updated_at", ""), reverse=True)
            listbox.delete(0, "end")
            for key in keys:
                record = self.books[key]
                listbox.insert("end", f"{record.get('name', '未命名')}  ·  {record.get('chapter', '')}")
            if keys:
                listbox.selection_set(0)

        def selected_key() -> str | None:
            chosen = listbox.curselection()
            return keys[chosen[0]] if chosen else None

        def resume(_event=None) -> None:
            key = selected_key()
            if key:
                dialog.destroy()
                self.open_saved_book(key)

        def rename() -> None:
            key = selected_key()
            if not key:
                return
            name = simpledialog.askstring("修改书名", "小说名称：", initialvalue=self.books[key].get("name", ""), parent=dialog)
            if name and name.strip():
                self.books[key]["name"] = name.strip()[:80]
                refresh()
                self._save_soon()

        def delete_selected() -> None:
            selected = listbox.curselection()
            selected_keys = [keys[index] for index in selected]
            if not selected_keys:
                return
            count = len(selected_keys)
            names = "、".join(self.books[key].get("name", "未命名") for key in selected_keys[:3])
            if count > 3:
                names += " 等"
            if not messagebox.askyesno(APP_NAME, f"删除本地书架记录和阅读进度？\n{names}", parent=dialog):
                return
            removed = self.delete_books(selected_keys)
            refresh()
            self.status.config(text=f"已删除 {removed} 条书架记录")
            if not self.books:
                dialog.destroy()

        buttons = tk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 0))
        tk.Button(buttons, text="继续阅读", command=resume).pack(side="left")
        tk.Button(buttons, text="修改书名", command=rename).pack(side="left", padx=6)
        tk.Button(buttons, text="删除记录", command=delete_selected).pack(side="left")
        tk.Button(buttons, text="关闭", command=dialog.destroy).pack(side="right")
        listbox.bind("<Double-Button-1>", resume)
        listbox.bind("<Return>", resume)
        refresh()

    def open_url(self, url: str, book_key: str = "", restore: bool = False) -> None:
        if self.loading:
            return
        url = url.strip()
        if not url:
            return
        self.chapter_generation += 1  # Ignore a previous chapter's in-flight prefetch.
        self.prefetch_url = ""
        self._record_position()
        self.pending_book_key = book_key
        self.pending_restore = restore
        self.loading = True
        self.status.config(text="正在提取正文…")

        def worker() -> None:
            try:
                result: Article | Exception = fetch_article(url)
            except Exception as error:
                result = error
            self.result_queue.put(result)

        threading.Thread(target=worker, daemon=True).start()

    def _poll_results(self) -> None:
        try:
            generation, url, prefetched = self.prefetch_queue.get_nowait()
        except queue.Empty:
            pass
        else:
            if generation == self.chapter_generation and url == self.prefetch_url:
                self.prefetch_url = ""
                if isinstance(prefetched, Exception):
                    self.status.config(text="下一章预加载失败，可点击下一章重试")
                elif not any(chapter.url == prefetched.url for chapter in self.chapters):
                    self._append_chapter(prefetched)
        try:
            result = self.result_queue.get_nowait()
        except queue.Empty:
            pass
        else:
            self._finish_load(result)
        self._results_after = self.root.after(80, self._poll_results)

    def _finish_load(self, result: Article | Exception) -> None:
        self.loading = False
        if isinstance(result, Exception):
            self.status.config(text="读取失败")
            messagebox.showerror(APP_NAME, str(result), parent=self.root)
            return
        self.chapter_generation += 1
        self.prefetch_url = ""
        self.prefetch_attempted.clear()
        key = self.pending_book_key or result.book_key or result.url
        previous_record = self.books.get(key, {})
        restore_index = None
        if self.pending_restore or previous_record.get("url") == result.url:
            restore_index = previous_record.get("scroll_index")
        self.restoring_position = True
        self.article = result
        self.chapters = [result]
        self.chapter_starts = []
        self.active_chapter_index = 0
        self.current_url = result.url
        self.current_book_key = key
        self.last_book_key = key
        self.books[key] = {
            "name": previous_record.get("name") or result.book_name or result.title,
            "chapter": result.title,
            "url": result.url,
            "scroll_index": restore_index or "1.0",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, result.url)
        self.title_label.config(text=f"  {result.title[:32]}")
        self._render_article()
        self.root.update_idletasks()
        if restore_index:
            try:
                self.text.yview(restore_index)
            except tk.TclError:
                self.text.yview_moveto(0)
        else:
            self.text.yview_moveto(0)
        self.restoring_position = False
        self.status.config(text=f"{len(result.text):,} 字 · 进度已保存")
        self.hide_url()
        self._save_soon()
        self._maybe_prefetch_next()

    def apply_palette(self, sample: tuple[int, int, int]) -> None:
        bg, fg, muted = readable_palette(sample)
        for widget in (self.root, self.header, self.url_frame, self.main, self.footer):
            widget.config(bg=bg)
        for widget in (self.title_label, self.status, self.grip):
            widget.config(bg=bg, fg=muted)
        for widget in (self.prev_button, self.next_button, self.url_button, self.close_button, self.go_button,
                       self.paste_button,
                       self.library_button, self.typography_button,
                       self.capture_button, self.auto_button):
            widget.config(bg=bg, fg=fg, activebackground=bg, activeforeground=fg)
        self.text.config(bg=bg, fg=fg, insertbackground=fg, selectbackground=muted)
        self.url_entry.config(bg=bg, fg=fg, insertbackground=fg)
        self.scroll.config(bg=bg, activebackground=muted, troughcolor=bg)
        self._last_color = sample
        self._style_typography_panel()

    def update_background(self, force: bool = False) -> bool:
        if self.hidden:
            return False
        try:
            sample = sample_background(self.root, self.affinity_set)
            if force or self._last_color is None or sum(abs(a - b) for a, b in zip(sample, self._last_color)) > 18:
                self.apply_palette(sample)
                self._save_soon()
            return True
        except Exception:
            return False  # Keep the last readable color if screen capture is unavailable.

    def _background_loop(self) -> None:
        if self.auto_background and not self.hidden:
            self.update_background()
        self._background_after = self.root.after(2500, self._background_loop)

    def close(self) -> None:
        self._closing = True
        for callback_id in (self._save_after, self._capture_after, self._background_after,
                            self._results_after, self._resume_after, self._alt_after):
            if callback_id:
                self.root.after_cancel(callback_id)
        self._save_settings()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    if "--smoke-test" in sys.argv:
        with tempfile.TemporaryDirectory() as folder:
            SETTINGS_PATH = Path(folder) / "settings.json"
            app = ReaderApp()
            app.root.update()
            app.update_background()
            user32 = ctypes.windll.user32
            user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user32.GetAncestor.restype = ctypes.c_void_p
            user32.GetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
            user32.GetWindowDisplayAffinity.restype = ctypes.c_int
            affinity = ctypes.c_uint(0)
            hwnd = user32.GetAncestor(app.root.winfo_id(), 2)
            expected = 0x11 if app.affinity_set else 0
            if not user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity)) or affinity.value != expected:
                raise RuntimeError("Unexpected reader capture affinity")
            app.close()
    else:
        ReaderApp().run()
