# adnd2-cd-to-fvtt

A standalone Python script that converts a personal copy of the **AD&D Core Rules 2.0 Expansion CD-ROM** into a [Foundry VTT](https://foundryvtt.com/) compendium module for the [ARS](https://foundryvtt.com/packages/ars) system (Variant 2).

> **Copyright notice** — The script embeds no copyrighted content from the CD-ROM. It reads your local copy at runtime and generates a module on your machine. Do not redistribute the generated module.

---

## What it generates

Running the script produces an `adnd2-compendium/` directory containing **11 Foundry VTT compendium packs** (~10 000 documents) built entirely from your CD-ROM:

| Pack | Contents |
|---|---|
| `adnd2-journals` | 19 journal entries, 999 pages of rulebook text |
| `adnd2-races` | 53 race items (45 base + 8 CP variants) with ability modifiers, movement, and active effects |
| `adnd2-classes` | 43 class items (26 base + 17 CP variants) with per-level advancement and PHB abilities |
| `adnd2-items` | 4 068 equipment items (weapons, armor, potions, magic items, gems) with AEG/DMG/C&T descriptions, gem prices, and use/heal/damage actions for consumables |
| `adnd2-spells` | 933 arcane and divine spells with cast actions (every reversible spell's reverse form is linked as a child of its primary) |
| `adnd2-powers` | 241 psionic powers with discipline and power score, plus 5 attack modes and 5 defense modes |
| `adnd2-monsters` | 1 524 NPC actors with full stat blocks, biography, and icons |
| `adnd2-proficiencies` | 106 weapon proficiency items |
| `adnd2-skills` | 162 skill items (rogue skills with Bard/Ranger score variants + PHB and S&P nonweapon proficiencies) |
| `adnd2-backgrounds` | 208 character kit items with bonus proficiency auto-grants and benefit/hindrance abilities |
| `adnd2-treasure` | 486 treasure roll tables with 3 480 results |

---

## Requirements

- **AD&D Core Rules 2.0 Expansion CD-ROM** — your own personal copy

  > **Important:** you need the **2.0 Expansion** (pictured below), not the original Core Rules CD-ROM.
  > The Expansion contains the additional rulebooks (Complete Handbooks, Player's Options, etc.) that
  > this script depends on.
  >
  > <a href="https://www.tsrarchive.com/add/add-cd2x.jpg"><img src="https://www.tsrarchive.com/add/add-cd2x.jpg" alt="AD&amp;D Core Rules 2.0 Expansion CD-ROM" width="50%"></a>
- **Python 3.8+**
- **Node.js** with [fvtt-cli](https://github.com/foundryvtt/foundryvtt-cli) — used to write the Foundry LevelDB packs
- **Foundry VTT v14** with the **ARS** system installed (Variant 2)

Install fvtt-cli globally:

```bash
npm install -g @foundryvtt/foundryvtt-cli
```

Install Python dependencies:

```bash
pip install beautifulsoup4 Pillow
```

---

## Setup

**1. Organize your files**

Place the CD-ROM content and the script so they sit side by side:

```
your-working-dir/
  migrate.py
  cd-rom/
    DATABASE/     ← *.DAT binary files
    MACBOOKS/
      HTML/       ← rulebook HTML files
    BITMAPS/      ← icon sprite sheets
```

**2. Configure paths** *(if your layout differs)*

Open `migrate.py` and adjust the constants at the top of the file:

```python
SOURCE_BASE   = "cd-rom/MACBOOKS/HTML"   # path to the HTML rulebooks
DATABASE_BASE = "cd-rom/DATABASE"         # path to the .DAT files
BITMAPS_BASE  = "cd-rom/BITMAPS"          # path to the bitmap icons
```

If `fvtt` is not on your PATH, change this constant to `'npx @foundryvtt/foundryvtt-cli'`:

```python
_FVTT_CLI_CMD = 'fvtt'
```

The output directory (`adnd2-compendium/`) is created alongside the script. You can change its location by editing the `OUTPUT_PACKS` dict and the `OUTPUT_IMG_*` constants.

---

## Running

```bash
python3 migrate.py
```

On Windows:

```
python migrate.py
```

The script is **idempotent** — re-running it deletes and fully regenerates the output from scratch. Expect it to take a few minutes on first run.

---

## Installing in Foundry VTT

Copy the generated `adnd2-compendium/` folder into your Foundry `Data/modules/` directory:

```
[Foundry User Data]/
  Data/
    modules/
      adnd2-compendium/   ← copy here
```

Then in Foundry: **Settings → Manage Modules → AD&D 2e Compendium → Enable**.

