# ESPNcricinfo Player Scraper

One script. Self-bootstrapping. Run on any Mac with Chrome + Python 3.11+.
First run auto-creates venv and installs all dependencies.

---

## This session's machine plan

| Machine    | Number | Tonight (profile)                           | Overnight (deep)                         |
|------------|--------|---------------------------------------------|------------------------------------------|
| M4 Air     | 1      | `python3 run.py --machine 1 --mode profile` | —                                        |
| M2 Mini    | 2      | `python3 run.py --machine 2 --mode profile` | `python3 run.py --machine 2 --mode deep` |
| M1 Mini #1 | 3      | `python3 run.py --machine 3 --mode profile` | `python3 run.py --machine 3 --mode deep` |
| M1 Mini #2 | 4      | `python3 run.py --machine 4 --mode profile` | `python3 run.py --machine 4 --mode deep` |

- **profile** = main player page (batting/bowling/role/image/bio) → ~1.5 hrs total
- **deep** = all 4 pages (profile + stats + records + matches) → ~6 hrs total

---

## One-time setup on M2 Mini

```bash
cd scripts/scraper

# Create players.csv (master) + 4 machine CSVs from DB
python3 run.py --init --total 4

# For 5 machines:
python3 run.py --init --total 5
```

Output:
```
players.csv              ← master (all players, never touch this)
chunks/machine_1.csv     ← M4 Air's player list
chunks/machine_2.csv     ← M2 Mini's player list
chunks/machine_3.csv     ← M1 Mini #1's player list
chunks/machine_4.csv     ← M1 Mini #2's player list
```

---

## Setup on each machine

### What to copy to each machine
```bash
# From M2 Mini:
scp scripts/scraper/run.py  user@m4air:~/scraper/
scp scripts/scraper/chunks/machine_1.csv  user@m4air:~/scraper/chunks/
scp scripts/scraper/.env.example  user@m4air:~/scraper/.env

scp scripts/scraper/run.py  user@m1mini1:~/scraper/
scp scripts/scraper/chunks/machine_3.csv  user@m1mini1:~/scraper/chunks/
scp scripts/scraper/.env.example  user@m1mini1:~/scraper/.env

scp scripts/scraper/run.py  user@m1mini2:~/scraper/
scp scripts/scraper/chunks/machine_4.csv  user@m1mini2:~/scraper/chunks/
scp scripts/scraper/.env.example  user@m1mini2:~/scraper/.env
```

### Edit .env on each machine
```bash
nano ~/.env    # or open in editor
```

Set these two values:
```
MACHINE_ID=m4_air          # change per machine: m4_air / m2_mini / m1_mini_1 / m1_mini_2
CHROME_VERSION=146         # check at chrome://version in Chrome
```

### Check Chrome version
Open Chrome → type `chrome://version` in address bar → note the major version number.

---

## Run commands

### Profile scrape (all 4 machines, tonight)

**M4 Air:**
```bash
python3 run.py --machine 1 --mode profile
```

**M2 Mini:**
```bash
python3 run.py --machine 2 --mode profile
```

**M1 Mini #1:**
```bash
python3 run.py --machine 3 --mode profile
```

**M1 Mini #2:**
```bash
python3 run.py --machine 4 --mode profile
```

### Resume after crash (same command, auto-skips done players)
```bash
python3 run.py --machine 1 --mode profile
```

### Deep scrape (M2 Mini + M1 x2, overnight)
```bash
python3 run.py --machine 2 --mode deep   # M2 Mini
python3 run.py --machine 3 --mode deep   # M1 Mini #1
python3 run.py --machine 4 --mode deep   # M1 Mini #2
```

### Check progress on any machine
```bash
python3 run.py --status
```

---

## After scraping — collect and load to DB

### 1. Copy output_json from all machines to M2 Mini
```bash
rsync -av user@m4air:~/scraper/output_json/    scripts/scraper/output_json/
rsync -av user@m1mini1:~/scraper/output_json/  scripts/scraper/output_json/
rsync -av user@m1mini2:~/scraper/output_json/  scripts/scraper/output_json/
```

