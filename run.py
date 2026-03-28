#!/usr/bin/env python3
"""
ESPNcricinfo Player Scraper — self-bootstrapping, single script.

First run auto-creates venv + installs all deps. Just needs Python 3.11+ and Chrome.

────────────────────────────────────────────────────────────────────────────────
QUICK START
────────────────────────────────────────────────────────────────────────────────

  Step 1 — On main machine (M2 Mini), create master CSV + split into machine CSVs:
      python3 run.py --init --total 4

  Step 2 — Copy to each machine (see README for rsync commands):
      Files needed: run.py  chunks/machine_N.csv  .env

  Step 3 — On each machine, run with its machine number:
      python3 run.py --machine 1 --mode profile
      python3 run.py --machine 2 --mode profile
      python3 run.py --machine 3 --mode profile
      python3 run.py --machine 4 --mode profile

  Step 4 — If it crashes, same command resumes automatically:
      python3 run.py --machine 1 --mode profile

  Step 5 — Overnight deep scrape on machines that have capacity:
      python3 run.py --machine 2 --mode deep

  Step 6 — After collecting output_json/ from all machines, parse + load to DB:
      python3 run.py --parse --dry-run --limit 50   (test first)
      python3 run.py --parse

────────────────────────────────────────────────────────────────────────────────
"""

# ── Bootstrap: create venv and re-exec inside it ─────────────────────────────

import sys
import os
from pathlib import Path

_SCRIPT_DIR = Path(__file__).parent.resolve()
_VENV_DIR   = _SCRIPT_DIR / ".venv"
_VENV_PY    = _VENV_DIR / "bin" / "python3"
_REQUIREMENTS = [
    "setuptools>=70.0",
    "undetected-chromedriver>=3.5",
    "selenium>=4.18",
    "psycopg2-binary>=2.9",
    "python-dotenv>=1.0",
    "beautifulsoup4>=4.12",
    "lxml>=5.0",
]


def _in_venv() -> bool:
    return (
        hasattr(sys, "real_prefix")
        or (hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix)
    )


def _bootstrap() -> None:
    import subprocess
    if not _VENV_DIR.exists():
        print("[setup] Creating virtual environment...")
        subprocess.run([sys.executable, "-m", "venv", str(_VENV_DIR)], check=True)
    pip = _VENV_DIR / "bin" / "pip"
    print("[setup] Installing / updating dependencies...")
    subprocess.run(
        [str(pip), "install", "--quiet", "--upgrade"] + _REQUIREMENTS,
        check=True,
    )
    print("[setup] Done. Restarting inside venv...\n")
    os.execv(str(_VENV_PY), [str(_VENV_PY)] + sys.argv)


if not _in_venv():
    _bootstrap()

# ── Real imports (only reachable inside venv) ─────────────────────────────────

import argparse
import csv
import json
import logging
import shutil
import time
from datetime import datetime, timezone
from multiprocessing import Pool

import psycopg2
import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from selenium.webdriver.common.by import By

load_dotenv(_SCRIPT_DIR / ".env")

# ── Config ────────────────────────────────────────────────────────────────────

OUTPUT_DIR   = _SCRIPT_DIR / "output_json"
CHUNKS_DIR   = _SCRIPT_DIR / "chunks"
PLAYERS_CSV  = _SCRIPT_DIR / "players.csv"
MACHINE_ID   = os.environ.get("MACHINE_ID", "unknown")
CHROME_VER   = int(os.environ.get("CHROME_VERSION", 146))
PAGE_DELAY   = float(os.environ.get("PAGE_DELAY", 2.0))

OUTPUT_DIR.mkdir(exist_ok=True)
CHUNKS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("scraper")

MASTER_FIELDS = [
    "player_id", "cricsheet_id", "name", "machine",
    "profile_status", "profile_error", "profile_scraped_at",
    "deep_status",    "deep_error",    "deep_scraped_at",
]

MACHINE_FIELDS = [
    "player_id", "cricsheet_id", "name",
    "profile_status", "profile_error", "profile_scraped_at",
    "deep_status",    "deep_error",    "deep_scraped_at",
]

