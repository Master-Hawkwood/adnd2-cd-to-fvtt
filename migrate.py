#!/usr/bin/env python3
"""
Build the `adnd2-compendium` Foundry VTT module from a local AD&D 2e Core Rules
CD-ROM. Uses fvtt-cli instead of plyvel for LevelDB writes (cross-platform).
Requires Node.js + npm install -g @foundryvtt/foundryvtt-cli.
Idempotent — each run deletes and regenerates every output pack from scratch.

Target: Foundry VTT v14, system ARS Variant 2 (AD&D 2e / THAC0). Packs are
LevelDB (ClassicLevel) directories under adnd2-compendium/packs/.

──────────────────────────────────────────────────────────────────────────────
SOURCES (read at runtime from the user's CD-ROM — nothing is shipped with this
script; see the copyright note below):

  cd-rom/MACBOOKS/HTML/{BOOK}/*.HTM   rulebook prose (Latin-1/cp1252, FONT-tag
                                      formatting, no CSS). Parsed with BeautifulSoup.
  cd-rom/DATABASE/*.DAT               structured game data in MFC CArchive binary
                                      format (schema 88+, ~1996). Helpers:
                                      parse_mfc_header(), find_records(),
                                      read_pascal()/read_mfc_long_pascal().
  cd-rom/BITMAPS/{EQUIP,MONSTERS,PORTRAIT}/  icon sprite sheets (extracted to PNG).

OUTPUT PACKS (OUTPUT_PACKS): journals, races, classes, items, spells, powers,
monsters (Actor), proficiencies, skills, backgrounds, treasure (RollTable).

PIPELINE (main): Phase 2 journals (migrate_book) → Phase 3 entities
(migrate_races/classes/items/spells/psionics/monsters) → Phase 4
(migrate_proficiencies/skills/backgrounds) → Phase 5 (migrate_treasure)
→ write_module_json. Each migrate_* opens a pack via _open_pack
(wipe+recreate), parses its source, and writes Foundry documents built by
the corresponding make_* factory functions.

──────────────────────────────────────────────────────────────────────────────
COPYRIGHT (critical — the AD&D 2e content is TSR/WotC's; the user holds no
redistribution rights):
  * This script embeds NO copyrighted content — no rules text, no stat blocks,
    and no numeric game values (movement, HD, saves, costs, XP, …). Every value
    the output contains is read from the user's own .DAT/.HTM at runtime.
  * What IS hard-coded is only *references into the source*: file paths, HTML
    anchors, regexes, parser offsets, and integer record indices.
  * When the logic genuinely needs a copyrighted string (a named-mage spell
    title, a reverse-pair relationship), we hard-code only its LOCATION (a
    SPELLS.DAT record index or a source .HTM path) and read the text at runtime —
    see _SPELL_ICON_INDEX, _SPELL_DESC_HTM_INDEX, _SPELL_REVERSE_INDEX and
    _spell_records(). Generic terms also present in OSRIC (OGL Open Game Content)
    are treated as safe and may appear as literals (e.g. icon-match keywords).
"""

import os
import re
import sys
import html
import json
import shutil
import random
import string
import struct
import subprocess
from html.parser import HTMLParser
import datetime
from typing import Any
from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag
from PIL import Image

# ─── Configuration ────────────────────────────────────────────────────────────

# fvtt-cli integration (migrate2.py — plyvel-free variant)
# Install fvtt-cli globally: npm install -g @foundryvtt/foundryvtt-cli
# If 'fvtt' is not on PATH, use the list form for npx:
#   _FVTT_CLI_CMD = ['npx', '@foundryvtt/foundryvtt-cli']
_FVTT_CLI_CMD: "str | list[str]" = 'fvtt'
# Intermediate JSON staging directory (deleted after packing).
_PACK_SRC_BASE = "adnd2-compendium-src"

# Phase 2 — HTML rulebooks
SOURCE_BASE  = "cd-rom/MACBOOKS/HTML"
# Optional errata override tree. The AD&D Core Rules 2.0 CD-ROM ships the 1995
# reprint text, which predates TSR's official errata. If the user drops corrected
# rulebook pages here (same HTML format as the CD; e.g. the WebHelp pages from the
# official errata distribution), the script substitutes them at import time by
# matching the page <TITLE>. Nothing errata-related is bundled with the script —
# this directory is the user's own local copy, read at runtime like the CD-ROM.
ERRATA_BASE  = "cd-rom/ERRATA"
OUTPUT_DB    = "adnd2-compendium/packs/adnd2-journals"
OUTPUT_IMG   = "adnd2-compendium/images"
MODULE_ID    = "adnd2-compendium"

# Phase 3 — DATABASE/*.DAT binary files
# Binary schema: MFC CArchive format
# Field mapping reference: see MAPPING.md
DATABASE_BASE = "cd-rom/DATABASE"
BITMAPS_BASE  = "cd-rom/BITMAPS"
OUTPUT_PACKS  = {
    'races':         "adnd2-compendium/packs/adnd2-races",
    'classes':       "adnd2-compendium/packs/adnd2-classes",
    'items':         "adnd2-compendium/packs/adnd2-items",
    'spells':        "adnd2-compendium/packs/adnd2-spells",
    'powers':        "adnd2-compendium/packs/adnd2-powers",
    'monsters':      "adnd2-compendium/packs/adnd2-monsters",
    'proficiencies': "adnd2-compendium/packs/adnd2-proficiencies",
    'skills':        "adnd2-compendium/packs/adnd2-skills",
    'backgrounds':   "adnd2-compendium/packs/adnd2-backgrounds",
    'treasure':      "adnd2-compendium/packs/adnd2-treasure",
}
OUTPUT_IMG_ITEMS     = "adnd2-compendium/images/items"
OUTPUT_IMG_MONSTERS  = "adnd2-compendium/images/monsters"
OUTPUT_IMG_PORTRAITS = "adnd2-compendium/images/portraits"

# Each folder groups related books.
# "key"    = subdirectory name under SOURCE_BASE
# "prefix" = actual filename prefix (may differ from key, e.g. CBGH dir → CBG files)
# "mode"   = "chapters" (one page per TOC chapter) | "pages" (one page per file)
FOLDERS = [
    {
        "name": "Core Rules",
        "books": [
            {"key": "PHB",  "name": "Player's Handbook",     "prefix": "PHB", "mode": "chapters"},
            {"key": "DMG",  "name": "Dungeon Master's Guide", "prefix": "DMG", "mode": "chapters"},
            {"key": "MM",   "name": "Monster Manual",         "prefix": "MM",  "mode": "pages"},
            {"key": "TOM",  "name": "Tome of Magic",          "prefix": "TOM", "mode": "chapters"},
            {"key": "AEG",  "name": "Arms and Equipment Guide", "prefix": "AEG", "mode": "chapters"},
        ],
    },
    {
        "name": "DM's Options",
        "books": [
            {"key": "HLC", "name": "High-Level Campaigns", "prefix": "HLC", "mode": "chapters"},
        ],
    },
    {
        "name": "Player's Options",
        "books": [
            {"key": "CT",  "name": "Combat & Tactics",  "prefix": "CT", "mode": "chapters"},
            {"key": "SM",  "name": "Spells & Magic",     "prefix": "SM", "mode": "chapters"},
            {"key": "SP",  "name": "Skills & Powers",    "prefix": "SP", "mode": "chapters"},
        ],
    },
    {
        "name": "Player's Handbooks",
        "books": [
            {"key": "CFH",  "name": "Complete Fighter's Handbook",         "prefix": "CFH", "mode": "chapters"},
            {"key": "CBH",  "name": "Complete Bard's Handbook",            "prefix": "CBH", "mode": "chapters"},
            {"key": "CBT",  "name": "Complete Thief's Handbook",           "prefix": "CBT", "mode": "chapters"},
            {"key": "CWH",  "name": "Complete Wizard's Handbook",          "prefix": "CWH", "mode": "chapters"},
            {"key": "CPRH", "name": "Complete Priest's Handbook",          "prefix": "CPR", "mode": "chapters"},
            {"key": "CDH",  "name": "Complete Druid's Handbook",           "prefix": "CDH", "mode": "chapters"},
            {"key": "CRH",  "name": "Complete Ranger's Handbook",          "prefix": "CRH", "mode": "chapters"},
            {"key": "CPAH", "name": "Complete Paladin's Handbook",         "prefix": "CPA", "mode": "chapters"},
            {"key": "CBD",  "name": "Complete Book of Dwarves",            "prefix": "CBD", "mode": "chapters"},
            {"key": "CBGH", "name": "Complete Book of Gnomes & Halflings", "prefix": "CBG", "mode": "chapters"},
        ],
    },
]

CORE_VERSION   = "14.363"
SYSTEM_ID      = "ars"
SYSTEM_VERSION = "2026.05.31"

# ─── ID generation ────────────────────────────────────────────────────────────

def make_id(length=16):
    """Random alphanumeric Foundry document `_id` (16 chars is Foundry's width)."""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

# ─── Image copy with transparency removal ─────────────────────────────────────

def copy_image_white_bg(src_path, dest_path):
    """Copy a GIF image to dest, compositing any transparency onto white."""
    try:
        img = Image.open(src_path)
        if img.mode in ('P', 'RGBA') or 'transparency' in img.info:
            rgba = img.convert('RGBA')
            bg = Image.new('RGBA', rgba.size, (255, 255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[3])
            bg.convert('RGB').save(dest_path, 'GIF')
        else:
            shutil.copy2(src_path, dest_path)
    except Exception:
        shutil.copy2(src_path, dest_path)

# ─── HTML title extraction ─────────────────────────────────────────────────────

class TitleParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._in = False
        self.title = ''
    def handle_starttag(self, tag, attrs):  # noqa: attrs required by HTMLParser interface
        if tag == 'title': self._in = True
    def handle_endtag(self, tag):
        if tag == 'title': self._in = False
    def handle_data(self, data):
        if self._in: self.title += data


def extract_title(filepath):
    """Page title from an .HTM <title>, minus the trailing "(Book Name)" suffix.
    Falls back to the filename. Only the first 4 KB is read (title is in <head>)."""
    with open(filepath, 'r', encoding='cp1252') as f:
        content = f.read(4096)
    p = TitleParser()
    p.feed(content)
    title = p.title.strip()
    # Strip any parenthetical book name suffix, e.g. "(Player's Handbook)"
    title = re.sub(r'\s*\([^)]+\)\s*$', '', title).strip()
    return title or os.path.basename(filepath)

# ─── TOC parsing ──────────────────────────────────────────────────────────────

class TOCParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.entries = []
        self._size = None
        self._bold = False
        self._href = None
        self._text = ''
        self._in_a = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'font':
            if 'size' in attrs: self._size = attrs['size']
        elif tag == 'b': self._bold = True
        elif tag == 'a':
            self._href = attrs.get('href', '')
            self._in_a = True
            self._text = ''

    def handle_endtag(self, tag):
        if tag == 'b': self._bold = False
        elif tag == 'a' and self._in_a:
            text = self._text.strip()
            href = self._href or ''
            if text and href and '#' not in href:
                is_chapter = self._bold and self._size in ('4', '5', '6')
                self.entries.append({'title': text, 'filename': href.upper(), 'is_chapter': is_chapter})
            self._in_a = False
            self._text = ''

    def handle_data(self, data):
        if self._in_a: self._text += data


def parse_toc(toc_path):
    """Parse a book's TOC page ({PREFIX}00000.HTM) into link entries
    [{title, filename, is_chapter}]. `is_chapter` flags bold SIZE>=4 links (the
    chapter starts) vs ordinary cross-references; anchor (#) links are skipped."""
    with open(toc_path, 'r', encoding='cp1252') as f:
        content = f.read()
    parser = TOCParser()
    parser.feed(content)
    return parser.entries


def _chapter_title_from_file(filepath):
    """
    Return the chapter-level heading text from a content file, or None.
    Used as fallback when the TOC has no SIZE>=4 bold chapter links.

    Color + size rules:
      #0000ff (blue)   SIZE=4 bold — CFH, CBT, CWH, CPRH
      #800000 (maroon) SIZE=4 bold — CBH, CDH, CRH, CPAH, CBD, CBGH
      #ff0000 (red)    SIZE>=5     — CT, SM, SP (Player's Option series)
    """
    with open(filepath, 'r', encoding='cp1252') as f:
        raw = f.read()
    soup = BeautifulSoup(raw, 'html.parser')
    for font in soup.find_all('font'):
        color = str(font.get('color') or '').lower()
        try:    size = int(str(font.get('size') or 0))
        except (TypeError, ValueError): size = 0
        is_bold = bool(font.find('b')) or (
            font.parent and getattr(font.parent, 'name', None) == 'b')
        if color in ('#0000ff', '#800000') and size == 4 and is_bold:
            text = font.get_text(strip=True)
            if text:
                return text
        if color == '#ff0000' and size >= 5:
            text = font.get_text(strip=True)
            if text:
                return text
    return None


def build_chapters(book_dir, book_prefix, toc_entries):
    """Group a book's content files into chapters [{title, files}].

    Two strategies: if the TOC exposes bold
    SIZE>=4 chapter links (PHB, DMG, TOM, AEG) split on those file boundaries;
    otherwise (the Complete/Option books, whose TOC links are only SIZE=3) scan
    each file for the first chapter-coloured heading (_chapter_title_from_file)
    and treat each match as a boundary. Files before the first boundary become a
    "Foreword"; if nothing is found, everything goes in one "Content" chapter."""
    all_files = sorted([
        f for f in os.listdir(book_dir)
        if f.upper().startswith(book_prefix) and f.upper().endswith('.HTM')
           and f.upper() != f'{book_prefix}00000.HTM'
    ])
    file_upper = [f.upper() for f in all_files]

    chapter_entries = [e for e in toc_entries if e['is_chapter']]

    if chapter_entries:
        # TOC-based splitting (PHB, DMG)
        starts = []
        for entry in chapter_entries:
            fname = entry['filename']
            if fname in file_upper:
                starts.append((file_upper.index(fname), entry['title']))
        starts.sort(key=lambda x: x[0])
    else:
        # Content-based splitting: scan files for blue SIZE=4 bold headings
        starts = []
        for i, filename in enumerate(all_files):
            filepath = os.path.join(book_dir, filename)
            title = _chapter_title_from_file(filepath)
            if title:
                starts.append((i, title))

    if not starts:
        return [{'title': 'Content', 'files': all_files}]

    chapters = []
    for i, (idx, title) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(all_files)
        files = all_files[idx:end]
        if files:
            chapters.append({'title': title, 'files': files})

    if starts and starts[0][0] > 0:
        intro = all_files[:starts[0][0]]
        if intro:
            chapters.insert(0, {'title': 'Foreword', 'files': intro})

    return chapters

# ─── HTML cleaning ────────────────────────────────────────────────────────────

BLOCK_TAGS = {'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'pre', 'ul', 'ol', 'blockquote'}


def merge_consecutive_headings(body):
    """
    Merge consecutive sibling headings of the same level into one.
    e.g. <h2>Chapter 1:</h2><h2>Title</h2> → <h2>Chapter 1: Title</h2>
    These appear because the chapter label and title are in separate FONT tags.
    """
    for level in ('h1', 'h2', 'h3'):
        changed = True
        while changed:
            changed = False
            for h in body.find_all(level):
                # Find next meaningful sibling (skip whitespace text nodes)
                nxt = h.next_sibling
                while nxt and isinstance(nxt, NavigableString) and not nxt.strip():
                    nxt = nxt.next_sibling
                if nxt and isinstance(nxt, Tag) and nxt.name == level:
                    # Merge: append content of nxt into h
                    h.append(NavigableString(' '))
                    for child in list(nxt.children):
                        h.append(child)
                    nxt.decompose()
                    changed = True
                    break  # restart scan after modification


def clean_heading_content(body):
    """Remove empty <p> elements from inside heading tags (they are FONT paragraph breaks)."""
    for h in body.find_all(list(BLOCK_TAGS)):
        for empty_p in h.find_all('p'):
            if not empty_p.get_text(strip=True):
                empty_p.replace_with(NavigableString(' '))


def restructure_paragraphs(body, soup):
    """
    Convert <p></p> paragraph-separator elements into proper <p> containers.
    Inline runs between separators and/or block elements are wrapped in <p>.
    Spans with no text content (anchor stubs) are discarded since all cross-links are removed.
    """
    children = list(body.children)  # snapshot before modification
    new_nodes = []
    inline_buf = []

    def flush():
        has_text = any(
            (c.strip() if isinstance(c, NavigableString) else bool(c.get_text(strip=True)))
            for c in inline_buf
        )
        if has_text:
            p = soup.new_tag('p')
            for node in inline_buf:
                p.append(node)  # BeautifulSoup auto-extracts from previous parent
            new_nodes.append(p)
        else:
            # Only whitespace / empty anchor spans — discard
            for node in inline_buf:
                if isinstance(node, Tag):
                    node.extract()
        inline_buf.clear()

    for child in children:
        name = getattr(child, 'name', None)

        if name == 'p':
            # Paragraph separator → flush buffer, discard the empty <p>
            flush()
            child.extract()
        elif name in BLOCK_TAGS:
            flush()
            child.extract()
            new_nodes.append(child)
        elif name == 'img':
            # Self-closing image → flush, then wrap in <p>
            flush()
            child.extract()
            p = soup.new_tag('p')
            p.append(child)
            new_nodes.append(p)
        else:
            # Inline: text node, span, strong, em, i, br, etc.
            inline_buf.append(child)

    flush()

    body.clear()
    for node in new_nodes:
        body.append(node)


def process_fonts(body, soup, src_dir_files, book_key):
    """Convert FONT-based formatting to semantic HTML and handle images."""
    # Remove navigation FORM
    for form in body.find_all('form'):
        form.decompose()

    # Remove TOC back-links (links to *00000.htm*)
    for a in body.find_all('a', href=True):
        if '00000.htm' in a.get('href', '').lower():
            a.decompose()

    # Handle images (case-insensitive lookup on Linux)
    for img in body.find_all('img'):
        src = img.get('src', '')
        if not src:
            img.decompose()
            continue
        actual = src_dir_files.get(os.path.basename(src).upper())
        if actual:
            dest_dir = os.path.join(OUTPUT_IMG, book_key)
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, actual)
            src_path = os.path.join(SOURCE_BASE, book_key, actual)
            if not os.path.exists(dest):
                copy_image_white_bg(src_path, dest)
            img['src'] = f'modules/{MODULE_ID}/images/{book_key}/{actual}'
            img.attrs.pop('border', None)
        else:
            img.decompose()

    # Convert FONT tags to semantic equivalents
    # Process bottom-up to handle nesting correctly
    for font in body.find_all('font'):
        color    = str(font.get('color') or '').lower()
        try:    size = int(str(font.get('size') or 3))
        except (TypeError, ValueError): size = 3

        is_bold = bool(font.find('b')) or (font.parent and getattr(font.parent, 'name', None) == 'b')

        red    = color in ('#ff0000', 'red')
        blue   = color in ('#0000ff', 'blue')
        purple = color in ('#800080', 'purple')
        navy   = color == '#000080'
        maroon = color == '#800000'

        if (red or navy) and size >= 5:
            new = soup.new_tag('h2')
        elif (red or navy) and size == 4:
            new = soup.new_tag('h3')
        elif (red or navy) and size == 3 and is_bold:
            new = soup.new_tag('strong')
        elif blue and size >= 5:
            new = soup.new_tag('h1')
        elif (blue or maroon) and size == 4:
            new = soup.new_tag('h2')
        elif (blue or maroon) and size == 3 and is_bold:
            new = soup.new_tag('h3')
        elif purple and size >= 4:
            new = soup.new_tag('h3' if is_bold else 'h4')
        elif (navy or purple) and size == 3 and is_bold:
            new = soup.new_tag('strong')
        else:
            font.unwrap()
            continue

        new.extend(list(font.children))
        font.replace_with(new)

    # Unwrap remaining <b> tags
    for b in body.find_all('b'):
        b.unwrap()

    # Convert remaining <A NAME="..."> anchors to id spans
    for a in body.find_all('a', attrs={'name': True}):
        span = soup.new_tag('span', id=a['name'])
        a.replace_with(span)

    # Strip href links to HTM files → keep text only
    for a in body.find_all('a', href=True):
        a.unwrap()


def _clean_html_body(body, soup, book_key, src_dir_files):
    """Run the semantic-cleanup pipeline on an in-memory <body> Tag and
    return the serialized inner HTML. Shared by clean_html_file (whole
    file) and the sub-race-section extractor."""
    process_fonts(body, soup, src_dir_files, book_key)
    clean_heading_content(body)
    merge_consecutive_headings(body)
    restructure_paragraphs(body, soup)
    for h in body.find_all(['h1', 'h2', 'h3', 'h4']):
        text = h.get_text()
        normalized = ' '.join(text.split())
        h.clear()
        h.append(NavigableString(normalized))
    inner = ''.join(str(c) for c in body.children)
    inner = re.sub(r'\n{3,}', '\n\n', inner).strip()
    return inner


_ERRATA_INDEX = None   # {normalized <TITLE>: errata filepath} or {} if no errata tree
_ERRATA_FETCH_DONE = False

_ERRATA_TITLE_RE = re.compile(r'<title>(.*?)</title>', re.IGNORECASE | re.DOTALL)

# Source of the corrected rulebook pages: the community-compiled distribution of
# TSR's official AD&D Core Rules 2.0 errata. We hardcode only the URLs (references,
# like a file path — never the copyrighted text); the pages are fetched to the
# user's own machine at runtime and are never bundled with this script. The CD-ROM
# ships the 1995 reprint text, which predates this errata.
_ERRATA_REMOTE_BASE = ('https://raw.githubusercontent.com/'
                       'Alby1987/AD-DCoreRule2.0Errata/main/patch/WebHelp')
_ERRATA_REMOTE_FILES = [
    'PHB/DD01857.htm', 'PHB/DD02184.htm', 'PHB/DD02316.htm', 'PHB/DD02355.htm',
    'DMG/DD00331.htm', 'DMG/DD00823.htm',
]


def _ensure_errata_downloaded():
    """Best-effort, once-per-run fetch of the errata pages into ERRATA_BASE.

    Skips entirely if ERRATA_BASE already holds errata pages (offline-friendly:
    download only happens the first time, and re-running works with no network).
    Any failure — no network, a moved/removed URL, an HTTP error — is swallowed
    with a warning so the migration always proceeds; errata is simply not applied.
    Nothing copyrighted is stored in this script: only the URLs are referenced and
    the fetched pages land in the user's local CD-ROM tree."""
    global _ERRATA_FETCH_DONE
    if _ERRATA_FETCH_DONE:
        return
    _ERRATA_FETCH_DONE = True
    # Already have a local copy? Don't re-download.
    if os.path.isdir(ERRATA_BASE):
        for _r, _d, files in os.walk(ERRATA_BASE):
            if any(f.lower().endswith(('.htm', '.html')) for f in files):
                return
    import urllib.request
    import urllib.error
    got = 0
    for rel in _ERRATA_REMOTE_FILES:
        url = f'{_ERRATA_REMOTE_BASE}/{rel}'
        dest = os.path.join(ERRATA_BASE, rel.replace('/', os.sep))
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = resp.read()
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, 'wb') as f:
                f.write(data)
            got += 1
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"  errata: could not fetch {rel} ({exc}); skipping")
    if got:
        print(f"  errata: downloaded {got} corrected page(s) to {ERRATA_BASE}")


def _errata_title(raw):
    """Return the normalized <TITLE> text of a CD-ROM/errata HTML string, or None."""
    m = _ERRATA_TITLE_RE.search(raw)
    if not m:
        return None
    return ' '.join(m.group(1).split()).strip().lower()


def _errata_index():
    """Lazily index the optional ERRATA_BASE override tree by page <TITLE>.

    Returns {normalized_title: filepath}. Empty if the directory is absent — the
    errata feature is opt-in and the script runs identically without it. The map
    is keyed by <TITLE> (not filename) because the errata distribution uses a
    different file-numbering layout than our CD-ROM, but page titles are stable."""
    global _ERRATA_INDEX
    if _ERRATA_INDEX is not None:
        return _ERRATA_INDEX
    _ensure_errata_downloaded()
    index = {}
    if os.path.isdir(ERRATA_BASE):
        for root, _dirs, files in os.walk(ERRATA_BASE):
            for fn in files:
                if not fn.lower().endswith(('.htm', '.html')):
                    continue
                fp = os.path.join(root, fn)
                try:
                    with open(fp, 'r', encoding='cp1252') as f:
                        title = _errata_title(f.read())
                except OSError:
                    continue
                if title:
                    index[title] = fp
        if index:
            print(f"  errata: {len(index)} corrected page(s) loaded from {ERRATA_BASE}")
    _ERRATA_INDEX = index
    return _ERRATA_INDEX


def _read_html_with_errata(filepath):
    """Read a CD-ROM HTML file, transparently substituting an errata override when
    one exists for the same page <TITLE>. Returns the raw HTML string."""
    with open(filepath, 'r', encoding='cp1252') as f:
        raw = f.read()
    index = _errata_index()
    if index:
        title = _errata_title(raw)
        errata_fp = index.get(title) if title else None
        if errata_fp and os.path.abspath(errata_fp) != os.path.abspath(filepath):
            with open(errata_fp, 'r', encoding='cp1252') as f:
                return f.read()
    return raw


def clean_html_file(filepath, book_key, src_dir_files):
    """Parse one HTML file and return clean semantic HTML."""
    raw = _read_html_with_errata(filepath)
    soup = BeautifulSoup(raw, 'html.parser')
    body = soup.find('body') or soup
    return _clean_html_body(body, soup, book_key, src_dir_files)


def merge_chapter_html(book_dir, book_key, chapter, src_dir_files):
    """Clean and concatenate every .HTM file of a chapter into one HTML string
    (one Foundry journal page per chapter). Tolerates upper/lowercase filenames
    (Linux is case-sensitive; the CD-ROM mixes cases) and skips missing/empty files."""
    parts = []
    for filename in chapter['files']:
        filepath = os.path.join(book_dir, filename)
        if not os.path.exists(filepath):
            filepath = os.path.join(book_dir, filename.lower())
        if not os.path.exists(filepath):
            continue
        html = clean_html_file(filepath, book_key, src_dir_files)
        if html.strip():
            parts.append(html)
    return '\n'.join(parts)

# ─── LevelDB record factories ─────────────────────────────────────────────────

def make_page(page_id, title, content, sort):
    """Build a JournalEntryPage document (text page). `text.format: 1` = HTML.
    Stored under `!journal.pages!{journalId}.{pageId}`."""
    return {
        "name": title, "type": "text",
        "title": {"show": True, "level": 1},
        "text": {"format": 1, "content": content, "markdown": ""},
        "_id": page_id,
        "image": {}, "video": {"controls": True, "volume": 0.5},
        "src": None, "system": {}, "sort": sort,
        "ownership": {"default": -1}, "flags": {},
        "_stats": {
            "compendiumSource": None, "duplicateSource": None, "exportSource": None,
            "coreVersion": CORE_VERSION, "systemId": SYSTEM_ID,
            "systemVersion": SYSTEM_VERSION, "lastModifiedBy": None,
        },
        "category": None,
    }


def make_journal(journal_id, name, page_ids, folder_id, sort=0):
    """Build a JournalEntry document. `pages` is the list of page `_id`s; the page
    bodies are written as separate sub-documents (see make_page). `!journal!{id}`."""
    return {
        "_id": journal_id, "name": name, "pages": page_ids,
        "folder": folder_id, "sort": sort,
        "ownership": {"default": -1}, "flags": {},
        "_stats": {
            "compendiumSource": None, "duplicateSource": None, "exportSource": None,
            "coreVersion": CORE_VERSION, "systemId": SYSTEM_ID,
            "systemVersion": SYSTEM_VERSION, "lastModifiedBy": None,
        },
    }


def make_folder(folder_id, name, parent_id=None, sort=0):
    """Build a JournalEntry Folder document (`sorting: "m"` = manual order).
    `parent_id` nests it under another folder. `!folders!{id}`."""
    return {
        "_id": folder_id, "name": name, "type": "JournalEntry",
        "folder": parent_id, "sorting": "m", "sort": sort, "color": None,
        "flags": {},
        "_stats": {
            "compendiumSource": None, "duplicateSource": None, "exportSource": None,
            "coreVersion": CORE_VERSION, "systemId": SYSTEM_ID,
            "systemVersion": SYSTEM_VERSION, "lastModifiedBy": None,
        },
    }

# ════════════════════════════════════════════════════════════════════════════
# Phase 3 — DATABASE/*.DAT migration
# Binary schema: MFC CArchive format, reverse-engineered from the CD-ROM binaries
# See MAPPING.md for DAT → Foundry field mapping
# ════════════════════════════════════════════════════════════════════════════

# ─── MFC CArchive primitives ──────────────────────────────────────────────────

def read_pascal(buf, off, max_len=255):
    """Read short Pascal string (1-byte length + ASCII). Returns (str|None, new_off)."""
    if off >= len(buf): return None, off
    n = buf[off]
    if 0 <= n <= max_len and off+1+n <= len(buf):
        seq = buf[off+1:off+1+n]
        if n == 0 or all(32 <= b < 127 for b in seq):
            try:
                return seq.decode('latin-1'), off + 1 + n
            except UnicodeDecodeError:
                return None, off
    return None, off


def read_mfc_long_pascal(buf, off):
    """Read MFC long Pascal (0xFF marker + WORD length + data) or short Pascal."""
    if off >= len(buf): return None, off
    n = buf[off]
    if n == 0xFF and off + 3 <= len(buf):
        n = struct.unpack_from('<H', buf, off+1)[0]
        if n == 0xFFFF and off + 7 <= len(buf):
            n = struct.unpack_from('<I', buf, off+3)[0]
            off += 7
        else:
            off += 3
        if off + n <= len(buf):
            return buf[off:off+n].decode('latin-1', 'replace'), off + n
        return None, off
    return read_pascal(buf, off)


def parse_mfc_header(buf):
    """Read CArchive class header. Returns (count, schema, class_name, header_end)."""
    count  = struct.unpack_from('<H', buf, 0)[0]
    # buf[2:4] is the class tag (expected 0xFFFF), skip it
    struct.unpack_from('<H', buf, 2)
    schema = struct.unpack_from('<H', buf, 4)[0]
    nlen   = struct.unpack_from('<H', buf, 6)[0]
    name   = buf[8:8+nlen].decode('ascii')
    return count, schema, name, 8 + nlen


def find_records(buf, header_end):
    """Walk CArchive file finding record boundaries via 01 80 + Pascal-name validation."""
    records = [header_end]
    i = header_end
    while i < len(buf) - 3:
        if buf[i] == 0x01 and buf[i+1] == 0x80:
            nlen = buf[i+2]
            if 1 <= nlen <= 60 and all(32 <= b < 127 for b in buf[i+3:i+3+nlen]):
                records.append(i+2)
                i += 2 + 1 + nlen
                continue
        i += 1
    return records


# ─── BITMAPS extraction ───────────────────────────────────────────────────────

def slugify(s):
    """Make a string filesystem-safe."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    return s or "unnamed"


def bmp_to_png(src_path, dest_path):
    """Convert a Windows BMP (mode P palette) to a transparent-aware PNG."""
    try:
        img = Image.open(src_path).convert('RGBA')
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        img.save(dest_path, 'PNG')
        return True
    except Exception as e:
        print(f"  ! BMP→PNG failed for {src_path}: {e}")
        return False


def extract_equip_icon(icon_id, dest_dir):
    """Extract a 64×64 icon from the EQUIP/ sprite sheets using the 1-700 icon ID
    stored in each PARTS record as a digit-ASCII Pascal string (see parse_part_record).
    Returns the relative module path, or None if the ID is missing/out-of-range."""
    if not icon_id or not (1 <= icon_id <= 700):
        return None
    sheet_idx = (icon_id - 1) // 50
    slot      = (icon_id - 1) % 50
    sheet_low = sheet_idx * 50 + 1
    sheet_hi  = (sheet_idx + 1) * 50
    sheet_name = f'E{sheet_low:03d}_{sheet_hi:03d}.BMP'
    sheet_path = os.path.join(BITMAPS_BASE, 'EQUIP', sheet_name)
    if not os.path.exists(sheet_path):
        return None
    dest = os.path.join(dest_dir, f'item_{icon_id:03d}.png')
    if not os.path.exists(dest):
        try:
            sheet = Image.open(sheet_path)
            icon = sheet.crop((slot * 64, 0, (slot + 1) * 64, 64)).convert('RGBA')
            os.makedirs(dest_dir, exist_ok=True)
            icon.save(dest, 'PNG')
        except Exception as e:
            print(f"  ! EQUIP crop failed for icon {icon_id}: {e}")
            return None
    return f'modules/{MODULE_ID}/images/items/item_{icon_id:03d}.png'


# Case-insensitive lookup cache for BITMAPS/MONSTERS/
_monsters_dir_cache = None

def _monsters_dir():
    """Cached {UPPERCASE: actual} listing of BITMAPS/MONSTERS/ for case-insensitive
    icon lookup (filenames are uppercase on disk but referenced in mixed case)."""
    global _monsters_dir_cache
    if _monsters_dir_cache is None:
        d = os.path.join(BITMAPS_BASE, 'MONSTERS')
        _monsters_dir_cache = {f.upper(): f for f in os.listdir(d)} if os.path.isdir(d) else {}
    return _monsters_dir_cache


def extract_monster_icon(individual_bmp, dest_dir):
    """Copy MONSTERS/{individual}.BMP to a PNG. Returns relative module path or None."""
    if not individual_bmp:
        return None
    target = individual_bmp.upper()
    files = _monsters_dir()
    actual = files.get(target)
    if not actual:
        return None
    src = os.path.join(BITMAPS_BASE, 'MONSTERS', actual)
    slug = os.path.splitext(actual)[0].lower()
    dest = os.path.join(dest_dir, f'monster_{slug}.png')
    if not os.path.exists(dest):
        if not bmp_to_png(src, dest):
            return None
    return f'modules/{MODULE_ID}/images/monsters/monster_{slug}.png'


_portrait_dir_cache = None

def _portrait_dir():
    """Cached {UPPERCASE: actual} listing of BITMAPS/PORTRAIT/ for case-insensitive
    portrait lookup (see _monsters_dir)."""
    global _portrait_dir_cache
    if _portrait_dir_cache is None:
        d = os.path.join(BITMAPS_BASE, 'PORTRAIT')
        _portrait_dir_cache = {f.upper(): f for f in os.listdir(d)} if os.path.isdir(d) else {}
    return _portrait_dir_cache


def extract_portrait(bmp_name, dest_dir):
    """Copy PORTRAIT/{bmp_name} to a PNG. Returns relative module path or None."""
    if not bmp_name:
        return None
    target = bmp_name.upper()
    files = _portrait_dir()
    actual = files.get(target)
    if not actual:
        return None
    src = os.path.join(BITMAPS_BASE, 'PORTRAIT', actual)
    slug = os.path.splitext(actual)[0].lower()
    dest = os.path.join(dest_dir, f'{slug}.png')
    if not os.path.exists(dest):
        if not bmp_to_png(src, dest):
            return None
    return f'modules/{MODULE_ID}/images/portraits/{slug}.png'


# ─── DAT parsers ──────────────────────────────────────────────────────────────

def _load_dat(filename):
    """Load DAT file → bytes."""
    path = os.path.join(DATABASE_BASE, filename)
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as f:
        return f.read()


def parse_montype(buf):
    """Return dict {monster_category_name: individual_bmp_filename}."""
    if not buf: return {}
    _, _, _, header_end = parse_mfc_header(buf)
    markers = find_records(buf, header_end)
    result = {}
    for off in markers:
        name, p = read_pascal(buf, off)
        if not name: continue
        # Skip the sheet Pascal (value unused)
        _, p = read_pascal(buf, p)
        # Skip 10 bytes of structured data
        p += 10
        # Individual Pascal
        indiv, _ = read_pascal(buf, p)
        if indiv:
            result[name] = indiv
    return result


# RACE.DAT — MFC CArchive, CRaceOb records
def parse_race_record(buf, start, end):
    """Parse one CRaceOb record (a race) into a dict of fields read at fixed
    post-name offsets: ability score min/max ranges (12
    int32), per-ability adjustments (6 int32), class level caps + demographics
    (18 int32 — caps, starting age, race id, height/weight), then a tail scan
    for portrait .BMP filenames. Returns None if the record is too short/invalid.
    Note: per-race base movement is NOT in RACE.DAT — it's read from PHB HTM."""
    p = start
    r = {}
    r['name'], p = read_pascal(buf, p)
    if r['name'] is None: return None
    p += 1                                          # gap byte
    if p + 48 > end: return None
    ranges = struct.unpack_from('<12i', buf, p); p += 48
    r['ability_ranges'] = {
        ab: {'min': ranges[2*i], 'max': ranges[2*i+1]}
        for i, ab in enumerate(['str','dex','con','int','wis','cha'])
    }
    adjs = struct.unpack_from('<6i', buf, p); p += 24
    r['ability_adjustments'] = dict(zip(['str','dex','con','int','wis','cha'], adjs))
    if p + 72 > end: return r
    inter = struct.unpack_from('<18i', buf, p); p += 72
    r['class_caps'] = {
        name: cap for name, cap in zip(
            ['fighter','ranger','paladin','thief','bard','cleric','druid','mage','specialist_wizard'],
            inter[0:9]
        )
    }
    r['starting_age_base']  = inter[9]
    r['starting_age_dice']  = inter[10]
    r['race_id']            = inter[11]
    r['max_male_height']    = inter[12]
    r['female_weight_base'] = inter[13]
    r['max_male_weight']    = inter[14]
    r['age_variation']      = inter[15]
    r['max_age']            = inter[16]
    r['subrace_bitfield']   = inter[17]

    # Movement: skip the optional MFC CString at name_end+146 (the long
    # description), then optionally a small base-race reference Pascal
    # (e.g. "Dwarf" for "Hill dwarf", "Half-elf" for "Standard half-elf"),
    # then the MV is the int32 at +10 (or +9 / +11 fallback) from there.
    # CString encoding:
    #   - empty   : marker = 0x00 (1 byte total)
    #   - short   : marker = length 1-254 (1 + length bytes total)
    #   - long    : marker = 0xFF + WORD length (3 + length bytes total)
    # Validated on all 48 races (PHB Table 64 + SP00059 + all sub-races) — 100% match.
    name_end_abs = start + 1 + len(r['name'])
    cstring_abs  = name_end_abs + 146
    if cstring_abs < end:
        marker = buf[cstring_abs]
        if marker == 0xFF and cstring_abs + 3 < end:
            cs_len = struct.unpack_from('<H', buf, cstring_abs + 1)[0]
            after_cs = cstring_abs + 3 + cs_len
        else:
            after_cs = cstring_abs + 1 + marker
        # Optional base-race Pascal (1-20 ASCII letters / dash, e.g. "Half-elf").
        after = after_cs
        if after < end:
            plen = buf[after]
            if 1 <= plen <= 20 and after + 1 + plen <= end:
                seq = buf[after+1:after+1+plen]
                if all(65 <= b <= 122 or b == 45 for b in seq):
                    after = after + 1 + plen
        # MV: try int32 at +10 (most common), then +9 / +11 as fallbacks.
        for off in (10, 9, 11):
            if after + off + 4 <= end:
                try:
                    mv = struct.unpack_from('<i', buf, after + off)[0]
                    if 0 < mv <= 36:
                        r['movement'] = mv
                        break
                except struct.error:
                    pass

        # Thief skill racial adjustments: 13 int32 at `after + 30`. Same
        # column order as SP00074 (the "Theiving Skill Racial Adjustments"
        # table). Valid only when every value falls in [-30, +30] — that
        # bound filters out exotic-race records whose bytes at this offset
        # contain unrelated content (ASCII fragments etc.). Demihuman
        # sub-races have all zeros here (they inherit from the parent
        # lineage at the caller, not in this record's bytes).
        if after + 30 + 13*4 <= end:
            vals = list(struct.unpack_from('<13i', buf, after + 30))
            if all(-30 <= v <= 30 for v in vals):
                r['thief_skill_adjustments'] = vals

    # Scan the rest of the record for portrait BMP filenames (Pascal strings ending in .bmp / .BMP)
    rec_data = buf[start:end]
    portraits = []
    seen = set()
    k = 0
    while k < len(rec_data) - 1:
        n = rec_data[k]
        if 5 <= n <= 20 and k+1+n <= len(rec_data):
            seq = rec_data[k+1:k+1+n]
            if all(32 <= b < 127 for b in seq):
                try:
                    s = seq.decode('latin-1')
                    if s.lower().endswith('.bmp') and s not in seen:
                        portraits.append(s)
                        seen.add(s)
                except UnicodeDecodeError:
                    pass
                k += 1 + n
                continue
        k += 1
    r['portrait_bmps'] = portraits
    return r


def parse_races():
    """Parse every RACE.DAT record into a race dict (see parse_race_record),
    dropping the CD-ROM character-generator placeholder records (zeroed-stat
    "Standard half-*" entries that only redirect to a real "Half-*" parent)."""
    buf = _load_dat('RACE.DAT')
    if not buf: return []
    _, _, _, header_end = parse_mfc_header(buf)
    markers = find_records(buf, header_end)
    out = []
    for i, start in enumerate(markers):
        end = markers[i+1] - 2 if i+1 < len(markers) else len(buf)
        r = parse_race_record(buf, start, end)
        if not r: continue
        # Drop CD-ROM character-generator placeholders ("Standard half-elf",
        # "Standard half-orc", "Standard half-ogre"). These are not races
        # published in the rulebooks — they're hollow records that point at
        # the real "Half-*" parent via the optional base-race Pascal and
        # carry no stats of their own (all ability ranges zeroed). Filter
        # using only DAT-extracted facts: name + zeroed ranges.
        if r['name'].lower().startswith('standard ') and \
                all(v['min'] == 0 and v['max'] == 0
                    for v in r.get('ability_ranges', {}).values()):
            continue
        out.append(r)
    return out


# CLASS.DAT — MFC CArchive, CClassOb records
def parse_class_record(buf, start, end):
    """Parse one CClassOb record (a class). Validates the per-record "TSRP3 V39"
    version Pascal, then reads name, group id (warrior/rogue/priest/wizard/
    psionicist), and locates two tables by signature scan rather than fixed
    offsets: the XP-by-level table (monotonic int run) and the THAC0 table. Also
    extracts the per-level saving-throw matrix (`save_table`) located by the
    Normal-Man baseline row [16,18,17,20,19] — that table is later read at
    level=HD to derive monster saves. Returns None if the
    version marker or name is missing."""
    p = start
    r = {}
    # "TSRP3 V39" version marker
    ver, p = read_pascal(buf, p)
    if ver != 'TSRP3 V39': return None
    r['name'], p = read_pascal(buf, p)
    if r['name'] is None: return None
    if p + 8 > end: return r
    r['group_id'], r['sub_class_id'] = struct.unpack_from('<2i', buf, p)
    gid = r['group_id']
    r['group'] = (['warrior', 'rogue', 'priest', 'wizard', 'psionicist'][gid]
                  if 0 <= gid <= 4 else 'unknown')
    # Proficiency fields live in the 33-int32 header zone between sub_class_id and
    # the XP table. Offsets validated against all 26 classes (see class_probe3.txt):
    #   [23] = NWP starting slots   [24] = NWP gain rate (every N levels)
    #   [27] = WP starting slots    [28] = WP gain rate  (every N levels)
    #   [29] = non-proficiency attack penalty (negative)
    _rel = p - start + 8   # relative offset of first proficiency int32 zone from start
    # Find XP table — sequence of monotonic ints starting with L2 XP
    # Locate by scanning for plausible start (small int < 5000)
    chunk = buf[start:start+1024]
    xp_offset = None
    thaco_offset = None
    for off in range(30, len(chunk) - 4*15):
        try:
            seq = struct.unpack_from('<15i', chunk, off)
        except struct.error:
            continue
        if (10 <= seq[0] <= 5000 and all(seq[k] < seq[k+1] for k in range(14)) and seq[14] < 50_000_000):
            xp_offset = off
            break
    if xp_offset is not None:
        # Proficiency fields at fixed int32 indices from p_data (see _rel above)
        if _rel + 29*4 + 4 <= xp_offset:
            # Rogue skill-point budget: [18]=starting points (level 1), [19]=per-level gain.
            # Non-rogue classes have 0 here. Validated: Thief=60/30, Bard=20/15, others=0/0.
            r['skill_pts_start'] = struct.unpack_from('<i', chunk, _rel + 18*4)[0]
            r['skill_pts_level'] = struct.unpack_from('<i', chunk, _rel + 19*4)[0]
            r['nwp_starting']  = struct.unpack_from('<i', chunk, _rel + 23*4)[0]
            r['nwp_gain_level']= struct.unpack_from('<i', chunk, _rel + 24*4)[0]
            r['wp_starting']   = struct.unpack_from('<i', chunk, _rel + 27*4)[0]
            r['wp_gain_level'] = struct.unpack_from('<i', chunk, _rel + 28*4)[0]
            r['wp_penalty']    = struct.unpack_from('<i', chunk, _rel + 29*4)[0]
        # HD die/count/HP-after live ~72 bytes before XP table (validated for Fighter, Mage, etc.)
        if xp_offset >= 72:
            r['hit_die']         = struct.unpack_from('<i', chunk, xp_offset - 72)[0]
            r['hit_dice_cap']    = struct.unpack_from('<i', chunk, xp_offset - 68)[0]
            r['hp_after_cap']    = struct.unpack_from('<i', chunk, xp_offset - 64)[0]
        # XP table = 99 int32
        if xp_offset + 99*4 <= len(chunk):
            r['xp_table'] = list(struct.unpack_from('<99i', chunk, xp_offset))
        # THAC0 table follows immediately
        thaco_offset = xp_offset + 99*4
        if start + thaco_offset + 99*4 <= len(buf):
            r['thaco_table'] = list(struct.unpack_from('<99i', buf, start + thaco_offset))
        # Spell-slot table — two stacked 99-row tables in the post-THAC0 zone,
        # 44-byte stride (11 int32 per row; cols 0-8 = slots for spell levels
        # 1..9, cols 9-10 padding). Table 0 begins 56 B after the THAC0 table;
        # Table 1 begins 101 rows further. In both, row i = character level i+1.
        # Full casters (priests, all wizard kinds, Paladin) fill Table 0; Bard
        # and Ranger leave Table 0 empty and fill Table 1. Prefer the first
        # table that holds a sane progression (read at level=L in _build_class_
        # ranks). Validated vs PHB 2e: Mage/Cleric/Druid L1, Paladin L9, Ranger
        # L8, Bard L2 first-spell levels all exact.
        spell_base = start + thaco_offset + 99*4 + 56

        def _read_spell_rows(base_off):
            rows = []
            o = base_off
            for _ in range(99):
                if o + 44 > end:
                    break
                rows.append(list(struct.unpack_from('<9i', buf, o)))
                o += 44
            return rows

        def _sane_spell_table(rows):
            # Guard against picking up unrelated bytes: the leading rows must be
            # a clean slot table (every value 0..9) with at least one slot.
            head = rows[:25]
            if not any(any(v > 0 for v in row) for row in head):
                return False
            return all(0 <= v <= 9 for row in head for v in row)

        t0 = _read_spell_rows(spell_base)
        if _sane_spell_table(t0):
            r['spell_table'] = t0
        else:
            t1 = _read_spell_rows(spell_base + 101 * 44)
            if _sane_spell_table(t1):
                r['spell_table'] = t1
    # Save table — 100 rows × 5 int32 (Normal Man + 99 levels).
    # Columns: [paralyze/poison/death, rod/staff/wand, petrify/polymorph, breath, spell]
    # Offset 15806 bytes from thaco_offset is constant for all 26 classes on the
    # AD&D 2e Core Rules CD-ROM expansion (validated Fighter → Air Elementalist).
    if thaco_offset is not None:
        sp = start + thaco_offset + 15806
        if sp + 100 * 20 <= end:
            rows = []
            o = sp
            while len(rows) < 100 and o + 20 <= end:
                rows.append(list(struct.unpack_from('<5i', buf, o)))
                o += 20
            if len(rows) >= 2:
                r['save_table'] = rows
    return r


def parse_classes():
    """Parse every CLASS.DAT record (see parse_class_record). Record boundaries
    are found by scanning for the "TSRP3 V39" version Pascal rather than the
    generic find_records tag, because class records lead with that marker."""
    buf = _load_dat('CLASS.DAT')
    if not buf: return []
    # Class records start with "TSRP3 V39" Pascal — find via marker
    marker = b'\x09TSRP3 V39'
    starts = []
    i = 0
    while True:
        j = buf.find(marker, i)
        if j < 0: break
        starts.append(j)
        i = j + 1
    starts.append(len(buf))
    out = []
    for k in range(len(starts) - 1):
        r = parse_class_record(buf, starts[k], starts[k+1])
        if r: out.append(r)
    return out


# PARTS.DAT — MFC CArchive, CPart + CPartKitOb records
# Known class names that appear as embedded references (to filter)
_PART_CLASS_NAMES = {'Fighter','Paladin','Ranger','Thief','Bard','Cleric','Druid','Mage',
    'Abjurer','Conjurer','Diviner','Enchanter','Illusionist','Invoker','Necromancer','Transmuter',
    'Alchemist','Geometer','Shadow Mage','Song Wizard','Wild Mage','Psionicist',
    'Fire Elementalist','Earth Elementalist','Water Elementalist','Air Elementalist'}


def parse_part_record(buf, start, end):
    """Parse a CPart record. Returns a dict of extractable fields."""
    p = start
    r = {}
    r['name'], p = read_pascal(buf, p)
    if r['name'] is None: return None
    # Skip embedded class names (filter from record list separately)
    if r['name'] in _PART_CLASS_NAMES: return None
    post = p
    if post + 250 > end:
        return r

    # Item category code (uint16 at +148) — high byte = category, low byte = sub-id.
    # NOT a direct EQUIP slot index; kept for reference only.
    if post + 148 + 2 <= end:
        r['item_id'] = struct.unpack_from('<H', buf, post + 148)[0]

    # Icon ID — a 1-3 digit ASCII Pascal string stored between the uint16 and the
    # description's MFC long-string marker (0xFF). Value is the 1-indexed slot into
    # the EQUIP/E{N}_{N+49}.BMP sprite sheets (1..700). Visually validated against
    # bows, swords, shields, potions, mounts, magic items, etc.
    icon_id = None
    desc_off = None
    p = post + 148 + 2
    while p < end - 2:
        if buf[p] == 0xFF:
            length = struct.unpack_from('<H', buf, p+1)[0]
            if 100 <= length <= 5000 and p + 3 + length <= end:
                desc_off = p
                break
        p += 1
    if desc_off is not None:
        i = post + 148 + 2
        while i < desc_off:
            n = buf[i]
            if 1 <= n <= 4 and i+1+n <= desc_off:
                seq = buf[i+1:i+1+n]
                if all(48 <= b <= 57 for b in seq):
                    try:
                        v = int(seq.decode('ascii'))
                        if 1 <= v <= 700:
                            icon_id = v
                            break
                    except ValueError:
                        pass
            i += 1
    r['icon_id'] = icon_id
    # Weight (float32 at +153) — may be wrong for fractional-weight items
    if post + 153 + 4 <= end:
        try:
            w = struct.unpack_from('<f', buf, post + 153)[0]
            if 0 < w < 1000:
                r['weight'] = round(w, 2)
        except struct.error:
            pass
    # Cost (byte at +165, gp)
    if post + 165 < end:
        c = buf[post + 165]
        if 0 <= c <= 255:
            r['cost_gp'] = c
    # Hands (byte at +209)
    if post + 209 < end:
        h = buf[post + 209]
        if h in (1, 2):
            r['handedness'] = h

    # Search the record for the TSRP3 V7 landmark
    rec_data = buf[start:end]
    v7_pos = rec_data.find(b'\x08TSRP3 V7')

    # Damage type doubled pattern (simple weapons). Capture the single-char match
    # position (dmg_pos) — the dice fields hang off it (see §3.3 "damage zone").
    dmg_pos = None
    for dt in ['B', 'P', 'S']:
        needle = bytes([1, ord(dt), 1, ord(dt)])
        dp = rec_data.find(needle)
        if 300 <= dp <= 450:
            r['damage_type'] = dt
            dmg_pos = dp
            break
    else:
        # Try multi-char damage types (Halberd P/S). The longer Pascal pair shifts
        # the dice layout, so we don't read dice off these (left blank, not guessed).
        for dt in ['P/S', 'B/P', 'B/S']:
            needle = bytes([len(dt)]) + dt.encode('latin-1')
            needle *= 2
            dp = rec_data.find(needle)
            if 300 <= dp <= 450:
                r['damage_type'] = dt
                break

    # Size category doubled pattern (S/M/L/G/T). size_pos anchors the size_S die.
    size_pos = None
    for sz in ['S', 'M', 'L', 'G', 'T', 'H']:
        needle = bytes([1, ord(sz), 1, ord(sz)])
        sp = rec_data.find(needle, 380)
        if sp > 0:
            r['size_category'] = sz
            size_pos = sp
            break

    # Damage dice (DAT-first, §3.3 "damage zone"). The doubled int32 dice fields
    # hang off the single-char damage-type pair (count_S@+16, count_L@+20,
    # size_L@+24) and the size-category pair (size_S@+4). Emit only when both
    # anchors were found and the values are sane dice — never fabricate.
    if dmg_pos is not None and size_pos is not None:
        try:
            count_S = struct.unpack_from('<i', rec_data, dmg_pos + 16)[0]
            count_L = struct.unpack_from('<i', rec_data, dmg_pos + 20)[0]
            size_L  = struct.unpack_from('<i', rec_data, dmg_pos + 24)[0]
            size_S  = struct.unpack_from('<i', rec_data, size_pos + 4)[0]
            if 1 <= count_S <= 6 and size_S in _DIE_SIZES:
                r['dmg_normal'] = f"{count_S}d{size_S}"
            if 1 <= count_L <= 6 and size_L in _DIE_SIZES:
                r['dmg_large'] = f"{count_L}d{size_L}"
        except struct.error:
            pass

    # Weapon speed factor (byte; melee slot at V7-82, missile slot at V7-92 — only
    # one is set per weapon, so take the non-zero of the two). DAT-sourced.
    if v7_pos >= 92:
        speed = max(rec_data[v7_pos - 82], rec_data[v7_pos - 92])
        if 0 < speed <= 20:
            r['speed'] = speed

    # ROF text "N/rnd" or "N/D rnd" — scan all medium Pascal strings
    for k in range(len(rec_data) - 1):
        n = rec_data[k]
        if 4 <= n <= 10 and k+1+n <= len(rec_data):
            seq = rec_data[k+1:k+1+n]
            try:
                s = seq.decode('latin-1')
                m = re.match(r'^(\d+)(?:/(\d+))? rnd$', s)
                if m:
                    r['rof'] = {'num': int(m.group(1)), 'den': int(m.group(2)) if m.group(2) else 1}
                    break
            except (UnicodeDecodeError, ValueError):
                pass

    # Magic enchantment bonus int32 @+438 (weapons)
    if post + 438 + 4 <= end:
        try:
            mag = struct.unpack_from('<i', buf, post + 438)[0]
            if -5 <= mag <= 5:
                r['magic_bonus'] = mag
        except struct.error:
            pass

    # Armor AC — landmark float32 -1.0 (bytes 00 00 80 BF)
    ac_marker = rec_data.find(b'\x00\x00\x80\xBF')
    if ac_marker > 8:
        ac_pos = ac_marker - 8
        try:
            ac_val = struct.unpack_from('<i', rec_data, ac_pos)[0]
            # Base armor AC is 1-10; ac=0 here is a spurious marker hit (it turns
            # up in many weapon/misc records), so require >=1 to call it armor.
            if 1 <= ac_val <= 10:
                r['armor_class'] = ac_val
                r['is_armor'] = True
                # Magic bonus for armor at AC_pos + 183
                if ac_pos + 183 + 4 <= len(rec_data):
                    mag_a = struct.unpack_from('<i', rec_data, ac_pos + 183)[0]
                    if -5 <= mag_a <= 5:
                        r['magic_bonus'] = mag_a
        except struct.error:
            pass

    # Restricted classes (Pascal strings in tail, last ~200 bytes)
    restricted = []
    seen = set()
    tail_start = max(0, len(rec_data) - 250)
    k = tail_start
    while k < len(rec_data) - 1:
        n = rec_data[k]
        if 3 <= n <= 25 and k+1+n <= len(rec_data):
            seq = rec_data[k+1:k+1+n]
            try:
                s = seq.decode('latin-1')
                if s in _PART_CLASS_NAMES and s not in seen:
                    restricted.append(s)
                    seen.add(s)
                k += 1 + n
                continue
            except UnicodeDecodeError:
                pass
        k += 1
    r['restricted_classes'] = restricted

    return r


def parse_parts():
    """Parse every PARTS.DAT record (weapons, armor, magic items, gems, …) via
    parse_part_record. ~4584 records (the header count of 733 is the original
    base-item count, before magic variants)."""
    buf = _load_dat('PARTS.DAT')
    if not buf: return []
    _, _, _, header_end = parse_mfc_header(buf)
    markers = find_records(buf, header_end)
    out = []
    for i, start in enumerate(markers):
        end = markers[i+1] - 2 if i+1 < len(markers) else len(buf)
        r = parse_part_record(buf, start, end)
        if r: out.append(r)
    return out


# PARTS.DAT character kits (CPartKitOb) — see the comment block on parse_kits().
# Section labels used to locate where a kit's own prose begins (the bytes before
# it are the previous kit's overflow). These are generic English field labels in
# the user's data (like the MM stat-block labels), used purely as parse anchors —
# no copyrighted content is embedded.
_KIT_LABEL_RE = re.compile(
    rb'(Benefits/Hindrances|Special Benefits|Special Hindrances|Benefits|Hindrances'
    rb'|Description|Role|Weapon Proficiencies|Nonweapon Proficiencies|Bonus Proficiencies'
    rb'|Recommended[^:]*|Barred[^:]*|Preferred[^:]*|Secondary Skills|Requirements?)\s*:',
    re.I)
_KIT_STR_RE = re.compile(rb'[\x20-\x7e]{3,}')


def parse_kits():
    """Parse the character-kit records (`CPartKitOb`) appended to PARTS.DAT.

    These are the Complete-Handbook / Player's-Option character kits (Amazon,
    Assassin, Swashbuckler, the race-specific Dwarf/Gnome/Elf/Halfling kits, …)
    that the CD-ROM's character generator offers. They are NOT covered by the
    generic find_records() walk used for items, so they were never migrated.

    Format (reverse-engineered): the kit region starts at the first `CPartKitOb`
    class marker. Each kit's serialized body begins with a 4-byte `CPKO` signature
    (1:1 with kits, in order) and ends with a `TSRP3 V7` version stamp immediately
    followed by the kit name as a printable string. Between the `CPKO` header and
    that trailer sit option lists (allowed weapons / proficiencies / wealth) and
    the kit's narrative (Benefits/Hindrances/Description/Role…).

    Two wrinkles handled here:
      • A long kit description spills *past* the next `CPKO` marker, so `CPKO`
        is only the record START — the previous kit's prose tail bleeds in at the
        top of the next record. We therefore take prose from the FIRST section
        label (see _KIT_LABEL_RE) up to the `TSRP3 V7` trailer; the unlabeled
        bleed before that label is dropped.
      • The trailer name sometimes carries a leading sort-key punctuation char
        (`!`, `%`, `/`, `,`, `*`); we strip leading non-alphanumerics. The
        placeholder kit literally named "None" and any record with no real prose
        (<40 chars, e.g. a trailing currency entry) are skipped.

    All names and prose are read from the user's PARTS.DAT at runtime — nothing
    is embedded. Returns a list of {name, text} dicts (text = raw prose).
    """
    buf = _load_dat('PARTS.DAT')
    if not buf: return []
    start = buf.find(b'CPartKitOb')
    if start < 0: return []
    region = buf[start:]
    cpko = [m.start() for m in re.finditer(rb'CPKO', region)] + [len(region)]
    kits = []
    for i in range(len(cpko) - 1):
        rec = region[cpko[i]:cpko[i+1]]
        cut = rec.find(b'TSRP3 V7')
        if cut < 0:
            continue
        nm = _KIT_STR_RE.search(rec[cut+8:])
        if not nm:
            continue
        name = re.sub(r'^[^0-9A-Za-z]+', '', nm.group().decode('latin-1')).strip()
        name = re.sub(r'\s*\(CRE\)\s*$', '', name, flags=re.I).strip()
        if not name or name.lower() == 'none':
            continue
        lm = _KIT_LABEL_RE.search(rec, 0, cut)
        body = rec[lm.start():cut] if lm else rec[:cut]
        blocks = [m.group().decode('latin-1')
                  for m in _KIT_STR_RE.finditer(body) if len(m.group()) >= 30]
        text = ' '.join(blocks).strip()
        if len(text) < 40:
            continue
        kits.append({'name': name, 'text': text})
    return kits


# ── Kit → handbook HTM matching (mandatory-vs-optional proficiency split) ──────
# PARTS.DAT does NOT uniformly flag which kit proficiencies are *bonus* (granted
# free = mandatory) vs *recommended* (player's choice = optional) — some handbooks
# grant bonus profs (bards, warriors), others only list allowed/recommended ones
# (thieves). The Complete-Handbook HTM pages, however, label this explicitly:
# "Nonweapon Proficiencies: Bonuses: …" (mandatory) vs "Suggested:"/"Recommended:"
# and "Weapon Proficiencies:" (a 'must select from' restriction = optional choice).
# Each kit is one HTM file with the kit name in <TITLE>, so we match the DAT kit
# name to that page, use the full prose as the description (optional info included
# inline), and auto-grant ONLY the bonus profs. All text read from the user's HTM
# at runtime — only the book keys / generic class labels are hard-coded.
_KIT_CLASS_BOOKS = [   # class-specific kit handbooks → class label for pack folders
    ('CFH', 'Fighter'), ('CBH', 'Bard'), ('CBT', 'Thief'), ('CWH', 'Wizard'),
    ('CPRH', 'Priest'), ('CDH', 'Druid'), ('CRH', 'Ranger'), ('CPAH', 'Paladin'),
]
_KIT_RACE_BOOKS = ['CBD', 'CBGH']   # race handbooks: kits span classes (no single class)

# Generic class keywords for kits with no class-handbook match (race-book / S&P /
# unmatched). Whole-substring match on the kit name — generic class words only.
_KIT_CLASS_KEYWORDS = [
    ('priest', 'Priest'), ('cleric', 'Priest'), ('votary', 'Priest'),
    ('druid', 'Druid'), ('ranger', 'Ranger'),
    ('paladin', 'Paladin'), ('chevalier', 'Paladin'),
    ('wizard', 'Wizard'), ('mage', 'Wizard'), ('sorcer', 'Wizard'),
    ('witch', 'Wizard'), ('wu jen', 'Wizard'),
    ('bard', 'Bard'), ('minstrel', 'Bard'), ('chanter', 'Bard'), ('whistler', 'Bard'),
    ('thief', 'Thief'), ('burglar', 'Thief'), ('smuggler', 'Thief'),
    ('bandit', 'Thief'), ('cutpurse', 'Thief'), ('fence', 'Thief'),
    ('fighter', 'Fighter'), ('warrior', 'Fighter'), ('gladiator', 'Fighter'),
    ('myrmidon', 'Fighter'), ('soldier', 'Fighter'),
]


def _kit_match_norm(s):
    """Normalize for kit-title / qualifier matching: DROP parenthetical content
    (book suffixes like '(Comp. Bard's Handbook)', flavor notes like
    '(native tongue)'), then lowercase + alnum-tokenize."""
    s = re.sub(r'\([^)]*\)', ' ', s)
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', s.lower())).strip()


def _kit_prof_norm(s):
    """Normalize for proficiency-name matching: KEEP parenthetical content (it can
    be the discriminator, e.g. 'Riding (Land-Based)' → 'Riding, Land-Based'),
    turning punctuation into spaces."""
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', s.lower())).strip()


_kit_html_index_cache = None


def _kit_html_index():
    """Build (and cache) {normalized kit name → (book_key, filepath)} from the kit
    handbooks' HTM <TITLE>s. Each kit is one file titled '<Kit> (Comp. … Handbook)'."""
    global _kit_html_index_cache
    if _kit_html_index_cache is not None:
        return _kit_html_index_cache
    idx = {}
    for book in [b for b, _ in _KIT_CLASS_BOOKS] + _KIT_RACE_BOOKS:
        d = os.path.join(SOURCE_BASE, book)
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if not f.upper().endswith('.HTM'):
                continue
            try:
                with open(os.path.join(d, f), encoding='latin-1') as fh:
                    head = fh.read(1200)
            except OSError:
                continue
            m = re.search(r'<TITLE>(.*?)</TITLE>', head, re.I | re.S)
            if not m:
                continue
            key = _kit_match_norm(m.group(1))
            if key:
                idx.setdefault(key, (book, os.path.join(d, f)))
    _kit_html_index_cache = idx
    return idx


def _match_kit_page(name):
    """Match a DAT kit name to its handbook HTM page. Tries the full name, then the
    name minus a trailing ', <Race>'/', <Class>' qualifier. Returns (book, path)."""
    idx = _kit_html_index()
    base = re.sub(r'\(cre\)', '', name, flags=re.I)
    cands = [base]
    parts = [p.strip() for p in base.split(',')]
    if len(parts) > 1:
        cands.append(parts[0])
    for c in cands:
        hit = idx.get(_kit_match_norm(c))
        if hit:
            return hit
    return None


_KIT_BONUS_RE = re.compile(r'Bonus Proficiencies\s*:|Bonus(?:es)?\b\s*:', re.I)
_KIT_NEXT_LABEL_RE = re.compile(
    r'(Suggested|Recommended|Required|Allowed|Barred|Armor|Equipment|Special|Notes|'
    r'Weapon Prof|Nonweapon Prof)\b\s*:', re.I)
_KIT_PROF_GROUP_RE = re.compile(
    r'^\((?:warrior|wizard|priest|rogue|general|psionicist)\)\s*', re.I)


def _split_profs(seg):
    """Paren-aware split of a 'A, B (x, y), C' proficiency list on top-level
    commas/semicolons/periods (so the comma inside '(Land-based, horse)' is kept)."""
    out, depth, cur = [], 0, []
    for ch in seg:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth = max(0, depth - 1)
        if ch in ',;.' and depth == 0:
            tok = ''.join(cur).strip()
            if tok:
                out.append(tok)
            cur = []
        else:
            cur.append(ch)
    tok = ''.join(cur).strip()
    if tok:
        out.append(tok)
    return [t for t in out if 1 < len(t) <= 45]


def _kit_bonus_profs(filepath):
    """Extract the mandatory 'Bonus' nonweapon proficiencies a kit's HTM page grants
    free of slot cost ('Nonweapon Proficiencies: Bonuses: …'). Returns a name list
    (empty when the handbook grants none, e.g. thief kits)."""
    try:
        with open(filepath, encoding='latin-1') as fh:
            raw = fh.read()
    except OSError:
        return []
    text = ' '.join(BeautifulSoup(raw, 'html.parser').get_text(' ').split())
    m = _KIT_BONUS_RE.search(text)
    if not m:
        return []
    seg = text[m.end():]
    nx = _KIT_NEXT_LABEL_RE.search(seg)
    if nx:
        seg = seg[:nx.start()]
    return _split_profs(seg[:300])


# Race-book (CBD/CBGH) kits are grouped under class chapters ("Warrior Kits",
# "Priest Kits", …); the kit name carries no class, so we read the class from the
# chapter the kit's file falls in. Generic class words only — no copyrighted data.
_RACE_KIT_CHAPTER_RE = re.compile(
    r'\b(Warrior|Fighter|Priest|Cleric|Thief|Rogue|Wizard|Mage|Druid|Ranger|Paladin|Bard)\b'
    r'[\w/ ]*?\bKits?\b', re.I)
_CLASS_CANON = {'warrior': 'Fighter', 'fighter': 'Fighter', 'priest': 'Priest',
                'cleric': 'Priest', 'thief': 'Thief', 'rogue': 'Thief',
                'wizard': 'Wizard', 'mage': 'Wizard', 'druid': 'Druid',
                'ranger': 'Ranger', 'paladin': 'Paladin', 'bard': 'Bard'}
_race_book_class_cache = {}


def _race_book_class_map(book):
    """For a race handbook (CBD/CBGH), map each file path → the class of the kit
    chapter it belongs to, by scanning titles for '<Class> Kits' chapter headings
    and carrying that class forward to subsequent files. Cached per book."""
    if book in _race_book_class_cache:
        return _race_book_class_cache[book]
    d = os.path.join(SOURCE_BASE, book)
    mapping, cur = {}, None
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if not f.upper().endswith('.HTM'):
                continue
            try:
                with open(os.path.join(d, f), encoding='latin-1') as fh:
                    head = fh.read(1200)
            except OSError:
                continue
            m = re.search(r'<TITLE>(.*?)</TITLE>', head, re.I | re.S)
            if m:
                cm = _RACE_KIT_CHAPTER_RE.search(m.group(1))
                if cm:
                    cur = _CLASS_CANON.get(cm.group(1).lower())
            if cur:
                mapping[os.path.join(d, f)] = cur
    _race_book_class_cache[book] = mapping
    return mapping


def _kit_class(name, book, filepath=None):
    """Folder/class label for a kit: its class-handbook's class if matched in one;
    for race handbooks, the class of the kit's chapter; else a generic class
    keyword in the name; else 'General'."""
    cls = dict(_KIT_CLASS_BOOKS).get(book) if book else None
    if cls:
        return cls
    if book in _KIT_RACE_BOOKS and filepath:
        cls = _race_book_class_map(book).get(filepath)
        if cls:
            return cls
    low = name.lower()
    for kw, c in _KIT_CLASS_KEYWORDS:
        if kw in low:
            return c
    return 'General'


# TREASURE.DAT — MFC CArchive, CTreasureSingleTableOb records: a table
# name followed by a list of CTreasureTableEntry objects. Each entry is framed
# `<mfc-tag> <uint32 weight> 01 <Pascal target>` — the weight is the d100
# percentage (validated: the top "DMG 88" table's 18 entry weights sum to 100),
# and the target is either a sub-table name ("DMG 89: …") or a leaf result/item
# name ("Oil of Timelessness"). All values read from the user's DAT at runtime.
def parse_treasure():
    """Parse TREASURE.DAT into [{name, entries:[(weight, target, flag)]}] — DMG
    treasure roll tables (see the format note above).
    `flag` is 1 for a sub-table reference, 0 for a leaf result/item; `weight` is
    the d100 percentage. Each table record is split by find_records; entries are
    located by their `<weight uint32> <flag 0|1> <Pascal target>` framing."""
    buf = _load_dat('TREASURE.DAT')
    if not buf: return []
    _, _, _, header_end = parse_mfc_header(buf)
    markers = find_records(buf, header_end)
    tables = []
    for i, start in enumerate(markers):
        end = markers[i+1] - 2 if i+1 < len(markers) else len(buf)
        name, p = read_pascal(buf, start)
        if not name:
            continue
        entries = []
        j = p
        while j < end - 6:
            # An entry target is a Pascal (len up to ~120 — some table names run
            # long) introduced by a flag byte: 0x01 = reference to a sub-table,
            # 0x00 = a leaf result/item. The uint32 weight sits in the 4 bytes
            # before the flag. The inter-entry data block is zero-filled (its
            # `xx 00…` never satisfies the printable-name test).
            if buf[j] in (0, 1):
                nlen = buf[j+1]
                if 2 <= nlen <= 120 and j+2+nlen <= end and \
                        all(32 <= b < 127 for b in buf[j+2:j+2+nlen]):
                    if j >= 4:
                        weight = struct.unpack_from('<I', buf, j-4)[0]
                        if weight <= 1000:   # sanity: real weights are 1..100
                            entries.append((weight,
                                            buf[j+2:j+2+nlen].decode('latin-1'),
                                            buf[j]))   # flag: 1=subtable, 0=leaf
                            j += 2 + nlen
                            continue
            j += 1
        if entries:
            tables.append({'name': name, 'entries': entries})
    return tables


# SPELLS.DAT — MFC CArchive, CSpellsOb records
_SCHOOL_NAMES = {'Abjuration','Alteration','Conjuration','Divination','Enchantment','Illusion',
    'Invocation','Evocation','Necromancy','Charm','Alchemy','Geometry','Shadow','Song','Wild Magic',
    'Universal','Greater Divination','Lesser Divination','Charm/Enchantment','Conjuration/Summoning',
    'Enchantment/Charm','Invocation/Evocation','Evocation/Invocation','Greater Illusion',
    'Illusion/Phantasm','Artifice','Dimension','Force','Mentalist'}
_SPHERE_NAMES = {'All','Animal','Astral','Chaos','Combat','Creation','Elemental','Elemental Air',
    'Elemental Fire','Elemental Water','Elemental Earth','Elemental All','Guardian','Healing','Law',
    'Necromantic','Numbers','Plant','Protection','Summoning','Sun','Thought','Time','Travelers',
    'War','Wards','Weather'}


def parse_spell_record(buf, start, end):
    """Parse one CSpellsOb record (a spell). Reads name,
    class type (int32 @+8: 1=wizard else priest), level (@+16), then school/
    sphere/range/components/duration/etc. Records whose name is itself a school
    or sphere label are embedded references, not spells, and return None."""
    p = start
    r = {}
    r['name'], p = read_pascal(buf, p)
    if r['name'] is None: return None
    name_collides = r['name'] in _SCHOOL_NAMES or r['name'] in _SPHERE_NAMES
    # 7 zero bytes, then class type int32 @+8 and level int32 @+16 (relative to post-name)
    if p + 20 > end:
        # No spell header present: a real spell always has one, a bare embedded
        # school/sphere reference does not — so drop a name-colliding stub here.
        return None if name_collides else r
    try:
        class_type_id = struct.unpack_from('<i', buf, p + 8)[0]
        level         = struct.unpack_from('<i', buf, p + 16)[0]
    except struct.error:
        return None if name_collides else r
    # A record whose name equals a school/sphere label is an embedded reference,
    # NOT a spell — UNLESS it carries a valid spell header (level 1-9). The two
    # real spells named "Chaos"/"Divination" do (levels 5/4); distinguish by the
    # header, not the name (names are not unique). Fixes GitHub issue #5.
    if name_collides and not (1 <= level <= 9):
        return None
    r['class_type_id'] = class_type_id
    r['level']         = level
    r['class_type'] = 'wizard' if r['class_type_id'] == 1 else 'priest'
    # Walk Pascal strings: area, casting time, components, duration, range, save, school, +
    fields_order = ['area_of_effect', 'casting_time', 'components',
                    'duration', 'range', 'saving_throw', 'school']
    # Skip past the int32 header (20 bytes)
    p = p + 20
    for fname in fields_order:
        # Skip bytes until we find a valid Pascal string start (tolerates non-zero junk).
        # n >= 1 is intentional: casting_time is often a single digit ('1','2','3') and
        # the old n >= 2 filter caused it to be skipped, shifting all subsequent fields.
        while p < end - 1:
            n = buf[p]
            if 1 <= n <= 60 and p+1+n <= end:
                seq = buf[p+1:p+1+n]
                if all(32 <= b < 127 for b in seq):
                    r[fname] = seq.decode('latin-1')
                    p += 1 + n
                    break
            p += 1
        else:
            break  # ran out of buffer
    # Some records embed an extra text field between saving_throw and school (e.g. brief
    # effect summaries like "Restores 1d8 hit points.").  If 'school' landed on a non-
    # school string, scan forward until we find a recognised school/sphere name.
    if r.get('school') and r['school'] not in _SCHOOL_NAMES and r['school'] not in _SPHERE_NAMES:
        while p < end - 1:
            n = buf[p]
            if 1 <= n <= 60 and p+1+n <= end:
                seq = buf[p+1:p+1+n]
                if all(32 <= b < 127 for b in seq):
                    s = seq.decode('latin-1')
                    if s in _SCHOOL_NAMES or s in _SPHERE_NAMES:
                        r['school'] = s
                        p += 1 + n
                        break
            p += 1
    # Collect remaining school/sphere references
    extras = []
    while p < end - 1:
        n = buf[p]
        if 2 <= n <= 50 and p+1+n <= end:
            seq = buf[p+1:p+1+n]
            try:
                s = seq.decode('latin-1')
                if all(c.isprintable() for c in s):
                    extras.append(s)
                    p += 1 + n
                    continue
            except UnicodeDecodeError:
                pass
        p += 1
    r['extra_schools_spheres'] = extras
    return r


def parse_spells():
    """Parse every SPELLS.DAT record (wizard + priest spells) via
    parse_spell_record. ~931 spells; record order is stable and is the basis for
    the hard-coded spell indices (_SPELL_ICON_INDEX, _SPELL_REVERSE_INDEX, …)."""
    buf = _load_dat('SPELLS.DAT')
    if not buf: return []
    _, _, _, header_end = parse_mfc_header(buf)
    markers = find_records(buf, header_end)
    out = []
    for i, start in enumerate(markers):
        end = markers[i+1] - 2 if i+1 < len(markers) else len(buf)
        r = parse_spell_record(buf, start, end)
        if r: out.append(r)
    return out


# MONSTER.DAT — MFC CArchive, CMonsterOb records
def parse_monster_record(buf, start, end):
    """Parse one CMonsterOb record (a monster stat block).
    The post-Description-label zone is variable (some records insert a special-
    attacks Pascal), so HD is read at the "Damage" landmark −12 and the int32
    stat block [_, XP, 0, THAC0] is found by signature scan past "Damage". AC/MV
    and No.Appearing come from fixed offsets; damage/#attacks/narrative are
    recovered from the trailing Pascal strings (`tail_strings`)."""
    p = start
    r = {}
    r['name'], p = read_pascal(buf, p)
    if r['name'] is None: return None
    _, p = read_pascal(buf, p)                 # "Description" label
    if p + 12 > end: return r
    p += 12                                    # 3 int32 internal
    r['organization'], p   = read_pascal(buf, p)
    r['activity_cycle'], p = read_pascal(buf, p)
    r['diet'], p           = read_pascal(buf, p)
    r['intelligence'], p   = read_pascal(buf, p)
    if p + 8 > end: return r
    r['noapp_low'], r['noapp_high'] = struct.unpack_from('<2i', buf, p); p += 8
    if p + 8 > end: return r
    r['ac'], r['mv'] = struct.unpack_from('<2i', buf, p); p += 8
    # Find "Damage" label landmark
    dmg_pos = buf.find(b'\x06Damage', start, end)
    if dmg_pos > 0:
        try:
            if dmg_pos >= start + 12:
                hd = struct.unpack_from('<i', buf, dmg_pos - 12)[0]
                if 0 < hd < 100:
                    r['hit_dice'] = hd
            # After "Damage\x00" the layout is one of:
            #   (A) immediate stat block: [int32 mystery, int32 XP, int32 0, int32 THAC0, ...]
            #   (B) a "special attacks" Pascal description first, then the same stat block
            # Both are covered by scanning byte-by-byte for the 4-int32 signature
            #   f0 ∈ [0, 200], f1 (XP) ∈ [0, 1_000_000], f2 == 0, f3 (THAC0) ∈ [-10, 25].
            # Validated visually: returns correct THAC0 / XP for Ankheg (17, 270),
            # Argos (15, 2000), Faerie Dragon (17, 3000), Pit Fiend (high HD), etc.
            scan_p = dmg_pos + 7
            scan_end = min(end - 16, dmg_pos + 800)   # local window
            while scan_p < scan_end:
                f0 = struct.unpack_from('<i', buf, scan_p)[0]
                f1 = struct.unpack_from('<i', buf, scan_p + 4)[0]
                f2 = struct.unpack_from('<i', buf, scan_p + 8)[0]
                f3 = struct.unpack_from('<i', buf, scan_p + 12)[0]
                if (0 <= f0 < 200 and 0 <= f1 <= 1_000_000 and f2 == 0
                        and -10 <= f3 <= 25):
                    r['xp']    = f1
                    r['thaco'] = f3
                    break
                scan_p += 1
        except struct.error:
            pass
    # Walk Pascals starting just after the int32 zone (XP/THAC0 + extras = ~28 bytes)
    # Display name = genus (first word/segment of full name, before any comma)
    genus = r['name'].split(',')[0].strip() if r['name'] else ''
    r['display_name'] = genus     # default; refined below if Pascal found

    # Search for the actual genus Pascal in the second half of the record (post-Damage area)
    if genus and len(genus) <= 60:
        needle = bytes([len(genus)]) + genus.encode('latin-1', 'ignore')
        gp = buf.find(needle, dmg_pos if dmg_pos > 0 else start, end)
        if gp > 0:
            # Walk Pascals from genus position: name, alignment, climate, ...
            wp = gp
            pascals_after = []
            while wp < end - 1 and len(pascals_after) < 6:
                n = buf[wp]
                if 2 <= n <= 80 and wp+1+n <= end:
                    seq = buf[wp+1:wp+1+n]
                    try:
                        s = seq.decode('latin-1')
                        if all(32 <= b < 127 for b in seq):
                            if s != 'String':
                                pascals_after.append(s)
                            wp += 1 + n
                            continue
                    except UnicodeDecodeError:
                        pass
                wp += 1
            if len(pascals_after) >= 1: r['display_name']    = pascals_after[0]
            if len(pascals_after) >= 2: r['alignment']       = pascals_after[1]
            if len(pascals_after) >= 3: r['climate_terrain'] = pascals_after[2]

    # No THAC0 fallback computation: the "20 - HD" formula is itself rules data
    # (PHB Table 38 / DMG). When MONSTER.DAT bytes don't yield a THAC0 value,
    # we leave the field unset rather than synthesize one.
    # Tail: damage notation, # attacks, SA, SD
    rec_data = buf[start:end]
    tail_strs = []
    tail_start = max(0, len(rec_data) - 250)
    k = tail_start
    while k < len(rec_data) - 1:
        n = rec_data[k]
        if 1 <= n <= 100 and k+1+n <= len(rec_data):
            seq = rec_data[k+1:k+1+n]
            try:
                s = seq.decode('latin-1')
                if all(32 <= b < 127 for b in seq):
                    tail_strs.append(s)
                    k += 1 + n
                    continue
            except UnicodeDecodeError:
                pass
        k += 1
    # Filter out "String" placeholders; keep meaningful tail entries
    real_tail = [s for s in tail_strs if s != 'String' and s != r.get('display_name')]
    r['tail_strings'] = real_tail
    # Heuristic field detection in tail
    for s in real_tail:
        if re.match(r'^\d+(?:[-x]\d+)?', s) and 'damage' not in r:
            r['damage'] = s
        elif re.match(r'^\d+( or \d+)?$', s) and 'num_attacks' not in r:
            r['num_attacks'] = s
    return r


def parse_monsters():
    """Parse every MONSTER.DAT record (~1524 monsters incl. HD variants) via
    parse_monster_record."""
    buf = _load_dat('MONSTER.DAT')
    if not buf: return []
    _, _, _, header_end = parse_mfc_header(buf)
    markers = find_records(buf, header_end)
    out = []
    for i, start in enumerate(markers):
        end = markers[i+1] - 2 if i+1 < len(markers) else len(buf)
        r = parse_monster_record(buf, start, end)
        if r: out.append(r)
    return out


# PSIONIC.DAT — MFC CArchive, CPsionicPowerOb records
def parse_psionic_record(buf, start, end):
    """Parse one CPsionicPowerOb record (a psionic power).
    The fields are stored as short Pascal strings before the long-string (0xFF)
    description marker: [0] power score "N/D", [1] range, [2] area of effect; the
    prerequisite is a short Pascal scanned backwards from the record end."""
    p = start
    r = {}
    r['name'], p = read_pascal(buf, p)
    if r['name'] is None: return None
    # Binary header: int32 seq_id at +0, int32 discipline at +4, int32 sci/dev at +8
    if p + 8 <= len(buf):
        r['discipline'] = struct.unpack_from('<I', buf, p + 4)[0]
    # Walk SHORT Pascal fields, stopping at the first MFC long string (0xFF marker)
    short_pascals = []
    desc_start = None
    while p < end - 1:
        n = buf[p]
        # Stop if we hit a long-string marker (the description follows)
        if n == 0xFF and p + 3 < end:
            desc_start = p
            break
        if 2 <= n <= 254 and p+1+n <= end:
            seq = buf[p+1:p+1+n]
            # Allow CR/LF/TAB inside description text alongside printable ASCII
            if all(b in (9, 10, 13) or (32 <= b < 127) for b in seq):
                short_pascals.append(seq.decode('latin-1'))
                p += 1 + n
                continue
        p += 1
    # Read the long description pascal that follows the 0xFF marker (> 254 chars)
    if desc_start is not None:
        desc, _ = read_mfc_long_pascal(buf, desc_start)
        if desc and len(desc) > 5:
            r['description'] = desc.strip()
    # Field assignment based on observed structure:
    # [0] = power score "N/D" or "N+/D+"
    # [1] = range (e.g. "50 yards", "Unlimited", "Personal")
    # [2] = area of effect (often "Personal", "20 yards", "individual")
    if len(short_pascals) >= 1:
        s0 = short_pascals[0]
        if re.match(r'^\d+\+?\s*/\s*\d+\+?', s0):
            r['power_score'] = s0
    if len(short_pascals) >= 2: r['range'] = short_pascals[1]
    # Fields [2]+ may be aoe and/or a short description (< 80 chars). Distinguish
    # by length: descriptions are typically >= 40 chars; aoe names are shorter.
    # Long pascal descriptions (> 80 chars) are handled separately above.
    for s in short_pascals[2:]:
        if len(s) >= 40:
            if 'description' not in r:
                r['description'] = s
        else:
            if 'area_of_effect' not in r:
                r['area_of_effect'] = s
    # Try to find prerequisite as a SHORT Pascal AFTER the long description (near record end)
    # Scan backwards from end for a Pascal of length 4-30 that's not text-fragment-like
    for k in range(end - 1, max(start, end - 60), -1):
        n = buf[k] if k < end else 0
        if 4 <= n <= 30 and k+1+n <= end:
            seq = buf[k+1:k+1+n]
            if all(32 <= b < 127 for b in seq):
                cand = seq.decode('latin-1')
                # Plausible prereq: title-cased single phrase
                if cand and cand[0].isupper() and ' ' not in cand[:20]:
                    r['prerequisite'] = cand
                    break
    return r


def parse_psionics():
    """Parse every PSIONIC.DAT record (~231 powers) via parse_psionic_record."""
    buf = _load_dat('PSIONIC.DAT')
    if not buf: return []
    _, _, _, header_end = parse_mfc_header(buf)
    markers = find_records(buf, header_end)
    out = []
    for i, start in enumerate(markers):
        end = markers[i+1] - 2 if i+1 < len(markers) else len(buf)
        r = parse_psionic_record(buf, start, end)
        if r: out.append(r)
    return out


# ─── HTML lookup for descriptions ─────────────────────────────────────────────

_html_indices = {}     # cache: (book_key, prefix) → {lowercased_name: filepath}


_PER_SPELL_TITLE_RE = re.compile(
    r'--\s*(?:\w+(?:st|nd|rd|th)|\d+(?:st|nd|rd|th))[- ]?[Ll]evel\s+(Wizard|Priest)\s+Spell',
    re.I,
)

# A spell anchored inside a multi-spell compilation page (typically the
# "Nth-Level Spells -- Wizard" page in PHB), with format:
#   <A NAME="hex_id"></A><B>Spell Name</B>
# Some pages wrap the <B> tag in a <FONT>; the regex tolerates that.
# Also tolerates an optional </FONT> between the anchor and the spell FONT:
# PHB compilation pages close the previous section's FONT before the spell FONT,
# so the first spell in each level group has </FONT><FONT ...><B>Name</B>.
_ANCHORED_SPELL_RE = re.compile(
    r'<A\s+NAME="([^"]+)"\s*></A>'   # the anchor
    r'(?:\s*</FONT>)?'               # optional closing FONT (first-in-series pages)
    r'(?:\s*<FONT[^>]*>)?'           # optional opening FONT wrapper
    r'\s*<B>\s*([^<]+?)\s*</B>',     # the spell name
    re.I,
)

# Complete Handbook (CWH/CFH/CBT/CPRH) spell pages list spells as blue SIZE=3
# bold text WITHOUT <A NAME> anchors. E.g. CWH00182:
#   <FONT COLOR="#0000ff" SIZE="3"><B>Blackmantle </B></FONT>
# We find all such markers and slice between consecutive ones for descriptions.
_CWH_SPELL_RE = re.compile(
    r'<FONT[^>]+COLOR="#0000ff"[^>]+SIZE="3"[^>]*>\s*<B>\s*([^<\n]{2,60}?)\s*</B>',
    re.I,
)

# S&M / SP compilation pages use <A NAME> followed by a colored heading FONT
# (no <B> tag). E.g. SM00259: <A NAME="..."></A></FONT><FONT COLOR="#ff0000" SIZE="4">
# <P></P>Cat's Grace<P></P></FONT>
_ANCHORED_HEADING_RE = re.compile(
    r'<A\s+NAME="([^"]+)"\s*></A>'              # anchor
    r'(?:[^<]*<[^>]+>)*?'                       # optional closing/opening tags
    r'\s*<FONT[^>]+COLOR="#ff0000"[^>]+SIZE="4"[^>]*>'  # red SIZE=4 font
    r'\s*(?:<P></P>\s*)?'                       # optional empty paragraph
    r'([A-Z][^\n<]{2,50}?)'                     # spell name (starts uppercase)
    r'\s*(?:<P></P>)?\s*</FONT>',               # closing font
    re.I,
)


def html_title_index(book_key, prefix):
    """Build/cache an index of {normalized_name: page_entry} for one book.
    page_entry is either a filepath (whole-file extraction) or a tuple
    (filepath, start_offset, end_offset) when the spell is anchored inside
    a multi-spell compilation page.

    Priority ordering: per-spell descriptive pages > anchored sections in
    multi-spell pages > TOC / Appendix / 'Spells by School' index pages."""
    cache_key = (book_key, prefix)
    if cache_key in _html_indices:
        return _html_indices[cache_key]
    book_dir = os.path.join(SOURCE_BASE, book_key)
    if not os.path.isdir(book_dir):
        _html_indices[cache_key] = {}
        return _html_indices[cache_key]
    index = {}                  # key → (priority, entry)
    for filename in os.listdir(book_dir):
        if not (filename.upper().startswith(prefix)
                and filename.upper().endswith('.HTM')):
            continue
        if filename.upper() == f'{prefix}00000.HTM':
            continue
        filepath = os.path.join(book_dir, filename)
        try:
            title = extract_title(filepath).strip()
            name = title.split('--')[0].strip()
            if name:
                # Priority 3 = per-spell descriptive page; 1 = anything else.
                priority = 3 if _PER_SPELL_TITLE_RE.search(title) else 1
                key = name.lower()
                cur = index.get(key)
                if cur is None or priority > cur[0]:
                    index[key] = (priority, filepath)
            # Now scan for anchored spells inside this page (priority 2).
            # Many PHB compilation pages put each spell behind a separate
            # <A NAME></A><B>SpellName</B> anchor, even single-spell pages
            # whose title is "Nth-Level Spells -- Wizard" rather than the
            # spell's own name. Slice between consecutive anchors; the
            # last slice runs to end-of-file. Skip if zero anchors.
            with open(filepath, 'r', encoding='cp1252') as f:
                raw = f.read()
            anchors = list(_ANCHORED_SPELL_RE.finditer(raw))
            # Also scan for S&M / SP colored-heading anchors (no <B> tag)
            heading_anchors = list(_ANCHORED_HEADING_RE.finditer(raw))
            all_anchors = sorted(anchors + heading_anchors, key=lambda m: m.start())
            for i, m in enumerate(all_anchors):
                aname = m.group(2).strip().rstrip(':')
                if not (3 <= len(aname) <= 60): continue
                start = m.start()
                end = all_anchors[i+1].start() if i+1 < len(all_anchors) else len(raw)
                # Normalize typographic apostrophes so keys match ASCII DAT names.
                key = aname.lower().replace('’', "'").replace('‘', "'").replace('\x92', "'")
                cur = index.get(key)
                if cur is None or cur[0] < 2:
                    index[key] = (2, (filepath, start, end))
            # ── CWH / CFH / CBT / CPRH: blue SIZE=3 bold spell names (no <A NAME>) ──
            # Run after anchor scanners so per-spell pages still take priority.
            cwh_markers = list(_CWH_SPELL_RE.finditer(raw))
            for i, m in enumerate(cwh_markers):
                sname = m.group(1).strip().rstrip('*').strip()
                if not (2 <= len(sname) <= 60): continue
                start = m.start()
                end = cwh_markers[i+1].start() if i+1 < len(cwh_markers) else len(raw)
                key = sname.lower().replace('’', "'").replace('‘', "'").replace('\x92', "'")
                cur = index.get(key)
                if cur is None or cur[0] < 2:
                    index[key] = (2, (filepath, start, end))
        except Exception:
            pass
    flat = {k: v[1] for k, v in index.items()}
    _html_indices[cache_key] = flat
    return flat


# True AD&D 2e reversible-spell pairs (primary → reverse): used to child-link the
# reverse onto the primary via `system.itemList`. Sourced at runtime from
# SPELLS.DAT index pairs (`_SPELL_REVERSE_INDEX`) so no spell names live here —
# call _reversibles_primary_to_reverse().


def _normalize_spell_name_for_lookup(name):
    """Generate progressively-loose lookup keys for a DAT spell name. Tried
    in order — first match wins. Handles common DAT vs HTM divergences:
    typographic apostrophes, multiple internal spaces (raw DAT artifact),
    commas/parentheses, 'Power Word, X' → 'X', 'X, N' radius variants,
    'X' → 'X spell' (PHB title quirk on Suggestion), Wounds/Wound singular,
    '&' ↔ 'and', "10-foot" ↔ "10'", and a few known spelling differences."""
    if not name: return []
    n = name.strip()
    # Whitespace normalization: collapse internal multi-space runs
    n_clean = re.sub(r'\s+', ' ', n)
    keys = []
    def _push(k):
        if not k: return
        k = k.strip().lower()
        # Final normalization on every key: collapse spaces, strip trailing
        # period left over from "Power Word, Stun." style entries.
        k = re.sub(r'\s+', ' ', k).rstrip('.').strip()
        if k and k not in keys: keys.append(k)
    _push(n_clean)
    # Typographic apostrophe → ASCII
    _push(n_clean.replace('’', "'").replace('‘', "'"))
    # Drop comma+space (e.g. "Darkness, 15' Radius" → "Darkness 15' Radius")
    _push(re.sub(r',\s*', ' ', n_clean))
    # "Power Word, X" → "X" (PHB titles them just "X")
    m = re.match(r'^power word,\s*(.+)$', n_clean, re.I)
    if m: _push(m.group(1))
    # Strip trailing parenthetical " (15' Radius)" etc.
    _push(re.sub(r'\s*\([^)]*\)\s*$', '', n_clean))
    # PHB sometimes titles a spell page "X Spell" (e.g. Suggestion Spell)
    _push(f'{n_clean} spell')

    # ── Bidirectional substitutions: for each existing key, generate variants
    # by toggling singular/plural endings, '&' ↔ 'and', and "10-foot" ↔ "10'"
    # so we hit either the DAT spelling or the HTM page title.
    def _expand_all():
        more = []
        for k in list(keys):
            # "Wounds" ↔ "Wound", "Stones" ↔ "Stone" (only single trailing s)
            if re.search(r'\bwounds\b', k):
                more.append(re.sub(r'\bwounds\b', 'wound', k))
            elif re.search(r'\bwound\b', k):
                more.append(re.sub(r'\bwound\b', 'wounds', k))
            # '&' ↔ 'and'
            if '&' in k: more.append(k.replace('&', 'and').replace('  ', ' '))
            if ' and ' in k: more.append(k.replace(' and ', ' & '))
            # "10-foot" / "15-foot" ↔ "10'" / "15'"
            kk = re.sub(r"(\d+)-foot", r"\1'", k)
            if kk != k: more.append(kk)
            kk = re.sub(r"(\d+)'(?:\s+radius)?", r"\1-foot radius", k)
            if kk != k: more.append(kk)
            # 'From' vs 'from' — Foundry comparison is already case-insensitive
            # but normalize anyway
            kk = re.sub(r'\bfrom\b', 'from', k, flags=re.I)
            if kk != k: more.append(kk.lower())
            # 'Vs' / 'Vs.' / 'vs' / 'vs.' — already normalized via lowercasing
        for k in more: _push(k)
    _expand_all()
    _expand_all()    # second pass to combine expansions

    # ── Bidirectional spelling-variant expansions for known HTM typos ──────────
    # These are parse-time references (patterns to try) so matching works even
    # when the CD-ROM HTML file title has a different spelling than the DAT name.
    def _typo_variants(k):
        """Return additional lookup keys generated by common typo patterns."""
        out = []
        # Magic ↔ Magical (e.g. "Nystal's Magic Aura" vs "Nystul's Magical Aura")
        for a, b in [('magical', 'magic'), ('magic', 'magical')]:
            kk = re.sub(r'\b' + a + r'\b', b, k)
            if kk != k: out.append(kk)
        # -ible ↔ -able  (irresistible ↔ irresistable)
        kk = re.sub(r'ible\b', 'able', k); (out.append(kk) if kk != k else None)
        kk = re.sub(r'able\b', 'ible', k); (out.append(kk) if kk != k else None)
        # -oney ↔ -ony  (stoney ↔ stony, boney ↔ bony)
        kk = re.sub(r'oney\b', 'ony', k);  (out.append(kk) if kk != k else None)
        kk = re.sub(r'ony\b', 'oney', k);  (out.append(kk) if kk != k else None)
        # transmutation ↔ tranmutation  (one-letter drop)
        kk = k.replace('transmutation', 'tranmutation')
        if kk != k: out.append(kk)
        kk = k.replace('tranmutation', 'transmutation')
        if kk != k: out.append(kk)
        # fundamental ↔ fundemental
        kk = k.replace('fundamental', 'fundemental')
        if kk != k: out.append(kk)
        kk = k.replace('fundemental', 'fundamental')
        if kk != k: out.append(kk)
        # nystul ↔ nystal  (person-name typo in the CD-ROM HTML)
        kk = k.replace('nystul', 'nystal')
        if kk != k: out.append(kk)
        kk = k.replace('nystal', 'nystul')
        if kk != k: out.append(kk)
        # hovering road ↔ hovering raod
        kk = k.replace('hovering road', 'hovering raod')
        if kk != k: out.append(kk)
        # ensnarement ↔ ensarement (missing 'n' in HTML title)
        kk = k.replace('ensnarement', 'ensarement')
        if kk != k: out.append(kk)
        kk = k.replace('ensarement', 'ensnarement')
        if kk != k: out.append(kk)
        # airboat ↔ air boat (one word vs two in TOM HTML title)
        kk = k.replace('airboat', 'air boat')
        if kk != k: out.append(kk)
        kk = k.replace('air boat', 'airboat')
        if kk != k: out.append(kk)
        # accelerate ↔ acclerate (missing 'e' in TOM HTML title)
        kk = k.replace('accelerate', 'acclerate')
        if kk != k: out.append(kk)
        kk = k.replace('acclerate', 'accelerate')
        if kk != k: out.append(kk)
        # demi-shadow ↔ demishadow (PHB pages drop the hyphen)
        kk = k.replace('demi-shadow', 'demishadow')
        if kk != k: out.append(kk)
        kk = k.replace('demishadow', 'demi-shadow')
        if kk != k: out.append(kk)
        # vs ↔ versus
        kk = re.sub(r'\bvs\b\.?', 'versus', k)
        if kk != k: out.append(kk)
        kk = re.sub(r'\bversus\b', 'vs', k)
        if kk != k: out.append(kk)
        # Strip trailing * / * asterisk (variant spells)
        kk = k.rstrip('* ').strip()
        if kk != k: out.append(kk)
        # Compound names "A/B" → try just "A"
        if '/' in k:
            out.append(k.split('/')[0].strip())
        # suspended ↔ suspend (animation)
        kk = k.replace('suspended animation', 'suspend animation')
        if kk != k: out.append(kk)
        kk = k.replace('suspend animation', 'suspended animation')
        if kk != k: out.append(kk)
        # Singular ↔ plural for last word
        kk = re.sub(r's\b$', '', k)
        if kk != k and len(kk) > 3: out.append(kk)
        kk = k + 's'
        if kk not in out: out.append(kk)
        return out

    # Apply typo variants, then apply again on the variants (handles combined typos
    # like "nystal's magic aura" which needs both nystul→nystal AND magical→magic).
    first_pass = list(keys)
    for base_key in first_pass:
        for variant in _typo_variants(base_key):
            _push(variant)
    for base_key in list(keys):   # second pass on all keys including first-pass results
        for variant in _typo_variants(base_key):
            _push(variant)

    # Known spelling divergences and reverse-spell aliases (DAT name → HTM key).
    ALIASES = {
        "detect snares & pits":          "detect snares and pits",
        "proofing vs combustion":        "proofing versus combustion",
        "create food & water":           "create food & drink",
        # Reverse spells — mapped to their primary's HTM page title
        "babble":                        "tongues",
        "badberry":                      "goodberry",
        "attraction":                    "avoidance",
        "call":                          "dismissal",
        "chill metal":                   "heat metal",
        "copy":                          "forget",
        "destruction":                   "resurrection",
        "dispel hallucinatory forest":   "hallucinatory forest",
        "fear ward":                     "fear",
        "flesh to stone":                "stone to flesh",
        "freedom":                       "imprisonment",
        "invulnerability to normal weapons": "globe of invulnerability",
        "lose the path":                 "find the path",
        "nightmare":                     "dream",
        "raise water":                   "lower water",
        "shadow form":                   "shadow walk",
        "shrink animal":                 "animal growth",
        "shrink insect":                 "giant insect",
        "snakes to sticks":              "sticks to snakes",
        "stabilize":                     "chaos",
        "streighten wood":               "warp wood",
        "temporal reinstatement":        "temporal stasis",
        "youthful object":               "age object",
        "selective passage":             "tanglefoot",
        "the black circle":              "the great circle",
    }
    base = keys[0] if keys else n_clean.lower()
    if base in ALIASES:
        _push(ALIASES[base])
        _push(ALIASES[base] + ' spell')  # e.g. "Dream Spell" page for Nightmare alias

    # ── Reversible-spell pairs: AD&D 2e PHB merges each reversible pair into a
    # single HTM page titled after the primary. The DAT splits them, so a reverse
    # spell's name must be routed to its primary's title for the description
    # lookup. The reverse→primary map is built at runtime from SPELLS.DAT index
    # pairs (no spell names embedded) — see _reverse_pairs().
    reverse_pairs = _reverse_pairs()
    rev_key = re.sub(r'\s+', ' ', n_clean.replace('’', "'").replace('‘', "'")).strip().lower()
    if rev_key in reverse_pairs:
        _push(reverse_pairs[rev_key])
        # Also try the singular form (e.g. "Cure Light Wounds" → "Cure Light Wound").
        _push(re.sub(r'\bwounds\b', 'wound', reverse_pairs[rev_key]))

    return keys


def lookup_html_description(name, books):
    """Lookup an HTML description by name across multiple books. Tries the
    progressive normalization keys from _normalize_spell_name_for_lookup,
    in book search order. Returns cleaned HTML string or '' if not found.

    Index entries are either a filepath (whole-file clean) or a tuple
    `(filepath, start_offset, end_offset)` for an anchored slice inside a
    multi-spell compilation page — sliced and cleaned the same way."""
    if not name: return ''
    candidates = _normalize_spell_name_for_lookup(name)
    for book_key, prefix in books:
        index = html_title_index(book_key, prefix)
        for cand in candidates:
            entry = index.get(cand)
            if not entry: continue
            try:
                if isinstance(entry, tuple):
                    filepath, start, end = entry
                    book_dir = os.path.dirname(filepath)
                    src_dir_files = {f.upper(): f for f in os.listdir(book_dir)}
                    with open(filepath, 'r', encoding='cp1252') as f:
                        raw = f.read()
                    chunk = raw[start:end]
                    # Wrap the chunk in a minimal HTML envelope so the
                    # cleanup pipeline parses it the same as a full page.
                    wrapped = f'<html><body>{chunk}</body></html>'
                    soup = BeautifulSoup(wrapped, 'html.parser')
                    body = soup.find('body') or soup
                    return _clean_html_body(body, soup, book_key, src_dir_files)
                else:
                    filepath = entry
                    book_dir = os.path.dirname(filepath)
                    src_dir_files = {f.upper(): f for f in os.listdir(book_dir)}
                    return clean_html_file(filepath, book_key, src_dir_files)
            except Exception:
                pass
    return ''


# Per-entity book search order
_SPELL_HTML_BOOKS   = [('PHB','PHB'), ('TOM','TOM'), ('SM','SM'), ('SP','SP'),
                       ('CWH','CWH')]
_MONSTER_HTML_BOOKS = [('MM','MM')]
_ITEM_HTML_BOOKS    = [('AEG','AEG')]
_ITEM_DMG_BOOKS     = [('DMG','DMG')]

# Regex to strip the weapon subtype from PARTS.DAT sword names so we can
# reconstruct the DMG's generic "Sword {rest}" title.
# e.g. "Sword, scimitar +5 Holy Avenger" → strip "Sword, scimitar " → "+5 Holy Avenger"
_SWORD_SUBTYPE_RE  = re.compile(
    r'^sword,\s*(?:scimitar|long|short|bastard|broad|two-handed|great)\s+', re.I)
# Strip ", heavy" / ", light" suffix from crossbow names
_CROSSBOW_LIGHT_HEAVY_RE = re.compile(r',\s*(?:heavy|light)\s*$', re.I)


def _item_magic_lookup_candidates(full_name):
    """Generate DMG lookup candidates for a PARTS.DAT item name, going beyond
    the simple +N-stripped base name. Three cases handled:
      1. Sword variants → strip subtype, reconstruct generic "sword {rest}" key.
      2. Crossbow variants → drop ", heavy"/", light" suffix.
      3. Missile-attraction armors → map to generic "armor of missile attraction".
    The full name (unstripped) is always appended as a final fallback."""
    candidates = []
    low = full_name.lower()

    # ── Swords: "Sword, scimitar +5 Holy Avenger" → "sword +5 holy avenger" ──
    if low.startswith('sword,'):
        rest = _SWORD_SUBTYPE_RE.sub('', full_name).strip()
        if rest:
            candidates.append(f'sword {rest}')      # "sword +5 Holy Avenger"
            candidates.append(f'sword, {rest}')     # "sword, Vorpal" style
            # Some DMG anchors use "sword+N, Name" (no space, comma after bonus)
            m_bonus = re.match(r'([+\-]\d+)\s+(.+)', rest)
            if m_bonus:
                candidates.append(f'sword{m_bonus.group(1)}, {m_bonus.group(2)}')

    # ── Crossbows: "Crossbow of speed, heavy" → "crossbow of speed" ──────────
    if 'crossbow' in low:
        without_suffix = _CROSSBOW_LIGHT_HEAVY_RE.sub('', full_name).strip()
        if without_suffix != full_name:
            candidates.append(without_suffix)

    # ── Missile attraction: all armor variants → single DMG entry ─────────────
    if 'missile attraction' in low:
        candidates.append('armor of missile attraction')

    # Full name without +N stripping (for "Sling of Seeking +2" etc.)
    candidates.append(full_name)

    return [c.lower().strip() for c in candidates if c and len(c) > 3]


def _item_dmg_magic_description(name):
    """Tightened DMG lookup for magic 'item' records the generic chain misses
    because of trailing variant qualifiers ("Ring of Wizardry, 3rd", "Boots of
    Levitation, 448 lbs", "Pearl of Power, Cursed (8th)"). Strips the magic ±N
    suffix, parentheticals and the trailing comma-clause, then matches the DMG
    "X-- Magical Item"/"X-- Scroll" index — but only on a MULTI-WORD key, so a
    misclassified "Sword, two-handed" never grabs the generic "sword" page.
    Returns cleaned HTML or ''."""
    idx = html_title_index('DMG', 'DMG')
    base = re.sub(r'\s*[+-]\d.*$', '', name)
    base = re.sub(r'\s*\(.*?\)\s*', ' ', base).strip()
    cands = []
    for c in (base, re.sub(r',\s*[^,]*$', '', base)):
        c = ' '.join(c.lower().split())
        if c and c not in cands:
            cands.append(c)
    # Protection scrolls: PARTS "Scroll of Protection from Cold" → the DMG page
    # is titled "Protection from Cold-- Scroll" (indexed "protection from cold").
    m = re.match(r'scroll of (.+)', base, re.I)
    if m:
        cands.append(' '.join(m.group(1).lower().split()))
    for c in cands:
        if len(c.split()) < 2:
            continue
        entry = idx.get(c)
        if not entry:
            continue
        try:
            if isinstance(entry, tuple):
                fp, start, end = entry
                src = {f.upper(): f for f in os.listdir(os.path.dirname(fp))}
                raw = open(fp, 'r', encoding='cp1252').read()[start:end]
                soup = BeautifulSoup(f'<html><body>{raw}</body></html>', 'html.parser')
                return _clean_html_body(soup.find('body') or soup, soup, 'DMG', src)
            src = {f.upper(): f for f in os.listdir(os.path.dirname(entry))}
            return clean_html_file(entry, 'DMG', src)
        except Exception:
            return ''
    return ''


# ─── Race factual data (numeric stats from PHB 2e, ability labels) ───────────
# Movement is in 6-second-round squares per PHB. Size category is the standard
# AD&D 2e size. Ability names are short factual labels (no narrative text).
# Direct PHB HTML files for the long descriptions are listed in _RACE_HTML_FILES.

_RACE_HTML_FILES = {
    'Dwarf':    'PHB/PHB00040.HTM',
    'Elf':      'PHB/PHB00042.HTM',
    'Gnome':    'PHB/PHB00043.HTM',
    'Half-elf': 'PHB/PHB00044.HTM',
    'Halfling': 'PHB/PHB00045.HTM',
    'Human':    'PHB/PHB00046.HTM',
}

# Map any race name (sub-races, normalized variants) to a canonical base-race key.
def _resolve_base_race(name):
    """Map a race/sub-race name to its PHB base race (Dwarf, Elf, Gnome, …) so a
    sub-race (Mountain Dwarf, Drow, …) can inherit the base race's PHB chapter
    for description/movement lookup. Returns the base name or None."""
    if not name: return None
    n = name.strip()
    low = n.lower()
    direct = {
        'dwarf': 'Dwarf', 'hill dwarf': 'Dwarf', 'mountain dwarf': 'Dwarf',
        'deep dwarf': 'Dwarf', 'gray dwarf (duergar)': 'Dwarf',
        'elf': 'Elf', 'high elf': 'Elf', 'gray elf': 'Elf', 'sylvan (wood) elf': 'Elf',
        'dark (drow) elf': 'Elf', 'aquatic (sea) elf': 'Elf',
        'gnome': 'Gnome', 'rock gnome': 'Gnome', 'forest gnome': 'Gnome',
        'deep gnome (svirfneblin)': 'Gnome',
        'halfling': 'Halfling', 'hairfoot halfling': 'Halfling',
        'stout halfling': 'Halfling', 'tallfellow halfling': 'Halfling',
        'half-stout halfling': 'Halfling',
        'half-elf': 'Half-elf', 'standard half-elf': 'Half-elf',
        'half-orc': 'Half-orc', 'standard half-orc': 'Half-orc',
        'half-ogre': 'Half-ogre', 'standard half-ogre': 'Half-ogre',
        'human': 'Human',
    }
    if low in direct:
        return direct[low]
    # Fallback: try the rightmost meaningful token
    for tok in reversed(re.split(r'[\s,()\-]+', n)):
        if tok and tok.lower() in direct:
            return direct[tok.lower()]
    return None


# Per-base-race factual data. Movement in PHB 6-second rounds.
# Each "abilities" entry is a Foundry Ability sub-item: (name, [(effectName, [changes])]).
# Changes use Foundry ActiveEffect schema: {key, mode, value}.
# Mode 2 = ADD, mode 5 = OVERRIDE. Path system.attributes.movement.value targets ARS movement.
# Path references into PC-oriented source files (PHB + Skills & Powers).
# These are file pointers, not game data — the values they index live on the
# user's CD-ROM and are parsed at runtime.
# Movement is now extracted directly from RACE.DAT (see parse_race_record),
# so PHB Table 64 (PHB00370.HTM) and the SP00059 MV column are no longer used.
_SP_ABILITY_LEGEND_FILES   = ['SP/SP00033.HTM',  # "Abilities and Restrictions -- Other Races"
                              'SP/SP00034.HTM']  # "Penalties -- Other Races"
_SP_EXOTIC_TABLE_FILE      = 'SP/SP00059.HTM'    # codes legend lookup for exotic-race abilities
_SP_THIEF_SKILLS_FILE      = 'SP/SP00074.HTM'    # column-header source for thief skill names
_SP_THIEF_BASE_FILE        = 'SP/SP00073.HTM'    # base scores per thief skill
_PHB_CLIMBING_RATES_FILE   = 'PHB/PHB00378.HTM'  # Table 65 — Base Climbing Success Rates

# Lineage → SP demihuman page containing detection sub-skill thresholds.
# (Half-elf / Half-ogre / Human SP pages have no parseable thresholds.)
_LINEAGE_SP_DETECTION_FILES = {
    'Dwarf':    'SP/SP00024.HTM',
    'Gnome':    'SP/SP00026.HTM',
    'Halfling': 'SP/SP00027.HTM',
    'Half-orc': 'SP/SP00029.HTM',
}

# SP per-base-race demihuman pages (Skills & Powers). Each page contains one
# "{Sub-race} Special Abilities" table per sub-race, listing ability names cell-by-cell.
_SP_DEMIHUMAN_FILES = {
    'Dwarf':    'SP/SP00024.HTM',
    'Elf':      'SP/SP00025.HTM',
    'Gnome':    'SP/SP00026.HTM',
    'Halfling': 'SP/SP00027.HTM',
    'Half-elf': 'SP/SP00028.HTM',
    'Half-orc': 'SP/SP00029.HTM',
    'Half-ogre':'SP/SP00030.HTM',
    'Human':    'SP/SP00031.HTM',
}

# SP per-exotic-race PC pages (one short PC-oriented description page each).
_SP_EXOTIC_FILES = {
    # Half-* demihumans: PHB 2e treats them only as optional, so their
    # dedicated PC-oriented pages live in Skills & Powers, not PHB.
    'Half-orc':   'SP/SP00029.HTM',  'Half-ogre':  'SP/SP00030.HTM',
    # SP00038 is titled "The Races" (chapter intro) but its body is the
    # full Aarakocra entry — per-race pages continue from SP00039.
    'Aarakocra':  'SP/SP00038.HTM',
    'Alaghi':     'SP/SP00039.HTM',  'Bugbear':    'SP/SP00040.HTM',
    'Bullywug':   'SP/SP00041.HTM',  'Centaur':    'SP/SP00042.HTM',
    'Flind':      'SP/SP00043.HTM',  'Giff':       'SP/SP00044.HTM',
    'Githzerai':  'SP/SP00045.HTM',  'Gnoll':      'SP/SP00046.HTM',
    'Goblin':     'SP/SP00047.HTM',  'Hobgoblin':  'SP/SP00048.HTM',
    'Kobold':     'SP/SP00049.HTM',  'Lizard man': 'SP/SP00050.HTM',
    'Minotaur':   'SP/SP00051.HTM',  'Mongrelman': 'SP/SP00052.HTM',
    'Ogre':       'SP/SP00053.HTM',  'Orc':        'SP/SP00054.HTM',
    'Satyr':      'SP/SP00055.HTM',  'Swanmay':    'SP/SP00056.HTM',
    'Thri-kreen': 'SP/SP00057.HTM',  'Wemic':      'SP/SP00058.HTM',
}

# Foundry icon paths (Foundry assets, not D&D content — fine to hardcode).
# All paths below have been verified to exist in FVTT/public/icons/ at the
# revision shipped in the project's FVTT/ subdirectory.
_FOUNDRY_ICON_DEFAULT     = 'icons/svg/aura.svg'
_FOUNDRY_ICON_INFRAVISION = 'icons/magic/perception/eye-ringed-glow-angry-red.webp'
_FOUNDRY_ICON_NORMALVISION= 'icons/creatures/eyes/human-single-brown.webp'
_FOUNDRY_ICON_SLEEPCHARM  = 'icons/magic/control/sleep-bubble-purple.webp'
_FOUNDRY_ICON_MOVEMENT    = 'icons/skills/movement/figure-running-gray.webp'
_FOUNDRY_ICON_RACEMOD     = 'icons/svg/upgrade.svg'
_FOUNDRY_ICON_SKILL_CLIMB = 'icons/environment/settlement/city-wall.webp'
_FOUNDRY_ICON_SKILL_LISTEN= 'icons/sundries/misc/teeth-dentures.webp'
_FOUNDRY_ICON_SKILL_SECRET= 'icons/svg/door-secret-outline.svg'
_FOUNDRY_ICON_SKILL_MINING= 'icons/commodities/stone/paver-brick-blue.webp'

# Keyword → icon mapping for race-ability labels. The label is the cell text
# from an SP per-sub-race table (e.g. "Saving Throw Bonuses", "Infravision, 60'")
# or the legend name for an exotic-race letter code (e.g. "Charge Attack",
# "Tracking"). Matching is done as a case-insensitive substring search, in
# declaration order (specific keywords first). Falls back to _FOUNDRY_ICON_DEFAULT.
_RACE_ABILITY_ICON_MAP = [
    # ── Vision / sleep / movement ──
    ('infravision',               _FOUNDRY_ICON_INFRAVISION),
    ('less sleep',                'icons/svg/regen.svg'),                       # before generic 'sleep'
    ('sleep',                     _FOUNDRY_ICON_SLEEPCHARM),
    ('charm',                     _FOUNDRY_ICON_SLEEPCHARM),
    ('hypnos',                    _FOUNDRY_ICON_SLEEPCHARM),
    ('base movement',             _FOUNDRY_ICON_MOVEMENT),
    ('forest movement',           'icons/magic/nature/root-vine-entangle-foot-green.webp'),

    # ── Defensive / resistance ──
    ('saving throw',              'icons/magic/defensive/shield-barrier-blue.webp'),
    ('save bonus',                'icons/magic/defensive/shield-barrier-blue.webp'),
    ('magic resistance',          'icons/magic/defensive/shield-barrier-deflect-teal.webp'),
    ('spell immunity',            'icons/magic/defensive/shield-barrier-deflect-teal.webp'),
    ('illusion resistant',        'icons/svg/blind.svg'),
    ('poison resistance',         'icons/magic/death/projectile-skull-animal-green.webp'),
    ('cold resistance',           'icons/magic/water/snowflake-ice-blue.webp'),
    ('heat resistance',           'icons/svg/fire.svg'),
    ('resistance',                'icons/magic/defensive/shield-barrier-blue.webp'),
    ('defensive bonus',           'icons/equipment/shield/buckler-iron-cross-gray.webp'),
    ('fearlessness',              'icons/magic/defensive/shield-barrier-blue.webp'),

    # ── Detection (mining / stone / poison / evil / secret doors) ──
    ('mining detection',          'icons/commodities/stone/paver-brick-blue.webp'),
    ('detect new construction',   'icons/commodities/stone/paver-brick-blue.webp'),
    ('detect sloping',            'icons/commodities/stone/paver-brick-blue.webp'),
    ('detect sliding',            'icons/commodities/stone/paver-brick-blue.webp'),
    ('detect stonework',          'icons/commodities/stone/paver-brick-blue.webp'),
    ('detect poison',             'icons/magic/death/projectile-skull-animal-green.webp'),
    ('detect evil',               'icons/svg/eye.svg'),
    ('secret door',               'icons/svg/door-closed.svg'),

    # ── Weapon bonuses (each weapon gets its own icon — long-form first) ──
    ('warhammer',                 'icons/weapons/hammers/hammer-war-spiked.webp'),
    ('short sword',               'icons/weapons/swords/shortsword-broad.webp'),
    ('axe',                       'icons/weapons/axes/axe-battle-black.webp'),
    ('crossbow',                  'icons/weapons/crossbows/crossbow-heavy-black.webp'),
    ('bow bonus',                 'icons/skills/ranged/arrow-flying-broadhead-metal.webp'),
    ('dagger',                    'icons/weapons/daggers/dagger-black.webp'),
    ('dart',                      'icons/skills/ranged/arrow-flying-broadhead-metal.webp'),
    ('javelin',                   'icons/weapons/polearms/javelin-simple.webp'),
    ('mace',                      'icons/weapons/maces/mace-flanged-steel.webp'),
    ('pick bonus',                'icons/weapons/hammers/hammer-war-spiked.webp'),
    ('sling',                     'icons/weapons/slings/sling-leather.webp'),
    ('spear bonus',               'icons/skills/melee/strike-spear-red.webp'),
    ('sword bonus',               'icons/weapons/swords/sword-guard-blue.webp'),
    ('trident',                   'icons/skills/melee/spear-tips-triple-orange.webp'),
    ('racial weapons',            'icons/skills/melee/hand-grip-sword-orange.webp'),

    # ── Combat / stats ──
    ('hit point',                 'icons/skills/wounds/anatomy-organ-heart-red.webp'),
    ('health bonus',              'icons/skills/wounds/anatomy-organ-heart-red.webp'),
    ('constitution/health',       'icons/skills/wounds/anatomy-organ-heart-red.webp'),
    ('fitness bonus',             'icons/skills/wounds/anatomy-organ-heart-red.webp'),
    ('damage bonus',              'icons/skills/wounds/blood-drip-droplet-red.webp'),
    ('damage',                    'icons/skills/wounds/blood-drip-droplet-red.webp'),
    ('improved stamina',          'icons/svg/regen.svg'),
    ('stamina',                   'icons/svg/regen.svg'),
    ('more muscles',              'icons/skills/melee/strike-sword-steel-yellow.webp'),
    ('muscle',                    'icons/skills/melee/strike-sword-steel-yellow.webp'),
    ('better balance',            'icons/skills/movement/feet-spurred-boots-brown.webp'),
    ('balance',                   'icons/skills/movement/feet-spurred-boots-brown.webp'),
    ('aim bonus',                 'icons/svg/target.svg'),
    ('experience bonus',          'icons/svg/up.svg'),
    ('reason bonus',              'icons/skills/wounds/anatomy-organ-brain-pink-red.webp'),
    ('reaction bonus',            'icons/skills/social/diplomacy-handshake.webp'),
    ('attack bonus',              'icons/skills/melee/hand-grip-sword-orange.webp'),
    ('melee combat',              'icons/skills/melee/hand-grip-sword-orange.webp'),

    # ── Body / senses ──
    ('dense skin',                'icons/equipment/chest/breastplate-banded-blue.webp'),
    ('tough hide',                'icons/commodities/leather/fur-brown.webp'),
    ('active sense of smell',     'icons/sundries/misc/teeth-dentures.webp'),
    ('acute taste',               'icons/sundries/misc/teeth-dentures.webp'),
    ('antennae',                  'icons/magic/perception/eye-ringed-glow-angry-teal.webp'),
    ('hideous appearance',        'icons/magic/control/fear-fright-mask-yellow.webp'),
    ('inhuman form',              'icons/creatures/claws/claw-straight-brown.webp'),
    ('size',                      'icons/svg/upgrade.svg'),

    # ── Trade / knowledge ──
    ('brewing',                   'icons/consumables/drinks/wine-amphora-cup-gray.webp'),
    ('evaluate gems',             'icons/commodities/gems/gem-cluster-red.webp'),
    ('expert haggler',            'icons/commodities/currency/coin-embossed-crown-gold.webp'),
    ('determine age',             'icons/svg/sun.svg'),
    ('determine stability',       'icons/svg/anchor.svg'),
    ('magic identification',      'icons/magic/symbols/runes-carved-stone-green.webp'),
    ('potion identification',     'icons/consumables/potions/bottle-bulb-corked-green.webp'),
    ('engineering',               'icons/sundries/misc/teeth-dentures.webp'),

    # ── Earth / stone affinity ──
    ('close to the earth',        'icons/commodities/stone/boulder-grey.webp'),
    ('meld into stone',           'icons/commodities/stone/boulder-grey.webp'),
    ('stone tell',                'icons/commodities/stone/boulder-grey.webp'),

    # ── Magic / nature / element ──
    ('spell abilities',           'icons/magic/symbols/runes-star-pentagon-orange.webp'),
    ('speak with plants',         'icons/magic/nature/leaf-glow-green.webp'),
    ('animal friendship',         'icons/creatures/abilities/paw-print-tan.webp'),
    ('companion',                 'icons/creatures/abilities/paw-print-tan.webp'),
    ('water breathing',           'icons/magic/water/wave-water-blue.webp'),
    ('amphibious',                'icons/magic/water/wave-water-blue.webp'),
    ('freeze',                    'icons/svg/frozen.svg'),

    # ── Stealth / perception ──
    ('hide',                      'icons/magic/perception/silhouette-stealth-shadow.webp'),
    ('stealth',                   'icons/magic/perception/silhouette-stealth-shadow.webp'),
    ('move silently',             'icons/magic/perception/silhouette-stealth-shadow.webp'),

    # ── Social ──
    ('taunt',                     'icons/skills/social/intimidation-impressing.webp'),

    # ── SP exotic legend labels (SP00033 + SP00034) ──
    ('charge attack',             'icons/environment/people/charge.webp'),
    ('surprise',                  'icons/magic/perception/silhouette-stealth-shadow.webp'),
    ('hard to surprise',          'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('leap',                      'icons/creatures/mammals/deer-movement-leap-green.webp'),
    ('tracking',                  'icons/svg/wingfoot.svg'),
    ('sound mimicry',             'icons/magic/sonic/projectile-sound-rings-wave.webp'),
    ('pick pockets',              'icons/svg/coins.svg'),
    ('magical pipes',             'icons/skills/trades/music-notes-sound-blue.webp'),
    ('paralyzing bite',           'icons/creatures/reptiles/snake-fangs-bite-green-yellow.webp'),
    ('dodge missiles',            'icons/skills/movement/arrow-upward-blue.webp'),
    ('swan form',                 'icons/svg/wing.svg'),
    ('claustrophobia',            'icons/svg/falling.svg'),
    ('dehydration',               'icons/magic/water/wave-water-blue.webp'),
    ('light',                     'icons/svg/d20-highlight.svg'),
    ('racial enmity',             'icons/skills/melee/hand-grip-sword-orange.webp'),
    ('easily distracted',         'icons/svg/aura.svg'),

    # ── Generic last ──
    ('language',                  'icons/sundries/documents/document-letter-tan.webp'),
    ('attack',                    'icons/skills/melee/hand-grip-sword-orange.webp'),
    # NOTE: no generic 'bonus' catch-all — it would homogenize every X Bonus
    # to the same upgrade icon. Add a specific keyword per concept instead.
]


def pick_race_ability_icon(label):
    """Map an ability label to a verified-existing Foundry icon path."""
    if not label:
        return _FOUNDRY_ICON_DEFAULT
    low = label.lower()
    for keyword, path in _RACE_ABILITY_ICON_MAP:
        if keyword in low:
            return path
    return _FOUNDRY_ICON_DEFAULT


# ─── Cached runtime extractors (each reads source HTML once per process) ──────

_sp_legend_cache   = None
_sp_exotic_cache   = None
_sp_subrace_cache  = {}     # {sp_file_path: {subrace_heading: [ability_cells]}}
_ability_desc_cache = None  # {normalized_label: html} merged across SP files
_thief_skill_names_cache = None    # ordered list of 13 skill names from SP00074
_sp_thief_base_cache     = None    # {skill_lower: percent_int}
_phb_unskilled_climb_cache = None  # percent_int
_lineage_detection_cache = {}      # {lineage: [(sub_skill, max_succ, die_size), ...]}




def _load_sp_thief_base_scores():
    """Parse SP00073 (Thief Skill Base Scores). Layout is a 3-column table
    with an empty first column: ['', 'Pick Pockets', '15%'] etc. Returns
    {skill_lower: percent_int}."""
    global _sp_thief_base_cache
    if _sp_thief_base_cache is not None:
        return _sp_thief_base_cache
    out = {}
    path = os.path.join(SOURCE_BASE, _SP_THIEF_BASE_FILE)
    if os.path.exists(path):
        with open(path, 'r', encoding='cp1252') as f:
            soup = BeautifulSoup(f.read(), 'html.parser')
        for table in soup.find_all('table'):
            for tr in table.find_all('tr'):
                cells = [td.get_text(' ', strip=True) for td in tr.find_all(['td','th'])]
                # Skip the first empty column; expect [name, percent] in the
                # remaining cells.
                vals = [c for c in cells if c]
                if len(vals) >= 2:
                    name = vals[0].strip()
                    m = re.match(r'^\s*(\d+)\s*%', vals[1])
                    if m and name.lower() not in ('skill','base chance'):
                        out[name.lower()] = int(m.group(1))
    _sp_thief_base_cache = out
    return out


def _load_phb_unskilled_climbing_pct():
    """Parse PHB00378 (Table 65) → percent for 'Unskilled climber' row."""
    global _phb_unskilled_climb_cache
    if _phb_unskilled_climb_cache is not None:
        return _phb_unskilled_climb_cache
    out = None
    path = os.path.join(SOURCE_BASE, _PHB_CLIMBING_RATES_FILE)
    if os.path.exists(path):
        with open(path, 'r', encoding='cp1252') as f:
            soup = BeautifulSoup(f.read(), 'html.parser')
        for table in soup.find_all('table'):
            for tr in table.find_all('tr'):
                cells = [td.get_text(' ', strip=True) for td in tr.find_all(['td','th'])]
                if len(cells) >= 2 and 'unskilled' in cells[0].lower():
                    m = re.search(r'(\d+)\s*%', cells[1])
                    if m: out = int(m.group(1))
    _phb_unskilled_climb_cache = out
    return out


def _shorten_detection_name(verbose):
    """Strip rule-text filler from a Mining Detection sub-skill phrase to
    keep just the essential noun phrase, for use as a short skill label.
    Examples:
      'Determine the approximate depth underground' → 'Detect Depth Underground'
      'Detect any sliding or shifting walls or rooms' → 'Detect Sliding or Shifting Walls'
      'Detect any grade or slope in the passage they are passing through' → 'Detect Grade or Slope'
      'Detect stonework traps, pits, and deadfalls' → 'Detect Stonework Traps'
      'Detect unsafe walls, ceilings, or floors' → 'Detect Unsafe Walls' """
    s = verbose.strip()
    # Normalize verb: always start with "Detect"
    s = re.sub(r'^Determine\b', 'Detect', s, flags=re.I)
    # Strip filler qualifiers
    s = re.sub(r'\b(?:the\s+)?approximate\s+', '', s, flags=re.I)
    s = re.sub(r'\bany\s+', '', s, flags=re.I)
    # Drop trailing locative clauses
    s = re.sub(r'\s+in\s+(?:the\s+)?(?:passage|stonework)[^,.]*', '', s, flags=re.I)
    # Drop trailing enumerated noun lists (keep only the first noun)
    s = re.sub(r'\s*,\s*(?:[a-z]+\s*,?\s*)*(?:and|or)\s+[a-z]+', '', s, flags=re.I)
    # Drop trailing "or rooms/floors/ceilings" without a leading comma
    # (e.g. "walls or rooms" → "walls"; doesn't affect "grade or slope")
    s = re.sub(r'\s+or\s+(?:rooms|floors|ceilings)$', '', s, flags=re.I)
    s = re.sub(r'\s+', ' ', s).strip(' ,.')
    # Title-case, leaving short connectives lowercase
    keep_lower = {'or','and','of','in','the','a'}
    parts = s.split()
    return ' '.join(
        w.capitalize() if (i == 0 or w.lower() not in keep_lower) else w.lower()
        for i, w in enumerate(parts)
    )


def _parse_lineage_detection_subskills(lineage):
    """Parse the SP demihuman page for a lineage's 'Mining Detection
    Abilities' bullet list and return [(short_skill_name, max_success, die_size), ...].
    Matches bullets like 'Detect X, 1–3 on 1d6' or 'Detect Y, 1 on 1d4'.
    Allows internal commas in the skill name (e.g. 'stonework traps, pits,
    and deadfalls') — the die-formula anchor is what terminates the phrase.
    Names are shortened to essentials by _shorten_detection_name."""
    if lineage in _lineage_detection_cache:
        return _lineage_detection_cache[lineage]
    rel = _LINEAGE_SP_DETECTION_FILES.get(lineage)
    out = []
    if rel:
        for label, html in _parse_sp_lineage_abilities_section(rel):
            if 'mining detection' not in label.lower(): continue
            text = re.sub(r'<[^>]+>', ' ', html)
            text = re.sub(r'\s+', ' ', text)
            pat = re.compile(
                # Capture from Detect/Determine up to the die-formula
                # anchor. Use a class that allows commas and stray periods
                # but stops at the *next* Detect/Determine so we don't
                # over-grab into the following bullet.
                r'((?:Detect|Determine)\s+(?:(?!Detect|Determine).){3,200}?)\s*[.,]?\s*'
                r'(?:1\s*[–\-]\s*)?(\d+)\s+on\s+1d(\d+)',
                re.I | re.DOTALL
            )
            for m in pat.finditer(text):
                name = re.sub(r'\s+', ' ', m.group(1)).strip().rstrip(',.')
                short = _shorten_detection_name(name)
                out.append((short, int(m.group(2)), int(m.group(3))))
    _lineage_detection_cache[lineage] = out
    return out


def _load_thief_skill_names():
    """Read the first column of SP00074 (Thief Skill Racial Adjustments)
    and return the list of 13 skill names in the same column order as the
    13-int32 values RACE.DAT stores per race. Returns [] if the file is
    missing or unparseable."""
    global _thief_skill_names_cache
    if _thief_skill_names_cache is not None:
        return _thief_skill_names_cache
    path = os.path.join(SOURCE_BASE, _SP_THIEF_SKILLS_FILE)
    out = []
    if os.path.exists(path):
        with open(path, 'r', encoding='cp1252') as f:
            soup = BeautifulSoup(f.read(), 'html.parser')
        for table in soup.find_all('table'):
            rows = []
            for tr in table.find_all('tr'):
                cells = [td.get_text(' ', strip=True) for td in tr.find_all(['td','th'])]
                if cells: rows.append(cells)
            if not rows: continue
            header = [c.lower() for c in rows[0]]
            if 'skill' not in header[0].lower(): continue
            # First column of every data row is the skill name
            for r in rows[1:]:
                if r and r[0]:
                    out.append(r[0])
            break
    _thief_skill_names_cache = out
    return out


def _normalize_ability_label(label):
    """Lowercase, drop parens/asterisks/trailing colon, collapse whitespace —
    used as the cross-source matching key for ability descriptions."""
    if not label: return ''
    s = label.lower().strip()
    s = re.sub(r'\([^)]*\)', '', s)
    s = s.rstrip('*: ').strip()
    s = re.sub(r'\s+', ' ', s)
    return s


def _ability_effect_changes(raw_name):
    """Map an ability label (CP-costed or otherwise — from SP bullets, per
    sub-race table cells, etc.) to a list of ARS v14 effect-change dicts.
    Returns [] for descriptive-only labels (no clean stock mechanic).
    See the ARS system documentation for effect/action patterns.

    Applied automatically by the race writer whenever an ability has no
    explicit `changes` already (so labels like "Racial Ability Modifiers"
    or "Base Movement N" that already carry hand-built changes are NOT
    overridden)."""
    if not raw_name: return []
    # Strip the cost suffix if present so both 'Stealth' and 'Stealth (10 CP)'
    # match the same patterns.
    raw_name = re.sub(r'\s*\(\d+\s*CP\)\s*$', '', raw_name)
    low = raw_name.lower()

    _CON_SAVE_FORMULA = '(min(5,max(1,round(@abilities.con.value/3.5))))'
    def _save(props, label):
        # Source the bonus magnitude from the S&P ability description at
        # runtime — never hardcode the rules value (copyright + accuracy).
        # If the +N can't be parsed, emit no change rather than fabricate.
        desc = _ability_description(label)
        m = re.search(r'\+\s*(\d+)\b', desc) if desc else None
        if not m:
            return []
        return [{'key':'system.mods.saves.all','type':'custom',
                 'value':{'formula':m.group(1),'properties':props},
                 'priority':20,'phase':'initial','last':''}]
    def _con_saves():
        # CON-based save bonus (dwarf/gnome/halfling PHB pattern):
        # applies to saves vs. poison, spell, rod, staff, wand only —
        # NOT vs. paralyzation, petrification, polymorph, or breath.
        # Formula from OSRIC dwarves/gnomes: round(CON/3.5), clamped 1-5.
        _c = {'type':'custom','priority':20,'phase':'initial','last':''}
        return [dict(_c, key=f'system.mods.saves.{s}',
                     value={'formula':_CON_SAVE_FORMULA,'properties':''})
                for s in ('poison','rod','staff','wand','spell')]
    # We emit a mechanical effect ONLY where ARS can apply the bonus
    # faithfully (and source its magnitude at runtime, never hardcoded):
    #   • resistances → a save bonus filtered to a damage type via `properties`
    #   • CON saves   → the per-save CON formula
    #   • infravision → token vision, range parsed from the description
    # The other S&P character-point buys are CONDITIONAL (vs a creature type,
    # a weapon subset, a terrain), per-level, per-race-variable, or raise an
    # S&P sub-ability score ARS Variant 2 does not track. ARS cannot gate a
    # flat effect on those conditions, so an unconditional `+N` would be both
    # wrong and a hardcoded rules value — we leave them descriptive-only.

    # ── Save bonus vs a damage type (the `properties` filter IS the condition) ──
    if low == 'cold resistance':         return _save('cold', raw_name)
    if low == 'heat resistance':         return _save('fire', raw_name)
    if low == 'poison resistance':       return _save('poison', raw_name)
    # ── CON-based save package (poison/rod/staff/wand/spell only) ──
    if low in ('saving throw bonuses','saving throw bonus','save bonus',
               'resistance'):
                                         return _con_saves()

    # ── Infravision (range read from the S&P description, e.g. "to 60 feet") ──
    if low == 'infravision':
        desc = _ability_description(raw_name)
        m = re.search(r'(\d+)\s*(?:feet|foot|ft)\b', desc) if desc else None
        if not m:
            return []
        return [{'key':'special.vision','type':'custom',
                 'value':{'range':int(m.group(1)),'angle':'360','mode':'basic'},
                 'priority':20,'phase':'initial'}]

    # ── Conditional / variable / sub-ability buys (Attack/Damage/Aim/Melee/
    #    Defensive/Hit-point/Health/Fitness bonus, per-weapon affinities,
    #    Stealth, Hide, Magic resistance, Secret Doors, …): descriptive only.
    #    The dwarf "+1 vs goblinoids" is already emitted as a proper
    #    `target.type` conditional effect via the PHB combat-bonus path
    #    (_build_race_combat_effect_docs), so it is not duplicated here. ──
    return []


# ── Runtime parsers for ability/action numeric values. Copyright: every rules
#    number (daily-use count, points-per-level, granted-spell list, detection
#    chance) is read from the user's source text at runtime, never hardcoded. ──
_WORD_NUM = {'once': 1, 'twice': 2, 'thrice': 3, 'one': 1, 'two': 2,
             'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7,
             'eight': 8, 'nine': 9, 'ten': 10}


def _strip_tags(text):
    return re.sub(r'<[^>]+>', ' ', text) if text else ''


def _parse_per_day(text):
    """Daily-use count from prose ('once a day', 'three times per day',
    '3/day'). Returns int, or 0 when none is stated — 0 means unlimited in
    ARS, so we never fabricate a frequency."""
    t = _strip_tags(text).lower()
    if not t:
        return 0
    m = re.search(r'(\d+|once|twice|thrice|one|two|three|four|five|six|'
                  r'seven|eight|nine|ten)\s+times?\s+(?:per|a)\s+day', t)
    if not m:
        m = re.search(r'(once|twice|thrice)\s+(?:per\s+|a\s+)?day', t)
    if not m:
        m = re.search(r'(\d+)\s*/\s*day', t)
    if not m:
        return 0
    g = m.group(1)
    return int(g) if g.isdigit() else _WORD_NUM.get(g, 0)


def _parse_points_per_level(text):
    """'N hit points per (experience) level' → N, else None."""
    t = _strip_tags(text).lower()
    m = re.search(r'(\d+)\s+(?:hit\s+)?points?\s+per\s+(?:experience\s+)?level', t)
    return int(m.group(1)) if m else None


def _parse_spell_casts(text):
    """Ordered list of spell names a racial 'Spell abilities' grants, parsed
    from prose like 'can cast X, Y, and Z ... can add A, B, and C'. Returns
    Title-Cased names ([] if unparseable)."""
    t = _strip_tags(text)
    out, seen = [], set()
    for m in re.finditer(r'\b(?:cast|add)\b,?\s+([a-z][a-z,\s]+?)'
                         r'(?:\s+as\s+a\b|\.|$)', t, re.I):
        for part in re.split(r',|\band\b', m.group(1)):
            nm = part.strip(' .')
            if nm and 2 <= len(nm) <= 40:
                key = nm.lower()
                if key not in seen:
                    seen.add(key)
                    out.append(nm.title())
    return out


def _parse_secret_door_chances():
    """Elf-style secret/concealed door detection chances, parsed in source
    order from the PHB Elf entry ('roll a 1 on 1d6', 'roll a 1 or 2 on 1d6',
    'roll a 1, 2, or 3 on 1d6'). Returns [(die, target), ...] or [] if the
    source can't be read. 'Elf' is a lookup key into _RACE_HTML_FILES, not
    game data."""
    txt = _phb_page_text('Elf')
    if not txt:
        return []
    out = []
    # "roll a 1 on 1d6" / "roll a 1 or 2 on 1d6" / "roll a 1, 2, or 3 on 1d6"
    for m in re.finditer(r'roll a ([\d,\s or]*?\d)\s+on 1d(\d+)', txt, re.I):
        die = int(m.group(2))
        faces = [int(x) for x in re.findall(r'\d+', m.group(1))]
        if faces:
            out.append((die, max(faces)))
    return out


def _ability_actions(raw_name):
    """Map an ability label to a list of action-group dicts. Returns []
    when the ability has no actionable mechanic worth a click-to-trigger
    card. See ARS_MECHANICS §3 for the action / actionGroup schema."""
    if not raw_name: return []
    raw_name = re.sub(r'\s*\(\d+\s*CP\)\s*$', '', raw_name)
    low = raw_name.lower()

    # ── Spell-like abilities: a single "cast" action that posts a card.
    # The chat card carries the description so the GM can resolve effects
    # manually. Charges populated when the rule is "once a day".
    SPELL_LIKE_DAILY = {
        'animal friendship': ('Animal Friendship', _FOUNDRY_ICON_DEFAULT, 'spell'),
        'companion':         ('Companion',         _FOUNDRY_ICON_DEFAULT, 'spell'),
        'speak with plants': ('Speak With Plants', _FOUNDRY_ICON_DEFAULT, 'spell'),
        'confer water breathing': ('Confer Water Breathing',
                                   _FOUNDRY_ICON_DEFAULT, 'spell'),
        'meld into stone':   ('Meld Into Stone',   _FOUNDRY_ICON_DEFAULT, 'spell'),
        'stone tell':        ('Stone Tell',        _FOUNDRY_ICON_DEFAULT, 'spell'),
        'freeze':            ('Freeze',            _FOUNDRY_ICON_DEFAULT, 'none'),
        'magic identification':  ('Identify Magic',  _FOUNDRY_ICON_DEFAULT, 'none'),
        'potion identification': ('Identify Potion', _FOUNDRY_ICON_DEFAULT, 'none'),
        'detect evil':       ('Detect Evil',       _FOUNDRY_ICON_DEFAULT, 'none'),
        'detect poison':     ('Detect Poison',     _FOUNDRY_ICON_DEFAULT, 'none'),
        'hideous appearance':('Cause Fear',        _FOUNDRY_ICON_DEFAULT,
                              'paralyzation'),   # save vs fear effect
        'create magical pipes': ('Play Magical Pipes',
                                 _FOUNDRY_ICON_DEFAULT, 'spell'),
        'sound mimicry':     ('Mimic Sound',      _FOUNDRY_ICON_DEFAULT, 'none'),
        'swan form':         ('Assume Swan Form', _FOUNDRY_ICON_DEFAULT, 'none'),
    }
    if low in SPELL_LIKE_DAILY:
        nm, icon, save = SPELL_LIKE_DAILY[low]
        # Frequency read from the description ('once a day', …); 0 = at-will
        # when the text states no limit — never a hardcoded daily count.
        per_day = _parse_per_day(_ability_description(raw_name))
        return [_make_action_group(nm, icon, [
            _make_action(nm, type_='cast', img=icon,
                         save_type=save, charges_per_day=per_day)
        ])]

    # ── Spell Abilities (Elf): the granted spell-like abilities and their
    # frequency are read from the ability description at runtime (never a
    # hardcoded spell list). Save type is left 'none' — the chat card carries
    # the description for manual resolution rather than guessing per spell.
    if low == 'spell abilities':
        desc = _ability_description(raw_name)
        spells = _parse_spell_casts(desc)
        if not spells:
            return []
        per_day = _parse_per_day(desc)
        return [_make_action_group('Spell Abilities', _FOUNDRY_ICON_DEFAULT, [
            _make_action(nm, type_='cast', img=_FOUNDRY_ICON_DEFAULT,
                         save_type='none', charges_per_day=per_day)
            for nm in spells
        ])]

    # ── Paralyzing Bite (Thri-kreen): bite melee → paralysis save. The bite
    # *damage* is not stated in this ability's text (it lives in the monster
    # stat block, not here), so no damage action is fabricated. The save type
    # is read from the description ('save versus poison') rather than guessed.
    if low == 'paralyzing bite':
        desc = _ability_description(raw_name)
        m = re.search(r'save\s+(?:vs\.?|versus)\s+(\w+)', desc or '', re.I)
        save = m.group(1).lower() if m else 'none'
        return [_make_action_group('Paralyzing Bite', _FOUNDRY_ICON_DEFAULT, [
            _make_action('Bite Attack', type_='melee', img=_FOUNDRY_ICON_DEFAULT,
                         targeting='single'),
            _make_action('Paralysis Save', type_='effect', img=_FOUNDRY_ICON_DEFAULT,
                         targeting='single', save_type=save,
                         effect_changes=[{
                             'key':'special.status', 'type':'custom',
                             'value':'paralysis', 'priority':20,
                             'phase':'initial'}]),
        ])]

    # ── Charge Attack: a clickable melee action. The +N to-hit and "double
    # damage" are described in the item text but ARS actions carry no to-hit-
    # modifier field, so no fabricated damage formula is emitted here.
    if low == 'charge attack':
        return [_make_action_group('Charge Attack', _FOUNDRY_ICON_DEFAULT, [
            _make_action('Charge (move + attack)', type_='melee',
                         img=_FOUNDRY_ICON_DEFAULT, targeting='single'),
        ])]

    # ── Leap (Thri-kreen / others): single-use movement burst
    if low == 'leap':
        return [_make_action_group('Leap', _FOUNDRY_ICON_DEFAULT, [
            _make_action('Leap', type_='cast', img=_FOUNDRY_ICON_DEFAULT,
                         targeting='self'),
        ])]

    # ── Determine Stability / Determine Age / Detect new construction /
    # detect sliding / detect stonework / detect sloping — these are
    # already rollable as standalone Skill items; the ability remains
    # descriptive.

    return []


def _lineage_cp_budget(base):
    """Parse the SP demihuman page's intro prose for 'N character points'
    and return N as int. Returns None when no value is found (or no SP
    page is mapped to this lineage)."""
    rel = _SP_DEMIHUMAN_FILES.get(base)
    if not rel: return None
    path = os.path.join(SOURCE_BASE, rel)
    if not os.path.exists(path): return None
    with open(path, 'r', encoding='cp1252') as f:
        txt = BeautifulSoup(f.read(), 'html.parser').get_text(' ', strip=True)
    m = re.search(r'(\d+)\s+character\s+points', txt, re.I)
    return int(m.group(1)) if m else None


def _parse_sp_lineage_abilities_section(sp_rel):
    """Parse the '{Lineage} Abilities' purple section at the bottom of a SP
    demihuman page (SP00024..SP00031). Returns list of (label, html).
    For SP pages with a dedicated '{Lineage} Abilities' purple heading
    (SP00024..SP00030) we use it as the section start; for SP00031 (Humans)
    which has only the top-level 'Humans' purple heading and no separate
    abilities section, we fall back to the first purple heading."""
    path = os.path.join(SOURCE_BASE, sp_rel)
    if not os.path.exists(path): return []
    with open(path, 'r', encoding='cp1252') as f:
        soup = BeautifulSoup(f.read(), 'html.parser')
    body = soup.find('body') or soup
    purple = [ft for ft in body.find_all('font')
              if ft.get('size') == '4'
              and str(ft.get('color') or '').lower() == '#800080']
    section_start = next((ft for ft in purple
                          if 'abilities' in ft.get_text(strip=True).lower()),
                         None)
    if section_start is None and purple:
        section_start = purple[0]
    if not section_start: return []
    out = []
    current_label = None
    current_chunks = []
    def _flush():
        if current_label and current_chunks:
            text = ' '.join(current_chunks).strip()
            if text:
                out.append((current_label, f'<p>{text}</p>'))
    for elem in section_start.next_elements:
        if not isinstance(elem, Tag): continue
        if elem.name == 'font' and elem.get('size') == '3' \
                and str(elem.get('color') or '').lower() == '#ff0000' \
                and elem.find('b'):
            txt = elem.get_text(' ', strip=True)
            if txt.endswith(':'):
                _flush()
                current_label = txt.rstrip(':').strip()
                current_chunks = []
                continue
        if elem.name == 'font' and elem.get('size') == '3' \
                and str(elem.get('color') or '').lower() != '#ff0000':
            t = elem.get_text(' ', strip=True)
            if t: current_chunks.append(t)
    _flush()
    return out


def _parse_sp_legend_descriptions(sp_rel):
    """Parse SP00033 / SP00034 legend pages. Format:
        'a. Charge Attack: <description...> b. Move Silently: <desc...>'
    Returns list of (ability_name, html)."""
    path = os.path.join(SOURCE_BASE, sp_rel)
    if not os.path.exists(path): return []
    with open(path, 'r', encoding='cp1252') as f:
        text = BeautifulSoup(f.read(), 'html.parser').get_text(' ', strip=True)
    out = []
    matches = list(re.finditer(r'\b([a-z]{1,2})\.\s+([A-Z][^:]{2,60}):', text))
    for i, m in enumerate(matches):
        name = m.group(2).strip()
        start = m.end()
        end = matches[i+1].start() if i+1 < len(matches) else len(text)
        desc = text[start:end].strip()
        # Trim trailing letter marker if any leaked in
        desc = re.sub(r'\s+[a-z]{1,2}\.\s*$', '', desc).strip()
        if desc:
            out.append((name, f'<p>{desc}</p>'))
    return out


_ABILITY_SUFFIX_RE = re.compile(r'\s+(bonus(?:es)?|abilities)$')


def _add_ability_desc(out, label, html):
    """Insert (label, html) into the descriptions dict under both the raw
    normalized key and a 'stripped' key without trailing 'bonus[es]' /
    'abilities' so we can match our labels even when they over- or
    under-specify those generic suffixes."""
    key = _normalize_ability_label(label)
    out.setdefault(key, html)
    stripped = _ABILITY_SUFFIX_RE.sub('', key).strip()
    if stripped and stripped != key:
        out.setdefault(stripped, html)


def _load_ability_descriptions():
    """Build {normalized_label → html} merging the SP demihuman lineage
    abilities sections and the SP exotic-race legend pages. Both the raw
    label and a suffix-stripped variant are registered so our internal
    labels match source headings even when the 'Bonus(es)' / 'Abilities'
    suffix differs."""
    global _ability_desc_cache
    if _ability_desc_cache is not None:
        return _ability_desc_cache
    out = {}
    # Order matters: _add_ability_desc is keep-first, so the four core
    # demihuman pages win for shared labels (e.g. the elf's Cold/Heat
    # resistance text). The half-races are appended only to supply labels
    # the core four lack — notably Half-Ogre's "Poison resistance".
    for base in ('Dwarf','Elf','Gnome','Halfling',
                 'Half-elf','Half-orc','Half-ogre'):
        for label, html in _parse_sp_lineage_abilities_section(_SP_DEMIHUMAN_FILES[base]):
            _add_ability_desc(out, label, html)
    for rel in _SP_ABILITY_LEGEND_FILES:
        for label, html in _parse_sp_legend_descriptions(rel):
            _add_ability_desc(out, label, html)
    _ability_desc_cache = out
    return out


def _ability_description(label):
    """Return cleaned HTML description for a racial ability label. Special
    cases the labels we generate internally (Base Movement, Racial Ability
    Modifiers, Resistance to Sleep/Charm, Languages); otherwise looks up
    the SP-sourced description table by normalized label, trying both the
    raw and suffix-stripped form. Returns '' if nothing matches."""
    if not label: return ''
    low = label.lower()
    if low.startswith('base movement'):
        return '<p>Base overland movement speed of this race.</p>'
    if 'racial ability modifiers' in low:
        return '<p>Ability score adjustments granted by this race.</p>'
    if low.startswith('resistance to sleep and charm'):
        return '<p>Innate resistance to <em>sleep</em> and <em>charm</em> spells.</p>'
    if low.startswith('language'):
        return '<p>Languages this race speaks at character creation.</p>'
    if low.startswith('vision, normal') or low == 'normal vision':
        return '<p>Sets the token to normal sight (no infravision).</p>'
    descs = _load_ability_descriptions()
    key = _normalize_ability_label(label)
    if key in descs: return descs[key]
    stripped = _ABILITY_SUFFIX_RE.sub('', key).strip()
    if stripped and stripped in descs:
        return descs[stripped]
    return ''


def _load_sp_ability_legend():
    """Combine SP00033 + SP00034 letter-code legends into {letter: name}."""
    global _sp_legend_cache
    if _sp_legend_cache is not None:
        return _sp_legend_cache
    out = {}
    for rel in _SP_ABILITY_LEGEND_FILES:
        path = os.path.join(SOURCE_BASE, rel)
        if not os.path.exists(path): continue
        with open(path, 'r', encoding='cp1252') as f:
            text = BeautifulSoup(f.read(), 'html.parser').get_text(' ', strip=True)
        for m in re.finditer(r'\b([a-z]{1,2})\.\s+([A-Z][^:]{2,60}):', text):
            letter, name = m.group(1), m.group(2).strip()
            if letter not in out:
                out[letter] = name
    _sp_legend_cache = out
    return out


def _load_sp_exotic_table():
    """Parse SP00059 master table → {race_name_lower: {mv, ac, hp, codes}}.
    `codes` is a list of single letters extracted from the Characteristics column."""
    global _sp_exotic_cache
    if _sp_exotic_cache is not None:
        return _sp_exotic_cache
    path = os.path.join(SOURCE_BASE, _SP_EXOTIC_TABLE_FILE)
    out = {}
    if os.path.exists(path):
        with open(path, 'r', encoding='cp1252') as f:
            soup = BeautifulSoup(f.read(), 'html.parser')
        prev_race = None
        for table in soup.find_all('table'):
            for row in table.find_all('tr'):
                cells = [td.get_text(' ', strip=True) for td in row.find_all('td')]
                if len(cells) < 6: continue
                race, ac, hp, mv, _, chars = cells[:6]
                if race.lower() == 'race': continue
                if not race and prev_race and chars:
                    # Continuation row — append codes to previous race
                    prev = out[prev_race]
                    prev['codes'].extend(_split_sp_codes(chars))
                    continue
                if not race: continue
                mv_int = None
                m = re.match(r'\s*(\d+)', mv)
                if m: mv_int = int(m.group(1))
                out[race.lower()] = {
                    'mv':    mv_int,
                    'ac':    ac,
                    'hp':    hp,
                    'codes': _split_sp_codes(chars),
                }
                prev_race = race.lower()
    _sp_exotic_cache = out
    return out


def _split_sp_codes(chars_cell):
    """Extract single-or-double-letter codes (a, b, ..., aa, bb, ...) from the
    Characteristics cell of SP00059. Ignores trailing parenthetical modifiers
    like '(40%)' or '(75%)'."""
    if not chars_cell: return []
    cleaned = re.sub(r'\([^)]*\)', '', chars_cell)
    out = []
    for m in re.finditer(r'\b([a-z]{1,2})\b', cleaned):
        c = m.group(1)
        if c not in out:
            out.append(c)
    return out


def _load_sp_subrace_abilities(sp_file_rel):
    """Parse one SP demihuman page; return {subrace_heading_text: [ability_cells]}.
    Each ability table is preceded in the document by a phrase like
    "Hill Dwarves' Special Abilities" (with a Windows-1252 smart apostrophe)."""
    if sp_file_rel in _sp_subrace_cache:
        return _sp_subrace_cache[sp_file_rel]
    path = os.path.join(SOURCE_BASE, sp_file_rel)
    out = {}
    if not os.path.exists(path):
        _sp_subrace_cache[sp_file_rel] = out
        return out
    with open(path, 'r', encoding='cp1252') as f:
        soup = BeautifulSoup(f.read(), 'html.parser')

    # Heading regex covers the variations actually present in SP race pages:
    #   "Hill Dwarves' Special Abilities"   (smart apostrophe \x92)
    #   "Hairfoots' Special Abilities"
    #   "Stout Racial Abilities"
    #   "Tallfellow Racial Abilities"
    #   "Half-Elf Standard Racial Abilities (20)"
    # The race name itself is in group 1. Allow ASCII/Windows-1252/Unicode quotes.
    HEADING_RE = re.compile(
        r"([A-Z][A-Za-z\- ]+?)(?:['’\x92]s?)?\s+(?:Standard\s+)?"
        r"(?:Special|Racial)\s+Abilities"
    )

    for table in soup.find_all('table'):
        # Walk siblings backward up the tree to collect preceding text
        bits = []
        el = table
        while True:
            prev = el.previous_sibling
            if prev is None:
                el = el.parent
                if el is None: break
                continue
            text = getattr(prev, 'get_text', lambda *a, **kw: str(prev))(' ', strip=True)
            if text:
                bits.insert(0, text)
                if sum(len(b) for b in bits) > 800: break
            el = prev
        preceding = ' '.join(bits)[-800:]
        m = HEADING_RE.search(preceding)
        if not m:
            continue
        heading = m.group(1).strip()
        cells = []
        for td in table.find_all('td'):
            txt = td.get_text(' ', strip=True)
            if not txt or txt in cells:
                continue
            # Skip score-bonus reference tables
            if re.match(r'^\d', txt) or txt.lower() in ('score', 'bonus'):
                continue
            cells.append(txt)
        if cells:
            out[heading] = cells
    _sp_subrace_cache[sp_file_rel] = out
    return out


def _ability_cell_to_infravision_ft(cell):
    """If a cell text like "Infravision, 90'" / "Infravision, 60 ft" is an
    infravision label, return its range in feet. Else return None."""
    if not cell or not re.search(r'infravision', cell, re.I):
        return None
    m = re.search(r"(\d+)\s*[\'’]", cell)        # 90'
    if m: return int(m.group(1))
    m = re.search(r"(\d+)\s*(?:ft|feet)", cell, re.I)
    return int(m.group(1)) if m else None


def _phb_page_text(base_race):
    """Plain text of the PHB chapter page for a base demihuman, or None
    if no PHB file is mapped or the file is missing."""
    rel = _RACE_HTML_FILES.get(base_race) if base_race else None
    if not rel:
        return None
    path = os.path.join(SOURCE_BASE, rel)
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='cp1252') as f:
        return BeautifulSoup(f.read(), 'html.parser').get_text(' ', strip=True)


def _phb_extract_infravision_feet(phb_text):
    """Return the infravision range mentioned in a PHB race chapter, or None.
    Matches phrasings like '...infravision enables them to see up to 60 feet
    in the dark.' or '60-foot infravision'."""
    m = re.search(r'infravision[^.]{0,80}?(\d+)\s*feet', phb_text, re.I)
    if m: return int(m.group(1))
    m = re.search(r'see\s+(?:up\s+to\s+)?(\d+)\s*feet[^.]{0,40}?dark', phb_text, re.I)
    return int(m.group(1)) if m else None


def _phb_extract_sleep_charm_pct(phb_text):
    """Return the sleep/charm resistance percentage from a PHB race chapter."""
    m = re.search(r'(\d+)\s*%[^.]{0,60}?(?:resistance|chance)[^.]{0,60}?(?:sleep|charm)',
                  phb_text, re.I)
    if m: return int(m.group(1))
    m = re.search(r'(?:sleep|charm)[^.]{0,60}?(\d+)\s*%[^.]{0,40}?(?:resistance|chance)',
                  phb_text, re.I)
    return int(m.group(1)) if m else None


def _normalize_creature_triggers(creature_list_str):
    """Split a prose creature list ("orcs, half-orcs, goblins, and hobgoblins")
    into lowercase singular trigger tokens (["orc","half-orc","goblin",
    "hobgoblin"]) for matching against an NPC's `system.details.type`.

    The creature names are NOT hardcoded — they come from the user's CD-ROM
    PHB text at runtime; this only reshapes the parsed string into tokens."""
    if not creature_list_str:
        return []
    s = re.sub(r'\b(?:and|or)\b', ',', creature_list_str, flags=re.I)
    toks = []
    for raw in s.split(','):
        w = raw.strip().lower()
        w = re.sub(r'^(?:the|a|an)\s+', '', w)
        w = w.strip(' .')
        if not w:
            continue
        w = re.sub(r's$', '', w)              # naive de-pluralize (orcs→orc)
        if w and w not in toks:
            toks.append(w)
    return toks


def _phb_extract_combat_bonuses(phb_text):
    """Parse the PHB race chapter for the two classic demihuman combat bonuses.

    Returns {'offensive': (magnitude, [triggers]) or None,
             'defensive': (magnitude, [triggers]) or None}.
    Both magnitude and the creature list are read from the CD-ROM text at
    runtime — nothing about the bonus is hardcoded. Returns None members when a
    pattern isn't present (so races without these bonuses yield nothing)."""
    out: dict[str, Any] = {'offensive': None, 'defensive': None}
    if not phb_text:
        return out
    # Offensive: "...add 1 to their [dice|attack] rolls to hit orcs, ... hobgoblins."
    m = re.search(r'add\s+(\d+)\s+to\s+(?:their\s+)?'
                  r'(?:dice|attack)\s+rolls?\s+to\s+hit\s+([^.]+?)\.',
                  phb_text, re.I)
    if m:
        trig = _normalize_creature_triggers(m.group(2))
        if trig:
            out['offensive'] = (int(m.group(1)), trig)
    # Defensive: "When ogres, ... titans attack <race>, these monsters must
    # subtract 4 from their attack rolls..."
    m = re.search(r'[Ww]hen\s+([^.]+?)\s+attack\s+\w+,?\s+these\s+monsters?\s+'
                  r'must\s+subtract\s+(\d+)\s+from\s+their\s+attack\s+rolls?',
                  phb_text, re.I)
    if m:
        trig = _normalize_creature_triggers(m.group(1))
        if trig:
            out['defensive'] = (int(m.group(2)), trig)
    return out


def _dedupe_triggers(triggers, category_members):
    """Drop specific-genus triggers already subsumed by a broad-category trigger
    in the same list, so a foe tagged with BOTH its genus and that category
    (e.g. an ogre's `details.type = "Ogre, Giant"`) matches only ONE change
    instead of two (which would double the bonus → the dwarf "-8 vs ogre" bug).

    `category_members` maps a category label (lowercased, e.g. "giant") to the
    set of genus tokens it covers (resolved from MONTYPE.DAT at runtime). When
    "giant" is among the triggers we drop "ogre"/"troll"/"titan"; gnoll/bugbear
    survive because their category ("goblinoid") is not a trigger here."""
    cm = category_members or {}
    cats_present = [t for t in triggers if t in cm]
    if not cats_present:
        return triggers
    covered = set()
    for c in cats_present:
        covered |= cm[c]
    return [t for t in triggers if t in cats_present or t not in covered]


def _build_race_combat_effect_docs(race_name, race_id, category_members=None):
    """Return standalone ActiveEffect docs for a race's PHB combat bonuses
    (empty list if the race's PHB chapter has none). One `target.type` change
    per foe for the to-hit bonus; one `attacker.type` change per foe for the
    defensive (attacker-penalty) bonus — matching OSRIC's effect shape. All
    values (magnitude, triggers) are extracted at runtime in
    `_phb_extract_combat_bonuses`; overlapping triggers are collapsed via
    `_dedupe_triggers` so each foe applies the bonus exactly once."""
    base = _resolve_base_race(race_name)
    bonuses = _phb_extract_combat_bonuses(_phb_page_text(base))
    origin = f"Compendium.{MODULE_ID}.adnd2-races.Item.{race_id}"
    docs = []
    off = bonuses.get('offensive')
    if off:
        mag, triggers = off
        triggers = _dedupe_triggers(triggers, category_members)
        changes = [{
            "key": "target.type", "type": "custom",
            "value": {"trigger": t, "type": "attack",
                      "properties": "", "formula": str(mag)},
        } for t in triggers]
        docs.append(_make_effect_doc(
            "Racial Attack Bonus", 'icons/svg/d20-highlight.svg',
            changes, origin, transfer=True))
    dfn = bonuses.get('defensive')
    if dfn:
        mag, triggers = dfn
        triggers = _dedupe_triggers(triggers, category_members)
        changes = [{
            "key": "attacker.type", "type": "custom",
            "value": {"trigger": t, "type": "attack",
                      "properties": "", "formula": f"-{mag}"},
        } for t in triggers]
        docs.append(_make_effect_doc(
            "Racial Defensive Bonus", 'icons/svg/shield.svg',
            changes, origin, transfer=True))
    return docs


def _build_taxonomy_category_members():
    """Resolve `_TAXONOMY_INDEX_MAP` against MONTYPE.DAT into
    {category_label_lower: {genus_token, ...}} — the genus tokens each broad
    category subsumes. Used by `_dedupe_triggers`. Empty when MONTYPE is
    missing or its record count doesn't match the expected layout."""
    names = list(parse_montype(_load_dat('MONTYPE.DAT')).keys())
    if len(names) != _MONTYPE_EXPECTED_COUNT:
        return {}
    out = {}
    for cat, idxs in _TAXONOMY_INDEX_MAP.items():
        toks = set()
        for i in idxs:
            if 0 <= i < len(names):
                # genus = first comma-segment ("Ogre, Half-" → "ogre",
                # "Skeleton, Giant" → "skeleton"), parentheticals stripped.
                genus = re.sub(r'\s*\([^)]*\)', '', names[i]).split(',', 1)[0].strip().lower()
                if genus:
                    toks.add(genus)
        out[cat.lower()] = toks
    return out


def _best_subrace_table_for(race_name, subrace_tables):
    """Find the heading in subrace_tables that best matches our DAT race name.
    Returns the cell list, or None.

    Matching rules (most-specific first):
      1. The full distinguishing word of the race (e.g. "Hill" for "Hill dwarf")
         appears in the heading.
      2. The base demihuman noun (e.g. "Halfling") appears in the heading.
      3. The race name is composed of pure qualifiers (e.g. "Standard half-elf"):
         strip qualifiers, then match the remaining base noun.
    """
    if not subrace_tables:
        return None
    QUALIFIERS = {'standard'}
    rn = re.sub(r'\([^)]*\)', '', race_name.lower()).strip()
    tokens = [t for t in re.split(r'[\s,\-]+', rn) if t]
    non_qual = [t for t in tokens if t not in QUALIFIERS]
    # 1. Try the first non-qualifier token (typically the distinguishing word
    #    or the base noun when there's only one)
    if non_qual:
        first = non_qual[0]
        for heading, cells in subrace_tables.items():
            if first in heading.lower():
                return cells
    # 2. Try ANY non-qualifier token against headings
    for tok in non_qual:
        for heading, cells in subrace_tables.items():
            if tok in heading.lower():
                return cells
    return None


def extract_race_runtime_data(race_name):
    """Pull additional race game-data from HTM (abilities, infravision).
    Note: movement now comes directly from RACE.DAT (parsed in parse_race_record),
    not from this function. Returns dict with keys: infravision_ft, abilities, source."""
    out = {'infravision_ft': None, 'abilities': [], 'source': None}
    base = _resolve_base_race(race_name)

    # --- Abilities (and infravision when buried in an ability cell) ---
    if base in _SP_DEMIHUMAN_FILES:
        subrace_tables = _load_sp_subrace_abilities(_SP_DEMIHUMAN_FILES[base])
        cells = _best_subrace_table_for(race_name, subrace_tables)
        if cells:
            out['abilities'] = list(cells)
            for c in cells:
                inf = _ability_cell_to_infravision_ft(c)
                if inf is not None:
                    out['infravision_ft'] = inf
                    break
            out['source'] = 'sp-demi'
        else:
            # Base demihuman with no matching SP sub-race table (e.g. "Elf",
            # "Halfling" — the SP tables are sub-race-specific). Fall back to
            # scanning the PHB chapter page for the few abilities we know how
            # to extract via regex: infravision range and sleep/charm resistance.
            phb_text = _phb_page_text(base)
            if phb_text:
                inf = _phb_extract_infravision_feet(phb_text)
                if inf is not None:
                    out['infravision_ft'] = inf
                    out['abilities'].append('Infravision')
                pct = _phb_extract_sleep_charm_pct(phb_text)
                if pct is not None:
                    out['abilities'].append(
                        f'Resistance to Sleep and Charm ({pct}%)')
                if out['abilities']:
                    out['source'] = 'phb'
    else:
        # Exotic race: look up codes in the master table, decode via the legend.
        exotic = _load_sp_exotic_table().get(race_name.lower())
        if exotic and exotic['codes']:
            legend = _load_sp_ability_legend()
            for code in exotic['codes']:
                name = legend.get(code)
                if name and name not in out['abilities']:
                    out['abilities'].append(name)
            # For exotic races whose code list includes 'd' (Infravision), pull
            # the range from the legend text itself instead of guessing.
            if 'd' in exotic['codes']:
                inf_text = _sp_legend_infravision_ft()
                if inf_text is not None:
                    out['infravision_ft'] = inf_text
            out['source'] = 'sp-exotic'

    return out


def _sp_legend_infravision_ft():
    """Read SP00033's 'Infravision: ...60 feet' text and return the parsed
    range. Cached via the legend cache. Returns None if not parseable."""
    path = os.path.join(SOURCE_BASE, _SP_ABILITY_LEGEND_FILES[0])
    if not os.path.exists(path): return None
    with open(path, 'r', encoding='cp1252') as f:
        text = BeautifulSoup(f.read(), 'html.parser').get_text(' ', strip=True)
    m = re.search(r'Infravision[^.]{0,80}?(\d+)\s*feet', text, re.I)
    return int(m.group(1)) if m else None


def _make_action(name, type_='cast', *, img='', targeting='single',
                 save_type='none', save_formula='', formula='',
                 damage_type='', speed=0, charges_per_day=0,
                 consume_item=False, effect_changes=None, description=''):
    """Build one ARS Action dict. Defaults work for a click-to-cast trigger
    that posts a chat card. Override per pattern (see ARS_MECHANICS §3).
    `consume_item` sets the OSRIC "drink/use one" resource (spends 1 of the
    owning item — e.g. a potion — when the action fires)."""
    resource = {"type": "none", "itemId": "", "reusetime": "",
                "count": {"cost": 0, "min": 0, "max": 0, "value": 0}}
    if charges_per_day > 0:
        resource = {"type": "charges", "itemId": "", "reusetime": "day",
                    "count": {"cost": 1, "min": 0, "max": charges_per_day,
                              "value": charges_per_day}}
    elif consume_item:
        resource = {"type": "item", "itemId": "", "reusetime": "",
                    "count": {"cost": 1, "min": 0, "max": 1, "value": 0},
                    "trackTime": 0}
    return {
        "id": make_id(), "sort": 0, "name": name, "img": img,
        "type": type_, "targeting": targeting, "successAction": "none",
        "formula": formula, "speed": speed, "damagetype": damage_type,
        "ability": "none",
        "abilityCheck": {"type": "none", "formula": ""},
        "saveCheck":   {"type": save_type, "formula": save_formula},
        "effect":      {"duration": {"formula": "", "type": "round"},
                        "changes":  effect_changes or []},
        "resource":    resource,
        "properties": [], "itemList": [], "effectList": [], "otherdmg": [],
        "description": description, "dmonlytext": "",
        "magicpotency": 0, "misc": "", "parentuuid": "",
        "castShape": {
            "shape": {"type":"circle"}, "coneShape": {"type":"circle"},
            "selection": {"type":"all"},
            "properties": {"radius":{"formula":""}, "range":{"formula":""},
                           "angle":{"formula":""}, "length":{"formula":""},
                           "width":{"formula":""},
                           "inRangeColor":"#f2ed69",
                           "outOfRangeColor":"#a80000"},
        },
    }


def _make_action_group(name, img, actions, description=''):
    """Build one ARS ActionGroup wrapping a list of actions."""
    return {
        "id": make_id(), "name": name, "img": img,
        "description": description, "sort": 0,
        "sourceuuid": "", "collapsedState": "",
        "origin": {"name":"", "level":0, "school":"", "sphere":""},
        "actions": actions,
    }


def _make_effect_doc(name, img, changes, origin_uuid, transfer=True,
                     description='', effect_id=None, aura=None):
    """Build a standalone ARS ActiveEffect document (subtype 'base').

    `aura`, when given, is merged into the `system.aura` block (e.g.
    {"enabled": True, "distance": 5}) — used for radius effects whose changes
    carry the `aura.`-prefixed keys ARS strips into a region-transferred effect.

    CRITICAL: ARS reads effect changes from `system.changes`, NOT Foundry's
    core top-level `changes` field — confirmed both in the ARS source
    (`effect.system.changes` everywhere) and against OSRIC 2026.05.20, whose
    round-tripped effects carry no top-level `changes` at all. Emitting
    top-level changes is a silent no-op. Each change is normalized to OSRIC's
    field shape: {key, value, priority, type, phase, last}."""
    norm = []
    for c in (changes or []):
        norm.append({
            "key":      c["key"],
            "value":    c.get("value"),
            "priority": c.get("priority", None),
            "type":     c.get("type", "add"),
            "phase":    c.get("phase", "initial"),
            "last":     c.get("last", ""),
        })
    return {
        "_id": effect_id or make_id(),
        "name": name,
        "img": img,
        "type": "base",
        "origin": origin_uuid,
        "duration": {"value": None, "units": "seconds",
                     "expiry": None, "expired": False},
        "disabled": False,
        "transfer": transfer,
        "statuses": [],
        "description": description,
        "tint": "#ffffff",
        "system": {
            "changes": norm,
            "aura": {"enabled": False, "distance": 10, "shape": "circle",
                     "disposition": "friendly", "permission": "all",
                     "color": "#ff0000", "opacity": 0.35,
                     "includeSource": False, "isAura": False,
                     "originUuid": "", "effectUuid": "", **(aura or {})},
        },
        "sort": 0,
        "start": None,
        "showIcon": 1,
        "folder": None,
        "flags": {},
        "_stats": _stats_block(),
    }


def make_ability_item(name, img, description='', effect_changes=None,
                      parent_race_id=None, action_groups=None):
    """Create a Foundry Ability item plus its (optional) attached ActiveEffect.
    Returns (ability_doc, effect_doc_or_None). The effect is keyed under the
    ability's own _id when written to the LevelDB.

    `action_groups`, when provided, populates `system.actionGroups[]` —
    the player gets a clickable card on the sheet that fires the chained
    actions (cast, save, damage, apply effect, etc.). See ARS_MECHANICS §3."""
    ability_id = make_id()
    effects_refs = []
    effect_doc = None
    if effect_changes:
        effect_doc = _make_effect_doc(
            name, img, effect_changes,
            origin_uuid=f"Compendium.{MODULE_ID}.adnd2-races.Item.{ability_id}",
            transfer=True)
        effects_refs.append(effect_doc["_id"])
    ability_doc = {
        "_id": ability_id,
        "name": name,
        "type": "ability",
        "img": img,
        "system": {
            "description": description,
            "dmonlytext": "",
            "itemList": [],
            "alias": "",
            "attributes": {"rarity": "", "type": "", "subtype": "", "magic": False,
                           "properties": [], "skillmods": {}, "conditionals": [],
                           "identified": True, "size": "medium"},
            "charges": {"value": 0, "min": 0, "max": 0, "reuse": "none"},
            "location": {"state": "carried", "parent": parent_race_id or ""},
            "resource": {"itemId": ""},
            "actions": [], "quantity": 0, "weight": 0,
            "cost": {"value": 0, "currency": "gp"},
            "source": "", "xp": 0, "abilityList": [],
            "actionGroups": action_groups or [],
        },
        "effects": effects_refs,
        "folder": None, "sort": 0,
        "ownership": {"default": 0},
        "flags": {}, "_stats": _stats_block(),
    }
    return ability_doc, effect_doc


def make_skill_item(name, img, formula, target, type_='decending',
                    groups='', description=''):
    """Create a Foundry Skill item with the rollable-skill mechanics in
    `system.features`. `type_` is 'decending' (sic, ARS schema typo) for
    roll-under or 'ascending' for roll-over. `target` is the numeric
    success threshold (or an @-formula string for ability-driven rolls)."""
    skill_id = make_id()
    return {
        "_id": skill_id,
        "name": name,
        "type": "skill",
        "img": img,
        "effects": [],
        "system": {
            "description": description,
            "dmonlytext": "",
            "itemList": [],
            "alias": "",
            "attributes": {"rarity": "", "type": "", "subtype": "", "magic": False,
                           "properties": [], "skillmods": [], "conditionals": [],
                           "identified": True, "size": "medium",
                           "material": "leather_book"},
            "charges": {"value": 0, "min": 0, "max": 0, "reuse": "none"},
            "location": {"state": "carried", "parent": ""},
            "resource": {"itemId": ""},
            "actions": [], "quantity": 0, "weight": 0,
            "source": "", "xp": 0,
            "audio": {"file":"", "volume":0.5, "effect":"", "success":"", "failure":""},
            "groups": groups,
            "features": {
                "type":    type_,
                "ability": "none",
                "target":  str(target),
                "formula": formula,
                "cost":    0,
                "modifiers": {
                    "formula":"0", "class":0, "background":0, "ability":0,
                    "armor":0, "item":0, "race":0, "other":0,
                },
            },
            "migrate": False,
            "actionGroups": [],
            "rank": {"levels": {"max":1, "arcane":1, "divine":1}},
        },
        "folder": None, "sort": 0,
        "ownership": {"default": 0},
        "flags": {}, "_stats": _stats_block(),
    }


# ─── Character kits → background items ─────────────────────────────────────────

# Generic role/theme keyword → core Foundry icon. Same accepted pattern as the
# spell/item icon maps: the keys are generic fantasy-role words matched (whole
# word) against the kit name read from the user's DAT, not copyrighted data.
# Order matters — first whole-word hit wins, so list specific terms first.
_KIT_ICON_KEYWORDS = [
    ('assassin',  'icons/skills/melee/strike-dagger-poison-dripping-green.webp'),
    ('spy',       'icons/skills/social/intimidation-impressing.webp'),
    ('smuggler',  'icons/environment/settlement/ship.webp'),
    ('pirate',    'icons/environment/settlement/ship.webp'),
    ('buccaneer', 'icons/environment/settlement/ship.webp'),
    ('mariner',   'icons/environment/settlement/ship.webp'),
    ('sailor',    'icons/environment/settlement/ship.webp'),
    ('sea',       'icons/environment/settlement/ship.webp'),
    ('archer',    'icons/skills/ranged/arrow-flying-broadhead-metal.webp'),
    ('sharpshooter', 'icons/skills/ranged/arrow-flying-broadhead-metal.webp'),
    ('bow',       'icons/skills/ranged/arrow-flying-broadhead-metal.webp'),
    ('rider',     'icons/environment/creatures/horse-brown.webp'),
    ('beast',     'icons/environment/creatures/horse-brown.webp'),
    ('animal',    'icons/environment/creatures/horse-brown.webp'),
    ('cavalier',  'icons/environment/creatures/horse-brown.webp'),
    ('barbarian', 'icons/weapons/axes/axe-battle-black.webp'),
    ('berserker', 'icons/weapons/axes/axe-battle-black.webp'),
    ('savage',    'icons/weapons/axes/axe-battle-black.webp'),
    ('gladiator', 'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('weapon master', 'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('soldier',   'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('myrmidon',  'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('militant',  'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('swashbuckler', 'icons/skills/melee/hand-grip-sword-orange.webp'),
    ('blade',     'icons/skills/melee/hand-grip-sword-orange.webp'),
    ('priest',    'icons/magic/holy/prayer-hands-glowing-yellow.webp'),
    ('prophet',   'icons/magic/holy/prayer-hands-glowing-yellow.webp'),
    ('votary',    'icons/magic/holy/prayer-hands-glowing-yellow.webp'),
    ('paladin',   'icons/magic/holy/angel-winged-humanoid-blue.webp'),
    ('druid',     'icons/environment/wilderness/tree-oak.webp'),
    ('forest',    'icons/environment/wilderness/tree-oak.webp'),
    ('wilderness','icons/environment/wilderness/tree-oak.webp'),
    ('wanderer',  'icons/environment/wilderness/tree-oak.webp'),
    ('wizard',    'icons/sundries/books/book-embossed-jewel-gold-purple.webp'),
    ('mage',      'icons/sundries/books/book-embossed-jewel-gold-purple.webp'),
    ('sorceress', 'icons/sundries/books/book-embossed-jewel-gold-purple.webp'),
    ('witch',     'icons/sundries/books/book-embossed-jewel-gold-purple.webp'),
    ('scholar',   'icons/sundries/books/book-embossed-jewel-gold-purple.webp'),
    ('loremaster','icons/sundries/books/book-embossed-jewel-gold-purple.webp'),
    ('bard',      'icons/tools/instruments/lute-gold-brown.webp'),
    ('minstrel',  'icons/tools/instruments/lute-gold-brown.webp'),
    ('jester',    'icons/tools/instruments/lute-gold-brown.webp'),
    ('jongleur',  'icons/tools/instruments/lute-gold-brown.webp'),
    ('skald',     'icons/tools/instruments/lute-gold-brown.webp'),
    ('chanter',   'icons/tools/instruments/lute-gold-brown.webp'),
    ('noble',     'icons/commodities/treasure/crown-gold-laurel-wreath.webp'),
    ('patrician', 'icons/commodities/treasure/crown-gold-laurel-wreath.webp'),
    ('highborn',  'icons/commodities/treasure/crown-gold-laurel-wreath.webp'),
    ('diplomat',  'icons/skills/social/diplomacy-handshake.webp'),
    ('envoy',     'icons/skills/social/diplomacy-handshake.webp'),
    ('herald',    'icons/skills/social/diplomacy-handshake.webp'),
    ('merchant',  'icons/commodities/currency/coins-assorted-mix-copper.webp'),
    ('trader',    'icons/commodities/currency/coins-assorted-mix-copper.webp'),
    ('thief',     'icons/skills/social/theft-pickpocket-bribery-brown.webp'),
    ('burglar',   'icons/skills/social/theft-pickpocket-bribery-brown.webp'),
    ('cutpurse',  'icons/skills/social/theft-pickpocket-bribery-brown.webp'),
    ('bandit',    'icons/skills/social/theft-pickpocket-bribery-brown.webp'),
    ('outlaw',    'icons/skills/social/theft-pickpocket-bribery-brown.webp'),
    ('beggar',    'icons/environment/people/commoner.webp'),
    ('begger',    'icons/environment/people/commoner.webp'),
    ('peasant',   'icons/environment/people/commoner.webp'),
    ('urchin',    'icons/environment/people/commoner.webp'),
    ('healer',    'icons/magic/life/heart-cross-green.webp'),
    ('medician',  'icons/magic/life/heart-cross-green.webp'),
    ('explorer',  'icons/tools/navigation/map-marked-green.webp'),
    ('scout',     'icons/tools/navigation/map-marked-green.webp'),
    ('pathfinder','icons/tools/navigation/map-marked-green.webp'),
    ('wayfinder', 'icons/tools/navigation/map-marked-green.webp'),
    ('mountain',  'icons/environment/wilderness/cave-entrance-mountain.webp'),
    # Extra exact tokens (whole-word) to cut the neutral-default rate; the
    # Complete-handbook race kits often use compound single-word names that a
    # substring keyword can't reach, so they are listed explicitly.
    ('amazon',    'icons/skills/melee/hand-grip-sword-orange.webp'),
    ('samurai',   'icons/weapons/swords/sword-katana.webp'),
    ('monk',      'icons/skills/melee/unarmed-punch-fist.webp'),
    ('pugilist',  'icons/skills/melee/unarmed-punch-fist.webp'),
    ('thug',      'icons/skills/melee/unarmed-punch-fist.webp'),
    ('acrobat',   'icons/skills/movement/feet-winged-boots-brown.webp'),
    ('tumbler',   'icons/skills/movement/feet-winged-boots-brown.webp'),
    ('hunter',    'icons/skills/ranged/arrow-flying-broadhead-metal.webp'),
    ('huntsman',  'icons/skills/ranged/arrow-flying-broadhead-metal.webp'),
    ('ranger',    'icons/skills/ranged/arrow-flying-broadhead-metal.webp'),
    ('stalker',   'icons/magic/perception/silhouette-stealth-shadow.webp'),
    ('infiltrator','icons/magic/perception/silhouette-stealth-shadow.webp'),
    ('vanisher',  'icons/magic/perception/silhouette-stealth-shadow.webp'),
    ('seeker',    'icons/tools/navigation/map-marked-green.webp'),
    ('warden',    'icons/environment/wilderness/tree-oak.webp'),
    ('beastmaster','icons/environment/creatures/horse-brown.webp'),
    ('beastfriend','icons/environment/creatures/horse-brown.webp'),
    ('falconer',  'icons/environment/creatures/horse-brown.webp'),
    ('feralan',   'icons/environment/creatures/horse-brown.webp'),
    ('hivemaster','icons/environment/creatures/horse-brown.webp'),
    ('fence',     'icons/skills/social/theft-pickpocket-bribery-brown.webp'),
    ('swindler',  'icons/skills/social/theft-pickpocket-bribery-brown.webp'),
    ('charlatan', 'icons/skills/social/theft-pickpocket-bribery-brown.webp'),
    ('bilker',    'icons/skills/social/theft-pickpocket-bribery-brown.webp'),
    ('spellfilcher','icons/skills/social/theft-pickpocket-bribery-brown.webp'),
    ('mouseburglar','icons/skills/social/theft-pickpocket-bribery-brown.webp'),
    ('locksmith', 'icons/skills/social/theft-pickpocket-bribery-brown.webp'),
    ('tunnelrat', 'icons/skills/social/theft-pickpocket-bribery-brown.webp'),
    ('avenger',   'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('inquisitor','icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('justifier', 'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('champion',  'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('vindicator','icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('militarist','icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('mercenary', 'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('guardian',  'icons/skills/melee/shield-block-gray-orange.webp'),
    ('hearth',    'icons/skills/melee/shield-block-gray-orange.webp'),
    ('battlerager','icons/weapons/axes/axe-battle-black.webp'),
    ('goblinsticker','icons/skills/melee/spear-tips-triple-orange.webp'),
    ('wyrmslayer','icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('slayer',    'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('killer',    'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('ghosthunter','icons/magic/holy/prayer-hands-glowing-yellow.webp'),
    ('oracle',    'icons/magic/perception/eye-ringed-glow-angry-small-teal.webp'),
    ('divinate',  'icons/magic/perception/eye-ringed-glow-angry-small-teal.webp'),
    ('professor', 'icons/sundries/books/book-embossed-jewel-gold-purple.webp'),
    ('academician','icons/sundries/books/book-embossed-jewel-gold-purple.webp'),
    ('adviser',   'icons/sundries/books/book-embossed-jewel-gold-purple.webp'),
    ('treetender','icons/environment/wilderness/tree-oak.webp'),
    ('leaftender','icons/environment/wilderness/tree-oak.webp'),
    ('forestwalker','icons/environment/wilderness/tree-oak.webp'),
    ('greenwood', 'icons/environment/wilderness/tree-oak.webp'),
    ('rocktender','icons/environment/wilderness/cave-entrance-mountain.webp'),
    ('cartographer','icons/tools/navigation/map-marked-green.webp'),
    ('traveler',  'icons/tools/navigation/map-marked-green.webp'),
    ('windrider', 'icons/environment/creatures/horse-brown.webp'),
    ('skyrider',  'icons/environment/creatures/horse-brown.webp'),
    ('squire',    'icons/environment/creatures/horse-brown.webp'),
    ('equerry',   'icons/environment/creatures/horse-brown.webp'),
    ('entertainer','icons/tools/instruments/lute-gold-brown.webp'),
    ('buffoon',   'icons/tools/instruments/lute-gold-brown.webp'),
    ('whistler',  'icons/tools/instruments/lute-gold-brown.webp'),
    # Additional keywords to cover CRE race-kits and other named archetypes
    ('anagakok',   'icons/magic/perception/eye-ringed-glow-angry-small-teal.webp'),
    ('bladesinger','icons/skills/melee/hand-grip-sword-orange.webp'),
    ('breachgnome','icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('clansdwarf', 'icons/equipment/shield/heater-wooden-blue.webp'),
    ('ghetto',     'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('pest controller','icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('temple guard','icons/magic/holy/prayer-hands-glowing-yellow.webp'),
    ('outcast',    'icons/environment/people/commoner.webp'),
    ('pariah',     'icons/environment/people/commoner.webp'),
    ('homesteader','icons/environment/people/commoner.webp'),
    ('collector',  'icons/commodities/treasure/crown-gold-laurel-wreath.webp'),
    ('gallant',    'icons/skills/social/diplomacy-peace-alliance.webp'),
    ('expatriate', 'icons/tools/navigation/map-marked-green.webp'),
    ('troubleshooter','icons/tools/navigation/map-marked-green.webp'),
    ('investigator','icons/magic/perception/eye-ringed-green.webp'),
    ('riddlemaster','icons/magic/symbols/question-stone-yellow.webp'),
    ('mystic',     'icons/magic/symbols/circle-ouroboros.webp'),
    ('meistersinger','icons/tools/instruments/harp-gold-glowing.webp'),
    ('thespian',   'icons/tools/instruments/harp-gold-glowing.webp'),
    ('herbalist',  'icons/magic/life/heart-cross-green.webp'),
    ('shapeshifter','icons/magic/nature/seed-acorn-glowing-green.webp'),
    ('sheriff',    'icons/equipment/shield/heater-wooden-blue.webp'),
    ('wu jen',     'icons/sundries/books/book-embossed-jewel-gold-purple.webp'),
    ('imagemaker', 'icons/sundries/books/book-embossed-jewel-gold-purple.webp'),
    ('natural philosopher','icons/sundries/books/book-embossed-jewel-gold-purple.webp'),
    ('adventurer', 'icons/environment/wilderness/cave-entrance-mountain.webp'),
    ('axe for hire','icons/weapons/axes/axe-battle-black.webp'),
]
_KIT_ICON_DEFAULT = 'icons/sundries/documents/document-sealed-signatures-red.webp'


def _kit_icon(name):
    """Pick a generic core Foundry icon for a kit from a whole-word match on its
    name; falls back to a neutral 'document' icon. Decorative only."""
    low = name.lower()
    for kw, icon in _KIT_ICON_KEYWORDS:
        if _kw_hit(low, kw):
            return icon
    return _KIT_ICON_DEFAULT


# Discipline index → name string, sourced from PSIONIC.DAT int32 at +4 after name.
# Indices confirmed against 231 CPsionicPowerOb records (probe 2026-05-28).
_DISC_NAMES = {
    0: 'Clairsentience',
    1: 'Psychokinesis',
    2: 'Psychometabolism',
    3: 'Psychoportation',
    4: 'Telepathy',
}

# Whole-word keyword → verified Foundry icon path, for psionic power items.
# All icon paths verified against FVTT/public/icons/ 2026-05-28.
# Ordered: most-specific first to prevent short keywords from shadowing longer ones.
_POWER_ICON_KEYWORDS = [
    # Perception / distant sensing
    ('clairvoyance',  'icons/magic/perception/orb-crystal-ball-scrying-blue.webp'),
    ('clairaudience', 'icons/magic/sonic/projectile-sound-rings-wave.webp'),
    ('precognition',  'icons/magic/time/hourglass-tilted-glowing-gold.webp'),
    ('sensitivity',   'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('detection',     'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('sight',         'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('vision',        'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('see',           'icons/magic/perception/eye-slit-orange.webp'),
    ('reading',       'icons/magic/perception/orb-crystal-ball-scrying-blue.webp'),
    ('scrying',       'icons/magic/perception/orb-crystal-ball-scrying-blue.webp'),
    # Sound / hearing
    ('sound',         'icons/magic/sonic/projectile-sound-rings-wave.webp'),
    ('hear',          'icons/magic/sonic/projectile-sound-rings-wave.webp'),
    ('scream',        'icons/magic/sonic/scream-wail-shout-teal.webp'),
    # Time
    ('time',          'icons/magic/time/hourglass-tilted-glowing-gold.webp'),
    ('temporal',      'icons/magic/time/clock-spinning-gold-pink.webp'),
    ('hourglass',     'icons/magic/time/hourglass-tilted-glowing-gold.webp'),
    # Teleportation / dimensional travel
    ('teleport',      'icons/magic/movement/portal-vortex-orange.webp'),
    ('astral',        'icons/magic/movement/portal-vortex-orange.webp'),
    ('dimensional',   'icons/magic/movement/portal-vortex-orange.webp'),
    ('dimension',     'icons/magic/movement/portal-vortex-orange.webp'),
    ('blink',         'icons/magic/movement/portal-vortex-orange.webp'),
    ('phase',         'icons/magic/movement/portal-vortex-orange.webp'),
    ('planar',        'icons/magic/air/air-burst-spiral-large-pink.webp'),
    ('ethereal',      'icons/magic/air/fog-gas-smoke-dense-gray.webp'),
    ('travel',        'icons/magic/movement/portal-vortex-orange.webp'),
    ('wormhole',      'icons/magic/movement/portal-vortex-orange.webp'),
    ('wrench',        'icons/magic/symbols/circle-ouroboros.webp'),
    # Flight / levitation / acceleration
    ('levitation',    'icons/magic/control/buff-flight-wings-blue.webp'),
    ('flight',        'icons/magic/control/buff-flight-wings-blue.webp'),
    ('acceleration',  'icons/magic/movement/acceleration-speed-tech-blue.webp'),
    ('accelerate',    'icons/magic/movement/acceleration-speed-tech-blue.webp'),
    # Mind control / domination
    ('domination',    'icons/magic/control/control-influence-puppet.webp'),
    ('encase',        'icons/magic/control/encase-creature-humanoid-hold.webp'),
    # Mind link / telepathic communication
    ('mindlink',      'icons/magic/control/energy-stream-link-blue.webp'),
    ('convergence',   'icons/magic/control/energy-stream-link-blue.webp'),
    ('empathy',       'icons/magic/control/energy-stream-link-blue.webp'),
    ('link',          'icons/magic/control/energy-stream-link-blue.webp'),
    ('send',          'icons/magic/control/energy-stream-link-blue.webp'),
    ('messenger',     'icons/magic/control/energy-stream-link-blue.webp'),
    # Fear / emotion control
    ('phobia',        'icons/magic/control/fear-fright-jackolantern-yellow.webp'),
    ('fear',          'icons/magic/control/fear-fright-jackolantern-yellow.webp'),
    ('aversion',      'icons/magic/control/fear-fright-jackolantern-yellow.webp'),
    ('attraction',    'icons/magic/control/energy-stream-link-blue.webp'),
    # Explosion / destruction
    ('detonate',      'icons/magic/fire/explosion-embers-orange.webp'),
    ('ultrablast',    'icons/magic/fire/explosion-embers-orange.webp'),
    ('disintegrate',  'icons/magic/lightning/bolt-forked-blue.webp'),
    # Lightning / electricity / magnetism
    ('static',        'icons/magic/lightning/bolt-forked-blue.webp'),
    ('discharge',     'icons/magic/lightning/bolt-forked-blue.webp'),
    ('magnetize',     'icons/magic/lightning/bolt-forked-blue.webp'),
    ('magnatize',     'icons/magic/lightning/bolt-forked-blue.webp'),
    # Cold / ice
    ('cryokinesis',   'icons/magic/water/barrier-ice-crystal-wall-faceted-blue.webp'),
    # Healing / life / recovery
    ('healing',       'icons/magic/life/cross-beam-green.webp'),
    ('heal',          'icons/magic/life/cross-beam-green.webp'),
    ('regenerate',    'icons/magic/life/cross-beam-green.webp'),
    ('biofeedback',   'icons/magic/life/cross-beam-green.webp'),
    ('cell',          'icons/magic/life/cross-beam-green.webp'),
    ('lend',          'icons/magic/life/cross-beam-green.webp'),
    # Poison
    ('poison',        'icons/magic/death/projectile-skull-animal-green.webp'),
    # Death / energy drain
    ('death',         'icons/magic/death/hand-withered-gray.webp'),
    ('draining',      'icons/magic/death/hand-withered-gray.webp'),
    ('drain',         'icons/magic/death/hand-withered-gray.webp'),
    ('vampirism',     'icons/magic/death/hand-withered-gray.webp'),
    # Body / physical transformation
    ('metamorphosis', 'icons/magic/nature/elemental-plant-humanoid.webp'),
    ('shadowform',    'icons/magic/air/fog-gas-smoke-dense-gray.webp'),
    ('chameleon',     'icons/magic/air/fog-gas-smoke-dense-gray.webp'),
    # Shield / barrier / protection
    ('inertial',      'icons/magic/air/air-pressure-shield-blue.webp'),
    ('barrier',       'icons/magic/defensive/shield-barrier-blue.webp'),
    ('immovability',  'icons/magic/earth/barrier-stone-brown-green.webp'),
    ('rigidity',      'icons/magic/earth/barrier-stone-brown-green.webp'),
    # Invisibility / concealment / smoke
    ('invisibility',  'icons/magic/air/fog-gas-smoke-dense-gray.webp'),
    ('conceal',       'icons/magic/air/fog-gas-smoke-dense-gray.webp'),
    ('suppress',      'icons/magic/defensive/shield-barrier-deflect-teal.webp'),
    # Telekinesis / kinetic force
    ('telekinesis',   'icons/magic/air/air-pressure-shield-blue.webp'),
    ('telekinetic',   'icons/magic/air/air-pressure-shield-blue.webp'),
    ('kinetic',       'icons/magic/air/air-pressure-shield-blue.webp'),
    ('megakinesis',   'icons/magic/air/air-pressure-shield-blue.webp'),
    ('project',       'icons/magic/air/air-wave-gust-blue.webp'),
    # Molecular / small-scale manipulation
    ('molecular',     'icons/magic/earth/construct-stone.webp'),
    ('compact',       'icons/magic/earth/construct-stone.webp'),
    # Body / anatomy
    ('body',          'icons/skills/wounds/anatomy-organ-heart-red.webp'),
    ('aura',          'icons/magic/symbols/circle-ouroboros.webp'),
    # Psionic mental powers
    ('probe',         'icons/skills/wounds/anatomy-organ-brain-pink-red.webp'),
    ('surgery',       'icons/skills/wounds/anatomy-organ-brain-pink-red.webp'),
    ('mindwipe',      'icons/skills/wounds/anatomy-organ-brain-pink-red.webp'),
    ('mindflame',     'icons/skills/wounds/anatomy-organ-brain-pink-red.webp'),
    ('synaptic',      'icons/skills/wounds/anatomy-organ-brain-pink-red.webp'),
    ('psychic',       'icons/magic/symbols/circle-ouroboros.webp'),
    # Psionic combat modes — attack
    ('whip',          'icons/magic/lightning/bolt-beam-strike-blue.webp'),
    ('insinuation',   'icons/magic/control/control-influence-puppet.webp'),
    ('blast',         'icons/magic/lightning/bolt-forked-large-blue.webp'),
    # Psionic combat modes — defense
    ('fortress',      'icons/magic/defensive/barrier-shield-dome-blue-purple.webp'),
    ('thought',       'icons/magic/defensive/shield-barrier-deflect-teal.webp'),
    ('tower',         'icons/magic/defensive/armor-shield-barrier-steel.webp'),
    ('mind',          'icons/magic/perception/eye-tendrils-web-purple.webp'),
    # Animal / plant / nature
    ('animal',        'icons/environment/creatures/horse-brown.webp'),
    ('beast',         'icons/environment/creatures/horse-brown.webp'),
    ('insect',        'icons/environment/wilderness/tree-oak.webp'),
    ('plant',         'icons/environment/wilderness/tree-oak.webp'),
    ('photosynthesis','icons/environment/wilderness/tree-oak.webp'),
    # Strength / energy
    ('strength',      'icons/skills/wounds/anatomy-organ-heart-red.webp'),
    ('energy',        'icons/magic/lightning/bolt-forked-blue.webp'),
    # Attack / combat
    ('attack',        'icons/skills/ranged/arrow-flying-broadhead-metal.webp'),
    ('inflict',       'icons/magic/fire/blast-jet-stream-embers-orange.webp'),
    ('pain',          'icons/magic/fire/blast-jet-stream-embers-orange.webp'),
    # Kinetic / animate / create
    ('animate',       'icons/skills/movement/figure-running-gray.webp'),
    ('control',       'icons/magic/control/encase-creature-humanoid-hold.webp'),
    ('create',        'icons/magic/symbols/ring-circle-smoke-blue.webp'),
    ('summon',        'icons/magic/symbols/ring-circle-smoke-blue.webp'),
    ('banishment',    'icons/magic/holy/prayer-hands-glowing-yellow.webp'),
    # Illusion / false senses / hallucination
    ('hallucination', 'icons/magic/air/fog-gas-smoke-dense-gray.webp'),
    ('false',         'icons/magic/air/fog-gas-smoke-dense-gray.webp'),
    ('daydream',      'icons/magic/air/fog-gas-smoke-dense-gray.webp'),
    # Mental influence / suggestion / memory
    ('suggestion',    'icons/magic/control/control-influence-puppet.webp'),
    ('amnesia',       'icons/magic/control/control-influence-puppet.webp'),
    ('post-hypnotic', 'icons/magic/control/control-influence-puppet.webp'),
    ('hivemind',      'icons/magic/control/energy-stream-link-blue.webp'),
    ('acceptance',    'icons/magic/symbols/circle-ouroboros.webp'),
    # Navigation / direction / location
    ('navigation',    'icons/tools/navigation/map-marked-green.webp'),
    ('radial',        'icons/tools/navigation/compass-plain-blue.webp'),
    ('know',          'icons/tools/navigation/map-marked-green.webp'),
    ('course',        'icons/tools/navigation/compass-plain-blue.webp'),
    ('direction',     'icons/tools/navigation/compass-plain-blue.webp'),
    # Knowledge / lore / cosmic
    ('cosmic',        'icons/magic/perception/orb-crystal-ball-scrying-blue.webp'),
    ('awareness',     'icons/magic/perception/orb-crystal-ball-scrying-blue.webp'),
    ('appraise',      'icons/magic/perception/orb-crystal-ball-scrying-blue.webp'),
    ('lore',          'icons/magic/perception/orb-crystal-ball-scrying-blue.webp'),
    ('retrospection', 'icons/magic/time/arrows-circling-green.webp'),
    ('predestination','icons/magic/time/arrows-circling-green.webp'),
    ('probability',   'icons/magic/time/arrows-circling-green.webp'),
    ('subjective',    'icons/magic/symbols/question-stone-yellow.webp'),
    ('safe',          'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('psionic',       'icons/magic/perception/eye-tendrils-web-purple.webp'),
    # Spirit / ghost / ethereal
    ('spirit',        'icons/magic/symbols/ring-circle-smoke-blue.webp'),
    ('ghost',         'icons/magic/symbols/ring-circle-smoke-blue.webp'),
    ('ectoplasmic',   'icons/magic/symbols/ring-circle-smoke-blue.webp'),
    # Physical armor / protection
    ('carapace',      'icons/equipment/shield/buckler-iron-cross-gray.webp'),
    ('armor',         'icons/equipment/shield/buckler-iron-cross-gray.webp'),
    ('flesh',         'icons/equipment/shield/buckler-iron-cross-gray.webp'),
    # Physical transformation / body
    ('expansion',     'icons/magic/earth/construct-stone.webp'),
    ('reduction',     'icons/magic/earth/construct-stone.webp'),
    ('soften',        'icons/magic/earth/construct-stone.webp'),
    ('graft',         'icons/skills/wounds/anatomy-organ-heart-red.webp'),
    ('nerve',         'icons/skills/wounds/anatomy-organ-heart-red.webp'),
    ('adrenaline',    'icons/skills/wounds/anatomy-organ-heart-red.webp'),
    ('heightened',    'icons/skills/wounds/anatomy-organ-heart-red.webp'),
    ('splice',        'icons/skills/wounds/anatomy-organ-heart-red.webp'),
    ('cannibalize',   'icons/magic/death/hand-withered-gray.webp'),
    ('aging',         'icons/magic/time/clock-analog-gray.webp'),
    # Concentration / trance / focus
    ('trance',        'icons/magic/symbols/circle-ouroboros.webp'),
    ('cognitive',     'icons/magic/symbols/circle-ouroboros.webp'),
    ('will',          'icons/magic/symbols/circle-ouroboros.webp'),
    ('focus',         'icons/magic/symbols/circle-ouroboros.webp'),
    ('alignment',     'icons/magic/symbols/circle-ouroboros.webp'),
    ('prolong',       'icons/magic/time/hourglass-tilted-glowing-gold.webp'),
    ('suspend',       'icons/magic/time/hourglass-tilted-gray.webp'),
    # Senses / sense
    ('sensory',       'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('sense',         'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('danger',        'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('trail',         'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('watcher',       'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    # Weather / environment
    ('weather',       'icons/magic/air/air-wave-gust-blue.webp'),
    ('environment',   'icons/magic/air/air-wave-gust-blue.webp'),
    ('wind',          'icons/magic/air/air-wave-gust-blue.webp'),
    ('water',         'icons/magic/water/barrier-ice-crystal-wall-faceted-blue.webp'),
    ('concentrate',   'icons/magic/symbols/circle-ouroboros.webp'),
    # Momentum / deflect / catfall
    ('momentum',      'icons/magic/movement/trail-streak-impact-blue.webp'),
    ('deflect',       'icons/magic/defensive/shield-barrier-deflect-teal.webp'),
    ('catfall',       'icons/skills/movement/figure-running-gray.webp'),
    ('displacement',  'icons/magic/movement/trail-streak-impact-blue.webp'),
    # Elemental
    ('elemental',     'icons/magic/symbols/elements-air-earth-fire-water.webp'),
    # Split / switch / personality
    ('split',         'icons/magic/symbols/mask-metal-silver-white.webp'),
    ('switch',        'icons/magic/symbols/mask-metal-silver-white.webp'),
    ('personality',   'icons/magic/symbols/mask-metal-silver-white.webp'),
    # Miscellaneous
    ('light',         'icons/magic/light/beam-rays-orange-large.webp'),
    ('magnify',       'icons/magic/perception/orb-crystal-ball-scrying-blue.webp'),
    ('gird',          'icons/magic/defensive/shield-barrier-blue.webp'),
    ('empower',       'icons/magic/control/buff-strength-muscle-damage-orange.webp'),
    ('spider',        'icons/environment/creatures/horse-brown.webp'),
    ('absorption',    'icons/magic/defensive/shield-barrier-deflect-teal.webp'),
    ('absorb',        'icons/magic/defensive/shield-barrier-deflect-teal.webp'),
    ('immunity',      'icons/magic/defensive/shield-barrier-deflect-teal.webp'),
    # Remaining targeted keywords for full coverage
    ('shadow',        'icons/magic/air/fog-gas-smoke-dense-gray.webp'),
    ('awe',           'icons/magic/control/fear-fright-jackolantern-yellow.webp'),
    ('decay',         'icons/magic/death/hand-withered-gray.webp'),
    ('sleep',         'icons/magic/control/buff-flight-wings-purple.webp'),
    ('chemical',      'icons/skills/wounds/anatomy-organ-heart-red.webp'),
    ('esp',           'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('enhancement',   'icons/magic/control/buff-strength-muscle-damage-orange.webp'),
    ('moisture',      'icons/magic/water/barrier-ice-crystal-wall-faceted-blue.webp'),
    ('symmetry',      'icons/magic/symbols/circle-ouroboros.webp'),
    ('penetration',   'icons/skills/wounds/anatomy-organ-brain-pink-red.webp'),
    ('impossible',    'icons/magic/symbols/question-stone-yellow.webp'),
    ('intensify',     'icons/magic/control/buff-strength-muscle-damage-orange.webp'),
    ('invincible',    'icons/magic/defensive/shield-barrier-blue.webp'),
    ('mass',          'icons/magic/air/air-pressure-shield-blue.webp'),
    ('manipulation',  'icons/magic/air/air-pressure-shield-blue.webp'),
    ('mysterious',    'icons/magic/air/fog-gas-smoke-dense-gray.webp'),
    ('traveler',      'icons/magic/movement/portal-vortex-orange.webp'),
    ('reaction',      'icons/magic/movement/trail-streak-impact-blue.webp'),
    ('inflation',     'icons/magic/symbols/question-stone-yellow.webp'),
    ('residue',       'icons/magic/symbols/question-stone-yellow.webp'),
    ('receptacle',    'icons/magic/symbols/ring-circle-smoke-blue.webp'),
    ('repugnance',    'icons/magic/control/fear-fright-jackolantern-yellow.webp'),
    ('distortion',    'icons/magic/movement/portal-vortex-orange.webp'),
    ('spatial',       'icons/magic/movement/portal-vortex-orange.webp'),
    ('stasis',        'icons/magic/time/hourglass-tilted-gray.webp'),
    ('projection',    'icons/magic/movement/portal-vortex-orange.webp'),
    ('worship',       'icons/magic/holy/prayer-hands-glowing-yellow.webp'),
    ('truthear',      'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('telempathic',   'icons/magic/control/energy-stream-link-blue.webp'),
    ('alter',         'icons/magic/nature/elemental-plant-humanoid.webp'),
    ('opposite',      'icons/magic/movement/trail-streak-impact-blue.webp'),
    ('walk',          'icons/skills/movement/figure-running-gray.webp'),
]
_POWER_ICON_DEFAULT = 'icons/magic/perception/eye-ringed-glow-angry-large-red.webp'


def _power_icon(name):
    """Pick a genre-appropriate Foundry icon for a psionic power from keywords
    in its name. Decorative only — no AD&D 2e content embedded."""
    low = name.lower()
    for kw, icon in _POWER_ICON_KEYWORDS:
        if _kw_hit(low, kw):
            return icon
    return _POWER_ICON_DEFAULT


def _kit_description_html(text):
    """Turn a kit's raw DAT prose into readable HTML: split at the section labels
    (Benefits / Hindrances / Description / Role / …) into <p> blocks, bolding each
    label, and HTML-escape the body. Text with no recognizable label is wrapped in
    a single <p>. All text comes from the user's PARTS.DAT at runtime."""
    pat = re.compile(
        r'(Benefits/Hindrances|Special Benefits|Special Hindrances|Benefits|Hindrances'
        r'|Description|Role|Weapon Proficiencies|Nonweapon Proficiencies|Bonus Proficiencies'
        r'|Secondary Skills|Requirements?)\s*:', re.I)
    idx = [m.start() for m in pat.finditer(text)]
    if not idx:
        return '<p>' + html.escape(text) + '</p>'
    out = []
    bounds = idx + [len(text)]
    if bounds[0] > 0:                                   # leading unlabeled lede
        lede = text[:bounds[0]].strip()
        if lede:
            out.append('<p>' + html.escape(lede) + '</p>')
    for j, pos in enumerate(idx):
        chunk = text[pos:bounds[j+1]]
        m = pat.match(chunk)
        if not m:
            continue
        label = m.group(0).rstrip(':').strip()
        rest = html.escape(chunk[m.end():].strip())
        out.append(f'<p><strong>{html.escape(label)}:</strong> {rest}</p>')
    return ''.join(out)


def make_kit_item(kit, img, description_html=None, item_list=None):
    """Build an ARS `background` item (type used for AD&D 2e character kits) from
    a parse_kits() record. Mirrors the common character/capability item shape and
    adds the ARSItemBackground `proficiencies` block (weapon/skill free-text).

    `description_html`, when given, is the kit's full handbook prose (HTM); else
    the DAT Benefits/Hindrances prose is used. `item_list` is the auto-grant list
    (mandatory bonus proficiencies/skills linked to their compendium items)."""
    item_id = make_id()
    return {
        "_id": item_id,
        "name": kit['name'],
        "type": "background",
        "img": img,
        "effects": [],
        "system": {
            "description": (description_html if description_html is not None
                            else _kit_description_html(kit['text'])),
            "dmonlytext": "",
            "itemList": item_list or [],
            "abilityList": [],
            "proficiencies": {"weapon": "", "skill": ""},
            "alias": "",
            "attributes": {"rarity": "", "type": "", "subtype": "", "magic": False,
                           "properties": [], "skillmods": {}, "conditionals": [],
                           "identified": True, "size": "medium"},
            "charges": {"value": 0, "min": 0, "max": 0, "reuse": "none"},
            "location": {"state": "carried", "parent": ""},
            "resource": {"itemId": ""},
            "actions": [], "quantity": 0, "weight": 0,
            "cost": {"value": 0, "currency": "gp"},
            "source": "", "xp": 0,
            "actionGroups": [],
        },
        "folder": None, "sort": 0,
        "ownership": {"default": 0},
        "flags": {}, "_stats": _stats_block(),
    }


# ─── Foundry record factories (Phase 3) ───────────────────────────────────────

def _stats_block():
    """The Foundry `_stats` provenance block stamped on every document (core +
    system version, no author). Identical across docs, hence factored out."""
    return {
        "compendiumSource": None, "duplicateSource": None, "exportSource": None,
        "coreVersion": CORE_VERSION, "systemId": SYSTEM_ID,
        "systemVersion": SYSTEM_VERSION, "lastModifiedBy": None,
    }


def make_compendium_folder(folder_id, name, folder_type, sort=0, parent=None, color=None):
    """Folder for any compendium type (Item, Actor, JournalEntry). `parent`
    is another folder's id for nested folders, or None for root."""
    return {
        "_id": folder_id, "name": name, "type": folder_type,
        "folder": parent, "sorting": "m", "sort": sort,
        "color": color, "description": "",
        "flags": {}, "_stats": _stats_block(),
    }


def make_race_item(race, img_path=None, description='', item_list_refs=None,
                   size=None, movement=None, skill_mods=None):
    """Build a Foundry Race item with NO directly-attached effects. All
    mechanical effects (stat modifiers, movement override, etc.) live on
    child Ability items referenced through `system.itemList`, so a PC who
    takes this race inherits the effects via the abilities they receive."""
    item_id = make_id()
    attributes = {
        # Tables sourced from RACE.DAT (parsed binary fields, no hardcoded values)
        "abilities":   race.get('ability_ranges', {}),
        "adjustments": race.get('ability_adjustments', {}),
        "classCaps":   race.get('class_caps', {}),
        "demographics": {
            "maleHeight":   {"base": 0, "dice": 0, "dieSize": 0},
            "femaleHeight": {"base": 0, "dice": 0, "dieSize": 0},
            "maleWeight":   race.get('max_male_weight', 0),
            "femaleWeight": race.get('female_weight_base', 0),
            "startingAge":  {"base": race.get('starting_age_base', 0),
                             "dice": race.get('starting_age_dice', 0)},
            "maxAge":       race.get('max_age', 0),
        },
        "thiefAdjustments": {},
        "rarity": "", "subtype": "", "magic": False,
        "properties": [], "skillmods": skill_mods or [], "conditionals": [],
        "identified": True,
    }
    if movement is not None:
        attributes["movement"] = movement
    if size:
        attributes["size"] = size
    # system.type = comma-separated tags following the OSRIC convention:
    # "<Lineage>, Humanoid". Lineage = the resolved base race (so sub-races
    # share their parent's tag), or the race name itself when no base resolves
    # (e.g. CBH humanoids like Goblin).
    base_lineage = _resolve_base_race(race['name']) or race['name']
    system_type  = f"{base_lineage}, Humanoid"
    return {
        "_id": item_id,
        "name": race['name'],
        "type": "race",
        "img": img_path or "icons/svg/mystery-man.svg",
        "system": {
            "description": description,
            "dmonlytext": "",
            "itemList": item_list_refs or [],
            "alias": "",
            "type": system_type,
            "attributes": attributes,
            "charges": {"value": 0, "min": 0, "max": 0, "reuse": "none"},
            "location": {"state": "carried", "parent": ""},
            "resource": {"itemId": ""},
            "actions": [], "quantity": 0, "weight": 0,
            "cost": {"value": 0, "currency": "gp"},
            "source": "", "xp": 0, "abilityList": [],
        },
        "effects": [],                # all effects live on child Ability items
        "flags": {"adnd2": {"raceId": race.get('race_id')}},
        "folder": None, "sort": 0,
        "ownership": {"default": 0},
        "_stats": _stats_block(),
    }


def make_race_stat_mod_changes(ability_adjustments):
    """Build the ActiveEffect change list for racial stat modifiers, or [] if
    every adjustment is 0/None. Used by the Racial Ability Modifiers Ability item."""
    changes = []
    for ab, val in (ability_adjustments or {}).items():
        if val and val != 0:
            changes.append({
                "key":   f"system.abilities.{ab}.value",
                "type":  "add",              # ARS v14 string type — NOT numeric mode
                "value": str(val),
                "priority": 20,
                "phase":    "initial",
            })
    return changes


def _sp_extract_subrace_html(race_name):
    """Extract just the sub-race-specific section from a SP demihuman page
    (SP00024..SP00031). Returns cleaned semantic HTML, or '' if the race
    is not a sub-race or no matching section heading is found.

    The SP demihuman pages are structured as a sequence of top-level FONT
    headings:
       <font color=#800080 size=4> Lineage name </font>            (chapter)
       <font color=#ff0000 size=4> Sub-race plural </font>        (each sub-race)
       <font color=#800080 size=4> Lineage Abilities </font>      (point-buy menu)
    plus paragraphs/tables in between. We slice from the matching red
    SIZE=4 heading up to the next SIZE=4 heading (red or purple) and run
    the standard cleanup pipeline on the slice."""
    base = _resolve_base_race(race_name)
    if not base or base not in _SP_DEMIHUMAN_FILES:
        return ''
    if race_name.strip().lower() == base.lower():
        return ''      # base race, not a sub-race
    rel = _SP_DEMIHUMAN_FILES[base]
    path = os.path.join(SOURCE_BASE, rel)
    if not os.path.exists(path):
        return ''

    # Build a normalized prefix from the sub-race name to match against the
    # heading text. e.g. "Hill dwarf" → "hill"; "Aquatic (Sea) elf" →
    # "aquatic"; "Sylvan (wood) elf" → "sylvan"; "Gray dwarf (duergar)" → "gray".
    cleaned = re.sub(r'\([^)]*\)', '', race_name).strip()
    tokens  = cleaned.split()
    if tokens and tokens[-1].lower() in ('dwarf','elf','gnome','halfling'):
        tokens = tokens[:-1]
    if not tokens:
        return ''
    search_prefix = ' '.join(tokens).lower()

    with open(path, 'r', encoding='cp1252') as f:
        raw = f.read()
    soup = BeautifulSoup(raw, 'html.parser')
    body = soup.find('body') or soup

    # Collect every SIZE=4 heading FONT (red sub-race + purple chapter breaks)
    # in document order, as direct children of <body>.
    boundaries = []
    for child in body.children:
        if not isinstance(child, Tag) or child.name != 'font':
            continue
        if child.get('size') != '4':
            continue
        col = str(child.get('color') or '').lower()
        if col not in ('#ff0000', '#800080'):
            continue
        boundaries.append(child)

    # Find the sub-race heading whose text starts with our prefix
    target_idx = -1
    for i, ft in enumerate(boundaries):
        col = str(ft.get('color') or '').lower()
        if col != '#ff0000': continue
        text = ft.get_text(' ', strip=True).lower()
        if text.startswith(search_prefix + ' ') or text == search_prefix:
            target_idx = i
            break
    if target_idx < 0:
        return ''

    start_font = boundaries[target_idx]
    end_font   = boundaries[target_idx + 1] if target_idx + 1 < len(boundaries) else None

    # Walk body children: keep everything from start_font (inclusive) up to
    # end_font (exclusive). Boundaries are themselves body-level FONT tags.
    selected = []
    in_range = False
    for child in body.children:
        if child is start_font: in_range = True
        if in_range:
            if end_font is not None and child is end_font: break
            selected.append(child)
    if not selected:
        return ''

    # Build a fresh soup with copies of the selected nodes, then run the
    # same cleanup pipeline as a full file.
    new_soup = BeautifulSoup('<html><body></body></html>', 'html.parser')
    new_body = new_soup.body
    assert new_body is not None
    for c in selected:
        # BeautifulSoup Tag/NavigableString support copy via __copy__
        new_body.append(c.__copy__() if hasattr(c, '__copy__') else NavigableString(str(c)))

    book_dir = os.path.dirname(path)
    book_key = os.path.basename(book_dir)
    src_dir_files = {f.upper(): f for f in os.listdir(book_dir)}
    return _clean_html_body(new_body, new_soup, book_key, src_dir_files)


def _race_html_description(race_name):
    """Return cleaned HTML description for a race. Order of preference:
    1) Sub-race-specific section in Skills & Powers (SP00024..SP00031),
       when the race is a sub-race of a demihuman lineage.
    2) PHB chapter page for base demihumans.
    3) Skills & Powers PC page for exotic races.
    Never falls back to the Monstrous Manual (NPC-oriented)."""
    sub_html = _sp_extract_subrace_html(race_name)
    if sub_html:
        return sub_html

    base = _resolve_base_race(race_name)
    if base and base in _RACE_HTML_FILES:
        rel = _RACE_HTML_FILES[base]
    else:
        rel = _SP_EXOTIC_FILES.get(race_name)
    if not rel:
        return ''
    path = os.path.join(SOURCE_BASE, rel)
    if not os.path.exists(path):
        return ''
    book_dir = os.path.dirname(path)
    book_key = os.path.basename(book_dir)
    try:
        src_dir_files = {f.upper(): f for f in os.listdir(book_dir)}
        return clean_html_file(path, book_key, src_dir_files)
    except Exception:
        return ''


# Map our internal group → matrixTable string used by ARS for THAC0/save tables.
# OSRIC 2026.05.20 ships 'fighter', 'wizard', 'cleric', 'thief', 'monster' tables.
_CLASS_MATRIX_TABLE = {
    'warrior':    'fighter',
    'rogue':      'thief',
    'priest':     'cleric',
    'wizard':     'wizard',
    'psionicist': 'fighter',   # closest fit; PSIONIC.DAT doesn't ship its own matrix
}


_CLASS_MAX_RANKS = 99   # CLASS.DAT stores 99 rows for xp/thaco/save/spell tables;
                         # emit all of them so S&P high-level advancement works.

# HLC (High-Level Campaigns) THAC0 limits: parsed from HLC00210.HTM at first use.
# Lower THAC0 = better; limits are minimum values (floor for the descent).
# GROUP → minimum THAC0 allowed by HLC rules.
_HLC_THACO_LIMITS: dict | None = None

def _get_hlc_thaco_limits() -> dict:
    global _HLC_THACO_LIMITS
    if _HLC_THACO_LIMITS is not None:
        return _HLC_THACO_LIMITS
    path = _hlc_path('HLC00210.HTM')
    limits = {}
    if path and os.path.exists(path):
        from bs4 import BeautifulSoup
        with open(path, encoding='latin-1') as f:
            soup = BeautifulSoup(f, 'html.parser')
        for table in soup.find_all('table'):
            for tr in table.find_all('tr'):
                cells = [td.get_text(strip=True) for td in tr.find_all('td')]
                if len(cells) >= 2:
                    group = cells[0].strip().lower()
                    try:
                        limit = int(cells[1].strip())
                        limits[group] = limit
                    except ValueError:
                        pass
    _HLC_THACO_LIMITS = limits
    return limits

def _hlc_path(filename):
    """Return the path to a HLC HTML file, or None if not found.
    SOURCE_BASE already points at .../MACBOOKS/HTML; HLC is a subdirectory."""
    p = os.path.join(SOURCE_BASE, 'HLC', filename)
    return p if os.path.exists(p) else None

# HLC Druid XP for levels 21-30: sourced from HLC00226.HTM at first use.
# Druid CLASS.DAT xp_table uses the Hierophant competition schedule, not HLC's
# simplified linear table. We override levels 21+ with HLC values.
_HLC_DRUID_XP: list | None = None  # index 0 = L21 XP threshold

def _get_hlc_druid_xp() -> list:
    global _HLC_DRUID_XP
    if _HLC_DRUID_XP is not None:
        return _HLC_DRUID_XP
    path = _hlc_path('HLC00226.HTM')
    xp_list = []
    if path and os.path.exists(path):
        from bs4 import BeautifulSoup
        with open(path, encoding='latin-1') as f:
            soup = BeautifulSoup(f, 'html.parser')
        for table in soup.find_all('table'):
            for tr in table.find_all('tr'):
                cells = [td.get_text(strip=True) for td in tr.find_all('td')]
                if len(cells) >= 3:
                    try:
                        lvl = int(cells[0])
                        # Column 2 = Druid XP (column 1 = Cleric XP)
                        xp_str = cells[2].replace(',', '').replace(' ', '')
                        xp = int(xp_str)
                        if lvl >= 21:
                            xp_list.append(xp)
                    except (ValueError, IndexError):
                        pass
    _HLC_DRUID_XP = xp_list
    return xp_list


def _class_spell_kind(cls):
    """Return 'arcane', 'divine', or None for a class's spell-slot table.
    The CLASS.DAT spell table is just numbers; which spell system those slots
    belong to is a structural Foundry-side categorization (which rank array to
    fill), keyed off the class group and the three hybrid casters. Wizard-group
    classes (Mage + specialists) cast arcane; priest-group classes (Cleric,
    Druid) cast divine; Bard casts arcane and Paladin/Ranger cast divine."""
    group = cls.get('group')
    name = (cls.get('name') or '').lower()
    if group == 'wizard' or name == 'bard':
        return 'arcane'
    if group == 'priest' or name in ('paladin', 'ranger'):
        return 'divine'
    return None


# Paladin and Ranger cast priest spells at a reduced **casting level** (an
# effective priest level lower than their character level), unlike full casters
# whose casting level == character level. That column is NOT present in
# CLASS.DAT (verified absent under every encoding/stride), but the PHB
# progression tables print it explicitly as a "Casting Level" column: Table 17
# (Paladin, PHB00060) and Table 18 (Ranger, PHB00062). DAT-absent → read it
# from the HTM table at runtime (copyright-clean: parsed from the user's CD-ROM,
# no values hardcoded — only the file names and the English column label).
_PARTIAL_CASTER_LEVEL_FILES = {'paladin': 'PHB00060.HTM', 'ranger': 'PHB00062.HTM'}
_partial_caster_levels_cache = {}


def _get_partial_caster_levels(class_name):
    """Return {character_level: casting_level} for a class whose spell-casting
    level differs from its character level (Paladin, Ranger), parsed from the
    'Casting Level' column of its PHB progression table. Empty dict otherwise."""
    key = (class_name or '').lower()
    if key in _partial_caster_levels_cache:
        return _partial_caster_levels_cache[key]
    out = {}
    fname = _PARTIAL_CASTER_LEVEL_FILES.get(key)
    if fname:
        path = os.path.join(SOURCE_BASE, 'PHB', fname)
        if os.path.exists(path):
            with open(path, encoding='latin-1') as fh:
                soup = BeautifulSoup(fh.read(), 'html.parser')
            rows = [[c.get_text(strip=True) for c in tr.find_all(['td', 'th'])]
                    for tr in soup.find_all('tr')]
            # The casting-level column header cell reads "Casting"; its index
            # varies (Paladin col 1, Ranger col 3). Char level is always col 0.
            cidx = next((j for r in rows for j, cell in enumerate(r)
                         if cell.lower() == 'casting'), None)
            if cidx is not None:
                for r in rows:
                    if not r or not r[0].rstrip('*').isdigit() or cidx >= len(r):
                        continue
                    cl = r[cidx].rstrip('*').strip()
                    if cl.isdigit():
                        out[int(r[0].rstrip('*'))] = int(cl)
    _partial_caster_levels_cache[key] = out
    return out


def _build_class_ranks(cls):
    """Emit the ARSClassRank list from CLASS.DAT-extracted xp/thaco/save tables.
    99 levels (full CLASS.DAT range). THAC0 capped at HLC Table 39 limits for
    L21+ (parsed from HLC00210.HTM). Druid XP at L21-30 sourced from HLC Table
    45 (HLC00226.HTM) instead of CLASS.DAT's Hierophant competition schedule.
    Saves: CLASS.DAT 5 columns → 10 ARS keys via _SAVE_COL_MAP. BAB: 0."""
    xp_table       = cls.get('xp_table',    []) or []
    thaco_table    = cls.get('thaco_table', []) or []
    save_table     = list(cls.get('save_table', []) or [])
    spell_table    = cls.get('spell_table', []) or []
    spell_kind     = _class_spell_kind(cls) if spell_table else None
    # Paladin/Ranger cast at a reduced "casting level" (see _get_partial_caster_
    # levels); empty for full casters, whose casting level == character level.
    partial_cl     = _get_partial_caster_levels(cls.get('name', '')) if spell_kind else {}
    partial_cl_max = max(partial_cl.values()) if partial_cl else 0

    def _eff_caster_level(level):
        # Full casters: casting level == character level. Partial casters: read
        # from the parsed table; beyond its last printed row carry the cap.
        if not partial_cl:
            return level
        return partial_cl.get(level, partial_cl_max)
    skill_pts_start = cls.get('skill_pts_start', 0) or 0
    skill_pts_level = cls.get('skill_pts_level', 0) or 0
    # Per-level advancement titles. Only the druid has special titles in the 2e
    # PHB (Initiate/Druid/Archdruid/Great Druid/Grand Druid/Hierophant); other
    # classes carry no level titles. Sourced from PHB prose at runtime.
    title_map = _druid_titles() if cls.get('name', '').lower() == 'druid' else {}
    hit_die       = cls.get('hit_die')
    hit_dice_cap  = cls.get('hit_dice_cap')  or 0
    hp_after_cap  = cls.get('hp_after_cap')  or 0
    # CLASS.DAT save-table layout (confirmed for all 26 classes):
    #   Warriors (group='warrior'): row 0 = Normal Man, row 1 = L1 saves.
    #   All other groups: row 0 = row 1 = Normal Man (signature appears twice),
    #   row 2 = L1 saves.
    # Access L-N saves via save_table[save_base + N - 1] where save_base is
    # 1 for warriors and 2 for everyone else. This is group-driven rather than
    # a value-comparison heuristic, so it works even if a warrior's L1 saves
    # happened to match the Normal Man row.
    save_base = 1 if cls.get('group') == 'warrior' else 2
    # HLC Table 39 THAC0 limits by class group (parsed from HLC00210.HTM).
    # group label in HLC HTML matches the PHB group names: "priest"/"rogue"/
    # "warrior"/"wizard". THAC0 is descending; limit = best (lowest) allowed.
    hlc_limits = _get_hlc_thaco_limits()
    group_name = cls.get('group', '')
    thaco_floor = hlc_limits.get(group_name)   # None if not in HLC (e.g. psionicist)
    # Druid XP note: CLASS.DAT uses the PHB Hierophant competition schedule
    # (cumulative, monotonically increasing: 3M@L15 … 5.5M@L20 … 6M@L21 …).
    # HLC Table 45 redefines Druid XP from a lower base (L20=2M), which would
    # create a non-monotonic decrease at the L20→L21 boundary. CLASS.DAT is kept
    # for all Druid XP levels; only the THAC0 cap uses HLC.
    real_n = min(_CLASS_MAX_RANKS, max(len(xp_table), len(thaco_table)))
    if real_n == 0:
        return []
    ranks = []
    for i in range(real_n):
        level = i + 1
        # 2e rule: HD are rolled through `hit_dice_cap`; beyond that the class
        # gains a flat number of hit points per level instead of another HD
        # (both values read from CLASS.DAT). The engine does NOT switch this
        # automatically — it just rolls whatever `hdformula` string we give it
        # — so post-cap ranks must carry the flat number as a literal string.
        # Mirrors OSRIC's Fighter: "d10" through level 9, then "3" from 10 on.
        if hit_dice_cap and level > hit_dice_cap and hp_after_cap:
            hdformula = str(hp_after_cap)
        else:
            hdformula = f"d{hit_die}" if hit_die else "1d6"
        # Saves: columns are [par/poi/death, rod/staff/wand, pet/poly, breath, spell].
        # Clamp to the last row so L99 non-warriors don't fall past the table end
        # (save_base=2 would need index 100 at L99, but the table has only 100 rows).
        save_idx = min(save_base + level - 1, len(save_table) - 1) if save_table else 0
        if save_table and save_idx < len(save_table):
            row = save_table[save_idx]
            par, rod, pet, bre, spl = row[0], row[1], row[2], row[3], row[4]
        else:
            par = rod = pet = bre = spl = 20
        # Spell slots: spell_table[i] is the row for this level (row i = char
        # level i+1). Cols 0..8 = slots for spell levels 1..9. ARS stores them
        # 1-indexed (index 0 = padding): arcane is length 10 (spell levels 1-9),
        # divine length 8 (spell levels 1-7). casterlevel for the cast type is
        # the character level once any slot exists, else 0 (matches OSRIC).
        arcane = [0]*10
        divine = [0]*8
        caster_arcane = caster_divine = 0
        if spell_kind and i < len(spell_table):
            srow = spell_table[i]
            has_slots = any(v > 0 for v in srow)
            if spell_kind == 'arcane':
                for s in range(1, 10):
                    arcane[s] = srow[s-1]
                if has_slots:
                    caster_arcane = _eff_caster_level(level)
            else:
                for s in range(1, 8):
                    divine[s] = srow[s-1]
                if has_slots:
                    caster_divine = _eff_caster_level(level)
        # THAC0: thaco_table has 99 entries (indices 0-98); index 0 = Normal Man,
        # index N = level-N THAC0. Level 99 would need index 99 which is out of
        # range, so clamp to the last available entry (repeating the plateau).
        # Then cap at HLC Table 39 limit for L21+ (lower = better; limit = floor).
        raw_thaco = thaco_table[min(level, len(thaco_table) - 1)] if thaco_table else 20
        if thaco_floor is not None and level > 20:
            thaco = max(raw_thaco, thaco_floor)
        else:
            thaco = raw_thaco
        # xp_table[i] = XP threshold to reach level i+2; stored in the rank for
        # level i+1 as "XP needed to advance to the next level" (FVTT convention).
        xp = xp_table[i] if i < len(xp_table) else 0
        ranks.append({
            "level":         level,
            "thaco":         thaco,
            "bab":           0,
            "numatks":       "1/1",
            "turnLevel":     0,
            "xp":            xp,
            "hdformula":     hdformula,
            "baseMove":      None,
            "baseAC":        None,
            "classpoints":   (skill_pts_start if level == 1 else skill_pts_level) if (skill_pts_start or skill_pts_level) else None,
            "title":         title_map.get(level, ""),
            "paralyzation": par, "poison": par, "death": par,
            "rod": rod, "staff": rod, "wand": rod,
            "petrification": pet, "polymorph": pet,
            "breath": bre,
            "spell": spl,
            "arcane": arcane,
            "divine": divine,
            "psionic": {"disciplines": 0, "sciences": 0, "devotions": 0,
                        "defenseModes": 0, "psp": 0},
            "casterlevel": {"arcane": caster_arcane, "divine": caster_divine, "psionic": 1},
        })
    return ranks


def _class_icon(name):
    """Pick a Foundry icon for a class from generic keywords in its name.
    All paths verified against FVTT/public/icons/ 2026-05-28. Decorative only."""
    low = name.lower()
    if 'fighter'    in low: return 'icons/skills/melee/weapons-crossed-swords-black.webp'
    if 'paladin'    in low: return 'icons/magic/holy/prayer-hands-glowing-yellow.webp'
    if 'ranger'     in low: return 'icons/environment/wilderness/tree-oak.webp'
    if 'thief'      in low: return 'icons/skills/social/theft-pickpocket-bribery-brown.webp'
    if 'bard'       in low: return 'icons/tools/instruments/lute-gold-brown.webp'
    if 'cleric'     in low: return 'icons/magic/holy/prayer-hands-glowing-yellow.webp'
    if 'druid'      in low: return 'icons/environment/wilderness/tree-oak.webp'
    if 'psionicist' in low: return 'icons/magic/perception/eye-ringed-glow-angry-large-red.webp'
    if 'necromancer' in low: return 'icons/magic/death/hand-undead-skeleton-fire-green.webp'
    if 'invoker'    in low: return 'icons/magic/fire/explosion-fireball-large-orange.webp'
    if 'illusionist' in low: return 'icons/magic/air/fog-gas-smoke-dense-gray.webp'
    if 'enchanter'  in low: return 'icons/magic/control/control-influence-puppet.webp'
    if 'diviner'    in low: return 'icons/magic/perception/orb-crystal-ball-scrying-blue.webp'
    if 'conjurer'   in low: return 'icons/magic/symbols/ring-circle-smoke-blue.webp'
    if 'abjurer'    in low: return 'icons/magic/defensive/shield-barrier-blue.webp'
    if 'transmuter' in low: return 'icons/magic/earth/construct-stone.webp'
    if 'geometer'   in low: return 'icons/magic/symbols/runes-carved-stone-purple.webp'
    if 'alchemist'  in low: return 'icons/consumables/potions/potion-flask-corked-blue.webp'
    if 'wild'       in low: return 'icons/magic/fire/explosion-embers-orange.webp'
    if 'shadow'     in low: return 'icons/magic/air/fog-gas-smoke-dense-gray.webp'
    if 'song'       in low: return 'icons/magic/sonic/projectile-sound-rings-wave.webp'
    if 'air'        in low: return 'icons/magic/air/air-burst-spiral-large-pink.webp'
    if 'fire'       in low: return 'icons/svg/fire.svg'
    if 'earth'      in low: return 'icons/magic/earth/barrier-stone-brown-green.webp'
    if 'water'      in low: return 'icons/magic/water/barrier-ice-crystal-wall-faceted-blue.webp'
    if 'mage'       in low: return 'icons/magic/symbols/circle-ouroboros.webp'
    if 'wizard'     in low: return 'icons/magic/symbols/circle-ouroboros.webp'
    return 'icons/svg/book.svg'


def make_class_item(cls, description=''):
    """Build an ARS `class` Item from a parsed CLASS.DAT record. Emits the typed
    `system.ranks[]` advancement table (per-level THAC0/saves/XP/HD/spell-slots,
    built by _build_class_ranks) plus class-level features (matrixTable,
    lasthitdice, proficiencies)."""
    item_id = make_id()
    group   = cls.get('group', 'unknown')
    is_psionic = (group == 'psionicist')
    # features.lasthitdice = the level after which the class stops rolling HD
    # and switches to flat HP per level (Fighter 9, Wizard 10, etc.).
    lasthitdice = cls.get('hit_dice_cap') or 0
    return {
        "_id": item_id,
        "name": cls['name'],
        "type": "class",
        "img": _class_icon(cls['name']),
        "system": {
            "description": description,
            "active":      True,
            "xp":          0,
            "xpbonus":     0,
            "ranks":       _build_class_ranks(cls),
            "features": {
                "acDexFormula":          "@abilities.dex.defensive",
                "hpConFormula":          "",
                "lasthitdice":           lasthitdice,
                "bonuscon":              (group == 'warrior'),
                "focus":                 {"major": "", "minor": ""},
                "wisSpellBonusDisabled": False,
            },
            "proficiencies": {
                "penalty":  cls.get('wp_penalty',    0),
                "weapon":   {"starting":  cls.get('wp_starting',    0),
                             "earnLevel": cls.get('wp_gain_level',  0)},
                "skill":    {"starting":  cls.get('nwp_starting',   0),
                             "earnLevel": cls.get('nwp_gain_level', 0)},
            },
            "matrixTable": _CLASS_MATRIX_TABLE.get(group, ''),
            "isPsionic":   is_psionic,
        },
        "effects": [],
        # Preserve internal hints (group / sub-class id / HD specifics)
        # under our module flag so we can re-derive without re-parsing.
        "flags": {"adnd2": {
            "group":       group,
            "subClassId":  cls.get('sub_class_id'),
            "hitDie":      cls.get('hit_die'),
            "hitDiceCap":  cls.get('hit_dice_cap'),
            "hpAfterCap":  cls.get('hp_after_cap'),
        }},
        "folder": None, "sort": 0, "ownership": {"default": -1}, "_stats": _stats_block(),
    }


_SIZE_NORMALIZE = {'T':'tiny','S':'small','M':'medium','L':'large','H':'huge','G':'gargantuan'}


_DMG_CODE_MAP = {'B': 'bludgeoning', 'P': 'piercing', 'S': 'slashing'}

def _ars_damage_type(dt):
    """Map a PARTS.DAT damage-type code to a valid ARS damage type. Multi-type
    weapons (P/S) collapse to their first component; ARS stores a single type
    in `system.damage.type` (the others are offered as choice actions — see
    `_weapon_damage_type_list`)."""
    first = (dt or '').split('/')[0].strip()
    return _DMG_CODE_MAP.get(first, _DMG_CODE_MAP.get(dt, 'none'))


def _weapon_damage_type_list(dt):
    """Split a PARTS.DAT damage-type code into its ordered list of ARS damage
    types: 'P/S' → ['piercing','slashing'], 'B/S' → ['bludgeoning','slashing'],
    'P' → ['piercing']. Used to offer a damage-type-choice action group for the
    2e weapons that strike for either type at the wielder's option (issue #15)."""
    out = []
    for code in (dt or '').split('/'):
        t = _DMG_CODE_MAP.get(code.strip())
        if t and t not in out:
            out.append(t)
    return out


# ── Item material / category classifiers ──────────────────────────────────────
# `attributes.material` (ARS itemSaveChoices, drives DMG item saving throws) and
# `attributes.type` (equipment category) are NOT in PARTS.DAT, so they're derived
# at runtime from the item's own name + parsed ftype. Copyright note: these tables
# hold only GENERIC physical-material and equipment words (the same keyword-parser
# pattern as pick_spell_icon) — no AD&D-specific names, gem lists, or numbers. A
# substring match on the lowercased name wins (first match), most specific first;
# unmatched items fall back to a neutral default.
_ITEM_MATERIAL_KEYWORDS = [
    ('leather', 'leather_book'), ('rawhide', 'leather_book'), ('hide', 'leather_book'),
    ('padded', 'cloth'), ('silk', 'cloth'), ('wool', 'cloth'), ('linen', 'cloth'),
    ('canvas', 'cloth'), ('cloth', 'cloth'), ('felt', 'cloth'), ('robe', 'cloth'),
    ('wooden', 'wood_thick'), ('oaken', 'wood_thick'), ('wood', 'wood_thick'),
    ('crystal', 'crystal_vial'), ('vial', 'crystal_vial'),
    ('glass', 'glass'), ('bottle', 'glass'),
    ('mirror', 'mirror'),
    ('ivory', 'bone_ivory'), ('bone', 'bone_ivory'), ('antler', 'bone_ivory'),
    ('horn', 'bone_ivory'), ('tusk', 'bone_ivory'),
    ('parchment', 'paper'), ('papyrus', 'paper'), ('paper', 'paper'), ('scroll', 'paper'),
    ('rope', 'rope'), ('cord', 'rope'), ('net', 'rope'),
    ('silver', 'metal_soft'), ('gold', 'metal_soft'), ('platinum', 'metal_soft'),
    ('electrum', 'metal_soft'),
    ('iron', 'metal'), ('steel', 'metal'), ('bronze', 'metal'), ('brass', 'metal'),
    ('copper', 'metal'), ('mithral', 'metal'), ('adamant', 'metal'), ('chain', 'metal'),
    ('metal', 'metal'),
    ('marble', 'rock_gem'), ('granite', 'rock_gem'), ('obsidian', 'rock_gem'),
    ('stone', 'rock_gem'), ('rock', 'rock_gem'), ('gem', 'rock_gem'), ('jade', 'rock_gem'),
    ('jewel', 'rock_gem'),
    ('elixir', 'potions'), ('philter', 'potions'), ('philtre', 'potions'),
    ('oil', 'oils'),
]

_ITEM_TYPE_KEYWORDS = [
    ('quarrel', 'ammunition'), ('arrow', 'ammunition'), ('bolt', 'ammunition'),
    ('bullet', 'ammunition'), ('sheaf', 'ammunition'),
    ('bracer', 'bracer'), ('bracelet', 'bracer'), ('vambrace', 'bracer'),
    ('amulet', 'jewelry'), ('necklace', 'jewelry'), ('pendant', 'jewelry'),
    ('periapt', 'jewelry'), ('brooch', 'jewelry'), ('medallion', 'jewelry'),
    ('talisman', 'jewelry'), ('scarab', 'jewelry'), ('phylactery', 'jewelry'),
    ('cloak', 'cloak'), ('cape', 'cloak'), ('mantle', 'cloak'),
    ('scroll', 'scroll'),
    ('ring', 'ring'),
    ('robe', 'clothing'), ('boot', 'clothing'), ('glove', 'clothing'),
    ('gauntlet', 'clothing'), ('girdle', 'clothing'), ('belt', 'clothing'),
    ('helm', 'clothing'), ('slipper', 'clothing'), ('sandal', 'clothing'),
    ('apron', 'clothing'), ('vest', 'clothing'),
    ('saddle', 'tackAndHarness'), ('bridle', 'tackAndHarness'), ('harness', 'tackAndHarness'),
    ('barding', 'tackAndHarness'), ('halter', 'tackAndHarness'),
    ('wagon', 'transport'), ('cart', 'transport'), ('boat', 'transport'),
    ('ship', 'transport'), ('raft', 'transport'), ('canoe', 'transport'),
    ('galley', 'transport'), ('barge', 'transport'),
    ('pony', 'animal'), ('mule', 'animal'), ('mount', 'animal'), ('horse', 'animal'),
    ('hound', 'animal'), ('mastiff', 'animal'), ('falcon', 'animal'),
    ('incense', 'alchemical'), ('powder', 'alchemical'), ('salve', 'alchemical'),
    ('ointment', 'alchemical'), ('perfume', 'alchemical'), ('dust', 'alchemical'),
    ('oil', 'alchemical'),
    ('gemstone', 'gem'), ('gem', 'gem'), ('jewel', 'gem'),
    ('figurine', 'gear'), ('wand', 'gear'), ('staff', 'gear'), ('pipe', 'gear'),
    ('lens', 'gear'),
]


def _kw_hit(low, kw):
    """Whole-word (optional trailing 's') match of a keyword in a lowercased
    name. Word-anchored so 'ring' hits "Ring of X" but not "Snaring", 'ship'
    not "Rulership", 'horse' not "horseman's"."""
    return re.search(r'\b' + re.escape(kw) + r's?\b', low) is not None


def _item_material(name, ftype):
    """Best-effort ARS itemSaveChoices material for an item, from its name then a
    structural ftype default. Returns '' when undeterminable (caller omits the
    field so the schema default applies). Heuristic, name-sourced — not embedded
    per-item data."""
    low = name.lower()
    for kw, mat in _ITEM_MATERIAL_KEYWORDS:
        if _kw_hit(low, kw):
            return mat
    if ftype == 'potion':
        return 'potions'
    if ftype in ('weapon', 'armor'):
        return 'metal'
    return ''


def _item_subcategory(name):
    """Best-effort ARS attributes.type equipment category from the item name, or
    '' (the schema default). Generic words only; no AD&D-specific content."""
    low = name.lower()
    for kw, cat in _ITEM_TYPE_KEYWORDS:
        if _kw_hit(low, kw):
            return cat
    return ''


# ── AC-source classification → ARS armorTypes (issue #4) ─────────────────────
# ARS routes an item's AC contribution through `system.protection.type`:
#   • armor / warding → `protection.ac` is the *base* AC (warding doesn't count
#     as worn armor — bracers, archmage robes);
#   • shield          → `protection.ac + modifier` is *added* to AC;
#   • ring  / cloak   → `protection.modifier` is the bonus (ac stays 0), and
#     only the best one applies (cloak only when no armor is worn).
# Body armor stays "armor". The rest are detected from the runtime item name
# (generic English equipment words only — no game numbers hardcoded; the +N
# bonuses are parsed from the name the user's DAT supplies). Mirrors OSRIC's
# items-gm modeling (Ring/Cloak of Protection, Bracers of Defense, …).
_SHIELD_NAME_RE  = re.compile(r'\b(shield|buckler)\b', re.I)
_WARDING_NAME_RE = re.compile(r'\b(bracers?|vambrace|robe)\b', re.I)


def _armor_protection_type(name):
    """protection.type for an is_armor PARTS item: shield / warding / armor."""
    if _SHIELD_NAME_RE.search(name):
        return 'shield'
    if _WARDING_NAME_RE.search(name):
        return 'warding'
    return 'armor'


def _ac_jewelry_protection(name):
    """For a Ring/Cloak of Protection that PARTS.DAT does NOT flag as armor:
    return (protection_type, ac_bonus, save_bonus, aura_distance_or_0) or None.
    The +N AC bonus, the +N save bonus, and the aura radius are all read from
    the runtime item name (e.g. 'Ring of Protection +2, 5-foot radius')."""
    low = name.lower()
    if 'protection' not in low:
        return None
    if re.search(r'\bring\b', low):
        ptype = 'ring'
    elif re.search(r'\b(cloak|cape|mantle)\b', low):
        ptype = 'cloak'
    else:
        return None
    m = re.search(r'protection\D*\+(\d+)', low)   # first +N after "protection" = AC
    if not m:
        return None
    ac_bonus = int(m.group(1))
    st = re.search(r'\+(\d+)\s*(?:st\b|saves?\b)', low)   # explicit "+N ST/Saves"
    save_bonus = int(st.group(1)) if st else ac_bonus     # else == AC bonus (2e)
    rad = re.search(r'(\d+)[\s-]*foot\s+radius', low)
    aura_distance = int(rad.group(1)) if rad else 0
    return (ptype, ac_bonus, save_bonus, aura_distance)


# Treasure gems and art objects carry their value in the name — "Diamond
# (5500 gp)", "Rhodochrosite (1 sp)" — while PARTS.DAT leaves cost_gp at 0.
# Parse it out so these items aren't priceless. Copyright-clean: it reads a
# number the user's own data already prints in the record name.
_NAME_COST_RE = re.compile(r'\((\d[\d,]*)\s*(gp|sp|cp|ep|pp)\)', re.I)

def _cost_from_name(name):
    """(value:int, currency:str) parsed from a trailing '(N gp)' in the item
    name, or None. Used only when PARTS.DAT gives no cost."""
    m = _NAME_COST_RE.search(name or '')
    if not m:
        return None
    return int(m.group(1).replace(',', '')), m.group(2).lower()


# PARTS.DAT mixes Skills & Powers character-build data into the part list:
# kit/trait records flagged "(CRE)", per-ability "purchase" rows (Class/Race +
# ability + parenthesized character-point cost), racial dialects, secret
# languages and follower grants. None are equipment — they carry no description,
# weight, or real price — so they're excluded from the items pack. Copyright-
# clean: only generic class/race words and structural markers are referenced.
_NONITEM_CLASS_RACE = (
    r'(?:Cleric|Druid|Priest|Fighter|Paladin|Ranger|Warrior|Thief|Rogue|Bard|'
    r'Wizard|Mage|Abjurer|Conjurer|Diviner|Enchanter|Illusionist|Invoker|'
    r'Necromancer|Transmuter|Specialist|Psionicist|Monk|Dwarf|Elf|Gnome|'
    r'Halfling|Half-elf|Half-orc|Human|Multi-class)')
_NONITEM_PATTERNS = [
    re.compile(r'\(CRE\)\s*$'),                        # S&P kit/trait records
    re.compile(r'\bdialect\b', re.I),                  # racial dialects
    re.compile(r'secret language', re.I),
    re.compile(r',\s*Followers?\s*\(\d+\)', re.I),     # follower grants
    re.compile(r'^' + _NONITEM_CLASS_RACE + r',\s+.+\(-?\d+\)\s*$'),  # ability buys
]

def _is_non_item_part(name):
    """True for PARTS.DAT records that are S&P character-build data, not gear."""
    return any(p.search(name or '') for p in _NONITEM_PATTERNS)


# Activated magic 'item' records that otherwise sit inert: consumables (used up
# in one use) vs reusable devices. Each gets a single click-to-use action so the
# sheet can actually trigger them (mirrors OSRIC, which actions ~all consumables).
# The detailed per-item effect changes stay out of scope (hand-authored data).
_ITEM_CONSUMABLE_RE = re.compile(
    r'^(oil|dust|powder|elixir|philter|philtre|salve) of\b', re.I)
_ITEM_SCROLL_RE = re.compile(r'^(scroll of\b|spell scroll\b|cursed scroll\b)', re.I)
_ITEM_DEVICE_RE = re.compile(r'^(wand|staff|rod|horn) of\b', re.I)

def _magic_item_action_group(name, img):
    """A use-action group for an activated magic 'item' (None if not activated).
    Consumables/scrolls spend one on use; wands/rods/horns are reusable."""
    if _ITEM_CONSUMABLE_RE.search(name):
        label, consume = 'Use', True
    elif _ITEM_SCROLL_RE.search(name):
        label, consume = 'Read', True
    elif _ITEM_DEVICE_RE.search(name):
        label, consume = 'Activate', False
    else:
        return None
    icon = img or 'icons/svg/item-bag.svg'
    return _make_action_group(label, icon, [
        _make_action(label, type_='use', targeting='self', img=icon,
                     consume_item=consume)])


def make_part_item(part, img_path=None, base_weapons=None):
    """Generate a Foundry/ARS Item from a parsed PART record, in the OSRIC
    2026.05.20 schema. Type inference: potion by name, weapon if it has a damage
    type, armor if it has AC, else a generic item. Every rules value is sourced
    from PARTS.DAT at runtime; absent values stay at neutral defaults (no
    hand-typed PHB numbers)."""
    item_id = make_id()
    # Strip magic suffixes (" +1", "+2 Dragon Slayer", …) for the HTML lookup and
    # for matching a magic variant back to its base weapon.
    base_name = re.sub(r'\s*[+\-]\d+.*$', '', part['name']).strip()
    # Magic weapon variants (Sword, long +2) lose their damage zone in PARTS.DAT
    # (different binary layout), so they arrive with no damage_type and often a
    # spurious armor marker. Recover the weapon traits from the matching base
    # weapon — a name match is authoritative over the bogus armor flag.
    if base_weapons and part.get('damage_type') is None:
        bw = base_weapons.get(base_name.lower())
        if bw:
            part = {**part, 'is_armor': False}
            for k in ('damage_type', 'dmg_normal', 'dmg_large', 'speed',
                      'size_category', 'handedness', 'rof'):
                if part.get(k) is None and bw.get(k) is not None:
                    part[k] = bw[k]
    is_armor = part.get('is_armor', False)
    has_weapon_traits = part.get('damage_type') is not None
    # Weapon takes priority over armor: real armor carries no damage type, while
    # the armor-AC marker (float32 -1.0) turns up spuriously in many weapon
    # records (yielding a bogus ac=0). Checking damage_type first avoids
    # mis-typing Spear / Long bow / Sword as armor.
    # Ring/Cloak of Protection: PARTS.DAT doesn't flag these as armor (the bonus
    # is only in the name), but ARS needs them as `armor` docs to route the AC.
    jewelry = None if (has_weapon_traits or part['name'].lower().startswith('potion')) \
        else _ac_jewelry_protection(part['name'])
    if part['name'].lower().startswith('potion'):
        ftype = 'potion'
    elif has_weapon_traits:
        ftype = 'weapon'
    elif is_armor or jewelry:
        ftype = 'armor'
    else:
        ftype = 'item'

    magic_bonus = part.get('magic_bonus', 0)
    is_magic = bool(magic_bonus) or bool(re.search(r'[+\-]\d', part['name']))
    size = _SIZE_NORMALIZE.get(part.get('size_category', ''), 'medium')

    # Common ARSItem attributes (OSRIC shape). classRestrictions isn't an ARS
    # field, so the DAT restricted-class list is preserved under flags instead.
    # type (equipment category) + material (item-save category) are name-derived
    # (see classifiers above); material is only set when determinable so the
    # schema default ("leather_book") covers the unknowns instead of an invalid "".
    attributes = {
        "rarity": "", "type": _item_subcategory(part['name']), "subtype": "",
        "magic": is_magic, "properties": [], "skillmods": [], "conditionals": [],
        "identified": True, "infiniteammo": False, "size": size,
    }
    material = _item_material(part['name'], ftype)
    if material:
        attributes["material"] = material

    # Primary: AEG (equipment descriptions — mundane + AEG-specific items)
    description = lookup_html_description(base_name, _ITEM_HTML_BOOKS)
    # Fallback: DMG (magic item descriptions — potions, rings, wands, special weapons…)
    # Try base_name first, then magic-specific candidates for sword/crossbow/armor variants.
    if not description:
        for dmg_cand in [base_name] + _item_magic_lookup_candidates(part['name']):
            description = lookup_html_description(dmg_cand, _ITEM_DMG_BOOKS)
            if description:
                break
    # Final fallback for weapons/armor: the C&T Weapon/Armor Description glossaries
    # (CT00375 / CT00378), which cover the exotic & historical gear the PHB/DMG/AEG
    # omit. Matches the weapon/armor type by contiguous phrase (so a magic variant
    # like "plate mail of blending +1" picks the *plate mail* entry, and a category
    # match like "Sword." backstops base/unmatched items).
    if not description and ftype in ('weapon', 'armor'):
        description = _ct_item_description(part['name'], ftype)
    # Magic 'item' records (rings, amulets, boots, bags…) whose trailing variant
    # qualifier defeated the generic DMG lookup above.
    if not description and ftype == 'item':
        description = _item_dmg_magic_description(part['name'])
    # Potions: their own DMG glossary page ("<Type>-- Potion").
    if ftype == 'potion' and not description:
        p_desc, _ = _potion_description_and_heal(part['name'])
        if p_desc:
            description = p_desc

    system = {
        "description": description, "dmonlytext": "", "itemList": [], "alias": "",
        "attributes": attributes,
        "charges": {"value": 0, "min": 0, "max": 0, "reuse": "none"},
        "location": {"state": "carried", "parent": ""},
        "resource": {"itemId": ""},
        "quantity": None,
        "weight": part.get('weight', 0.0),
        "cost": {"value": part.get('cost_gp', 0), "currency": "gp"},
        "source": "", "xp": 0,
    }
    # Gems / art objects price themselves in the name. This wins over cost_gp:
    # the DAT cost is a single byte, so values >255 wrap (Art Object 300 gp →
    # byte 44); the name carries the true, untruncated value.
    nc = _cost_from_name(part['name'])
    if nc:
        system["cost"] = {"value": nc[0], "currency": nc[1]}

    if ftype == 'weapon':
        rof = part.get('rof')
        per_round = f"{rof['num']}/{rof['den']}" if rof else "1/1"
        # Launcher weapons (bows/crossbows/slings/blowgun/arquebus) attack at
        # range; everything else defaults to melee. Thrown melee weapons
        # (dagger, spear, …) stay "melee" — we model a single item, not a
        # separate thrown profile. Range bands come from PHB Table 45 at runtime.
        is_ranged = bool(_RANGED_WEAPON_RE.search(part['name']))
        attack_range = (_missile_range_for(part['name']) if is_ranged else None) \
            or {"short": "", "medium": "", "long": ""}
        system["attack"] = {
            "speed": part.get('speed', 0),
            "type": "ranged" if is_ranged else "melee",
            "perRound": per_round,
            "modifier": 0, "magicBonus": magic_bonus, "magicPotency": 0,
            "range": attack_range,
            "primary": False, "speedmod": "",
        }
        dmg_code = part.get('damage_type', '')
        dmg_types = _weapon_damage_type_list(dmg_code)
        dmg_normal = part.get('dmg_normal')
        dmg_large  = part.get('dmg_large')
        # Multi-type 2e weapons (P/S polearms, throwing knife, B/S clubs) are
        # stored with no dice in PARTS.DAT — fill them from the rulebook tables
        # so the base damage roll (and the per-type choice actions below) work.
        # PHB Table 44 first (exact for the standard polearms), then the more
        # complete C&T Master Weapons Table for the exotic weapons the PHB omits
        # (stone axe, no-dachi, mace-axe, spade, scythe, bill). Limited to
        # multi-type weapons to avoid mis-assigning melee dice to launchers.
        if len(dmg_types) > 1 and not dmg_normal:
            for _dice_src in (_phb_weapon_dice, _ct_weapon_dice):
                src_n, src_l = _dice_src(part['name'])
                if src_n:
                    dmg_normal = src_n
                    if not dmg_large:
                        dmg_large = src_l
                    break
        system["damage"] = {
            "type": _ars_damage_type(dmg_code),
            "normal": dmg_normal or "0",
            "large":  dmg_large or "0",
            "otherdmg": [], "modifier": 0, "magicBonus": magic_bonus,
        }
        system["weaponstyle"] = ""
        # Weapon has a top-level system.size (schema initial "medium") in addition
        # to attributes.size; set it so non-medium weapons don't all read medium.
        system["size"] = size
        # Damage-type choice (issue #15): a 2e weapon that strikes for either of
        # two types (e.g. hook fauchard P/S) keeps its primary type in
        # system.damage.type and offers one click-to-roll damage action per type
        # (same dice) so the player picks the type that bypasses a resistance.
        # ARS has no native "either/or" damage field; OSRIC drops the second type
        # entirely, so this is new ground but valid action schema.
        system["actionGroups"] = []
        if len(dmg_types) > 1 and dmg_normal:
            _dmg_icon = 'systems/ars/icons/general/DamageColor.png'
            choice_actions = [
                _make_action(t.capitalize(), type_='damage', targeting='single',
                             formula=dmg_normal, damage_type=t, img=_dmg_icon)
                for t in dmg_types
            ]
            system["actionGroups"] = [
                _make_action_group('Damage (type choice)', _dmg_icon,
                                   choice_actions)
            ]
    effect_docs = []
    if ftype == 'armor':
        dat_ac = part.get('armor_class', 10)
        if jewelry:
            # Ring/Cloak of Protection: bonus lives in protection.modifier
            # (ac stays 0); the matching save bonus is a separate effect. Radius
            # versions deliver both via an aura instead (modifier 0).
            ptype, ac_bonus, save_bonus, aura_dist = jewelry
            origin = f"Compendium.{MODULE_ID}.adnd2-items.Item.{item_id}"
            system["protection"] = {
                "type": ptype,
                "ac": 0,
                "modifier": 0 if aura_dist else ac_bonus,
                "bulk": "none",
                "points": {"min": 0, "max": 0, "value": 0},
            }
            if aura_dist:
                effect_docs.append(_make_effect_doc(
                    f"{part['name']} (Aura)", img_path or "icons/svg/aura.svg",
                    [{"key": "aura.system.mods.ac.value", "type": "add", "value": ac_bonus},
                     {"key": "aura.system.mods.saves.all", "type": "custom",
                      "value": {"formula": str(save_bonus), "properties": ""}}],
                    origin, transfer=True,
                    aura={"enabled": True, "distance": aura_dist}))
            else:
                effect_docs.append(_make_effect_doc(
                    f"{part['name']} (Saves)", img_path or "icons/svg/upgrade.svg",
                    [{"key": "system.mods.saves.all", "type": "custom",
                      "value": {"formula": str(save_bonus), "properties": ""}}],
                    origin, transfer=True))
        else:
            # is_armor item: body armor / shield / warding (bracers, robe).
            # PARTS.DAT carries the base (unmodified) AC and the magic bonus
            # separately; ARS applies the modifier on top of the base. For a
            # shield the DAT stores the *resulting* AC (10 - bonus), so the
            # shield's own contribution is 10 - dat_ac (added by ARS).
            ptype = _armor_protection_type(part['name'])
            ac_val = max(0, 10 - dat_ac) if ptype == 'shield' else dat_ac
            system["protection"] = {
                "type": ptype,
                "ac": ac_val,
                "modifier": magic_bonus,
                "bulk": "none",
                "points": {"min": 0, "max": 0, "value": 0},
            }
        system["armorstyle"] = ""
        system["actionGroups"] = []

    # Every potion gets a "Quaff" action group so it's actually usable from the
    # sheet instead of sitting inert until drunk (OSRIC gives 88/89 potions a
    # use action). A `use` action drinks-and-consumes one potion (resource type
    # "item"); heal/damage actions are added when the description text states the
    # dice. The richer per-effect changes (Giant Strength → STR, etc.) are
    # hand-authored game data in OSRIC and remain out of scope.
    if ftype == 'potion':
        quaff_icon = img_path or 'icons/svg/item-bag.svg'
        quaff_actions = [_make_action('Drink', type_='use', targeting='self',
                                      img=quaff_icon, consume_item=True)]
        quaff_actions += _mechanic_actions(description, part['name'])
        system["actionGroups"] = [_make_action_group('Quaff', quaff_icon,
                                                     quaff_actions)]
    # Other activated magic items (oils, scrolls, wands, dust, horns…) get a
    # use/activate action, plus heal/damage actions parsed from their text.
    if ftype == 'item':
        extra = _mechanic_actions(description, part['name'])
        _mag_ag = _magic_item_action_group(part['name'], img_path)
        if _mag_ag:
            _mag_ag['actions'].extend(extra)
            system["actionGroups"] = [_mag_ag]
        elif extra:
            system["actionGroups"] = [_make_action_group(
                'Use', img_path or 'icons/svg/item-bag.svg', extra)]

    flags = {"adnd2": {"partId": part.get('item_id')}}
    if part.get('restricted_classes'):
        flags["adnd2"]["restrictedClasses"] = part['restricted_classes']

    item = {
        "_id": item_id,
        "name": part['name'],
        "type": ftype,
        "img": img_path or "icons/svg/item-bag.svg",
        "system": system,
        "effects": [e["_id"] for e in effect_docs], "flags": flags,
        "folder": None, "sort": 0, "ownership": {"default": -1}, "_stats": _stats_block(),
    }
    return item, effect_docs


_SPELL_SCHOOL_ICONS = {
    'Necromancy':     'icons/magic/death/skeleton-eye-skull-glow-orange.webp',
    'Necromantic':    'icons/magic/death/skeleton-eye-skull-glow-orange.webp',
    'Alteration':     'icons/magic/control/silhouette-grow-shrink-tan.webp',
    'Conjuration':    'icons/magic/symbols/runes-star-pentagon-orange.webp',
    'Conjuration/Summoning': 'icons/magic/symbols/runes-star-pentagon-orange.webp',
    'Divination':     'icons/magic/perception/orb-eye-scrying.webp',
    'Greater Divination':  'icons/magic/perception/orb-eye-scrying.webp',
    'Lesser Divination':   'icons/magic/perception/orb-eye-scrying.webp',
    'Enchantment':    'icons/magic/perception/eye-ringed-glow-angry-large-red.webp',
    'Enchantment/Charm':   'icons/magic/perception/eye-ringed-glow-angry-large-red.webp',
    'Illusion':       'icons/magic/perception/silhouette-stealth-shadow.webp',
    'Illusion/Phantasm':   'icons/magic/perception/silhouette-stealth-shadow.webp',
    'Invocation':     'icons/magic/lightning/bolt-strike-blue.webp',
    'Evocation':      'icons/magic/lightning/bolt-strike-blue.webp',
    'Evocation/Invocation':'icons/magic/lightning/bolt-strike-blue.webp',
    'Abjuration':     'icons/magic/defensive/shield-barrier-glowing-blue.webp',
}


# Per-sphere icons for divine spells whose school is empty.
_SPELL_SPHERE_ICONS = {
    'Healing':    'icons/magic/life/heart-cross-strong-flame-purple-orange.webp',
    'Plant':      'icons/magic/nature/leaf-glow-green.webp',
    'Animal':     'icons/creatures/abilities/paw-print-tan.webp',
    'Astral':     'icons/magic/movement/abstract-ribbons-red-orange.webp',
    'Combat':     'icons/weapons/swords/sword-broad-worn.webp',
    'Divination': 'icons/magic/perception/orb-eye-scrying.webp',
    'Necromantic':'icons/magic/death/skeleton-eye-skull-glow-orange.webp',
    'Sun':        'icons/svg/sun.svg',
    'Summoning':  'icons/magic/symbols/runes-star-pentagon-orange.webp',
    'Weather':    'icons/magic/air/wind-stream-blue-gray.webp',
    'Elemental':  'icons/magic/fire/dagger-rune-enchant-flame-blue-yellow.webp',
    'Elemental, Fire':  'icons/magic/fire/dagger-rune-enchant-flame-blue-yellow.webp',
    'Elemental, Water': 'icons/magic/water/wave-water-blue.webp',
    'Elemental, Earth': 'icons/commodities/stone/boulder-grey.webp',
    'Elemental, Air':   'icons/magic/air/wind-stream-blue-gray.webp',
    'Chaos':      'icons/svg/aura.svg',
    'Law':        'icons/magic/defensive/shield-barrier-blue.webp',
    'Wards':      'icons/magic/defensive/shield-barrier-glowing-blue.webp',
    'Charm':      'icons/magic/control/hypnosis-mesmerism-eye.webp',
    'Travelers':  'icons/magic/movement/trail-streak-pink.webp',
    'Protection': 'icons/magic/defensive/shield-barrier-glowing-blue.webp',
    'Creation':   'icons/magic/symbols/runes-star-pentagon-orange.webp',
}


# ── Runtime registry for copyrighted spell text (see project copyright rule) ──
# No AD&D spell titles / proper names are stored as literals here or in the icon
# map below. For text the logic genuinely needs (named-mage signature spells, and
# spells whose DAT name diverges from their page title), we hard-code only a
# LOCATION — an integer record index into the user's SPELLS.DAT, or a source .HTM
# path — and read the actual text from the user's own files at runtime. Comments
# use generic theme descriptors only.
_IC_HAND    = 'icons/magic/control/control-influence-puppet.webp'
_IC_WILD    = 'icons/magic/movement/abstract-ribbons-red-orange.webp'
_IC_SHAPE   = 'icons/magic/control/silhouette-grow-shrink-tan.webp'
_IC_RUNE    = 'icons/magic/symbols/rune-sigil-green-purple.webp'
_IC_MESMER  = 'icons/magic/control/hypnosis-mesmerism-eye.webp'
_IC_STEALTH = 'icons/magic/perception/silhouette-stealth-shadow.webp'
_IC_WARD    = 'icons/magic/symbols/rune-sigil-hook-white-red.webp'

# SPELLS.DAT record index → themed icon. The spell name is read at runtime; only
# the integer index is in source. Grouped by generic theme.
_SPELL_ICON_INDEX = {
    # conjured hand / fist
    168: _IC_HAND, 201: _IC_HAND, 240: _IC_HAND, 266: _IC_HAND, 288: _IC_HAND,
    # size / shape alteration
    37: _IC_SHAPE, 235: _IC_SHAPE, 412: _IC_SHAPE,
    # mesmerism / forced laughter
    81: _IC_MESMER,
    # arcane signature rune-work
    186: _IC_RUNE, 224: _IC_RUNE, 251: _IC_RUNE, 252: _IC_RUNE,
    296: _IC_RUNE, 331: _IC_RUNE, 403: _IC_RUNE, 459: _IC_RUNE,
    # wild-magic surge
    305: _IC_WILD, 311: _IC_WILD, 338: _IC_WILD, 363: _IC_WILD, 364: _IC_WILD, 378: _IC_WILD,
    # concealment band
    404: _IC_STEALTH,
    # warding refusal
    310: _IC_WARD,
    # radiant colour burst (not in OSRIC → sourced by index, not a keyword)
    9: 'icons/magic/light/explosion-star-glow-blue-purple.webp',
}

# SPELLS.DAT record index → source .HTM path, for spells whose DAT name diverges
# from their page title (typo). Without this the name lookup fails and the spell
# would be dropped (migrate_spells drops description-less spells). Text read at
# runtime from the user's CD-ROM.
# Per-spell HTM-page overrides for records whose DAT name doesn't match any
# page title — typos, missing words, cross-book spells (MM monster pages, S&M),
# or empty <TITLE>. Keys are CURRENT SPELLS.DAT indices; like the reverse-index
# tables they must be recalibrated whenever spell-record parsing shifts (the
# casting-time field-shift fix desynced them, silently dropping these spells AND
# feeding wrong descriptions to the records that landed on the stale indices).
_SPELL_DESC_HTM_INDEX = {
    28:  'PHB/PHB00433.HTM',   # Nystul's Magical Aura (wizard L1)
    328: 'TOM/TOM00067.HTM',   # Maximilian's Stony Grasp — HTM "Stoney" (wizard L3)
    745: 'TOM/TOM00169.HTM',   # Create Campsite/Break Camp (priest L3)
    # Call Phoenix is embedded in the Phoenix monster page (MM) — anchor to spell section.
    920: ('MM/MM00240.HTM', 'Call Phoenix'),
    # HTM title is "Chariot Sustarre" (missing "of") — bypass name lookup.
    529: 'PHB/PHB00871.HTM',
    # HTM title has no space after comma: "Control Temperature,10' Radius" vs DAT "10-foot".
    648: 'PHB/PHB00802.HTM',
    # Create/Destroy Crypt Thing (wizard + priest) — embedded in the Crypt Thing monster page.
    454: ('MM/MM00043.HTM', 'Create Crypt Thing'),
    455: ('MM/MM00043.HTM', 'Destroy Crypt Thing'),
    916: ('MM/MM00043.HTM', 'Create Crypt Thing'),
    917: ('MM/MM00043.HTM', 'Destroy Crypt Thing'),
    # HTM title "Dimension Blade" (missing "al") — bypass name lookup.
    417: 'SM/SM00285.HTM',
    # DAT typo "Enegry Drain" — PHB page title is correct "Energy Drain".
    912: 'PHB/PHB00700.HTM',
    # HTM title "Persistance" (missing 'e') — bypass name lookup.
    424: 'SM/SM00292.HTM',
    # SM00336 (Recitation) has an empty <TITLE> — bypass name lookup.
    856: 'SM/SM00336.HTM',
    # DAT "Summon" maps to "Summon Animal Spirit" page (title singular vs DAT generic name).
    914: 'SM/SM00326.HTM',
}

_spell_records_cache = None
def _spell_records():
    """Parse + cache SPELLS.DAT once (ordered) so index references resolve to the
    same records migrate_spells iterates."""
    global _spell_records_cache
    if _spell_records_cache is None:
        try:
            _spell_records_cache = parse_spells()
        except Exception:
            _spell_records_cache = []
    return _spell_records_cache

_spell_icon_by_name = None
def _spell_icon_for_name(name):
    """Themed icon for a named-mage spell, resolved by reading the spell name at
    its hard-coded SPELLS.DAT index at runtime. '' if not an indexed spell."""
    global _spell_icon_by_name
    if _spell_icon_by_name is None:
        _spell_icon_by_name = {}
        recs = _spell_records()
        for idx, icon in _SPELL_ICON_INDEX.items():
            if 0 <= idx < len(recs):
                _spell_icon_by_name[recs[idx]['name'].strip().lower()] = icon
    return _spell_icon_by_name.get((name or '').strip().lower(), '')


def _spell_description_from_path(rel):
    """Read + clean a spell description directly from a hard-coded source .HTM
    path (a location reference; the text is read at runtime).

    `rel` may be a tuple (path, anchor_text) when the spell is embedded inside
    a larger page (e.g. a monster page).  In that case only the HTML starting
    from the first occurrence of `anchor_text` (case-insensitive) is cleaned.
    """
    anchor = None
    if isinstance(rel, tuple):
        rel, anchor = rel
    path = os.path.join(SOURCE_BASE, rel)
    if not os.path.exists(path):
        return ''
    try:
        book_key = rel.split('/')[0]
        src_dir_files = {f.upper(): f for f in os.listdir(os.path.dirname(path))}
        if anchor is None:
            return clean_html_file(path, book_key, src_dir_files)
        # Anchor mode: extract the HTML slice starting at anchor_text.
        with open(path, 'r', encoding='cp1252') as fh:
            raw = fh.read()
        lo = raw.lower().find(anchor.lower())
        if lo < 0:
            return clean_html_file(path, book_key, src_dir_files)
        # Back up to the nearest tag boundary so we don't start mid-tag.
        tag_start = raw.rfind('<', 0, lo)
        if tag_start < 0:
            tag_start = lo
        snippet = raw[tag_start:]
        wrapped = f'<HTML><BODY>{snippet}</BODY></HTML>'
        soup = BeautifulSoup(wrapped, 'html.parser')
        body = soup.find('body') or soup
        return _clean_html_body(body, soup, book_key, src_dir_files)
    except Exception:
        return ''


# Reversible-spell relationships as (reverse_idx, primary_idx) SPELLS.DAT index
# pairs — no spell names in source; names read at runtime. Two lists, because the
# two uses differ:
#   _SPELL_REVERSE_INDEX        — TRUE reversibles (Cure↔Cause, Light↔Darkness …):
#                                 feed BOTH the HTM-lookup redirect (reverse→primary)
#                                 AND the child-link (primary→reverse, Pass B).
#   _SPELL_LOOKUP_REDIRECT_INDEX — lookup-only variants (Improved Blink→Blink,
#                                 Dismiss X→Conjure Fire …): feed ONLY the HTM
#                                 lookup; they must NOT auto-chain as children.
# Pairs whose reverse isn't a distinct DAT record are omitted (nothing to link).
# Indices recalibrated to the current SPELLS.DAT parser (record count 933).
# These shift whenever spell record parsing changes (e.g. the casting-time
# field-shift fix added records, desyncing the original v0.1 indices and
# silently dropping the reverses). _build_reverse_maps() now asserts each pair
# shares class+level at runtime and warns loudly if a future shift recurs.
_SPELL_REVERSE_INDEX = [
    (431, 440), (432, 439), (433, 441), (434, 46), (435, 50), (438, 66),
    (442, 109), (444, 154), (448, 197), (476, 390), (872, 581), (873, 480),
    (874, 548), (876, 639), (878, 483), (879, 506), (880, 534), (891, 509),
    (892, 583), (893, 539), (898, 561), (901, 585), (902, 631), (903, 594),
    (905, 546), (908, 586), (911, 522), (886, 590), (887, 591),
    (477, 237), (924, 570),  # Improved Create Water <- Transmute Water to Dust (wiz, priest)
]
_SPELL_LOOKUP_REDIRECT_INDEX = [
    (410, 85), (436, 84), (437, 50), (443, 118), (472, 474), (473, 474),
    (883, 482), (906, 566), (909, 566), (910, 561), (931, 566), (932, 566),
]
_reverse_maps_cache = None
def _build_reverse_maps():
    """Build (and cache) the two reversible-spell maps by reading spell names at
    the hard-coded SPELLS.DAT indices at runtime: (rev2prim, prim2rev). True
    reversibles populate both directions; lookup-only redirects populate rev2prim
    only (so variants don't auto-chain as child items). See _reverse_pairs()."""
    global _reverse_maps_cache
    if _reverse_maps_cache is not None:
        return _reverse_maps_cache
    recs = _spell_records()
    def nm(i):
        return recs[i]['name'].strip().lower() if 0 <= i < len(recs) else None
    def _consistent(ri, pi):
        # A reversible and its primary share class_type and level. If they don't,
        # the hard-coded indices have desynced from the parser (record count
        # changed) — warn loudly so the desync is caught, never silently dropped.
        if not (0 <= ri < len(recs) and 0 <= pi < len(recs)):
            return False
        a, b = recs[ri], recs[pi]
        ok = (a.get('class_type') == b.get('class_type')
              and a.get('level') == b.get('level'))
        if not ok:
            print(f"  ⚠ reverse-index desync: {a.get('name')!r} (idx {ri}) vs "
                  f"{b.get('name')!r} (idx {pi}) — recalibrate _SPELL_*_INDEX")
        return ok
    rev2prim, prim2rev = {}, {}
    for ri, pi in _SPELL_REVERSE_INDEX:        # true reversibles → both directions
        if not _consistent(ri, pi): continue
        r, p = nm(ri), nm(pi)
        if r and p:
            rev2prim.setdefault(r, p)
            prim2rev.setdefault(p, r)
    for ri, pi in _SPELL_LOOKUP_REDIRECT_INDEX:  # variants → lookup direction only
        # lookup redirects may legitimately cross level (variant → base spell),
        # so only require the same class_type here.
        if not (0 <= ri < len(recs) and 0 <= pi < len(recs)
                and recs[ri].get('class_type') == recs[pi].get('class_type')):
            if 0 <= ri < len(recs):
                print(f"  ⚠ lookup-redirect desync at idx {ri} "
                      f"({recs[ri].get('name')!r}) — recalibrate _SPELL_*_INDEX")
            continue
        r, p = nm(ri), nm(pi)
        if r and p:
            rev2prim.setdefault(r, p)
    _reverse_maps_cache = (rev2prim, prim2rev)
    return _reverse_maps_cache

def _reverse_pairs():
    """reverse-name → primary-name (for routing a reverse spell's HTM lookup to
    its primary's page). Names read from SPELLS.DAT at runtime."""
    return _build_reverse_maps()[0]

def _reversibles_primary_to_reverse():
    """primary-name → reverse-name (for child-linking the reverse onto the
    primary). Names read from SPELLS.DAT at runtime."""
    return _build_reverse_maps()[1]


# Keyword → icon mapping for GENERIC spell-effect words (fire, frost, heal, …).
# Substring matched in declaration order (longest / most specific first). Used by
# pick_spell_icon after the index-sourced named-spell icons, then school/sphere.
_SPELL_NAME_ICON_MAP = [
    # ── Healing / life ──
    ('cure',                'icons/magic/life/heart-cross-strong-flame-purple-orange.webp'),
    ('heal',                'icons/magic/life/heart-cross-strong-flame-purple-orange.webp'),
    ('wound',               'icons/skills/wounds/blood-drip-droplet-red.webp'),
    ('restore',             'icons/magic/life/cross-area-circle-green-white.webp'),
    ('regenerat',           'icons/magic/life/cross-flared-green.webp'),
    ('resurrect',           'icons/magic/life/ankh-gold-blue.webp'),
    ('raise dead',          'icons/magic/life/ankh-gold-blue.webp'),

    # ── Death / necromancy ──
    ('death',               'icons/magic/death/skeleton-eye-skull-glow-orange.webp'),
    ('slay',                'icons/magic/death/skeleton-eye-skull-glow-orange.webp'),
    ('animate dead',        'icons/magic/death/hand-undead-skeleton-fire-green.webp'),
    ('skeleton',            'icons/magic/death/hand-undead-skeleton-fire-green.webp'),
    ('necromantic',         'icons/magic/death/skeleton-eye-skull-glow-orange.webp'),
    ('plague',              'icons/magic/death/projectile-skull-animal-green.webp'),
    ('disease',             'icons/magic/death/projectile-skull-animal-green.webp'),

    # ── Detection / divination ──
    # Alignment detection variants: same divinatory mechanic but mirror
    # polarity — distinguish by colored eye (red for evil, teal for good).
    ('detect evil',         'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('detect good',         'icons/magic/perception/eye-ringed-glow-angry-teal.webp'),
    ('detect magic',        'icons/magic/symbols/runes-carved-stone-purple.webp'),
    ('detect',              'icons/magic/perception/orb-eye-scrying.webp'),
    ('find',                'icons/magic/perception/orb-eye-scrying.webp'),
    ('locate',              'icons/magic/perception/orb-eye-scrying.webp'),
    ('know alignment',      'icons/magic/perception/orb-eye-scrying.webp'),
    ('true seeing',         'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('clairvoyance',        'icons/magic/perception/orb-eye-scrying.webp'),
    ('clairaudience',       'icons/magic/perception/orb-eye-scrying.webp'),
    ('identify',            'icons/magic/perception/orb-eye-scrying.webp'),
    ('augury',              'icons/magic/perception/orb-eye-scrying.webp'),
    ('divination',          'icons/magic/perception/orb-eye-scrying.webp'),
    ('contact other plane', 'icons/magic/perception/orb-eye-scrying.webp'),
    ('vision',              'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('see',                 'icons/magic/perception/eye-ringed-glow-angry-red.webp'),

    # ── Summon / conjure ──
    ('summon monster',      'icons/magic/symbols/runes-star-pentagon-orange.webp'),
    ('summon shadow',       'icons/magic/light/orb-shadow-blue.webp'),
    ('summon swarm',        'icons/creatures/invertebrates/bee-simple-green.webp'),
    ('summon',              'icons/magic/symbols/runes-star-pentagon-orange.webp'),
    ('conjure elemental',   'icons/magic/fire/dagger-rune-enchant-flame-blue-yellow.webp'),
    ('conjure',             'icons/magic/symbols/runes-star-pentagon-orange.webp'),
    ('create',              'icons/magic/symbols/runes-star-pentagon-orange.webp'),
    ('call',                'icons/magic/symbols/runes-star-pentagon-orange.webp'),
    ('gate',                'icons/magic/movement/portal-vortex-orange.webp'),

    # ── Fire ──
    ('fireball',            'icons/magic/fire/blast-jet-stream-embers-orange.webp'),
    ('flame strike',        'icons/magic/fire/beam-jet-stream-spiral-yellow.webp'),
    ('wall of fire',        'icons/magic/fire/barrier-wall-flame-ring-yellow.webp'),
    ('flame',               'icons/magic/fire/dagger-rune-enchant-flame-blue-yellow.webp'),
    ('burning',             'icons/magic/fire/dagger-rune-enchant-flame-blue-yellow.webp'),
    ('fire shield',         'icons/magic/fire/barrier-wall-flame-ring-yellow.webp'),
    ('flaming sphere',      'icons/magic/fire/dagger-rune-enchant-flame-blue-yellow.webp'),
    ('produce flame',       'icons/magic/fire/dagger-rune-enchant-flame-blue-yellow.webp'),
    ('fire',                'icons/magic/fire/dagger-rune-enchant-flame-blue-yellow.webp'),

    # ── Cold / ice ──
    ('cone of cold',        'icons/magic/water/snowflake-ice-blue-white.webp'),
    ('ice storm',           'icons/magic/water/snowflake-ice-blue-white.webp'),
    ('wall of ice',         'icons/magic/water/barrier-ice-crystal-wall-faceted-blue.webp'),
    ('frost',               'icons/magic/water/snowflake-ice-blue.webp'),
    ('cold',                'icons/magic/water/snowflake-ice-blue.webp'),
    ('chill',               'icons/magic/water/snowflake-ice-blue.webp'),
    ('snowball',            'icons/magic/water/snowflake-ice-blue.webp'),
    ('snow',                'icons/magic/water/snowflake-ice-blue.webp'),
    ('ice',                 'icons/magic/water/snowflake-ice-blue.webp'),

    # ── Lightning / storm ──
    ('lightning bolt',      'icons/magic/lightning/bolt-strike-blue.webp'),
    ('chain lightning',     'icons/magic/lightning/bolt-strike-blue.webp'),
    ('lightning',           'icons/magic/lightning/bolt-strike-blue.webp'),
    ('thunder',             'icons/magic/lightning/bolt-strike-blue.webp'),
    ('storm',               'icons/magic/air/air-burst-spiral-large-yellow.webp'),
    ('shocking',            'icons/magic/lightning/bolt-strike-blue.webp'),
    ('shock',               'icons/magic/lightning/bolt-strike-blue.webp'),

    # ── Air / wind ──
    ('whirlwind',           'icons/magic/air/air-burst-spiral-large-blue.webp'),
    ('gust of wind',        'icons/magic/air/wind-stream-blue-gray.webp'),
    ('wind walk',           'icons/magic/air/wind-stream-blue-gray.webp'),
    ('control wind',        'icons/magic/air/wind-stream-blue-gray.webp'),
    ('wind',                'icons/magic/air/wind-stream-blue-gray.webp'),
    ('fog',                 'icons/magic/air/air-smoke-casting.webp'),
    ('cloud',               'icons/magic/air/air-smoke-casting.webp'),
    ('mist',                'icons/magic/air/air-smoke-casting.webp'),
    ('air',                 'icons/magic/air/wind-stream-blue-gray.webp'),

    # ── Earth / stone ──
    ('wall of stone',       'icons/magic/earth/barrier-stone-brown-green.webp'),
    ('stone shape',         'icons/commodities/stone/boulder-grey.webp'),
    ('stone tell',          'icons/commodities/stone/boulder-grey.webp'),
    ('stone',               'icons/commodities/stone/boulder-grey.webp'),
    ('earthquake',          'icons/magic/earth/explosion-lava-orange.webp'),
    ('rock',                'icons/commodities/stone/boulder-grey.webp'),
    ('earth',               'icons/magic/earth/barrier-stone-brown-green.webp'),
    ('meld into',           'icons/commodities/stone/boulder-grey.webp'),

    # ── Water ──
    ('water breathing',     'icons/magic/water/wave-water-blue.webp'),
    ('water walk',          'icons/magic/water/wave-water-blue.webp'),
    ('control water',       'icons/magic/water/wave-water-blue.webp'),
    ('water',               'icons/magic/water/wave-water-blue.webp'),
    ('tidal',               'icons/magic/water/wave-water-blue.webp'),
    ('wave',                'icons/magic/water/wave-water-blue.webp'),

    # ── Acid / poison ──
    ('acid',                'icons/magic/death/projectile-skull-animal-green.webp'),
    ('poison',              'icons/magic/death/projectile-skull-animal-green.webp'),

    # ── Wall / barrier ──
    ('wall of force',       'icons/magic/defensive/shield-barrier-deflect-teal.webp'),
    ('wall',                'icons/magic/defensive/shield-barrier-glowing-blue.webp'),

    # ── Protection / abjuration ──
    # Alignment-protection variants: same defensive abjuration mirrored.
    # Blue shield (protection from evil → ward against evil = good's shield)
    # vs gold-touched shield (protection from good → evil's shield).
    ('protection from evil',  'icons/magic/defensive/shield-barrier-deflect-teal.webp'),
    ('protection from good',  'icons/magic/defensive/shield-barrier-deflect-gold.webp'),
    ('protection from',     'icons/magic/defensive/shield-barrier-glowing-blue.webp'),
    ('protection',          'icons/magic/defensive/shield-barrier-glowing-blue.webp'),
    ('shield',              'icons/magic/defensive/shield-barrier-glowing-blue.webp'),
    ('sanctuary',           'icons/magic/defensive/shield-barrier-glowing-blue.webp'),
    # 'aura' moved below to the "Auras / circle / sigil" section so the
    # specific aura-of-comfort / magical-aura / elemental-aura entries can
    # match first.
    ('magic resistance',    'icons/magic/defensive/shield-barrier-deflect-teal.webp'),
    ('absorb',              'icons/magic/defensive/shield-barrier-deflect-teal.webp'),

    # ── Charm / hold / sleep / fear ──
    ('charm',               'icons/magic/control/hypnosis-mesmerism-eye.webp'),
    ('hold person',         'icons/magic/sonic/explosion-shock-wave-teal.webp'),
    ('hold monster',        'icons/magic/sonic/explosion-shock-wave-teal.webp'),
    ('hold',                'icons/magic/sonic/explosion-shock-wave-teal.webp'),
    ('sleep',               'icons/magic/control/sleep-bubble-purple.webp'),
    ('hypnosis',            'icons/magic/control/hypnosis-mesmerism-pendulum.webp'),
    ('hypnotic',            'icons/magic/control/hypnosis-mesmerism-pendulum.webp'),
    ('fear',                'icons/magic/control/fear-fright-mask-yellow.webp'),
    ('cause fear',          'icons/magic/control/fear-fright-mask-yellow.webp'),
    ('confusion',           'icons/magic/control/hypnosis-mesmerism-pendulum.webp'),
    ('suggest',             'icons/magic/control/hypnosis-mesmerism-eye.webp'),
    ('command',             'icons/magic/control/control-influence-crown-gold.webp'),
    ('emotion',             'icons/magic/control/hypnosis-mesmerism-eye.webp'),
    ('domination',          'icons/magic/control/control-influence-crown-gold.webp'),
    ('feeblemind',          'icons/magic/control/hypnosis-mesmerism-pendulum.webp'),
    ('quest',               'icons/magic/control/control-influence-crown-gold.webp'),
    ('geas',                'icons/magic/control/control-influence-crown-gold.webp'),

    # ── Polymorph / shape ──
    ('polymorph',           'icons/magic/control/silhouette-grow-shrink-tan.webp'),
    ('shape change',        'icons/magic/control/silhouette-grow-shrink-tan.webp'),
    ('alter self',          'icons/magic/control/silhouette-grow-shrink-blue.webp'),
    ('change self',         'icons/magic/control/silhouette-grow-shrink-blue.webp'),
    ('shrink',              'icons/magic/control/silhouette-grow-shrink-blue.webp'),
    ('reduce',              'icons/magic/control/silhouette-grow-shrink-blue.webp'),
    ('enlarge',             'icons/magic/control/silhouette-grow-shrink-tan.webp'),

    # ── Light / dark ──
    ('continual light',     'icons/magic/air/weather-sunlight-sky.webp'),
    ('sunray',              'icons/magic/air/weather-sunlight-sky.webp'),
    ('sunburst',            'icons/magic/air/weather-sunlight-sky.webp'),
    ('light',               'icons/magic/air/weather-sunlight-sky.webp'),
    ('darkness',            'icons/magic/light/orb-shadow-blue.webp'),
    ('dark',                'icons/magic/light/orb-shadow-blue.webp'),

    # ── Invisibility / illusion ──
    ('mirror image',        'icons/magic/defensive/illusion-evasion-echo-purple.webp'),
    ('invisibility',        'icons/magic/perception/silhouette-stealth-shadow.webp'),
    ('shadow',              'icons/magic/perception/silhouette-stealth-shadow.webp'),
    ('phantasm',            'icons/magic/perception/silhouette-stealth-shadow.webp'),
    ('illusion',            'icons/magic/perception/silhouette-stealth-shadow.webp'),
    ('blur',                'icons/magic/perception/silhouette-stealth-shadow.webp'),

    # ── Movement / teleport / fly ──
    ('teleport',            'icons/magic/movement/portal-vortex-orange.webp'),
    ('dimension door',      'icons/environment/wilderness/mine-interior-dungeon-door.webp'),
    ('plane shift',         'icons/magic/movement/portal-vortex-orange.webp'),
    ('fly',                 'icons/magic/control/buff-flight-wings-purple.webp'),
    ('levitate',            'icons/magic/control/buff-flight-wings-purple.webp'),
    ('jump',                'icons/creatures/mammals/deer-movement-leap-green.webp'),
    ('haste',               'icons/magic/movement/trail-streak-pink.webp'),
    ('expeditious',         'icons/magic/movement/trail-streak-pink.webp'),
    ('slow',                'icons/magic/symbols/rune-sigil-hook-white-red.webp'),
    ('feather fall',        'icons/magic/control/buff-flight-wings-purple.webp'),
    ('astral',              'icons/magic/movement/abstract-ribbons-red-orange.webp'),
    ('etherealness',        'icons/magic/movement/abstract-ribbons-red-orange.webp'),

    # ── Mind / psychic ──
    ('mind blank',          'icons/magic/sonic/explosion-shock-wave-teal.webp'),
    ('mind',                'icons/magic/perception/eye-slit-red-orange.webp'),
    ('telepathy',           'icons/magic/sonic/projectile-sound-rings-wave.webp'),
    ('esp',                 'icons/magic/sonic/projectile-sound-rings-wave.webp'),

    # ── Speech / sound ──
    ('speak with',          'icons/magic/sonic/projectile-sound-rings-wave.webp'),
    ('tongues',             'icons/magic/sonic/projectile-sound-rings-wave.webp'),
    ('silence',             'icons/magic/sonic/explosion-shock-wave-teal.webp'),
    ('audible',             'icons/magic/sonic/projectile-sound-rings-wave.webp'),
    ('shout',               'icons/magic/sonic/projectile-sound-rings-wave.webp'),
    ('sound',               'icons/magic/sonic/projectile-sound-rings-wave.webp'),

    # ── Bless / curse / dispel ──
    # Order: 'remove curse' / 'bestow curse' / 'cure' specific keywords
    # match BEFORE the generic 'curse' so the heal-or-buff vs malevolent
    # variants get distinct icons.
    ('remove curse',        'icons/magic/symbols/rune-sigil-hook-white-red.webp'),
    ('bestow curse',        'icons/magic/death/skeleton-eye-skull-glow-orange.webp'),
    ('bless',               'icons/magic/life/cross-area-circle-green-white.webp'),
    ('curse',               'icons/magic/death/skeleton-eye-skull-glow-orange.webp'),
    ('dispel evil',         'icons/magic/defensive/shield-barrier-glowing-blue.webp'),
    ('dispel good',         'icons/magic/symbols/rune-sigil-hook-white-red.webp'),
    ('dispel magic',        'icons/magic/symbols/rune-sigil-hook-white-red.webp'),
    ('dispel',              'icons/magic/symbols/rune-sigil-hook-white-red.webp'),
    ('remove',              'icons/magic/symbols/rune-sigil-hook-white-red.webp'),
    ('negate',              'icons/magic/symbols/rune-sigil-hook-white-red.webp'),

    # ── Purify / Putrefy (food/drink) ──
    ('purify',              'icons/magic/life/cross-area-circle-green-white.webp'),
    ('putrefy',             'icons/magic/death/projectile-skull-animal-green.webp'),

    # ── Door / lock / passwall ──
    ('knock',               'icons/environment/wilderness/mine-interior-dungeon-door.webp'),
    ('hold portal',         'icons/sundries/misc/key-angular-white.webp'),
    ('passwall',            'icons/environment/wilderness/mine-interior-dungeon-door.webp'),
    ('wizard lock',         'icons/sundries/misc/key-angular-white.webp'),
    ('arcane lock',         'icons/sundries/misc/key-angular-white.webp'),
    ('door',                'icons/environment/wilderness/mine-interior-dungeon-door.webp'),

    # ── Plant / nature ──
    ('entangle',            'icons/magic/nature/root-vine-entangle-foot-green.webp'),
    ('plant growth',        'icons/magic/nature/leaf-glow-green.webp'),
    ('tree',                'icons/magic/nature/tree-bare-glow-yellow.webp'),
    ('plant',               'icons/magic/nature/leaf-glow-green.webp'),
    ('animal',              'icons/creatures/abilities/paw-print-tan.webp'),

    # ── Weapon-like / projectile ──
    ('magic missile',       'icons/magic/lightning/bolt-strike-blue.webp'),
    ('arrow',               'icons/skills/ranged/arrow-flying-broadhead-metal.webp'),
    ('bolt',                'icons/magic/lightning/bolt-strike-blue.webp'),
    ('blade',               'icons/weapons/swords/sword-broad-worn.webp'),
    ('sword',               'icons/weapons/swords/sword-broad-worn.webp'),
    ('hammer',              'icons/weapons/hammers/hammer-war-spiked.webp'),

    # ── Misc magic plumbing ──
    ('rune',                'icons/magic/symbols/runes-carved-stone-purple.webp'),
    ('symbol',              'icons/magic/symbols/runes-carved-stone-red.webp'),
    ('mark',                'icons/magic/symbols/runes-carved-stone-purple.webp'),
    ('glyph',               'icons/magic/symbols/runes-carved-stone-red.webp'),
    ('scroll',              'icons/sundries/scrolls/scroll-bound-blue-tan.webp'),

    # ── Creatures ──
    ('wolf',                'icons/creatures/mammals/wolf-howl-moon-black.webp'),
    ('cat',                 'icons/creatures/mammals/cat-hunched-glowing-red.webp'),
    ('spider',              'icons/creatures/invertebrates/spider-large-white-green.webp'),
    ('serpent',             'icons/creatures/reptiles/snake-fangs-bite-green-yellow.webp'),
    ('snake',               'icons/creatures/reptiles/snake-fangs-bite-green-yellow.webp'),
    ('hawk',                'icons/creatures/birds/raptor-hawk-flying.webp'),
    ('eagle',               'icons/creatures/birds/raptor-hawk-flying.webp'),
    ('bird',                'icons/creatures/birds/raptor-hawk-flying.webp'),
    ('bat',                 'icons/creatures/mammals/bats-movement-flying-black.webp'),
    ('insect',              'icons/creatures/invertebrates/ant-strength-green.webp'),
    ('swarm',               'icons/creatures/invertebrates/bee-simple-green.webp'),
    ('elemental',           'icons/magic/fire/dagger-rune-enchant-flame-blue-yellow.webp'),

    # ── Conjured hand/fist effects ──
    ('hand',                'icons/magic/control/control-influence-puppet.webp'),
    ('fist',                'icons/magic/control/control-influence-puppet.webp'),

    # ── Buffs ──
    ('friends',             'icons/magic/control/hypnosis-mesmerism-eye.webp'),
    ('courage',             'icons/magic/life/cross-area-circle-green-white.webp'),
    ('chant',               'icons/magic/symbols/runes-carved-stone-purple.webp'),
    ('prayer',              'icons/magic/life/cross-area-circle-green-white.webp'),
    ('blessing',            'icons/magic/life/cross-area-circle-green-white.webp'),
    ('endurance',           'icons/magic/life/cross-flared-green.webp'),
    ('strength',            'icons/magic/control/silhouette-grow-shrink-tan.webp'),
    ('agility',             'icons/magic/life/cross-flared-green.webp'),
    ('blink',               'icons/magic/movement/portal-vortex-orange.webp'),

    # ── Statue / object animation ──
    ('animate object',      'icons/magic/control/silhouette-grow-shrink-tan.webp'),
    ('animate',             'icons/magic/control/silhouette-grow-shrink-tan.webp'),
    ('statue',              'icons/commodities/stone/boulder-grey.webp'),

    # ── Time / counterspell / extension ──
    ('time stop',           'icons/magic/time/hourglass-tilted-glowing-gold.webp'),
    ('reverse time',        'icons/magic/time/hourglass-tilted-glowing-gold.webp'),
    ('antimagic',           'icons/magic/symbols/rune-sigil-hook-white-red.webp'),
    ('counterspell',        'icons/magic/symbols/rune-sigil-hook-white-red.webp'),
    ('extension',           'icons/magic/life/cross-flared-green.webp'),

    # ── Status effects ──
    ('blind',               'icons/magic/perception/eye-slit-red-orange.webp'),
    ('paraly',              'icons/magic/sonic/explosion-shock-wave-teal.webp'),

    # ── Glitter / sparkle / dust ──
    ('glitter',             'icons/magic/symbols/runes-carved-stone-yellow.webp'),
    ('dust',                'icons/magic/symbols/runes-carved-stone-yellow.webp'),
    ('sparkle',             'icons/magic/symbols/runes-carved-stone-yellow.webp'),

    # ── Wish ──
    ('wish',                'icons/magic/symbols/runes-star-pentagon-orange.webp'),

    # ── Mislead / disguise ──
    ('mislead',             'icons/magic/perception/silhouette-stealth-shadow.webp'),
    ('disguise',            'icons/magic/control/silhouette-grow-shrink-blue.webp'),

    # ── Bard song / music ──
    ('song',                'icons/magic/sonic/projectile-sound-rings-wave.webp'),
    ('music',               'icons/magic/sonic/projectile-sound-rings-wave.webp'),

    # ── Numbers / calculation (Numbers sphere) ──
    ('calculate',           'icons/magic/symbols/runes-carved-stone-purple.webp'),
    ('number',              'icons/magic/symbols/runes-carved-stone-purple.webp'),

    # ── Material crafting ──
    ('copy',                'icons/sundries/scrolls/scroll-bound-blue-tan.webp'),
    ('dictation',           'icons/sundries/scrolls/scroll-bound-blue-tan.webp'),
    ('transcribe',          'icons/sundries/scrolls/scroll-bound-blue-tan.webp'),

    # ── Power Words (kill / blind not caught by 'X spell' alone) ──
    ('power word',          'icons/magic/sonic/projectile-sound-rings-wave.webp'),

    # ── Holy / Unholy Word ──
    # Order matters: 'unholy word' MUST come before 'holy word' because the
    # latter is a substring of the former and substring-matching is
    # first-wins.
    ('unholy word',         'icons/magic/death/skeleton-eye-skull-glow-orange.webp'),
    ('holy word',           'icons/magic/light/explosion-star-glow-blue-purple.webp'),

    # ── Wards / locks / sealing / cage / imprison / contingency ──
    ('ward',                'icons/magic/defensive/shield-barrier-glowing-blue.webp'),
    ('seal',                'icons/sundries/misc/key-angular-white.webp'),
    ('lock',                'icons/sundries/misc/key-angular-white.webp'),
    ('forcecage',           'icons/magic/defensive/shield-barrier-deflect-teal.webp'),
    ('imprison',            'icons/magic/sonic/explosion-shock-wave-teal.webp'),
    ('binding',             'icons/magic/sonic/explosion-shock-wave-teal.webp'),
    ('bind',                'icons/magic/sonic/explosion-shock-wave-teal.webp'),
    ('contingency',         'icons/magic/time/hourglass-tilted-glowing-gold.webp'),
    ('repulsion',           'icons/magic/defensive/shield-barrier-deflect-teal.webp'),
    ('safeguard',           'icons/magic/defensive/shield-barrier-glowing-blue.webp'),
    ('sanctuary',           'icons/magic/defensive/shield-barrier-glowing-blue.webp'),

    # ── Wild / chaos magic ──
    ('wild',                'icons/magic/movement/abstract-ribbons-red-orange.webp'),
    ('chaos',               'icons/magic/movement/abstract-ribbons-red-orange.webp'),
    ('random',              'icons/magic/movement/abstract-ribbons-red-orange.webp'),
    ('unluck',              'icons/magic/movement/abstract-ribbons-red-orange.webp'),
    ('vortex',              'icons/magic/air/air-burst-spiral-large-blue.webp'),
    ('miscast',             'icons/magic/symbols/rune-sigil-hook-white-red.webp'),
    ('surge',               'icons/magic/movement/abstract-ribbons-red-orange.webp'),

    # ── Time (Time sphere + Body Clock / Hesitation / Skip Day / Reverse) ──
    ('temporal',            'icons/magic/time/hourglass-tilted-glowing-gold.webp'),
    ('timeless',            'icons/magic/time/hourglass-tilted-glowing-gold.webp'),
    ('time',                'icons/magic/time/hourglass-tilted-glowing-gold.webp'),
    ('clock',               'icons/magic/time/hourglass-tilted-glowing-gold.webp'),
    ('skip day',            'icons/magic/time/hourglass-tilted-glowing-gold.webp'),
    ('nap',                 'icons/magic/control/sleep-bubble-purple.webp'),

    # ── Thought / mental / memory ──
    ('thought',             'icons/skills/wounds/anatomy-organ-brain-pink-red.webp'),
    ('memory',              'icons/skills/wounds/anatomy-organ-brain-pink-red.webp'),
    ('amnesia',             'icons/skills/wounds/anatomy-organ-brain-pink-red.webp'),
    ('forget',              'icons/skills/wounds/anatomy-organ-brain-pink-red.webp'),
    ('genius',              'icons/skills/wounds/anatomy-organ-brain-pink-red.webp'),
    ('idea',                'icons/skills/wounds/anatomy-organ-brain-pink-red.webp'),
    ('mnemonic',            'icons/skills/wounds/anatomy-organ-brain-pink-red.webp'),
    ('telepathy',           'icons/skills/wounds/anatomy-organ-brain-pink-red.webp'),
    ('telethaumaturgy',     'icons/skills/wounds/anatomy-organ-brain-pink-red.webp'),
    ('feeblemind',          'icons/skills/wounds/anatomy-organ-brain-pink-red.webp'),
    ('madness',             'icons/skills/wounds/anatomy-organ-brain-pink-red.webp'),
    ('insanity',            'icons/skills/wounds/anatomy-organ-brain-pink-red.webp'),

    # ── Spheres / globes / orbs ──
    ('sphere',              'icons/magic/symbols/runes-star-pentagon-orange.webp'),
    ('globe',               'icons/magic/defensive/shield-barrier-deflect-teal.webp'),
    ('orb',                 'icons/magic/symbols/runes-star-pentagon-orange.webp'),

    # ── Prismatic / rainbow light (generic; in OSRIC) ──
    ('prismatic',           'icons/magic/light/explosion-star-glow-blue-purple.webp'),
    ('rainbow',             'icons/magic/light/explosion-star-glow-blue-purple.webp'),
    ('kaleidoscop',         'icons/magic/light/explosion-star-glow-blue-purple.webp'),

    # ── Ground hazards / Web / Grease / Caltrops / Trip ──
    ('web',                 'icons/creatures/invertebrates/spider-large-white-green.webp'),
    ('grease',              'icons/magic/control/silhouette-grow-shrink-blue.webp'),
    ('caltrops',            'icons/weapons/ammunition/arrow-broadhead-glowing-orange.webp'),
    ('trip',                'icons/magic/control/silhouette-grow-shrink-blue.webp'),

    # ── Wood ──
    ('wood',                'icons/magic/nature/tree-bare-glow-yellow.webp'),

    # ── Geometry / spatial weirdness ──
    ('maze',                'icons/magic/symbols/runes-carved-stone-purple.webp'),
    ('spacewarp',           'icons/magic/symbols/runes-carved-stone-purple.webp'),
    ('distance distortion', 'icons/magic/symbols/runes-carved-stone-purple.webp'),
    ('dimensional',         'icons/magic/symbols/runes-carved-stone-purple.webp'),
    ('extradimensional',    'icons/magic/symbols/runes-carved-stone-purple.webp'),
    ('reverse gravity',     'icons/magic/movement/abstract-ribbons-red-orange.webp'),
    ('squaring',            'icons/magic/symbols/runes-carved-stone-purple.webp'),
    ('duo-dimension',       'icons/magic/symbols/runes-carved-stone-purple.webp'),

    # ── Probability / numerology ──
    ('probability',         'icons/magic/symbols/runes-carved-stone-purple.webp'),

    # ── Communication / sending / message / mouth ──
    ('sending',             'icons/magic/sonic/projectile-sound-rings-wave.webp'),
    ('message',             'icons/magic/sonic/projectile-sound-rings-wave.webp'),
    ('magic mouth',         'icons/magic/sonic/projectile-sound-rings-wave.webp'),
    ('ventriloquism',       'icons/magic/sonic/projectile-sound-rings-wave.webp'),
    ('audible',             'icons/magic/sonic/projectile-sound-rings-wave.webp'),
    ('dictate',             'icons/magic/sonic/projectile-sound-rings-wave.webp'),
    ('recitation',          'icons/magic/sonic/projectile-sound-rings-wave.webp'),
    ('deafness',            'icons/magic/sonic/explosion-shock-wave-teal.webp'),
    ('shout',               'icons/magic/sonic/projectile-sound-rings-wave.webp'),
    ('wail',                'icons/magic/sonic/projectile-sound-rings-wave.webp'),
    ('shatter',             'icons/magic/sonic/projectile-sound-rings-wave.webp'),
    ('lament',              'icons/magic/sonic/projectile-sound-rings-wave.webp'),
    ('banshee',             'icons/magic/death/skeleton-eye-skull-glow-orange.webp'),

    # ── Object manipulation / item / mending / warp ──
    ('mending',             'icons/sundries/scrolls/scroll-bound-blue-tan.webp'),
    ('warp',                'icons/magic/nature/root-vine-entangle-foot-green.webp'),
    ('item',                'icons/magic/symbols/runes-star-pentagon-orange.webp'),

    # ── Telekinesis / floating ──
    ('telekines',           'icons/magic/movement/abstract-ribbons-red-orange.webp'),
    ('floating',            'icons/magic/movement/trail-streak-pink.webp'),
    ('hovering',            'icons/magic/movement/trail-streak-pink.webp'),

    # ── Truth / lore / sight ──
    ('truth',               'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('lore',                'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('legend',              'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('sight',               'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('foresight',           'icons/magic/perception/orb-eye-scrying.webp'),
    ('premonition',         'icons/magic/perception/orb-eye-scrying.webp'),
    ('omniscient',          'icons/magic/perception/orb-eye-scrying.webp'),
    ('scry',                'icons/magic/perception/orb-eye-scrying.webp'),
    ('analyze',             'icons/magic/perception/orb-eye-scrying.webp'),
    ('weather predict',     'icons/magic/perception/orb-eye-scrying.webp'),

    # ── Vanish / veil / disbelief / mislead / misdirection ──
    ('vanish',              'icons/magic/perception/silhouette-stealth-shadow.webp'),
    ('veil',                'icons/magic/perception/silhouette-stealth-shadow.webp'),
    ('disbelief',           'icons/magic/perception/silhouette-stealth-shadow.webp'),
    ('misdirection',        'icons/magic/perception/silhouette-stealth-shadow.webp'),
    ('delude',              'icons/magic/perception/silhouette-stealth-shadow.webp'),
    ('hallucinatory',       'icons/magic/perception/silhouette-stealth-shadow.webp'),
    ('massmorph',           'icons/magic/control/silhouette-grow-shrink-tan.webp'),
    ('massmorph',           'icons/magic/control/silhouette-grow-shrink-tan.webp'),

    # ── Negative status (irritation / fatigue / enfeeblement / scare / spook) ──
    ('irritation',          'icons/magic/symbols/rune-sigil-rough-white-teal.webp'),
    ('fatigue',             'icons/magic/control/sleep-bubble-purple.webp'),
    ('enfeeblement',        'icons/magic/sonic/explosion-shock-wave-teal.webp'),
    ('scare',               'icons/magic/control/fear-fright-mask-yellow.webp'),
    ('spook',               'icons/magic/control/fear-fright-mask-yellow.webp'),
    ('malison',             'icons/magic/death/skeleton-eye-skull-glow-orange.webp'),
    ('contagion',           'icons/magic/death/projectile-skull-animal-green.webp'),

    # ── Wraith / spirit / form change ──
    ('wraithform',          'icons/magic/perception/silhouette-stealth-shadow.webp'),
    ('spirit',              'icons/magic/perception/silhouette-stealth-shadow.webp'),
    ('shade',               'icons/magic/perception/silhouette-stealth-shadow.webp'),

    # ── Vampiric / draining ──
    ('vampiric',            'icons/magic/death/skeleton-eye-skull-glow-orange.webp'),
    ('drain',               'icons/magic/death/skeleton-eye-skull-glow-orange.webp'),
    ('enervation',          'icons/magic/death/skeleton-eye-skull-glow-orange.webp'),
    ('disintegrat',         'icons/magic/death/skeleton-eye-skull-glow-orange.webp'),

    # ── Body buffs (vigil / wrath / armor / courage / fortify) ──
    ('vigil',               'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('wrath',               'icons/skills/melee/unarmed-punch-fist.webp'),
    ('armor',               'icons/equipment/chest/breastplate-banded-blue.webp'),
    ('vestment',            'icons/equipment/chest/breastplate-banded-blue.webp'),
    ('fortify',             'icons/magic/life/cross-flared-green.webp'),
    ('fortitude',           'icons/magic/life/cross-flared-green.webp'),

    # ── Plane / planar travel ──
    ('plane',               'icons/magic/movement/portal-vortex-orange.webp'),
    ('astral',              'icons/magic/movement/abstract-ribbons-red-orange.webp'),
    ('ethereal',            'icons/magic/movement/abstract-ribbons-red-orange.webp'),

    # ── Object / surface effects (oil / glassteel / heat metal) ──
    ('glassteel',           'icons/commodities/stone/boulder-grey.webp'),
    ('crystalbrittle',      'icons/commodities/stone/boulder-grey.webp'),
    ('heat metal',          'icons/magic/fire/dagger-rune-enchant-flame-blue-yellow.webp'),
    ('corrosion',           'icons/magic/death/projectile-skull-animal-green.webp'),
    ('rusting',             'icons/magic/death/projectile-skull-animal-green.webp'),
    ('solvent',             'icons/magic/death/projectile-skull-animal-green.webp'),

    # ── Movement: rope trick / hut / lodge / chest / hidden lodge ──
    ('rope trick',          'icons/magic/symbols/runes-star-pentagon-orange.webp'),
    ('lodge',               'icons/magic/defensive/shield-barrier-glowing-blue.webp'),
    ('hut',                 'icons/magic/defensive/shield-barrier-glowing-blue.webp'),
    ('chest',               'icons/magic/symbols/runes-star-pentagon-orange.webp'),

    # ── Pyrotechnics / explosion ──
    ('pyrotechnics',        'icons/magic/fire/blast-jet-stream-embers-orange.webp'),
    ('explos',              'icons/magic/fire/blast-jet-stream-embers-orange.webp'),

    # ── Magic interaction / dispel / disjunction / spell turning ──
    ('disjunction',         'icons/magic/symbols/rune-sigil-hook-white-red.webp'),
    ('spell turning',       'icons/magic/defensive/shield-barrier-deflect-teal.webp'),
    ('spell shape',         'icons/magic/symbols/runes-carved-stone-purple.webp'),
    ('invulnerability',     'icons/magic/defensive/shield-barrier-glowing-blue.webp'),

    # ── Wyvern / faithful hound / sentinel — guarding creatures ──
    ('wyvern',              'icons/creatures/reptiles/snake-fangs-bite-green-yellow.webp'),
    ('hound',               'icons/creatures/mammals/wolf-howl-moon-black.webp'),
    ('sentinel',            'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('servant',             'icons/magic/symbols/runes-star-pentagon-orange.webp'),
    ('chariot',             'icons/magic/symbols/runes-star-pentagon-orange.webp'),
    ('mount',               'icons/magic/symbols/runes-star-pentagon-orange.webp'),
    ('vermin',              'icons/creatures/invertebrates/ant-strength-green.webp'),

    # ── Holy / divine ──
    ('holy might',          'icons/magic/life/cross-area-circle-green-white.webp'),
    ('holy',                'icons/magic/life/cross-area-circle-green-white.webp'),
    ('sacred',              'icons/magic/life/cross-area-circle-green-white.webp'),
    ('divine',              'icons/magic/life/cross-area-circle-green-white.webp'),
    ('orison',              'icons/magic/life/cross-area-circle-green-white.webp'),

    # ── Chaos / order ──
    ('order',               'icons/magic/defensive/shield-barrier-glowing-blue.webp'),
    ('combine',             'icons/magic/symbols/runes-star-pentagon-orange.webp'),
    ('defensive harmony',   'icons/magic/defensive/shield-barrier-glowing-blue.webp'),

    # ── Sphere of Sun / sunmote / sunscorch ──
    ('sunmote',             'icons/magic/air/weather-sunlight-sky.webp'),
    ('sunscorch',           'icons/magic/air/weather-sunlight-sky.webp'),

    # ── Dance ──
    ('dance',               'icons/magic/control/hypnosis-mesmerism-pendulum.webp'),

    # ── Random catchalls ──
    ('reality',             'icons/magic/symbols/runes-carved-stone-purple.webp'),
    ('estate',              'icons/magic/movement/portal-vortex-orange.webp'),
    ('transfer',            'icons/magic/movement/portal-vortex-orange.webp'),
    ('exaction',            'icons/magic/control/control-influence-crown-gold.webp'),
    ('alacrity',            'icons/magic/movement/trail-streak-pink.webp'),
    ('alternate',           'icons/magic/movement/portal-vortex-orange.webp'),

    # ── Adjustments: dilation / extension / far reaching / augmentation ──
    ('dilation',            'icons/magic/control/silhouette-grow-shrink-tan.webp'),
    ('augmentation',        'icons/magic/symbols/runes-star-pentagon-orange.webp'),
    ('far reaching',        'icons/magic/symbols/runes-star-pentagon-orange.webp'),

    # ── Misc ──
    ('babble',              'icons/magic/sonic/projectile-sound-rings-wave.webp'),
    ('inversion',           'icons/magic/symbols/rune-sigil-hook-white-red.webp'),
    ('cantrip',             'icons/magic/symbols/runes-carved-stone-yellow.webp'),
    ('focus',               'icons/magic/symbols/runes-star-pentagon-orange.webp'),
    ('persistence',         'icons/magic/life/cross-flared-green.webp'),
    ('seclusion',           'icons/magic/defensive/shield-barrier-glowing-blue.webp'),
    ('iron body',           'icons/equipment/chest/breastplate-banded-blue.webp'),
    ('iron vigil',          'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('eye',                 'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('seven',               'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('insat',               'icons/magic/water/wave-water-blue.webp'),
    ('dig',                 'icons/magic/earth/explosion-lava-orange.webp'),
    ('grasp',               'icons/skills/melee/unarmed-punch-fist.webp'),
    ('grasping',            'icons/skills/melee/unarmed-punch-fist.webp'),
    ('dispatch',            'icons/magic/movement/portal-vortex-orange.webp'),
    ('descent',             'icons/magic/death/skeleton-eye-skull-glow-orange.webp'),
    ('banishment',          'icons/magic/symbols/rune-sigil-hook-white-red.webp'),
    ('temperature',         'icons/magic/fire/dagger-rune-enchant-flame-blue-yellow.webp'),
    ('proofing',            'icons/magic/defensive/shield-barrier-glowing-blue.webp'),
    ('weighty',             'icons/commodities/stone/boulder-grey.webp'),
    ('squeaking',           'icons/magic/sonic/projectile-sound-rings-wave.webp'),
    ('genius',              'icons/skills/wounds/anatomy-organ-brain-pink-red.webp'),
    ('compulsi',            'icons/magic/control/hypnosis-mesmerism-eye.webp'),
    ('enthrall',            'icons/magic/control/hypnosis-mesmerism-eye.webp'),
    ('hypnot',              'icons/magic/control/hypnosis-mesmerism-pendulum.webp'),
    ('hypnos',              'icons/magic/control/hypnosis-mesmerism-pendulum.webp'),
    ('cloak',               'icons/equipment/chest/breastplate-banded-blue.webp'),
    ('lance',               'icons/weapons/polearms/spear-flared-blue.webp'),
    ('meteor',              'icons/magic/fire/blast-jet-stream-embers-orange.webp'),
    ('missile',             'icons/magic/lightning/bolt-strike-blue.webp'),
    ('gaze',                'icons/magic/perception/eye-ringed-glow-angry-red.webp'),
    ('reflection',          'icons/magic/defensive/shield-barrier-deflect-teal.webp'),
    ('mirror',              'icons/magic/defensive/illusion-evasion-echo-purple.webp'),
    ('mineral',             'icons/commodities/stone/boulder-grey.webp'),
    ('vile venom',          'icons/magic/death/projectile-skull-animal-green.webp'),
    ('mace',                'icons/weapons/maces/mace-flanged-steel.webp'),
    ('zone',                'icons/magic/defensive/shield-barrier-glowing-blue.webp'),

    # ── Auras / circle / mystic sigil ──
    ('aura of comfort',     'icons/magic/life/cross-area-circle-green-white.webp'),
    ('aura',                'icons/magic/symbols/rune-sigil-green-purple.webp'),
    ('magical aura',        'icons/magic/symbols/rune-sigil-green-purple.webp'),
    ('elemental aura',      'icons/magic/fire/dagger-rune-enchant-flame-blue-yellow.webp'),
    ('great circle',        'icons/magic/symbols/circle-ouroboros.webp'),
    ('black circle',        'icons/magic/symbols/rune-sigil-black-pink.webp'),
    ('circle',              'icons/magic/symbols/circle-ouroboros.webp'),

    # ── Travel theme (Travelers sphere — Highway / Hovering Road / etc.) ──
    ('highway',             'icons/magic/movement/trail-streak-pink.webp'),
    ('hovering road',       'icons/magic/movement/trail-streak-pink.webp'),
    ('clear path',          'icons/magic/movement/trail-streak-pink.webp'),
    ('clutter path',        'icons/magic/movement/trail-streak-pink.webp'),
    ('path',                'icons/magic/movement/trail-streak-pink.webp'),
    ('traveler',            'icons/magic/movement/trail-streak-pink.webp'),
    ('road',                'icons/magic/movement/trail-streak-pink.webp'),
    ('withdraw',            'icons/magic/movement/portal-vortex-orange.webp'),

    # ── Weather (Control Weather / Uncontrolled / Land of Stability) ──
    ('weather',             'icons/magic/air/weather-clouds.webp'),
    ('land of stability',   'icons/magic/defensive/shield-barrier-glowing-blue.webp'),

    # ── Comfort / freedom / uplift / harmony ──
    ('comfort',             'icons/magic/life/cross-area-circle-green-white.webp'),
    ('freedom',             'icons/magic/movement/portal-vortex-orange.webp'),
    ('uplift',              'icons/magic/control/buff-flight-wings-purple.webp'),
    ('harmony',             'icons/magic/life/cross-area-circle-green-white.webp'),

    # ── Crypt / undead ──
    ('crypt',               'icons/magic/death/skeleton-eye-skull-glow-orange.webp'),

    # ── Order / conformance / permission / ethics ──
    ('conformance',         'icons/magic/control/control-influence-crown-gold.webp'),
    ('permission',          'icons/magic/control/control-influence-crown-gold.webp'),
    ('ethics',              'icons/magic/control/control-influence-crown-gold.webp'),
    ('inverted',            'icons/magic/movement/abstract-ribbons-red-orange.webp'),

    # ── Binding bands ──
    ('bands of',            'icons/magic/perception/silhouette-stealth-shadow.webp'),

    # ── Study / speed ──
    ('lucubration',         'icons/skills/wounds/anatomy-organ-brain-pink-red.webp'),
    ('celerity',            'icons/magic/movement/trail-streak-pink.webp'),

    # ── Magnetism / pull / attract ──
    ('magnetism',           'icons/magic/symbols/rune-sigil-hook-white-red.webp'),
    ('magnet',              'icons/magic/symbols/rune-sigil-hook-white-red.webp'),

    # ── Gas / vapor / mist / smoke (Neutralize Gas) ──
    ('gas',                 'icons/magic/air/air-smoke-casting.webp'),
    ('vapor',               'icons/magic/air/air-smoke-casting.webp'),
    ('smoke',               'icons/magic/air/air-smoke-casting.webp'),
    ('neutralize',          'icons/magic/symbols/rune-sigil-hook-white-red.webp'),

    # ── Erase / refusal ──
    ('erase',               'icons/magic/symbols/rune-sigil-hook-white-red.webp'),
    ('refusal',             'icons/magic/symbols/rune-sigil-hook-white-red.webp'),

    # ── Laughter / mockery / hilarity ──
    ('laughter',            'icons/magic/control/hypnosis-mesmerism-eye.webp'),
    ('taunt',               'icons/magic/control/hypnosis-mesmerism-eye.webp'),

    # ── Spectral effects ──
    ('spectral',            'icons/magic/perception/silhouette-stealth-shadow.webp'),

    # ── Hideous / weird mental ──
    ('weird',               'icons/magic/control/fear-fright-mask-yellow.webp'),
    ('hideous',             'icons/magic/control/fear-fright-mask-yellow.webp'),
    ('hesitation',          'icons/magic/time/hourglass-tilted-glowing-gold.webp'),
    ('reversion',           'icons/magic/time/hourglass-tilted-glowing-gold.webp'),
    ('repeat',              'icons/magic/time/hourglass-tilted-glowing-gold.webp'),
    ('know',                'icons/magic/perception/orb-eye-scrying.webp'),

    # ── Solipsism / fumble / irritation / sense shifting ──
    ('solipsism',           'icons/skills/wounds/anatomy-organ-brain-pink-red.webp'),
    ('fumble',              'icons/magic/control/silhouette-grow-shrink-blue.webp'),
    ('irritation',          'icons/magic/symbols/rune-sigil-red-orange.webp'),
    ('sense shifting',      'icons/magic/perception/silhouette-stealth-shadow.webp'),

    # ── Stalker / pursuer ──
    ('stalker',             'icons/magic/perception/silhouette-stealth-shadow.webp'),
    ('there/not',           'icons/magic/perception/silhouette-stealth-shadow.webp'),

    # ── Suspend / animation / stability ──
    ('suspend',             'icons/magic/time/hourglass-tilted-gray.webp'),
    ('stabilize',           'icons/magic/defensive/shield-barrier-glowing-blue.webp'),
    ('stability',           'icons/magic/defensive/shield-barrier-glowing-blue.webp'),

    # ── Spiral / inversion / degeneration ──
    ('spiral',              'icons/magic/movement/abstract-ribbons-red-orange.webp'),
    ('degeneration',        'icons/magic/death/skeleton-eye-skull-glow-orange.webp'),

    # ── Resonance / shatter / sonic destruction ──
    ('resonance',           'icons/magic/sonic/explosion-shock-wave-teal.webp'),
    ('destructive',         'icons/magic/sonic/explosion-shock-wave-teal.webp'),

    # ── Transformation (broader catch) ──
    ('transformation',      'icons/magic/control/silhouette-grow-shrink-tan.webp'),
    ('transform',           'icons/magic/control/silhouette-grow-shrink-tan.webp'),

    # ── Tsunami / wave catch-all ──
    ('tsunami',             'icons/magic/water/wave-water-blue.webp'),

    # ── Forest / nature with adjective qualifier ──
    ('forest',              'icons/magic/nature/leaf-glow-green.webp'),

    # ── Entrench / preservation ──
    ('entrench',            'icons/magic/defensive/shield-barrier-glowing-blue.webp'),
    ('preserve',            'icons/magic/defensive/shield-barrier-glowing-blue.webp'),
    ('preservation',        'icons/magic/defensive/shield-barrier-glowing-blue.webp'),

    # ── Self-displacement ──
    ('displace',            'icons/magic/perception/silhouette-stealth-shadow.webp'),

    # ── Boulder/pebble transformation ──
    ('boulder',             'icons/commodities/stone/boulder-grey.webp'),
    ('pebble',              'icons/commodities/stone/boulder-grey.webp'),
]


def pick_spell_icon(name, school, sphere=''):
    """Pick a spell-themed icon: longest keyword in name wins; falls back
    to the spell's school, then sphere, then a generic magical aura.
    Returns a verified-existing Foundry icons/ path.

    `book.svg` is intentionally NOT used as the generic fallback — it
    suggests the spell is about a literal book. Reserve it for spells
    whose name explicitly mentions a book (none in the AD&D 2e corpus
    on the CD-ROM as of this writing)."""
    if name:
        # Named-mage signature spells: icon resolved by reading the name at its
        # hard-coded SPELLS.DAT index (no spell title embedded in source).
        indexed = _spell_icon_for_name(name)
        if indexed:
            return indexed
        low = name.lower()
        for keyword, path in _SPELL_NAME_ICON_MAP:
            if keyword in low:
                return path
    if school and school in _SPELL_SCHOOL_ICONS:
        return _SPELL_SCHOOL_ICONS[school]
    if sphere and sphere in _SPELL_SPHERE_ICONS:
        return _SPELL_SPHERE_ICONS[sphere]
    return 'icons/magic/symbols/rune-sigil-rough-white-teal.webp'


_SAVE_KEYWORD_MAP = [
    # DAT saving-throw text → ARS saveCheck.type. First substring match wins.
    ('paralyzation',  'paralyzation'),
    ('poison',        'poison'),
    ('death',         'death'),
    ('petrification', 'petrification'),
    ('polymorph',     'polymorph'),
    ('breath',        'breath'),
    ('rod',           'rod'),
    ('staff',         'staff'),
    ('wand',          'wand'),
    ('spell',         'spell'),
]


def _spell_save_type(raw_save):
    """Map a SPELLS.DAT 'saving_throw' free-text field ('Spell; negates',
    'Neg.', 'Special', 'None', '½', etc.) to an ARS saveCheck.type slug.
    Defaults to 'spell' when a save is implied but ambiguous, 'none'
    when the field clearly signals no save."""
    if not raw_save: return 'none'
    low = raw_save.lower()
    if not low.strip() or low.strip() in ('none', 'nil'): return 'none'
    for kw, slug in _SAVE_KEYWORD_MAP:
        if kw in low: return slug
    return 'spell'   # generic save when unspecified — 'Neg.', 'Special', '½'


_SPELL_DAMAGE_FORMULA_RE = re.compile(
    # Match phrases like "1d6 per level", "2d8 + 1 per level", "5d4", "1d8/lvl"
    r'\b(\d+d\d+(?:\s*[+\-]\s*\d+)?(?:\s*(?:per\s+level|/(?:lvl|level)))?)\b',
    re.I,
)


def _spell_damage_formula(html_description):
    """Sniff a damage dice formula out of the spell's prose. Returns the
    raw formula string (e.g. '1d6 per level' → '1d6*@rank.levels.arcane',
    '5d4' → '5d4'), or '' if none is found. Conservative: only the FIRST
    plausible formula is returned, so spells with multiple variants get
    their primary damage."""
    if not html_description: return ''
    text = re.sub(r'<[^>]+>', ' ', html_description)
    m = _SPELL_DAMAGE_FORMULA_RE.search(text)
    if not m: return ''
    raw = re.sub(r'\s+', '', m.group(1)).lower()
    if 'perlevel' in raw or '/lvl' in raw or '/level' in raw:
        base = re.sub(r'(perlevel|/(?:lvl|level))$', '', raw)
        # The @rank reference is filled in by the action's actor at roll time
        return f'{base}*@rank.levels.arcane'
    return raw


_HEAL_NAME_KEYWORDS   = ('cure', 'heal', 'restore', 'regenerat', 'accelerate healing')
_DAMAGE_NAME_KEYWORDS = ('cause', 'wound', 'harm', 'inflict', 'drain', 'slay',
                         'destroy', 'enervat', 'disintegrat', 'energy drain')


def _spell_effect_action_type(name):
    """Decide whether a spell's dice formula should fire as a 'heal' or
    'damage' action based on the spell name. Cure/Heal/Restore variants
    heal; Cause/Inflict/Harm/Wound variants damage. Anything else (most
    spells) defaults to 'damage' — the GM applies as appropriate."""
    if not name: return 'damage'
    low = name.lower()
    for kw in _HEAL_NAME_KEYWORDS:
        if kw in low: return 'heal'
    for kw in _DAMAGE_NAME_KEYWORDS:
        if kw in low: return 'damage'
    return 'damage'


def _make_spell_action_groups(name, save_type, damage_formula,
                              targeting='single', img=''):
    """Build the actionGroup(s) for a spell item. Minimum: one 'cast'
    action posting a chat card (with the spell's save type so the GM gets
    the right Roll Save button). When a dice formula was sniffed from
    the description, append either a 'heal' or 'damage' action depending
    on the spell name (Cure/Cause/etc.), chained behind the cast.
    `img` is the spell item's icon, propagated to the group and cast action."""
    actions = [
        _make_action(name, type_='cast', img=img, targeting=targeting,
                     save_type=save_type, save_formula=''),
    ]
    if damage_formula:
        eff_type = _spell_effect_action_type(name)
        label    = 'Healing' if eff_type == 'heal' else 'Damage'
        actions.append(
            _make_action(label, type_=eff_type, img=img, targeting=targeting,
                         formula=damage_formula, damage_type='')
        )
    return [_make_action_group(name, img, actions)]


def _parse_spell_components(raw):
    """Parse a V/S/M components string into the ARS bool dict {verbal, somatic, material}.
    Returns all-False when raw is absent or contains non-component text."""
    if not raw: return {'verbal': False, 'somatic': False, 'material': False}
    toks = {t.strip().upper() for t in re.split(r'[,\s/]+', raw) if t.strip()}
    return {'verbal': 'V' in toks, 'somatic': 'S' in toks, 'material': 'M' in toks}


def _resolve_spell_components(dat_raw, description_html):
    """Pick the best V/S/M source: DAT field if it yields any component, otherwise
    parse the 'Components:' row from the spell's HTML stat-block table. The DAT
    field is sometimes misaligned (contains duration or range instead of components)
    so the HTML is the authoritative fallback."""
    result = _parse_spell_components(dat_raw)
    if result['verbal'] or result['somatic'] or result['material']:
        return result   # DAT gave a clean V/S/M string
    # DAT was empty or contained non-component text — try the HTML stat block
    html_raw = _spell_components_from_html(description_html)
    if html_raw:
        return _parse_spell_components(html_raw)
    return result   # all-False (no source found)


def _spell_components_from_html(description_html):
    """Extract V/S/M components from a spell's stat-block table in its description HTML.
    Looks for 'Components: V, S, M' in the right column of the 2-column stat block.
    Returns '' when not found."""
    if not description_html:
        return ''
    soup = BeautifulSoup(description_html, 'html.parser')
    for tbl in soup.find_all('table'):
        for row in tbl.find_all('tr'):
            cells = [td.get_text(' ', strip=True) for td in row.find_all('td')]
            for cell in cells:
                m = re.match(r'Components?\s*:\s*(.+)', cell, re.I)
                if m:
                    return m.group(1).strip()
    return ''


def make_spell_item(spell, desc_override_rel=None):
    """Build an ARS `spell` Item from a parsed SPELLS.DAT record. Spell metadata
    (level/school/sphere/range/components/…) lives at the top of system.{}, and
    `system.type` ("Arcane"/"Divine") is set explicitly (required field — see
    Adds a cast actionGroup (+ a damage/heal action when a dice
    formula is sniffed from the prose). `desc_override_rel` forces the
    description from a specific .HTM path for name/title-divergent spells."""
    item_id = make_id()
    school = spell.get('school', '')
    # Description: normally by name lookup; when the DAT name diverges from the
    # page title (desc_override_rel set from _SPELL_DESC_HTM_INDEX), read it from
    # the hard-coded source path instead so the spell isn't dropped.
    description = ''
    if desc_override_rel:
        description = _spell_description_from_path(desc_override_rel)
    if not description:
        description = lookup_html_description(spell['name'], _SPELL_HTML_BOOKS)
    cls = spell.get('class_type', 'wizard')
    spell_type = 'Divine' if cls == 'priest' else 'Arcane'
    save_type  = _spell_save_type(spell.get('saving_throw', ''))
    dmg        = _spell_damage_formula(description)
    aoe_raw    = (spell.get('area_of_effect','') or '').lower()
    targeting  = 'self' if 'caster' in aoe_raw else 'single'
    sphere     = (spell.get('extra_schools_spheres') or [''])[0] if cls == 'priest' else ''
    extra_spheres = spell.get('extra_schools_spheres', []) if cls == 'priest' else []
    # Spell schema (ARSItemSpell, verified against ARS 2026.05.25 + osric 2026.05.20):
    # level/school/sphere/range/components/durationText/castingTime/areaOfEffect/save/learned
    # live at the TOP of system.{}, not under system.attributes.
    # `system.type` ("Arcane"/"Divine") IS a real, required field (schema initial
    # "Arcane"). It must be set explicitly: omitting it silently makes every
    # priest spell "Arcane" (OSRIC populates it on all 476 of its spells, and
    # school-vs-sphere inference is unreliable — divine spells can carry both).
    spell_img = pick_spell_icon(spell['name'], school, sphere)
    return {
        "_id": item_id,
        "name": spell['name'],
        "type": "spell",
        "img": spell_img,
        "system": {
            "description":  description,
            "type":         spell_type,
            "level":        spell.get('level', 0),
            "school":       school,
            "sphere":       sphere,
            "range":        spell.get('range', ''),
            "components":   _resolve_spell_components(
                                spell.get('components', ''), description),
            "durationText": spell.get('duration', ''),
            "castingTime":  spell.get('casting_time', ''),
            "areaOfEffect": spell.get('area_of_effect', ''),
            "save":         spell.get('saving_throw', ''),
            "learned":      False,
            "actionGroups": _make_spell_action_groups(
                                spell['name'], save_type, dmg,
                                targeting=targeting, img=spell_img),
            # itemList stays empty here; migrate_spells fills it in a
            # post-pass when the spell is the primary of a true reversible
            # pair, so a PC who memorizes it auto-receives the reverse.
            "itemList":     [],
        },
        # Preserve the additional spheres (S&P spells appear in multiple
        # spheres) in a module flag — ARS schema only carries one `sphere`.
        "effects": [],
        "flags": {"adnd2": {"spellClass": cls, "extraSpheres": extra_spheres}},
        "folder": None, "sort": 0, "ownership": {"default": -1}, "_stats": _stats_block(),
    }


def make_power_item(power, description=''):
    """Build an ARS `power` Item (psionic power) from a parsed PSIONIC.DAT record.
    `description` is either S&P HTML (preferred) or plain DAT text (fallback).

    PSP costs come from the `power_score` field parsed from PSIONIC.DAT/S&P, which
    follows the format "initial/maintenance" (e.g. "5/2", "7+/3+"). These map to:
      system.powercost    = initial activation PSP cost
      system.maintenance  = per-round maintenance PSP cost
    Combat-mode costs (powerCostAttack / powerCostDefense) are left "0" for
    regular powers; they apply only to the 5 psionic attack/defense combat modes."""
    item_id  = make_id()
    disc_idx = power.get('discipline', -1)
    # Plain-text DAT descriptions: normalise line breaks for HTML storage.
    if description and '<' not in description:
        description = re.sub(r'[\r\n]+', ' ', description).strip()
    # Split "initial/maintenance" PSP cost string read from the source at runtime.
    raw_cost   = str(power.get('power_score', '') or '')
    parts      = raw_cost.split('/')
    psp_cost   = parts[0].strip() if parts else '0'
    psp_maint  = parts[1].strip() if len(parts) > 1 else '0'
    return {
        "_id": item_id,
        "name": power['name'],
        "type": "power",
        "img": _power_icon(power['name']),
        "system": {
            "description":      description,
            "discipline":       _DISC_NAMES.get(disc_idx, ''),
            "range":            power.get('range', ''),
            "areaOfEffect":     power.get('area_of_effect', '') or 'personal',
            "prerequisites":    power.get('prerequisite', '') or 'none',
            "abilityMod":       "0",
            "powercost":        psp_cost   or '0',
            "maintenance":      psp_maint  or '0',
            "powerCostAttack":  '0',
            "powerCostDefense": '0',
        },
        "effects": [], "flags": {"adnd2": {}},
        "folder": None, "sort": 0, "ownership": {"default": -1}, "_stats": _stats_block(),
    }


_ALIGN_MAP = {
    'Lawful Good':'lg','Lawful Neutral':'ln','Lawful Evil':'le',
    'Neutral Good':'ng','True Neutral':'n','Neutral Evil':'ne',
    'Chaotic Good':'cg','Chaotic Neutral':'cn','Chaotic Evil':'ce',
    'Any':'any','Any Alignment':'any',
}

# AD&D 2e size codes/words → ARS size slugs. Mapped by first letter, which
# covers both the single-letter codes (T/S/M/L/H/G) and the spelled-out words
# (Tiny/Small/Medium/Large/Huge/Gargantuan) seen in the MM "SIZE:" stat line.
_SIZE_LETTER_MAP = {'t': 'tiny', 's': 'small', 'm': 'medium',
                    'l': 'large', 'h': 'huge', 'g': 'gargantuan'}


def _size_token_to_slug(text):
    """Map a MM SIZE cell value to an ARS size slug, or None. Handles both the
    spelled-out word and the single-letter code, and tolerates label prefixes
    ("Individual: T (1\" long)") or qualifiers ("Varies, usually M")."""
    if not text:
        return None
    m = re.search(r'\b(tiny|small|medium|large|huge|gargantuan)\b', text, re.I)
    if m:
        return m.group(1).lower()
    m = re.search(r'\b([TSMLHG])\b', text)        # standalone size code letter
    if m:
        return _SIZE_LETTER_MAP.get(m.group(1).lower())
    return None


def _mm_statblock_column(variant_key, col_names):
    """Pick the value-column index in a multi-column MM stat block that matches
    this variant record, or 0 (first column) when there's no confident match.
    `variant_key` is the monster name minus its genus (e.g. 'Huge', 'Orog',
    'Great, Leopard'); `col_names` are the comparison-table headers ('Large',
    'Huge', 'Giant'). Match is exact first, then either-way containment on a
    trimmed header token, so 'Great, Leopard' resolves to the 'Leopard' column
    and 'Azmyth' to 'Night Azmyth'. Pure name-vs-header string logic — no game
    data is embedded, only the caller's own record names and the MM headers."""
    vk = (variant_key or '').strip().lower()
    if not vk or not col_names:
        return 0
    for i, c in enumerate(col_names):
        if c.strip().lower() == vk:
            return i
    for i, c in enumerate(col_names):
        cl = c.strip().lower().rstrip('-/ ').strip()
        if cl and (cl in vk or vk in cl):
            return i
    return 0


def _parse_mm_statblock(biography_html, variant_key=None):
    """Parse the MM stat-block table from the (already-built) biography HTML
    into {NORMALIZED_LABEL: value}. Labels are uppercased with the trailing colon
    stripped (e.g. "NO. OF ATTACKS"). Multi-form comparison blocks (Giant Scorpion
    Large/Huge/Giant, Bear Black/Brown/Cave/Polar, Orc/Orog, …) open with a header
    row of variant names followed by one value column each; the column matching
    `variant_key` is selected — previously the first column was applied to every
    variant, so Huge/Giant scorpions inherited the Large damage/morale/etc.
    (issue #16). Single-form blocks have no header row and yield their sole value
    column (variant_key ignored). Returns {} if absent. This replaces the fragile
    tail-strings heuristic for #attacks / morale / special attacks & defenses /
    damage etc. — all read straight from the MM."""
    out = {}
    if not biography_html or '<table' not in biography_html.lower():
        return out
    tbl = BeautifulSoup(biography_html, 'html.parser').find('table')
    if not tbl:
        return out
    rows = tbl.find_all('tr')
    # A multi-column comparison block opens with a header row: a blank first cell
    # (the label column) followed by >=2 non-empty variant names. Single-form
    # blocks start straight into a labeled stat row (one value cell), so this is
    # never triggered for them.
    col_names = []
    body_rows = rows
    if rows:
        head = [c.get_text(' ', strip=True) for c in rows[0].find_all('td')]
        if head and not head[0].strip() and sum(1 for c in head[1:] if c.strip()) >= 2:
            col_names = [c.strip() for c in head[1:]]
            body_rows = rows[1:]
    col_idx = _mm_statblock_column(variant_key, col_names)
    for tr in body_rows:
        cells = tr.find_all('td')
        if len(cells) < 2:
            continue
        label = cells[0].get_text(' ', strip=True).upper().rstrip(':').strip()
        if not label or label in out:
            continue
        vals = [c.get_text(' ', strip=True) for c in cells[1:]]
        out[label] = vals[col_idx] if col_idx < len(vals) else vals[0]
    return out


def _size_from_statblock(biography_html, statblock=None):
    """Map the MM stat-block "SIZE:" value to an ARS size slug, or None.
    Size is not in MONSTER.DAT, so the MM HTM stat line is the source."""
    sb = statblock if statblock is not None else _parse_mm_statblock(biography_html)
    return _size_token_to_slug(sb.get('SIZE', ''))


_DIE_SIZES = {2, 3, 4, 6, 8, 10, 12, 20}


def _range_to_dice(text):
    """Convert a MM 'a-b' damage range to dice notation, pass through an existing
    dice formula, else None. The endpoints uniquely determine the dice, so this
    is an exact re-expression of the MM range — not a hand-typed value. Two forms,
    multiplier first: 'XdY' when hi is a multiple of lo (1-8→1d8, 2-8→2d4,
    3-18→3d6); then '1dY+Z' when the spread (hi-lo+1) is a die size (5-8→1d4+4,
    2-5→1d4+1). Never both (a multiplier hit means the spread isn't a die)."""
    if not text:
        return None
    text = text.strip()
    m = re.fullmatch(r'(\d+)\s*-\s*(\d+)', text)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo >= 1 and hi > lo:
            if hi % lo == 0 and (hi // lo) in _DIE_SIZES:
                return f"{lo}d{hi // lo}"
            spread = hi - lo + 1
            if spread in _DIE_SIZES:
                mod = lo - 1
                return f"1d{spread}+{mod}" if mod else f"1d{spread}"
    if re.fullmatch(r'\d+d\d+([+-]\d+)?', text):
        return text
    return None


def _pick_damage(dat_dmg, sb_dmg):
    """Choose the free-text damage string. Prefer the labeled MM stat-block value,
    but fall back to the DAT value when the stat-block one is detectably truncated
    — MM multi-column comparison blocks (Ogre, Pit Fiend) split a value across
    columns, leaving unbalanced parentheses or a dangling '/' or 'or'. Both are
    runtime-sourced (MM HTM / MONSTER.DAT); nothing is hand-typed."""
    sb = (sb_dmg or '').strip()
    dat = (dat_dmg or '').strip()
    truncated = bool(sb) and (sb.count('(') != sb.count(')')
                              or re.search(r'(?:/|\bor)\s*$', sb))
    if sb and not truncated:
        return sb
    return dat or sb


def _natural_attack_formulas(damage_str):
    """Split a MM DAMAGE/ATTACK string into per-attack dice formulas. Takes the
    part before 'or' ("1-6/1-6/2-8 or by weapon" → the three claw/bite dice),
    strips parenthetical qualifiers, and converts each range to dice. A
    component that can't be converted (e.g. 'by weapon') yields '' so the action
    is an attack roll only — we never fabricate a damage value."""
    if not damage_str:
        return []
    main = re.split(r'\bor\b', damage_str, maxsplit=1)[0]
    out = []
    for comp in main.split('/'):
        core = re.sub(r'\s*\(.*$', '', comp).strip()
        if core:
            out.append(_range_to_dice(core) or '')
    return out


def _build_natural_weaponry(damage_str):
    """Build a 'Natural Weaponry' action group (melee attack + damage actions)
    from the MM damage string, or None when there's nothing usable. Lets a
    migrated monster roll an attack out of the box (e.g. the Ogre)."""
    # Per-action icons match OSRIC (an empty img renders as a broken link in the
    # action list): melee → core d20-highlight, damage → the ARS damage glyph.
    formulas = _natural_attack_formulas(damage_str)
    actions = [_make_action('Attack', type_='melee', targeting='single',
                            img='icons/svg/d20-highlight.svg')]
    for f in formulas[:4]:
        if f:
            actions.append(_make_action('Damage', type_='damage',
                                        targeting='single', formula=f,
                                        img='systems/ars/icons/general/DamageColor.png'))
    # No damage components parsed → still give a bare attack roll.
    return _make_action_group('Natural Weaponry',
                              'icons/skills/melee/unarmed-punch-fist-white.webp',
                              actions)


# 5 CLASS.DAT save columns → the 10 ARS save keys + display labels.
_SAVE_COL_MAP = [
    (0, [('paralyzation', 'Paralyzation'), ('poison', 'Poison'), ('death', 'Death')]),
    (1, [('rod', 'Rod'), ('staff', 'Staff'), ('wand', 'Wand')]),
    (2, [('petrification', 'Petrification'), ('polymorph', 'Polymorph')]),
    (3, [('breath', 'Breath')]),
    (4, [('spell', 'Spell')]),
]


def _monster_saves(save_table, hit_dice):
    """Build the `system.saves` block for a monster from a CLASS.DAT save table
    (the fighter table — ARS variant 2 saves NPCs as fighters by HD). Returns
    None when no table is available. All values are sourced from CLASS.DAT at
    runtime; nothing is hardcoded."""
    if not save_table or len(save_table) < 2:
        return None
    lvl = max(1, min(int(hit_dice or 1), len(save_table) - 1))
    row = save_table[lvl]
    out = {}
    for col, keys in _SAVE_COL_MAP:
        for key, label in keys:
            out[key] = {"value": row[col], "label": label, "base": 0}
    return out


def make_monster_actor(monster, img_path=None, categories=None, fighter_saves=None,
                       embed_index=None):
    """Build an ARS `npc` Actor from a parsed MONSTER.DAT record. AC/THAC0/HD/XP
    come from the DAT; the MM stat-block table (parsed from the matched .HTM
    biography by _parse_mm_statblock) fills #attacks/morale/damage/special
    atk-def/size; saves are derived from the CLASS.DAT fighter table at level=HD
    (`fighter_saves`); `categories` are broad taxonomy tags appended to
    details.type for cross-actor type-trigger effects; a Natural Weaponry
    actionGroup makes the monster click-to-attack. `embed_index` maps normalized
    monster names to (journal_id, page_id) pairs so biography.value uses @embed
    instead of the raw HTML when a matching MM journal page exists."""
    actor_id = make_id()
    img = img_path or "icons/svg/mystery-man.svg"
    align_text = monster.get('alignment', '')
    align_code = _ALIGN_MAP.get(align_text, 'n')
    # Look up the MM HTML — try full name (minus HD suffix) then genus
    # "Dragon, Cloud, wyrm" → try "Dragon, Cloud, wyrm", "Dragon, Cloud", "Dragon"
    biography = ''
    name = monster['name']
    candidates = []
    # Remove HD suffix like ", 3 Hit Dice" or ", 8HD"
    base = re.sub(r',\s*\d+\s*(?:Hit\s*Dice|HD).*$', '', name).strip()
    candidates.append(base)
    # Variant qualifier = the record name past its genus ('Scorpion, Huge' →
    # 'Huge', 'Cat, Great, Leopard' → 'Great, Leopard'). Used to pick this
    # variant's column out of a multi-form MM comparison stat block (issue #16).
    variant_key = base.split(',', 1)[1].strip() if ',' in base else ''
    # Try progressively shorter prefixes
    parts = [p.strip() for p in base.split(',')]
    while len(parts) > 1:
        parts.pop()
        candidates.append(', '.join(parts))
    # Finally try the bare genus
    if monster.get('display_name') and monster['display_name'] not in candidates:
        candidates.append(monster['display_name'])
    for cand in candidates:
        biography = lookup_html_description(cand, _MONSTER_HTML_BOOKS)
        if biography: break
    # Parse the MM stat-block table once. It's the authoritative, structured
    # source for the 2e monster-block fields (#attacks, morale, damage, special
    # attacks/defenses, …) — far cleaner than the DAT "tail strings" heuristic.
    sb = _parse_mm_statblock(biography, variant_key)
    # Resolve biography display value: prefer an @embed reference to the MM
    # journal page (already migrated in Phase 2) so the NPC sheet shows the
    # formatted rulebook page. Fall back to the raw HTML when no matching page
    # is found (e.g. monsters only in supplemental books, not MM proper).
    bio_display = biography   # default: raw HTML
    if embed_index and biography:
        for cand in candidates:
            norm = cand.strip().lower()
            entry = embed_index.get(norm)
            if entry:
                jid, pid = entry
                bio_display = (
                    f'<p>@embed[Compendium.{MODULE_ID}.adnd2-journals'
                    f'.JournalEntry.{jid}.JournalEntryPage.{pid}]{{ }}</p>'
                )
                break
    # Damage string: labeled MM stat-block value, with the DAT value as the
    # fallback for multi-column blocks where the stat-block cell is truncated.
    # Reused for system.damage and the Natural Weaponry action group.
    damage_src = _pick_damage(monster.get('damage'), sb.get('DAMAGE/ATTACK', ''))
    # Build attributes from MONSTER.DAT-extracted fields. Each numeric value
    # is only set when MONSTER.DAT actually yielded one; no hand-typed defaults
    # (AC 10, THAC0 20, MV 12, HD 1, etc.) are substituted.
    attributes: dict[str, Any] = {
        "hp":   {"value": 0, "min": 0, "max": 0, "temp": 0, "tempmax": 0, "base": 0},
        "init": {"value": 0, "modifier": 0},
    }
    if monster.get('ac') is not None:        attributes["ac"]     = {"value": monster['ac']}
    if monster.get('thaco') is not None:     attributes["thaco"]  = {"value": monster['thaco']}
    if monster.get('mv') is not None:        attributes["movement"] = {"value": monster['mv'], "unit": "yd", "text": ""}
    if monster.get('hit_dice') is not None:  attributes["hitDice"] = monster['hit_dice']
    # Size — sourced from the MM "SIZE:" stat line (absent from MONSTER.DAT).
    size = _size_from_statblock(biography, sb)
    if size:
        attributes["size"] = size

    # `details.type` = the monster's own genus followed by any broader category
    # tags (Goblinoid, Giant, Undead, …) so cross-actor type triggers fire.
    # Skip a category already present as a comma-token of an earlier entry.
    # ARS matches a trigger against details.type by splitting on COMMAS and
    # comparing tokens for EXACT equality (lowercased) — so a parenthetical
    # qualifier left on the genus ("Goblin (lair)") would defeat a "goblin"
    # trigger. Strip parentheticals to keep the genus token clean and matchable.
    type_tokens = []
    seen_types = set()
    genus = monster.get('display_name', '') or monster.get('name', '')
    genus = re.sub(r'\s*\([^)]*\)', '', genus).strip().rstrip(',').strip()
    if genus:
        type_tokens.append(genus)
        for piece in genus.split(','):
            seen_types.add(piece.strip().lower())
    for c in (categories or []):
        cl = (c or '').strip().lower()
        if cl and cl not in seen_types:
            type_tokens.append(c)
            seen_types.add(cl)

    details = {
        "biography": {"value": bio_display, "public": ""},
        "type":      ", ".join(type_tokens),
        "source":    "Monstrous Manual",
        "alignment": align_code,
    }
    system = {
        "alias": monster.get('display_name', monster['name']),
        # No ability score block: MONSTER.DAT doesn't carry per-monster STR/DEX/etc.
        # values, and "ability 10 = average" is itself a PHB rule. Foundry will
        # apply its own placeholder defaults; we don't claim any here.
        "attributes": attributes,
        "details":    details,
        # NPCs save as fighters by HD in ARS Variant 2 (matrixTable default
        # "fighter"); set it explicitly so THAC0/save derivation is consistent.
        "matrixTable": "fighter",
        "organization":  monster.get('organization', ''),
        "activity":      monster.get('activity_cycle', ''),
        "diet":          monster.get('diet', ''),
        "intelligence":  monster.get('intelligence', ''),
        "climate":       monster.get('climate_terrain', ''),
        # 2e monster-block fields read straight from the structured MM stat-block
        # table (label→value rows) rather than the fragile DAT tail strings, which
        # mis-sliced these (e.g. Orc specialDefenses came out as "1-8 (weapon); 1"
        # instead of "Nil"). DAT num_attacks remains a fallback when no stat block.
        "numberAttacks":   (sb.get('NO. OF ATTACKS', '')
                            or (str(monster['num_attacks']) if monster.get('num_attacks') else '')),
        "damage":          damage_src,
        "morale":          sb.get('MORALE', ''),
        "specialAttacks":  sb.get('SPECIAL ATTACKS', ''),
        "specialDefenses": sb.get('SPECIAL DEFENSES', ''),
        "magicresist":     sb.get('MAGIC RESISTANCE', ''),
        "frequency":       sb.get('FREQUENCY', ''),
        "treasureType":    sb.get('TREASURE', ''),
    }
    # Hit dice as the top-level string ARS reads for effectiveLevel / HP rolling.
    if monster.get('hit_dice') is not None:
        system["hitdice"] = str(monster['hit_dice'])
    # Saving throws — resolved from the CLASS.DAT fighter table at level = HD
    # (Option B). Sourced at runtime, never hardcoded.
    saves = _monster_saves(fighter_saves, monster.get('hit_dice'))
    if saves:
        system["saves"] = saves
    # "Number Appearing" — DAT range first (structured), MM stat line as fallback.
    if monster.get('noapp_low') is not None and monster.get('noapp_high') is not None:
        system["numberAppearing"] = f"{monster['noapp_low']}-{monster['noapp_high']}"
    elif sb.get('NO. APPEARING'):
        system["numberAppearing"] = sb['NO. APPEARING']
    # XP — the MONSTER.DAT value, as the {value} object ARS reads. (DAT-sourced;
    # not the OSRIC hp-scaling formula, which would invent a number.)
    if monster.get('xp') is not None:
        system["xp"] = {"value": str(monster['xp'])}
    # Natural Weaponry — a melee attack (+ damage actions parsed from the MM
    # damage string) so the monster can roll out of the box. Only when there's a
    # damage string to work from; we never fabricate an empty attack.
    system["actionGroups"] = [_build_natural_weaponry(damage_src)] if damage_src else []

    return {
        "_id": actor_id,
        "name": monster['name'],
        "type": "npc",
        "img": img,
        "system": system,
        "prototypeToken": {
            "name": monster['name'],
            "texture": {"src": img},
        },
        "items": [], "effects": [], "flags": {"adnd2": {}},
        "folder": None, "sort": 0, "ownership": {"default": -1}, "_stats": _stats_block(),
    }


# ─── Phase 3 migration drivers ────────────────────────────────────────────────

class _JsonPack:
    """Drop-in replacement for a plyvel DB used by migrate2.py.

    Accumulates key→value entries in memory (same put/close interface as plyvel).
    On close(), restructures embedded sub-documents (pages into journal entries,
    effects into items/actors, results into roll tables) and writes one JSON file
    per top-level document into *src_dir* for fvtt-cli to consume.
    """

    def __init__(self, pack_name, src_dir):
        self._pack_name = pack_name
        self._src_dir   = src_dir
        self._entries   = {}   # key_str -> value_str

    def put(self, key, value):
        k = key.decode('utf-8')   if isinstance(key,   bytes) else key
        v = value.decode('utf-8') if isinstance(value, bytes) else value
        self._entries[k] = v

    def close(self):
        # top: doc_id -> (collection, doc dict)
        top      = {}
        # children: (parent_col, parent_id) -> {child_array: [child_doc]}
        children = {}

        for key, val_str in self._entries.items():
            doc = json.loads(val_str)
            # Key format: "!collection!id" or "!parent.child!parentId.childId"
            raw        = key.lstrip('!')
            collection, _, ids = raw.partition('!')
            if not ids:
                continue

            if '.' in collection:
                # Embedded sub-document: "items.effects", "journal.pages", etc.
                parent_col, child_array = collection.split('.', 1)
                parent_id, _, child_id = ids.partition('.')
                if not doc.get('_id'):
                    doc['_id'] = child_id
                # fvtt-cli needs _key on sub-docs to write them as separate LevelDB entries
                doc['_key'] = key
                children.setdefault((parent_col, parent_id), {}).setdefault(child_array, []).append(doc)
            else:
                top[ids] = (collection, doc)

        # Embed children into their parent documents.
        # First clear any arrays that contain legacy string IDs (not dict objects):
        # fvtt-cli requires arrays to contain full sub-doc objects (with _key), not
        # bare ID strings. Items written with effects=[id1, id2] get cleared here;
        # the actual effect objects (from !items.effects! entries) are re-embedded below.
        _EMBEDDED_ARRAYS = ('effects', 'pages', 'results', 'items')
        for doc_id, (collection, doc) in top.items():
            for arr in _EMBEDDED_ARRAYS:
                val = doc.get(arr)
                if isinstance(val, list) and any(not isinstance(e, dict) for e in val):
                    doc[arr] = []

        for (parent_col, parent_id), arrays in children.items():
            if parent_id not in top:
                continue
            _, parent_doc = top[parent_id]
            for array_name, child_list in arrays.items():
                parent_doc[array_name] = child_list

        # Write one JSON file per top-level document; _key is required by fvtt-cli
        for doc_id, (collection, doc) in top.items():
            doc['_key'] = f'!{collection}!{doc_id}'
            safe = re.sub(r'[^A-Za-z0-9_-]', '_', doc_id)
            filepath = os.path.join(self._src_dir, f'{safe}.json')
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)


def _open_pack(path):
    """Return a _JsonPack that stages documents as JSON files for fvtt-cli.
    The staging directory is wiped on open so each run is idempotent."""
    pack_name = os.path.basename(path)
    src_dir   = os.path.join(_PACK_SRC_BASE, pack_name)
    if os.path.exists(src_dir):
        shutil.rmtree(src_dir)
    os.makedirs(src_dir, exist_ok=True)
    return _JsonPack(pack_name, src_dir)


def _finalize_with_fvtt_cli():
    """Convert every staged JSON directory into a LevelDB pack via fvtt-cli.

    Requires fvtt-cli installed globally:
        npm install -g @foundryvtt/foundryvtt-cli
    or set _FVTT_CLI_CMD = 'npx @foundryvtt/foundryvtt-cli' above.
    """
    print("\n=== Packing LevelDB with fvtt-cli ===")
    if not os.path.exists(_PACK_SRC_BASE):
        print("  No staged packs found — nothing to pack.")
        return

    # fvtt-cli creates a subdirectory named after -n inside --out.
    # So --out must be the packs PARENT and -n the pack directory name.
    packs_dir = os.path.dirname(OUTPUT_DB)   # adnd2-compendium/packs/
    os.makedirs(packs_dir, exist_ok=True)

    ok = fail = 0
    for pack_name in sorted(os.listdir(_PACK_SRC_BASE)):
        src = os.path.join(_PACK_SRC_BASE, pack_name)
        if not os.path.isdir(src):
            continue
        # Remove existing pack dir so fvtt-cli starts clean
        existing = os.path.join(packs_dir, pack_name)
        if os.path.exists(existing):
            shutil.rmtree(existing)

        # --out = parent dir; fvtt-cli writes to parent/pack_name/.
        # Use abspath so fvtt-cli (Node.js) receives OS-native absolute paths;
        # on Windows this avoids forward slashes being misread as cmd.exe flags.
        src_abs   = os.path.abspath(src)
        packs_abs = os.path.abspath(packs_dir)
        # _FVTT_CLI_CMD may be a string ('fvtt') or a list (['npx', '@foundryvtt/foundryvtt-cli'])
        cli = _FVTT_CLI_CMD if isinstance(_FVTT_CLI_CMD, list) else [_FVTT_CLI_CMD]
        cmd = cli + ['package', 'pack', '-n', pack_name,
                     '--in', src_abs, '--out', packs_abs]
        # On Windows npm-global commands are .cmd files and require shell=True to be
        # found by CreateProcess. encoding= avoids cp1252 decoding errors on non-ASCII output.
        result = subprocess.run(cmd, capture_output=True, text=True,
                                shell=(sys.platform == 'win32'),
                                encoding='utf-8', errors='replace')
        n      = len([f for f in os.listdir(src) if f.endswith('.json')])
        if result.returncode == 0:
            print(f"  ✓ {pack_name} ({n} docs)")
            ok += 1
        else:
            msg = (result.stderr or result.stdout).strip()
            print(f"  ✗ {pack_name}: {msg}")
            fail += 1

    print(f"  → {ok} packs OK, {fail} failed")
    if fail == 0:
        shutil.rmtree(_PACK_SRC_BASE)   # clean up staging dir on full success


def _race_portrait_for(race):
    """Return relative module path to a portrait image, using the DAT-extracted
    BMP list first, then sub-race → base-race fallback heuristics."""
    img = None
    for bmp in race.get('portrait_bmps', []):
        img = extract_portrait(bmp, OUTPUT_IMG_PORTRAITS)
        if img: break
    if img: return img
    cleaned = re.sub(r'\([^)]*\)', '', race['name']).strip()
    tokens = re.split(r'[\s,\-]+', cleaned)
    base_words = {'DWARF': 'DWARF', 'ELF': 'ELF', 'GNOME': 'GNOME',
                  'HALFLING': 'HAFLING', 'HUMAN': 'MALE', 'ORC': 'ORC'}
    guesses = []
    for tok in reversed(tokens):
        key = tok.upper()
        if key in base_words:
            stem = base_words[key]
            guesses.extend([f"{stem}1.BMP", f"{stem}.BMP"])
            break
    QUALIFIERS = {'STANDARD', 'HILL', 'MOUNTAIN', 'DEEP', 'HIGH', 'GRAY', 'DARK',
                  'SYLVAN', 'WOOD', 'AQUATIC', 'SEA', 'FOREST', 'ROCK',
                  'HAIRFOOT', 'STOUT', 'TALLFELLOW'}
    stripped = [t for t in tokens if t and t.upper() not in QUALIFIERS]
    joined = ''.join(t.capitalize() for t in stripped).strip()
    if joined.startswith('Half') and len(joined) > 4:
        guesses.append(f"{joined[:8]}.BMP")
    if tokens:
        guesses.append(f"{tokens[0].upper()}1.BMP")
    for guess in guesses:
        img = extract_portrait(guess, OUTPUT_IMG_PORTRAITS)
        if img: return img
    return None


def _race_skill_mods(race, all_by_name):
    """Build the `system.attributes.skillmods` array for a race item.
    DAT-first: read 13 int32 from `race['thief_skill_adjustments']` (set by
    parse_race_record) and zip with skill names from SP00074. Skip cells
    whose value is 0 (no adjustment) — matches osric-compendium's
    convention of only emitting non-zero rows.

    For sub-races whose own DAT bytes are all zeros, inherit from the
    base lineage (Dwarf/Elf/Gnome/Halfling/Half-elf/Half-orc/Human)."""
    vals = race.get('thief_skill_adjustments')
    if vals is None or all(v == 0 for v in vals):
        base = _resolve_base_race(race['name'])
        if base and base != race['name']:
            parent = all_by_name.get(base)
            if parent:
                vals = parent.get('thief_skill_adjustments')
    if not vals: return []
    names = _load_thief_skill_names()
    if not names or len(names) != len(vals):
        return []
    out = []
    for nm, v in zip(names, vals):
        if v == 0: continue
        out.append({'name': nm, 'value': v})
    return out


def _race_size_category(race, all_by_name):
    """Derive Foundry size category ('tiny'|'small'|'medium'|'large'|'huge')
    from RACE.DAT's max_male_height (in inches). The thresholds are chosen
    so that the user-confirmed anchors hold: dwarf/halfling/gnome = small,
    elf/half-elf/half-orc/human = medium, ogre = large.

    For records where max_male_height is missing (0) — Half-orc, Half-ogre,
    Half-Stout Halfling, Centaur, Alaghi — we fall back to the height of
    the closest "ancestor" race whose name token appears in this race's
    name (e.g. 'Half-orc' → 'Orc'; 'Half-Stout Halfling' → 'Halfling').
    Records with no usable height after fallback get None (caller decides
    the default)."""
    h = race.get('max_male_height') or 0
    if h <= 0:
        # Fallback: look at every other race whose name token appears in ours
        my_lower = race['name'].lower()
        candidates = []
        for other_name, other in all_by_name.items():
            if other_name == race['name']: continue
            if (other.get('max_male_height') or 0) <= 0: continue
            ol = other_name.lower()
            if ol in my_lower:
                candidates.append(other['max_male_height'])
        if candidates:
            h = max(candidates)   # use the bulkier ancestor
    if h <= 0:
        return None
    if h <= 54:   return 'small'
    if h >= 96:   return 'large'
    return 'medium'


def _race_folder_bucket(race_name):
    """Group a race into one of the sub-folders: 'Standard Races', 'Dwarven Subraces',
    'Elven Subraces', 'Gnomish Subraces', 'Halfling Subraces', 'Humanoid Races'."""
    base = _resolve_base_race(race_name) or ''
    DEMI_BASES = {'Dwarf','Elf','Gnome','Halfling','Half-elf','Half-orc','Half-ogre','Human'}
    if race_name in DEMI_BASES:
        return 'Standard Races'
    if base == 'Dwarf':    return 'Dwarven Subraces'
    if base == 'Elf':      return 'Elven Subraces'
    if base == 'Gnome':    return 'Gnomish Subraces'
    if base == 'Halfling': return 'Halfling Subraces'
    if base in ('Half-elf','Half-orc','Half-ogre','Human'):
        return 'Standard Races'   # "Standard half-*" variants go with their base
    return 'Humanoid Races'


def _vision_effect_change(range_ft):
    """Build the ActiveEffect change that drives a token's vision range
    (used by both Infravision and Normal Vision abilities). Per OSRIC
    2026.05.20, `special.vision` value is a JSON OBJECT (not a stringified
    blob). Phase 'initial' matches OSRIC's universal convention."""
    return {
        "key":   "special.vision",
        "type":  "custom",
        "value": {"range": range_ft, "angle": "360", "mode": "basic"},
        "priority": 20,
        "phase":    "initial",
    }


def _race_abilities_for(race):
    """Compute the ordered list of (label, icon, changes) tuples for a race.
    `changes` is the ActiveEffect changes array (or None for descriptive-only
    abilities). The label is used for cross-race deduplication.

    Two osric-compendium patterns mirrored here:
      1. Each Infravision ability carries a `special.vision` effect that
         drives the token's vision range; races without infravision get a
         "Vision, Normal" ability with range 0 (so the token gets a
         sensible default rather than no vision configuration at all).
      2. Movement is emitted as feet/round (the DAT value × 10), matching
         the AD&D 2e PHB convention where "MV 12" means 120 ft/round."""
    runtime  = extract_race_runtime_data(race['name'])
    infravision_ft       = runtime['infravision_ft']
    extracted_abilities  = runtime['abilities']

    out = []
    seen_labels = set()
    def _push(label, icon, changes=None):
        if not label or label in seen_labels: return
        seen_labels.add(label)
        out.append((label, icon, changes))

    # NOTE: the baseline traits every race carries (ability-score modifiers +
    # movement) are NOT emitted here — they go DIRECTLY on the race document
    # via _race_direct_effect_specs(). Abilities surfaced here are the *special*
    # racial traits a player should see as named items.

    has_infravision = False
    for lab in extracted_abilities:
        inf = _ability_cell_to_infravision_ft(lab)
        if inf is not None:
            _push(f"Infravision ({inf} ft)", _FOUNDRY_ICON_INFRAVISION,
                  [_vision_effect_change(inf)])
            has_infravision = True
        elif lab.lower() == 'infravision' and infravision_ft is not None:
            _push(f"Infravision ({infravision_ft} ft)", _FOUNDRY_ICON_INFRAVISION,
                  [_vision_effect_change(infravision_ft)])
            has_infravision = True
        else:
            _push(lab, pick_race_ability_icon(lab))

    if not has_infravision:
        _push("Vision, Normal", _FOUNDRY_ICON_NORMALVISION,
              [_vision_effect_change(0)])

    return out


def _race_direct_effect_specs(race):
    """Baseline traits every race carries — ability-score modifiers and base
    movement — placed DIRECTLY on the race document (not surfaced as named
    ability items). Returns [(label, icon, changes), ...]. Per design: direct
    = the universal stat package; abilities = what is *special* about a race."""
    out = []
    stat_changes = make_race_stat_mod_changes(race.get('ability_adjustments'))
    if stat_changes:
        out.append((f"{race['name']} Racial Ability Modifiers",
                    _FOUNDRY_ICON_RACEMOD, stat_changes))
    movement = race.get('movement')
    if movement is not None:
        movement_ft = movement * 10     # PHB "MV 12" → 120 ft/round
        out.append((f"Base Movement {movement_ft}",
                    pick_race_ability_icon("Base Movement"),
                    [{"key": "system.mods.movement.base", "type": "override",
                      "value": str(movement_ft), "priority": 20,
                      "phase": "initial"}]))
    return out


# A racial weapon attack bonus reads as "+N ... attack ... with <weapon>"
# (e.g. dwarf/gnome "+1 to attack rolls with darts"). Implemented as OSRIC does:
# a `proficiency` item whose system.hit carries the bonus, added to the race
# itemList — NOT a flat actor effect (which would hit every weapon).
# The weapon clause must follow the attack word closely (bounded gap): this
# matches "+1 to attack rolls with darts" but NOT charge-style prose like
# "+2 bonus to attack and inflicting double damage with an impaling weapon".
_WEAPON_ATTACK_BONUS_RE = re.compile(
    r'\+\s*(\d+)\b[^.]{0,30}?\b(?:attacks?|to\s+hit|missile)\b[^.]{0,12}?\bwith\s+'
    r'([^.]+?)(?:\.|,|—|$)', re.I)


def _weapon_attack_bonus_from_label(label):
    """(bonus, weapon_name) parsed from a racial ability's description when it
    is a weapon attack bonus, else None. Weapon-, bonus-, and label-agnostic:
    all values come from the user's source text at runtime."""
    desc = _ability_description(label)
    if not desc:
        return None
    m = _WEAPON_ATTACK_BONUS_RE.search(desc)
    if not m:
        return None
    weapon = re.sub(r'\s+', ' ', m.group(2)).strip().rstrip('.').strip()
    weapon = re.split(r',|\btheir\b|\bas\b|\bwhich\b', weapon)[0].strip()
    if not weapon:
        return None
    return m.group(1), weapon


def _make_race_weapon_prof_item(name, hit, description, icon):
    """Minimal `proficiency` item carrying a racial weapon attack bonus in
    system.hit — mirrors OSRIC's "H: Bow Attack Bonus" shape (appliedto empty,
    proficiencies.cost 0)."""
    return {
        "_id": make_id(), "name": name, "type": "proficiency", "img": icon,
        "effects": [],
        "system": {
            "description": description, "dmonlytext": "", "itemList": [],
            "appliedto": [], "cost": 0,
            "hit": str(hit), "damage": "0", "speed": 0, "attacks": "",
            "migrate": False,
            "attributes": {"rarity": "", "type": "", "subtype": "",
                           "magic": False, "properties": [], "skillmods": [],
                           "conditionals": [], "identified": True,
                           "infiniteammo": False, "size": "medium",
                           "material": "leather_book"},
            "charges": {"value": 0, "min": 0, "max": 0, "reuse": "none"},
            "location": {"state": "carried", "parent": ""},
            "resource": {"itemId": ""},
            "quantity": 0, "weight": 0, "source": "", "xp": 0,
            "actions": [], "actionGroups": [],
            "proficiencies": {"cost": 0},
            "rank": {"levels": {"max": 0, "arcane": 0, "divine": 0}},
        },
        "folder": None, "sort": 0, "ownership": {"default": 0},
        "flags": {}, "_stats": _stats_block(),
    }


# ─── Skill spec generator + per-race skill enumeration ───────────────────────

# Three strength-test skills, lifted from osric-compendium under the OGL
# (originally items uTXqi14JXe8jJQpB, o3sdrCvutrAzanKp, TyLafBBvWnCmy3pf).
# They use ARS's @abilities.str.* engine references so the per-character
# STR table values come from the actor at roll time — no rules data
# embedded here.
_UNIVERSAL_STR_TEST_SKILLS = [
    {
        'name': 'Minor STR Test (Open Door)',
        'formula': '@abilities.str.open.0',
        'target':  '@abilities.str.open.1',
        'type':    'decending', 'groups': '',
        'icon':    'icons/magic/control/buff-strength-muscle-damage.webp',
        'description': "<p>This is used to determine success on minor "
                       "feats of strength such as forcing a dungeon door "
                       "that doesn't open easily (this is the average "
                       "dungeon door).</p>",
    },
    {
        'name': 'Minor STR Test (Open Held Door)',
        'formula': '@abilities.str.open.2',
        'target':  '@abilities.str.open.3',
        'type':    'decending', 'groups': '',
        'icon':    'icons/magic/control/buff-strength-muscle-damage-orange.webp',
        'description': "<p>This is used to attempt an extraordinary success "
                       "on minor feats of strength such as forcing a dungeon "
                       "door enspelled by minor magics such as "
                       "<em>Hold Portal</em> or <em>Wizard Lock.</em></p>",
    },
    {
        'name': 'Major STR Test (Bend Bars / Lift Gates)',
        'formula': '1d100',
        'target':  '@abilities.str.bendbars',
        'type':    'decending', 'groups': '',
        'icon':    'icons/environment/wilderness/tomb-entrance.webp',
        'description': '',
    },
]


def _race_skill_specs(race):
    """Return list of skill-spec dicts to attach to this race via itemList.
    Each spec: {name, formula, target, type, groups, description, icon}.
    All skills are pooled by (name, formula, target) so identical specs
    across races share a single skill item — racial differences in
    Detect Noise are routed through the race's `system.attributes.skillmods`
    rather than per-race target values."""
    out = []
    base = _resolve_base_race(race['name']) or race['name']

    # 1. Universal: Unskilled Climbing (PHB Table 65)
    pct = _load_phb_unskilled_climbing_pct() or 0
    if pct:
        out.append({
            'name': 'Unskilled Climbing', 'formula': '1d100',
            'target': pct, 'type': 'decending', 'groups': '',
            'description': '', 'icon': _FOUNDRY_ICON_SKILL_CLIMB,
        })

    # 2. Detect Noise — single shared skill at the thief L1 base score
    # (15% per SP00073). Per-race deltas come from the race's skillmods
    # which the engine routes into features.modifiers.race at roll time.
    base_pct = _load_sp_thief_base_scores().get('detect noise', 15)
    out.append({
        'name': 'Detect Noise', 'formula': '1d100',
        'target': base_pct, 'type': 'decending', 'groups': '',
        'description': '', 'icon': _FOUNDRY_ICON_SKILL_LISTEN,
    })

    # 2b. The two Minor STR Test skills — universal for every race; the
    # engine resolves target/formula from the actor's STR sub-attributes.
    for spec in _UNIVERSAL_STR_TEST_SKILLS:
        out.append(dict(spec))

    # 3. Lineage-specific Mining Detection sub-skills
    for (sub_name, succ, die) in _parse_lineage_detection_subskills(base):
        out.append({
            'name': f'{base}: {sub_name}', 'formula': f'1d{die}',
            'target': succ, 'type': 'decending', 'groups': '',
            'description': '', 'icon': _FOUNDRY_ICON_SKILL_MINING,
        })

    # 4. Secret doors: elf/half-elf/halfling get the elf-style detection
    #    skills; every other lineage gets the universal "Detect Secret Doors".
    #    The die and success target for each are parsed from the PHB Elf entry
    #    at runtime (the three 'roll a … on 1dN' chances, in source order) —
    #    never hardcoded. If the source can't be read, no skill is fabricated.
    chances = _parse_secret_door_chances()
    if chances:
        def _door(name, die, target):
            return {'name': name, 'formula': f'1d{die}', 'target': target,
                    'type': 'decending', 'groups': '', 'description': '',
                    'icon': _FOUNDRY_ICON_SKILL_SECRET}
        if base in ('Elf', 'Half-elf', 'Halfling'):
            labels = ['Passing Secret Doors', 'Searching Secret Doors',
                      'Searching Concealed Doors']
            for lab, (die, target) in zip(labels, chances):
                out.append(_door(f'{base}: {lab}', die, target))
        else:
            die, target = chances[0]      # universal = the 'passing' chance
            out.append(_door('Detect Secret Doors', die, target))

    return out


def migrate_races():
    """Phase 3: write the races pack — a `race` Item per RACE.DAT record (in
    PHB/Complete-book sub-folders), with child ability/skill sub-items and the
    dwarf/gnome racial combat-bonus ActiveEffects. Returns the race count."""
    print("\n=== Races (RACE.DAT) ===")
    races = parse_races()
    if not races:
        print("  No races parsed."); return 0
    db = _open_pack(OUTPUT_PACKS['races'])
    os.makedirs(OUTPUT_IMG_PORTRAITS, exist_ok=True)
    races_by_name = {r['name']: r for r in races}    # for size lineage fallback
    # Taxonomy member sets, used to collapse overlapping combat-bonus triggers
    # (so an ogre tagged "Ogre, Giant" doesn't double the dwarf's defensive -4).
    taxonomy_members = _build_taxonomy_category_members()

    # ── 1. Build folder hierarchy ──────────────────────────────────────────
    folders = {}
    def _folder(name, parent=None, sort=0):
        fid = make_id()
        folders[name] = make_compendium_folder(fid, name, 'Item', sort=sort, parent=parent)
        return fid

    races_root  = _folder('Races',           sort=100000)
    abilities_root = _folder('Racial Abilities', sort=200000)

    bucket_ids = {
        'Standard Races':        _folder('Standard Races',        parent=races_root, sort=100000),
        'Dwarven Subraces':  _folder('Dwarven Subraces',  parent=races_root, sort=200000),
        'Elven Subraces':    _folder('Elven Subraces',    parent=races_root, sort=300000),
        'Gnomish Subraces':  _folder('Gnomish Subraces',  parent=races_root, sort=400000),
        'Halfling Subraces': _folder('Halfling Subraces', parent=races_root, sort=500000),
        'Humanoid Races':    _folder('Humanoid Races',    parent=races_root, sort=600000),
    }
    shared_abilities_folder = _folder('Shared',         parent=abilities_root, sort=100000)
    per_race_abilities_folder = _folder('Per-Race',     parent=abilities_root, sort=200000)
    skills_root             = _folder('Racial Skills',                       sort=300000)

    # ── 2. First pass: enumerate every race's ability tuples ───────────────
    # Sharing is keyed on a CASE-INSENSITIVE label so sub-race tables that spell
    # the same ability differently (e.g. "Secret Doors" vs "Secret doors") share
    # ONE doc instead of minting duplicates. `norm_display` keeps the nicest
    # (most-capitalised) spelling as the canonical name.
    def _ability_key(lab):
        return ' '.join(lab.strip().lower().split())
    race_abilities = []   # [(race_dict, [(label, icon, changes), ...])]
    label_count    = {}   # normalized-label → number of races emitting it
    norm_display   = {}   # normalized-label → canonical display name
    for race in races:
        abs_ = _race_abilities_for(race)
        race_abilities.append((race, abs_))
        for (lab, _, __) in abs_:
            # Weapon attack bonuses become proficiency items (per race); the
            # descriptive "Melee combat …" label is merged into the single
            # per-race combat ability below — neither is a shared ability.
            if _weapon_attack_bonus_from_label(lab):
                continue
            if 'melee combat' in lab.lower():
                continue
            key = _ability_key(lab)
            label_count[key] = label_count.get(key, 0) + 1
            cur = norm_display.get(key)
            if cur is None or (sum(c.isupper() for c in lab)
                               > sum(c.isupper() for c in cur)):
                norm_display[key] = lab.strip()

    # ── 3. Second pass: mint shared ability docs once, per-race specifics inline ──
    shared_doc_id = {}   # label → ability_doc_id  (lookup table for sharing)
    shared_pool_specs = []  # (label, icon, changes) for the eventually-shared ones

    # Collect spec for each shared normalized-label using its first occurrence
    seen_shared = set()
    for _race, abs_ in race_abilities:
        for (lab, icon, chg) in abs_:
            key = _ability_key(lab)
            if label_count.get(key, 0) >= 2 and key not in seen_shared:
                seen_shared.add(key)
                shared_pool_specs.append((key, icon, chg))

    # Write the shared abilities (one Ability item per normalized label, Shared folder)
    action_groups_written = 0
    for (key, icon, chg) in shared_pool_specs:
        disp = norm_display[key]
        chg = chg or _ability_effect_changes(disp)
        acts = _ability_actions(disp)
        ab, ef = make_ability_item(disp, icon,
                                   description=_ability_description(disp),
                                   effect_changes=chg,
                                   action_groups=(acts or None))
        if acts: action_groups_written += len(acts)
        ab['folder'] = shared_abilities_folder
        shared_doc_id[key] = ab['_id']
        db.put(f'!items!{ab["_id"]}'.encode(), json.dumps(ab).encode())
        if ef:
            db.put(f'!items.effects!{ab["_id"]}.{ef["_id"]}'.encode(),
                   json.dumps(ef).encode())

    # ── 3b. Skill enumeration + shared-pool dedup ──────────────────────────
    race_skill_specs = []      # [(race, [spec, ...]), ...]
    shared_skill_pool = {}     # (name, formula, target) → first spec seen
    for race in races:
        specs = _race_skill_specs(race)
        race_skill_specs.append((race, specs))
        for spec in specs:
            key = (spec['name'], spec['formula'], spec['target'])
            shared_skill_pool.setdefault(key, spec)
    skill_id_by_key = {}       # (name, formula, target) → skill_doc_id
    for key, spec in shared_skill_pool.items():
        sk = make_skill_item(spec['name'], spec['icon'], spec['formula'],
                             spec['target'], type_=spec['type'],
                             groups=spec['groups'],
                             description=spec['description'])
        sk['folder'] = skills_root
        skill_id_by_key[key] = sk['_id']
        db.put(f'!items!{sk["_id"]}'.encode(), json.dumps(sk).encode())
    skills_written = len(skill_id_by_key)

    # ── 4. Third pass: write races + per-race-specific abilities ───────────
    no_desc = 0
    unique_abilities_written = 0
    effects_written = len([s for s in shared_pool_specs if s[2]])  # shared effects already counted

    # Resolve base-lineage portraits first so sub-races can inherit them
    # uniformly (overrides any sub-race-specific BMP from RACE.DAT). Map
    # key is the resolved base name (e.g. "Dwarf"); value is the portrait path.
    base_lineage_img = {}
    for race, _abs in race_abilities:
        base = _resolve_base_race(race['name'])
        if base and base.lower() == race['name'].strip().lower():
            base_lineage_img[base] = _race_portrait_for(race)

    race_skill_specs_by_name = {race['name']: specs for race, specs in race_skill_specs}
    written_race_docs = {}        # name → written race_item (for CP-copy reuse)
    for race, abs_ in race_abilities:
        base = _resolve_base_race(race['name'])
        if base and base.lower() != race['name'].strip().lower() \
                and base_lineage_img.get(base):
            img = base_lineage_img[base]    # sub-race → inherit base icon
        else:
            img = _race_portrait_for(race)
        description = _race_html_description(race['name'])
        if not description: no_desc += 1

        race_id = make_id()
        bucket  = _race_folder_bucket(race['name'])
        bucket_folder = bucket_ids[bucket]

        item_list_refs = []
        # Weapon attack bonuses → proficiency items (system.hit), OSRIC-style.
        for (lab, icon, chg) in abs_:
            wb = _weapon_attack_bonus_from_label(lab)
            if not wb:
                continue
            bonus, weapon = wb
            prof = _make_race_weapon_prof_item(
                f'{weapon.title()} Attack Bonus', bonus,
                _ability_description(lab) or '', icon)
            prof['folder'] = per_race_abilities_folder
            db.put(f'!items!{prof["_id"]}'.encode(), json.dumps(prof).encode())
            unique_abilities_written += 1
            item_list_refs.append({
                "id": prof['_id'], "uuid": f"Item.{prof['_id']}",
                "sourceuuid": f"Compendium.{MODULE_ID}.adnd2-races.Item.{race_id}",
                "type": "proficiency", "name": prof['name'], "img": prof['img'],
                "level": "0",
            })

        for (lab, icon, chg) in abs_:
            if _weapon_attack_bonus_from_label(lab):
                continue   # already emitted as a proficiency item above
            if 'melee combat' in lab.lower():
                continue   # merged into the single combat ability below
            key = _ability_key(lab)
            disp = norm_display.get(key, lab.strip())
            if key in shared_doc_id:
                ab_id = shared_doc_id[key]
                ab_img = icon
            else:
                # Race-specific: mint a new Ability item now
                chg = chg or _ability_effect_changes(disp)
                acts = _ability_actions(disp)
                ab, ef = make_ability_item(disp, icon,
                                            description=_ability_description(disp),
                                            effect_changes=chg,
                                            parent_race_id=race_id,
                                            action_groups=(acts or None))
                if acts: action_groups_written += len(acts)
                ab['folder'] = per_race_abilities_folder
                db.put(f'!items!{ab["_id"]}'.encode(), json.dumps(ab).encode())
                if ef:
                    db.put(f'!items.effects!{ab["_id"]}.{ef["_id"]}'.encode(),
                           json.dumps(ef).encode())
                    effects_written += 1
                ab_id = ab['_id']
                ab_img = ab['img']
                unique_abilities_written += 1
            item_list_refs.append({
                "id":   ab_id,
                "uuid": f"Item.{ab_id}",
                "sourceuuid": f"Compendium.{MODULE_ID}.adnd2-races.Item.{race_id}",
                "type": "ability",
                "name": disp,
                "img":  ab_img,
                "level": "0",
            })

        # Append skill refs (shared pool — multiple races may target the
        # same skill id when they share an identical name/formula/target).
        for spec in race_skill_specs_by_name.get(race['name'], []):
            key = (spec['name'], spec['formula'], spec['target'])
            sk_id = skill_id_by_key.get(key)
            if not sk_id: continue
            item_list_refs.append({
                "id":   sk_id,
                "uuid": f"Item.{sk_id}",
                "sourceuuid": f"Compendium.{MODULE_ID}.adnd2-races.Item.{race_id}",
                "type": "skill",
                "name": spec['name'],
                "img":  spec['icon'],
                "level": "0",
            })

        # ── Combat bonuses (dwarf/gnome to-hit + AC vs giant-kin) → ONE
        # "Melee Combat Bonuses" ability carrying BOTH the S&P prose and the
        # mechanical changes (target.type for the to-hit bonus, attacker.type
        # for the AC bonus). This replaces the earlier redundant split of a
        # descriptive-only "Melee Combat Bonuses" + effect-only "Racial
        # Attack/Defensive Bonus" items. Magnitudes/triggers parsed at runtime.
        combat_changes = [
            c for cef in _build_race_combat_effect_docs(
                race['name'], race_id, category_members=taxonomy_members)
            for c in cef['system']['changes']]
        if combat_changes:
            melee_labels = [lab for (lab, _, _) in abs_
                            if 'melee combat' in lab.lower()]
            melee_desc = (_ability_description(melee_labels[0])
                          if melee_labels else '')
            ab, ef = make_ability_item(
                'Melee Combat Bonuses',
                pick_race_ability_icon('Melee Combat'),
                description=melee_desc, effect_changes=combat_changes,
                parent_race_id=race_id)
            ab['folder'] = per_race_abilities_folder
            db.put(f'!items!{ab["_id"]}'.encode(), json.dumps(ab).encode())
            if ef:
                db.put(f'!items.effects!{ab["_id"]}.{ef["_id"]}'.encode(),
                       json.dumps(ef).encode())
                effects_written += 1
            unique_abilities_written += 1
            item_list_refs.append({
                "id": ab['_id'], "uuid": f"Item.{ab['_id']}",
                "sourceuuid": f"Compendium.{MODULE_ID}.adnd2-races.Item.{race_id}",
                "type": "ability", "name": 'Melee Combat Bonuses', "img": ab['img'],
                "level": "0",
            })

        race_item = make_race_item(
            race, img,
            description=description,
            item_list_refs=item_list_refs,
            size=_race_size_category(race, races_by_name),
            movement=race.get('movement'),
            skill_mods=_race_skill_mods(race, races_by_name),
        )
        race_item['_id']    = race_id
        race_item['folder'] = bucket_folder
        # ── Baseline traits every race carries (ability-score modifiers + base
        # movement) → placed DIRECTLY on the race document, like OSRIC's
        # "Racial Ability Modifiers". The *special* traits are the abilities. ──
        origin = f"Compendium.{MODULE_ID}.adnd2-races.Item.{race_id}"
        direct_ids = []
        for (lab, icon, chg) in _race_direct_effect_specs(race):
            ef = _make_effect_doc(lab, icon, chg, origin, transfer=True,
                                  description=_ability_description(lab))
            db.put(f'!items.effects!{race_id}.{ef["_id"]}'.encode(),
                   json.dumps(ef).encode())
            direct_ids.append(ef['_id'])
            effects_written += 1
        race_item['effects'] = direct_ids
        written_race_docs[race['name']] = race_item
        db.put(f'!items!{race_id}'.encode(), json.dumps(race_item).encode())

    # ── 4b. CP system: (CP) race copies + per-lineage buyable abilities ─────
    cp_races_folder = _folder('Races (CP)', sort=700000)
    cp_abilities_root = _folder('Racial CP Abilities', sort=400000)
    UNIVERSAL_SKILL_NAMES = {
        'Unskilled Climbing', 'Detect Noise',
        'Minor STR Test (Open Door)',
        'Minor STR Test (Open Held Door)',
        'Major STR Test (Bend Bars / Lift Gates)',
    }
    CP_LINEAGES = ['Dwarf','Elf','Gnome','Halfling',
                   'Half-elf','Half-orc','Half-ogre','Human']
    cp_races_written = 0
    cp_abilities_written = 0
    cp_effects_written = 0
    for base in CP_LINEAGES:
        src = written_race_docs.get(base)
        cp_budget = _lineage_cp_budget(base)
        if not src or cp_budget is None:
            continue
        # ── (CP) race copy ──
        cp_doc = json.loads(json.dumps(src))     # deep copy
        cp_id = make_id()
        cp_doc['_id']    = cp_id
        cp_doc['name']   = f'{src["name"]} (CP)'
        cp_doc['folder'] = cp_races_folder
        cp_doc['_stats'] = _stats_block()
        cp_doc['flags']  = dict(src.get('flags') or {})
        # Strip non-universal item refs: keep only the 5 universal skills.
        cp_doc['system']['itemList'] = [
            r for r in cp_doc['system'].get('itemList', [])
            if r.get('type') == 'skill' and r.get('name') in UNIVERSAL_SKILL_NAMES
        ]
        banner = (f'<h2>Character Points budget: {cp_budget} CP</h2>\n'
                  f'<p><em>Spend on racial abilities purchased separately '
                  f'from the "{src["name"]} CP Abilities" folder.</em></p>\n'
                  f'<hr/>\n')
        cp_doc['system']['description'] = banner + (src['system'].get('description') or '')
        # Re-embed the baseline direct effects (ability mods + movement) under
        # the copy's OWN id — embedded effect docs are keyed by owner id, so the
        # copied effects[] (pointing at the original race) would otherwise be
        # dropped by the pack builder, leaving the CP race with no stat package.
        cp_origin = f"Compendium.{MODULE_ID}.adnd2-races.Item.{cp_id}"
        cp_eff_ids = []
        race_dict = races_by_name.get(base)
        if race_dict:
            for (lab, icon, chg) in _race_direct_effect_specs(race_dict):
                ef = _make_effect_doc(lab, icon, chg, cp_origin, transfer=True,
                                      description=_ability_description(lab))
                db.put(f'!items.effects!{cp_id}.{ef["_id"]}'.encode(),
                       json.dumps(ef).encode())
                cp_eff_ids.append(ef['_id'])
                cp_effects_written += 1
        cp_doc['effects'] = cp_eff_ids
        db.put(f'!items!{cp_id}'.encode(), json.dumps(cp_doc).encode())
        cp_races_written += 1

        # ── Per-lineage CP abilities folder + items ──
        lineage_folder = _folder(f'{base} CP Abilities',
                                 parent=cp_abilities_root,
                                 sort=100000 + CP_LINEAGES.index(base) * 10000)
        for label, body_html in _parse_sp_lineage_abilities_section(_SP_DEMIHUMAN_FILES[base]):
            m = re.match(r'^(.*?)\s*\((\d+)\)\s*$', label.strip())
            if not m: continue
            raw_name, cost = m.group(1).strip(), int(m.group(2))
            # Title-case the name, keeping short connectives lowercase
            keep_lower = {'or','and','of','in','the','a','to','with'}
            parts = raw_name.split()
            pretty = ' '.join(
                w.capitalize() if (i == 0 or w.lower() not in keep_lower)
                              else w.lower()
                for i, w in enumerate(parts)
            )
            display = f'{pretty} ({cost} CP)'
            mech = _ability_effect_changes(raw_name)
            acts = _ability_actions(raw_name)
            ab, ef = make_ability_item(
                display, pick_race_ability_icon(raw_name),
                description=body_html, effect_changes=(mech or None),
                action_groups=(acts or None),
            )
            if acts: action_groups_written += len(acts)
            ab['folder'] = lineage_folder
            ab['flags'] = {'adnd2': {'cpCost': cost, 'cpLineage': base}}
            db.put(f'!items!{ab["_id"]}'.encode(), json.dumps(ab).encode())
            if ef:
                db.put(f'!items.effects!{ab["_id"]}.{ef["_id"]}'.encode(),
                       json.dumps(ef).encode())
                cp_effects_written += 1
            cp_abilities_written += 1

    # ── 5. Persist folders ─────────────────────────────────────────────────
    for f in folders.values():
        db.put(f'!folders!{f["_id"]}'.encode(), json.dumps(f).encode())

    db.close()
    n_shared  = len(shared_pool_specs)
    n_races   = len(races)
    n_total_abs = n_shared + unique_abilities_written
    print(f"  → {n_races} races (in {len(bucket_ids)} sub-folders)")
    print(f"    {n_total_abs} ability sub-items  ({n_shared} shared, {unique_abilities_written} race-specific)")
    print(f"    {skills_written} skill items")
    print(f"    {action_groups_written} action groups attached to abilities")
    print(f"    {cp_races_written} (CP) race copies, {cp_abilities_written} CP-purchasable abilities "
          f"({cp_effects_written} with mechanical effects)")
    print(f"    {effects_written} effects, {len(folders)} folders")
    if no_desc:
        print(f"  ({no_desc} races without HTML description)")
    return n_races

# ─── S&P psionic-power and Psionicist-class HTML index ────────────────────────
# SP individual power pages follow the title pattern:
#   "{Name}-- {Discipline} Power (Skills & Powers)"
# The Psionicist class page is:
#   "Psionicist-- Character Class (Skills & Powers)"
# We extract the name before the first " --" and use it as the lookup key.

_sp_psionic_cache = None


def _build_sp_psionic_index():
    """Build {name_lower → clean_html} for S&P psionic power + Psionicist class pages.
    Scans all SP*.HTM files; selects those whose <TITLE> ends with 'Power (Skills &
    Powers)' or 'Character Class (Skills & Powers)'. Returns '' values for empty pages."""
    global _sp_psionic_cache
    if _sp_psionic_cache is not None:
        return _sp_psionic_cache
    book_dir = os.path.join(SOURCE_BASE, 'SP')
    index = {}
    if not os.path.isdir(book_dir):
        _sp_psionic_cache = index
        return index
    src_dir_files = {f.upper(): f for f in os.listdir(book_dir)}
    for fn in sorted(os.listdir(book_dir)):
        if not (fn.upper().startswith('SP') and fn.upper().endswith('.HTM')):
            continue
        path = os.path.join(book_dir, fn)
        try:
            with open(path, encoding='cp1252') as fh:
                content = fh.read()
        except Exception:
            continue
        if '<TITLE>' not in content:
            continue
        raw_title = content.split('<TITLE>')[1].split('</TITLE>')[0].strip()
        # Match power or class pages
        if not (raw_title.endswith('Power (Skills & Powers)') or
                raw_title.endswith('Character Class (Skills & Powers)')):
            continue
        # Extract the name: everything before the first "--"
        name_part = raw_title.split('--')[0].strip()
        key = name_part.lower()
        if key in index:
            continue
        html = clean_html_file(path, 'SP', src_dir_files)
        if html.strip():
            index[key] = html
    _sp_psionic_cache = index
    return index


# Paths to dedicated SP pages for the 5 psionic attack modes and 5 defense modes.
# Hard-coded as location references only — names and descriptions read at runtime.
_SP_ATTACK_MODE_FILES = [
    'SP/SP00329.HTM',  # Ego Whip (EW)
    'SP/SP00330.HTM',  # Id Insinuation (II)
    'SP/SP00331.HTM',  # Mind Thrust (MT)
    'SP/SP00332.HTM',  # Psionic Blast (PB)
    'SP/SP00333.HTM',  # Psychic Crush (PsC)
]
_SP_DEFENSE_MODE_FILES = [
    'SP/SP00335.HTM',  # Intellect Fortress (IF)
    'SP/SP00336.HTM',  # Mental Barrier (MB)
    'SP/SP00337.HTM',  # Mind Blank (MBk)
    'SP/SP00338.HTM',  # Thought Shield (TS)
    'SP/SP00339.HTM',  # Tower of Iron Will (TW)
]


def _parse_sp_combat_mode_page(rel_path, src_dir_files):
    """Parse one SP psionic attack/defense mode page.  Returns (name, html_desc)
    where name is read from the <TITLE> (before the first '--') and desc is the
    cleaned HTML body.  Returns ('', '') on any error."""
    path = os.path.join(SOURCE_BASE, rel_path)
    if not os.path.exists(path):
        return ('', '')
    try:
        with open(path, encoding='cp1252') as fh:
            content = fh.read()
        raw_title = content.split('<TITLE>')[1].split('</TITLE>')[0].strip()
        name = raw_title.split('--')[0].strip()
        html = clean_html_file(path, 'SP', src_dir_files)
        return (name, html)
    except Exception:
        return ('', '')


_phb_class_desc_cache = None


def _build_phb_class_desc_index():
    """Build {title_lower → clean_html} for PHB class-chapter files (051-109).
    For each file in that range whose <TITLE> has no '--' suffix (i.e. not a
    table), strips '(Player's Handbook)' and lowercases. Also aliases common
    CD-ROM title typos to their corrected spellings so callers can look up by
    canonical name. First match per title wins; skips empty pages."""
    global _phb_class_desc_cache
    if _phb_class_desc_cache is not None:
        return _phb_class_desc_cache
    book_dir = os.path.join(SOURCE_BASE, 'PHB')
    index = {}
    if not os.path.isdir(book_dir):
        _phb_class_desc_cache = index
        return index
    src_dir_files = {f.upper(): f for f in os.listdir(book_dir)}
    all_files = sorted([
        f for f in os.listdir(book_dir)
        if f.upper().startswith('PHB') and f.upper().endswith('.HTM')
    ])
    for fn in all_files:
        num_str = fn.upper().replace('PHB', '').replace('.HTM', '')
        try:    n = int(num_str)
        except: continue
        if not (51 <= n <= 109):
            continue
        path = os.path.join(book_dir, fn)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding='cp1252') as fh:
                content = fh.read()
        except Exception:
            continue
        if '<TITLE>' not in content:
            continue
        raw_title = content.split('<TITLE>')[1].split('</TITLE>')[0].strip()
        # Strip book-name suffix
        title = re.sub(r"\s*\(Player's Handbook\)", '', raw_title, flags=re.I).strip()
        # Skip table-of-contents and chapter-intro entries (have "--" in title)
        if '--' in title or not title:
            continue
        key = title.lower()
        if key in index:
            continue
        html = clean_html_file(path, 'PHB', src_dir_files)
        if not html.strip():
            continue
        index[key] = html
        # Alias CD-ROM typos to corrected spellings ("Speicalist" → "Specialist")
        corrected = re.sub(r'\bspeicalist\b', 'specialist', key)
        if corrected != key and corrected not in index:
            index[corrected] = html
    _phb_class_desc_cache = index
    return index


def _get_class_desc(cls_name, group, class_descs):
    """Look up a class description from PHB chapter 3 HTML (class_descs), with a
    S&P fallback for the Psionicist (not in PHB). Also tries group-level fallbacks
    for specialist wizards and generic priest sub-classes. Returns '' when nothing
    matches."""
    key = cls_name.lower()
    # Psionicist: not in PHB — source from S&P "Psionicist-- Character Class" page
    if group == 'psionicist' or key == 'psionicist':
        sp_index = _build_sp_psionic_index()
        return sp_index.get('psionicist', '')
    if key in class_descs:
        return class_descs[key]
    # Specialist wizard sub-classes share the "Specialist Wizards" PHB section
    if group == 'wizard' and key not in ('mage', 'illusionist'):
        for k, v in class_descs.items():
            if 'specialist' in k and 'wizard' in k:
                return v
    # Priest group: Cleric and Druid have their own entries; fall back to
    # the group "Priest" intro for any unnamed priest sub-class
    if group == 'priest' and key not in ('cleric', 'druid'):
        return class_descs.get('priest', '')
    return ''


# ─── Class CP (Character Points) system ──────────────────────────────────────
#
# Each class that has a matching S&P page gets a "(CP)" copy with no auto-granted
# abilities and a per-class folder of purchasable CP ability items (same pattern
# as the race CP system: `Races (CP)` / `Racial CP Abilities`).
#
# _SP_CLASS_FILES maps class names to their S&P page relative paths.
# These are FILE REFERENCES only — no game data is hardcoded here.
# Specialist wizards share SP00092.HTM; abilities are written once per unique file.

_SP_CLASS_FILES = {
    'Fighter':     'SP/SP00064.HTM',
    'Paladin':     'SP/SP00065.HTM',
    'Ranger':      'SP/SP00066.HTM',
    'Thief':       'SP/SP00068.HTM',
    'Bard':        'SP/SP00077.HTM',
    'Cleric':      'SP/SP00084.HTM',
    'Druid':       'SP/SP00087.HTM',
    'Mage':        'SP/SP00089.HTM',
    # Standard PHB specialist wizards share SP00092
    'Abjurer':     'SP/SP00092.HTM',
    'Conjurer':    'SP/SP00092.HTM',
    'Diviner':     'SP/SP00092.HTM',
    'Enchanter':   'SP/SP00092.HTM',
    'Illusionist': 'SP/SP00092.HTM',
    'Invoker':     'SP/SP00092.HTM',
    'Necromancer': 'SP/SP00092.HTM',
    'Transmuter':  'SP/SP00092.HTM',
    'Psionicist':  'SP/SP00094.HTM',
}

# Friendly folder label per unique SP file (for shared pages like specialist wizards)
_SP_CLASS_FOLDER_LABELS = {
    'SP/SP00092.HTM': 'Specialist Wizards',
}


def _class_cp_budget(cls_name):
    """Parse the S&P class page for 'N character points' and return N.
    Returns None when no CP page is mapped or budget cannot be found."""
    rel = _SP_CLASS_FILES.get(cls_name)
    if not rel:
        return None
    path = os.path.join(SOURCE_BASE, rel)
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='cp1252') as f:
        txt = BeautifulSoup(f.read(), 'html.parser').get_text(' ', strip=True)
    m = re.search(r'(\d+)\s+character\s+points', txt, re.I)
    return int(m.group(1)) if m else None


def _parse_sp_class_abilities_section(sp_rel):
    """Parse a S&P class page into [(label_with_cost, html), ...].
    Abilities are red (#ff0000) bold SIZE=3 headings matching '(N):' at end.
    Descriptions are the normal SIZE=3 text that follows each heading until the
    next heading or end of content."""
    path = os.path.join(SOURCE_BASE, sp_rel)
    if not os.path.exists(path):
        return []
    with open(path, 'r', encoding='cp1252') as f:
        soup = BeautifulSoup(f.read(), 'html.parser')

    # Cost suffix: "(N):", "(N/M):", "(N/M/P):", "(N+):" — colon is part of the heading
    _COST_RE = re.compile(r'\([\d+/]+\)\s*:?\s*$')
    out = []
    current_label  = None
    current_chunks = []

    def _flush():
        if current_label and current_chunks:
            text = ' '.join(current_chunks).strip()
            if text:
                out.append((current_label, f'<p>{text}</p>'))

    body = soup.find('body') or soup
    for elem in body.find_all('font'):
        color = str(elem.get('color') or '').lower()
        size  = elem.get('size', '')
        bold  = bool(elem.find('b'))
        txt   = elem.get_text(' ', strip=True)
        if not txt:
            continue
        if color == '#ff0000' and size == '3' and bold and _COST_RE.search(txt):
            _flush()
            current_label  = txt.rstrip(':').strip()
            current_chunks = []
        elif size == '3' and color not in ('#ff0000', '#008000') and current_label:
            current_chunks.append(txt)
    _flush()
    return out


# ─── Class ability generation ──────────────────────────────────────────────────
#
# Each class item gets a set of Ability sub-items (written to adnd2-classes) and
# cross-pack Skill links (pointing to adnd2-skills thieving-skill items).
#
# Pattern mirrors the race ability system: _class_abilities_for() returns a list
# of spec dicts; migrate_classes() mints the Ability/Effect documents and builds
# the class item's system.itemList.
#
# COPYRIGHT: all labels, HTML descriptions, and numeric values are read at runtime
# from the PHB/S&P HTML files. Only parse anchors (regex patterns, English section
# names) and ARS schema keys are hardcoded below.

# ── Regex patterns that map a PHB <strong> lead sentence to an ability name ──
# Key: the pattern itself is a REFERENCE (what to look for), not game content.
_CLASS_ABILITY_LEAD_RE = [
    (re.compile(r'detect.*evil',               re.I), 'Detect Evil'),
    (re.compile(r'saving throw',               re.I), 'Saving Throw Bonus'),
    (re.compile(r'lay.*hands|laying.*hands',   re.I), 'Lay on Hands'),
    (re.compile(r'cure.*disease',              re.I), 'Cure Disease'),
    (re.compile(r'aura of protection',         re.I), 'Protection From Evil, 10\' Radius'),
    (re.compile(r'holy sword',                 re.I), 'Circle of Power'),
    (re.compile(r'turn undead',                re.I), 'Turn Undead'),
    (re.compile(r'war.?horse',                 re.I), 'War Horse'),
    (re.compile(r'paladin.*priest spells|priest spells.*paladin', re.I),
     'Priest Spells'),
    (re.compile(r'not possess.*magical|magical items',  re.I), 'Code of Conduct'),
    (re.compile(r'never retains wealth|retains wealth', re.I), 'Code of Conduct'),
    (re.compile(r'must tithe',                 re.I), 'Code of Conduct'),
    (re.compile(r'does not attract.*follower',  re.I), 'Code of Conduct'),
    (re.compile(r'employ only.*henchmen',       re.I), 'Code of Conduct'),
    # Ranger bullets
    (re.compile(r'trained.*untamed|animal.*empathy|adept.*creatures', re.I),
     'Animal Empathy'),
    (re.compile(r'ranger.*priest spells|priest spells.*ranger|ranger.*learn priest', re.I),
     'Priest Spells'),
    (re.compile(r'castle|fort|stronghold',     re.I), 'Stronghold'),
    (re.compile(r'attract.*follower|followers.*arrive|attracts.*follower', re.I),
     'Followers'),
    (re.compile(r'code of behavior|ranger.*code',  re.I), 'Code of Conduct'),
    # Bard skill bullets (already clean names)
    (re.compile(r'^Climb Walls$',              re.I), 'Climb Walls'),
    (re.compile(r'^Detect Noise$',             re.I), 'Detect Noise'),
    (re.compile(r'^Pick Pockets$',             re.I), 'Pick Pockets'),
    (re.compile(r'^Read Languages$',           re.I), 'Read Languages'),
    # Bard special abilities
    (re.compile(r'influence.*reaction',        re.I), 'Influence Reactions'),
    (re.compile(r'music.*poetry|rally.*morale|bard.*inspire', re.I), 'Rally Allies'),
    (re.compile(r'counter.*effect.*song|counter.*songs', re.I), 'Counter Song'),
    (re.compile(r'bards.*learn.*little|bit of everything|legend.*lore', re.I), 'Legend Lore'),
    (re.compile(r'wizard.*spell.*bard|bard.*cast.*spell', re.I), 'Wizard Spell Use'),
]

# Thieving skill names used as parse anchors when linking to adnd2-skills items
_THIEVING_SKILL_NAMES = [
    'Pick Pockets', 'Open Locks', 'Find/Remove Traps',
    'Move Silently', 'Hide in Shadows', 'Detect Noise',
    'Climb Walls', 'Read Languages',
]
# Subset granted to bards
_BARD_THIEVING_SKILLS = frozenset({
    'Climb Walls', 'Detect Noise', 'Pick Pockets', 'Read Languages',
})


def _class_ability_icon(name):
    """Pick a verified Foundry webp icon for a class ability from its canonical name.
    All paths validated against FVTT/public/icons/. No SVG fallback — use a
    recognisable webp for every case."""
    low = name.lower()
    # ── HLC high-level (21st+) class powers ─────────────────────────────────────
    if 'intimidation' in low:        return 'icons/skills/social/intimidation-impressing.webp'
    if 'scrying' in low:             return 'icons/magic/perception/orb-crystal-ball-scrying-blue.webp'
    if 'scroll' in low:              return 'icons/tools/scribal/ink-quill-pink.webp'
    if 'sage ability' in low:        return 'icons/skills/trades/academics-study-reading-book.webp'
    if 'item identification' in low: return 'icons/tools/scribal/magnifying-glass.webp'
    if 'undead turning' in low:      return 'icons/magic/holy/prayer-hands-glowing-yellow.webp'
    if 'holy army' in low:           return 'icons/magic/holy/angel-winged-humanoid-blue.webp'
    if 'extra thieving' in low:      return 'icons/skills/social/theft-pickpocket-bribery-brown.webp'
    if 'extra followers' in low:     return 'icons/skills/social/diplomacy-handshake.webp'
    # ── Hierophant druid powers (elemental planes + high-level boons) ───────────
    if 'elemental plane of earth' in low: return 'icons/magic/earth/projectile-stone-ball-brown.webp'
    if 'elemental plane of fire'  in low: return 'icons/magic/fire/flame-burning-campfire-orange.webp'
    if 'elemental plane of water' in low: return 'icons/magic/water/wave-water-blue.webp'
    if 'elemental plane of air'   in low: return 'icons/magic/air/wind-vortex-swirl-blue.webp'
    if 'hibernation'      in low: return 'icons/magic/control/sleep-bubble-purple.webp'
    if 'alter appearance' in low: return 'icons/magic/control/mouth-smile-deception-purple.webp'
    if 'ageless'          in low or 'vigor' in low: return 'icons/magic/time/hourglass-tilted-glowing-gold.webp'
    if 'natural poison'   in low: return 'icons/magic/defensive/shield-barrier-glowing-triangle-green.webp'
    # ── Holy / Divine ──────────────────────────────────────────────────────────
    if 'detect evil'      in low: return 'icons/magic/perception/eye-ringed-green.webp'
    if 'lay on hands'     in low: return 'icons/magic/life/heart-glowing-red.webp'
    if 'cure disease'     in low: return 'icons/magic/life/cross-yellow-green.webp'
    if 'expert healer'    in low: return 'icons/magic/life/cross-flared-green.webp'
    if 'curative'         in low: return 'icons/magic/life/heart-cross-strong-green.webp'
    if 'healing'          in low: return 'icons/magic/life/heart-cross-strong-green.webp'
    if 'turn undead'      in low or 'detect undead' in low:
        return 'icons/magic/holy/prayer-hands-glowing-yellow.webp'
    if 'protection'       in low: return 'icons/magic/holy/prayer-hands-glowing-yellow.webp'
    if 'circle of power'  in low: return 'icons/magic/holy/prayer-hands-glowing-yellow-white.webp'
    if 'priestly wizard'  in low or 'wizardly priest' in low or 'warrior-priest' in low or 'warrior priest' in low:
        return 'icons/magic/holy/prayer-hands-glowing-yellow-green.webp'
    if 'know alignment'   in low: return 'icons/magic/perception/orb-eye-scrying.webp'
    # ── Saving throws / Resistances ────────────────────────────────────────────
    if 'saving throw'     in low: return 'icons/skills/social/intimidation-impressing.webp'
    if 'resist energy'    in low: return 'icons/magic/defensive/barrier-shield-dome-deflect-blue.webp'
    if 'magic resistance' in low or 'spell resistance' in low:
        return 'icons/magic/defensive/barrier-shield-dome-deflect-teal.webp'
    if 'resist charm' in low or 'charm resistance' in low or 'resistance to sleep' in low or 'sound resistance' in low:
        return 'icons/magic/defensive/barrier-shield-dome-blue-purple.webp'
    if 'defense bonus'    in low: return 'icons/magic/defensive/armor-shield-barrier-steel.webp'
    if 'poison resistance' in low: return 'icons/skills/toxins/symbol-poison-drop-skull-green.webp'
    if 'cold resistance'  in low: return 'icons/magic/water/barrier-ice-crystal-wall-faceted-blue.webp'
    if 'fire' in low and 'electrical' in low: return 'icons/magic/fire/flame-burning-campfire-orange.webp'
    if 'fire' in low or 'lightning' in low or 'electrical' in low:
        return 'icons/magic/fire/flame-burning-campfire-orange.webp'
    if 'immunity' in low or 'immune' in low:
        return 'icons/magic/defensive/shield-barrier-glowing-triangle-green.webp'
    if 'guarded mind'     in low or 'mental defense' in low:
        return 'icons/magic/defensive/shield-barrier-blue.webp'
    # ── HP / Health ────────────────────────────────────────────────────────────
    if 'hit point' in low or '1d12' in low or 'health' in low:
        return 'icons/magic/life/heart-cross-strong-blue.webp'
    # ── Armor ──────────────────────────────────────────────────────────────────
    if 'armored wizard' in low or 'limited armor' in low or 'armor use' in low:
        return 'icons/equipment/chest/breastplate-banded-steel.webp'
    if 'weapon allowance' in low or 'weapon use' in low or 'limited weapon' in low:
        return 'icons/weapons/swords/greatsword-crossguard-silver.webp'
    # ── Combat / Weapons ───────────────────────────────────────────────────────
    if 'weapon specialization' in low or 'multiple specialization' in low:
        return 'icons/weapons/swords/greatsword-crossguard-embossed-gold.webp'
    if 'two-weapon' in low or 'two weapon' in low:
        return 'icons/weapons/swords/greatsword-crossguard-blue.webp'
    if 'combat bonus'     in low: return 'icons/skills/melee/blade-tips-triple-steel.webp'
    if 'attack mode'      in low: return 'icons/skills/melee/blade-tips-triple-steel.webp'
    if 'sneak attack'     in low: return 'icons/weapons/daggers/dagger-bone-black.webp'
    if 'backstab'         in low: return 'icons/weapons/daggers/dagger-curved-black.webp'
    if 'bow bonus'        in low: return 'icons/skills/ranged/archery-bow-attack-yellow.webp'
    if 'war machines'     in low: return 'icons/weapons/artillery/cannon-banded.webp'
    # ── Movement / Stealth ─────────────────────────────────────────────────────
    if 'increased movement' in low: return 'icons/magic/movement/acceleration-speed-tech-blue.webp'
    if 'hide in shadow'   in low or 'hide in shadow' in low:
        return 'icons/magic/air/air-smoke-casting.webp'
    if 'move silently'    in low or 'stealth' in low:
        return 'icons/magic/air/air-smoke-casting.webp'
    if 'climbing'         in low or 'climb walls' in low or 'climb' in low:
        return 'icons/skills/movement/figure-running-gray.webp'
    # ── Detection / Perception ─────────────────────────────────────────────────
    if 'detect magic'     in low: return 'icons/magic/perception/eye-ringed-green.webp'
    if 'detect illusion'  in low: return 'icons/magic/perception/eye-ringed-green.webp'
    if 'detect noise'     in low: return 'icons/magic/sonic/bell-alarm-red-purple.webp'
    if 'read magic'       in low: return 'icons/tools/scribal/ink-quill-red.webp'
    # ── Spells / Magic ─────────────────────────────────────────────────────────
    if 'priest spell'     in low: return 'icons/magic/air/air-burst-spiral-teal-green.webp'
    if 'wizard spell'     in low or 'automatic spell' in low or 'bonus spell' in low:
        return 'icons/magic/symbols/runes-star-blue.webp'
    if 'casting reduction' in low or 'spell duration' in low or 'extend duration' in low:
        return 'icons/magic/time/hourglass-tilted-glowing-gold.webp'
    if 'range boost'      in low or 'range boost' in low:
        return 'icons/magic/symbols/arrowhead-green.webp'
    if 'intense magic'    in low or 'no components' in low:
        return 'icons/magic/symbols/cog-glowing-green.webp'
    if 'elemental spell'  in low: return 'icons/magic/symbols/elements-air-earth-fire-water.webp'
    if 'learning bonus'   in low or 'research bonus' in low:
        return 'icons/skills/trades/academics-book-study-runes.webp'
    if 'learning penalty' in low or 'opposition school' in low or 'limited magical' in low:
        return 'icons/magic/symbols/cross-circle-blue.webp'
    if 'scroll use'       in low: return 'icons/tools/scribal/ink-quill-pink.webp'
    # ── Nature / Druid ─────────────────────────────────────────────────────────
    if 'war horse' in low or ('horse' in low and 'war' in low):
        return 'icons/environment/creatures/horse-brown.webp'
    if 'faithful mount'   in low: return 'icons/environment/creatures/horse-brown.webp'
    if 'shapechange'      in low or ('shape' in low and 'change' in low):
        return 'icons/magic/nature/seed-acorn-glowing-green.webp'
    if 'identify'         in low: return 'icons/magic/nature/leaf-glow-green.webp'
    if 'pass without trace' in low: return 'icons/magic/nature/vines-thorned-glow-green.webp'
    if 'bonus spell'      in low and 'druid' in low: return 'icons/magic/nature/leaf-elm-beam-green.webp'
    if 'purify water'     in low: return 'icons/magic/water/water-drop-swirl-blue.webp'
    if 'communicate with' in low or 'speak with' in low:
        return 'icons/magic/nature/wolf-paw-glow-orange.webp'
    if 'animal'           in low or 'empathy' in low:
        return 'icons/magic/nature/wolf-paw-glow-orange.webp'
    if 'tracking'         in low: return 'icons/magic/nature/wolf-paw-glow-orange.webp'
    if 'species enemy'    in low or 'special enemy' in low:
        return 'icons/magic/nature/wolf-paw-glow-large-orange.webp'
    if 'secret language'  in low: return 'icons/skills/trades/academics-merchant-scribe.webp'
    # ── Thieving skills ────────────────────────────────────────────────────────
    if 'open locks'       in low or 'escaping bonds' in low or 'find' in low and 'trap' in low:
        return 'icons/skills/trades/security-lockpicking-chest-blue.webp'
    if 'pick pocket'      in low or 'bribe' in low:
        return 'icons/skills/trades/security-locksmith-key-gray.webp'
    if 'read language'    in low or 'thieves\' cant' in low:
        return 'icons/skills/trades/academics-merchant-scribe.webp'
    if 'tunneling'        in low or 'building' in low:
        return 'icons/skills/trades/construction-mason-bricklayer-red.webp'
    # ── Psionic ────────────────────────────────────────────────────────────────
    if 'psp bonus'        in low or 'psychic adept' in low or 'penetrating mind' in low:
        return 'icons/magic/perception/third-eye-blue-red.webp'
    if 'contact'          in low: return 'icons/magic/perception/eye-ringed-green.webp'
    if 'mental'           in low: return 'icons/magic/defensive/shield-barrier-blue.webp'
    if 'defense mode'     in low or 'guarded mind' in low:
        return 'icons/magic/defensive/barrier-shield-dome-deflect-blue.webp'
    # ── Social / Leadership ────────────────────────────────────────────────────
    if 'leadership'       in low or 'supervisor' in low:
        return 'icons/skills/social/diplomacy-peace-alliance.webp'
    if 'alter moods'      in low or 'influence' in low or 'reaction' in low:
        return 'icons/skills/social/diplomacy-handshake-yellow.webp'
    if 'follower'         in low: return 'icons/skills/social/diplomacy-handshake.webp'
    if 'stronghold'       in low: return 'icons/environment/settlement/castle.webp'
    if 'code of conduct' in low or 'code of behavior' in low:
        return 'icons/equipment/shield/heater-wooden-blue.webp'
    # ── Bard ───────────────────────────────────────────────────────────────────
    if 'song' in low or 'music' in low or 'rally' in low or 'counter effect' in low:
        return 'icons/tools/instruments/harp-gold-glowing.webp'
    if 'counter'          in low: return 'icons/skills/trades/music-notes-sound-blue.webp'
    if 'history'          in low or 'lore' in low or 'legend' in low:
        return 'icons/skills/trades/academics-book-study-purple.webp'
    # ── Catch-all (verified webp, not SVG) ────────────────────────────────────
    return 'icons/skills/trades/academics-investigation-study-blue.webp'


def _class_ability_effect_changes(name, lead_text=''):
    """Map a class ability canonical name to ARS v14 effect change dicts.
    Parses any numeric bonus from lead_text at runtime rather than hardcoding."""
    low = name.lower()

    def _save(formula, props=''):
        return [{'key': 'system.mods.saves.all', 'type': 'custom',
                 'value': {'formula': formula, 'properties': props},
                 'priority': 20, 'phase': 'initial', 'last': ''}]

    def _statusimmune(conditions):
        # ARS reads special.statusimmune as a comma-separated string of condition
        # names (String(value).split(',')). The precise rules scoping (e.g. the
        # druid's charm immunity applies only to woodland creatures) stays in the
        # description for the DM; the effect models the dominant mechanical case.
        return [{'key': 'special.statusimmune', 'type': 'custom',
                 'value': conditions, 'priority': 20, 'phase': 'initial', 'last': ''}]

    if 'saving throw bonus' in low:
        # Parse the bonus magnitude from the PHB text at runtime; emit nothing
        # if it can't be read rather than fabricate a value.
        m = re.search(r'\+\s*(\d+)', lead_text)
        return _save(m.group(1)) if m else []

    if 'fire' in low and ('electrical' in low or 'lightning' in low):
        m = re.search(r'\+\s*(\d+)', lead_text)
        return _save(m.group(1), 'fire,lightning') if m else []

    # True immunities only (not "resistance"/"resist", which are save bonuses).
    if 'charm' in low and 'immun' in low:
        return _statusimmune('charm')

    if 'poison' in low and 'immun' in low:
        return _statusimmune('poison')

    if 'disease' in low and 'immun' in low:
        return _statusimmune('disease')

    return []


def _class_ability_action_groups(name, text=''):
    """Build ARS action groups for class abilities with clickable mechanics.
    `text` is the ability's own source description; any numeric value (heal
    per level, daily uses) is parsed from it at runtime, never hardcoded."""
    low = name.lower()
    icon = _class_ability_icon(name)

    if 'turn undead' in low:
        return [_make_action_group('Turn Undead', icon, [
            _make_action('Turn Undead', type_='use', targeting='template',
                         img=icon, save_type='none'),
        ])]

    if 'lay on hands' in low:
        # Heal-per-level multiplier read from the text ('N hit points per
        # level'); if absent we emit no fabricated formula.
        mult = _parse_points_per_level(text)
        formula = f'@rank.levels.max*{mult}' if mult else ''
        return [_make_action_group('Lay on Hands', icon, [
            _make_action('Heal', type_='heal', targeting='single', img=icon,
                         formula=formula,
                         charges_per_day=_parse_per_day(text)),
        ])]

    if 'cure disease' in low:
        return [_make_action_group('Cure Disease', icon, [
            _make_action('Cure Disease', type_='use', targeting='single',
                         img=icon, save_type='none',
                         charges_per_day=_parse_per_day(text)),
        ])]

    if 'shapechange' in low:
        # Daily-use count read from the text ('three times per day'); 0 = no
        # stated limit rather than a hardcoded frequency.
        return [_make_action_group('Shapechange', icon, [
            _make_action('Shapechange', type_='use', targeting='self',
                         img=icon, charges_per_day=_parse_per_day(text)),
        ])]

    # Activatable hierophant utility powers — a clickable self "use" card. Any
    # stated daily limit is read from the text; 0 = at will.
    if ('alter appearance' in low or 'hibernation' in low
            or 'elemental plane' in low):
        return [_make_action_group(name, icon, [
            _make_action(name, type_='use', targeting='self',
                         img=icon, charges_per_day=_parse_per_day(text)),
        ])]

    if 'influence reactions' in low:
        return [_make_action_group('Influence Reactions', icon, [
            _make_action('Influence Reactions', type_='use', targeting='template',
                         img=icon),
        ])]

    if 'counter song' in low:
        return [_make_action_group('Counter Song', icon, [
            _make_action('Counter Song', type_='use', targeting='self',
                         img=icon),
        ])]

    if 'legend lore' in low:
        return [_make_action_group('Legend Lore', icon, [
            _make_action('Legend Lore', type_='use', targeting='self',
                         img=icon),
        ])]

    return []


def _parse_strong_ability_blocks(html):
    """Extract (lead_text, enclosing_p_html) pairs from a class HTML page.
    Each <strong> element is a bullet lead; its enclosing <p> is the full block."""
    if not html:
        return []
    soup = BeautifulSoup(html, 'html.parser')
    blocks = []
    seen_leads = set()
    for strong in soup.find_all('strong'):
        lead = strong.get_text(strip=True)
        if not lead or len(lead) < 5 or lead in seen_leads:
            continue
        seen_leads.add(lead)
        # Use the enclosing paragraph for the full block
        parent = strong.parent
        block_html = str(parent) if parent and parent.name else str(strong)
        blocks.append((lead, block_html))
    return blocks


def _ranger_extra_abilities(html):
    """Extract Ranger prose abilities (tracking, stealth, species enemy) that
    are not in <strong> bullet leads. Returns [(name, para_html), ...]."""
    if not html:
        return []
    soup = BeautifulSoup(html, 'html.parser')
    extras = []
    seen = set()
    # Anchors: English concepts used as parse references (not copyrighted values)
    ANCHORS = [
        (re.compile(r'tracking proficiency|has a tracking|tracking skill', re.I),
         'Tracking'),
        (re.compile(r'move with great stealth|hiding and moving silently|'
                    r'move silently.*natural|hide.*natural surroundings', re.I),
         'Stealth'),
        (re.compile(r'particular creature|species enemy|marauds their homeland', re.I),
         'Species Enemy'),
    ]
    for para in soup.find_all('p'):
        text = para.get_text(strip=True)
        for pat, name in ANCHORS:
            if pat.search(text) and name not in seen:
                seen.add(name)
                extras.append((name, str(para)))
                break
    return extras


def _druid_granted_abilities():
    """Parse PHB00088.HTM (Granted Powers-- Druid) into [(name, para_html), ...]
    at runtime. Abilities are separated by 'He can...' or 'He gains...' sentences."""
    book_dir = os.path.join(SOURCE_BASE, 'PHB')
    path = os.path.join(book_dir, 'PHB00088.HTM')
    if not os.path.exists(path):
        return []
    src_files = {f.upper(): f for f in os.listdir(book_dir)}
    html = clean_html_file(path, 'PHB', src_files)
    soup = BeautifulSoup(html, 'html.parser')

    ANCHORS = [
        (re.compile(r'saving throw.*fire|fire.*electrical|fire.*attack', re.I),
         'Saving Throw Bonus vs Fire/Electricity'),
        (re.compile(r'secret language|druidic language', re.I), 'Druidic Language'),
        (re.compile(r'identify plants|plants.*animals.*pure water', re.I),
         'Identify Plants, Animals, and Pure Water'),
        (re.compile(r'pass through overgrown|pass.*without.*trail', re.I),
         'Pass Without Trace'),
        (re.compile(r'language.*woodland|woodland.*creature.*language', re.I),
         'Woodland Creature Languages'),
        (re.compile(r'immune.*charm.*woodland|charm.*woodland', re.I),
         'Immunity to Charm'),
        (re.compile(r'shapechange|shape.*reptile|reptile.*bird.*mammal', re.I),
         'Shapechange'),
    ]

    results = []
    seen = set()
    for para in soup.find_all('p'):
        text = para.get_text(strip=True)
        if not text:
            continue
        for pat, name in ANCHORS:
            if pat.search(text) and name not in seen:
                seen.add(name)
                results.append((name, str(para), text))
                break
    return results


# ── Hierophant druid powers (16th-20th level) — PHB00092 ─────────────────────
# Names are descriptive labels/parse anchors; the level comes from the source's
# "Nth level:" markers and the description from the matching sentences at runtime.
_DRUID_HIEROPHANT_ANCHORS = [
    (re.compile(r'immun\w*\s+to\s+all\s+natural\s+poison', re.I),
     'Immunity to Natural Poisons'),
    (re.compile(r'no longer subject to the ability score adjustments for aging', re.I),
     'Ageless Vigor'),
    (re.compile(r'alter his appearance', re.I), 'Alter Appearance'),
    (re.compile(r'hibernat', re.I), 'Hibernation'),
    (re.compile(r'elemental plane of earth', re.I), 'Enter the Elemental Plane of Earth'),
    (re.compile(r'elemental plane of fire', re.I), 'Enter the Elemental Plane of Fire'),
    (re.compile(r'elemental plane of water', re.I), 'Enter the Elemental Plane of Water'),
    (re.compile(r'elemental plane of air', re.I), 'Enter the Elemental Plane of Air'),
]


def _druid_hierophant_abilities():
    """Parse PHB00092 → [(name, html_desc, level)] for the high-level (16th-20th)
    hierophant druid powers. Levels come from the 'Nth level:' markers; each
    ability's description is the matching sentence(s) read from the file."""
    path = os.path.join(SOURCE_BASE, 'PHB', 'PHB00092.HTM')
    if not os.path.exists(path):
        return []
    txt = re.sub(r'\s+', ' ',
                 BeautifulSoup(open(path, encoding='cp1252').read(),
                               'html.parser').get_text(' ')).strip()
    marks = list(re.finditer(r'(\d+)(?:st|nd|rd|th)\s+[Ll]evel:', txt))
    out, seen = [], set()
    for i, mk in enumerate(marks):
        lvl = int(mk.group(1))
        seg = txt[mk.end(): marks[i + 1].start() if i + 1 < len(marks) else len(txt)]
        sentences = re.split(r'(?<=[.])\s+', seg)
        for pat, name in _DRUID_HIEROPHANT_ANCHORS:
            if name in seen:
                continue
            matched = [s.strip() for s in sentences if pat.search(s)]
            if matched:
                seen.add(name)
                out.append((name, '<p>' + ' '.join(matched) + '</p>', lvl))
    return out


def _druid_titles():
    """Parse PHB00091/PHB00092 → {level: title} for the druid's special advancement
    titles (Initiate below 12th, Druid 12th, Archdruid 13th, Great Druid 14th,
    Grand Druid 15th, Hierophant 16th-20th). All title strings are read from the
    files at runtime; only the parse anchors are hardcoded."""
    titles = {}
    parts = []
    for fn in ('PHB00091.HTM', 'PHB00092.HTM'):
        path = os.path.join(SOURCE_BASE, 'PHB', fn)
        if os.path.exists(path):
            parts.append(re.sub(r'\s+', ' ',
                BeautifulSoup(open(path, encoding='cp1252').read(),
                              'html.parser').get_text(' ')))
    if not parts:
        return titles
    txt = ' '.join(parts)
    # "<Title> (Nth level)" — covers Great Druid (14th), Grand Druid (15th)
    for m in re.finditer(r'((?:[A-Z][a-z]+\s+)*[A-Z][a-z]+)\s*'
                         r'\((\d+)(?:st|nd|rd|th)\s+level\)', txt):
        titles[int(m.group(2))] = re.sub(r'^The\s+', '', m.group(1)).strip()
    # Archdruid (13th) — lowercase/plural in prose
    m = re.search(r'(\w*druids?)\s*\(13th\s+level\)', txt, re.I)
    if m:
        titles[13] = m.group(1).rstrip('s').capitalize()
    # 12th: "title of 'druid'"
    m = re.search(r'12th\s+level.{0,80}?title\s+of\s+["“]?(\w+)', txt, re.I)
    if m:
        titles[12] = m.group(1).capitalize()
    # below 12th: "officially known as 'initiates'"
    m = re.search(r'officially known as\s+["“]?(\w+)', txt, re.I)
    if m:
        initiate = m.group(1).rstrip('s').capitalize()
        for lvl in range(1, 12):
            titles.setdefault(lvl, initiate)
    # 16th-20th: hierophant
    m = re.search(r'(hierophant)', txt, re.I)
    if m:
        for lvl in range(16, 21):
            titles[lvl] = m.group(1).capitalize()
    return titles


# ── High-level (21st+) class powers — HLC "<Class> Beyond 20th Level" pages ───
# Each page lists its powers as red bold "<Name>:" sub-headers (→ <strong> after
# cleaning), so _parse_strong_ability_blocks reads them like the PHB paladin/
# ranger bullets. The acquisition level comes from the prose ("at 21st level",
# "at 24th level"); abilities whose prose states no threshold default to 21 (the
# chapter baseline). Only the page references are hardcoded; names/levels/text
# are read from the user's HLC files at runtime. The wizard page maps to Mage and
# the priest page to Cleric (its powers — improved turning, holy army — are
# cleric-flavoured; the druid has its own hierophant powers instead).
_HLC_CLASS_PAGES = {
    'Fighter': 'HLC00216', 'Ranger':  'HLC00217', 'Paladin': 'HLC00218',
    'Mage':    'HLC00222', 'Cleric':  'HLC00227', 'Thief':   'HLC00235',
    'Bard':    'HLC00237',
}


def _hlc_class_abilities(cls_name):
    """Parse the HLC 'Beyond 20th Level' page for a class → [(name, html, level)]
    for its high-level (21st+) special powers, or [] if the class has none."""
    fn = _HLC_CLASS_PAGES.get(cls_name)
    if not fn:
        return []
    book_dir = os.path.join(SOURCE_BASE, 'HLC')
    path = os.path.join(book_dir, fn + '.HTM')
    if not os.path.exists(path):
        return []
    src_files = {f.upper(): f for f in os.listdir(book_dir)}
    html = clean_html_file(path, 'HLC', src_files)
    out = []
    for lead, block_html in _parse_strong_ability_blocks(html):
        name = lead.rstrip(':').strip()
        if not name:
            continue
        level = max(21, _ability_acquisition_level(block_html))
        out.append((name, block_html, level))
    return out


def _cleric_turn_undead_block(html):
    """Extract the paragraph containing Turn Undead from the Cleric description."""
    if not html:
        return None
    soup = BeautifulSoup(html, 'html.parser')
    pat = re.compile(r'turn undead', re.I)
    for para in soup.find_all('p'):
        if pat.search(para.get_text()):
            return str(para)
    return None


def _class_followers_block(html):
    """Extract the paragraph(s) describing a class's follower-attraction
    feature ('attracts a body of men-at-arms/believers/followers...' at
    some level) from its PHB class description. Mirrors
    _cleric_turn_undead_block / _thief_backstab_block: captures the
    triggering paragraph plus immediately-following paragraphs that
    continue the same topic (stronghold/men-at-arms/followers prose)."""
    if not html:
        return None
    soup = BeautifulSoup(html, 'html.parser')
    paras = soup.find_all('p')
    trigger_pat = re.compile(r'attracts?\s+(?:a\s+|an\s+|\d*d?\d*\s*)?'
                             r'(?:fanatically loyal group of |body of |elite )?'
                             r'(?:men-at-arms|follow|believ|soldier|bodyguard)', re.I)
    follow_pat  = re.compile(r'follow|men-at-arms|believ|soldier|stronghold|'
                             r'castle|household|bodyguard|\bLord\b', re.I)
    out, capture = [], False
    for p in paras:
        text = p.get_text()
        if not capture and trigger_pat.search(text):
            capture = True
        if capture:
            if out and not follow_pat.search(text):
                break
            out.append(str(p))
            if len(out) >= 3:
                break
    return ''.join(out) if out else None


def _thief_backstab_block(html):
    """Extract Backstab ability block from PHB class text or PHB00100 explanations."""
    # Try primary class description first
    if html:
        soup = BeautifulSoup(html, 'html.parser')
        pat = re.compile(r'backstab', re.I)
        for para in soup.find_all('p'):
            if pat.search(para.get_text()):
                return str(para)
    # Fall back to thieving skill explanations page
    book_dir = os.path.join(SOURCE_BASE, 'PHB')
    path = os.path.join(book_dir, 'PHB00100.HTM')
    if not os.path.exists(path):
        return None
    src_files = {f.upper(): f for f in os.listdir(book_dir)}
    expl_html = clean_html_file(path, 'PHB', src_files)
    soup2 = BeautifulSoup(expl_html, 'html.parser')
    pat = re.compile(r'backstab', re.I)
    paras = []
    capture = False
    for tag in soup2.find_all(['p', 'h3', 'h4', 'strong']):
        if pat.search(tag.get_text()) and tag.name in ('strong', 'h3', 'h4'):
            capture = True
        if capture and tag.name == 'p':
            paras.append(str(tag))
            if len(paras) >= 3:
                break
    return ''.join(paras) if paras else None


def _load_skill_link_map():
    """Build {skill_name_lower → (id, name, img, pack)} from the adnd2-skills
    staging JSON, written by migrate_skills() earlier in the run."""
    skills_src = os.path.join(_PACK_SRC_BASE,
                              os.path.basename(OUTPUT_PACKS['skills']))
    result = {}
    if not os.path.isdir(skills_src):
        return result
    for fn in os.listdir(skills_src):
        if not fn.endswith('.json'):
            continue
        try:
            with open(os.path.join(skills_src, fn), encoding='utf-8') as fh:
                doc = json.load(fh)
        except Exception:
            continue
        if doc.get('type') != 'skill':
            continue
        name = doc.get('name', '')
        if name:
            result[name.lower()] = (doc['_id'], name, doc.get('img', ''), 'skills')
    return result


def _load_weapon_specialization_data():
    """Read PHB00125–127 and return cleaned HTML descriptions plus bonus
    values and slot costs parsed from the source files at runtime."""
    phb_dir       = os.path.join(SOURCE_BASE, 'PHB')
    src_dir_files = {f.upper(): f for f in os.listdir(phb_dir)}

    def _page(fname):
        path = os.path.join(phb_dir, fname)
        if not os.path.exists(path):
            return ''
        return clean_html_file(path, 'PHB', src_dir_files)

    intro_html   = _page('PHB00125.HTM')
    cost_html    = _page('PHB00126.HTM')
    effects_html = _page('PHB00127.HTM')

    melee_atk = melee_dmg = bow_atk = 0
    melee_cost = bow_cost = 0
    bow_pb = None      # bow point-blank distance band (min, max) — sourced below
    eff_path  = os.path.join(phb_dir, 'PHB00127.HTM')
    cost_path = os.path.join(phb_dir, 'PHB00126.HTM')
    # Number words that appear in the point-blank prose ("six feet to 30 feet").
    _NUMWORD = {'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
                'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10}
    def _num(tok):
        tok = tok.strip().lower()
        return int(tok) if tok.isdigit() else _NUMWORD.get(tok)
    if os.path.exists(eff_path):
        with open(eff_path, encoding='latin-1') as fh:
            eff_text = ' '.join(BeautifulSoup(fh.read(), 'html.parser').get_text(' ').split())
        m = re.search(r'\+(\d+)\s+bonus to all his attack rolls', eff_text)
        if m: melee_atk = int(m.group(1))
        m = re.search(r'\+(\d+)\s+bonus to all damage rolls', eff_text)
        if m: melee_dmg = int(m.group(1))
        m = re.search(r'gains a \+(\d+) modifier on attack rolls', eff_text)
        if m: bow_atk = int(m.group(1))
        # Point-blank range band for bows, e.g. "...for bows is from six feet to
        # 30 feet." Both bounds (and the +2) are PHB rules data, so they are read
        # from the file at runtime — never hardcoded. Used to gate the bow
        # specialization attack bonus to point-blank range via a conditional.
        m = re.search(r'point[- ]blank range for bows is from\s+(\w+)\s+feet'
                      r'\s+to\s+(\w+)\s+feet', eff_text, re.I)
        if m:
            lo, hi = _num(m.group(1)), _num(m.group(2))
            if lo is not None and hi is not None:
                bow_pb = (lo, hi)
    if os.path.exists(cost_path):
        with open(cost_path, encoding='latin-1') as fh:
            cost_text = ' '.join(BeautifulSoup(fh.read(), 'html.parser').get_text(' ').split())
        m = re.search(r'melee weapon or crossbow.*?(\w+)\s+slots', cost_text, re.S)
        _WORD_NUM = {'one': 1, 'two': 2, 'three': 3, 'four': 4}
        if m: melee_cost = max(0, _WORD_NUM.get(m.group(1).lower(), 2) - 1)
        m = re.search(r'bow.*?total of\s+(\w+)\s+proficiency slots', cost_text, re.S)
        if m: bow_cost   = max(0, _WORD_NUM.get(m.group(1).lower(), 3) - 1)

    return {
        'full_desc':   intro_html + cost_html + effects_html,
        'melee_desc':  effects_html,
        'bow_desc':    effects_html,
        'melee_atk':   melee_atk,
        'melee_dmg':   melee_dmg,
        'bow_atk':     bow_atk,
        'melee_cost':  melee_cost,
        'bow_cost':    bow_cost,
        'bow_pb':      bow_pb,
    }


# ── Acquisition level — the class level at which an ability/skill is gained ───
# Parsed from PHB prose ("at 3rd level", "when he reaches 9th level"). The first
# convertible level is the acquisition level; later mentions are scaling. The
# regex/word-map are parse anchors only — the actual level comes from the file.
_ORDINAL_WORDS = {
    'first': 1, 'second': 2, 'third': 3, 'fourth': 4, 'fifth': 5, 'sixth': 6,
    'seventh': 7, 'eighth': 8, 'ninth': 9, 'tenth': 10, 'eleventh': 11,
    'twelfth': 12, 'thirteenth': 13, 'fourteenth': 14, 'fifteenth': 15,
    'sixteenth': 16, 'seventeenth': 17, 'eighteenth': 18, 'nineteenth': 19,
    'twentieth': 20,
}
_ABILITY_LEVEL_RE = re.compile(
    r'(?:at|upon reaching|upon attaining|when (?:he|she|they) reach(?:es)?|'
    r'beginning at|reaches?|attains?)\s+(?:the\s+)?'
    r'(\d+(?:st|nd|rd|th)|' + '|'.join(_ORDINAL_WORDS) + r')\s+level',
    re.I)


def _ability_acquisition_level(*texts):
    """Return the class level at which an ability/skill becomes available, parsed
    from its PHB description prose. Defaults to 1 (available from the start) when
    no acquisition cue is present."""
    blob = re.sub(r'<[^>]+>', ' ', ' '.join(t for t in texts if t))
    for m in _ABILITY_LEVEL_RE.finditer(blob):
        tok = m.group(1).lower()
        md = re.match(r'(\d+)', tok)
        lvl = int(md.group(1)) if md else _ORDINAL_WORDS.get(tok)
        if lvl:
            return lvl
    return 1


def _class_first_spell_level(cls):
    """Return the character level at which a class first gains spell slots, read
    from CLASS.DAT's per-level spell-slot table (row i = char level i+1, first
    non-zero row). DAT-first source for the spellcasting acquisition level
    (Paladin 9, Ranger 8, Bard 2, full casters 1). None for non-casters."""
    table = cls.get('spell_table')
    if not table:
        return None
    for i, row in enumerate(table):
        if any(v > 0 for v in row):
            return i + 1
    return None


def _class_abilities_for(cls, class_descs):
    """Return a list of ability spec dicts for a CLASS.DAT class record.
    Each spec has: name, description (HTML), effect_changes, action_groups,
    icon, level (acquisition level), and optionally skill_link=True."""
    group = cls.get('group', '')
    name  = cls.get('name', '')
    desc_html = _get_class_desc(name, group, class_descs)
    dat_spell_level = _class_first_spell_level(cls)   # DAT-first for spellcasting
    specs = []
    seen_names = set()

    def _push(spec_name, desc='', chg=None, acts=None, lead='', level=None):
        if not spec_name or spec_name in seen_names:
            return
        seen_names.add(spec_name)
        icon = _class_ability_icon(spec_name)
        if not chg:
            chg = _class_ability_effect_changes(spec_name, lead)
        if not acts:
            acts = _class_ability_action_groups(spec_name, f'{desc} {lead}')
        # Acquisition level: an explicit level wins (e.g. hierophant powers whose
        # level is parsed from the source); otherwise spellcasting grants come
        # from CLASS.DAT (DAT-first) and every other ability from the PHB prose.
        low = spec_name.lower()
        if level is None:
            if dat_spell_level and ('priest spells' in low or 'wizard spell' in low):
                level = dat_spell_level
            else:
                level = _ability_acquisition_level(desc, lead)
        specs.append({
            'name':           spec_name,
            'description':    desc,
            'effect_changes': chg or None,
            'action_groups':  acts or None,
            'icon':           icon,
            'level':          level,
        })

    # ── Fighter: Weapon Specialization (PHB00125–127) + Followers (9th, "Lord") ─
    if name.lower() == 'fighter':
        ws = _load_weapon_specialization_data()
        _push('Weapon Specialization', ws['full_desc'])
        _push('Followers', _class_followers_block(desc_html) or '')

    # ── Paladin & Ranger: <strong> bullet blocks + prose extras ──────────────
    elif name.lower() in ('paladin', 'ranger'):
        blocks = _parse_strong_ability_blocks(desc_html)
        for lead, block_html in blocks:
            ability_name = None
            for pat, aname in _CLASS_ABILITY_LEAD_RE:
                if pat.search(lead):
                    ability_name = aname
                    break
            if not ability_name:
                words = re.sub(r'[,.].*', '', lead).split()[:4]
                ability_name = ' '.join(w.capitalize() for w in words) if words else lead[:30]
            _push(ability_name, block_html, lead=lead)
        if name.lower() == 'ranger':
            for extra_name, extra_html in _ranger_extra_abilities(desc_html):
                _push(extra_name, extra_html)

    # ── Bard: <strong> blocks EXCEPT the thieving-skill names (those become
    #    cross-pack skill links in migrate_classes, not duplicate ability items)
    elif name.lower() == 'bard':
        blocks = _parse_strong_ability_blocks(desc_html)
        for lead, block_html in blocks:
            # Skip pure skill-name bullets — they become itemList skill links
            if lead in _BARD_THIEVING_SKILLS:
                continue
            ability_name = None
            for pat, aname in _CLASS_ABILITY_LEAD_RE:
                if pat.search(lead):
                    ability_name = aname
                    break
            if not ability_name:
                words = re.sub(r'[,.].*', '', lead).split()[:4]
                ability_name = ' '.join(w.capitalize() for w in words) if words else lead[:30]
            _push(ability_name, block_html, lead=lead)
        # Followers (9th level, stronghold) — plain prose, not a <strong> bullet
        _push('Followers', _class_followers_block(desc_html) or '')

    # ── Thief: Backstab ability (thieving skills themselves become skill links) ─
    elif name.lower() == 'thief':
        backstab_html = _thief_backstab_block(desc_html)
        if backstab_html:
            _push('Backstab', backstab_html)

    # ── Cleric: Turn Undead + Followers (8th level, believers) from prose ────
    elif name.lower() == 'cleric':
        turn_block = _cleric_turn_undead_block(desc_html)
        _push('Turn Undead', turn_block or '')
        _push('Followers', _class_followers_block(desc_html) or '')
        _push('Priest Spells')

    # ── Druid: granted powers (3rd/7th, PHB00088) + hierophant powers
    #    (16th-20th, PHB00092) with their parsed acquisition levels ────────────
    elif name.lower() == 'druid':
        for ab_name, para_html, para_text in _druid_granted_abilities():
            chg = _class_ability_effect_changes(ab_name, para_text)
            _push(ab_name, para_html, chg=chg)
        for ab_name, para_html, lvl in _druid_hierophant_abilities():
            _push(ab_name, para_html, level=lvl)

    # ── High-level (21st+) powers from the HLC book, for every class that has a
    #    "Beyond 20th Level" page. _push de-dupes against the PHB abilities above
    #    and auto-attaches an effect where modelable (e.g. Disease Immunity). ───
    for ab_name, para_html, lvl in _hlc_class_abilities(name):
        _push(ab_name, para_html, level=lvl)

    return specs


_RANGER_THIEVING_SKILLS = frozenset({'Move Silently', 'Hide in Shadows'})

# Bard and Ranger use their own level-1 base scores for the thief skills they
# share with the Thief (Tables 33 / 18). Where a class's score differs, a
# distinct "<Skill> (<Class>)" variant skill item is emitted by migrate_skills
# and auto-granted instead of the Thief-scored item. The label/subset here are
# the only hardcoded bits — the scores that decide whether a variant exists are
# read from the PHB tables at runtime (loaders resolved at call time, since they
# are defined later in the file).
def _class_thief_variant_spec(cls_low):
    """Return (label, base_skill_set, class_scores_dict) for a class that has
    its own thieving-skill base scores, or None."""
    if cls_low == 'bard':
        return 'Bard', _BARD_THIEVING_SKILLS, _load_bard_thief_skill_base_scores()
    if cls_low == 'ranger':
        return 'Ranger', _RANGER_THIEVING_SKILLS, _load_ranger_thief_skill_base_scores()
    return None


def _class_thief_skill_variant_name(label, base_name, cls_scores, thief_scores):
    """Return the granted skill name for a class's thieving skill: the
    "<base> (<label>)" variant when its base score differs from the Thief's,
    otherwise the shared base name."""
    key = base_name.lower()
    pct = cls_scores.get(key)
    if pct is not None and pct != thief_scores.get(key):
        return f"{base_name} ({label})"
    return base_name


def _class_thieving_skills_for(cls_name):
    """Return the list of thieving skill item names to auto-grant for a class.
    Thief gets all 8 (shared, Thief-scored); Bard/Ranger get their subset, using
    the class-specific variant name wherever their base score differs."""
    low = cls_name.lower()
    if low == 'thief':
        return list(_THIEVING_SKILL_NAMES)
    spec = _class_thief_variant_spec(low)
    if not spec:
        return []
    label, base_set, cls_scores = spec
    thief_scores = _thief_canonical_scores()
    return [_class_thief_skill_variant_name(label, s, cls_scores, thief_scores)
            for s in _THIEVING_SKILL_NAMES if s in base_set]


# Normalize a S&P thief-skill ability label to the classic skill display name.
_CP_SKILL_NAME_ALIASES = {'escaping bonds': 'escape bonds'}
# Only these classes' CP abilities link to rogue skills (the others may list a
# similarly-named option, e.g. a S&P Fighter's "Move silently", which is out of
# scope here).
_CP_ROGUE_SKILL_CLASSES = frozenset({'thief', 'bard', 'ranger'})


def _cp_skill_link_name(cls_name, raw_skill_name):
    """Given a rogue class and a skill name parsed from its S&P CP page, return
    the *classic* skill item name the CP-purchase ability should link to (auto-
    grant), or None if the name is not one of the 13 scored thieving skills.
    Bard/Ranger map to their "(Class)" variant where their base score differs;
    everything else maps to the shared Thief-scored skill."""
    key = raw_skill_name.strip().lower()
    key = _CP_SKILL_NAME_ALIASES.get(key, key)
    if key not in _thief_canonical_scores():
        return None     # not a scored rogue skill → no link
    # The 8 PHB skills may have a Bard/Ranger variant; the 5 S&P expansion skills
    # (bribe, detect magic, detect illusion, tunneling, escape bonds) do not, so
    # they link straight to the base skill. The returned name is only ever used
    # via .lower() for the skill_map lookup, so a lowercase key resolves fine.
    base_display = next((s for s in _THIEVING_SKILL_NAMES if s.lower() == key), None)
    if base_display is None:
        return key
    spec = _class_thief_variant_spec(cls_name.lower())
    if not spec or base_display not in spec[1]:
        return base_display
    label, _base_set, cls_scores = spec
    return _class_thief_skill_variant_name(label, base_display, cls_scores,
                                           _thief_canonical_scores())


def migrate_classes():
    """Phase 3: write the classes pack — a `class` Item per CLASS.DAT record,
    foldered by group (Warriors/Rogues/Priests/Wizards/Psionicists).
    Each class also gets Ability sub-items (descriptive + mechanical effects where
    parseable) and cross-pack Skill links to thieving skills (Thief/Bard).
    Must run AFTER migrate_skills() so the skill staging JSON is readable.
    Returns count."""
    print("\n=== Classes (CLASS.DAT) ===")
    classes = parse_classes()
    if not classes:
        print("  No classes parsed."); return 0
    class_descs = _build_phb_class_desc_index()
    print(f"  PHB class descriptions indexed: {len(class_descs)} titles")
    # Load skill link map (requires migrate_skills() to have run first)
    skill_map = _load_skill_link_map()
    print(f"  Skills available for thieving-skill links: {len(skill_map)}")

    db = _open_pack(OUTPUT_PACKS['classes'])
    folders = {}
    for label, sort in [('Warriors', 100000), ('Rogues', 200000),
                        ('Priests', 300000), ('Wizards', 400000),
                        ('Psionicists', 500000)]:
        fid = make_id()
        folders[label] = make_compendium_folder(fid, label, 'Item', sort=sort)
    GROUP_FOLDER = {
        'warrior':    'Warriors',
        'rogue':      'Rogues',
        'priest':     'Priests',
        'wizard':     'Wizards',
        'psionicist': 'Psionicists',
    }

    # ── Ability folder hierarchy: Class Abilities → {ClassName} ──────────────
    abilities_root_id = make_id()
    abilities_root = make_compendium_folder(abilities_root_id, 'Class Abilities',
                                            'Item', sort=600000)
    folders['Class Abilities'] = abilities_root
    # One sub-folder per class that has abilities; created lazily below.
    ability_folders = {}   # cls_name → folder _id

    no_desc = 0
    total_abilities = 0
    total_effects   = 0
    total_skill_links = 0
    count = 0
    written_class_docs = {}   # cls_name → written class item dict (for CP deep-copy)

    for cls in classes:
        desc   = _get_class_desc(cls['name'], cls.get('group', ''), class_descs)
        if not desc:
            no_desc += 1
        item   = make_class_item(cls, description=desc)
        cls_id = item['_id']
        cls_uuid = f"Compendium.{MODULE_ID}.adnd2-classes.Item.{cls_id}"

        # ── Generate and write Ability sub-items ─────────────────────────────
        specs = _class_abilities_for(cls, class_descs)
        item_list_refs = []
        if specs:
            # Create a per-class ability sub-folder on first use
            if cls['name'] not in ability_folders:
                sort_val = count * 1000 + 100
                ab_fid = make_id()
                ab_folder = make_compendium_folder(
                    ab_fid, cls['name'], 'Item',
                    sort=sort_val, parent=abilities_root_id)
                ability_folders[cls['name']] = ab_fid
                db.put(f'!folders!{ab_fid}'.encode(), json.dumps(ab_folder).encode())

        for spec in specs:
            ab, ef = make_ability_item(
                spec['name'], spec['icon'],
                description=spec['description'],
                effect_changes=spec['effect_changes'],
                action_groups=spec['action_groups'],
            )
            ab['folder'] = ability_folders.get(cls['name'])
            db.put(f'!items!{ab["_id"]}'.encode(), json.dumps(ab).encode())
            if ef:
                db.put(f'!items.effects!{ab["_id"]}.{ef["_id"]}'.encode(),
                       json.dumps(ef).encode())
                total_effects += 1
            total_abilities += 1
            item_list_refs.append({
                'id':       ab['_id'],
                'uuid':     f'Item.{ab["_id"]}',
                'sourceuuid': cls_uuid,
                'type':     'ability',
                'name':     spec['name'],
                'img':      spec['icon'],
                'level':    str(spec.get('level', 1)),
            })

        # ── Add thieving skill cross-pack links (Thief, Bard, Ranger) ────────
        # All thieving skills are available from level 1.
        for skill_name in _class_thieving_skills_for(cls['name']):
            entry = skill_map.get(skill_name.lower())
            if not entry:
                continue
            sid, sname, simg, spack = entry
            item_list_refs.append({
                'id':       sid,
                'uuid':     f'Compendium.{MODULE_ID}.adnd2-{spack}.Item.{sid}',
                'sourceuuid': cls_uuid,
                'type':     'skill',
                'name':     sname,
                'img':      simg,
                'level':    '1',
            })
            total_skill_links += 1

        item['system']['itemList'] = item_list_refs

        bucket = GROUP_FOLDER.get(cls.get('group', ''), None)
        if bucket:
            item['folder'] = folders[bucket]['_id']
        db.put(f'!items!{cls_id}'.encode(), json.dumps(item).encode())
        written_class_docs[cls['name']] = item
        count += 1

    # ── 2. CP class copies + per-class CP abilities (S&P system) ─────────────
    cp_classes_folder_id  = make_id()
    cp_classes_folder     = make_compendium_folder(
        cp_classes_folder_id, 'Classes (CP)', 'Item', sort=700000)
    cp_abilities_root_id  = make_id()
    cp_abilities_root     = make_compendium_folder(
        cp_abilities_root_id, 'Class CP Abilities', 'Item', sort=800000)
    db.put(f'!folders!{cp_classes_folder_id}'.encode(),
           json.dumps(cp_classes_folder).encode())
    db.put(f'!folders!{cp_abilities_root_id}'.encode(),
           json.dumps(cp_abilities_root).encode())

    keep_lower_words = {'or', 'and', 'of', 'in', 'the', 'a', 'to', 'with', 'for',
                        'from', 'on', 'at', 'by'}
    def _title_case(s):
        words = s.split()
        return ' '.join(
            w.capitalize() if (i == 0 or w.lower() not in keep_lower_words)
                          else w.lower()
            for i, w in enumerate(words)
        )

    cp_classes_written   = 0
    cp_abilities_written = 0
    cp_effects_written   = 0
    # Track which SP files have had their abilities written to avoid duplicates
    # (all standard specialist wizards share SP00092.HTM).
    written_sp_files  = {}   # sp_rel → ability_folder_id

    for cls in classes:
        cls_name = cls['name']
        sp_rel   = _SP_CLASS_FILES.get(cls_name)
        src      = written_class_docs.get(cls_name)
        if not sp_rel or not src:
            continue
        cp_budget = _class_cp_budget(cls_name)
        if cp_budget is None:
            continue

        # ── CP class copy ─────────────────────────────────────────────────────
        cp_doc  = json.loads(json.dumps(src))
        cp_id   = make_id()
        cp_doc['_id']    = cp_id
        cp_doc['name']   = f'{cls_name} (CP)'
        cp_doc['folder'] = cp_classes_folder_id
        cp_doc['_stats'] = _stats_block()
        cp_doc['flags']  = dict(src.get('flags') or {})
        # Strip auto-granted itemList (player buys abilities à la carte)
        cp_doc['system']['itemList'] = []
        # Determine the abilities folder label for the banner
        ab_folder_label = _SP_CLASS_FOLDER_LABELS.get(sp_rel, cls_name)
        banner = (
            f'<h2>Character Points budget: {cp_budget} CP</h2>\n'
            f'<p><em>Spend on class abilities purchased separately '
            f'from the "{ab_folder_label} CP Abilities" folder.</em></p>\n'
            f'<hr/>\n'
        )
        cp_doc['system']['description'] = (
            banner + (src['system'].get('description') or ''))
        db.put(f'!items!{cp_id}'.encode(), json.dumps(cp_doc).encode())
        cp_classes_written += 1

        # ── Per-class (or per-shared-file) CP abilities folder + items ────────
        if sp_rel not in written_sp_files:
            # Create a new abilities sub-folder for this SP file
            ab_fid = make_id()
            ab_folder = make_compendium_folder(
                ab_fid, f'{ab_folder_label} CP Abilities', 'Item',
                sort=100000 + len(written_sp_files) * 10000,
                parent=cp_abilities_root_id)
            db.put(f'!folders!{ab_fid}'.encode(), json.dumps(ab_folder).encode())
            written_sp_files[sp_rel] = ab_fid

            for label, body_html in _parse_sp_class_abilities_section(sp_rel):
                # Parse "Name (cost)" format — cost may be N, N/M, N/M/P, or N+
                # (the trailing '*' the source puts on scored thief skills is
                # stripped; detection of those is by name, below).
                m = re.match(r'^(.*?)\s*\(([\d+/]+)\)\s*$',
                             label.strip())
                if not m:
                    continue
                raw_name  = m.group(1).strip().rstrip('*').strip()
                cost_str  = m.group(2)       # e.g. "5", "5/10", "5+"
                display   = f'{_title_case(raw_name)} ({cost_str} CP)'
                icon      = _class_ability_icon(raw_name)
                mech      = _class_ability_effect_changes(raw_name, body_html)
                acts      = _class_ability_action_groups(raw_name, body_html)
                ab, ef    = make_ability_item(
                    display, icon,
                    description=body_html,
                    effect_changes=(mech or None),
                    action_groups=(acts or None),
                )
                # Store the first numeric cost for flag metadata
                m_cost = re.match(r'\d+', cost_str)
                first_cost = int(m_cost.group(0)) if m_cost else 0
                ab['folder'] = ab_fid
                ab['flags']  = {'adnd2': {'cpCost': first_cost,
                                          'cpClass': ab_folder_label}}
                # Link a rogue-class CP purchase to the classic skill it grants,
                # so buying the ability auto-grants the skill at the correct
                # Thief/Bard/Ranger base score (no skill duplication).
                if cls_name.lower() in _CP_ROGUE_SKILL_CLASSES:
                    link_name = _cp_skill_link_name(cls_name, raw_name)
                    entry = skill_map.get(link_name.lower()) if link_name else None
                    if entry:
                        sid, sname, simg, spack = entry
                        ab['system']['itemList'] = [{
                            'id':         sid,
                            'uuid':       f'Compendium.{MODULE_ID}.adnd2-{spack}.Item.{sid}',
                            'sourceuuid': f'Compendium.{MODULE_ID}.adnd2-classes.Item.{ab["_id"]}',
                            'type':       'skill',
                            'name':       sname,
                            'img':        simg,
                            'quantity':   '1',
                            'level':      '0',
                        }]
                        total_skill_links += 1
                db.put(f'!items!{ab["_id"]}'.encode(),
                       json.dumps(ab).encode())
                if ef:
                    db.put(f'!items.effects!{ab["_id"]}.{ef["_id"]}'.encode(),
                           json.dumps(ef).encode())
                    cp_effects_written += 1
                cp_abilities_written += 1

    for f in folders.values():
        db.put(f'!folders!{f["_id"]}'.encode(), json.dumps(f).encode())
    db.close()
    print(f"  → {count} classes in {len(folders)} folders "
          f"({no_desc} without PHB description)")
    print(f"    {total_abilities} ability items, {total_effects} effects, "
          f"{total_skill_links} thieving-skill links")
    print(f"    {cp_classes_written} (CP) class copies, "
          f"{cp_abilities_written} CP abilities "
          f"({cp_effects_written} with mechanical effects)")
    return count


def migrate_items():
    """Phase 3: write the items pack from PARTS.DAT — weapon/armor/potion/item,
    foldered by type. Pre-builds a base-weapon name index so magic variants
    (Sword +1) recover their damage/speed from the base record. Returns count."""
    print("\n=== Items (PARTS.DAT) ===")
    parts = parse_parts()
    if not parts:
        print("  No parts parsed."); return 0
    # Base-weapon index: stripped name → the base record, for recovering the
    # damage/speed of magic variants (Sword +1) whose own damage zone is unread.
    base_weapons = {}
    for part in parts:
        if part.get('damage_type') is not None:
            bn = re.sub(r'\s*[+\-]\d+.*$', '', part['name']).strip().lower()
            base_weapons.setdefault(bn, part)
    db = _open_pack(OUTPUT_PACKS['items'])
    os.makedirs(OUTPUT_IMG_ITEMS, exist_ok=True)
    # Folders by inferred ARS item type (from make_part_item's ftype).
    folders = {}
    for label, sort in [('Weapons', 100000), ('Armor', 200000),
                        ('Potions', 300000), ('Other Items', 400000)]:
        fid = make_id()
        folders[label] = make_compendium_folder(fid, label, 'Item', sort=sort)
    TYPE_FOLDER = {'weapon': 'Weapons', 'armor': 'Armor',
                   'potion': 'Potions', 'item': 'Other Items'}
    count = 0
    effects_written = 0
    skipped_nonitem = 0
    for part in parts:
        if _is_non_item_part(part['name']):   # S&P build data, not equipment
            skipped_nonitem += 1
            continue
        img = extract_equip_icon(part.get('icon_id'), OUTPUT_IMG_ITEMS)
        item, item_effects = make_part_item(part, img, base_weapons=base_weapons)
        bucket = TYPE_FOLDER.get(item.get('type', ''), 'Other Items')
        item['folder'] = folders[bucket]['_id']
        db.put(f'!items!{item["_id"]}'.encode(), json.dumps(item).encode())
        for ef in item_effects:
            db.put(f'!items.effects!{item["_id"]}.{ef["_id"]}'.encode(),
                   json.dumps(ef).encode())
            effects_written += 1
        count += 1
        if count % 1000 == 0:
            print(f"    {count} items...")
    for f in folders.values():
        db.put(f'!folders!{f["_id"]}'.encode(), json.dumps(f).encode())
    db.close()
    print(f"  → {count} items in {len(folders)} folders ({effects_written} AC effects)"
          f"; skipped {skipped_nonitem} non-equipment S&P build records")
    return count


def migrate_spells():
    """Phase 3: write the spells pack from SPELLS.DAT, foldered Wizard/Priest by
    level. Pass A builds each `spell` Item and DROPS any whose description lookup
    came up empty (incomplete page not shipped on this CD). Pass B links each
    reversible's reverse spell as a child via system.itemList. Returns count."""
    print("\n=== Spells (SPELLS.DAT) ===")
    # Use the cached record list so enumerate indices line up with the
    # hard-coded SPELLS.DAT index tables (_SPELL_ICON_INDEX, _SPELL_DESC_HTM_INDEX).
    spells = _spell_records()
    if not spells:
        print("  No spells parsed."); return 0
    db = _open_pack(OUTPUT_PACKS['spells'])
    # Folder hierarchy: Wizard Spells/Level N, Priest Spells/Level N,
    # plus Priest Spells/Quest & Other for the 29 special priest spells
    # (Quest spells from TOM, etc.) whose DAT level isn't in 1..7.
    folders = {}
    wiz_root = make_id(); folders[('root','wizard')] = make_compendium_folder(
        wiz_root, 'Wizard Spells', 'Item', sort=100000)
    pri_root = make_id(); folders[('root','priest')] = make_compendium_folder(
        pri_root, 'Priest Spells', 'Item', sort=200000)
    for lvl in range(1, 10):
        fid = make_id()
        folders[('wizard', lvl)] = make_compendium_folder(
            fid, f'Level {lvl}', 'Item', parent=wiz_root, sort=lvl*100000)
    for lvl in range(1, 8):
        fid = make_id()
        folders[('priest', lvl)] = make_compendium_folder(
            fid, f'Level {lvl}', 'Item', parent=pri_root, sort=lvl*100000)
    other_pri_id = make_id()
    folders[('priest','other')] = make_compendium_folder(
        other_pri_id, 'Quest & Other', 'Item',
        parent=pri_root, sort=900000)

    count = 0
    dropped = 0
    # ── Pass A: build all spell items in memory, drop the description-less,
    # capture by lowercase name so the post-pass can link reversibles.
    items_by_name = {}      # name_lower → (spell_dict, item_dict, folder_key)
    for idx, spell in enumerate(spells):
        item = make_spell_item(spell, desc_override_rel=_SPELL_DESC_HTM_INDEX.get(idx))
        # Drop spells whose description came up empty after every
        # name-normalization attempt — they're SPELLS.DAT records whose
        # source page isn't shipped on this CD-ROM (supplements, Spelljammer,
        # specialty starred variants, etc.). A spell item without prose
        # would be incomplete and misleading on a character sheet.
        if not (item['system'].get('description') or '').strip():
            dropped += 1
            continue
        cls = spell.get('class_type', 'wizard')
        lvl = spell.get('level', 0)
        if (cls, lvl) in folders:
            key = (cls, lvl)
        elif cls == 'priest':
            key = ('priest','other')
        else:
            key = ('root', cls)
        item['folder'] = folders[key]['_id']
        # Use (class, name) so wizard/priest variants of the same spell
        # (e.g., Detect Magic exists in both) get patched independently.
        items_by_name[(cls, spell['name'].lower())] = item

    # ── Pass B: for each spell that's the primary of a true reversible
    # pair, append the reverse as a child item via system.itemList so a
    # PC who memorizes the primary automatically picks up the reverse.
    paired = 0
    for (cls, name_low), item in items_by_name.items():
        rev_name = _reversibles_primary_to_reverse().get(name_low)
        if not rev_name: continue
        rev = items_by_name.get((cls, rev_name))
        if not rev: continue
        item['system']['itemList'].append({
            "id":         rev['_id'],
            "uuid":       f"Item.{rev['_id']}",
            "sourceuuid": f"Compendium.{MODULE_ID}.adnd2-spells.Item.{item['_id']}",
            "type":       "spell",
            "name":       rev['name'],
            "img":        rev['img'],
            "level":      str(rev['system'].get('level', 0)),
        })
        paired += 1

    # ── Pass C: write everything to the LevelDB.
    count = 0
    for item in items_by_name.values():
        db.put(f'!items!{item["_id"]}'.encode(), json.dumps(item).encode())
        count += 1
    for f in folders.values():
        db.put(f'!folders!{f["_id"]}'.encode(), json.dumps(f).encode())
    db.close()
    print(f"  → {count} spells in {len(folders)} folders "
          f"({dropped} dropped — no description source on this CD-ROM)")
    print(f"    {paired} reversible primaries link their reverse as a child item")
    return count


def migrate_psionics():
    """Phase 3: write the powers pack — a `power` Item per PSIONIC.DAT record,
    foldered by discipline (Clairsentience / Psychokinesis / Psychometabolism /
    Psychoportation / Telepathy), plus two extra folders for the 5 psionic attack
    modes and 5 defense modes sourced from dedicated S&P pages.
    Description priority: S&P HTML > DAT text > ''.  Returns count."""
    print("\n=== Psionic Powers (PSIONIC.DAT) ===")
    powers = parse_psionics()
    if not powers:
        print("  No powers parsed."); return 0
    sp_index = _build_sp_psionic_index()
    print(f"  S&P power pages indexed: {len(sp_index)}")
    db = _open_pack(OUTPUT_PACKS['powers'])

    sp_book_dir = os.path.join(SOURCE_BASE, 'SP')
    sp_dir_files = ({f.upper(): f for f in os.listdir(sp_book_dir)}
                   if os.path.isdir(sp_book_dir) else {})

    # One folder per discipline, in canonical order
    disc_folders = {}
    for sort_i, (disc_idx, disc_name) in enumerate(_DISC_NAMES.items()):
        fid = make_id()
        disc_folders[disc_idx] = fid
        folder = make_compendium_folder(fid, disc_name, 'Item', sort=sort_i * 1000)
        db.put(f'!folders!{fid}'.encode(), json.dumps(folder).encode())
    base_sort = len(_DISC_NAMES) * 1000
    # Attack Modes folder (disc=-2) and Defense Modes folder (disc=-3)
    atk_fid = make_id()
    disc_folders[-2] = atk_fid
    db.put(f'!folders!{atk_fid}'.encode(),
           json.dumps(make_compendium_folder(atk_fid, 'Attack Modes', 'Item',
                                             sort=base_sort)).encode())
    def_fid = make_id()
    disc_folders[-3] = def_fid
    db.put(f'!folders!{def_fid}'.encode(),
           json.dumps(make_compendium_folder(def_fid, 'Defense Modes', 'Item',
                                             sort=base_sort + 1000)).encode())
    # Fallback folder for powers without a recognised discipline
    other_fid = make_id()
    disc_folders[-1] = other_fid
    other_folder = make_compendium_folder(other_fid, 'Other', 'Item',
                                          sort=base_sort + 2000)
    db.put(f'!folders!{other_fid}'.encode(), json.dumps(other_folder).encode())

    sp_hits = dat_hits = no_desc = count = 0
    for power in powers:
        name_key = power['name'].lower()
        sp_html  = sp_index.get(name_key, '')
        dat_text = power.get('description', '')
        if sp_html:
            desc = sp_html;  sp_hits += 1
        elif dat_text:
            desc = dat_text; dat_hits += 1
        else:
            desc = '';       no_desc += 1
        item = make_power_item(power, description=desc)
        disc_idx = power.get('discipline', -1)
        item['folder'] = disc_folders.get(disc_idx, other_fid)
        db.put(f'!items!{item["_id"]}'.encode(), json.dumps(item).encode())
        count += 1

    # Attack modes and defense modes: sourced from dedicated S&P pages.
    for rel_files, folder_disc in [(_SP_ATTACK_MODE_FILES, -2),
                                   (_SP_DEFENSE_MODE_FILES, -3)]:
        for sort_i, rel in enumerate(rel_files):
            name, html = _parse_sp_combat_mode_page(rel, sp_dir_files)
            if not name or not html.strip():
                continue
            power_dict = {
                'name': name,
                'discipline': folder_disc,
                'power_score': '',
                'range': '',
                'area_of_effect': '',
            }
            item = make_power_item(power_dict, description=html)
            item['folder'] = disc_folders[folder_disc]
            item['sort'] = sort_i * 1000
            db.put(f'!items!{item["_id"]}'.encode(), json.dumps(item).encode())
            count += 1

    db.close()
    print(f"  → {count} powers in {len(disc_folders)} folders  "
          f"(S&P HTML: {sp_hits}, DAT text: {dat_hits}, none: {no_desc})")
    return count


# ─── Monster taxonomy (genus → broad category) ───────────────────────────────
#
# `system.details.type` is a free-text comma list read by `target.type` /
# `attacker.type` effects on other actors (e.g. a dwarf's "+1 to hit goblinoids"
# or "AC bonus vs giants"). To make those bonuses fire we tag each monster with
# the broader categories it belongs to, not just its own genus.
#
# COPYRIGHT NOTE — no genus names are stored here. The broad groupings below are
# expressed purely as (a) generic taxonomy words (common nouns: "Goblinoid",
# "Giant", "Undead") and (b) integer *record indices* into MONTYPE.DAT. The
# actual creature names are resolved from the user's MONTYPE.DAT at runtime; the
# script hardcodes the grouping LOGIC, never the copyrighted text. Families that
# the data already labels in the record name (e.g. "Giant, Hill", "Dragon, Red")
# are derived at runtime from that comma-prefix and need no index here.
#
# Indices are positions in the ordered list of MONTYPE records that carry both a
# category name and an individual icon (i.e. `list(parse_montype(...).keys())`).
# They were read off the AD&D 2e Core Rules CD-ROM's MONTYPE.DAT. The guard in
# `_build_montype_index_categories` disables index-based tagging if a differently
# built CD-ROM yields a different record count, so a layout shift degrades
# gracefully to genus-only typing rather than mislabelling.
_MONTYPE_EXPECTED_COUNT = 293
_TAXONOMY_INDEX_MAP = {
    # Goblin-kin humanoids — dwarf/gnome "to-hit vs goblinoids" trigger.
    'Goblinoid': (19, 123, 126, 150, 167, 218),
    # Giant-class creatures not already self-labelled "Giant, …" in the record
    # name (those are picked up from the comma-prefix at runtime). Dwarf/gnome
    # "AC bonus vs giant-class" trigger covers ogres, half-ogres, titans, trolls.
    'Giant': (215, 216, 267, 272),
    # The undead — broadly useful for turning and anti-undead effects.
    'Undead': (8, 36, 37, 45, 97, 98, 145, 147, 174, 206, 207,
               223, 227, 233, 242, 245, 246, 247, 252, 276, 279, 284, 292),
}


def _build_montype_index_categories(n_records):
    """Map each MONTYPE record index → list of broad-category labels.

    Returns {} (index-based tagging disabled) if the parsed record count doesn't
    match the layout `_TAXONOMY_INDEX_MAP` was derived from — guards against a
    differently built CD-ROM shifting record order. Stores only integers +
    generic taxonomy words; genus names come from MONTYPE.DAT at runtime."""
    if n_records != _MONTYPE_EXPECTED_COUNT:
        return {}
    idx2cat = {}
    for cat, indices in _TAXONOMY_INDEX_MAP.items():
        for i in indices:
            if 0 <= i < n_records:
                idx2cat.setdefault(i, []).append(cat)
    return idx2cat


def _monster_categories(key, name_to_index, idx2cat):
    """Broad categories for a monster that matched MONTYPE record `key`.

    Combines (a) the comma-prefix family encoded in the record name itself, read
    at runtime (e.g. "Giant, Hill" → "Giant"), with (b) the hardcoded
    index→category logic for groupings absent from the name (e.g. "Undead")."""
    cats = []
    if not key:
        return cats
    if ',' in key:
        fam = key.split(',', 1)[0].strip()
        if fam:
            cats.append(fam)
    idx = name_to_index.get(key)
    if idx is not None:
        for c in idx2cat.get(idx, []):
            if c not in cats:
                cats.append(c)
    return cats


def _match_montype_key(name, display_name, montype_data):
    """Return the MONTYPE record key (original-case name) best matching this
    monster, or None. The same normalization ladder backs both icon lookup and
    taxonomy so they resolve to the *same* record."""
    if not montype_data:
        return None
    by_lower = {k.lower(): k for k in montype_data}     # lower → original key
    def try_key(k):
        if not k: return None
        if k in montype_data: return k
        return by_lower.get(k.lower())

    candidates = []
    for raw in (display_name, name):
        if not raw: continue
        s = raw.strip()
        candidates.append(s)
        # Strip parenthesized qualifier: "Bugbear (individuals)" → "Bugbear"
        no_paren = re.sub(r'\s*\([^)]*\)', '', s).strip()
        if no_paren and no_paren != s:
            candidates.append(no_paren)
        # Normalize "/" → " and "  (Beholder/Beholder-kin → Beholder and Beholder-kin)
        if '/' in s:
            candidates.append(s.replace('/', ' and '))
        # Progressive comma-split: "Beetle, Giant, Boring" → "Beetle, Giant" → "Beetle"
        parts = [p.strip() for p in s.split(',')]
        while len(parts) >= 1:
            candidates.append(', '.join(parts))
            parts.pop()
        # Strip trailing HD/quantity descriptor: "Eye of Deep 10HD" → "Eye of Deep"
        no_hd = re.sub(r'\s+\d+(?:\+\d+)?\s*HD\b.*$', '', s).strip()
        if no_hd and no_hd != s:
            candidates.append(no_hd)

    seen = set()
    for c in candidates:
        cl = c.lower()
        if cl in seen: continue
        seen.add(cl)
        k = try_key(c)
        if k:
            return k
    # Prefix match: any MONTYPE key that is the leading token(s) of the monster name
    full = (display_name or name or '').lower()
    if full:
        best = None
        for key_lower in by_lower:
            if full.startswith(key_lower) and (best is None or len(key_lower) > len(best)):
                best = key_lower
        if best:
            return by_lower[best]
    return None



_mm_embed_index_cache = None


def _build_mm_embed_index():
    """Build {normalized_monster_name → (journal_id, page_id)} from the staged
    adnd2-journals JSON files (written by db.close() before Phase 3 runs).
    Page titles follow the pattern '{Name} (Monstrous Manual)'; we strip the
    suffix and lowercase to get the lookup key."""
    global _mm_embed_index_cache
    if _mm_embed_index_cache is not None:
        return _mm_embed_index_cache
    journal_name = os.path.basename(OUTPUT_DB)        # "adnd2-journals"
    journal_src  = os.path.join(_PACK_SRC_BASE, journal_name)
    index = {}
    if not os.path.isdir(journal_src):
        _mm_embed_index_cache = index
        return index
    for fn in os.listdir(journal_src):
        if not fn.endswith('.json'):
            continue
        try:
            with open(os.path.join(journal_src, fn), encoding='utf-8') as fh:
                doc = json.load(fh)
        except Exception:
            continue
        if doc.get('name') != 'Monster Manual':
            continue
        journal_id = doc.get('_id', '')
        for page in doc.get('pages', []):
            page_name = page.get('name', '')
            page_id   = page.get('_id', '')
            if not page_name or not page_id:
                continue
            # "Argos (Monstrous Manual)" → "argos"
            norm = re.sub(r'\s*\([^)]*\)', '', page_name).strip().lower()
            if norm:
                index[norm] = (journal_id, page_id)
        break   # only one MM journal entry
    _mm_embed_index_cache = index
    print(f"  MM embed index: {len(index)} pages indexed")
    return index


def migrate_monsters():
    """Phase 3: write the monsters pack — an `npc` Actor per MONSTER.DAT record,
    foldered A-Z by name. Resolves MONTYPE icons + broad taxonomy categories and
    passes the CLASS.DAT fighter save table for HD-based saves. Returns count."""
    print("\n=== Monsters (MONSTER.DAT) ===")
    montype_data = parse_montype(_load_dat('MONTYPE.DAT'))
    monsters = parse_monsters()
    if not monsters:
        print("  No monsters parsed."); return 0
    # Fighter save table (Option B): NPCs save as fighters by HD in Variant 2.
    fighter_saves = next((c.get('save_table') for c in parse_classes()
                          if c.get('name', '').lower() == 'fighter'), None)
    if not fighter_saves:
        print("  ! No fighter save table in CLASS.DAT; saves left unset.")
    # Taxonomy lookups: record-index per category name, and the index→category
    # map (empty if the CD-ROM's MONTYPE layout doesn't match the expected one).
    montype_names = list(montype_data.keys())
    name_to_index = {n: i for i, n in enumerate(montype_names)}
    idx2cat = _build_montype_index_categories(len(montype_names))
    if not idx2cat and montype_names:
        print(f"  ! MONTYPE record count {len(montype_names)} != "
              f"{_MONTYPE_EXPECTED_COUNT}; broad-category tagging disabled "
              f"(genus-only types).")
    db = _open_pack(OUTPUT_PACKS['monsters'])
    os.makedirs(OUTPUT_IMG_MONSTERS, exist_ok=True)
    # Folders by first letter of name (A-Z + Other). 1524 monsters across
    # ~300 MONTYPE categories would be too many sub-folders; alphabetical
    # is the practical compromise (matches MM table-of-contents style).
    folders = {}
    for ch in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
        fid = make_id()
        folders[ch] = make_compendium_folder(fid, ch, 'Actor', sort=ord(ch)*1000)
    folders['Other'] = make_compendium_folder(make_id(), 'Other', 'Actor', sort=900000)

    embed_index = _build_mm_embed_index()
    count = 0
    for monster in monsters:
        key  = _match_montype_key(monster.get('name',''), monster.get('display_name',''), montype_data)
        bmp  = montype_data.get(key) if key else None
        img  = extract_monster_icon(bmp, OUTPUT_IMG_MONSTERS) if bmp else None
        cats = _monster_categories(key, name_to_index, idx2cat)
        actor = make_monster_actor(monster, img, categories=cats,
                                   fighter_saves=fighter_saves,
                                   embed_index=embed_index)
        name = actor.get('name','').strip()
        first = name[0].upper() if name else 'Z'
        bucket = first if first in folders else 'Other'
        actor['folder'] = folders[bucket]['_id']
        db.put(f'!actors!{actor["_id"]}'.encode(), json.dumps(actor).encode())
        count += 1
        if count % 500 == 0:
            print(f"    {count} monsters...")
    for f in folders.values():
        db.put(f'!folders!{f["_id"]}'.encode(), json.dumps(f).encode())
    db.close()
    print(f"  → {count} monsters in {len(folders)} folders")
    return count


# ─── Phase 4 — Proficiencies & Skills ────────────────────────────────────────
#
# Three packs of player-facing reference items:
#   • Weapon proficiencies  → proficiency items   (adnd2-proficiencies)
#   • Nonweapon proficiencies → skill items       (adnd2-skills)
#   • Rogue (thief) skills    → skill items       (adnd2-skills)
#
# Sources are all parsed at runtime from PHB/SP HTML — no rules text embedded.

# ── PHB Table 44 — Weapons List (PHB00219.HTM) ───────────────────────────────
_phb_weapons_cache = None
def _load_phb_weapons_table():
    """Parse PHB00219 → list of {name, cost, weight, size, type, speed, dmg_sm, dmg_l}.
    Excludes category-header rows (e.g., 'Bow', 'Sword', 'Polearm' with no stats)."""
    global _phb_weapons_cache
    if _phb_weapons_cache is not None: return _phb_weapons_cache
    path = os.path.join(SOURCE_BASE, 'PHB', 'PHB00219.HTM')
    out = []
    if not os.path.exists(path):
        _phb_weapons_cache = out; return out
    soup = BeautifulSoup(open(path, encoding='cp1252').read(), 'html.parser')
    for tr in soup.find_all('tr'):
        cells = [td.get_text(strip=True) for td in tr.find_all('td')]
        if len(cells) < 7: continue
        name = cells[0]
        if not name or name == 'Item': continue
        # Category-header rows like "Bow", "Sword", "Polearm" have stat cells = "--"
        if all(c.strip() in ('', '--') for c in cells[1:]): continue
        out.append({
            'name':   re.sub(r'\s*\d+$', '', name).strip(),
            'cost':   cells[1], 'weight': cells[2],
            'size':   cells[3], 'type':   cells[4],
            'speed':  cells[5], 'dmg_sm': cells[6],
            'dmg_l':  cells[7] if len(cells) > 7 else '',
        })
    _phb_weapons_cache = out
    return out


_phb_weapon_dice_cache = None
def _phb_weapon_dice(name):
    """Best-effort (normal, large) damage dice for a weapon from PHB Table 44
    (PHB00219), or (None, None). Fills dice the binary PARTS.DAT leaves unparsed
    — notably every P/S polearm (halberd, hook fauchard, …) reads as 0 damage in
    the DAT. Matching strips our 'Polearm,'/'Sword,' prefix, the magic ±N suffix,
    and folds the DAT 'volge' misspelling to 'voulge'. Copyright-clean: the dice
    are read from the user's PHB at runtime, never hardcoded."""
    global _phb_weapon_dice_cache
    if _phb_weapon_dice_cache is None:
        _phb_weapon_dice_cache = {}
        for r in _load_phb_weapons_table():
            _phb_weapon_dice_cache[_phb_weapon_dice_key(r['name'])] = \
                (r['dmg_sm'], r['dmg_l'])
    key = _phb_weapon_dice_key(name)
    hit = _phb_weapon_dice_cache.get(key)
    if not hit:
        # Token-subset fallback: a table row whose words are all present in the
        # weapon name, most specific (longest) winning — so 'Knife, throwing' and
        # 'Club, war' match the 'knife'/'club' rows that an exact/suffix match
        # would miss (base noun is first, not last).
        itoks = set(key.split())
        best_n = 0
        for tk, dice in _phb_weapon_dice_cache.items():
            ttoks = set(tk.split())
            if ttoks and ttoks <= itoks and len(ttoks) > best_n:
                hit, best_n = dice, len(ttoks)
    return hit or (None, None)

def _phb_weapon_dice_key(name):
    n = (name or '').lower()
    n = re.sub(r'\s*[+-]\d+.*$', '', n)        # magic suffix
    n = re.sub(r'^polearm,\s*', '', n)          # our PARTS prefix
    n = n.replace('volge', 'voulge')            # DAT misspelling
    n = re.sub(r'[^a-z0-9 ]', ' ', n)
    return ' '.join(n.split())


# ── Combat & Tactics Master Weapons Table (CT00374) — dice fallback ──────────
# Far more complete than PHB Table 44: covers exotic/primitive weapons (stone
# axe, no-dachi, spade, scythe, …) the PHB omits. Its hierarchy is category →
# variant, shown by a 3-space indent on the variant's name cell ("Axe" header,
# then "   Battle"/"   Stone"). We rebuild the full identity (category + variant)
# from that indent so "Stone" under "Axe" resolves to our PARTS "Axe, stone".
# Damage lives in the last-3 / last-2 columns (Sm-Med / Large) — robust across
# the table's 3 sub-tables, which differ in column count. Copyright-clean: dice
# read from the user's CT files at runtime.
_CT_DICE_RE = re.compile(r'^\d+d\d+([+-]\d+)?$')
_CT_FOOTNOTE_TOKENS = {'h', 's', 'm', 'b', 'k'}   # trailing superscript markers

def _ct_name_tokens(s):
    """Word tokens of a CT weapon name, dropping the trailing footnote markers
    (single h/s/m/b/k letters or digits) the table appends as superscripts."""
    s = re.sub(r'[^a-z0-9]+', ' ', (s or '').lower())
    return [t for t in s.split()
            if t not in _CT_FOOTNOTE_TOKENS and not t.isdigit()]

_ct_master_weapons_cache = None
def _load_ct_master_weapons():
    """Parse CT00374 → list of (token_set, normal, large). Variant rows (indent
    > 0) carry their category's tokens too; indented dice spill from the nearest
    preceding non-indented header. Only rows with two real dice values are kept."""
    global _ct_master_weapons_cache
    if _ct_master_weapons_cache is not None:
        return _ct_master_weapons_cache
    out = []
    path = os.path.join(SOURCE_BASE, 'CT', 'CT00374.HTM')
    if not os.path.exists(path):
        _ct_master_weapons_cache = out
        return out
    soup = BeautifulSoup(open(path, encoding='latin-1').read(), 'html.parser')
    for table in soup.find_all('table'):
        category = None
        for tr in table.find_all('tr'):
            tds = tr.find_all('td')
            if not tds:
                continue
            raw = tds[0].get_text(' ', strip=False)
            indent = len(raw) - len(raw.lstrip())
            name = tds[0].get_text(strip=True)
            if not name or name == 'Weapon':
                continue
            cells = [td.get_text(' ', strip=True) for td in tds]
            while cells and cells[-1] == '':
                cells.pop()
            dice = None
            if (len(cells) >= 3 and _CT_DICE_RE.match(cells[-3])
                    and _CT_DICE_RE.match(cells[-2])):
                dice = (cells[-3], cells[-2])
            nt = _ct_name_tokens(name)
            if indent == 0:
                category = nt
                if dice:
                    out.append((set(nt), dice[0], dice[1]))
            elif dice:
                base = set(category or []) | set(nt)
                out.append((base, dice[0], dice[1]))
    _ct_master_weapons_cache = out
    return out

def _ct_weapon_dice(name):
    """Best-effort (normal, large) dice for a PARTS weapon from the CT master
    table, or (None, None). A CT entry matches when every one of its tokens is
    covered by a PARTS-name token (exact, or the PARTS token is a ≥4-char prefix
    — absorbing CT's glued footnotes like 'Billh'); the most specific (largest)
    matching entry wins."""
    # Keep the PARTS category token (e.g. 'polearm') — unlike the PHB key, CT
    # entries are identified by category + variant, so both must be present.
    clean = re.sub(r'\s*[+-]\d+.*$', '', name or '').replace('volge', 'voulge')
    P = set(_ct_name_tokens(clean))
    best, best_n = None, -1
    for toks, n, l in _load_ct_master_weapons():
        if all(any(t == p or (len(p) >= 4 and t.startswith(p)) for p in P)
               for t in toks) and len(toks) > best_n:
            best, best_n = (n, l), len(toks)
    return best or (None, None)


# ── C&T weapon / armor description glossaries (CT00375 / CT00378) ─────────────
# These chapters describe each piece of gear as an inline glossary: a bold
# "Name." header begins an entry, the prose runs until the next bold header, and
# green (#008000) cross-reference links interrupt the flow (decomposed first).
# Used as the last-resort description source for weapons/armor the PHB/DMG/AEG
# don't cover. Copyright-clean: prose read from the user's CT files at runtime.
_CT_DESC_STOP = {'of', 'the', 'a', 'and', 'vs', 'blending'}
_CT_DESC_HEAD = re.compile(r"^([A-Z][A-Za-z][\w ,/'\-]{0,40})\.\s*$")

def _ct_desc_words(s):
    s = re.sub(r'\s*\(.*?\)', '', s or '')        # drop "(AC 3)" parentheticals
    s = re.sub(r'\s*[+-]\d.*$', '', s)            # drop magic ±N suffix
    s = re.sub(r'[^a-z0-9]+', ' ', s.lower())
    return [w for w in s.split() if w not in _CT_DESC_STOP and len(w) > 1]

def _ct_contig(needle, hay):
    n = len(needle)
    return bool(n) and n <= len(hay) and \
        any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))

_ct_item_desc_cache = {}
def _load_ct_item_descriptions(kind):
    """Parse a CT glossary → list of (word_tuple, html) sorted longest-name-first
    (so the most specific entry wins). kind: 'weapon'→CT00375, 'armor'→CT00378."""
    if kind in _ct_item_desc_cache:
        return _ct_item_desc_cache[kind]
    fname = 'CT00375.HTM' if kind == 'weapon' else 'CT00378.HTM'
    path = os.path.join(SOURCE_BASE, 'CT', fname)
    out = []
    if os.path.exists(path):
        soup = BeautifulSoup(open(path, encoding='latin-1').read(), 'html.parser')
        for f in soup.find_all('font'):
            if str(f.get('color', '')).lower() == '#008000':
                f.decompose()
        body = soup.body or soup
        heads = [(b, m.group(1).strip())
                 for b in body.find_all('b')
                 for m in [_CT_DESC_HEAD.match(b.get_text(' ', strip=True))] if m]
        for i, (b, name) in enumerate(heads):
            stop = heads[i + 1][0] if i + 1 < len(heads) else None
            texts = []
            for s in b.next_elements:
                if s is stop:
                    break
                if isinstance(s, str):
                    texts.append(s)
            desc = ' '.join(' '.join(texts).split()).lstrip('. ').strip()
            desc = re.sub(r'^' + re.escape(name) + r'\.\s*', '', desc, flags=re.I)
            if len(desc) >= 25:
                out.append((tuple(_ct_desc_words(name)), f'<p>{desc}</p>'))
        out.sort(key=lambda e: -len(e[0]))
    _ct_item_desc_cache[kind] = out
    return out

def _ct_item_description(name, kind):
    """Best contiguous-phrase match of a PARTS weapon/armor name against the CT
    glossary, or ''. Tries the name as-is, the comma-swapped form ('Axe, battle'
    → 'battle axe'), and the post-comma variant alone; the longest matching CT
    name wins (entries are pre-sorted longest-first)."""
    forms = [_ct_desc_words(name)]
    if ',' in name:
        head, tail = name.split(',', 1)
        forms.append(_ct_desc_words(tail) + _ct_desc_words(head))
        forms.append(_ct_desc_words(tail))
    for toks, html in _load_ct_item_descriptions(kind):
        if any(_ct_contig(list(toks), f) for f in forms):
            return html
    return ''


# ── DMG potion descriptions + heal actions (the "X-- Potion" glossary) ───────
# Each potion has its own DMG page titled "<Type>-- Potion" (e.g. "Healing--
# Potion", "Oil of Elemental Invulnerability-- Potion"). PARTS names them
# "Potion of <Type>" / "Oil of <Type>" with parenthetical or comma variants, so
# we match on a token set that drops the wrapper words (potion/oil/elixir/of) and
# the variant qualifiers. HP-restoring potions also yield a rollable heal formula
# from "restores NdN[+N] hit points". Copyright-clean: read from the user's DMG.
_POTION_DROP_WORDS = {'potion', 'oil', 'elixir', 'philter', 'philtre',
                      'of', 'the', 'a', 'and'}
_POTION_VARIANT_RE = re.compile(
    r',\s*(air|water|fire|earth|red|blue|green|gold|silver|copper|brass|bronze'
    r'|white|black)\b.*$', re.I)
_POTION_HEAL_RE = re.compile(
    r'restores?\s+(\d+d\d+(?:\s*[+-]\s*\d+)?)\s+hit\s+points', re.I)

def _potion_tokens(name):
    s = re.sub(r'\s*\(.*?\)', '', (name or '').lower())   # drop "(Blue)"/"(fish)"
    s = _POTION_VARIANT_RE.sub('', s)                      # drop ", Air"/", red"
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return frozenset(w for w in s.split()
                     if w not in _POTION_DROP_WORDS and len(w) > 1)

_dmg_potion_index_cache = None
def _load_dmg_potion_index():
    """{token_set: filepath} for every DMG "<Type>-- Potion" page."""
    global _dmg_potion_index_cache
    if _dmg_potion_index_cache is not None:
        return _dmg_potion_index_cache
    out = {}
    book_dir = os.path.join(SOURCE_BASE, 'DMG')
    if os.path.isdir(book_dir):
        for fn in os.listdir(book_dir):
            if not (fn.upper().startswith('DMG') and fn.upper().endswith('.HTM')):
                continue
            fp = os.path.join(book_dir, fn)
            try:
                title = extract_title(fp).strip()
            except Exception:
                continue
            m = re.match(r'(.+?)--\s*Potion\b', title)
            if m:
                ts = _potion_tokens(m.group(1))
                if ts and ts not in out:
                    out[ts] = fp
    _dmg_potion_index_cache = out
    return out

def _potion_description_and_heal(name):
    """(description_html, heal_formula) for a potion from its DMG page, or
    ('', ''). The token set matches exactly or by the most specific subset; the
    heal formula is filled only for HP-restoring potions."""
    idx = _load_dmg_potion_index()
    q = _potion_tokens(re.sub(r'^potion of\s+', '', name or '', flags=re.I))
    fp = idx.get(q)
    if not fp:
        best, bn = None, 0
        for ts, path in idx.items():
            if ts and ts <= q and len(ts) > bn:
                best, bn = path, len(ts)
        fp = best
    if not fp:
        return '', ''
    src = {f.upper(): f for f in os.listdir(os.path.dirname(fp))}
    try:
        html = clean_html_file(fp, 'DMG', src)
    except Exception:
        return '', ''
    text = ' '.join(BeautifulSoup(open(fp, encoding='latin-1').read(),
                                  'html.parser').get_text(' ').split())
    m = _POTION_HEAL_RE.search(text)
    return html, (re.sub(r'\s+', '', m.group(1)) if m else '')


# ── Mechanics parsed from item/potion description prose → rollable actions ────
# When the rules text states dice ("restores 2d4+2 hit points", "inflicts 6d6
# points of damage"), generate the matching heal/damage action so the item is
# rollable, not just a trigger. Guards against the two ways "N points of damage"
# misleads: reader-penalty tomes, and ingestion side-effects ("consumption
# causes …"). Save/duration are left to the description (no negate/half data).
_MECH_DICE = r'(\d+d\d+(?:\s*[+-]\s*\d+)?)'
_MECH_HEAL_RE = re.compile(
    r'(?:restores?|cures?|heals?|regenerates?)\s+(?:up to\s+)?' + _MECH_DICE +
    r'\s+(?:hit\s+points|points?\s+of\s+(?:damage|wounds))', re.I)
_MECH_DMG_RE = re.compile(
    r'(?:inflicts?|deals?|causes?|does|strikes? for)\s+' + _MECH_DICE +
    r'\s+(?:points?\s+of\s+)?([a-z]+\s+)?damage', re.I)
_MECH_DMG_EXNAME = re.compile(r'\b(book|tome|manual|libram)\b', re.I)
_MECH_DMG_INGEST = re.compile(
    r'consumption|spoonful|if (?:drunk|eaten|swallow)|mistaken for', re.I)
_MECH_DMG_TYPES = {'fire': 'fire', 'cold': 'cold', 'frost': 'cold', 'acid': 'acid',
                   'lightning': 'lightning', 'electrical': 'lightning',
                   'electricity': 'lightning', 'force': 'force', 'poison': 'poison'}

def _item_action_mechanics(description, name):
    """(heal_formula, damage_formula, damage_type) parsed from an item/potion
    description, each '' when absent."""
    text = ' '.join(BeautifulSoup(description or '', 'html.parser')
                    .get_text(' ').split())
    heal = ''
    m = _MECH_HEAL_RE.search(text)
    if m:
        heal = re.sub(r'\s+', '', m.group(1))
    dmg = dtype = ''
    if not _MECH_DMG_EXNAME.search(name):
        for m in _MECH_DMG_RE.finditer(text):
            seg = text[max(0, m.start() - 40):m.start()]
            if re.search(r'restores?|cures?|heals?|regenerat', seg, re.I):
                continue
            if _MECH_DMG_INGEST.search(seg):
                continue
            dmg = re.sub(r'\s+', '', m.group(1))
            dtype = _MECH_DMG_TYPES.get((m.group(2) or '').strip().lower(), '')
            break
    return heal, dmg, dtype

_ACTION_DMG_ICON = 'systems/ars/icons/general/DamageColor.png'
_ACTION_HEAL_ICON = 'icons/svg/heal.svg'

def _mechanic_actions(description, name):
    """Heal/damage action objects parsed from the description (possibly empty)."""
    heal, dmg, dtype = _item_action_mechanics(description, name)
    acts = []
    if heal:
        acts.append(_make_action('Heal', type_='heal', targeting='self',
                                 formula=heal, img=_ACTION_HEAL_ICON))
    if dmg:
        acts.append(_make_action('Damage', type_='damage', targeting='single',
                                 formula=dmg, damage_type=dtype,
                                 img=_ACTION_DMG_ICON))
    return acts


# ── Ranged-weapon typing (PHB Table 45 — Missile Weapon Ranges, PHB00220) ────
# Word-boundary keywords for launcher weapons (bows, crossbows, slings, blowgun,
# arquebus). \b prevents 'bow' matching inside 'elbow'/'rainbow'/'bowl'/'bowyer'.
# This is a copyright-clean reference (a name pattern, not rules data); the range
# *values* are read from PHB Table 45 at runtime, never hardcoded.
_RANGED_WEAPON_RE = re.compile(
    r'\b(crossbow|longbow|shortbow|bow|sling|blowgun|arquebus)\b', re.I)

# Ammo qualifiers stripped from a Table-45 weapon name to reach its base weapon
# ("Sling bullet"/"Sling stone" → "sling"; "Comp. long bow, flight arrow" → the
# composite long bow). Order matters: two-word forms before their single words.
_MISSILE_AMMO_WORDS = ['flight arrow', 'sheaf arrow', 'arrow',
                       'quarrel', 'bolt', 'bullet', 'stone', 'needle']

def _missile_name_tokens(s):
    """Normalize a weapon name to a token set for subset matching. Folds the
    'Comp.' abbreviation and the one-word 'longbow'/'shortbow' spellings used in
    Table 45 so they line up with the PARTS.DAT 'Composite long bow' style."""
    s = s.lower().replace('comp.', 'composite')
    s = re.sub(r'\blongbow\b', 'long bow', s)
    s = re.sub(r'\bshortbow\b', 'short bow', s)
    s = re.sub(r'[^a-z0-9 ]', ' ', s)
    return set(t for t in s.split() if t)

_phb_missile_ranges_cache = None
def _load_phb_missile_ranges():
    """Parse PHB00220 (Table 45, Missile Weapon Ranges) → list of
    (base_token_set, {short, medium, long}) records. Multi-row spillover (a
    weapon whose name wraps onto a stat row, e.g. 'Comp. long bow,' + 'flight
    arrow') is rejoined; the first profile per base weapon wins (flight-arrow
    ranges precede sheaf-arrow). '--' (no range at that band) becomes ''. All
    values are sourced from the user's PHB at runtime."""
    global _phb_missile_ranges_cache
    if _phb_missile_ranges_cache is not None:
        return _phb_missile_ranges_cache
    path = os.path.join(SOURCE_BASE, 'PHB', 'PHB00220.HTM')
    out = []
    if not os.path.exists(path):
        _phb_missile_ranges_cache = out
        return out
    soup = BeautifulSoup(open(path, encoding='cp1252').read(), 'html.parser')
    pending = None
    seen = set()
    for tr in soup.find_all('tr'):
        cells = [td.get_text(strip=True) for td in tr.find_all('td')]
        if len(cells) < 5:
            continue
        name, _rof, s, m, l = cells[0], cells[1], cells[2], cells[3], cells[4]
        if name in ('', 'Weapon') or s == 'S':       # header rows
            continue
        if not (s or m or l):                          # name-only continuation
            pending = name.rstrip(',').strip()
            continue
        full = (pending + ' ' + name).strip() if pending else name
        pending = None
        base = full.lower()
        for ammo in _MISSILE_AMMO_WORDS:
            base = re.sub(r',?\s*' + re.escape(ammo) + r'\b', '', base)
        base = base.strip().strip(',').strip()
        toks = frozenset(_missile_name_tokens(base))
        if not toks or toks in seen:
            continue
        seen.add(toks)
        rng = {'short':  s if s != '--' else '',
               'medium': m if m != '--' else '',
               'long':   l if l != '--' else ''}
        out.append((toks, rng))
    _phb_missile_ranges_cache = out
    return out

def _missile_range_for(name):
    """Best-effort PHB Table-45 range bands for a weapon name, or None. Matches a
    table entry whose tokens are a subset of the weapon's tokens and prefers the
    most specific (largest token set) — so 'Staff sling' picks the staff-sling
    row over the plain 'sling' row. Returns None when nothing matches; callers
    leave the range blank rather than fabricate one."""
    itoks = _missile_name_tokens(name)
    best, best_n = None, -1
    for toks, rng in _load_phb_missile_ranges():
        if toks <= itoks and len(toks) > best_n:
            best, best_n = rng, len(toks)
    return best


# ── PHB Table 37 — Nonweapon Proficiency Groups (PHB00133.HTM) ───────────────
_phb_nwp_table_cache = None
def _load_phb_nwp_table():
    """Parse PHB00133 → {name_lower: {groups: set, slots, ability, modifier, anchor}}.
    Same proficiency name may appear in multiple groups (General/Priest/Rogue/Warrior/Wizard);
    we union the group set and keep the first (slots, ability, modifier) seen."""
    global _phb_nwp_table_cache
    if _phb_nwp_table_cache is not None: return _phb_nwp_table_cache
    path = os.path.join(SOURCE_BASE, 'PHB', 'PHB00133.HTM')
    out = {}
    if not os.path.exists(path):
        _phb_nwp_table_cache = out; return out
    soup = BeautifulSoup(open(path, encoding='cp1252').read(), 'html.parser')
    # Group sections are marked by red SIZE=3 bold FONT preceding a table.
    cur_group = None
    for node in soup.find_all(['font', 'table']):
        if node.name == 'font' and str(node.get('color', '')).lower() == '#ff0000':
            txt = node.get_text(strip=True).rstrip(':').title()
            if txt in ('General', 'Priest', 'Rogue', 'Warrior', 'Wizard'):
                cur_group = txt
            continue
        if node.name == 'table' and cur_group:
            for tr in node.find_all('tr'):
                cells = [td.get_text(strip=True) for td in tr.find_all('td')]
                if len(cells) < 4: continue
                name = cells[0]
                if name in ('', 'Proficiency') or name.startswith('# of'): continue
                try: slots = int(cells[1])
                except ValueError: continue
                ability = cells[2]
                mod = cells[3].replace(' ','')
                # Find anchor file from the <a href> in this row
                href = None
                for a in tr.find_all('a', href=True):
                    if str(a.get('href', '')).endswith('.htm') and '#' not in a['href']:
                        href = a['href']; break
                key = name.lower()
                if key not in out:
                    out[key] = {'name': name, 'groups': set(), 'slots': slots,
                                'ability': ability, 'modifier': mod, 'anchor': href}
                out[key]['groups'].add(cur_group)
    _phb_nwp_table_cache = out
    return out


# ── SP Table 45 — Nonweapon Proficiency Groups (SP00456.HTM) ─────────────────
_sp_nwp_table_cache = None
def _load_sp_nwp_table():
    """Parse SP00456 (Table 45) → {name_lower: {groups: set, cp_cost, initial, ability, anchor}}.

    In SP00456 the group headers (GENERAL, PRIEST, ROGUE, WARRIOR, WIZARD) are embedded
    as table rows whose first cell contains a red bold font — not as separate elements
    between tables. We iterate the single table's <tr> rows, detect group-header rows
    by the red+bold font in their first cell, and assign subsequent skill rows to that
    group until the next header."""
    global _sp_nwp_table_cache
    if _sp_nwp_table_cache is not None: return _sp_nwp_table_cache
    path = os.path.join(SOURCE_BASE, 'SP', 'SP00456.HTM')
    out = {}
    if not os.path.exists(path):
        _sp_nwp_table_cache = out; return out
    soup = BeautifulSoup(open(path, encoding='cp1252').read(), 'html.parser')
    # Skills before the first explicit group header belong to General
    cur_group = 'General'
    _GROUPS = {'General', 'Priest', 'Rogue', 'Warrior', 'Wizard', 'Psionicist'}
    for table in soup.find_all('table'):
        for tr in table.find_all('tr'):
            cells = tr.find_all('td')
            if not cells:
                continue
            # Detect group-header row: first cell contains a red bold font
            first_cell = cells[0]
            red_bold = next(
                (f for f in first_cell.find_all('font')
                 if str(f.get('color', '')).lower() == '#ff0000' and f.find('b')),
                None)
            if red_bold:
                raw = red_bold.get_text(strip=True).rstrip(':').title()
                if raw in _GROUPS:
                    cur_group = raw
                continue
            if not cur_group:
                continue
            cell_texts = [c.get_text(strip=True) for c in cells]
            if len(cell_texts) < 4:
                continue
            name = cell_texts[0]
            if name in ('', 'Proficiency') or 'Cost' in cell_texts[1]:
                continue
            try:
                cp = int(cell_texts[1])
            except ValueError:
                continue
            try:
                init = int(cell_texts[2])
            except ValueError:
                init = 0
            ability = cell_texts[3]
            href = None
            for a in tr.find_all('a', href=True):
                raw_href = str(a.get('href', ''))
                if raw_href.endswith('.htm') or '#' in raw_href:
                    href = raw_href.split('#')[0]
                    break
            key = name.lower()
            if key not in out:
                out[key] = {'name': name, 'groups': set(), 'cp_cost': cp,
                            'initial': init, 'ability': ability, 'anchor': href}
            out[key]['groups'].add(cur_group)
    _sp_nwp_table_cache = out
    return out


# ── Description extractor for individual proficiency pages ──────────────────
def _load_sp_psionicist_nwps():
    """Scan SP*.HTM for Psionicist-specific NWPs (individual pages with title
    '{Name}-- Psionicist Nonweapon Proficiency (Skills & Powers)').
    Returns [{name, description}] in file order."""
    book_dir = os.path.join(SOURCE_BASE, 'SP')
    if not os.path.isdir(book_dir):
        return []
    src_files = {f.upper(): f for f in os.listdir(book_dir)}
    out = []
    for fn in sorted(os.listdir(book_dir)):
        if not (fn.upper().startswith('SP') and fn.upper().endswith('.HTM')):
            continue
        path = os.path.join(book_dir, fn)
        try:
            with open(path, encoding='cp1252') as fh:
                content = fh.read()
        except Exception:
            continue
        if '<TITLE>' not in content:
            continue
        title = content.split('<TITLE>')[1].split('</TITLE>')[0].strip()
        if 'Psionicist Nonweapon Proficiency' not in title:
            continue
        # Name is everything before the first "--"
        name = title.split('--')[0].strip()
        desc = clean_html_file(path, 'SP', src_files)
        if name and desc.strip():
            out.append({'name': name, 'description': desc})
    return out


def _extract_proficiency_description(book_key, anchor_file):
    """Read one PHB/SP nonweapon-proficiency page and return cleaned HTML.
    Reuses _clean_html_body, dropping the leading bold label and trailing TOC link."""
    if not anchor_file: return ''
    book_dir = os.path.join(SOURCE_BASE, book_key)
    path = os.path.join(book_dir, anchor_file.upper())
    if not os.path.exists(path):
        path = os.path.join(book_dir, anchor_file)
    if not os.path.exists(path): return ''
    try:
        src_dir_files = {f.upper(): f for f in os.listdir(book_dir)}
        return clean_html_file(path, book_key, src_dir_files)
    except Exception:
        return ''


# ── PHB00100 — Thief Skill Explanations ──────────────────────────────────────
_phb_thief_skill_desc_cache = None
def _load_phb_thief_skill_descriptions():
    """Parse PHB00100 → {skill_name_lower: html_description}. Each skill is
    introduced by a red bold FONT label like '<B>Pick Pockets:</B>' followed
    by prose; the next red bold FONT marks the next skill."""
    global _phb_thief_skill_desc_cache
    if _phb_thief_skill_desc_cache is not None: return _phb_thief_skill_desc_cache
    path = os.path.join(SOURCE_BASE, 'PHB', 'PHB00100.HTM')
    out = {}
    if not os.path.exists(path):
        _phb_thief_skill_desc_cache = out; return out
    raw = open(path, encoding='cp1252').read()
    # Find red+size3+bold labels with a colon — skill headers
    pattern = re.compile(
        r'<FONT[^>]*COLOR="#ff0000"[^>]*SIZE="3"[^>]*>\s*<B>\s*'
        r'([^<:]+?):\s*</B>\s*</FONT>',
        re.IGNORECASE)
    anchors = [(m.group(1).strip(), m.start(), m.end()) for m in pattern.finditer(raw)]
    for i, (name, _s, body_start) in enumerate(anchors):
        body_end = anchors[i+1][1] if i+1 < len(anchors) else len(raw)
        chunk = raw[body_start:body_end]
        # Strip trailing nav/TOC content
        wrapped = f'<html><body>{chunk}</body></html>'
        soup = BeautifulSoup(wrapped, 'html.parser')
        body = soup.find('body') or soup
        try:
            html = _clean_html_body(body, soup, 'PHB', {})
        except Exception:
            html = ''
        if html and len(html) > 40:
            out[name.lower()] = html
    _phb_thief_skill_desc_cache = out
    return out


# ── PHB Table 26 — Thief Skill Base Scores (PHB00952.HTM) ────────────────────
_phb_thief_base_cache = None
def _load_phb_thief_skill_base_scores():
    """Parse PHB00952 → {skill_name_lower: percent_int}."""
    global _phb_thief_base_cache
    if _phb_thief_base_cache is not None: return _phb_thief_base_cache
    path = os.path.join(SOURCE_BASE, 'PHB', 'PHB00952.HTM')
    out = {}
    if not os.path.exists(path):
        _phb_thief_base_cache = out; return out
    soup = BeautifulSoup(open(path, encoding='cp1252').read(), 'html.parser')
    for tr in soup.find_all('tr'):
        cells = [td.get_text(strip=True) for td in tr.find_all('td')]
        if len(cells) != 2: continue
        name = cells[0]
        m = re.search(r'(\d+)\s*%', cells[1])
        if not m or name == 'Skill': continue
        out[name.lower()] = int(m.group(1))
    _phb_thief_base_cache = out
    return out


# ── S&P Table 27 — Thief Skill Base Scores (SP00073.HTM) ─────────────────────
# Same 8 PHB skills + 5 expansions (Detect magic, Detect illusion, Bribe,
# Tunneling, Escape bonds). Names match PHB where they coincide.
_sp_thief_base_cache_full = None
def _load_sp_thief_skill_base_scores():
    """Parse SP00073 → {skill_name_lower: percent_int}, all 13 S&P thief skills."""
    global _sp_thief_base_cache_full
    if _sp_thief_base_cache_full is not None: return _sp_thief_base_cache_full
    path = os.path.join(SOURCE_BASE, 'SP', 'SP00073.HTM')
    out = {}
    if not os.path.exists(path):
        _sp_thief_base_cache_full = out; return out
    soup = BeautifulSoup(open(path, encoding='cp1252').read(), 'html.parser')
    for tr in soup.find_all('tr'):
        cells = [td.get_text(strip=True) for td in tr.find_all('td')]
        if len(cells) < 2: continue
        cells = [c for c in cells if c]  # drop blank leading cells
        if len(cells) < 2: continue
        name = cells[0]
        m = re.search(r'(\d+)\s*%', cells[1])
        if not m or name.lower() in ('skill','base chance'): continue
        out[name.lower()] = int(m.group(1))
    _sp_thief_base_cache_full = out
    return out


def _thief_canonical_scores():
    """Thief level-1 thieving-skill base scores keyed lowercase. S&P (SP00073,
    13 skills) is the reference superset; PHB (8 skills) is identical for the
    shared ones. Used both to build the rogue skills and to decide whether a
    class variant (Bard/Ranger) differs from the Thief baseline."""
    base = dict(_load_phb_thief_skill_base_scores())
    for k, v in _load_sp_thief_skill_base_scores().items():
        base.setdefault(k, v)
    return base


def _parse_two_row_header_pct_table(path, data_first_cell=None):
    """Parse a PHB ability table whose column names span two header rows
    (e.g. 'Climb'/'Walls', 'Hide in'/'Shadows') and return
    {reconstructed_column_name_lower: percent_int} for one value row.

    If `data_first_cell` is given (e.g. '1' for the Ranger level-1 row), the
    value row is the first data row whose first cell equals it, and the two rows
    above it are the header. Otherwise the value row is the first row whose
    non-empty cells are all 'N%', with the two rows above as the header.
    Only columns whose reconstructed name matches a known thieving-skill anchor
    are kept. All names/numbers are read from the file at runtime."""
    out = {}
    if not os.path.exists(path):
        return out
    soup = BeautifulSoup(open(path, encoding='cp1252').read(), 'html.parser')
    rows = []
    for tr in soup.find_all('tr'):
        rows.append([td.get_text(strip=True) for td in tr.find_all('td')])
    val_idx = None
    if data_first_cell is not None:
        for i, r in enumerate(rows):
            if r and r[0] == data_first_cell:
                val_idx = i; break
    else:
        for i, r in enumerate(rows):
            nz = [c for c in r if c]
            if nz and all(re.fullmatch(r'\d+%', c) for c in nz):
                val_idx = i; break
    if val_idx is None or val_idx < 2:
        return out
    h1, h2, vals = rows[val_idx - 2], rows[val_idx - 1], rows[val_idx]
    anchors = {s.lower() for s in _THIEVING_SKILL_NAMES}
    for i, v in enumerate(vals):
        m = re.fullmatch(r'(\d+)%', v or '')
        if not m:
            continue
        name = ' '.join(p for p in [h1[i] if i < len(h1) else '',
                                    h2[i] if i < len(h2) else ''] if p).strip()
        if name.lower() in anchors:
            out[name.lower()] = int(m.group(1))
    return out


# ── PHB Table 33 — Bard thieving-skill base scores (PHB00958.HTM) ────────────
_bard_thief_base_cache = None
def _load_bard_thief_skill_base_scores():
    """Parse PHB00958 (Table 33: Bard Abilities) → {skill_lower: pct}. The bard's
    level-1 base scores for Climb Walls / Detect Noise / Pick Pockets / Read
    Languages differ from the Thief's; sourced at runtime."""
    global _bard_thief_base_cache
    if _bard_thief_base_cache is None:
        _bard_thief_base_cache = _parse_two_row_header_pct_table(
            os.path.join(SOURCE_BASE, 'PHB', 'PHB00958.HTM'))
    return _bard_thief_base_cache


# ── PHB Table 18 — Ranger thieving-skill base scores (PHB00948.HTM) ──────────
_ranger_thief_base_cache = None
def _load_ranger_thief_skill_base_scores():
    """Parse PHB00948 (Table 18: Ranger Abilities) → {skill_lower: pct} for the
    level-1 row. The ranger's Hide in Shadows / Move Silently base scores differ
    from the Thief's; sourced at runtime."""
    global _ranger_thief_base_cache
    if _ranger_thief_base_cache is None:
        _ranger_thief_base_cache = _parse_two_row_header_pct_table(
            os.path.join(SOURCE_BASE, 'PHB', 'PHB00948.HTM'), data_first_cell='1')
    return _ranger_thief_base_cache


# ── SP00068 — Thief class page, contains descriptions of all 13 rogue skills ─
_sp_thief_skill_desc_cache = None
def _load_sp_thief_skill_descriptions():
    """Parse SP00068 → {skill_name_lower: html_description}. Each skill is
    a red SIZE=3 bold label like '<B>Bribe* (5):</B>' followed by prose.
    Strip the trailing '*' and '(N)' CP-cost marker from the label."""
    global _sp_thief_skill_desc_cache
    if _sp_thief_skill_desc_cache is not None: return _sp_thief_skill_desc_cache
    path = os.path.join(SOURCE_BASE, 'SP', 'SP00068.HTM')
    out = {}
    if not os.path.exists(path):
        _sp_thief_skill_desc_cache = out; return out
    raw = open(path, encoding='cp1252').read()
    pattern = re.compile(
        r'<FONT[^>]*COLOR="#ff0000"[^>]*SIZE="3"[^>]*>\s*<B>\s*'
        r'([^<:]+?):\s*</B>\s*</FONT>',
        re.IGNORECASE)
    anchors = [(m.group(1).strip(), m.start(), m.end()) for m in pattern.finditer(raw)]
    for i, (name, _s, body_start) in enumerate(anchors):
        body_end = anchors[i+1][1] if i+1 < len(anchors) else len(raw)
        # Strip "*" and "(N)" suffix from label
        clean_name = re.sub(r'\s*\*\s*', '', name)
        clean_name = re.sub(r'\s*\([^)]+\)\s*$', '', clean_name).strip()
        chunk = raw[body_start:body_end]
        wrapped = f'<html><body>{chunk}</body></html>'
        soup = BeautifulSoup(wrapped, 'html.parser')
        body = soup.find('body') or soup
        try:
            html = _clean_html_body(body, soup, 'SP', {})
        except Exception:
            html = ''
        if html and len(html) > 40:
            out[clean_name.lower()] = html
    _sp_thief_skill_desc_cache = out
    return out


# ── SP00278 — Table 49: Weapon Groups (tight groups within broad sections) ──
_sp_weapon_groups_cache = None
def _load_sp_weapon_groups():
    """Parse SP00278 → list of dicts {tight_group, broad_group, weapons[]}.
    Tight groups: italic-tagged sub-headings inside broad sections, OR
    italic-bold red headings that act as single tight groups (Bows, Crossbows,
    Daggers & Knives, Lances, Chain & Rope Weapons, Martial Arts Weapons).
    'Unrelated' italic blocks are skipped (rule excludes them from familiarity)."""
    global _sp_weapon_groups_cache
    if _sp_weapon_groups_cache is not None: return _sp_weapon_groups_cache
    path = os.path.join(SOURCE_BASE, 'SP', 'SP00278.HTM')
    out = []
    if not os.path.exists(path):
        _sp_weapon_groups_cache = out; return out
    soup = BeautifulSoup(open(path, encoding='cp1252').read(), 'html.parser')

    # Walk the document linearly, tracking current broad group.
    current_broad = None
    body = soup.find('body') or soup
    # Each red SIZE=3 bold <B> is a broad header. Inside it, optional <I>
    # marks tight sub-group label; trailing plain text is the weapon list.
    # Single-tight-group sections have <B><I>NAME</I></B> as the header and
    # the weapon list directly under it.
    for font in body.find_all('font'):
        if str(font.get('color', '')).lower() != '#ff0000': continue
        if font.get('size', '') != '3': continue
        b = font.find('b')
        if not b: continue
        label = b.get_text(strip=True).rstrip(':').strip()
        if not label: continue
        # Is this header itself italic? → single tight group, weapons follow
        is_italic_header = bool(b.find('i'))
        if is_italic_header:
            # Collect text from FONT siblings after this one until the next red header
            weapons_text = _collect_text_until_next_red_font(font)
            if weapons_text:
                out.append({
                    'tight_group': label,
                    'broad_group': label,
                    'weapons': _split_weapons(weapons_text),
                })
            current_broad = label
        else:
            # Broad group header. Tight sub-groups follow as <I>NAME:</I> labels.
            current_broad = label
            # Parse the body of this broad group: find italic labels with weapon lists
            tights = _extract_tight_groups_under_broad(font, current_broad)
            out.extend(tights)
    _sp_weapon_groups_cache = out
    return out


def _collect_text_until_next_red_font(start_font):
    """Collect plain text from following siblings (across <font> boundaries)
    until we hit another red SIZE=3 bold header. Used for single-tight-group
    sections where weapons follow the bold-italic header directly."""
    parts = []
    for sib in start_font.next_siblings:
        if getattr(sib, 'name', None) == 'font':
            if (sib.get('color','').lower() == '#ff0000'
                and sib.get('size','') == '3' and sib.find('b')):
                break
            text = sib.get_text(separator=' ', strip=True)
            if text and 'Table of Contents' not in text:
                parts.append(text)
        elif isinstance(sib, NavigableString):
            text = str(sib).strip()
            if text: parts.append(text)
    return ' '.join(parts).strip()


def _extract_tight_groups_under_broad(start_font, broad_name):
    """For a broad-group section, find <I>Label:</I> tags in following
    siblings and grab the comma-separated weapon list after each."""
    tights = []
    for sib in start_font.next_siblings:
        if getattr(sib, 'name', None) == 'font':
            color = sib.get('color','').lower()
            if color == '#ff0000' and sib.get('size','') == '3' and sib.find('b'):
                break  # next broad/tight group header
            # Look for <i>Label:</i> followed by text inside this font tag
            italics = sib.find_all('i')
            for it in italics:
                label = it.get_text(strip=True).rstrip(':').strip()
                if not label or label.lower() == 'unrelated': continue
                # Weapon text follows the italic tag, inside the same font
                tail = ''
                for n in it.next_siblings:
                    if getattr(n, 'name', None) == 'i': break
                    if isinstance(n, NavigableString):
                        tail += str(n)
                    elif getattr(n, 'name', None) == 'p':
                        break
                    else:
                        tail += n.get_text(' ', strip=True)
                tail = tail.strip().lstrip(':').strip()
                if tail:
                    tights.append({
                        'tight_group': label,
                        'broad_group': broad_name,
                        'weapons': _split_weapons(tail),
                    })
    return tights


def _split_weapons(text):
    """Split a comma-separated weapon list, normalize apostrophes/spaces."""
    if not text: return []
    # Drop trailing punctuation, normalize fancy quotes (cp1252 → ASCII)
    text = text.replace('’', "'").replace('‘', "'")
    text = text.replace('\x92', "'").replace('\x91', "'")
    text = re.sub(r'\s+', ' ', text).strip().rstrip('.').strip()
    parts = [w.strip() for w in text.split(',')]
    return [w for w in parts if w and len(w) > 1]


# Tight groups whose entries are bare qualifiers (the broad-group noun is
# implicit). Append the noun to each weapon so 'Light' in the Lances group
# matches 'Light horse lance' but not 'Light crossbow'.
_GROUP_QUALIFIER_SUFFIX = {
    'lances': 'lance',
}


# ── Icon picker for proficiencies/skills (keyword match, ordered) ────────────
_PROF_SKILL_ICON_MAP = [
    # Weapons by sub-category
    ('bow',           'icons/skills/ranged/arrow-flying-broadhead-metal.webp'),
    ('crossbow',      'icons/skills/ranged/arrow-flying-broadhead-metal.webp'),
    ('quarrel',       'icons/skills/ranged/arrow-flying-broadhead-metal.webp'),
    ('arrow',         'icons/skills/ranged/arrow-flying-broadhead-metal.webp'),
    ('blowgun',       'icons/skills/ranged/bomb-grenade-thrown-orange.webp'),
    ('sling',         'icons/weapons/ammunition/shot-round-blue.webp'),
    ('dart',          'icons/skills/ranged/bomb-grenade-thrown-orange.webp'),
    ('javelin',       'icons/skills/melee/spear-tips-triple-orange.webp'),
    ('lance',         'icons/skills/melee/spear-tips-triple-orange.webp'),
    ('spear',         'icons/skills/melee/strike-spear-red.webp'),
    ('trident',       'icons/skills/melee/spear-tips-triple-orange.webp'),
    ('harpoon',       'icons/skills/melee/spear-tips-triple-orange.webp'),
    ('pike',          'icons/skills/melee/spear-tips-triple-orange.webp'),
    ('halberd',       'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('polearm',       'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('partisan',      'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('glaive',        'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('guisarme',      'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('voulge',        'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('ranseur',       'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('spetum',        'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('bardiche',      'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('fauchard',      'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('bec de corbin', 'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('bill',          'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('lucern',        'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('two-hand',      'icons/skills/melee/weapons-crossed-swords-black.webp'),
    ('bastard sword', 'icons/skills/melee/sword-echo-stylized-tan.webp'),
    ('broad sword',   'icons/weapons/swords/sword-guard-bronze.webp'),
    ('long sword',    'icons/weapons/swords/sword-guard-bronze.webp'),
    ('short sword',   'icons/weapons/swords/shortsword-guard-silver.webp'),
    ('scimitar',      'icons/weapons/swords/scimitar-guard-brown.webp'),
    ('khopesh',       'icons/weapons/swords/sword-guard-bronze.webp'),
    ('sword',         'icons/weapons/swords/sword-guard-bronze.webp'),
    ('dagger',        'icons/weapons/daggers/dagger-curved-black.webp'),
    ('dirk',          'icons/weapons/daggers/dagger-curved-black.webp'),
    ('knife',         'icons/weapons/daggers/dagger-straight-blue.webp'),
    ('axe',           'icons/skills/melee/hand-grip-sword-orange.webp'),
    ('hand axe',      'icons/skills/melee/hand-grip-sword-orange.webp'),
    ('battle axe',    'icons/skills/melee/hand-grip-sword-orange.webp'),
    ('mace',          'icons/weapons/maces/mace-round-spiked-grey.webp'),
    ('flail',         'icons/weapons/maces/flail-studded-grey.webp'),
    ('morning star',  'icons/weapons/maces/mace-round-spiked-grey.webp'),
    ('pick pocket',   'icons/skills/social/theft-pickpocket-bribery-brown.webp'),
    ('warhammer',     'icons/weapons/hammers/hammer-double-simple.webp'),
    ('hammer',        'icons/weapons/hammers/hammer-double-simple.webp'),
    ('pick',          'icons/weapons/hammers/hammer-double-simple.webp'),
    ('club',          'icons/weapons/clubs/club-simple-black.webp'),
    ('quarterstaff',  'icons/weapons/staves/staff-engraved-wood.webp'),
    ('staff',         'icons/weapons/staves/staff-engraved-wood.webp'),
    ('whip',          'icons/skills/melee/sword-echo-stylized-tan.webp'),
    ('scourge',       'icons/skills/melee/sword-echo-stylized-tan.webp'),
    ('sickle',        'icons/tools/laboratory/bowl-mixing.webp'),
    ('arquebus',      'icons/weapons/ammunition/shot-round-blue.webp'),
    ('matchlock',     'icons/weapons/ammunition/shot-round-blue.webp'),
    ('flintlock',     'icons/weapons/ammunition/shot-round-blue.webp'),
    ('pistol',        'icons/weapons/ammunition/shot-round-blue.webp'),
    ('boomerang',     'icons/weapons/ammunition/arrow-broadhead-glowing-orange.webp'),
    ('chakram',       'icons/skills/ranged/bomb-grenade-thrown-orange.webp'),
    ('mancatcher',    'icons/skills/melee/strike-spear-red.webp'),
    ('fork',          'icons/skills/melee/spear-tips-triple-orange.webp'),
    ('needle',        'icons/skills/ranged/bomb-grenade-thrown-orange.webp'),
    # Nonweapon proficiencies — by keyword
    ('agricul',       'icons/skills/trades/farming-wheat-circle-yellow.webp'),
    ('ancient hist',  'icons/skills/trades/academics-book-study-purple.webp'),
    ('ancient lang',  'icons/skills/trades/academics-study-reading-book.webp'),
    ('animal handl',  'icons/creatures/abilities/paw-print-tan.webp'),
    ('animal lore',   'icons/creatures/abilities/paw-print-tan.webp'),
    ('animal train',  'icons/creatures/abilities/paw-print-tan.webp'),
    ('appraising',    'icons/commodities/currency/coin-embossed-crown-gold.webp'),
    ('armorer',       'icons/equipment/chest/breastplate-collared-steel.webp'),
    ('artistic',      'icons/skills/trades/music-singing-voice-blue.webp'),
    ('astrology',     'icons/magic/perception/orb-eye-scrying.webp'),
    ('astronomy',     'icons/magic/perception/orb-eye-scrying.webp'),
    ('blacksmith',    'icons/skills/trades/smithing-anvil-brown.webp'),
    ('blind-fight',   'icons/skills/melee/unarmed-punch-fist.webp'),
    ('blind fight',   'icons/skills/melee/unarmed-punch-fist.webp'),
    ('boat',          'icons/magic/water/water-drop-swirl-blue.webp'),
    ('bowyer',        'icons/skills/ranged/arrow-flying-broadhead-metal.webp'),
    ('brewing',       'icons/consumables/drinks/alcohol-beer-stein-wooden-brown.webp'),
    ('carpentry',     'icons/skills/trades/woodcutting-logging-axe-stump.webp'),
    ('chariot',       'icons/environment/people/cavalry.webp'),
    ('cobbling',      'icons/equipment/feet/boots-leather-brown.webp'),
    ('cooking',       'icons/consumables/food/shank-meat-bone-glazed-brown.webp'),
    ('cryptography',  'icons/skills/trades/academics-investigation-puzzles.webp'),
    ('dancing',       'icons/skills/social/diplomacy-handshake.webp'),
    ('deep diving',   'icons/magic/water/wave-water-blue.webp'),
    ('disguise',      'icons/equipment/head/cap-simple-leather-brown.webp'),
    ('endurance',     'icons/skills/wounds/anatomy-organ-heart-red.webp'),
    ('engineering',   'icons/tools/hand/wrench-iron-grey.webp'),
    ('etiquette',     'icons/skills/social/diplomacy-handshake.webp'),
    ('fire-build',    'icons/magic/fire/flame-burning-fist-strike.webp'),
    ('fire build',    'icons/magic/fire/flame-burning-fist-strike.webp'),
    ('fishing',       'icons/environment/creatures/fish-crosshatched-grey-blue.webp'),
    ('forgery',       'icons/skills/trades/academics-investigation-puzzles.webp'),
    ('gaming',        'icons/sundries/gaming/gaming-set-dice.webp'),
    ('gem',           'icons/commodities/gems/gem-faceted-asscher-blue.webp'),
    ('healing',       'icons/skills/wounds/anatomy-organ-heart-red.webp'),
    ('heraldry',      'icons/equipment/shield/heater-crystal-blue.webp'),
    ('herbalism',     'icons/magic/nature/leaf-glow-green.webp'),
    ('hunting',       'icons/environment/creatures/horse-brown.webp'),
    ('juggling',      'icons/skills/social/diplomacy-handshake.webp'),
    ('jumping',       'icons/skills/movement/feet-spurred-boots-brown.webp'),
    ('leather',       'icons/commodities/leather/fur-pelt-brown.webp'),
    ('local hist',    'icons/skills/trades/academics-book-study-purple.webp'),
    ('mining',        'icons/commodities/stone/boulder-grey.webp'),
    ('modern lang',   'icons/skills/trades/academics-study-reading-book.webp'),
    ('languages, mod','icons/skills/trades/academics-study-reading-book.webp'),
    ('languages, anc','icons/skills/trades/academics-study-reading-book.webp'),
    ('mountain',      'icons/environment/wilderness/cave-entrance-mountain.webp'),
    ('musical',       'icons/skills/trades/music-notes-sound-blue.webp'),
    ('navigation',    'icons/tools/navigation/compass-brass-blue-red.webp'),
    ('orienteering',  'icons/tools/navigation/compass-brass-blue-red.webp'),
    ('painting',      'icons/skills/trades/music-singing-voice-blue.webp'),
    ('pottery',       'icons/commodities/materials/bowl-powder-grey.webp'),
    ('reading lips',  'icons/skills/social/diplomacy-handshake.webp'),
    ('reading/writ',  'icons/skills/trades/academics-study-reading-book.webp'),
    ('reading',       'icons/skills/trades/academics-study-reading-book.webp'),
    ('religion',      'icons/magic/holy/saint-glass-portrait-halo.webp'),
    ('riding, air',   'icons/creatures/abilities/wings-birdlike-blue.webp'),
    ('riding, land',  'icons/environment/people/cavalry.webp'),
    ('riding',        'icons/environment/people/cavalry.webp'),
    ('rope',          'icons/sundries/survival/rope-coiled-tan.webp'),
    ('running',       'icons/skills/movement/figure-running-gray.webp'),
    ('sculpt',        'icons/commodities/stone/boulder-grey.webp'),
    ('seamanship',    'icons/magic/water/water-drop-swirl-blue.webp'),
    ('seamstress',    'icons/commodities/cloth/cloth-bolt-grey.webp'),
    ('tailor',        'icons/commodities/cloth/cloth-bolt-grey.webp'),
    ('set snares',    'icons/environment/traps/trap-jaw-steel.webp'),
    ('singing',       'icons/skills/trades/music-notes-sound-blue.webp'),
    ('spellcraft',    'icons/magic/symbols/runes-star-pentagon-orange.webp'),
    ('stonemason',    'icons/commodities/stone/paver-brick-blue.webp'),
    ('survival',      'icons/environment/wilderness/tree-oak.webp'),
    ('swimming',      'icons/magic/water/wave-water-blue.webp'),
    ('throwing',      'icons/skills/ranged/bomb-grenade-thrown-orange.webp'),
    ('tightrope',     'icons/skills/movement/feet-spurred-boots-brown.webp'),
    ('tracking',      'icons/skills/movement/figure-running-gray.webp'),
    ('tumbling',      'icons/skills/movement/feet-spurred-boots-brown.webp'),
    ('ventriloq',     'icons/skills/social/diplomacy-handshake.webp'),
    ('weaponsmith',   'icons/skills/trades/smithing-anvil-brown.webp'),
    ('weather',       'icons/magic/air/wind-stream-blue-gray.webp'),
    ('weaving',       'icons/commodities/cloth/cloth-bolt-grey.webp'),
    # Rogue skills
    ('pick pocket',   'icons/skills/social/theft-pickpocket-bribery-brown.webp'),
    ('open lock',     'icons/skills/trades/security-lockpicking-chest-blue.webp'),
    ('find/remove tr','icons/environment/traps/pressure-plate.webp'),
    ('move silent',   'icons/skills/movement/figure-running-gray.webp'),
    ('hide in shad',  'icons/magic/perception/silhouette-stealth-shadow.webp'),
    ('detect noise',  'icons/skills/social/diplomacy-handshake.webp'),
    ('hear noise',    'icons/skills/social/diplomacy-handshake.webp'),
    ('climb wall',    'icons/skills/movement/feet-spurred-boots-brown.webp'),
    ('read lang',     'icons/skills/trades/academics-study-reading-book.webp'),
]

def _pick_prof_skill_icon(name, fallback):
    """Pick an icon for a proficiency/skill by keyword in its name, else
    `fallback`. (Keyword→icon search table — generic descriptors, see the
    copyright note: these are search references, not embedded content.)"""
    n = (name or '').lower()
    for kw, icon in _PROF_SKILL_ICON_MAP:
        if kw in n: return icon
    return fallback


# ── Build PARTS.DAT weapon → item index for the proficiency `appliedto[]` ────
# ── Factories ────────────────────────────────────────────────────────────────
def make_weapon_proficiency_item(weapon, img, applied_part_ids):
    """Build a Foundry proficiency item from one PHB Table 44 row, with
    `appliedto[]` populated with refs to the matching PARTS.DAT weapon items
    in the adnd2-items pack."""
    item_id = make_id()
    # Description: short HTML stat-block
    desc_parts = []
    desc_parts.append(f"<p>Speed factor: {weapon['speed']} · "
                      f"Damage S-M: {weapon['dmg_sm']} · "
                      f"Damage L: {weapon['dmg_l']}</p>")
    desc_parts.append(f"<p>Cost: {weapon['cost']} · "
                      f"Weight: {weapon['weight']} lb · "
                      f"Size: {weapon['size']} · "
                      f"Type: {weapon['type']}</p>")
    try: speed = int(weapon['speed'])
    except ValueError: speed = 0
    appliedto = []
    for pid, pname in applied_part_ids[:20]:
        appliedto.append({
            "id":         pid,
            "uuid":       f"Compendium.{MODULE_ID}.adnd2-items.Item.{pid}",
            "type":       "weapon",
            "name":       pname,
        })
    return {
        "_id": item_id,
        "name": weapon['name'],
        "type": "proficiency",
        "img": img,
        "effects": [],
        "system": {
            "description": ''.join(desc_parts),
            "dmonlytext": "",
            "itemList": [],
            "appliedto": appliedto,
            "cost": 1,
            "hit": "", "damage": "", "speed": speed, "attacks": "",
            "migrate": False,
            "attributes": {"rarity": "", "type": "", "subtype": "", "magic": False,
                           "properties": [], "skillmods": [], "conditionals": [],
                           "identified": True, "infiniteammo": False,
                           "size": "medium", "material": "leather_book"},
            "charges": {"value": 0, "min": 0, "max": 0, "reuse": "none"},
            "location": {"state": "", "parent": ""},
            "resource": {"itemId": ""},
            "quantity": 0, "weight": 0, "source": "", "xp": 0,
            "actions": [], "actionGroups": [],
            "proficiencies": {"cost": 1},
            "rank": {"levels": {"max":1, "arcane":1, "divine":1}},
        },
        "folder": None, "sort": 0,
        "ownership": {"default": 0},
        "flags": {}, "_stats": _stats_block(),
    }


_NWP_ABILITY_MAP = {
    'strength': 'str', 'dexterity': 'dex', 'constitution': 'con',
    'intelligence': 'int', 'wisdom': 'wis', 'charisma': 'cha',
}
def _nwp_ability_slug(ability_text):
    """Map PHB/SP ability label ('Wisdom', 'Intelligence/Knowledge', 'NA')
    to an ARS @abilities key, or '' if NA / unrecognized."""
    if not ability_text or ability_text.upper() == 'NA': return ''
    head = ability_text.split('/')[0].split(',')[0].strip().lower()
    return _NWP_ABILITY_MAP.get(head, '')


def make_nwp_skill_item(name, source_label, slots_or_cp, ability_label,
                        modifier, description, icon, extra_meta=''):
    """Build a Foundry skill item representing one nonweapon proficiency.
    Roll: 1d20 vs ability score + modifier (decending — lower roll succeeds).
    target is an @-formula so it resolves on the actor."""
    ab = _nwp_ability_slug(ability_label)
    if ab:
        target_str = f"@abilities.{ab}.value{modifier if modifier and modifier[0] in '+-' else ''}"
        formula    = "1d20"
        kind       = "decending"
    else:
        target_str = "0"
        formula    = "1d20"
        kind       = "decending"
    desc = f"<p><em>{source_label}{extra_meta}</em></p>" + (description or '')
    sk = make_skill_item(name, icon, formula, target_str,
                         type_=kind, groups='', description=desc)
    sk['system']['features']['cost'] = slots_or_cp
    sk['system']['features']['ability'] = ab if ab else 'none'
    return sk


def make_rogue_skill_item(name, base_pct, description, icon):
    """Build a Foundry skill item for one thief skill (1d100 vs base score)."""
    sk = make_skill_item(name, icon, "1d100", str(base_pct),
                         type_='decending', groups='Thief',
                         description=description or '')
    return sk


# ── Migrators ────────────────────────────────────────────────────────────────
def _norm_weapon_name(n):
    """Lowercase, normalize apostrophes/punctuation, collapse spaces, and
    fold a few known PHB↔SP variant spellings so cross-source matching works
    (e.g. 'Warhammer' ↔ 'war hammer', 'Broad sword' ↔ 'broadsword',
    'Two-hand. sword' ↔ 'two-handed sword', 'hand or throwing axe' ↔
    'hand/throwing axe')."""
    if not n: return ''
    s = n.lower().replace('’', "'").replace('‘', "'").replace('\x92', "'").replace('\x91', "'")
    s = re.sub(r'\s+', ' ', s).strip().rstrip('.').strip()
    s = s.replace('two-hand. ', 'two-handed ')
    s = s.replace(' or ', '/')
    s = re.sub(r'\bwar hammer\b', 'warhammer', s)
    s = re.sub(r'\bbroad sword\b', 'broadsword', s)
    return s


def _weapon_matches_group(weapon_name, group_weapons, tight_group_name=''):
    """Return True if a PHB Table 44 weapon belongs to one of the SP weapon
    group's listed weapons. Substring match either way (PHB names are often
    longer, e.g. 'Composite long bow' vs SP 'composite long bow'; sometimes
    SP is more specific, e.g. SP 'long sword' vs PHB 'Long sword').
    For groups whose entries are bare qualifiers (Lances: Light/Medium/Heavy/
    Jousting), require the group's noun in the weapon name as well."""
    pn = _norm_weapon_name(weapon_name)
    suffix = _GROUP_QUALIFIER_SUFFIX.get(tight_group_name.lower(), '')
    for gw in group_weapons:
        gn = _norm_weapon_name(gw)
        if not gn: continue
        if suffix:
            # Both qualifier and noun must appear in the weapon name
            if (re.search(rf'\b{re.escape(gn)}\b', pn)
                and re.search(rf'\b{re.escape(suffix)}\b', pn)):
                return True
            continue
        if pn == gn: return True
        if re.search(rf'\b{re.escape(gn)}\b', pn): return True
        if re.search(rf'\b{re.escape(pn)}\b', gn): return True
    return False


_GROUP_FAMILIARITY_ICON_MAP = {
    'axes':            'icons/skills/melee/hand-grip-sword-orange.webp',
    'picks':           'icons/weapons/hammers/hammer-double-simple.webp',
    'hammers':         'icons/weapons/hammers/hammer-double-simple.webp',
    'bows':            'icons/skills/ranged/arrow-flying-broadhead-metal.webp',
    'maces':           'icons/weapons/maces/mace-round-spiked-grey.webp',
    'clubs':           'icons/weapons/clubs/club-simple-black.webp',
    'flails':          'icons/weapons/maces/flail-studded-grey.webp',
    'crossbows':       'icons/skills/ranged/arrow-flying-broadhead-metal.webp',
    'daggers & knives':'icons/weapons/daggers/dagger-curved-black.webp',
    'lances':          'icons/skills/melee/spear-tips-triple-orange.webp',
    'spear-like polearms': 'icons/skills/melee/strike-spear-red.webp',
    'poleaxes':        'icons/skills/melee/weapons-crossed-swords-black.webp',
    'bills':           'icons/skills/melee/weapons-crossed-swords-black.webp',
    'glaives':         'icons/skills/melee/weapons-crossed-swords-black.webp',
    'beaked':          'icons/skills/melee/weapons-crossed-swords-black.webp',
    'spears':          'icons/skills/melee/strike-spear-red.webp',
    'javelins':        'icons/skills/melee/spear-tips-triple-orange.webp',
    'ancient':         'icons/weapons/swords/sword-guard-bronze.webp',
    'roman':           'icons/weapons/swords/sword-guard-bronze.webp',
    'middle eastern':  'icons/weapons/swords/scimitar-guard-brown.webp',
    'oriental':        'icons/weapons/swords/sword-katana.webp',
    'short':           'icons/weapons/swords/shortsword-guard-silver.webp',
    'medium':          'icons/weapons/swords/sword-guard-bronze.webp',
    'large':           'icons/skills/melee/sword-echo-stylized-tan.webp',
    'fencing weapons': 'icons/weapons/swords/sword-guard-purple.webp',
    'chain & rope weapons':'icons/weapons/maces/flail-studded-grey.webp',
    'martial arts weapons':'icons/skills/melee/unarmed-punch-fist.webp',
    'hand match weapons':'icons/weapons/ammunition/shot-round-blue.webp',
    'matchlocks':      'icons/weapons/ammunition/shot-round-blue.webp',
    'wheellocks':      'icons/weapons/ammunition/shot-round-blue.webp',
    'snaplocks and flintlocks':'icons/weapons/ammunition/shot-round-blue.webp',
}


def make_group_familiarity_item(tight_group, broad_group, weapons, icon):
    """Build a proficiency item that represents familiarity with all members
    of a weapon tight-group (S&P Table 49 rule). Drag-and-drop onto a PC."""
    item_id = make_id()
    name = f"Familiarity - {tight_group} Group"
    weapons_list = ', '.join(weapons)
    desc = (f"<p><em>S&P weapon-group familiarity (Table 49)</em></p>"
            f"<p>Proficiency in any single weapon of the <strong>{tight_group}</strong> "
            f"tight group automatically grants familiarity with every other "
            f"weapon in the group.</p>"
            f"<p><strong>Broad group:</strong> {broad_group}</p>"
            f"<p><strong>Weapons in this group:</strong> {weapons_list}</p>")
    return {
        "_id": item_id,
        "name": name,
        "type": "proficiency",
        "img": icon,
        "effects": [],
        "system": {
            "description": desc,
            "dmonlytext": "",
            "itemList": [],
            "appliedto": [],
            "cost": 0,
            "hit": "", "damage": "", "speed": 0, "attacks": "",
            "migrate": False,
            "attributes": {"rarity": "", "type": "", "subtype": "", "magic": False,
                           "properties": [], "skillmods": [], "conditionals": [],
                           "identified": True, "infiniteammo": False,
                           "size": "medium", "material": "leather_book"},
            "charges": {"value": 0, "min": 0, "max": 0, "reuse": "none"},
            "location": {"state": "", "parent": ""},
            "resource": {"itemId": ""},
            "quantity": 0, "weight": 0, "source": "", "xp": 0,
            "actions": [], "actionGroups": [],
            "proficiencies": {"cost": 0},
            "rank": {"levels": {"max":1, "arcane":1, "divine":1}},
        },
        "folder": None, "sort": 0,
        "ownership": {"default": 0},
        "flags": {}, "_stats": _stats_block(),
    }


def migrate_proficiencies():
    """Emit weapon-proficiency items (PHB Table 44) plus group-familiarity
    proficiency items (S&P Table 49). Each weapon prof links to every
    familiarity it belongs to via system.itemList — so taking the weapon
    prof on a character auto-grants the matching group familiarities."""
    print("\n=== Proficiencies (PHB Table 44 + S&P Table 49 group familiarities) ===")
    weapons = _load_phb_weapons_table()
    if not weapons:
        print("  No PHB weapons table parsed."); return 0
    weapon_groups = _load_sp_weapon_groups()

    db = _open_pack(OUTPUT_PACKS['proficiencies'])
    wp_folder = make_compendium_folder(make_id(), 'Weapon Proficiencies',
                                       'Item', sort=100000)
    fam_folder = make_compendium_folder(make_id(), 'Weapon Group Familiarities',
                                        'Item', sort=200000)

    # ── Build familiarity items first, keyed by tight_group name ────────────
    fam_items = {}  # tight_group_lower → item
    for g in weapon_groups:
        tg = g['tight_group']
        if tg.lower() in fam_items: continue  # dedupe (if any)
        icon = _GROUP_FAMILIARITY_ICON_MAP.get(tg.lower(),
            'icons/skills/melee/weapons-crossed-swords-black.webp')
        item = make_group_familiarity_item(tg, g['broad_group'], g['weapons'], icon)
        item['folder'] = fam_folder['_id']
        fam_items[tg.lower()] = item

    # ── Build weapon-proficiency items and link to all matching familiarities ─
    wp_items = []
    matched_count = 0
    for w in weapons:
        icon = _pick_prof_skill_icon(w['name'],
            'icons/skills/melee/weapons-crossed-swords-black.webp')
        item = make_weapon_proficiency_item(w, icon, [])
        item['folder'] = wp_folder['_id']
        # Find every tight group this weapon belongs to and add a child ref
        for g in weapon_groups:
            if _weapon_matches_group(w['name'], g['weapons'], g['tight_group']):
                fam = fam_items[g['tight_group'].lower()]
                item['system']['itemList'].append({
                    "id":         fam['_id'],
                    "uuid":       f"Item.{fam['_id']}",
                    "sourceuuid": f"Compendium.{MODULE_ID}.adnd2-proficiencies.Item.{item['_id']}",
                    "type":       "proficiency",
                    "name":       fam['name'],
                    "img":        fam['img'],
                })
        if item['system']['itemList']:
            matched_count += 1
        wp_items.append(item)

    # ── Weapon Specialization proficiency items (Melee + Bow) ────────────────
    # Bonuses live in the proficiency item's own built-in `hit`/`damage`
    # fields (see OSRIC "Specialization (Missile)") — no ActiveEffect needed.
    ws = _load_weapon_specialization_data()
    ws_folder = make_compendium_folder(make_id(), 'Weapon Specialization',
                                       'Item', sort=300000)
    _WS_ICON = 'icons/weapons/swords/greatsword-crossguard-embossed-gold.webp'
    _WS_BOW_ICON = 'icons/skills/ranged/archery-bow-attack-yellow.webp'
    # The bow specialist's +2 to-hit applies only at point-blank range. Model it
    # as an ARS proficiency conditional `{key: "target.distance", value: "LO,HI"}`
    # (band sourced from PHB00127 above): the engine then gates the item's `hit`
    # bonus to targets within that range instead of granting it unconditionally.
    bow_conditionals = []
    if ws.get('bow_pb'):
        lo, hi = ws['bow_pb']
        bow_conditionals = [{'key': 'target.distance', 'value': f'{lo},{hi}'}]
    ws_specs = [
        ('Weapon Specialization (Melee)', _WS_ICON,
         ws['melee_desc'], ws['melee_atk'], ws['melee_dmg'], ws['melee_cost'], []),
        ('Weapon Specialization (Bow)',   _WS_BOW_ICON,
         ws['bow_desc'],   ws['bow_atk'],   0,              ws['bow_cost'], bow_conditionals),
    ]
    ws_items_extra = []
    for ws_name, ws_icon, ws_desc, ws_hit, ws_dmg, ws_cost, ws_conds in ws_specs:
        ws_items_extra.append({
            '_id': make_id(), 'name': ws_name, 'type': 'proficiency', 'img': ws_icon,
            'effects': [],
            'system': {
                'description': ws_desc, 'dmonlytext': '', 'itemList': [],
                'appliedto': [], 'cost': None,
                'hit': str(ws_hit) if ws_hit else '',
                'damage': str(ws_dmg) if ws_dmg else '',
                'speed': 0, 'attacks': '', 'migrate': False,
                'attributes': {'rarity': '', 'type': '', 'subtype': '', 'magic': False,
                               'properties': [], 'skillmods': [], 'conditionals': ws_conds,
                               'identified': True, 'infiniteammo': False,
                               'size': 'medium', 'material': 'leather_book'},
                'charges':  {'value': 0, 'min': 0, 'max': 0, 'reuse': 'none'},
                'location': {'state': '', 'parent': ''},
                'resource': {'itemId': ''},
                'quantity': 0, 'weight': 0, 'source': '', 'xp': 0,
                'actions': [], 'actionGroups': [],
                'proficiencies': {'cost': ws_cost},
                'rank': {'levels': {'max': 1, 'arcane': 1, 'divine': 1}},
            },
            'folder': ws_folder['_id'], 'sort': 0,
            'ownership': {'default': 0},
            'flags': {}, '_stats': _stats_block(),
        })

    for item in wp_items:
        db.put(f'!items!{item["_id"]}'.encode(), json.dumps(item).encode())
    for item in fam_items.values():
        db.put(f'!items!{item["_id"]}'.encode(), json.dumps(item).encode())
    for item in ws_items_extra:
        db.put(f'!items!{item["_id"]}'.encode(), json.dumps(item).encode())
    for f in (wp_folder, fam_folder, ws_folder):
        db.put(f'!folders!{f["_id"]}'.encode(), json.dumps(f).encode())
    db.close()
    total = len(wp_items) + len(fam_items) + len(ws_items_extra)
    print(f"  → {len(wp_items)} weapon proficiencies + {len(fam_items)} group familiarities")
    print(f"    + {len(ws_items_extra)} weapon specialization items")
    print(f"    {matched_count} weapon proficiencies link at least one familiarity")
    return total


def migrate_skills():
    """Emit nonweapon-proficiency skills (PHB + S&P) and rogue skills to
    adnd2-skills, organized by source / group folder."""
    print("\n=== Skills (Rogue + Nonweapon Proficiencies, PHB & S&P) ===")
    phb_nwp = _load_phb_nwp_table()
    sp_nwp  = _load_sp_nwp_table()
    rogue_base = _load_phb_thief_skill_base_scores()
    rogue_desc = _load_phb_thief_skill_descriptions()

    db = _open_pack(OUTPUT_PACKS['skills'])

    # ── Folders ──────────────────────────────────────────────────────────────
    folders = {}
    rogue_folder = make_compendium_folder(make_id(), 'Rogue Skills', 'Item', sort=10000)
    folders['rogue'] = rogue_folder
    phb_root = make_compendium_folder(make_id(), 'Nonweapon Proficiencies (PHB)',
                                      'Item', sort=20000)
    folders['phb_root'] = phb_root
    sp_root  = make_compendium_folder(make_id(), 'Nonweapon Proficiencies (S&P)',
                                      'Item', sort=30000)
    folders['sp_root'] = sp_root
    for i, g in enumerate(('General','Priest','Rogue','Warrior','Wizard'), 1):
        fid = make_id()
        folders[('phb', g)] = make_compendium_folder(
            fid, g, 'Item', parent=phb_root['_id'], sort=i*1000)
    for i, g in enumerate(('General','Priest','Rogue','Warrior','Wizard','Psionicist'), 1):
        fid = make_id()
        folders[('sp', g)] = make_compendium_folder(
            fid, g, 'Item', parent=sp_root['_id'], sort=i*1000)

    counts = {'rogue': 0, 'phb_nwp': 0, 'sp_nwp': 0}

    # ── Rogue skills (PHB 8 + S&P 5 expansions) ─────────────────────────────
    _SMALL_WORDS = {'in','on','of','the','and','or','to'}
    def _title_skill(name_lower):
        # Capitalize each word and each slash-segment; keep small words lower
        # except first. Handles "Find/Remove Traps", "Hide in Shadows".
        words = name_lower.split(' ')
        out_words = []
        for i, w in enumerate(words):
            if '/' in w:
                w = '/'.join(seg[:1].upper() + seg[1:] for seg in w.split('/'))
            elif i > 0 and w in _SMALL_WORDS:
                pass
            else:
                w = w[:1].upper() + w[1:]
            out_words.append(w)
        return ' '.join(out_words)

    # Use S&P base scores (superset of PHB) as the canonical list; merge
    # descriptions preferring S&P (more detailed), falling back to PHB.
    sp_base = _load_sp_thief_skill_base_scores()
    sp_desc = _load_sp_thief_skill_descriptions()
    # Some keys differ between SP00073 ("Escape bonds") and SP00068 ("Escaping
    # bonds*"); normalize by dropping trailing -ing/-ping ambiguity.
    desc_aliases = {
        'escape bonds': 'escaping bonds',
        "thieves' cant": "thieves’ cant",
    }
    merged_base = _thief_canonical_scores()   # PHB 8 + S&P 5 expansions (13)
    for name_lower, pct in merged_base.items():
        display = _title_skill(name_lower)
        source = 'PHB' if name_lower in rogue_base else 'S&P'
        # PHB skills: prefer the richer PHB description; S&P expansion skills
        # only exist in S&P. SP00068 has terse stubs for the 8 PHB skills
        # while PHB00100 carries the full text.
        if source == 'PHB':
            desc = (rogue_desc.get(name_lower)
                    or sp_desc.get(name_lower)
                    or sp_desc.get(desc_aliases.get(name_lower, ''))
                    or '')
        else:
            desc = (sp_desc.get(name_lower)
                    or sp_desc.get(desc_aliases.get(name_lower, ''))
                    or rogue_desc.get(name_lower, ''))
            if desc:
                desc = '<p><em>Skills & Powers thief expansion</em></p>' + desc
        icon = _pick_prof_skill_icon(display,
            'icons/skills/social/theft-pickpocket-bribery-brown.webp')
        item = make_rogue_skill_item(display, pct, desc, icon)
        item['folder'] = rogue_folder['_id']
        db.put(f'!items!{item["_id"]}'.encode(), json.dumps(item).encode())
        counts['rogue'] += 1

    # ── Class-specific rogue skill variants (Bard / Ranger) ──────────────────
    # The Bard (Table 33) and Ranger (Table 18) share several thief skills with
    # the Thief but at different level-1 base scores. Where a class's score
    # differs, emit a distinct "<Skill> (<Class>)" variant so that class auto-
    # grants the correctly-scored copy instead of the Thief one. Scores read from
    # the PHB tables at runtime; description/icon reused from the base skill.
    counts['rogue_variant'] = 0
    for cls_low in ('bard', 'ranger'):
        spec = _class_thief_variant_spec(cls_low)
        if not spec:
            continue
        label, _base_set, cls_scores = spec
        for name_lower, pct in cls_scores.items():
            if name_lower not in merged_base or pct == merged_base[name_lower]:
                continue   # no variant needed when identical to the Thief score
            base_display = _title_skill(name_lower)
            display = f"{base_display} ({label})"
            desc = (rogue_desc.get(name_lower)
                    or sp_desc.get(name_lower)
                    or sp_desc.get(desc_aliases.get(name_lower, ''))
                    or '')
            icon = _pick_prof_skill_icon(base_display,
                'icons/skills/social/theft-pickpocket-bribery-brown.webp')
            item = make_rogue_skill_item(display, pct, desc, icon)
            item['folder'] = rogue_folder['_id']
            db.put(f'!items!{item["_id"]}'.encode(), json.dumps(item).encode())
            counts['rogue_variant'] += 1

    # NOTE: the Character-Point path does NOT duplicate these skills. Instead,
    # migrate_classes emits a CP *ability* per scored thief skill (in the class's
    # CP Abilities folder) whose system.itemList auto-grants the matching classic
    # skill item above when bought — so the player spends CP in one place and the
    # correct Thief/Bard/Ranger base score is preserved. See _cp_skill_link_name.

    # ── PHB nonweapon proficiencies ──────────────────────────────────────────
    for key, rec in sorted(phb_nwp.items()):
        desc = _extract_proficiency_description('PHB', rec['anchor'])
        icon = _pick_prof_skill_icon(rec['name'],
            'icons/skills/trades/academics-study-reading-book.webp')
        groups = ', '.join(sorted(rec['groups']))
        extra = (f" · Slots: {rec['slots']} · Ability: {rec['ability']}"
                 f" · Check Modifier: {rec['modifier'] or '0'}"
                 f" · Groups: {groups}")
        item = make_nwp_skill_item(rec['name'], 'PHB Nonweapon Proficiency',
                                   rec['slots'], rec['ability'],
                                   rec['modifier'], desc, icon,
                                   extra_meta=extra)
        # Place under one bucket — prefer the most "primary" group order
        for pri in ('General','Warrior','Priest','Wizard','Rogue'):
            if pri in rec['groups']:
                item['folder'] = folders[('phb', pri)]['_id']; break
        else:
            item['folder'] = folders[('phb','General')]['_id']
        db.put(f'!items!{item["_id"]}'.encode(), json.dumps(item).encode())
        counts['phb_nwp'] += 1

    # ── S&P nonweapon proficiencies ──────────────────────────────────────────
    for key, rec in sorted(sp_nwp.items()):
        desc = _extract_proficiency_description('SP', rec['anchor'])
        icon = _pick_prof_skill_icon(rec['name'],
            'icons/skills/trades/academics-study-reading-book.webp')
        groups = ', '.join(sorted(rec['groups']))
        extra = (f" · CP Cost: {rec['cp_cost']} · Initial Rating: {rec['initial']}"
                 f" · Ability: {rec['ability']} · Groups: {groups}")
        item = make_nwp_skill_item(rec['name'], 'S&P Nonweapon Proficiency',
                                   rec['cp_cost'], rec['ability'],
                                   '', desc, icon, extra_meta=extra)
        for pri in ('General','Warrior','Priest','Wizard','Rogue','Psionicist'):
            if pri in rec['groups']:
                item['folder'] = folders[('sp', pri)]['_id']; break
        else:
            item['folder'] = folders[('sp','General')]['_id']
        db.put(f'!items!{item["_id"]}'.encode(), json.dumps(item).encode())
        counts['sp_nwp'] += 1

    # ── S&P Psionicist-specific nonweapon proficiencies (individual chapter pages) ─
    psi_icon = 'icons/magic/perception/third-eye-blue-red.webp'
    for rec in _load_sp_psionicist_nwps():
        icon = _pick_prof_skill_icon(rec['name'], psi_icon)
        item = make_nwp_skill_item(rec['name'], 'S&P Psionicist Nonweapon Proficiency',
                                   0, '', '', rec['description'], icon)
        item['folder'] = folders[('sp', 'Psionicist')]['_id']
        db.put(f'!items!{item["_id"]}'.encode(), json.dumps(item).encode())
        counts['sp_nwp'] += 1

    for f in folders.values():
        db.put(f'!folders!{f["_id"]}'.encode(), json.dumps(f).encode())
    db.close()
    total = sum(counts.values())
    print(f"  → {counts['rogue']} rogue skills "
          f"(+{counts.get('rogue_variant', 0)} Bard/Ranger variants), "
          f"{counts['phb_nwp']} PHB nonweapon profs, "
          f"{counts['sp_nwp']} S&P nonweapon profs ({total} skill items total)")
    return total


def _treasure_formula(total):
    """Roll formula spanning all weight slots (1d100 for the % top tables, 1dN
    for the equal-weight leaf lists)."""
    return f"1d{total}" if total > 0 else "1d1"


def make_treasure_table(table, table_id, table_uuids, item_uuids):
    """Build a Foundry RollTable doc + its TableResult subdocs from a parsed
    treasure table. Result ranges are cumulative from the DAT weights. A sub-table
    target (flag 1) links by exact name to its RollTable UUID; a leaf target
    (flag 0) links to the matching Item UUID; unresolved targets become plain
    text. All sourced from the user's TREASURE.DAT/PARTS.DAT at runtime."""
    results, result_ids = [], []
    cursor = 1
    for weight, target, flag in table['entries']:
        span = max(1, weight)
        lo, hi = cursor, cursor + span - 1
        cursor = hi + 1
        rid = make_id()
        result_ids.append(rid)
        uuid = (table_uuids.get(target) if flag == 1 else None) or item_uuids.get(target)
        res = {"_id": rid, "weight": span, "range": [lo, hi], "drawn": False,
               "flags": {}, "_stats": _stats_block()}
        if uuid:
            res.update({"type": "document", "name": target, "description": "",
                        "documentUuid": uuid, "img": "icons/svg/d20-highlight.svg"})
        else:
            res.update({"type": "text", "name": "", "description": target,
                        "img": "icons/svg/d20-black.svg"})
        results.append(res)
    doc = {
        "_id": table_id, "name": table['name'], "description": "",
        "formula": _treasure_formula(cursor - 1),
        "replacement": True, "displayRoll": True,
        "img": "icons/sundries/gaming/dice-pair-white-green.webp",
        "results": result_ids,
        "folder": None, "sort": 0, "ownership": {"default": -1},
        "flags": {}, "_stats": _stats_block(),
    }
    return doc, results


# ─── Kit benefit / hindrance ability extraction ───────────────────────────────
#
# Each character kit has at least a "Special Benefits:" and/or "Special
# Hindrances:" section. These are extracted as Ability sub-items and added to
# the kit's system.itemList so the abilities appear on the actor sheet.
#
# The section labels are the same parse anchors already used in _KIT_LABEL_RE
# (generic English field names, no copyrighted content). The HTML text is read
# from the user's handbook HTM file or, for unmatched kits, the DAT prose.

_KIT_ABILITY_SECTION_RE = re.compile(
    r'^(Special Benefits?|Special Hindrances?|Benefits?(?:/Hindrances?)?|Hindrances?)',
    re.I
)

# Any known kit field label — used to mark where a benefit/hindrance section ends
# (the prose continues across several <p> paragraphs until the next label). These
# are generic English section names, not copyrighted content.
_KIT_SECTION_LABEL_RE = re.compile(
    r'^(Description|Role|Secondary Skills?|Weapon Proficien\w*|Nonweapon Proficien\w*|'
    r'Bonus Proficien\w*|Equipment|Money|Starting|Wealth(?: Options?)?|Special Benefits?|'
    r'Special Hindrances?|Benefits?(?:/Hindrances?)?|Hindrances?|Races?|Notes?|'
    r'Recommended|Required|Suggested|Allowed|Barred|Restrictions?)\b', re.I)


def _extract_kit_ability_sections(html):
    """Return [(label, html_block), ...] for benefit/hindrance sections in a kit
    description. The label is the section heading text (without colon); the block
    is the FULL section — the labelled <p> plus every following <p> up to the next
    kit field label (benefit/hindrance prose spans several paragraphs). Works for
    both HTM-sourced (plain-text labels) and DAT-sourced (<strong> labels)."""
    if not html:
        return []
    soup = BeautifulSoup(html, 'html.parser')
    ps = soup.find_all('p')
    results = []
    seen = set()
    for idx, p in enumerate(ps):
        m = _KIT_ABILITY_SECTION_RE.match(p.get_text(strip=True))
        if not m:
            continue
        key = m.group(1).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        block = [str(p)]
        for q in ps[idx + 1:]:
            if _KIT_SECTION_LABEL_RE.match(q.get_text(strip=True)):
                break
            block.append(str(q))
        results.append((m.group(1).strip(), ''.join(block)))
    return results


def _kit_ability_icon(label):
    """Return a verified webp icon for a kit benefit or hindrance ability."""
    low = label.lower()
    if 'hindrance' in low:
        return 'icons/skills/wounds/injury-triple-slash-bleed.webp'
    if 'benefit' in low:
        return 'icons/skills/social/diplomacy-peace-alliance.webp'
    # Combined benefits/hindrances
    return 'icons/skills/social/diplomacy-handshake.webp'


def migrate_kits():
    """Emit AD&D 2e character kits (PARTS.DAT `CPartKitOb`) as ARS `background`
    items in the adnd2-backgrounds pack, foldered by class. Each kit is matched to
    its Complete-Handbook HTM page (≈78% of kits): from there it takes the full
    prose description and auto-grants (via system.itemList) only the *mandatory*
    bonus nonweapon proficiencies labeled 'Bonuses:' — linked to the corresponding
    skill / weapon-proficiency compendium items. Optional 'Suggested'/allowed-weapon
    lists stay inline in the description. Unmatched kits fall back to the terse DAT
    Benefits/Hindrances prose with no auto-grant. Must run after migrate_skills and
    migrate_proficiencies (it reads those packs back to resolve link UUIDs).
    Returns the kit count."""
    print("\n=== Character Kits → Backgrounds (PARTS.DAT + handbook HTM) ===")
    kits = parse_kits()
    if not kits:
        print("  No kit records parsed."); return 0

    db = _open_pack(OUTPUT_PACKS['backgrounds'])

    # Folders by class (kits are class-kits; the race, when relevant, is already in
    # the kit name, e.g. "Battlerager, Dwarf"). 'General' holds S&P / undetermined.
    CLASSES = ['Fighter', 'Paladin', 'Ranger', 'Wizard', 'Priest',
               'Druid', 'Thief', 'Bard', 'General']
    folders = {}
    for i, c in enumerate(CLASSES, 1):
        folders[c] = make_compendium_folder(make_id(), f'{c} Kits', 'Item', sort=i * 1000)
        db.put(f'!folders!{folders[c]["_id"]}'.encode(), json.dumps(folders[c]).encode())

    # ── Ability folders: "Kit Abilities" root + one sub-folder per class ──────
    ab_root_id = make_id()
    ab_root    = make_compendium_folder(ab_root_id, 'Kit Abilities', 'Item', sort=10000)
    db.put(f'!folders!{ab_root_id}'.encode(), json.dumps(ab_root).encode())
    ab_folders = {}   # cls → folder_id
    for i, c in enumerate(CLASSES, 1):
        fid = make_id()
        f   = make_compendium_folder(fid, f'{c} Kit Abilities', 'Item',
                                     sort=i * 1000, parent=ab_root_id)
        db.put(f'!folders!{fid}'.encode(), json.dumps(f).encode())
        ab_folders[c] = fid

    # Link map: name → (id, name, img, pack-key, type) read back from the skills and
    # weapon-proficiency packs (written earlier in the run). Keyed by _kit_prof_norm
    # so a kit's bonus-prof name resolves to its compendium item.
    link_map = {}
    for packkey in ('skills', 'proficiencies'):
        src_dir = os.path.join(_PACK_SRC_BASE, os.path.basename(OUTPUT_PACKS[packkey]))
        if not os.path.isdir(src_dir):
            continue
        for fname in os.listdir(src_dir):
            if not fname.endswith('.json'):
                continue
            with open(os.path.join(src_dir, fname), encoding='utf-8') as fh:
                it = json.load(fh)
            if 'img' not in it or it.get('type') not in ('skill', 'proficiency'):
                continue
            link_map.setdefault(_kit_prof_norm(it['name']),
                                (it['_id'], it['name'], it['img'], packkey, it['type']))

    # Cache each handbook dir's case-insensitive filename map for clean_html_file.
    srcfiles = {}
    def _srcfiles(book):
        if book not in srcfiles:
            d = os.path.join(SOURCE_BASE, book)
            srcfiles[book] = ({f.upper(): f for f in os.listdir(d)}
                              if os.path.isdir(d) else {})
        return srcfiles[book]

    count = matched_n = linked = abilities_written = 0
    for kit in kits:
        page = _match_kit_page(kit['name'])
        if page:
            book, filepath = page
            matched_n += 1
            desc = clean_html_file(filepath, book, _srcfiles(book))
            bonus = _kit_bonus_profs(filepath)
            cls = _kit_class(kit['name'], book, filepath)
        else:
            book, desc, bonus = None, None, []
            cls = _kit_class(kit['name'], None)

        item = make_kit_item(kit, _kit_icon(kit['name']), description_html=desc)
        kit_uuid = f"Compendium.{MODULE_ID}.adnd2-backgrounds.Item.{item['_id']}"

        # Auto-grant the mandatory bonus proficiencies, linked to their items.
        seen = set()
        for prof in bonus:
            stripped = _KIT_PROF_GROUP_RE.sub('', prof)         # drop "(Warrior) " etc.
            ent = (link_map.get(_kit_prof_norm(stripped))       # keep paren content
                   or link_map.get(_kit_match_norm(stripped)))  # then drop it
            if not ent or ent[0] in seen:
                continue
            seen.add(ent[0])
            sid, sname, simg, spack, stype = ent
            item['system']['itemList'].append({
                "id": sid,
                "uuid": f"Compendium.{MODULE_ID}.adnd2-{spack}.Item.{sid}",
                "sourceuuid": kit_uuid,
                "type": stype, "name": sname, "img": simg, "level": "0",
            })
            linked += 1

        # ── Benefit / Hindrance ability items ────────────────────────────────
        # Source: full HTM description (matched kits) or DAT-formatted prose (else)
        source_html = desc if desc else _kit_description_html(kit.get('text', ''))
        ab_folder_id = ab_folders.get(cls if cls in ab_folders else 'General')
        for label, para_html in _extract_kit_ability_sections(source_html):
            ab_name = f'{kit["name"]} — {label}'   # em-dash separator
            icon    = _kit_ability_icon(label)
            ab, _ef = make_ability_item(ab_name, icon, description=para_html)
            ab['folder'] = ab_folder_id
            db.put(f'!items!{ab["_id"]}'.encode(), json.dumps(ab).encode())
            item['system']['itemList'].append({
                'id':       ab['_id'],
                'uuid':     f'Item.{ab["_id"]}',
                'sourceuuid': kit_uuid,
                'type':     'ability',
                'name':     ab_name,
                'img':      icon,
                'level':    '0',
            })
            abilities_written += 1

        item['folder'] = folders[cls if cls in folders else 'General']['_id']
        db.put(f'!items!{item["_id"]}'.encode(), json.dumps(item).encode())
        count += 1
    db.close()
    print(f"  → {count} background kits ({matched_n} matched to handbook HTM, "
          f"{linked} mandatory bonus-proficiency links, "
          f"{abilities_written} benefit/hindrance abilities)")
    return count


def migrate_treasure():
    """Phase 5: write the treasure pack — a Foundry RollTable per TREASURE.DAT
    table (+ TableResult subdocs). Pre-assigns table ids and reads back the items
    pack so results link to their sub-table / item UUID (else plain text). Must
    run after migrate_items (reads the items pack). Returns table count."""
    print("\n=== Treasure tables (TREASURE.DAT) ===")
    tables = parse_treasure()
    if not tables:
        print("  No treasure tables parsed."); return 0
    # Pre-assign table IDs so intra-pack sub-table references resolve to UUIDs.
    for t in tables:
        t['_id'] = make_id()
    table_uuids = {t['name']: f"Compendium.{MODULE_ID}.adnd2-treasure.RollTable.{t['_id']}"
                   for t in tables}
    # Item name → UUID, read back from the already-written items pack so leaf
    # results link to the real item. Best-effort; unmatched names stay text.
    item_uuids = {}
    items_src = os.path.join(_PACK_SRC_BASE, os.path.basename(OUTPUT_PACKS['items']))
    if os.path.isdir(items_src):
        for fname in os.listdir(items_src):
            if not fname.endswith('.json'):
                continue
            with open(os.path.join(items_src, fname), encoding='utf-8') as fh:
                it = json.load(fh)
            item_uuids.setdefault(it['name'],
                f"Compendium.{MODULE_ID}.adnd2-items.Item.{it['_id']}")
    db = _open_pack(OUTPUT_PACKS['treasure'])
    n_tables = n_results = n_linked = 0
    for t in tables:
        doc, results = make_treasure_table(t, t['_id'], table_uuids, item_uuids)
        db.put(f'!tables!{doc["_id"]}'.encode(), json.dumps(doc).encode())
        for r in results:
            db.put(f'!tables.results!{doc["_id"]}.{r["_id"]}'.encode(),
                   json.dumps(r).encode())
            n_results += 1
            n_linked += (r['type'] == 'document')
        n_tables += 1
    db.close()
    print(f"  → {n_tables} roll tables, {n_results} results "
          f"({n_linked} linked, {n_results - n_linked} text)")
    return n_tables


def write_module_json(stats):
    """Write/update module.json to declare all Phase 2 + Phase 3 packs."""
    packs = [
        {"name": "adnd2-journals", "label": "AD&D 2e Rulebooks",
         "path": "packs/adnd2-journals", "type": "JournalEntry", "system": "ars",
         "ownership": {"PLAYER": "OBSERVER", "ASSISTANT": "OWNER"}},
    ]
    for key, label in [
        ('races',         'AD&D 2e Races'),
        ('classes',       'AD&D 2e Classes'),
        ('items',         'AD&D 2e Items'),
        ('spells',        'AD&D 2e Spells'),
        ('powers',        'AD&D 2e Psionic Powers'),
        ('monsters',      'AD&D 2e Monsters'),
        ('proficiencies', 'AD&D 2e Weapon Proficiencies'),
        ('skills',        'AD&D 2e Skills (Rogue & Nonweapon Proficiencies)'),
        ('backgrounds',   'AD&D 2e Character Kits'),
    ]:
        packs.append({
            "name": f"adnd2-{key}", "label": label,
            "path": f"packs/adnd2-{key}",
            "type": "Actor" if key == 'monsters' else "Item",
            "system": "ars",
            "ownership": {"PLAYER": "OBSERVER", "ASSISTANT": "OWNER"},
        })
    packs.append({
        "name": "adnd2-treasure", "label": "AD&D 2e Treasure Tables",
        "path": "packs/adnd2-treasure", "type": "RollTable", "system": "ars",
        "ownership": {"PLAYER": "OBSERVER", "ASSISTANT": "OWNER"},
    })

    module = {
        "id": MODULE_ID, "title": "AD&D 2e Compendium",
        "description": "<p>AD&D 2nd Edition Compendium for Foundry VTT (ARS system, Variant 2). "
                       "Built from the user's local AD&D 2e Core Rules CD-ROM.</p>",
        "version": datetime.date.today().strftime("%Y.%m.%d"),
        "compatibility": {"minimum": "14", "verified": "14"},
        "authors": [{"name": "Hawkwood", "flags": {}},
                    {"name": "Claude Code", "flags": {}}],
        "relationships": {"systems": [{"id": "ars", "type": "system"}]},
        "packs": packs,
    }
    out_path = os.path.join("adnd2-compendium", "module.json")
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(module, f, indent=4, ensure_ascii=False)
    print(f"\n  Module manifest written to {out_path}")


# ─── Main migration ───────────────────────────────────────────────────────────

def migrate_book(book_config, folder_id, db, stats, book_sort=0):
    """Phase 2: migrate one rulebook into `db` — build its chapters, clean each
    chapter's HTML into a journal page, and write the JournalEntry + pages under
    `folder_id`. Updates `stats` (journals, pages) in place."""
    book_key    = book_config['key']
    book_name   = book_config['name']
    book_prefix = book_config['prefix']
    book_dir    = os.path.join(SOURCE_BASE, book_key)
    mode        = book_config['mode']

    print(f"\n{'='*60}")
    print(f"  {book_name}  (mode={mode})")

    if not os.path.isdir(book_dir):
        print(f"  ERROR: directory not found, skipping.")
        return

    # Pre-build case-insensitive map of source images
    src_dir_files = {f.upper(): f for f in os.listdir(book_dir)}

    journal_id = make_id()
    page_ids   = []

    if mode == 'chapters':
        toc_file = os.path.join(book_dir, f'{book_prefix}00000.HTM')
        if not os.path.exists(toc_file):
            toc_file = os.path.join(book_dir, f'{book_prefix}00000.htm')

        toc_entries = parse_toc(toc_file) if os.path.exists(toc_file) else []
        chapters = build_chapters(book_dir, book_prefix, toc_entries)
        n_chapters = sum(1 for e in toc_entries if e['is_chapter'])
        print(f"  TOC chapters: {n_chapters}  →  pages to generate: {len(chapters)}")

        for sort_i, chapter in enumerate(chapters):
            title = chapter['title']
            print(f"    [{sort_i+1:2d}] {title[:55]} ({len(chapter['files'])} files)")
            content = merge_chapter_html(book_dir, book_key, chapter, src_dir_files)
            if not content.strip():
                continue
            pid = make_id()
            page_ids.append(pid)
            db.put(
                f'!journal.pages!{journal_id}.{pid}'.encode(),
                json.dumps(make_page(pid, title, content, sort_i * 10000)).encode()
            )
            stats['pages'] += 1

    elif mode == 'pages':
        all_files = sorted([
            f for f in os.listdir(book_dir)
            if f.upper().startswith(book_prefix) and f.upper().endswith('.HTM')
               and f.upper() != f'{book_prefix}00000.HTM'
        ])
        print(f"  Files: {len(all_files)}")

        for sort_i, filename in enumerate(all_files):
            filepath = os.path.join(book_dir, filename)
            if not os.path.exists(filepath):
                filepath = os.path.join(book_dir, filename.lower())
            if not os.path.exists(filepath):
                continue
            title   = extract_title(filepath)
            content = clean_html_file(filepath, book_key, src_dir_files)
            if not content.strip():
                continue
            pid = make_id()
            page_ids.append(pid)
            db.put(
                f'!journal.pages!{journal_id}.{pid}'.encode(),
                json.dumps(make_page(pid, title, content, sort_i * 10000)).encode()
            )
            stats['pages'] += 1

        print(f"  Pages generated: {len(page_ids)}")

    if page_ids:
        db.put(
            f'!journal!{journal_id}'.encode(),
            json.dumps(make_journal(journal_id, book_name, page_ids, folder_id,
                                    sort=book_sort * 1000)).encode()
        )
        stats['journals'] += 1


def _check_cdrom_version():
    """Warn if CLASS.DAT MD5 doesn't match the tested CD-ROM version.

    This script targets exclusively the AD&D 2e Core Rules CD-ROM Expansion
    (Evermore Entertainment, 1999, v2.00.000). The checksum is taken from
    CLASS.DAT because it is the most structured and version-sensitive file.
    A mismatch means the CD-ROM content differs from what the script was
    developed and tested against; parsing offsets and field layouts may be
    wrong, producing silent errors or crashes.
    """
    import hashlib
    KNOWN_MD5 = '8d8a23d00ed6759d11eda0cef465333c'  # CLASS.DAT, v2.00.000 (1999-06-14)
    dat_path = os.path.join(DATABASE_BASE, 'CLASS.DAT')
    if not os.path.exists(dat_path):
        return  # DATABASE/ absent — Phase 3 will skip gracefully on its own
    with open(dat_path, 'rb') as f:
        actual_md5 = hashlib.md5(f.read()).hexdigest()
    if actual_md5 == KNOWN_MD5:
        return
    print()
    print("=" * 70)
    print("WARNING: CD-ROM content differs from the tested version")
    print("=" * 70)
    print(f"  Expected CLASS.DAT MD5 : {KNOWN_MD5}")
    print(f"  Found    CLASS.DAT MD5 : {actual_md5}")
    print()
    print("  This script was developed and tested exclusively against the")
    print("  AD&D 2e Core Rules CD-ROM Expansion (Evermore Entertainment,")
    print("  1999, version 2.00.000). A different version may have a")
    print("  different binary layout, causing silent errors or crashes.")
    print()
    print("  To verify your CD-ROM version, open DATA.TAG at the root of")
    print("  your CD-ROM and check the following fields:")
    print("    Application=AD&D Core Rules 2.0 Expansion")
    print("    Version=2.00.000")
    print("    Misc=06-14-99")
    print()
    answer = input("  Continue anyway? [y/N] ").strip().lower()
    if answer != 'y':
        print("Aborted.")
        raise SystemExit(1)
    print()


def main():
    """Entry point: wipe the output module, run Phase 2 (journals) into the
    journals pack, then Phases 3-5 (the migrate_* drivers) into their packs,
    write module.json, and print a summary. Phase 3+ is skipped gracefully if the
    DATABASE/ directory is absent."""
    _check_cdrom_version()
    print("AD&D 2e Migration")
    print(f"Output: {OUTPUT_DB}")

    os.makedirs(OUTPUT_IMG, exist_ok=True)

    journal_name = os.path.basename(OUTPUT_DB)
    journal_src  = os.path.join(_PACK_SRC_BASE, journal_name)
    if os.path.exists(journal_src):
        shutil.rmtree(journal_src)
    os.makedirs(journal_src, exist_ok=True)

    db = _JsonPack(journal_name, journal_src)
    stats = {'journals': 0, 'pages': 0}

    for folder_sort, folder_config in enumerate(FOLDERS):
        folder_id = make_id()
        db.put(
            f'!folders!{folder_id}'.encode(),
            json.dumps(make_folder(folder_id, folder_config['name'], sort=folder_sort * 1000)).encode()
        )
        print(f"\n{'='*60}")
        print(f"Folder: {folder_config['name']}")

        for book_sort, book_config in enumerate(folder_config['books']):
            migrate_book(book_config, folder_id, db, stats, book_sort=book_sort)

    db.close()

    # ─── Phase 3 — DATABASE/*.DAT migration ───────────────────────────────────
    phase3_stats = {}
    if os.path.isdir(DATABASE_BASE):
        print(f"\n{'='*60}")
        print(f"PHASE 3 — DATABASE/*.DAT migration")
        print(f"{'='*60}")
        try:
            phase3_stats['races']         = migrate_races()
            phase3_stats['spells']        = migrate_spells()
            phase3_stats['powers']        = migrate_psionics()
            phase3_stats['items']         = migrate_items()
            phase3_stats['monsters']      = migrate_monsters()
            phase3_stats['proficiencies'] = migrate_proficiencies()
            phase3_stats['skills']        = migrate_skills()
            # classes runs after skills so it can link thieving skills cross-pack
            phase3_stats['classes']       = migrate_classes()
            phase3_stats['backgrounds']   = migrate_kits()
            phase3_stats['treasure']      = migrate_treasure()
        except Exception as e:
            print(f"  Phase 3 error: {e}")
            import traceback; traceback.print_exc()
    else:
        print(f"\n  Phase 3 skipped: {DATABASE_BASE} not found.")

    # ─── fvtt-cli pack ────────────────────────────────────────────────────────
    _finalize_with_fvtt_cli()

    # ─── Module manifest ──────────────────────────────────────────────────────
    write_module_json(stats)

    # ─── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"Done!")
    print(f"  Phase 2 — Journals: {stats['journals']}, Pages: {stats['pages']}")
    if phase3_stats:
        print(f"  Phase 3 — Races: {phase3_stats.get('races', 0)}, "
              f"Classes: {phase3_stats.get('classes', 0)}, "
              f"Items: {phase3_stats.get('items', 0)}, "
              f"Spells: {phase3_stats.get('spells', 0)}, "
              f"Powers: {phase3_stats.get('powers', 0)}, "
              f"Monsters: {phase3_stats.get('monsters', 0)}")
        print(f"  Phase 4 — Weapon Proficiencies: {phase3_stats.get('proficiencies', 0)}, "
              f"Skills (rogue + NWP): {phase3_stats.get('skills', 0)}, "
              f"Character Kits (backgrounds): {phase3_stats.get('backgrounds', 0)}")
        print(f"  Phase 5 — Treasure roll tables: {phase3_stats.get('treasure', 0)}")


if __name__ == '__main__':
    main()