### 2. Copy updated machine CSVs for progress tracking
```bash
rsync -av user@m4air:~/scraper/chunks/    scripts/scraper/chunks/
rsync -av user@m1mini1:~/scraper/chunks/  scripts/scraper/chunks/
rsync -av user@m1mini2:~/scraper/chunks/  scripts/scraper/chunks/
```

### 3. Check combined progress
```bash
python3 scripts/scraper/run.py --status
```

### 4. Parse HTML + write to DB
```bash
# Test first (no DB writes)
python3 scripts/scraper/run.py --parse --dry-run --limit 50

# Full run
python3 scripts/scraper/run.py --parse
```

---

## Using with different number of machines

Works for any count — just change `--total`:

```bash
# 3 machines
python3 run.py --init --total 3
python3 run.py --machine 1 --mode profile
python3 run.py --machine 2 --mode profile
python3 run.py --machine 3 --mode profile

# 6 machines
python3 run.py --init --total 6
python3 run.py --machine 1 --mode profile
# ... up to machine 6
```

---

## Environment variables (.env)

| Variable         | Default    | Description                                      |
|------------------|------------|--------------------------------------------------|
| `MACHINE_ID`     | unknown    | Label for this machine in CSV                    |
| `CHROME_VERSION` | 146        | Chrome major version (from chrome://version)     |
| `WORKERS`        | 4          | Parallel Chrome workers (reduce to 2 on M1 if slow) |
| `PAGE_DELAY`     | 2.0        | Seconds to wait after page load                  |
| `DB_HOST`        | localhost  | DB — only needed for `--init` and `--parse`      |
| `DB_PORT`        | 5432       |                                                  |
| `DB_NAME`        | krickbiz   |                                                  |
| `DB_USER`        | himanshu   |                                                  |
| `DB_PASSWORD`    | admin      |                                                  |

---

## Folder structure

```
scraper/
  run.py                ← only script needed on every machine
  players.csv           ← master list (created by --init, never modified after)
  .env                  ← per-machine config
  .venv/                ← auto-created on first run
  chunks/
    machine_1.csv       ← M4 Air progress
    machine_2.csv       ← M2 Mini progress
    machine_3.csv       ← M1 Mini #1 progress
    machine_4.csv       ← M1 Mini #2 progress
  output_json/
    {player_id}/
      profile.html      ← always scraped
      stats.html        ← deep mode only
      records.html      ← deep mode only
      matches.html      ← deep mode only
```

---

## Troubleshooting

**"machine_N.csv not found"** → Copy the correct chunk CSV from M2 Mini to this machine.

**"Failed to connect to browser"** → Wrong Chrome version in `.env`. Check `chrome://version`.

**Many errors** → Reduce workers: `python3 run.py --machine 1 --mode profile --workers 2`

**Slow / Akamai blocks** → Increase page delay: set `PAGE_DELAY=3` in `.env`

to run:
cp scripts/scraper/.env.machine_2 scripts/scraper/.env                                                                                                                                   
python3 scripts/scraper/run.py --init --total 4   

output:

[setup] Done. Restarting inside venv...

Connecting to DB...
Total players  : 17,790
Total machines : 4
Chunk size     : ~4,448 per machine

  players.csv           → 17,790 rows  (master, do not modify)
  chunks/machine_1.csv  → 4,448 players
  chunks/machine_2.csv  → 4,448 players
  chunks/machine_3.csv  → 4,448 players
  chunks/machine_4.csv  → 4,446 players

─── Commands for each machine ───────────────────────────────
  Machine 1: python3 run.py --machine 1 --mode profile
  Machine 2: python3 run.py --machine 2 --mode profile
  Machine 3: python3 run.py --machine 3 --mode profile
  Machine 4: python3 run.py --machine 4 --mode profile

─── Overnight deep scrape ────────────────────────────────────
  Machine 1: python3 run.py --machine 1 --mode deep
  Machine 2: python3 run.py --machine 2 --mode deep
  Machine 3: python3 run.py --machine 3 --mode deep
  Machine 4: python3 run.py --machine 4 --mode deep

─── After collecting output_json from all machines ───────────
  python3 run.py --parse --dry-run --limit 50
  python3 run.py --parse