PAGES = {
    "profile": "https://www.espncricinfo.com/cricketers/unknown-{id}",
    "stats":   "https://www.espncricinfo.com/cricketers/unknown-{id}/bowling-batting-stats",
    "records": "https://www.espncricinfo.com/cricketers/unknown-{id}/tests-odi-t20-records",
    "matches": "https://www.espncricinfo.com/cricketers/unknown-{id}/matches",
}

# ── Normalisation maps ────────────────────────────────────────────────────────

_BATTING_MAP = {
    "right-hand bat": "right-hand bat", "right hand bat": "right-hand bat", "rhb": "right-hand bat",
    "left-hand bat":  "left-hand bat",  "left hand bat":  "left-hand bat",  "lhb": "left-hand bat",
}
_ROLE_MAP = {
    "batter": "batter", "batsman": "batter", "top order batter": "batter",
    "middle order batter": "batter", "opening batter": "batter",
    "bowler": "bowler",
    "all-rounder": "all-rounder", "allrounder": "all-rounder",
    "batting all-rounder": "all-rounder", "bowling all-rounder": "all-rounder",
    "wicketkeeper": "wicket-keeper", "wicket-keeper": "wicket-keeper",
    "wicket keeper": "wicket-keeper", "wk-batsman": "wicket-keeper",
    "wk-batter": "wicket-keeper", "wicketkeeper batter": "wicket-keeper",
    "wicketkeeper-batter": "wicket-keeper",
}

# ── CSV helpers ───────────────────────────────────────────────────────────────

def _load_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _save_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _machine_csv(machine: int) -> Path:
    return CHUNKS_DIR / f"machine_{machine}.csv"


