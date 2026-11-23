"""Public Joomla to static HTML archiver. Created with ChatGPT Codex.

The fetch/discovery pass precedes rewriting, so only successful downloads get
local links. Original response bodies are cached separately for resumable runs.
"""

from __future__ import annotations

import argparse
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import mimetypes
from pathlib import Path
import posixpath
import re
import time
from urllib.parse import parse_qsl, quote, unquote, urldefrag, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup, Comment
import requests
import tinycss2

DEFAULT_URL = "https://archief.boomgaardenstichting.be/"
USER_AGENT = "NBSStaticArchive/1.0 (website preservation; made with ChatGPT Codex)"
DISCLAIMER = "This static archive was made with ChatGPT Codex."
ARCHIVE_NOTICE = "Dit is een archiefkopie van deze website. Sommige functies en links werken mogelijk niet meer."
LOG = logging.getLogger("archive")
HTML_TYPES = {"text/html", "application/xhtml+xml"}
DOWNLOAD_SUFFIXES = {
    ".pdf", ".zip", ".gz", ".doc", ".docx", ".xls", ".xlsx", ".csv",
    ".ppt", ".pptx", ".odt", ".ods", ".jpg", ".jpeg", ".png", ".gif",
    ".webp", ".svg", ".ico", ".mp3", ".mp4", ".ogg", ".wav", ".webm",
    ".woff", ".woff2", ".ttf", ".eot", ".css", ".txt", ".xml",
}
# JEvents can generate previous/next dates forever, even for empty months.
CALENDAR_RE = re.compile(
    r"/(?:maandkalender|evenementenper(?:jaar|maand|week|dag)|"
    r"eventsby(?:year|month|week|day)|month\.calendar|year\.listevents|"
    r"week\.listevents|day\.listevents|search_form)(?:/|$)", re.I
)
CSS_ONLY_MENU = """
/* Static archive: keep the original menu usable without JavaScript. */
@media screen and (max-width: 1050px) {
  #navigation-toggle, #sidr-close { display: none !important; }
  #gruemenu { display: block !important; }
  #gruemenu > ul > li { float: none !important; display: block !important; }
  #gruemenu ul ul { position: static !important; display: block !important;
    padding-left: 1rem !important; width: auto !important; }
}
#gruemenu li:focus-within > ul { left: 0; }
.static-archive-notice { clear: both; padding: 1rem; margin: 1rem;
  background: #fffbe6; border: 1px solid #bbb; color: #222;
  font: 14px/1.5 sans-serif; }
"""


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def write_json(path: Path, value: object) -> None:
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def digest(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def normalize_url(value: str, base: str = "") -> str | None:
    """Keep meaningful queries; drop fragments and marketing/session parameters."""
    try:
        parts = urlsplit(urljoin(base, value.strip()))
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return None
        if parts.username or parts.password:
            return None
        hostname = parts.hostname.lower().encode("idna").decode("ascii")
        if ":" in hostname:
            hostname = f"[{hostname}]"
        port = parts.port
        scheme = parts.scheme.lower()
        # The old template uses HTTP for Google Fonts, which rejects HTTP now.
        if hostname in {"fonts.googleapis.com", "fonts.gstatic.com"}:
            scheme = "https"
        netloc = hostname
        if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
            netloc += f":{port}"
        path = posixpath.normpath(parts.path or "/")
        if not path.startswith("/"):
            path = "/" + path
        if parts.path.endswith("/") and not path.endswith("/"):
            path += "/"
        path = quote(path, safe="/%:@!$&'()*+,;=-._~")
        # Preserve query encoding/order (some download endpoints require it).
        query = "&".join(
            item for item in parts.query.split("&") if item and not
            re.match(r"(?:utm_[^=]*|fbclid|gclid|phpsessid)=", item, re.I)
        )
        return urlunsplit((scheme, netloc, path, query, ""))
    except (ValueError, UnicodeError):
        return None


def css_urls(source: str, replace) -> str:
    """Rewrite CSS url() and @import, including nested rules and escaped URLs."""
    tokens = tinycss2.parse_component_value_list(source)

    def string_value(value: str) -> str:
        # JSON string escaping is compatible for the URL characters used here.
        return json.dumps(value, ensure_ascii=False)

    def walk(items):
        import_pending = False
        for token in items:
            if token.type == "at-keyword":
                import_pending = token.lower_value == "import"
            elif token.type == "url":
                token.value = replace(token.value)
                token.representation = "url(" + string_value(token.value) + ")"
                import_pending = False
            elif token.type == "string" and import_pending:
                token.value = replace(token.value)
                token.representation = string_value(token.value)
                import_pending = False
            elif token.type == "function":
                if token.lower_name == "url":
                    significant = [t for t in token.arguments if t.type not in {"whitespace", "comment"}]
                    if len(significant) == 1 and significant[0].type == "string":
                        value = replace(significant[0].value)
                        token.arguments = tinycss2.parse_component_value_list(string_value(value))
                else:
                    walk(token.arguments)
                import_pending = False
            elif hasattr(token, "content"):
                walk(token.content)
                import_pending = False
            elif token.type not in {"whitespace", "comment"}:
                import_pending = False

    walk(tokens)
    return tinycss2.serialize(tokens)


def srcset_urls(source: str, replace) -> str:
    """Read srcset candidates without splitting commas inside data URLs."""
    candidates = []
    position = 0
    while position < len(source):
        while position < len(source) and (source[position].isspace() or source[position] == ","):
            position += 1
        start = position
        while position < len(source) and not source[position].isspace():
            position += 1
        url = source[start:position]
        if not url:
            break
        if url.endswith(","):
            candidates.append(replace(url.rstrip(",")))
            continue
        start = position
        while position < len(source) and source[position] != ",":
            position += 1
        descriptor = source[start:position].strip()
        candidates.append(replace(url) + (" " + descriptor if descriptor else ""))
        position += 1
    return ", ".join(candidates)


@dataclass
class Config:
    url: str = DEFAULT_URL
    output: Path = Path("archive")
    max_pages: int = 2000
    max_resources: int = 20000
    delay: float = 0.25
    timeout: float = 30
    retries: int = 2
    max_size_mb: float = 100
    include_hosts: list[str] = field(default_factory=list)
    seeds: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    allow_calendar: bool = False
    external_assets: bool = True
    ignore_robots: bool = True
    refresh: bool = False
    notice_top: bool = True
    notice_bottom: bool = True


class Crawler:
    def __init__(self, config: Config):
        self.config = config
        self.start = normalize_url(config.url)
        if not self.start:
            raise ValueError("The starting URL must be an absolute HTTP(S) URL without credentials.")
        self.output = config.output.resolve()
        self.cache = self.output.with_name("." + self.output.name + "-cache")
        self.hosts = {urlsplit(self.start).netloc.lower(), *[h.lower() for h in config.include_hosts]}
        self.exclude = [re.compile(pattern) for pattern in config.exclude]
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.queue: deque[tuple[str, str]] = deque()
        self.scheduled: set[str] = set()
        self.records: dict[str, dict] = {}
        self.aliases: dict[str, str] = {}
        self.failures: dict[str, str] = {}
        self.skipped: dict[str, str] = {}
        self.robots: dict[str, RobotFileParser] = {}
        self.page_count = 0
        self.interrupted = False
        self.last_request = 0.0
        self.started = datetime.now(timezone.utc).isoformat()

    def in_scope(self, url: str) -> bool:
        return urlsplit(url).netloc.lower() in self.hosts

    def page_skip_reason(self, url: str) -> str | None:
        parts = urlsplit(url)
        path = unquote(parts.path).lower()
        query = dict(parse_qsl(parts.query))
        if re.search(r"/(?:administrator|api|login|logout|registration|search)(?:/|$)", path):
            return "interactive or administrative endpoint"
        if query.get("option") in {"com_users", "com_search", "com_finder"}:
            return "interactive endpoint"
        if query.get("task") in {"user.login", "user.logout", "search"}:
            return "interactive endpoint"
        if query.get("print") == "1" or query.get("format") in {"feed", "opensearch", "raw"}:
            return "duplicate print view or service endpoint"
        if not self.config.allow_calendar:
            if CALENDAR_RE.search(path) or query.get("task", "").lower() in {
                "month.calendar", "year.listevents", "week.listevents", "day.listevents",
                "search.form", "search.listevents",
            }:
                return "unbounded calendar navigation (use --allow-calendar and seeds if needed)"
        return None

    def enqueue(self, value: str, base: str, kind: str = "page") -> None:
        if value.strip().startswith("#") or not value.strip():
            return
        url = normalize_url(value, base)
        if not url or url in self.scheduled:
            return
        if kind == "page" and not self.in_scope(url):
            return
        if kind == "asset" and not self.in_scope(url) and not self.config.external_assets:
            self.skipped[url] = "external assets disabled"
            return
        if any(pattern.search(url) for pattern in self.exclude):
            self.skipped[url] = "excluded by --exclude"
            return
        if kind == "page":
            reason = self.page_skip_reason(url)
            if reason:
                self.skipped[url] = reason
                return
        if len(self.scheduled) >= self.config.max_resources:
            self.skipped[url] = "resource limit reached"
            return
        self.scheduled.add(url)
        self.queue.append((url, kind))

    def request(self, url: str) -> requests.Response:
        """GET without automatic redirects, throttled and with bounded retries."""
        for attempt in range(self.config.retries + 1):
            time.sleep(max(0, self.config.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                response = self.session.get(url, timeout=self.config.timeout, stream=True, allow_redirects=False)
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                return response
            except requests.RequestException as error:
                response = getattr(error, "response", None)
                if response is not None:
                    response.close()
                if attempt == self.config.retries:
                    raise
                time.sleep(min(2 ** attempt, 8))
        raise RuntimeError("unreachable")

    def robots_allowed(self, url: str) -> bool:
        if self.config.ignore_robots:
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            parser = RobotFileParser(origin + "/robots.txt")
            try:
                current = parser.url
                for _ in range(6):
                    with self.request(current) as response:
                        if response.is_redirect or response.is_permanent_redirect:
                            target = normalize_url(response.headers.get("Location", ""), current)
                            if not target or urlsplit(target).netloc != parts.netloc:
                                raise ValueError("robots.txt redirects outside its host")
                            current = target
                            continue
                        if response.status_code in {404, 410}:
                            parser.parse([])
                        elif response.status_code >= 400:
                            parser.parse(["User-agent: *", "Disallow: /"])
                            LOG.warning("robots.txt returned HTTP %s for %s", response.status_code, origin)
                        else:
                            parser.parse(self.read_body(response).decode("utf-8", "replace").splitlines())
                        break
                else:
                    raise ValueError("Too many robots.txt redirects")
            except (requests.RequestException, ValueError) as error:
                LOG.warning("Cannot check robots.txt for %s: %s", origin, error)
                parser.parse(["User-agent: *", "Disallow: /"])
            self.robots[origin] = parser
        parser = self.robots[origin]
        interval = parser.crawl_delay(USER_AGENT) or parser.crawl_delay("*") or 0
        if interval:
            time.sleep(max(0, interval - (time.monotonic() - self.last_request)))
        return parser.can_fetch(USER_AGENT, url)

    def read_body(self, response: requests.Response) -> bytes:
        maximum = int(self.config.max_size_mb * 1024 * 1024)
        data = bytearray()
        for chunk in response.iter_content(64 * 1024):
            data.extend(chunk)
            if len(data) > maximum:
                raise ValueError(f"File exceeds {self.config.max_size_mb:g} MB limit")
        return bytes(data)

    def fetch(self, url: str, kind: str) -> tuple[dict, bytes]:
        key = digest(url)
        metadata = self.cache / (key + ".json")
        body_path = self.cache / (key + ".bin")
        if not self.config.refresh and metadata.is_file() and body_path.is_file():
            try:
                record = json.loads(metadata.read_text("utf-8"))
                body = body_path.read_bytes()
                if (record["url"] == url and digest(record["url"]) == key
                        and record["body_sha256"] == hashlib.sha256(body).hexdigest()):
                    # Options/scope can change between runs; validate again.
                    if kind == "page" and not self.in_scope(record["final_url"]):
                        raise ValueError("Cached page redirects outside the included hosts")
                    return record, body
            except (KeyError, ValueError, OSError):
                LOG.warning("Ignoring invalid cache entry for %s", url)
        current = url
        chain = []
        for _ in range(11):
            if kind == "page" and not self.in_scope(current):
                raise ValueError("Page redirects outside the included hosts")
            if kind == "asset" and not self.config.external_assets and not self.in_scope(current):
                raise ValueError("Asset redirects outside the included hosts")
            if any(pattern.search(current) for pattern in self.exclude):
                raise ValueError("Redirect target excluded by --exclude")
            if kind == "page" and self.page_skip_reason(current):
                raise ValueError("Redirect target is an excluded interactive/calendar endpoint")
            if not self.robots_allowed(current):
                raise ValueError("Disallowed by robots.txt (or robots.txt unavailable)")
            chain.append(current)
            with self.request(current) as response:
                if response.is_redirect or response.is_permanent_redirect:
                    target = normalize_url(response.headers.get("Location", ""), current)
                    if not target or target in chain:
                        raise ValueError("Invalid or looping redirect")
                    current = target
                    continue
                response.raise_for_status()
                body = self.read_body(response)
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
                content_type = content_type or mimetypes.guess_type(current)[0] or "application/octet-stream"
                # Detect mislabeled Joomla error/HTML pages, including extensionless PHP.
                if body.lstrip()[:100].lower().startswith((b"<!doctype html", b"<html")):
                    content_type = "text/html"
                record = {
                    "url": url, "final_url": current, "redirects": chain,
                    "content_type": content_type,
                    "encoding": response.encoding if "charset=" in response.headers.get("Content-Type", "").lower() else None,
                    "size": len(body), "body_sha256": hashlib.sha256(body).hexdigest(),
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }
                atomic_write(body_path, body)
                write_json(metadata, record)
                return record, body
        raise ValueError("Too many redirects")

    def local_path(self, url: str, content_type: str) -> str:
        if url == self.start:
            return "index.html"
        name = unquote(urlsplit(url).path).rstrip("/").rsplit("/", 1)[-1] or "page"
        suffix = posixpath.splitext(name)[1].lower()
        if content_type in HTML_TYPES:
            suffix = ".html"
        elif content_type == "text/css":
            suffix = ".css"
        elif not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) or suffix in {".php", ".asp", ".aspx"}:
            suffix = mimetypes.guess_extension(content_type) or ".bin"
        stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", posixpath.splitext(name)[0]).strip("-")[:65] or "file"
        folder = "pages" if content_type in HTML_TYPES else "assets"
        return f"{folder}/{stem}--{digest(url)[:20]}{suffix}"

    def text_body(self, record: dict, body: bytes) -> str:
        encoding = record.get("encoding")
        if not encoding and record["content_type"] == "text/css":
            match = re.match(br'@charset\s+["\']([^"\']+)["\']', body)
            if match:
                encoding = match[1].decode("ascii", "replace")
        try:
            return body.decode(encoding or "utf-8-sig")
        except (LookupError, UnicodeError):
            return body.decode("windows-1252", "replace")

    def reference(self, value: str, base: str, local: str, discover: bool, kind: str) -> str:
        value = value.strip()
        if not value or value.startswith("#"):
            return value
        if value.lower().startswith(("javascript:", "vbscript:")):
            return "#"
        if discover:
            self.enqueue(value, base, kind)
            return value
        absolute = urljoin(base, value)
        target = normalize_url(absolute)
        if not target:
            return value
        record = self.records.get(self.aliases.get(target, target))
        if record:
            relative = posixpath.relpath(record["path"], posixpath.dirname(local) or ".")
            fragment = urldefrag(absolute)[1]
            return relative + ("#" + fragment if fragment else "")
        return absolute

    def html(self, record: dict, body: bytes, discover: bool) -> str:
        soup = BeautifulSoup(body, "html.parser", from_encoding=record.get("encoding"))
        base = record["final_url"]
        base_tag = soup.find("base", href=True)
        if base_tag:
            base = urljoin(base, base_tag["href"])
        for tag in soup.find_all("base"):
            tag.decompose()
        local = record["path"]

        def ref(value, kind="asset"):
            return self.reference(str(value), base, local, discover, kind)

        # Scripts, refreshes, plugins and frames cannot be part of an HTML/CSS archive.
        for tag in list(soup.find_all(["script", "iframe", "frame", "frameset", "embed", "object", "applet"])):
            if tag.parent is None:
                continue
            source = tag.get("src") or tag.get("data")
            if source and tag.name in {"iframe", "embed", "object"}:
                absolute = normalize_url(str(source), base)
                if absolute:
                    link = soup.new_tag("a", href=ref(absolute, "page"))
                    link.string = "Embedded content (original source)"
                    tag.replace_with(link)
                    continue
            tag.decompose()
        for tag in list(soup.find_all("meta")):
            if tag.get("http-equiv", "").lower() in {"refresh", "content-security-policy", "content-type"}:
                tag.decompose()
        for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
            # Old IE conditional comments may contain active script or remote assets.
            if "<script" in str(comment).lower() or "[if" in str(comment).lower():
                comment.extract()
        for tag in list(soup.find_all("link")):
            rel = {str(value).lower() for value in tag.get("rel", [])}
            if not rel.intersection({"stylesheet", "icon", "apple-touch-icon", "mask-icon"}):
                tag.decompose()

        for form in soup.find_all("form"):
            form.name = "div"
            for attribute in ("action", "method", "onsubmit", "target", "enctype"):
                form.attrs.pop(attribute, None)
            notice = soup.new_tag("p")
            notice.string = "Static archive: this form is unavailable. / Statisch archief: dit formulier is niet beschikbaar."
            form.insert(0, notice)
            for control in form.find_all(["input", "button", "select", "textarea"]):
                control["disabled"] = "disabled"

        for tag in soup.find_all(True):
            for attribute in list(tag.attrs):
                if attribute.lower().startswith("on") or attribute.lower() in {"integrity", "crossorigin", "nonce", "srcdoc", "formaction", "ping", "manifest"}:
                    del tag[attribute]
            if tag.has_attr("href"):
                kind = "page" if tag.name in {"a", "area"} else "asset"
                tag["href"] = ref(tag["href"], kind)
            for attribute in ("src", "poster", "background", "xlink:href"):
                if tag.has_attr(attribute):
                    tag[attribute] = ref(tag[attribute])
            for attribute in ("data-src", "data-original", "data-lazy-src"):
                if tag.get(attribute) and tag.name in {"img", "source"}:
                    tag["src"] = ref(tag[attribute])
                    del tag[attribute]
            for attribute in ("srcset", "data-srcset"):
                if tag.has_attr(attribute):
                    tag["srcset"] = srcset_urls(tag[attribute], ref)
                    if attribute != "srcset":
                        del tag[attribute]
            if tag.has_attr("style"):
                tag["style"] = css_urls(tag["style"], ref)
            if tag.name == "style" and tag.string:
                tag.string.replace_with(css_urls(str(tag.string), ref))
            if tag.get("target") == "_blank":
                tag["rel"] = "noopener noreferrer"

        if not discover:
            for tag in soup.find_all("meta", charset=True):
                tag.decompose()
            if soup.head:
                soup.head.insert(0, soup.new_tag("meta", charset="utf-8"))
                style = soup.new_tag("style")
                style.string = CSS_ONLY_MENU
                soup.head.append(style)
            if soup.body:
                if self.config.notice_top:
                    notice = soup.new_tag("aside", attrs={"class": "static-archive-notice", "data-archive-notice": "top"})
                    notice.string = ARCHIVE_NOTICE
                    soup.body.insert(0, notice)
                if self.config.notice_bottom:
                    notice = soup.new_tag("footer", attrs={"class": "static-archive-notice", "data-archive-notice": "bottom"})
                    notice.string = ARCHIVE_NOTICE
                    soup.body.append(notice)
        return str(soup)

    def crawl(self) -> dict:
        self.output.mkdir(parents=True, exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.enqueue(self.start, self.start)
        for seed in self.config.seeds:
            self.enqueue(seed, self.start)
        try:
            while self.queue:
                url, kind = self.queue.popleft()
                if url in self.aliases:
                    continue
                is_download = posixpath.splitext(urlsplit(url).path)[1].lower() in DOWNLOAD_SUFFIXES
                if kind == "page" and not is_download and self.page_count >= self.config.max_pages:
                    self.skipped[url] = "page limit reached"
                    continue
                LOG.info("[%s pages, %s files, %s pending] %s", self.page_count, len(self.records), len(self.queue), url)
                try:
                    record, body = self.fetch(url, kind)
                    content_type = record["content_type"]
                    if content_type in {"application/javascript", "text/javascript", "application/x-javascript"} or urlsplit(record["final_url"]).path.lower().endswith((".js", ".mjs")):
                        self.skipped[url] = "JavaScript omitted from static archive"
                        continue
                    if content_type in HTML_TYPES:
                        if not self.in_scope(record["final_url"]):
                            raise ValueError("External asset returned HTML; external pages are not archived")
                        if self.page_count >= self.config.max_pages:
                            self.skipped[url] = "page limit reached"
                            continue
                        self.page_count += 1
                    canonical = self.aliases.get(record["final_url"])
                    if canonical:
                        self.aliases[url] = canonical
                        if content_type in HTML_TYPES:
                            self.page_count -= 1
                        continue
                    record["path"] = self.local_path(url, content_type)
                    self.records[url] = record
                    for alias in record["redirects"]:
                        self.aliases.setdefault(alias, url)
                    if content_type in HTML_TYPES:
                        self.html(record, body, discover=True)
                    elif content_type == "text/css":
                        css_urls(self.text_body(record, body), lambda value: self.reference(value, record["final_url"], record["path"], True, "asset"))
                except (requests.RequestException, ValueError, OSError) as error:
                    self.failures[url] = str(error)
                    LOG.warning("Could not save %s: %s", url, error)
        except KeyboardInterrupt:
            self.interrupted = True
            LOG.warning("Interrupted; writing the pages and assets downloaded so far.")
        finally:
            self.session.close()
        return self.export()

    def write_files(self) -> None:
        for url, record in self.records.items():
            body = (self.cache / (digest(url) + ".bin")).read_bytes()
            content_type = record["content_type"]
            if content_type in HTML_TYPES:
                body = self.html(record, body, discover=False).encode("utf-8")
            elif content_type == "text/css":
                text = css_urls(self.text_body(record, body), lambda value: self.reference(value, record["final_url"], record["path"], False, "asset"))
                text = re.sub(r'^\ufeff?\s*@charset\s+[^;]+;', '', text, count=1, flags=re.I)
                body = ('@charset "UTF-8";\n' + text).encode("utf-8")
            atomic_write(self.output / record["path"], body)
        atomic_write(self.output / ".nojekyll", b"")

    def render_existing(self) -> dict:
        """Apply presentation settings to the existing archive without network access."""
        self.session.close()
        report_path = self.output / "archive-report.json"
        report = json.loads(report_path.read_text("utf-8"))
        if normalize_url(report.get("source", "")) != self.start:
            raise ValueError("The archive source differs from the starting URL; pass its original URL.")
        self.records = report["files"]
        self.aliases = report["aliases"]
        # Validate before overwriting any output; the raw cache is required.
        for url, record in self.records.items():
            if record["path"] != self.local_path(url, record["content_type"]):
                raise ValueError(f"Unexpected local path in archive report: {record['path']}")
            if not (self.cache / (digest(url) + ".bin")).is_file():
                raise ValueError(f"Cached download missing for {url}; rerun the crawler first.")
        self.write_files()
        report["notices"] = {"top": self.config.notice_top, "bottom": self.config.notice_bottom}
        report["rendered_at"] = datetime.now(timezone.utc).isoformat()
        write_json(report_path, report)
        LOG.info("Rendered %s saved files without downloading. Notices: top=%s, bottom=%s.",
                 len(self.records), self.config.notice_top, self.config.notice_bottom)
        return report

    def export(self) -> dict:
        self.write_files()
        report = {
            "source": self.start, "started_at": self.started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "disclaimer": DISCLAIMER, "interrupted": self.interrupted,
            "notices": {"top": self.config.notice_top, "bottom": self.config.notice_bottom},
            "summary": {"pages": self.page_count, "files": len(self.records),
                        "failed": len(self.failures), "skipped": len(self.skipped),
                        "pending": len(self.queue)},
            "files": self.records, "aliases": self.aliases,
            "failures": self.failures, "skipped": self.skipped,
            "pending": [url for url, _ in self.queue],
            "skip_reasons": dict(Counter(self.skipped.values())),
            "failure_reasons": dict(Counter(
                "HTTP " + match.group(1) if (match := re.search(r"\b([45]\d{2}) (?:Client|Server) Error", error))
                else "robots.txt" if "robots.txt" in error else "other"
                for error in self.failures.values()
            )),
        }
        write_json(self.output / "archive-report.json", report)
        LOG.info("Saved %s pages and %s total files to %s", self.page_count, len(self.records), self.output)
        LOG.info("%s failed, %s skipped. See archive-report.json.", len(self.failures), len(self.skipped))
        if report["failure_reasons"]:
            LOG.warning("Failure breakdown: %s", "; ".join(
                f"{reason}: {count}" for reason, count in report["failure_reasons"].items()
            ))
        if "HTTP 404" in report["failure_reasons"]:
            LOG.warning("HTTP 404 means the source server reports a missing URL; ignoring robots.txt does not fix it.")
        return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    result.add_argument("url", nargs="?", default=DEFAULT_URL, help="Starting URL")
    result.add_argument("--output", type=Path, default=Path("archive"), help="Static output directory; use a dedicated folder")
    result.add_argument("--max-pages", type=int, default=2000, help="Maximum HTML pages; assets/downloads have a separate resource cap")
    result.add_argument("--max-resources", type=int, default=20000, help="Maximum queued pages and assets combined")
    result.add_argument("--delay", type=float, default=0.25, help="Minimum seconds between requests")
    result.add_argument("--timeout", type=float, default=30, help="Connection/read timeout in seconds")
    result.add_argument("--retries", type=int, default=2, help="Retries for connection errors, HTTP 429 and 5xx")
    result.add_argument("--max-size-mb", type=float, default=100, help="Maximum decoded size per file in MB")
    result.add_argument("--include-host", action="append", default=[], dest="include_hosts", help="Additional page host (hostname[:port]); repeatable")
    result.add_argument("--seed-url", action="append", default=[], dest="seeds", help="Extra page URL to discover, e.g. an older calendar year; repeatable")
    result.add_argument("--seed-file", type=Path, help="UTF-8 file with one extra page URL per line (# comments allowed)")
    result.add_argument("--exclude", action="append", default=[], help="Exclude URL matching this Python regex; repeatable")
    result.add_argument("--allow-calendar", action="store_true", help="Follow calendar date navigation; use bounded --exclude rules and page limits")
    result.add_argument("--no-external-assets", action="store_false", dest="external_assets", help="Leave external fonts/images/stylesheets online")
    robots = result.add_mutually_exclusive_group()
    robots.add_argument("--ignore-robots", action="store_true", dest="ignore_robots", help="Ignore robots.txt (default for this owner-authorized archive)")
    robots.add_argument("--respect-robots", action="store_false", dest="ignore_robots", help="Check robots.txt before downloading")
    result.set_defaults(ignore_robots=True)
    result.add_argument("--refresh", action="store_true", help="Fetch again instead of reusing successfully cached responses")
    result.add_argument("--notice-top", action=argparse.BooleanOptionalAction, default=True, help="Show the public archive notice at the top of each page")
    result.add_argument("--notice-bottom", action=argparse.BooleanOptionalAction, default=True, help="Show the public archive notice at the bottom of each page")
    result.add_argument("--render-only", action="store_true", help="Regenerate the existing archive from its report and raw cache, without downloading")
    result.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser()
    options = vars(arguments.parse_args(argv))
    logging.basicConfig(level=logging.DEBUG if options.pop("verbose") else logging.INFO, format="%(levelname)s %(message)s")
    seed_file = options.pop("seed_file")
    render_only = options.pop("render_only")
    if seed_file:
        try:
            options["seeds"].extend(line.strip() for line in seed_file.read_text("utf-8-sig").splitlines() if line.strip() and not line.lstrip().startswith("#"))
        except OSError as error:
            arguments.error(str(error))
    config = Config(**options)
    if (config.max_pages < 1 or config.max_resources < 1 or config.delay < 0
            or config.timeout <= 0 or config.retries < 0 or config.max_size_mb <= 0):
        arguments.error("Limits/timeouts must be positive; delay/retries may be zero.")
    try:
        crawler = Crawler(config)
        if render_only:
            crawler.render_existing()
            return 0
        report = crawler.crawl()
    except (ValueError, OSError, re.error) as error:
        LOG.error("%s", error)
        return 1
    summary = report["summary"]
    if report["interrupted"]:
        return 130
    if summary["pages"] == 0 or summary["failed"] or any("limit reached" in reason for reason in report["skip_reasons"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
