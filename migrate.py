#!/usr/bin/env python3
"""
Build the `adnd2-compendium` Foundry VTT module from a local AD&D 2e Core Rules
CD-ROM. Standalone deliverable: stdlib + plyvel + beautifulsoup4 + Pillow only.
Idempotent — each run deletes and regenerates every output pack from scratch.

Target: Foundry VTT v14, system ARS Variant 2 (AD&D 2e / THAC0). Packs are
LevelDB (ClassicLevel) directories under adnd2-compendium/packs/.

──────────────────────────────────────────────────────────────────────────────
SOURCES (read at runtime from the user's CD-ROM — nothing is shipped with this
script; see the copyright note below):

  cd-rom/MACBOOKS/HTML/{BOOK}/*.HTM   rulebook prose (Latin-1/cp1252, FONT-tag
                                      formatting, no CSS). Parsed with BeautifulSoup.
  cd-rom/DATABASE/*.DAT               structured game data in MFC `CArchive`
                                      binary format (schema 88+, ~1996). Parsed by
                                      hand from reverse-engineered offsets — see
                                      DAT_FORMAT.md. Helpers: parse_mfc_header()
                                      (count/schema/class header), find_records()
                                      (split records on the `01 80` + Pascal-name
                                      object tag), read_pascal()/read_mfc_long_pascal().
  cd-rom/BITMAPS/{EQUIP,MONSTERS,PORTRAIT}/  icon sprite sheets (extracted to PNG).

OUTPUT PACKS (OUTPUT_PACKS): journals, races, classes, items, spells, powers,
monsters (Actor), proficiencies, skills, treasure (RollTable).

PIPELINE (main): Phase 2 journals (migrate_book) → Phase 3 entities
(migrate_races/classes/items/spells/psionics/monsters) → Phase 4
(migrate_proficiencies/skills) → Phase 5 (migrate_treasure) → write_module_json.
Each migrate_* opens a pack via _open_pack (wipe+recreate), parses its .DAT, and
writes Foundry documents built by the corresponding make_* / factory functions.

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

Companion docs at the project root: DAT_FORMAT.md (binary formats), CLAUDE.md
(HTML format + ARS schema reference), ARS_MECHANICS.md (effect/action recipes).
"""

import os
import re
import html
import json
import shutil
import random
import string
import struct
import plyvel
from html.parser import HTMLParser
from bs4 import BeautifulSoup, NavigableString, Tag
from PIL import Image

# ─── Configuration ────────────────────────────────────────────────────────────

# Phase 2 — HTML rulebooks
SOURCE_BASE  = "cd-rom/MACBOOKS/HTML"
OUTPUT_DB    = "adnd2-compendium/packs/adnd2-journals"
OUTPUT_IMG   = "adnd2-compendium/images"
MODULE_ID    = "adnd2-compendium"

# Phase 3 — DATABASE/*.DAT binary files
# Schema reference: see DAT_FORMAT.md
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

CORE_VERSION   = "14.361"
SYSTEM_ID      = "ars"
SYSTEM_VERSION = "2026.05.19"

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
    def handle_starttag(self, tag, attrs):
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
        color = (font.get('color') or '').lower()
        try:    size = int(font.get('size') or 0)
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

    Two strategies (see CLAUDE.md "Chapter detection"): if the TOC exposes bold
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


def merge_consecutive_headings(body, soup):
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
                if nxt and getattr(nxt, 'name', None) == level:
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
        color    = (font.get('color') or '').lower()
        try:    size = int(font.get('size') or 3)
        except (TypeError, ValueError): size = 3

        is_bold = bool(font.find('b')) or (font.parent and getattr(font.parent, 'name', None) == 'b')

        red    = color in ('#ff0000', 'red')
        green  = color in ('#008000', 'green')
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
    merge_consecutive_headings(body, soup)
    restructure_paragraphs(body, soup)
    for h in body.find_all(['h1', 'h2', 'h3', 'h4']):
        text = h.get_text()
        normalized = ' '.join(text.split())
        h.clear()
        h.append(NavigableString(normalized))
    inner = ''.join(str(c) for c in body.children)
    inner = re.sub(r'\n{3,}', '\n\n', inner).strip()
    return inner