def _html_is_valid(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        if path.stat().st_size < 8000:
            return False
        head = path.read_text(encoding="utf-8", errors="ignore")[:500].lower()
        return "access denied" not in head and "<title>error</title>" not in head
    except Exception:
        return False

# ── DB config ─────────────────────────────────────────────────────────────────

def _db_config() -> dict:
    return dict(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", 5432)),
        dbname=os.environ.get("DB_NAME", "krickbiz"),
        user=os.environ.get("DB_USER", "himanshu"),
        password=os.environ.get("DB_PASSWORD", "admin"),
    )

# ── Browser helpers ───────────────────────────────────────────────────────────

def _make_driver() -> uc.Chrome:
    opts = uc.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    return uc.Chrome(options=opts, version_main=CHROME_VER)


def _dismiss_popup(driver: uc.Chrome) -> None:
    try:
        for btn in driver.find_elements(By.XPATH,
            "//*[contains(text(),'Accept') or contains(text(),'agree') or contains(text(),'Got it')]"):
            if btn.is_displayed():
                btn.click()
                time.sleep(0.5)
                break
    except Exception:
        pass


def _scrape_pages(player_id: str, name: str, pages: dict) -> tuple[bool, str]:
    driver = None
    errors = []
    try:
        driver = _make_driver()
        driver.set_page_load_timeout(30)
        popup_done = False
        for page_name, out_path in pages.items():
            try:
                driver.get(PAGES[page_name].format(id=player_id))
                time.sleep(3)
                if not popup_done:
                    _dismiss_popup(driver)
                    popup_done = True
                time.sleep(PAGE_DELAY)
                html = driver.page_source
                if len(html) < 8000:
                    raise ValueError(f"Page too short ({len(html)} chars)")
                if "access denied" in html[:500].lower():
                    raise ValueError("Access denied")
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(html, encoding="utf-8")
                logger.info("  ✓ %-32s %s", name[:30], page_name)
            except Exception as exc:
                errors.append(f"{page_name}:{str(exc)[:100]}")
                logger.warning("  ✗ %-32s %s: %s", name[:30], page_name, exc)
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
    return (len(errors) == 0, "; ".join(errors))

# ── Workers ───────────────────────────────────────────────────────────────────

def worker_profile(row: dict) -> dict:
    player_id = row["player_id"]
    out_path  = OUTPUT_DIR / player_id / "profile.html"
    if _html_is_valid(out_path):
        return {**row, "profile_status": "done"}
    if out_path.exists():
        out_path.unlink()
    ok, err = _scrape_pages(player_id, row["name"], {"profile": out_path})
    now = datetime.now(timezone.utc).isoformat()
    return {**row, "profile_status": "done" if ok else "error",
            "profile_error": "" if ok else err, "profile_scraped_at": now}


def worker_deep(row: dict) -> dict:
    player_id = row["player_id"]
    out_dir   = OUTPUT_DIR / player_id
    needed = {}
    for page in PAGES:
        p = out_dir / f"{page}.html"
        if not _html_is_valid(p):
            if p.exists():
                p.unlink()
            needed[page] = p
    if not needed:
        return {**row, "deep_status": "done"}
    ok, err = _scrape_pages(player_id, row["name"], needed)
    now = datetime.now(timezone.utc).isoformat()
    return {**row, "deep_status": "done" if ok else "error",
            "deep_error": "" if ok else err, "deep_scraped_at": now}

# ── Init ──────────────────────────────────────────────────────────────────────

def cmd_init(total_machines: int) -> None:
    print(f"Connecting to DB...")
    conn = psycopg2.connect(**_db_config())
    cur  = conn.cursor()
    cur.execute("""
        SELECT key_cricinfo, cricsheet_id, name
        FROM people
        WHERE key_cricinfo IS NOT NULL
        ORDER BY cricsheet_id
    """)
    db_rows = cur.fetchall()
    cur.close()
    conn.close()

    total      = len(db_rows)
    chunk_size = (total + total_machines - 1) // total_machines

    print(f"Total players  : {total:,}")
    print(f"Total machines : {total_machines}")
    print(f"Chunk size     : ~{chunk_size:,} per machine\n")

    master_rows = []
    machine_buckets: dict[int, list[dict]] = {m: [] for m in range(1, total_machines + 1)}

    for i, (player_id, cricsheet_id, name) in enumerate(db_rows):
        machine_num = min((i // chunk_size) + 1, total_machines)
        row = {
            "player_id":          str(player_id),
            "cricsheet_id":       cricsheet_id,
            "name":               name or "",
            "machine":            machine_num,
            "profile_status":     "pending",
            "profile_error":      "",
            "profile_scraped_at": "",
            "deep_status":        "pending",
            "deep_error":         "",
            "deep_scraped_at":    "",
        }
        master_rows.append(row)
        machine_buckets[machine_num].append(row)

    # Save master CSV (keep original, never modified after init)
    _save_csv(PLAYERS_CSV, master_rows, MASTER_FIELDS)
    print(f"  players.csv           → {total:,} rows  (master, do not modify)")

    # Save per-machine CSVs
    for m in range(1, total_machines + 1):
        path  = _machine_csv(m)
        count = len(machine_buckets[m])
        _save_csv(path, machine_buckets[m], MACHINE_FIELDS)
        print(f"  chunks/machine_{m}.csv  → {count:,} players")

    print(f"\n─── Commands for each machine ───────────────────────────────")
    for m in range(1, total_machines + 1):
        print(f"  Machine {m}: python3 run.py --machine {m} --mode profile")
    print(f"\n─── Overnight deep scrape ────────────────────────────────────")
    for m in range(1, total_machines + 1):
        print(f"  Machine {m}: python3 run.py --machine {m} --mode deep")
    print(f"\n─── After collecting output_json from all machines ───────────")
    print(f"  python3 run.py --parse --dry-run --limit 50")
    print(f"  python3 run.py --parse")
    print(f"\nCopy to each machine:  run.py  chunks/machine_N.csv  .env")

# ── Scrape ────────────────────────────────────────────────────────────────────

def cmd_scrape(machine: int, mode: str, workers: int) -> None:
    csv_path   = _machine_csv(machine)
    status_col = "profile_status" if mode == "profile" else "deep_status"
    worker_fn  = worker_profile if mode == "profile" else worker_deep

    if not csv_path.exists():
        raise FileNotFoundError(
            f"{csv_path} not found.\n"
            f"Run --init on M2 Mini first, then copy chunks/machine_{machine}.csv to this machine."
        )

    rows    = _load_csv(csv_path)
    pending = [r for r in rows if r[status_col] != "done"]
    done_n  = sum(1 for r in rows if r[status_col] == "done")
    error_n = sum(1 for r in rows if r[status_col] == "error")
    total   = len(rows)

    secs_per = (3 + PAGE_DELAY) * (1 if mode == "profile" else 4)
    eta_min  = len(pending) * secs_per / workers / 60

    print(f"\n{'='*62}")
    print(f"  ESPNcricinfo Scraper  |  machine={machine}  mode={mode}")
    print(f"  Machine ID  : {MACHINE_ID}")
    print(f"{'='*62}")
    print(f"  Total       : {total:,}")
    print(f"  Done        : {done_n:,}")
    print(f"  Errors      : {error_n:,}  (will retry)")
    print(f"  Pending     : {len(pending):,}")
    print(f"  Workers     : {workers}")
    print(f"  ETA         : ~{eta_min:.0f} min")
    print(f"{'='*62}\n")

    if not pending:
        print("All done for this machine!")
        return

    row_index = {r["player_id"]: i for i, r in enumerate(rows)}

    with Pool(processes=workers) as pool:
        for result in pool.imap_unordered(worker_fn, pending, chunksize=1):
            pid = result["player_id"]
            if pid in row_index:
                rows[row_index[pid]] = result
            _save_csv(csv_path, rows, MACHINE_FIELDS)

    rows    = _load_csv(csv_path)
    done_n  = sum(1 for r in rows if r[status_col] == "done")
    error_n = sum(1 for r in rows if r[status_col] == "error")

    print(f"\n{'='*62}")
    print(f"  Done   : {done_n:,} / {total:,}")
    print(f"  Errors : {error_n:,}")
    if error_n:
        print(f"  Re-run same command to retry errors.")
    print(f"{'='*62}\n")

# ── Parse + load to DB ────────────────────────────────────────────────────────

def _parse_html(html: str) -> dict:
    soup  = BeautifulSoup(html, "lxml")
    lines = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]
    out   = {"batting_style": None, "bowling_style": None, "primary_role": None,
             "full_name": None, "image_url": None, "bio": None}

    title = soup.find("title")
    if title:
        t = title.get_text(strip=True)
        out["full_name"] = t.split(" Profile")[0].strip() if "Profile" in t else t

    for img in soup.find_all("img"):
        src = img.get("src", "")
        if "PICTURES" in src and "player" in src.lower():
            out["image_url"] = src
            break

    for i, line in enumerate(lines):
        u = line.upper()
        if u == "BATTING STYLE" and i + 1 < len(lines):
            raw = lines[i + 1]
            out["batting_style"] = _BATTING_MAP.get(raw.lower().strip())
            out["batting_style_raw"] = raw
        elif u == "BOWLING STYLE" and i + 1 < len(lines):
            out["bowling_style"] = lines[i + 1].strip().lower()
        elif u == "PLAYING ROLE" and i + 1 < len(lines):
            raw = lines[i + 1]
            out["primary_role"] = _ROLE_MAP.get(raw.lower().strip())
            out["primary_role_raw"] = raw

    paras = [p.get_text(strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 80]
    if paras:
        out["bio"] = paras[0][:1000]

    return out


def cmd_parse(dry_run: bool, limit: int | None) -> None:
    conn   = None if dry_run else psycopg2.connect(**_db_config())
    id_map = {}
    if conn:
        cur = conn.cursor()
        cur.execute("SELECT key_cricinfo::text, cricsheet_id FROM people WHERE key_cricinfo IS NOT NULL")
        id_map = dict(cur.fetchall())
        cur.close()

    dirs = sorted(OUTPUT_DIR.iterdir())
    if limit:
        dirs = dirs[:limit]

    parsed = skipped = errors = 0
    for d in dirs:
        f = d / "profile.html"
        if not f.exists():
            skipped += 1
            continue
        try:
            fields = _parse_html(f.read_text(encoding="utf-8"))
            if dry_run:
                logger.info("[DRY] %s  bat=%-18s bowl=%-25s role=%s",
                            d.name, fields["batting_style"], fields["bowling_style"], fields["primary_role"])
            else:
                cid = id_map.get(d.name)
                if cid:
                    cur = conn.cursor()
                    cur.execute("""
                        UPDATE people SET
                            batting_style     = COALESCE(%s, batting_style),
                            bowling_style     = COALESCE(%s, bowling_style),
                            primary_role      = COALESCE(%s, primary_role),
                            enrichment_source = 'espncricinfo_html',
                            enriched_at       = NOW(),
                            enrichment_raw    = %s::jsonb,
                            updated_at        = NOW()
                        WHERE cricsheet_id = %s
                    """, [fields["batting_style"], fields["bowling_style"], fields["primary_role"],
                          json.dumps({k: v for k, v in fields.items() if v}), cid])
                    conn.commit()
            parsed += 1
        except Exception as exc:
            logger.error("Error %s: %s", d.name, exc)
            errors += 1

    if conn:
        conn.close()

    print(f"\n{'='*50}")
    print(f"  Parsed  : {parsed:,}")
    print(f"  Skipped : {skipped:,}  (no profile.html)")
    print(f"  Errors  : {errors:,}")
    print(f"{'='*50}\n")

# ── Progress report ───────────────────────────────────────────────────────────

def cmd_status() -> None:
    machine_csvs = sorted(CHUNKS_DIR.glob("machine_*.csv"))
    if not machine_csvs:
        print("No machine CSVs found in chunks/")
        return

    print(f"\n{'='*62}")
    print(f"  {'Machine':<12} {'Total':>7} {'P.Done':>8} {'P.Error':>8} {'D.Done':>8} {'D.Error':>8}")
    print(f"{'─'*62}")
    for csv_path in machine_csvs:
        rows  = _load_csv(csv_path)
        total = len(rows)
        pd    = sum(1 for r in rows if r["profile_status"] == "done")
        pe    = sum(1 for r in rows if r["profile_status"] == "error")
        dd    = sum(1 for r in rows if r["deep_status"] == "done")
        de    = sum(1 for r in rows if r["deep_status"] == "error")
        print(f"  {csv_path.stem:<12} {total:>7,} {pd:>8,} {pe:>8,} {dd:>8,} {de:>8,}")
    print(f"{'='*62}\n")

# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="ESPNcricinfo player scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--init",          action="store_true",
                   help="Create players.csv + machine CSVs from DB (run once on main machine)")
    p.add_argument("--total",         type=int, default=4,
                   help="Total number of machines for --init (default: 4)")
    p.add_argument("--machine",       type=int,
                   help="Machine number to run (1, 2, 3, ...)")
    p.add_argument("--mode",          choices=["profile", "deep"], default="profile",
                   help="profile=main page only | deep=all 4 pages")
    p.add_argument("--workers",       type=int, default=int(os.environ.get("WORKERS", 4)),
                   help="Parallel Chrome workers (default: 4)")
    p.add_argument("--parse",         action="store_true",
                   help="Parse saved HTML files and write to DB (run on main machine after collecting)")
    p.add_argument("--dry-run",       action="store_true",
                   help="With --parse: show output without DB writes")
    p.add_argument("--limit",         type=int,
                   help="With --parse: process only first N players")
    p.add_argument("--status",        action="store_true",
                   help="Show progress summary for all machines")
    args = p.parse_args()

    if args.init:
        cmd_init(total_machines=args.total)
    elif args.parse:
        cmd_parse(dry_run=args.dry_run, limit=args.limit)
    elif args.status:
        cmd_status()
    elif args.machine:
        cmd_scrape(machine=args.machine, mode=args.mode, workers=args.workers)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
