import ctypes
import http.server
import importlib.util
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch



SOURCE = Path(__file__).resolve().parents[1] / "outputs" / "novel_reader.py"
spec = importlib.util.spec_from_file_location("novel_reader", SOURCE)
reader = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = reader
spec.loader.exec_module(reader)


BODY = "".join(f"<p>第{i}段：风从窗外吹进来，他继续读着这本书，想知道故事下一步会怎样。</p>" for i in range(15))
HTML = ("<html><head><meta charset='utf-8'><title>测试小说_广告网站</title></head><body>"
        "<header>导航栏 推荐阅读</header><div class='ad'>扫码关注广告</div>"
        "<h1>第一章 小窗</h1><div id='chapter-content'>" + BODY + "</div>"
        "<a href='/previous'>上一章</a><a href='/next'>下一章</a>"
        "<footer>页脚广告</footer></body></html>").encode("utf-8")


class FixtureHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        data = HTML
        content_type = "text/html; charset=utf-8"
        if self.path == "/no-charset":
            data = HTML.decode("utf-8").replace("charset='utf-8'", "charset='gb18030'").encode("gb18030")
            content_type = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        pass


class ReaderTests(unittest.TestCase):
    def test_extracts_chapter_and_navigation_without_ad(self):
        article = reader.extract_article(HTML, "https://example.com/chapter", "utf-8")
        self.assertEqual(article.title, "第一章 小窗")
        self.assertIn("第14段", article.text)
        self.assertNotIn("扫码关注广告", article.text)
        self.assertNotIn("页脚广告", article.text)
        self.assertEqual(article.previous_url, "https://example.com/previous")
        self.assertEqual(article.next_url, "https://example.com/next")

    def test_http_fetch_path(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            article = reader.fetch_article(f"http://127.0.0.1:{server.server_port}/chapter")
            self.assertIn("第14段", article.text)
            legacy_article = reader.fetch_article(f"http://127.0.0.1:{server.server_port}/no-charset")
            self.assertIn("第14段", legacy_article.text)
        finally:
            server.shutdown()
            server.server_close()

    def test_palette_readable_on_light_and_dark_background(self):
        for sample in ((245, 245, 245), (25, 28, 31), (125, 125, 125)):
            bg_hex, fg_hex, _ = reader.readable_palette(sample)
            convert = lambda value: tuple(int(value[i:i+2], 16) for i in (1, 3, 5))
            a, b = reader._relative_luminance(convert(bg_hex)), reader._relative_luminance(convert(fg_hex))
            ratio = (max(a, b) + .05) / (min(a, b) + .05)
            self.assertGreaterEqual(ratio, 4.5)

    def test_letter_spacing_renderer_keeps_source_text_unchanged(self):
        source = "春风\nabc def"
        rendered = reader.render_with_letter_spacing(source, 2)
        self.assertEqual(source, "春风\nabc def")
        self.assertEqual(rendered, "春\u200a\u200a风\na\u200a\u200ab\u200a\u200ac d\u200a\u200ae\u200a\u200af")

    def test_separate_books_on_same_site(self):
        first = reader.extract_article(HTML, "https://example.com/book/alpha/001", "utf-8")
        second = reader.extract_article(HTML, "https://example.com/book/beta/001", "utf-8")
        self.assertNotEqual(first.book_key, second.book_key)

    def test_saved_chapter_and_scroll_position_restored(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(reader, "SETTINGS_PATH", Path(folder) / "settings.json"):
            article = reader.extract_article(HTML, "https://example.com/book/alpha/001", "utf-8")
            article.next_url = None
            first = reader.ReaderApp()
            try:
                first._finish_load(article)
                first.text.yview_moveto(0.65)
                first.root.update_idletasks()
                first._save_settings()
                saved = json.loads(reader.SETTINGS_PATH.read_text(encoding="utf-8"))
                self.assertEqual(saved["books"][article.book_key]["url"], article.url)
                self.assertNotEqual(saved["books"][article.book_key]["scroll_index"], "1.0")
            finally:
                first.close()

            second = reader.ReaderApp()
            try:
                second.pending_book_key = article.book_key
                second.pending_restore = True
                second._finish_load(article)
                self.assertGreater(second.text.yview()[0], 0.2)
                self.assertEqual(second.last_book_key, article.book_key)
            finally:
                second.close()

    def test_auto_background_off_keeps_color_until_manual_capture(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(reader, "SETTINGS_PATH", Path(folder) / "settings.json"):
            app = reader.ReaderApp()
            try:
                app.toggle_auto_background()
                original = app._last_color
                with patch.object(reader, "sample_background", return_value=(40, 60, 80)) as sample:
                    app._background_loop()
                    self.assertEqual(sample.call_count, 0)
                    self.assertEqual(app._last_color, original)
                    app.manual_capture()
                    self.assertEqual(app._last_color, (40, 60, 80))
                app.change_font(1)
                self.assertEqual(app.font_size, 13)
            finally:
                app.close()

    def test_reopens_last_book_after_restart(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/book/alpha/001"
            article = reader.extract_article(HTML, url, "utf-8")
            with tempfile.TemporaryDirectory() as folder, patch.object(reader, "SETTINGS_PATH", Path(folder) / "settings.json"):
                reader.SETTINGS_PATH.write_text(json.dumps({
                    "url": url, "last_book_key": article.book_key,
                    "books": {article.book_key: {"name": "测试小说", "chapter": article.title,
                                               "url": url, "scroll_index": "10.0", "updated_at": "2026-10-09T12:00:00"}},
                }), encoding="utf-8")
                app = reader.ReaderApp()
                try:
                    deadline = time.monotonic() + 5
                    while app.article is None and time.monotonic() < deadline:
                        app.root.update()
                        time.sleep(0.02)
                    self.assertIsNotNone(app.article)
                    self.assertEqual(app.current_url, url)
                    self.assertGreater(app.text.yview()[0], 0.2)
                finally:
                    app.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_capture_affinity_follows_background_mode(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(reader, "SETTINGS_PATH", Path(folder) / "settings.json"):
            app = reader.ReaderApp()
            try:
                app.root.update()
                user32 = ctypes.windll.user32
                user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                user32.GetAncestor.restype = ctypes.c_void_p
                user32.GetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
                user32.GetWindowDisplayAffinity.restype = ctypes.c_int
                hwnd = user32.GetAncestor(app.root.winfo_id(), 2)
                affinity = ctypes.c_uint(999)
                self.assertTrue(user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity)))
                self.assertEqual(affinity.value, 0x11 if app.affinity_set else 0)
                app.toggle_auto_background()
                self.assertTrue(user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity)))
                self.assertEqual(affinity.value, 0)
                app.manual_capture()
                self.assertTrue(user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity)))
                self.assertEqual(affinity.value, 0)
            finally:
                app.close()

    def test_reading_keys_and_alt_toggle(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(reader, "SETTINGS_PATH", Path(folder) / "settings.json"):
            article = reader.extract_article(HTML, "https://example.com/book/alpha/001", "utf-8")
            article.next_url = None
            app = reader.ReaderApp()
            try:
                app._finish_load(article)
                before = app.text.yview()[0]
                self.assertEqual(app.page_down(SimpleNamespace(widget=app.text)), "break")
                self.assertGreater(app.text.yview()[0], before)
                with patch.object(app, "open_next") as next_chapter, patch.object(app, "open_previous") as previous_chapter:
                    app.next_chapter_key(SimpleNamespace(widget=app.text))
                    app.previous_chapter_key(SimpleNamespace(widget=app.text))
                    self.assertEqual(next_chapter.call_count, 1)
                    self.assertEqual(previous_chapter.call_count, 1)
                    app.next_chapter_key(SimpleNamespace(widget=app.url_entry))
                    self.assertEqual(next_chapter.call_count, 1)

                    self.assertTrue(app.text.bind("<space>"))
                    self.assertTrue(app.text.bind("<Right>"))
                    self.assertTrue(app.text.bind("<Left>"))

                self.assertFalse(app.root.bind("<Escape>"))
                key_state = [(False, False)]
                with patch.object(reader, "alt_key_state", side_effect=lambda: key_state[0]), patch.object(reader, "reader_is_foreground", return_value=True):
                    key_state[0] = (True, False)
                    deadline = time.monotonic() + 0.08
                    while time.monotonic() < deadline:
                        app.root.update()
                        time.sleep(0.01)
                    self.assertFalse(app.hidden)
                    key_state[0] = (False, False)
                    deadline = time.monotonic() + 0.3
                    while not app.hidden and time.monotonic() < deadline:
                        app.root.update()
                        time.sleep(0.01)
                    self.assertTrue(app.hidden)
                    key_state[0] = (True, False)
                    deadline = time.monotonic() + 0.08
                    while time.monotonic() < deadline:
                        app.root.update()
                        time.sleep(0.01)
                    key_state[0] = (False, False)
                    deadline = time.monotonic() + 0.3
                    while app.hidden and time.monotonic() < deadline:
                        app.root.update()
                        time.sleep(0.01)
                    self.assertFalse(app.hidden)
                    key_state[0] = (True, False)
                    deadline = time.monotonic() + 0.08
                    while time.monotonic() < deadline:
                        app.root.update()
                        time.sleep(0.01)
                    key_state[0] = (True, True)
                    deadline = time.monotonic() + 0.08
                    while time.monotonic() < deadline:
                        app.root.update()
                        time.sleep(0.01)
                    key_state[0] = (False, False)
                    deadline = time.monotonic() + 0.08
                    while time.monotonic() < deadline:
                        app.root.update()
                        time.sleep(0.01)
                    self.assertFalse(app.hidden)
            finally:
                app.close()

    def test_next_chapter_appends_at_75_percent_and_saves_its_own_progress(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(reader, "SETTINGS_PATH", Path(folder) / "settings.json"):
            first = reader.Article("第一章", ("第一章正文内容。" * 75 + "\n\n") * 20,
                                   "https://example.com/book/alpha/001",
                                   next_url="https://example.com/book/alpha/002",
                                   book_key="https://example.com/book/alpha", book_name="测试小说")
            second = reader.Article("第二章", ("第二章正文内容。" * 75 + "\n\n") * 20,
                                    first.next_url, previous_url=first.url,
                                    book_key=first.book_key, book_name=first.book_name)
            app = reader.ReaderApp()
            try:
                with patch.object(reader, "fetch_article", return_value=second) as fetch:
                    app._finish_load(first)
                    app.root.update()
                    self.assertEqual(fetch.call_count, 0)
                    app.text.yview_moveto(1.0)
                    deadline = time.monotonic() + 2
                    while len(app.chapters) < 2 and time.monotonic() < deadline:
                        app.root.update()
                        time.sleep(0.01)
                    self.assertEqual(len(app.chapters), 2)
                    self.assertEqual(fetch.call_count, 1)
                content = app.text.get("1.0", "end")
                self.assertLess(content.index("第一章"), content.index("第二章"))
                self.assertEqual(app.current_url, first.url)
                app.open_next()
                app.root.update()
                self.assertEqual(app.current_url, second.url)
                app.text.yview_moveto(1.0)
                app.root.update()
                app._save_settings()
                saved = json.loads(reader.SETTINGS_PATH.read_text(encoding="utf-8"))
                self.assertEqual(saved["books"][first.book_key]["url"], second.url)
                self.assertNotEqual(saved["books"][first.book_key]["scroll_index"], "1.0")
                app._set_typography("font_size", 14)
                self.assertIn("第二章", app.text.get("1.0", "end"))
            finally:
                app.close()

            reopened = reader.ReaderApp()
            try:
                reopened.pending_book_key = first.book_key
                reopened.pending_restore = True
                reopened._finish_load(second)
                reopened.root.update()
                self.assertEqual(reopened.current_url, second.url)
                self.assertGreater(reopened.text.yview()[0], 0.2)
            finally:
                reopened.close()

    def test_continuous_reading_prefetches_subsequent_chapters(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(reader, "SETTINGS_PATH", Path(folder) / "settings.json"):
            key = "https://example.com/book/alpha"
            chapters = [reader.Article(f"第{i}章", (f"第{i}章正文。" * 75 + "\n\n") * 20,
                                       f"{key}/{i:03}", book_key=key, book_name="测试小说")
                        for i in (1, 2, 3)]
            chapters[0].next_url = chapters[1].url
            chapters[1].next_url = chapters[2].url
            app = reader.ReaderApp()
            try:
                with patch.object(reader, "fetch_article", side_effect=chapters[1:]) as fetch:
                    app._finish_load(chapters[0])
                    app.root.update()
                    for expected in (2, 3):
                        app.text.yview_moveto(1.0)
                        deadline = time.monotonic() + 2
                        while len(app.chapters) < expected and time.monotonic() < deadline:
                            app.root.update()
                            time.sleep(0.01)
                        self.assertEqual(len(app.chapters), expected)
                        if expected == 2:
                            app.open_next()
                            app.root.update()
                    self.assertEqual(fetch.call_count, 2)
                    self.assertEqual([chapter.title for chapter in app.chapters], ["第1章", "第2章", "第3章"])
            finally:
                app.close()

    def test_typography_panel_updates_layout_and_persists(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(reader, "SETTINGS_PATH", Path(folder) / "settings.json"):
            article = reader.extract_article(HTML, "https://example.com/book/alpha/001", "utf-8")
            app = reader.ReaderApp()
            try:
                app._finish_load(article)
                app.show_typography_panel()
                app.root.update()
                self.assertTrue(app.typography_panel.winfo_exists())
                self.assertEqual(app.typography_button.cget("text"), "排版")
                app._set_typography("font_size", 16)
                app._set_typography("line_spacing", 12)
                app._set_typography("paragraph_spacing", 18)
                app._set_typography("letter_spacing", 2)
                self.assertEqual(app.text.cget("spacing2"), 12)
                self.assertEqual(app.text.cget("spacing3"), 18)
                self.assertIn("\u200a\u200a", app.text.get("1.0", "end"))
                self.assertNotIn("\u200a", app.article.text)
                app._save_settings()
                saved = json.loads(reader.SETTINGS_PATH.read_text(encoding="utf-8"))
                self.assertEqual(saved["font_size"], 16)
                self.assertEqual(saved["line_spacing"], 12)
                self.assertEqual(saved["paragraph_spacing"], 18)
                self.assertEqual(saved["letter_spacing"], 2)
            finally:
                app.close()

    def test_paste_button_populates_url_entry(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(reader, "SETTINGS_PATH", Path(folder) / "settings.json"):
            app = reader.ReaderApp()
            try:
                with patch.object(app.root, "clipboard_get", return_value="  https://example.com/chapter/8  "):
                    app.paste_url()
                self.assertEqual(app.url_entry.get(), "https://example.com/chapter/8")
                self.assertIn("已粘贴", app.status.cget("text"))
            finally:
                app.close()

    def test_deleting_books_removes_only_local_records_and_resume_state(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(reader, "SETTINGS_PATH", Path(folder) / "settings.json"):
            article = reader.extract_article(HTML, "https://example.com/book/alpha/001", "utf-8")
            app = reader.ReaderApp()
            try:
                app._finish_load(article)
                app.books["https://example.com/book/beta"] = {
                    "name": "第二本", "chapter": "第一章", "url": "https://example.com/book/beta/001",
                    "scroll_index": "1.0", "updated_at": "2026-10-10T10:00:00",
                }
                self.assertEqual(app.delete_books(["https://example.com/book/beta"]), 1)
                self.assertNotIn("https://example.com/book/beta", app.books)
                self.assertIn(article.book_key, app.books)
                self.assertEqual(app.delete_books([article.book_key]), 1)
                self.assertEqual(app.current_book_key, "")
                saved = json.loads(reader.SETTINGS_PATH.read_text(encoding="utf-8"))
                self.assertEqual(saved["books"], {})
                self.assertEqual(saved["url"], "")
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