def clean_html_file(filepath, book_key, src_dir_files):
    """Parse one HTML file and return clean semantic HTML."""
    with open(filepath, 'r', encoding='cp1252') as f:
        raw = f.read()
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
    Stored under `!journal.pages!{journalId}.{pageId}` (see CLAUDE.md key table)."""
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
# See DAT_FORMAT.md for binary schema reference
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
    _tag   = struct.unpack_from('<H', buf, 2)[0]   # expect 0xFFFF
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
        # Skip the sheet Pascal
        _sheet, p = read_pascal(buf, p)
        # Skip 10 bytes of structured data
        p += 10
        # Individual Pascal
        indiv, _ = read_pascal(buf, p)
        if indiv:
            result[name] = indiv
    return result


# RACE.DAT — see DAT_FORMAT.md §3.1
def parse_race_record(buf, start, end):
    """Parse one CRaceOb record (a race) into a dict of fields read at fixed
    post-name offsets (DAT_FORMAT.md §3.1): ability score min/max ranges (12
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


# CLASS.DAT — see DAT_FORMAT.md §3.2
def parse_class_record(buf, start, end):
    """Parse one CClassOb record (a class). Validates the per-record "TSRP3 V39"
    version Pascal, then reads name, group id (warrior/rogue/priest/wizard/
    psionicist), and locates two tables by signature scan rather than fixed
    offsets: the XP-by-level table (monotonic int run) and the THAC0 table. Also
    extracts the per-level saving-throw matrix (`save_table`) located by the
    Normal-Man baseline row [16,18,17,20,19] — that table is later read at
    level=HD to derive monster saves (DAT_FORMAT.md §3.2). Returns None if the
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
    # Find XP table — sequence of monotonic ints starting with L2 XP
    # Locate by scanning for plausible start (small int < 5000)
    chunk = buf[start:start+1024]
    xp_offset = None
    for off in range(30, len(chunk) - 4*15):
        try:
            seq = struct.unpack_from('<15i', chunk, off)
        except struct.error:
            continue
        if (10 <= seq[0] <= 5000 and all(seq[k] < seq[k+1] for k in range(14)) and seq[14] < 50_000_000):
            xp_offset = off
            break
    if xp_offset is not None:
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
    # Save table — located by the "Normal Man" baseline row [16,18,17,20,19].
    # Layout: 1 baseline row (row 0) + 99 level rows, 5 int32 each. Columns:
    #   [paralyze/poison/death, rod/staff/wand, petrify/polymorph, breath, spell]
    # rows[L] = saving throws at class level L (rows[0] = level-0 / Normal Man).
    # Validated vs PHB 2e for fighter/thief/cleric/mage (DAT_FORMAT.md §3.2).
    sig = struct.pack('<5i', 16, 18, 17, 20, 19)
    sp = buf.find(sig, start, end)
    if sp > 0:
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


# PARTS.DAT — see DAT_FORMAT.md §3.3
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


# TREASURE.DAT — see DAT_FORMAT.md §3.7. CTreasureSingleTableOb records: a table
# name followed by a list of CTreasureTableEntry objects. Each entry is framed
# `<mfc-tag> <uint32 weight> 01 <Pascal target>` — the weight is the d100
# percentage (validated: the top "DMG 88" table's 18 entry weights sum to 100),
# and the target is either a sub-table name ("DMG 89: …") or a leaf result/item
# name ("Oil of Timelessness"). All values read from the user's DAT at runtime.
def parse_treasure():
    """Parse TREASURE.DAT into [{name, entries:[(weight, target, flag)]}] — DMG
    treasure roll tables (see the format note above and DAT_FORMAT.md §3.7).
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


# SPELLS.DAT — see DAT_FORMAT.md §3.4
_SCHOOL_NAMES = {'Abjuration','Alteration','Conjuration','Divination','Enchantment','Illusion',
    'Invocation','Evocation','Necromancy','Charm','Alchemy','Geometry','Shadow','Song','Wild Magic',
    'Universal','Greater Divination','Lesser Divination','Charm/Enchantment','Conjuration/Summoning',
    'Evocation/Invocation','Greater Illusion','Illusion/Phantasm','Artifice','Dimension','Force',
    'Mentalist'}
_SPHERE_NAMES = {'All','Animal','Astral','Chaos','Combat','Creation','Elemental','Elemental Air',
    'Elemental Fire','Elemental Water','Elemental Earth','Elemental All','Guardian','Healing','Law',
    'Necromantic','Numbers','Plant','Protection','Summoning','Sun','Thought','Time','Travelers',
    'War','Wards','Weather'}


def parse_spell_record(buf, start, end):
    """Parse one CSpellsOb record (a spell) — DAT_FORMAT.md §3.4. Reads name,
    class type (int32 @+8: 1=wizard else priest), level (@+16), then school/
    sphere/range/components/duration/etc. Records whose name is itself a school
    or sphere label are embedded references, not spells, and return None."""
    p = start
    r = {}
    r['name'], p = read_pascal(buf, p)
    if r['name'] is None: return None
    if r['name'] in _SCHOOL_NAMES or r['name'] in _SPHERE_NAMES:
        return None     # embedded school/sphere reference, not a top-level spell
    # 7 zero bytes, then class type int32 @+8 and level int32 @+16 (relative to post-name)
    if p + 20 > end: return r
    try:
        r['class_type_id'] = struct.unpack_from('<i', buf, p + 8)[0]
        r['level']         = struct.unpack_from('<i', buf, p + 16)[0]
    except struct.error:
        return r
    r['class_type'] = 'wizard' if r['class_type_id'] == 1 else 'priest'
    # Walk Pascal strings: area, casting time, components, duration, range, save, school, +
    fields_order = ['area_of_effect', 'casting_time', 'components',
                    'duration', 'range', 'saving_throw', 'school']
    # Skip past the int32 header (20 bytes)
    p = p + 20
    for fname in fields_order:
        # Skip bytes until we find a valid Pascal string start (tolerates non-zero junk)
        while p < end - 1:
            n = buf[p]
            if 2 <= n <= 60 and p+1+n <= end:
                seq = buf[p+1:p+1+n]
                if all(32 <= b < 127 for b in seq):
                    r[fname] = seq.decode('latin-1')
                    p += 1 + n
                    break
            p += 1
        else:
            break  # ran out of buffer
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


# MONSTER.DAT — see DAT_FORMAT.md §3.5
def parse_monster_record(buf, start, end):
    """Parse one CMonsterOb record (a monster stat block) — DAT_FORMAT.md §3.5.
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


# PSIONIC.DAT — see DAT_FORMAT.md §3.6
def parse_psionic_record(buf, start, end):
    """Parse one CPsionicPowerOb record (a psionic power) — DAT_FORMAT.md §3.6.
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
    while p < end - 1:
        n = buf[p]
        # Stop if we hit a long-string marker (the description follows)
        if n == 0xFF and p + 3 < end:
            break
        if 2 <= n <= 80 and p+1+n <= end:
            seq = buf[p+1:p+1+n]
            if all(32 <= b < 127 for b in seq):
                short_pascals.append(seq.decode('latin-1'))
                p += 1 + n
                continue
        p += 1
    # Field assignment based on observed structure:
    # [0] = power score "N/D" or "N+/D+"
    # [1] = range (e.g. "50 yards", "Unlimited", "Personal")
    # [2] = area of effect (often "Personal", "20 yards", "individual")
    if len(short_pascals) >= 1:
        s0 = short_pascals[0]
        if re.match(r'^\d+\+?\s*/\s*\d+\+?', s0):
            r['power_score'] = s0
    if len(short_pascals) >= 2: r['range']          = short_pascals[1]
    if len(short_pascals) >= 3: r['area_of_effect'] = short_pascals[2]
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
_ANCHORED_SPELL_RE = re.compile(
    r'<A\s+NAME="([^"]+)"\s*></A>'   # the anchor
    r'(?:\s*<FONT[^>]*>)?'           # optional FONT wrapper
    r'\s*<B>\s*([^<]+?)\s*</B>',     # the spell name
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
            if not anchors: continue
            for i, m in enumerate(anchors):
                aname = m.group(2).strip()
                if not (3 <= len(aname) <= 60): continue
                start = m.start()
                end = anchors[i+1].start() if i+1 < len(anchors) else len(raw)
                key = aname.lower()
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

    # Known spelling divergences (DAT spelling → HTM spelling). Only generic,
    # non-proprietary phrasings are listed here; per-name spelling fixes that
    # would require embedding AD&D proper names are left to the runtime
    # singular/plural and punctuation expansion above instead.
    ALIASES = {
        "detect snares & pits":          "detect snares and pits",
    }
    base = keys[0] if keys else n_clean.lower()
    if base in ALIASES:
        _push(ALIASES[base])

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
_SPELL_HTML_BOOKS  = [('PHB','PHB'), ('TOM','TOM'), ('SM','SM'), ('SP','SP')]
_MONSTER_HTML_BOOKS = [('MM','MM')]
_ITEM_HTML_BOOKS    = [('AEG','AEG')]


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
_DMG_HEAR_NOISE_FILE       = 'DMG/DMG00472.HTM'  # Table 83 — Chance to Hear Noise by Race
_DMG_LISTENING_FILE        = 'DMG/DMG00471.HTM'  # Listening (chapter prose)
_PHB_CLIMBING_RATES_FILE   = 'PHB/PHB00378.HTM'  # Table 65 — Base Climbing Success Rates
_PHB_CLIMBING_CHAPTER_FILE = 'PHB/PHB00375.HTM'  # Climbing (chapter prose)

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
_dmg_hear_noise_cache    = None    # {race_lower: percent_int}
_sp_thief_base_cache     = None    # {skill_lower: percent_int}
_phb_unskilled_climb_cache = None  # percent_int
_lineage_detection_cache = {}      # {lineage: [(sub_skill, max_succ, die_size), ...]}


def _load_dmg_hear_noise_table():
    """Parse DMG Table 83 (Chance to Hear Noise by Race). The table is laid
    out in row-major order as alternating bands of race-name cells and
    percent-value cells (e.g. 3 names, then 3 values; the second band has
    its own 3 names + 3 values, separated by blank cells). We walk every
    cell in document order, sort label-vs-value by content (label = words,
    value = starts with N%), then pair adjacent groups in 1:1 order.
    Returns {race_lower: percent_int}."""
    global _dmg_hear_noise_cache
    if _dmg_hear_noise_cache is not None:
        return _dmg_hear_noise_cache
    out = {}
    path = os.path.join(SOURCE_BASE, _DMG_HEAR_NOISE_FILE)
    if os.path.exists(path):
        with open(path, 'r', encoding='cp1252') as f:
            soup = BeautifulSoup(f.read(), 'html.parser')
        labels, percents = [], []
        for table in soup.find_all('table'):
            for tr in table.find_all('tr'):
                for td in tr.find_all(['td','th']):
                    txt = td.get_text(' ', strip=True)
                    if not txt: continue
                    pm = re.match(r'^\s*(\d+)\s*%', txt)
                    if pm:
                        percents.append(int(pm.group(1)))
                    elif re.match(r'^[A-Za-z][A-Za-z\- ]+$', txt):
                        labels.append(txt)
        for lab, pct in zip(labels, percents):
            out[lab.lower()] = pct
    _dmg_hear_noise_cache = out
    return out


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
    See ARS_MECHANICS.md for the patterns used here.

    Applied automatically by the race writer whenever an ability has no
    explicit `changes` already (so labels like "Racial Ability Modifiers"
    or "Base Movement N" that already carry hand-built changes are NOT
    overridden)."""
    if not raw_name: return []
    # Strip the cost suffix if present so both 'Stealth' and 'Stealth (10 CP)'
    # match the same patterns.
    raw_name = re.sub(r'\s*\(\d+\s*CP\)\s*$', '', raw_name)
    low = raw_name.lower()

    def _save(props, formula='2'):
        return [{'key':'system.mods.saves.all','type':'custom',
                 'value':{'formula':formula,'properties':props},
                 'priority':20,'phase':'initial'}]
    def _add(key, val):
        return [{'key':key,'type':'add','value':str(val),
                 'priority':20,'phase':'initial'}]

    # ── Specific stat bonuses ──
    if low == 'attack bonus':            return _add('system.mods.attack.value', 1)
    if low == 'damage bonus':            return _add('system.mods.damage.value', 1)
    if low == 'aim bonus':               return _add('system.mods.attack.ranged', 1)
    if low == 'melee combat' or low == 'melee combat bonus':
                                         return _add('system.mods.attack.melee', 1)
    if low in ('hit point bonus','health bonus','fitness bonus',
               'constitution/health bonus'):
                                         return _add('system.attributes.hp.base', 1)
    if low in ('defensive bonus','tough hide','dense skin'):
                                         return _add('system.mods.ac.value', -1)
    if low == 'magic resistance':        return _add('system.mods.magic.resist', 10)
    if low == 'experience bonus':        return _add('system.mods.formula.xp', '@rank.levels.max*0.10')

    # ── Save bonuses (descending tags: properties filter the save trigger) ──
    if low == 'cold resistance':         return _save('cold')
    if low == 'heat resistance':         return _save('fire')
    if low == 'poison resistance':       return _save('poison')
    if low in ('saving throw bonuses','saving throw bonus','save bonus'):
                                         return _save('', '1')   # +1 all saves
    if low == 'resistance':              return _save('', '1')

    # ── Weapon bonuses (over-approximation: +1 to the matching attack mode) ──
    MELEE_WEAPONS = ('axe','warhammer','mace','sword bonus','short sword',
                     'dagger bonus','spear bonus','pick bonus','trident bonus')
    RANGED_WEAPONS = ('bow bonus','crossbow','dart bonus','sling bonus',
                      'javelin bonus')
    if any(w in low for w in MELEE_WEAPONS):
        return _add('system.mods.attack.melee', 1)
    if any(w in low for w in RANGED_WEAPONS):
        return _add('system.mods.attack.ranged', 1)

    # ── Infravision (S&P infravision is 60 ft for all races that buy it) ──
    if low == 'infravision':
        return [{'key':'special.vision','type':'custom',
                 'value':{'range':60,'angle':'360','mode':'basic'},
                 'priority':20,'phase':'initial'}]

    # ── Stealth (boost both Hide in Shadows and Move Silently) ──
    if low == 'stealth':
        return [
            {'key':'system.mods.skill.hide-in-shadows','type':'add','value':'10',
             'priority':20,'phase':'initial'},
            {'key':'system.mods.skill.move-silently','type':'add','value':'10',
             'priority':20,'phase':'initial'},
        ]
    if low == 'hide':
        return _add('system.mods.skill.hide-in-shadows', 10)

    # ── Secret Doors (Half-elf / Halfling / Human 'Secret Doors' bullet) ──
    # The buy gives the elf-style passive 1/6 detection. No stock immunity
    # or vision key fits; descriptive-only is the honest answer for now.
    # ── Everything else: descriptive only ──
    return []


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
        return [_make_action_group(nm, icon, [
            _make_action(nm, type_='cast', img=icon,
                         save_type=save, charges_per_day=1)
        ])]

    # ── Spell Abilities (Elf 15 CP): six spell-like abilities granted
    # by level. Build six separate 1/day actions in one group.
    if low == 'spell abilities':
        ELF_SPELLS = [
            ('Faerie Fire',     'spell'),
            ('Dancing Lights',  'none'),
            ('Darkness',        'none'),
            ('Levitate',        'spell'),
            ('Detect Magic',    'none'),
            ('Know Alignment',  'none'),
        ]
        return [_make_action_group('Elven Spell Abilities', _FOUNDRY_ICON_DEFAULT, [
            _make_action(nm, type_='cast', save_type=save, charges_per_day=1)
            for nm, save in ELF_SPELLS
        ])]

    # ── Paralyzing Bite (Thri-kreen): melee attack → damage → save vs paralysis
    if low == 'paralyzing bite':
        return [_make_action_group('Paralyzing Bite', _FOUNDRY_ICON_DEFAULT, [
            _make_action('Bite Attack', type_='melee', targeting='single',
                         speed=4),
            _make_action('Damage', type_='damage', targeting='single',
                         formula='1d4', damage_type='piercing'),
            _make_action('Paralysis Save', type_='effect',
                         targeting='single', save_type='paralyzation',
                         effect_changes=[{
                             'key':'special.status', 'type':'custom',
                             'value':'paralysis', 'priority':20,
                             'phase':'initial'}]),
        ])]

    # ── Charge Attack: cast + damage at 2x speed; standard SP charge rule
    if low == 'charge attack':
        return [_make_action_group('Charge Attack', _FOUNDRY_ICON_DEFAULT, [
            _make_action('Charge (move + attack)', type_='melee',
                         targeting='single', speed=0),
            _make_action('Charge Damage', type_='damage',
                         targeting='single', formula='2', damage_type=''),
        ])]

    # ── Leap (Thri-kreen / others): single-use movement burst
    if low == 'leap':
        return [_make_action_group('Leap', _FOUNDRY_ICON_DEFAULT, [
            _make_action('Leap', type_='cast', targeting='self'),
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
              and (ft.get('color') or '').lower() == '#800080']
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
        if isinstance(elem, NavigableString): continue
        if elem.name == 'font' and elem.get('size') == '3' \
                and (elem.get('color') or '').lower() == '#ff0000' \
                and elem.find('b'):
            txt = elem.get_text(' ', strip=True)
            if txt.endswith(':'):
                _flush()
                current_label = txt.rstrip(':').strip()
                current_chunks = []
                continue
        if elem.name == 'font' and elem.get('size') == '3' \
                and (elem.get('color') or '').lower() != '#ff0000':
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
    for base in ('Dwarf','Elf','Gnome','Halfling'):
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
                race, ac, hp, mv, attacks, chars = cells[:6]
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
            text = getattr(prev, 'get_text', lambda *a, **k: str(prev))(' ', strip=True)
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
    out = {'offensive': None, 'defensive': None}
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
                 effect_changes=None, description=''):
    """Build one ARS Action dict. Defaults work for a click-to-cast trigger
    that posts a chat card. Override per pattern (see ARS_MECHANICS §3)."""
    resource = {"type": "none", "itemId": "", "reusetime": "",
                "count": {"cost": 0, "min": 0, "max": 0, "value": 0}}
    if charges_per_day > 0:
        resource = {"type": "charges", "itemId": "", "reusetime": "day",
                    "count": {"cost": 1, "min": 0, "max": charges_per_day,
                              "value": charges_per_day}}
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
                     description='', effect_id=None):
    """Build a standalone ARS ActiveEffect document (subtype 'base').

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
                     "originUuid": "", "effectUuid": ""},
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
        if getattr(child, 'name', None) != 'font':
            continue
        if child.get('size') != '4':
            continue
        col = (child.get('color') or '').lower()
        if col not in ('#ff0000', '#800080'):
            continue
        boundaries.append(child)

    # Find the sub-race heading whose text starts with our prefix
    target_idx = -1
    for i, ft in enumerate(boundaries):
        col = (ft.get('color') or '').lower()
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


_CLASS_MAX_RANKS = 20   # 2e PHB Table 14 stops at level 20; beyond-20 is a
                         # fixed per-level XP/HP rule that ARS handles via
                         # features.lasthitdice and post-cap config.


def _build_class_ranks(cls):
    """Emit the ARSClassRank list from CLASS.DAT-extracted xp/thaco tables.
    Capped at level 20 (standard 2e advancement-table length). Only fields
    CLASS.DAT actually carries are populated; saves, spell slots, BAB,
    attacks-per-round, psionics, caster level default to the schema's
    neutral initial values."""
    xp_table    = cls.get('xp_table',    []) or []
    thaco_table = cls.get('thaco_table', []) or []
    hit_die     = cls.get('hit_die')
    real_n = min(_CLASS_MAX_RANKS, max(len(xp_table), len(thaco_table)))
    if real_n == 0:
        return []
    ranks = []
    for i in range(real_n):
        level = i + 1
        # After hit_dice_cap, HD rolls stop (flat HP per level via
        # features.lasthitdice). We still emit the rank with the same
        # hdformula since OSRIC does (the engine drops the HD roll itself).
        hdformula = f"d{hit_die}" if hit_die else "1d6"
        ranks.append({
            "level":         level,
            "thaco":         thaco_table[i] if i < len(thaco_table) else 20,
            "bab":           0,
            "numatks":       "1/1",
            "turnLevel":     0,
            "xp":            xp_table[i] if i < len(xp_table) else 0,
            "hdformula":     hdformula,
            "baseMove":      None,
            "baseAC":        None,
            "classpoints":   0,
            "title":         "",
            # Saves default to 20 (always-fail) — CLASS.DAT doesn't ship
            # per-level saving-throw tables in our decoded subset.
            "paralyzation": 20, "poison": 20, "death": 20, "rod": 20, "staff": 20,
            "wand": 20, "petrification": 20, "polymorph": 20, "breath": 20, "spell": 20,
            # Spell-slot arrays default to all-zero. CLASS.DAT doesn't ship
            # them in our decoded subset; populate later if extracted.
            "arcane": [0]*10,
            "divine": [0]*8,
            "psionic": {"disciplines": 0, "sciences": 0, "devotions": 0,
                        "defenseModes": 0, "psp": 0},
            "casterlevel": {"arcane": 0, "divine": 0, "psionic": 1},
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


def make_class_item(cls):
    """Build an ARS `class` Item from a parsed CLASS.DAT record. Emits the typed
    `system.ranks[]` advancement table (per-level THAC0/saves/XP/HD/spell-slots,
    built by _build_class_ranks) plus class-level features (matrixTable,
    lasthitdice, proficiencies). See CLAUDE.md "Class item conventions"."""
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
            "description": "",
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
                # No starting/earnLevel data in CLASS.DAT; emit schema defaults.
                "penalty":  0,
                "weapon":   {"starting": 0, "earnLevel": 0},
                "skill":    {"starting": 0, "earnLevel": 0},
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
_DAMAGE_TYPE_LABELS = {
    'B': 'bludgeoning', 'P': 'piercing', 'S': 'slashing',
    'B/P': 'bludgeoning_or_piercing', 'P/S': 'piercing_or_slashing', 'B/S': 'bludgeoning_or_slashing',
}


def _ars_damage_type(dt):
    """Map a PARTS.DAT damage-type code to a valid ARS damage type. Multi-type
    weapons (P/S) collapse to their first component; ARS stores a single type."""
    return {'B': 'bludgeoning', 'P': 'piercing', 'S': 'slashing',
            'P/S': 'piercing', 'B/P': 'bludgeoning', 'B/S': 'bludgeoning'}.get(dt, 'none')


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
    if part['name'].lower().startswith('potion'):
        ftype = 'potion'
    elif has_weapon_traits:
        ftype = 'weapon'
    elif is_armor:
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

    description = lookup_html_description(base_name, _ITEM_HTML_BOOKS)

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

    if ftype == 'weapon':
        rof = part.get('rof')
        per_round = f"{rof['num']}/{rof['den']}" if rof else "1/1"
        system["attack"] = {
            "speed": part.get('speed', 0),
            "type": "melee",
            "perRound": per_round,
            "modifier": 0, "magicBonus": magic_bonus, "magicPotency": 0,
            "range": {"short": "", "medium": "", "long": ""},
            "primary": False, "speedmod": "",
        }
        system["damage"] = {
            "type": _ars_damage_type(part.get('damage_type', '')),
            "normal": part.get('dmg_normal', "0"),
            "large":  part.get('dmg_large', "0"),
            "otherdmg": [], "modifier": 0, "magicBonus": magic_bonus,
        }
        system["weaponstyle"] = ""
        # Weapon has a top-level system.size (schema initial "medium") in addition
        # to attributes.size; set it so non-medium weapons don't all read medium.
        system["size"] = size
        system["actionGroups"] = []
    elif ftype == 'armor':
        # PARTS.DAT carries the base (unmodified) AC and the magic bonus
        # separately; ARS applies the modifier on top of the base.
        system["protection"] = {
            "type": "armor",
            "ac": part.get('armor_class', 10),
            "modifier": magic_bonus,
            "bulk": "none",
            "points": {"min": 0, "max": 0, "value": 0},
        }
        system["armorstyle"] = ""
        system["actionGroups"] = []

    flags = {"adnd2": {"partId": part.get('item_id')}}
    if part.get('restricted_classes'):
        flags["adnd2"]["restrictedClasses"] = part['restricted_classes']

    return {
        "_id": item_id,
        "name": part['name'],
        "type": ftype,
        "img": img_path or "icons/svg/item-bag.svg",
        "system": system,
        "effects": [], "flags": flags,
        "folder": None, "sort": 0, "ownership": {"default": -1}, "_stats": _stats_block(),
    }


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
_SPELL_DESC_HTM_INDEX = {
    28:  'PHB/PHB00433.HTM',
    327: 'TOM/TOM00067.HTM',
    743: 'TOM/TOM00169.HTM',
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
    path (a location reference; the text is read at runtime)."""
    path = os.path.join(SOURCE_BASE, rel)
    if not os.path.exists(path):
        return ''
    try:
        src_dir_files = {f.upper(): f for f in os.listdir(os.path.dirname(path))}
        return clean_html_file(path, rel.split('/')[0], src_dir_files)
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
_SPELL_REVERSE_INDEX = [
    (430, 439), (431, 438), (432, 440), (433, 46), (434, 50), (437, 66),
    (441, 109), (443, 154), (447, 196), (475, 389), (870, 579), (871, 479),
    (872, 546), (874, 23), (876, 482), (877, 505), (878, 533), (889, 508),
    (890, 581), (891, 538), (896, 559), (899, 583), (900, 629), (901, 592),
    (903, 237), (906, 584), (909, 521), (884, 588), (885, 589),
]
_SPELL_LOOKUP_REDIRECT_INDEX = [
    (409, 85), (435, 84), (436, 50), (442, 118), (444, 892), (471, 473),
    (472, 473), (476, 546), (881, 50), (904, 473), (907, 473), (908, 559),
    (929, 473), (930, 473),
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
    rev2prim, prim2rev = {}, {}
    for ri, pi in _SPELL_REVERSE_INDEX:        # true reversibles → both directions
        r, p = nm(ri), nm(pi)
        if r and p:
            rev2prim.setdefault(r, p)
            prim2rev.setdefault(p, r)
    for ri, pi in _SPELL_LOOKUP_REDIRECT_INDEX:  # variants → lookup direction only
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


def _make_spell_action_groups(name, save_type, damage_formula, spell_type,
                              targeting='single'):
    """Build the actionGroup(s) for a spell item. Minimum: one 'cast'
    action posting a chat card (with the spell's save type so the GM gets
    the right Roll Save button). When a dice formula was sniffed from
    the description, append either a 'heal' or 'damage' action depending
    on the spell name (Cure/Cause/etc.), chained behind the cast."""
    actions = [
        _make_action(name, type_='cast', targeting=targeting,
                     save_type=save_type, save_formula=''),
    ]
    if damage_formula:
        eff_type = _spell_effect_action_type(name)
        label    = 'Healing' if eff_type == 'heal' else 'Damage'
        actions.append(
            _make_action(label, type_=eff_type, targeting=targeting,
                         formula=damage_formula, damage_type='')
        )
    return [_make_action_group(name, '', actions)]


def _parse_spell_components(raw):
    """Parse SPELLS.DAT components string ('V', 'V, S', 'V, S, M', etc.)
    into the ARS bool dict {verbal, somatic, material}. Per ARSItemSpell
    schema (and confirmed against osric-compendium 2026.05.20), keys are
    spelled out — NOT the V/S/M shorthand used by older drafts."""
    if not raw: return {'verbal': False, 'somatic': False, 'material': False}
    toks = {t.strip().upper() for t in re.split(r'[,\s/]+', raw) if t.strip()}
    return {'verbal': 'V' in toks, 'somatic': 'S' in toks, 'material': 'M' in toks}


def make_spell_item(spell, desc_override_rel=None):
    """Build an ARS `spell` Item from a parsed SPELLS.DAT record. Spell metadata
    (level/school/sphere/range/components/…) lives at the top of system.{}, and
    `system.type` ("Arcane"/"Divine") is set explicitly (required field — see
    CLAUDE.md). Adds a cast actionGroup (+ a damage/heal action when a dice
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
    return {
        "_id": item_id,
        "name": spell['name'],
        "type": "spell",
        "img": pick_spell_icon(spell['name'], school, sphere),
        "system": {
            "description":  description,
            "type":         spell_type,
            "level":        spell.get('level', 0),
            "school":       school,
            "sphere":       sphere,
            "range":        spell.get('range', ''),
            "components":   _parse_spell_components(spell.get('components','')),
            "durationText": spell.get('duration', ''),
            "castingTime":  spell.get('casting_time', ''),
            "areaOfEffect": spell.get('area_of_effect', ''),
            "save":         spell.get('saving_throw', ''),
            "learned":      False,
            "actionGroups": _make_spell_action_groups(
                                spell['name'], save_type, dmg, spell_type,
                                targeting=targeting),
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


def make_power_item(power):
    """Build an ARS `power` Item (psionic power) from a parsed PSIONIC.DAT record.
    See the schema note below and CLAUDE.md."""
    item_id = make_id()
    # ARSItemPower schema: range / areaOfEffect / prerequisites (note the
    # trailing 's') / ability / formula etc. live at the TOP of system.{},
    # not under system.attributes. The legacy 'power_score' field maps to
    # 'abilityMod' (the modifier applied to the d20 roll-under check).
    disc_idx = power.get('discipline', -1)
    return {
        "_id": item_id,
        "name": power['name'],
        "type": "power",
        "img": _power_icon(power['name']),
        "system": {
            "description":   "",
            "discipline":    _DISC_NAMES.get(disc_idx, ''),
            "range":         power.get('range', ''),
            "areaOfEffect":  power.get('area_of_effect', '') or 'personal',
            "prerequisites": power.get('prerequisite', '') or 'none',
            "abilityMod":    str(power.get('power_score', '')) or '0',
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


def _parse_mm_statblock(biography_html):
    """Parse the MM stat-block table from the (already-built) biography HTML
    into {NORMALIZED_LABEL: first_value_column}. Labels are uppercased with the
    trailing colon stripped (e.g. "NO. OF ATTACKS"). The first value column is
    taken (multi-form blocks like Orc/Orog list several). Returns {} if absent.
    This replaces the fragile tail-strings heuristic for #attacks / morale /
    special attacks & defenses / damage etc. — all read straight from the MM."""
    out = {}
    if not biography_html or '<table' not in biography_html.lower():
        return out
    tbl = BeautifulSoup(biography_html, 'html.parser').find('table')
    if not tbl:
        return out
    for tr in tbl.find_all('tr'):
        cells = tr.find_all('td')
        if len(cells) < 2:
            continue
        label = cells[0].get_text(' ', strip=True).upper().rstrip(':').strip()
        value = cells[1].get_text(' ', strip=True)
        if label and label not in out:
            out[label] = value
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
    main = re.split(r'\bor\b', damage_str, 1)[0]
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


def make_monster_actor(monster, img_path=None, categories=None, fighter_saves=None):
    """Build an ARS `npc` Actor from a parsed MONSTER.DAT record. AC/THAC0/HD/XP
    come from the DAT; the MM stat-block table (parsed from the matched .HTM
    biography by _parse_mm_statblock) fills #attacks/morale/damage/special
    atk-def/size; saves are derived from the CLASS.DAT fighter table at level=HD
    (`fighter_saves`); `categories` are broad taxonomy tags appended to
    details.type for cross-actor type-trigger effects; a Natural Weaponry
    actionGroup makes the monster click-to-attack. See CLAUDE.md "NPC schema"."""
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
    sb = _parse_mm_statblock(biography)
    # Damage string: labeled MM stat-block value, with the DAT value as the
    # fallback for multi-column blocks where the stat-block cell is truncated.
    # Reused for system.damage and the Natural Weaponry action group.
    damage_src = _pick_damage(monster.get('damage'), sb.get('DAMAGE/ATTACK', ''))
    # Build attributes from MONSTER.DAT-extracted fields. Each numeric value
    # is only set when MONSTER.DAT actually yielded one; no hand-typed defaults
    # (AC 10, THAC0 20, MV 12, HD 1, etc.) are substituted.
    attributes = {
        "hp":     {"value": 0, "min": 0, "max": 0, "temp": 0, "tempmax": 0, "base": 0},
        "init":   {"value": 0, "modifier": 0},
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
        "biography": {"value": biography, "public": ""},
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

def _open_pack(path):
    """Wipe and recreate a LevelDB pack directory, returning an open plyvel DB.
    The wipe is what makes each run idempotent (output regenerated from scratch)."""
    if os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)
    return plyvel.DB(path, create_if_missing=True)


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
    low  = race_name.lower()
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
    movement = race.get('movement')
    runtime  = extract_race_runtime_data(race['name'])
    infravision_ft       = runtime['infravision_ft']
    extracted_abilities  = runtime['abilities']

    out = []
    seen_labels = set()
    def _push(label, icon, changes=None):
        if not label or label in seen_labels: return
        seen_labels.add(label)
        out.append((label, icon, changes))

    # Race-specific stat mod (always unique to the race name → never shared)
    stat_changes = make_race_stat_mod_changes(race.get('ability_adjustments'))
    if stat_changes:
        _push(f"{race['name']} Racial Ability Modifiers",
              _FOUNDRY_ICON_RACEMOD, stat_changes)

    if movement is not None:
        movement_ft = movement * 10     # PHB "MV 12" → 120 ft/round
        _push(f"Base Movement {movement_ft}",
              pick_race_ability_icon("Base Movement"),
              [{"key": "system.mods.movement.base", "type": "override",
                "value": str(movement_ft), "priority": 20, "phase": "initial"}])

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

    # 4. Secret doors: elf/half-elf/halfling get 3 elf-style skills
    #    (passing 1d6≤1, searching secret 1d6≤2, searching concealed
    #    1d6≤3 — PHB elf entry and S&P SP00025); every other lineage
    #    gets the universal "Detect Secret Doors" 1d6 ≤ 1 skill.
    if base in ('Elf','Half-elf','Halfling'):
        prefix = base
        out.append({'name': f'{prefix}: Passing Secret Doors',
                    'formula':'1d6','target':1,'type':'decending','groups':'',
                    'description':'','icon':_FOUNDRY_ICON_SKILL_SECRET})
        out.append({'name': f'{prefix}: Searching Secret Doors',
                    'formula':'1d6','target':2,'type':'decending','groups':'',
                    'description':'','icon':_FOUNDRY_ICON_SKILL_SECRET})
        out.append({'name': f'{prefix}: Searching Concealed Doors',
                    'formula':'1d6','target':3,'type':'decending','groups':'',
                    'description':'','icon':_FOUNDRY_ICON_SKILL_SECRET})
    else:
        out.append({'name':'Detect Secret Doors',
                    'formula':'1d6','target':1,'type':'decending','groups':'',
                    'description':'','icon':_FOUNDRY_ICON_SKILL_SECRET})

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
    race_abilities = []   # [(race_dict, [(label, icon, changes), ...])]
    label_count    = {}   # label → number of races emitting it
    for race in races:
        abs_ = _race_abilities_for(race)
        race_abilities.append((race, abs_))
        for (lab, _icon, _chg) in abs_:
            label_count[lab] = label_count.get(lab, 0) + 1

    # ── 3. Second pass: mint shared ability docs once, per-race specifics inline ──
    shared_doc_id = {}   # label → ability_doc_id  (lookup table for sharing)
    shared_pool_specs = []  # (label, icon, changes) for the eventually-shared ones

    # Collect spec for each shared label using its first occurrence
    seen_shared = set()
    for _race, abs_ in race_abilities:
        for (lab, icon, chg) in abs_:
            if label_count[lab] >= 2 and lab not in seen_shared:
                seen_shared.add(lab)
                shared_pool_specs.append((lab, icon, chg))

    # Write the shared abilities (one Ability item per label, into the Shared folder)
    action_groups_written = 0
    for (lab, icon, chg) in shared_pool_specs:
        chg = chg or _ability_effect_changes(lab)
        acts = _ability_actions(lab)
        ab, ef = make_ability_item(lab, icon,
                                   description=_ability_description(lab),
                                   effect_changes=chg,
                                   action_groups=(acts or None))
        if acts: action_groups_written += len(acts)
        ab['folder'] = shared_abilities_folder
        shared_doc_id[lab] = ab['_id']
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
        for (lab, icon, chg) in abs_:
            if lab in shared_doc_id:
                ab_id = shared_doc_id[lab]
                ab_img = icon
            else:
                # Race-specific: mint a new Ability item now
                chg = chg or _ability_effect_changes(lab)
                acts = _ability_actions(lab)
                ab, ef = make_ability_item(lab, icon,
                                            description=_ability_description(lab),
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
                "name": lab,
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
        # Direct-on-race combat-bonus effects (dwarf/gnome to-hit + defensive
        # vs giant-kin). Parsed from the PHB chapter at runtime; placed straight
        # on the race doc like OSRIC's "Dwarf Attack Bonuses" etc.
        combat_effects = _build_race_combat_effect_docs(
            race['name'], race_id, category_members=taxonomy_members)
        if combat_effects:
            race_item['effects'] = [e['_id'] for e in combat_effects]
            for ef in combat_effects:
                db.put(f'!items.effects!{race_id}.{ef["_id"]}'.encode(),
                       json.dumps(ef).encode())
                effects_written += 1
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


def migrate_classes():
    """Phase 3: write the classes pack — a `class` Item per CLASS.DAT record,
    foldered by group (Warriors/Rogues/Priests/Wizards/Psionicists). Returns count."""
    print("\n=== Classes (CLASS.DAT) ===")
    classes = parse_classes()
    if not classes:
        print("  No classes parsed."); return 0
    db = _open_pack(OUTPUT_PACKS['classes'])
    # Folder hierarchy by class group (from CLASS.DAT). Specialist wizards
    # all live under 'Wizard' too; 'Psionicist' is its own group from PSIONIC.DAT.
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
    count = 0
    for cls in classes:
        item = make_class_item(cls)
        bucket = GROUP_FOLDER.get(cls.get('group',''), None)
        if bucket:
            item['folder'] = folders[bucket]['_id']
        db.put(f'!items!{item["_id"]}'.encode(), json.dumps(item).encode())
        count += 1
    for f in folders.values():
        db.put(f'!folders!{f["_id"]}'.encode(), json.dumps(f).encode())
    db.close()
    print(f"  → {count} classes in {len(folders)} folders")
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
    for part in parts:
        img = extract_equip_icon(part.get('icon_id'), OUTPUT_IMG_ITEMS)
        item = make_part_item(part, img, base_weapons=base_weapons)
        bucket = TYPE_FOLDER.get(item.get('type'), 'Other Items')
        item['folder'] = folders[bucket]['_id']
        db.put(f'!items!{item["_id"]}'.encode(), json.dumps(item).encode())
        count += 1
        if count % 1000 == 0:
            print(f"    {count} items...")
    for f in folders.values():
        db.put(f'!folders!{f["_id"]}'.encode(), json.dumps(f).encode())
    db.close()
    print(f"  → {count} items in {len(folders)} folders")
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
    """Phase 3: write the powers pack — a `power` Item per PSIONIC.DAT record.
    Returns count."""
    print("\n=== Psionic Powers (PSIONIC.DAT) ===")
    powers = parse_psionics()
    if not powers:
        print("  No powers parsed."); return 0
    db = _open_pack(OUTPUT_PACKS['powers'])
    count = 0
    for power in powers:
        item = make_power_item(power)
        db.put(f'!items!{item["_id"]}'.encode(), json.dumps(item).encode())
        count += 1
    db.close()
    print(f"  → {count} psionic powers written to {OUTPUT_PACKS['powers']}")
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


def _lookup_montype_bmp(name, display_name, montype_data):
    """Resolve a monster to its MONTYPE individual-icon filename (or None)."""
    key = _match_montype_key(name, display_name, montype_data)
    return montype_data.get(key) if key else None


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

    count = 0
    for monster in monsters:
        key  = _match_montype_key(monster.get('name',''), monster.get('display_name',''), montype_data)
        bmp  = montype_data.get(key) if key else None
        img  = extract_monster_icon(bmp, OUTPUT_IMG_MONSTERS) if bmp else None
        cats = _monster_categories(key, name_to_index, idx2cat)
        actor = make_monster_actor(monster, img, categories=cats,
                                   fighter_saves=fighter_saves)
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
        if node.name == 'font' and node.get('color','').lower() == '#ff0000':
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
                    if a.get('href','').endswith('.htm') and '#' not in a['href']:
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
    """Parse SP00456 → {name_lower: {groups: set, cp_cost, initial, ability, anchor}}."""
    global _sp_nwp_table_cache
    if _sp_nwp_table_cache is not None: return _sp_nwp_table_cache
    path = os.path.join(SOURCE_BASE, 'SP', 'SP00456.HTM')
    out = {}
    if not os.path.exists(path):
        _sp_nwp_table_cache = out; return out
    soup = BeautifulSoup(open(path, encoding='cp1252').read(), 'html.parser')
    cur_group = None
    for node in soup.find_all(['font', 'table']):
        if node.name == 'font' and node.get('color','').lower() == '#ff0000':
            raw = node.get_text(strip=True).rstrip(':').title()
            if raw in ('General','Priest','Rogue','Warrior','Wizard','Psionicist'):
                cur_group = raw
            continue
        if node.name == 'table' and cur_group:
            for tr in node.find_all('tr'):
                cells = [td.get_text(strip=True) for td in tr.find_all('td')]
                if len(cells) < 4: continue
                name = cells[0]
                if name in ('','Proficiency') or 'Cost' in cells[1]: continue
                try: cp = int(cells[1])
                except ValueError: continue
                try: init = int(cells[2])
                except ValueError: init = 0
                ability = cells[3]
                href = None
                for a in tr.find_all('a', href=True):
                    if a.get('href','').endswith('.htm') or '#' in a.get('href',''):
                        href = a['href'].split('#')[0]; break
                key = name.lower()
                if key not in out:
                    out[key] = {'name': name, 'groups': set(), 'cp_cost': cp,
                                'initial': init, 'ability': ability, 'anchor': href}
                out[key]['groups'].add(cur_group)
    _sp_nwp_table_cache = out
    return out


# ── Description extractor for individual proficiency pages ──────────────────
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
        if font.get('color','').lower() != '#ff0000': continue
        if font.get('size','') != '3': continue
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
def _index_weapons_by_name(parts):
    """Return {base_name_lower: [(item_id, name)]} keyed by stripped name (no +N)."""
    idx = {}
    for p in parts:
        if not p.get('damage_type'): continue
        if p.get('is_armor'): continue
        base = re.sub(r'\s*[+\-]\d+.*$', '', p.get('name','')).strip().lower()
        if not base: continue
        idx.setdefault(base, []).append(p)
    return idx


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

    # ── Write everything ───────────────────────────────────────────────────
    for item in wp_items:
        db.put(f'!items!{item["_id"]}'.encode(), json.dumps(item).encode())
    for item in fam_items.values():
        db.put(f'!items!{item["_id"]}'.encode(), json.dumps(item).encode())
    for f in (wp_folder, fam_folder):
        db.put(f'!folders!{f["_id"]}'.encode(), json.dumps(f).encode())
    db.close()
    total = len(wp_items) + len(fam_items)
    print(f"  → {len(wp_items)} weapon proficiencies + {len(fam_items)} group familiarities")
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
    merged_base = dict(rogue_base)   # start from PHB to keep order familiar
    for k, v in sp_base.items():
        merged_base.setdefault(k, v)
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

    for f in folders.values():
        db.put(f'!folders!{f["_id"]}'.encode(), json.dumps(f).encode())
    db.close()
    total = sum(counts.values())
    print(f"  → {counts['rogue']} rogue skills, {counts['phb_nwp']} PHB nonweapon profs, "
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

    # Link map: name → (id, name, img, pack-key, type) read back from the skills and
    # weapon-proficiency packs (written earlier in the run). Keyed by _kit_prof_norm
    # so a kit's bonus-prof name resolves to its compendium item.
    link_map = {}
    for packkey in ('skills', 'proficiencies'):
        path = OUTPUT_PACKS[packkey]
        if not os.path.exists(path):
            continue
        pdb = plyvel.DB(path)
        for k, v in pdb:
            if k.startswith(b'!items!'):
                it = json.loads(v)
                link_map.setdefault(_kit_prof_norm(it['name']),
                                    (it['_id'], it['name'], it['img'], packkey, it['type']))
        pdb.close()

    # Cache each handbook dir's case-insensitive filename map for clean_html_file.
    srcfiles = {}
    def _srcfiles(book):
        if book not in srcfiles:
            d = os.path.join(SOURCE_BASE, book)
            srcfiles[book] = ({f.upper(): f for f in os.listdir(d)}
                              if os.path.isdir(d) else {})
        return srcfiles[book]

    count = matched_n = linked = 0
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

        item['folder'] = folders[cls if cls in folders else 'General']['_id']
        db.put(f'!items!{item["_id"]}'.encode(), json.dumps(item).encode())
        count += 1
    db.close()
    print(f"  → {count} background kits ({matched_n} matched to handbook HTM, "
          f"{linked} mandatory bonus-proficiency links)")
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
    items_path = OUTPUT_PACKS['items']
    if os.path.exists(items_path):
        idb = plyvel.DB(items_path)
        for k, v in idb:
            if k.startswith(b'!items!'):
                it = json.loads(v)
                item_uuids.setdefault(it['name'],
                    f"Compendium.{MODULE_ID}.adnd2-items.Item.{it['_id']}")
        idb.close()
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
        "version": SYSTEM_VERSION,
        "compatibility": {"minimum": "14", "verified": "14"},
        "authors": [{"name": "AD&D 2e Compendium Project", "flags": {}}],
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


def main():
    """Entry point: wipe the output module, run Phase 2 (journals) into the
    journals pack, then Phases 3-5 (the migrate_* drivers) into their packs,
    write module.json, and print a summary. Phase 3+ is skipped gracefully if the
    DATABASE/ directory is absent."""
    print("AD&D 2e Migration")
    print(f"Output: {OUTPUT_DB}")

    if os.path.exists(OUTPUT_DB):
        shutil.rmtree(OUTPUT_DB)
    os.makedirs(OUTPUT_DB, exist_ok=True)
    os.makedirs(OUTPUT_IMG, exist_ok=True)

    db = plyvel.DB(OUTPUT_DB, create_if_missing=True)
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
            phase3_stats['classes']       = migrate_classes()
            phase3_stats['spells']        = migrate_spells()
            phase3_stats['powers']        = migrate_psionics()
            phase3_stats['items']         = migrate_items()
            phase3_stats['monsters']      = migrate_monsters()
            phase3_stats['proficiencies'] = migrate_proficiencies()
            phase3_stats['skills']        = migrate_skills()
            phase3_stats['backgrounds']   = migrate_kits()
            phase3_stats['treasure']      = migrate_treasure()
        except Exception as e:
            print(f"  Phase 3 error: {e}")
            import traceback; traceback.print_exc()
    else:
        print(f"\n  Phase 3 skipped: {DATABASE_BASE} not found.")

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
