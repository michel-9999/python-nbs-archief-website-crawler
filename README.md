# Nationale Boomgaardenstichting static archive crawler

A Python program to preserve the public pages of the old Joomla website at
<https://archief.boomgaardenstichting.be/>. The resulting
folder can be hosted by a normal static web server: no PHP, Joomla, database or
JavaScript runtime is needed.

**Disclaimer: this project was made with ChatGPT Codex.** Review the generated
archive and its download report before publishing. This attribution is kept in
the project documentation. Each archived HTML page shows this public notice at
the top and bottom by default:

> Dit is een archiefkopie van deze website. Sommige functies en links werken mogelijk niet meer.

The two positions can be enabled or disabled independently; see below.

## Quick start (Windows / PowerShell)

Install Python **3.10 or newer**, download or clone this repository, and open a
terminal in its folder. There is no need to activate the virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe archive_crawler.py
```

The default source is the old NBS website. Keep the host online until the crawl
has finished and the report has been reviewed. A large website can take a while:
the crawler downloads sequentially with a 0.25-second pause between requests.

On macOS / Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python archive_crawler.py
```

After installation, `nbs-archive` is also available inside the virtual environment.
Run `python archive_crawler.py --help` for all options (using your venv's Python).

## Preview the saved website

```powershell
.\.venv\Scripts\python.exe -m http.server 8000 --bind 127.0.0.1 --directory archive
```

Open <http://localhost:8000/>. Stop the preview with **Ctrl+C**.
Ordinary saved pages also work by opening `archive/index.html` directly, though
a local web server gives a more representative preview of fonts and downloads.

Check the desktop and mobile menus, a few articles, images and PDFs. Original
search, login, orders, contact forms, live calendars and social widgets cannot
operate on a static host. Forms are visibly disabled, and embedded frames are
replaced with links to their original sources. A CSS fallback keeps this site's
mobile menu visible after the Joomla scripts are removed.

## What is saved

- Public HTML pages reached through links on the starting host, including Joomla
  `index.php?...` articles and pagination. Different query strings get different
  local filenames, so one article does not overwrite another.
- Images, CSS, CSS `url(...)` resources and `@import` dependencies, web fonts,
  responsive image candidates, inline styles and common lazy-loaded images.
- Linked documents such as PDFs, ZIP files and office documents on included hosts.
- External assets directly referenced by the pages/CSS, such as Google Fonts.
  Ordinary links to other websites stay external; those websites are not crawled.
- Relative links between successful downloads, usable at a domain root or under
  a subdirectory such as a GitHub Pages project URL.

The crawler strips script tags and event handlers. It does not execute JavaScript,
log in, submit forms or access the Joomla database. Files and pages that have no
discoverable link need an extra seed URL. JavaScript-only content and links inside
PDFs, SVGs, XML feeds or other downloaded documents are not discovered or rewritten.
Linked external websites, videos/embeds and any failed downloads may still need
internet access. Assets such as images, fonts and PDFs are retained alongside the
HTML and CSS; "static" does not mean throwing those files away.

## Output and report

```text
archive/
  index.html                # starting page
  pages/                    # other HTML pages
  assets/                   # stylesheets, images, fonts, PDFs, etc.
  archive-report.json       # original URLs, local paths, errors, skipped URLs
  .nojekyll                 # serve files directly on GitHub Pages
.archive-cache/             # original downloads; never needs to be hosted
```

The cache and generated output are ignored by Git by default. Keep a backup of
**both** folders while preserving the site. Once the original host is gone, the
cache lets you regenerate pages already downloaded, but cannot recover missing
content. The output folder itself is the deployable website.

Open `archive/archive-report.json` and review:

- `summary`: saved HTML pages, total files, errors, skips and pending work;
- `failures`: missing/broken URLs, HTTP errors, robots restrictions and timeouts;
- `failure_reasons`: error counts grouped by HTTP status, robots.txt or other causes;
- `skipped` / `skip_reasons`: calendar navigation, exclusions and limits;
- `files`: the original-to-local URL map and download timestamps.

Failed or excluded links retain their absolute original URL; they do not point
at nonexistent local files. Those links will stop working if their source goes
offline. Fix important failures or preserve those resources separately before
the host is shut down. Exit code **0** means the run finished without download
errors or reached limits; **1** means errors/limits or no HTML was saved; **130**
means interrupted. Deliberately skipped calendar/interactive pages do not make
the exit code fail. A successful exit code is not proof that every page on the
server has been discovered.

**HTTP 404 is not a robots.txt restriction.** It means the source server reports
that URL as missing. Old Joomla routes, removed PHP viewers (such as
`pomologia.php`) and missing documents can produce many such errors even while
the remaining archive is saved successfully. Bypassing robots.txt cannot recover
these files. Repair the source links, add known working URLs with `--seed-url`,
or recover the missing material from the original hosting files/database backup.
Successfully saved cover images do not restore a missing magazine viewer or its
unlinked pages.

## Resume, limits and useful options

### Test the archive notice placement

The Python configuration has two booleans: `notice_top=True` and
`notice_bottom=True`. Use these command-line switches to change each separately.
With an existing archive and its adjacent raw cache, `--render-only` applies the
change locally without downloading anything or retrying missing URLs:

```powershell
# Top and bottom (the default).
.\.venv\Scripts\python.exe archive_crawler.py --render-only --notice-top --notice-bottom

# Top only.
.\.venv\Scripts\python.exe archive_crawler.py --render-only --notice-top --no-notice-bottom

# Bottom only.
.\.venv\Scripts\python.exe archive_crawler.py --render-only --no-notice-top --notice-bottom

# Neither position.
.\.venv\Scripts\python.exe archive_crawler.py --render-only --no-notice-top --no-notice-bottom
```

Reload your local preview after changing settings. These switches also work on a
normal crawl without `--render-only`. For another output folder, also pass
`--output folder-name`. Rendering uses the original cached HTML, so notices do
not accumulate. Existing download errors remain in the report; render-only exits
with code 0 when local rendering succeeds. Keep using the same notice switches
on later runs: settings are chosen per invocation.

### Resume a crawl

Run the same command again to resume. Successfully downloaded response bodies
are reused from the adjacent cache, pages are rediscovered, and failed URLs are
tried again. Ctrl+C during downloading exports the files fetched so far. If the
process is forcibly terminated, rerun it to export from the cache.

```powershell
# A small trial; a limit warning/exit code 1 is expected.
.\.venv\Scripts\python.exe archive_crawler.py --output smoke-archive --max-pages 5

# Full run with a larger cap. Assets/downloads have a separate resource limit.
.\.venv\Scripts\python.exe archive_crawler.py --max-pages 5000 --max-resources 30000

# Download fresh copies after changes to the original site.
.\.venv\Scripts\python.exe archive_crawler.py --refresh

# Slower requests, longer timeout, and larger PDFs.
.\.venv\Scripts\python.exe archive_crawler.py --delay 1 --timeout 60 --max-size-mb 250

# Add a known article that is no longer linked from the menus.
.\.venv\Scripts\python.exe archive_crawler.py --seed-url 'https://archief.boomgaardenstichting.be/index.php?option=com_content&view=article&id=123'

# General use on another public website.
.\.venv\Scripts\python.exe archive_crawler.py 'https://example.org/' --output other-archive
```

Additional options:

| Option | Purpose |
| --- | --- |
| `--include-host hostname` | Also follow page links on this host; repeat for multiple hosts. Do not include `https://` or paths. |
| `--seed-file urls.txt` | Additional UTF-8 page URLs, one per line; blank lines and lines starting with `#` are ignored. |
| `--exclude 'regex'` | Skip URLs matching a Python regular expression; repeatable. |
| `--no-external-assets` | Leave third-party asset references online instead of downloading them. |
| `--ignore-robots` | Bypass robots.txt; already the default for this owner-authorized archive. |
| `--respect-robots` | Opt in to checking robots.txt before downloading. |
| `--allow-calendar` | Follow date navigation; see below before enabling. |
| `--retries 3` | Retry transient connection errors and HTTP 429/5xx responses. |
| `--verbose` | More detailed logging. |
| `--notice-top` / `--no-notice-top` | Enable/disable the top archive notice (default: enabled). |
| `--notice-bottom` / `--no-notice-bottom` | Enable/disable the bottom archive notice (default: enabled). |
| `--render-only` | Apply settings to the existing archive from cached downloads, without network access. |

The starting host is the crawl boundary, rather than the starting directory.
Other subdomains are separate hosts; they are not automatically included. Both
HTTP and HTTPS URLs on an included host may be followed. Page redirects to other
hosts are stopped unless that host is explicitly included.

The default limits are **2,000 HTML pages**, **20,000 resources** and **100 MB per
download**. Increase them and rerun if the report says a limit was reached.
robots.txt is **ignored by default**, with permission from the NBS site owner.
The same setting applies to directly referenced external assets. Use
`--respect-robots` to check rules when needed, for example when reusing this tool
for another site. In that mode, missing robots.txt (404/410) permits crawling;
an unreadable robots.txt fails closed. Rules apply to new network fetches;
already cached responses are reused offline. Ignoring robots.txt does not bypass
authentication or turn HTTP 403/404 errors into successful downloads.

### Calendar pages

JEvents has endless previous/next month and day links. The main calendar page is
saved, but dated navigation, search views and alternate print/feed views are
skipped by default. To preserve a known period, explicitly seed it, enable date
navigation and constrain it with exclusions and a page cap. For example, to add
the annual 2025 overview without following its neighbouring years or months:

```powershell
.\.venv\Scripts\python.exe archive_crawler.py --allow-calendar --seed-url 'https://archief.boomgaardenstichting.be/activiteiten/activ/kalender/evenementenperjaar/2025/-' --exclude '/kalender/(?!evenementenperjaar/2025/)' --max-pages 5000
```

Check the resulting report: date exclusions intentionally omit other event
periods. Event details reachable without navigating an excluded date view are
still followed. Add more seed URLs if you need older material.

Use a dedicated output directory. Existing files are overwritten when downloaded
again; unrelated or obsolete files in that directory are not deleted. For a clean
publication, export into a fresh folder. A cache belongs to its adjacent output
folder, so keep its name/location when resuming.

## Publish the archive

On any static host, upload the **contents** of `archive/`, including `.nojekyll`,
so that `index.html` sits at the website's root. Python is needed only to create
the archive, never to serve it in production.

For GitHub Pages, use a separate publication repository so colleagues can keep
this source repository small:

1. Finish crawling and review the report locally.
2. Create the publication repository and commit the contents of `archive/` at
   its root. Large archives are easier to transfer with Git than browser uploads.
3. Under **Settings → Pages**, select deployment from the publication branch,
   with the root folder as the source.
4. Open the published site and check the menus, images and document links.

Keep a separate offline backup as well. GitHub Pages and Git have file-size and
site-size limits; check the current [GitHub Pages limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)
before choosing it for a document-heavy archive. Another static host may suit a
large collection better. This repository's CI runs tests; it does not crawl the
live site or automatically publish content on every push.

## Development

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests use a local HTTP server and do not contact the original website. They cover
query variants, redirects, scope, CSS dependencies, local links, missing resources,
robots.txt, disabled interactive features, caching/resuming, retries and limits.
GitHub Actions runs them on Windows and Linux with Python 3.10 and 3.14.

The source code is [MIT licensed](LICENSE). Downloaded website content retains
its existing ownership and licenses; the code license does not relicense NBS
articles, photographs, fonts or documents.
