"""End-to-end tests against a small local Joomla-like site; no internet needed."""

from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import unquote, urlsplit
from unittest.mock import patch

from bs4 import BeautifulSoup

from archive_crawler import ARCHIVE_NOTICE, Config, Crawler, css_urls, main, normalize_url


HOME = """<!doctype html><html><head><base href='/site/'>
<meta charset='windows-1252'><meta http-equiv='refresh' content='0;url=/bad'>
<link rel='stylesheet' href='../style.css?v=1'><link rel='preconnect' href='https://example.com'>
<style>.hero { background: url('../inline.png'); }</style>
<script src='/script.js'></script></head><body onload='alert(1)'>
<a href='../index.php?option=com_content&amp;view=article&amp;id=1#details'>One</a>
<a href='../index.php?option=com_content&amp;view=article&amp;id=2'>Two</a>
<a href='/redirect'>Redirect</a><a href='/missing'>Missing</a>
<a href='https://example.com/other'>External</a><a href='/blocked'>Blocked</a>
<a href='/activiteiten/activ/kalender/maandkalender/2030/1/-'>Calendar</a>
<a href='/manual.pdf?download=1'>PDF</a><a href='javascript:alert(1)'>Unsafe</a>
<img src='../photo.png' srcset='../photo.png 1x, ../large.png 2x'>
<img data-src='../lazy.png' src='../placeholder.png'>
<div style='background:url(../inline.png)'>inline</div>
<form action='/submit' method='post'><input name='test'><button>Send</button></form>
<iframe src='https://example.com/embed'></iframe>
<!--[if IE]><script src='/ie.js'></script><![endif]-->
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    hits = Counter()

    def log_message(self, *_):
        pass

    def do_GET(self):
        type(self).hits[self.path] += 1
        if self.path == "/robots.txt":
            self.send(200, "text/plain", b"User-agent: *\nDisallow: /blocked\n")
        elif self.path == "/":
            self.send(200, "text/html; charset=utf-8", HOME.encode())
        elif self.path.startswith("/index.php?"):
            self.send(200, "text/html", ("<html><head></head><body><p id='details'>"
                + self.path + "</p><a href='/'>Home</a></body></html>").encode())
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/index.php?option=com_content&view=article&id=1")
            self.end_headers()
        elif self.path == "/external-redirect":
            self.send_response(302)
            self.send_header("Location", "https://example.com/outside")
            self.end_headers()
        elif self.path == "/style.css?v=1":
            self.send(200, "text/css", b'@import "nested.css"; @media screen { .x { background: url("photo.png"); } }')
        elif self.path == "/nested.css":
            self.send(200, "text/css", b'@font-face { src: url("font.woff2?#iefix"); }')
        elif self.path == "/manual.pdf?download=1":
            self.send(200, "application/pdf", b"%PDF-1.4 fixture")
        elif self.path.endswith((".png", ".woff2")):
            self.send(200, "image/png" if self.path.endswith(".png") else "font/woff2", b"fixture-bytes")
        elif self.path == "/retry" and type(self).hits[self.path] == 1:
            self.send(503, "text/plain", b"try again")
        elif self.path in {"/retry", "/extra"}:
            self.send(200, "text/html", b"<html><head></head><body>Extra page</body></html>")
        elif self.path == "/large-file":
            self.send(200, "application/octet-stream", b"x" * 4096)
        else:
            self.send(404, "text/plain", b"not found")

    def send(self, status, mime, data):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join()

    def setUp(self):
        Handler.hits.clear()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "archive"

    def crawler(self, **options):
        values = dict(url=self.origin + "/", output=self.output, delay=0, retries=0, ignore_robots=False)
        values.update(options)
        return Crawler(Config(**values))

    def test_complete_archive_rewrites_pages_assets_and_css(self):
        crawler = self.crawler()
        report = crawler.crawl()
        self.assertEqual(report["summary"]["pages"], 3)
        self.assertIn(self.origin + "/missing", report["failures"])
        self.assertIn(self.origin + "/blocked", report["failures"])
        self.assertEqual(report["failure_reasons"], {"HTTP 404": 1, "robots.txt": 1})
        self.assertEqual(Handler.hits["/blocked"], 0)
        self.assertEqual(Handler.hits["/script.js"], 0)
        self.assertEqual(Handler.hits["/activiteiten/activ/kalender/maandkalender/2030/1/-"], 0)
        self.assertEqual(Handler.hits["/nested.css"], 1)
        self.assertEqual(Handler.hits["/font.woff2"], 1)
        soup = BeautifulSoup((self.output / "index.html").read_text("utf-8"), "html.parser")
        self.assertIsNone(soup.find("base"))
        self.assertIsNone(soup.find("script"))
        self.assertIsNone(soup.find("iframe"))
        self.assertIsNone(soup.find("form"))
        self.assertTrue(soup.find("input").has_attr("disabled"))
        self.assertIsNone(soup.body.get("onload"))
        self.assertEqual(soup.find("a", string="Unsafe")["href"], "#")
        self.assertEqual(soup.find("a", string="Missing")["href"], self.origin + "/missing")
        self.assertEqual(soup.find("a", string="External")["href"], "https://example.com/other")
        one = soup.find("a", string="One")["href"]
        two = soup.find("a", string="Two")["href"]
        self.assertNotEqual(one.split("#")[0], two)
        self.assertEqual(one, soup.find("a", string="Redirect")["href"] + "#details")
        self.assertEqual(soup.meta["charset"], "utf-8")
        self.assertEqual(soup.footer.get_text(), ARCHIVE_NOTICE)
        self.assertNotIn("ChatGPT", soup.footer.get_text())
        self.assertEqual(soup.body.find(True, recursive=False).get("data-archive-notice"), "top")
        self.assertEqual(soup.body.find_all(True, recursive=False)[-1].get("data-archive-notice"), "bottom")
        self.assertTrue((self.output / ".nojekyll").is_file())
        self.assertTrue(soup.find("img", attrs={"srcset": True})["srcset"].endswith("2x"))
        self.assertIn("assets/", soup.find("img")["src"])
        # Every rewritten local reference resolves to a saved file, including CSS.
        for record in report["files"].values():
            path = self.output / record["path"]
            references = []
            if record["content_type"] == "text/html":
                document = BeautifulSoup(path.read_text("utf-8"), "html.parser")
                references += [tag[attr] for tag in document.find_all(True) for attr in ("href", "src") if tag.has_attr(attr)]
            elif record["content_type"] == "text/css":
                css_urls(path.read_text("utf-8"), lambda url: references.append(url) or url)
            for reference in references:
                parts = urlsplit(reference)
                if not parts.scheme and parts.path:
                    self.assertTrue((path.parent / unquote(parts.path)).is_file(), reference)
        self.assertTrue(json.loads((self.output / "archive-report.json").read_text("utf-8"))["files"])

    def test_resume_uses_cache_without_any_network(self):
        config = dict(url=self.origin + "/extra")
        self.crawler(**config).crawl()
        hits_before = Handler.hits.copy()
        crawler = self.crawler(**config)
        with patch.object(crawler.session, "get", side_effect=AssertionError("Cache must avoid network")):
            report = crawler.crawl()
        self.assertEqual(report["summary"]["pages"], 1)
        self.assertEqual(Handler.hits, hits_before)

    def test_notice_options_render_all_combinations_offline(self):
        from archive_crawler import parser
        self.crawler(url=self.origin + "/extra").crawl()
        for top in (False, True):
            for bottom in (False, True):
                with self.subTest(top=top, bottom=bottom):
                    flags = ["--notice-top" if top else "--no-notice-top",
                             "--notice-bottom" if bottom else "--no-notice-bottom"]
                    parsed = parser().parse_args(flags)
                    self.assertEqual((parsed.notice_top, parsed.notice_bottom), (top, bottom))
                    crawler = self.crawler(url=self.origin + "/extra", notice_top=top, notice_bottom=bottom)
                    with patch.object(crawler.session, "get", side_effect=AssertionError("Rendering must stay offline")):
                        report = crawler.render_existing()
                    soup = BeautifulSoup((self.output / "index.html").read_text("utf-8"), "html.parser")
                    notices = soup.select(".static-archive-notice")
                    self.assertEqual(len(notices), int(top) + int(bottom))
                    self.assertEqual({n["data-archive-notice"] for n in notices},
                                     ({"top"} if top else set()) | ({"bottom"} if bottom else set()))
                    self.assertTrue(all(n.get_text() == ARCHIVE_NOTICE for n in notices))
                    self.assertEqual(report["notices"], {"top": top, "bottom": bottom})

    def test_page_limit_still_downloads_assets_and_can_resume(self):
        report = self.crawler(max_pages=1).crawl()
        self.assertEqual(report["summary"]["pages"], 1)
        self.assertIn("page limit reached", report["skip_reasons"])
        self.assertEqual(Handler.hits["/photo.png"], 1)
        self.assertEqual(Handler.hits["/manual.pdf?download=1"], 1)
        report = self.crawler(max_pages=10).crawl()
        self.assertEqual(report["summary"]["pages"], 3)
        self.assertEqual(Handler.hits["/photo.png"], 1)

    def test_extra_seed_discovered(self):
        report = self.crawler(url=self.origin + "/extra", seeds=[self.origin + "/retry"]).crawl()
        self.assertIn(self.origin + "/retry", report["failures"])

    def test_owner_authorized_default_skips_robots_check(self):
        self.assertTrue(Config().ignore_robots)
        crawler = self.crawler(url=self.origin + "/extra", ignore_robots=True)
        with patch.object(crawler, "request", side_effect=AssertionError("robots.txt must not be fetched")):
            self.assertTrue(crawler.robots_allowed(self.origin + "/blocked"))
        report = crawler.crawl()
        self.assertEqual(Handler.hits["/robots.txt"], 0)
        self.assertEqual(report["summary"]["pages"], 1)

    def test_respect_robots_option_checks_rules(self):
        from archive_crawler import parser
        self.assertTrue(parser().parse_args([]).ignore_robots)
        self.assertTrue(parser().parse_args(["--ignore-robots"]).ignore_robots)
        self.assertFalse(parser().parse_args(["--respect-robots"]).ignore_robots)
        crawler = self.crawler()
        self.assertFalse(crawler.robots_allowed(self.origin + "/blocked"))
        self.assertEqual(Handler.hits["/robots.txt"], 1)
        crawler.session.close()

    def test_transient_server_error_is_retried(self):
        with patch("archive_crawler.time.sleep"):
            report = self.crawler(url=self.origin + "/retry", retries=1).crawl()
        self.assertEqual(report["summary"]["pages"], 1)
        self.assertEqual(Handler.hits["/retry"], 2)

    def test_external_page_redirect_is_not_followed(self):
        crawler = self.crawler(url=self.origin + "/external-redirect")
        original = crawler.session.get

        def only_local(url, **kwargs):
            self.assertTrue(url.startswith(self.origin + "/"), url)
            return original(url, **kwargs)

        with patch.object(crawler.session, "get", side_effect=only_local):
            report = crawler.crawl()
        self.assertEqual(report["summary"]["pages"], 0)
        self.assertIn("outside", report["failures"][self.origin + "/external-redirect"])

    def test_max_file_size_is_enforced(self):
        report = self.crawler(url=self.origin + "/large-file", max_size_mb=0.001).crawl()
        self.assertIn("exceeds", report["failures"][self.origin + "/large-file"])

    def test_refresh_refetches_successful_pages(self):
        self.crawler(url=self.origin + "/extra").crawl()
        self.crawler(url=self.origin + "/extra", refresh=True).crawl()
        self.assertEqual(Handler.hits["/extra"], 2)

    def test_excluded_assets_stay_absolute(self):
        report = self.crawler(exclude=[r"photo\.png"]).crawl()
        self.assertIn(self.origin + "/photo.png", report["skipped"])
        self.assertEqual(Handler.hits["/photo.png"], 0)
        soup = BeautifulSoup((self.output / "index.html").read_text("utf-8"), "html.parser")
        self.assertEqual(soup.find("img")["src"], self.origin + "/photo.png")

    def test_command_returns_failure_for_missing_start_page(self):
        self.assertEqual(main([self.origin + "/missing", "--output", str(self.output), "--delay", "0", "--retries", "0"]), 1)


class URLTests(unittest.TestCase):
    def test_queries_are_preserved_and_fragments_removed(self):
        self.assertEqual(normalize_url("../index.php?id=2&start=10&utm_source=test#x", "https://EXAMPLE.com/a/b/"),
                         "https://example.com/a/index.php?id=2&start=10")
        self.assertIsNone(normalize_url("javascript:alert(1)"))
        self.assertIsNone(normalize_url("https://user:password@example.com/"))
        self.assertIsNone(normalize_url("https://example.com:bad/"))

    def test_css_import_nested_urls_and_data_urls(self):
        discovered = []
        css_urls('@import "a.css"; @supports (display:grid) {a{background:url("a\\20 b.png")}}',
                 lambda value: discovered.append(value) or value)
        self.assertEqual(discovered, ["a.css", "a b.png"])


if __name__ == "__main__":
    unittest.main()
