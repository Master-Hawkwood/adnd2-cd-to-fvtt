# adnd2-cd-to-fvtt

A standalone Python script that converts a personal copy of the **AD&D Core Rules 2.0 Expansion CD-ROM** into a [Foundry VTT](https://foundryvtt.com/) compendium module for the [ARS](https://foundryvtt.com/packages/ars) system (Variant 2).

> **Copyright notice** — The script embeds no copyrighted content from the CD-ROM. It reads your local copy at runtime and generates a module on your machine. Do not redistribute the generated module.

---

## What it generates

Running the script produces an `adnd2-compendium/` directory containing **11 Foundry VTT compendium packs** (~13 000 documents) built entirely from your CD-ROM:

| Pack | Contents |
|---|---|
| `adnd2-journals` | 19 journal entries, 999 pages of rulebook text |
| `adnd2-races` | 53 race items with ability modifiers, movement, and active effects |
| `adnd2-classes` | 26 class items with full per-level advancement tables |
| `adnd2-items` | 4 584 equipment items (weapons, armor, potions, magic items, gems) |
| `adnd2-spells` | 841 arcane and divine spells |
| `adnd2-powers` | 231 psionic powers with discipline and power score |
| `adnd2-monsters` | 1 524 NPC actors with full stat blocks, biography, and icons |
| `adnd2-proficiencies` | 104 weapon proficiency items |
| `adnd2-skills` | 151 skill items (rogue skills + nonweapon proficiencies) |
| `adnd2-backgrounds` | 208 character kit items with mandatory bonus proficiency auto-grants |
| `adnd2-treasure` | 486 treasure roll tables with 3 480 results |

---

## Requirements

- **AD&D Core Rules 2.0 Expansion CD-ROM** — your own personal copy
- **Python 3.8+**
- **Foundry VTT v14** with the **ARS** system installed (Variant 2)

Python dependencies:

```bash
pip install plyvel beautifulsoup4 Pillow
```

> **Linux/macOS** — `plyvel` requires LevelDB. On Fedora/RHEL: `sudo dnf install leveldb-devel`. On Debian/Ubuntu: `sudo apt install libleveldb-dev`.

> **Windows** — see the [Windows instructions](#windows) below before running `pip install`.

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

The output directory (`adnd2-compendium/`) is created alongside the script. You can change its location by editing the `OUTPUT_PACKS` dict and the `OUTPUT_IMG_*` constants.

---

## Running

```bash
python3 migrate.py
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

---

## Windows

`plyvel` wraps LevelDB, which has no official Windows binary distribution. The easiest path is **WSL2**.

### Option 1 — WSL2 (recommended)

1. [Install WSL2](https://learn.microsoft.com/en-us/windows/wsl/install) with Ubuntu (one command in PowerShell: `wsl --install`)
2. Open the Ubuntu terminal and install dependencies:
   ```bash
   sudo apt install python3 python3-pip libleveldb-dev
   pip3 install plyvel beautifulsoup4 Pillow
   ```
3. Access your CD-ROM files from WSL at `/mnt/c/...` (or the drive letter where they live), and update `SOURCE_BASE`, `DATABASE_BASE`, and `BITMAPS_BASE` in `migrate.py` accordingly.
4. Run the script from the WSL terminal:
   ```bash
   python3 migrate.py
   ```
5. The generated `adnd2-compendium/` folder is accessible from Windows Explorer under `\\wsl$\Ubuntu\...` and can be copied to your Foundry `Data/modules/` folder as usual.

### Option 2 — Anaconda / Miniconda (native Windows)

[Miniconda](https://docs.anaconda.com/miniconda/) provides a pre-built `plyvel` package for Windows via conda-forge, with no compilation required.

1. Install [Miniconda](https://docs.anaconda.com/miniconda/) (or Anaconda).
2. Open the **Anaconda Prompt** and install all dependencies in one command:
   ```
   conda install -c conda-forge plyvel beautifulsoup4 pillow
   ```
3. Run the script from the Anaconda Prompt:
   ```
   python migrate.py
   ```
   Note: on Windows the command is `python`, not `python3`.
