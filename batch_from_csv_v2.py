#!/usr/bin/env python3
"""
batch_from_csv_v2_1.py

Fixes vs v2:
- Category selection reverted to "open once and click levels" (like your working version),
  but uses a safer dropdown option click (no scroll_into_view_if_needed).
- Dropdown option clicking is now stable: JS scrollIntoView + JS click fallback.
- Brand not found: clicks #custom-select-brand ("Utilizza "<brand>" come brand").
- Failed-only rerun by row_index from error log.

Usage:
  python3 batch_from_csv_v2_1.py --csv 135971823/items_publish.csv

  # rerun only failed:
  python3 batch_from_csv_v2_1.py --csv 135971823/items_publish.csv \
    --failed-only --failed-log errors.csv --error-log errors_v2_1.csv
"""

import argparse
import csv
import json
import re
import shutil
import subprocess
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import List, Tuple, Set, Optional

from playwright.sync_api import sync_playwright
from playwright.sync_api import Error as PlaywrightError


NEW_ITEM_URL = "https://www.vinted.it/items/new"


# ---------- CDP helpers ----------
def wait_for_cdp(cdp_http: str, timeout_s: float = 15.0) -> str:
    t0 = time.time()
    last_err = None
    while time.time() - t0 < timeout_s:
        try:
            with urllib.request.urlopen(cdp_http + "/json/version", timeout=1) as r:
                data = json.loads(r.read().decode("utf-8"))
            ws = data.get("webSocketDebuggerUrl")
            if ws:
                return ws
        except Exception as e:
            last_err = e
        time.sleep(0.1)
    raise RuntimeError(f"CDP not reachable at {cdp_http} (last error: {last_err})")


def find_chrome() -> str:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    raise RuntimeError("Chrome/Chromium not found in PATH.")


# ---------- Page robustness ----------
def get_or_create_context(browser):
    return browser.contexts[0] if browser.contexts else browser.new_context()


def new_fresh_page(context):
    p = context.new_page()
    p.bring_to_front()
    return p


def safe_wait_ms(page, ms: int):
    try:
        page.wait_for_timeout(ms)
    except Exception:
        pass


def safe_goto(page, url: str, wait_until: str = "domcontentloaded", retries: int = 4):
    last_err = None
    backoff = 0.8
    for _ in range(retries):
        try:
            page.goto(url, wait_until=wait_until)
            return page
        except PlaywrightError as e:
            last_err = e
            msg = str(e)

            if "TargetClosedError" in msg or "has been closed" in msg:
                page = new_fresh_page(page.context)
                time.sleep(0.2)
                continue

            if "net::ERR_" in msg or "Navigation failed" in msg:
                time.sleep(backoff)
                backoff *= 1.8
                continue

            raise
    raise RuntimeError(f"safe_goto failed for {url} (last error: {last_err})")


# ---------- small text helpers ----------
def normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def title_case_like_ui(s: str) -> str:
    s = normalize_spaces(s)
    if not s:
        return s

    out = []
    for w in s.split(" "):
        if not w:
            continue
        if len(w) <= 3 and w.isupper():
            out.append(w)
            continue
        if any(ch.isdigit() for ch in w) or any(ch in "&/+-_" for ch in w):
            out.append(w)
            continue
        out.append(w[:1].upper() + w[1:].lower())
    return " ".join(out)


def canonicalize_package_size(s: str) -> str:
    key = normalize_spaces(s).lower()
    mapping = {"piccola": "Piccola", "media": "Media", "grande": "Grande"}
    return mapping.get(key, title_case_like_ui(s))


def canonicalize_material(s: str) -> str:
    key = normalize_spaces(s).lower()
    mapping = {
        "cotone": "Cotone",
        "acrilico": "Acrilico",
        "poliestere": "Poliestere",
        "lana": "Lana",
        "viscosa": "Viscosa",
        "seta": "Seta",
        "lino": "Lino",
        "pelle": "Pelle",
        "nylon": "Nylon",
        "denim": "Denim",
    }
    return mapping.get(key, title_case_like_ui(s))


# ---------- UI helpers ----------
def close_any_open_dropdown(page):
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    try:
        page.locator("body").click(position={"x": 5, "y": 5})
    except Exception:
        pass


def js_scroll_into_view(locator):
    try:
        locator.evaluate("el => el.scrollIntoView({block: 'center', inline: 'center'})")
    except Exception:
        pass


def safe_click(locator, timeout_ms: int = 8000):
    """
    Click with fallbacks that avoid Playwright "element stable" waits.
    """
    locator.wait_for(state="attached", timeout=timeout_ms)
    try:
        # try normal click first (sometimes works fine)
        locator.click(timeout=timeout_ms)
        return
    except Exception:
        pass

    # fallback: JS scroll + JS click
    try:
        js_scroll_into_view(locator)
        locator.evaluate("el => el.click()")
        return
    except Exception:
        pass

    # last resort: force click (still can try to scroll, but sometimes works)
    locator.click(timeout=timeout_ms, force=True)


def wait_dropdown_visible(dropdown, timeout_ms: int = 20000):
    dropdown.wait_for(state="visible", timeout=timeout_ms)


def find_option_button(dropdown, text: str):
    """
    Returns locator for the option button (role=button ancestor) for a given visible title.
    Matching is:
      - exact (ignorecase, normalized spaces)
      - else contains (ignorecase)
    """
    wanted = normalize_spaces(text)
    rx_exact = re.compile(rf"^{re.escape(wanted)}$", re.IGNORECASE)

    title = dropdown.locator(".web_ui__Cell__title", has_text=rx_exact).first
    if title.count() == 0:
        rx_contains = re.compile(re.escape(wanted), re.IGNORECASE)
        title = dropdown.locator(".web_ui__Cell__title", has_text=rx_contains).first

    title.wait_for(state="visible", timeout=30000)
    btn = title.locator("xpath=ancestor::*[@role='button'][1]").first
    return btn


def click_cell_title(dropdown, text: str):
    btn = find_option_button(dropdown, text)
    safe_click(btn, timeout_ms=12000)


def open_dropdown_for_input(input_loc, content_testid: Optional[str]):
    input_loc.wait_for(state="visible", timeout=30000)
    try:
        js_scroll_into_view(input_loc)
    except Exception:
        pass

    # open dropdown
    try:
        input_loc.click(timeout=8000)
    except Exception:
        safe_click(input_loc, timeout_ms=8000)

    if content_testid:
        dd = input_loc.page.locator(f"[data-testid='{content_testid}']").first
        dd.wait_for(state="visible", timeout=20000)
        return dd

    container = input_loc.locator("xpath=ancestor::div[contains(@class,'c-input__content')][1]")
    dd = container.locator(".input-dropdown").first
    dd.wait_for(state="visible", timeout=20000)
    return dd


# ---------- Selectors / actions ----------
def resolve_photos_folder(folder: str, csv_dir: Path) -> Path:
    raw = folder.strip()
    if not raw:
        return Path("")

    p = Path(raw).expanduser()

    if p.is_absolute():
        return p.resolve()

    cand1 = (csv_dir / p).resolve()
    if cand1.exists():
        return cand1

    cand2 = (csv_dir / p.name).resolve()
    if cand2.exists():
        return cand2

    parts = p.parts
    if len(parts) >= 2 and parts[0] == csv_dir.name:
        cand3 = (csv_dir / Path(*parts[1:])).resolve()
        if cand3.exists():
            return cand3

    return cand1


def upload_photos_from_folder(page, folder: str, csv_dir: Path):
    if not folder:
        return 0

    p = resolve_photos_folder(folder, csv_dir)
    if not p.exists() or not p.is_dir():
        raise RuntimeError(f"photos_dir is not a folder: {p}")

    files = []
    for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp", "*.gif"):
        files.extend(sorted(p.glob(ext)))
    files = [str(x) for x in files if x.is_file()]

    if not files:
        raise RuntimeError(f"No images found in: {p}")

    inp = page.locator("input[type='file'][data-testid='add-photos-input'], input[name='photos'][type='file']").first
    inp.wait_for(state="attached", timeout=30000)
    inp.set_input_files(files)
    safe_wait_ms(page, 2000)
    return len(files)


def select_category_path(page, path: List[str], delay_between_s: float):
    """
    Back to the WORKING behavior: open dropdown ONCE and click through.
    The only change: safer clicking (no scroll_into_view_if_needed).
    """
    cat_input = page.locator(
        "input#category, input[data-testid='catalog-select-dropdown-input'], input[name='category']"
    ).first

    dd = open_dropdown_for_input(cat_input, "catalog-select-dropdown-content")
    wait_dropdown_visible(dd, 20000)

    for i, label in enumerate(path):
        # If the option list is virtualized and doesn't show label,
        # typing can help. We'll try click first; if fails, type then retry once.
        try:
            click_cell_title(dd, label)
        except Exception:
            try:
                cat_input.fill("")
                cat_input.type(label, delay=20)
                safe_wait_ms(page, 200)
            except Exception:
                pass
            click_cell_title(dd, label)

        if i != len(path) - 1:
            safe_wait_ms(page, int(delay_between_s * 1000))

    # best-effort verify
    try:
        el = cat_input.element_handle()
        if el:
            page.wait_for_function(
                "(el,v)=> (el.value||'').toLowerCase().includes(v.toLowerCase())",
                el,
                path[-1],
                timeout=15000,
            )
    except Exception:
        pass


def select_brand(page, brand: str):
    brand = normalize_spaces(brand)

    brand_input = page.locator(
        "input#brand, input[data-testid='brand-select-dropdown-input'], input[name='brand']"
    ).first

    dd = open_dropdown_for_input(brand_input, "brand-select-dropdown-content")
    wait_dropdown_visible(dd, 20000)

    brand_input.fill("")
    brand_input.type(brand, delay=20)
    safe_wait_ms(page, 200)

    # Try exact/contains option
    try:
        click_cell_title(dd, brand)
        return
    except Exception:
        pass

    # If brand not found, click the custom brand option:
    custom = dd.locator("#custom-select-brand, [id='custom-select-brand']").first
    if custom.count() > 0:
        safe_click(custom, timeout_ms=12000)
        return

    raise RuntimeError(f"Brand not selectable: '{brand}'")


def select_single_dropdown_value(page, input_selector: str, content_testid: str, value_text: str, type_to_filter: bool = True):
    inp = page.locator(input_selector).first
    dd = open_dropdown_for_input(inp, content_testid)
    wait_dropdown_visible(dd, 20000)

    if type_to_filter:
        try:
            inp.fill("")
            inp.type(value_text, delay=20)
            safe_wait_ms(page, 200)
        except Exception:
            pass

    click_cell_title(dd, value_text)

    try:
        el = inp.element_handle()
        if el:
            page.wait_for_function(
                "(el,v)=> (el.value||'').toLowerCase().includes(v.toLowerCase())",
                el,
                value_text,
                timeout=15000,
            )
    except Exception:
        pass


def select_colors(page, colors: List[str]):
    colors = [c.strip() for c in colors if c.strip()][:2]
    if not colors:
        return

    color_input = page.locator(
        "input#color, input[data-testid='color-select-dropdown-input'], input[name='color']"
    ).first
    dd = open_dropdown_for_input(color_input, "color-select-dropdown-content")
    wait_dropdown_visible(dd, 20000)

    for c in colors:
        # typing helps virtualized lists (e.g. "Blu Marino")
        try:
            color_input.fill("")
            color_input.type(c, delay=20)
            safe_wait_ms(page, 150)
        except Exception:
            pass
        click_cell_title(dd, c)

    close_any_open_dropdown(page)


def select_material(page, material: str):
    material = canonicalize_material(material)

    mat_input = page.locator(
        "input#material, input[data-testid='category-material-multi-list-input'], input[name='material']"
    ).first
    dd = open_dropdown_for_input(mat_input, "category-material-multi-list-content")
    wait_dropdown_visible(dd, 20000)

    try:
        mat_input.fill("")
        mat_input.type(material, delay=20)
        safe_wait_ms(page, 150)
    except Exception:
        pass

    click_cell_title(dd, material)
    close_any_open_dropdown(page)


def set_price(page, price_value: str):
    price = page.locator("input#price, input[data-testid='price-input--input'], input[name='price']").first
    price.wait_for(state="visible", timeout=30000)
    js_scroll_into_view(price)
    price.fill("")
    price.type(str(price_value), delay=20)
    try:
        price.press("Tab")
    except Exception:
        pass


def select_package_size(page, size_label: str):
    size_label = canonicalize_package_size(size_label)

    container = page.locator("#package_size").first
    try:
        container.wait_for(state="visible", timeout=8000)
    except Exception:
        return

    key = (size_label or "").strip().lower()
    mapping = {"piccola": 1, "media": 2, "grande": 3}

    if key in mapping:
        n = mapping[key]
        radio = container.locator(f"input[data-testid='package_type_selector_{n}--input']").first
        radio.wait_for(state="attached", timeout=30000)

        try:
            if radio.is_checked():
                return
        except Exception:
            pass

        label = container.locator(f"label[data-testid='package_type_selector_{n}']").first
        safe_click(label, timeout_ms=12000)
        return

    wanted = (size_label or "").strip()
    xpath = (
        f"//div[@id='package_size']"
        f"//div[contains(@class,'web_ui__Cell__cell')]"
        f"[.//span[contains(@class,'web_ui__Text__title') and normalize-space()='{wanted}']]"
        f"//label[contains(@class,'web_ui__Radio__radio')]"
    )
    label = page.locator(f"xpath={xpath}").first
    safe_click(label, timeout_ms=12000)


def click_submit(page, action: str):
    action = (action or "draft").lower().strip()
    if action == "publish":
        btn = page.locator("[data-testid='upload-form-save-button']").first
    else:
        btn = page.locator("[data-testid='upload-form-save-draft-button']").first
    btn.wait_for(state="visible", timeout=30000)
    safe_click(btn, timeout_ms=12000)


# ---------- core per-article ----------
def fill_one_article(page, row: dict, csv_dir: Path, in_between_time: float, inner_time: float):
    page = safe_goto(page, NEW_ITEM_URL, wait_until="domcontentloaded", retries=4)
    page.bring_to_front()

    photos_dir = (row.get("photos_dir") or "").strip()
    if photos_dir:
        n = upload_photos_from_folder(page, photos_dir, csv_dir=csv_dir)
        print(f"✅ Foto caricate: {n} ({photos_dir})")
        safe_wait_ms(page, int(in_between_time * 1000))

    title_text = (row.get("title") or "").strip()
    title = page.locator("input#title, input[data-testid='title--input'], input[name='title']").first
    title.wait_for(state="visible", timeout=30000)
    title.fill(title_text)
    print(f"✅ Titolo: {title_text}")
    safe_wait_ms(page, int(in_between_time * 1000))

    desc_text = (row.get("description") or "").strip()
    desc = page.locator(
        "textarea#description, textarea[data-testid='description--input'], textarea[name='description']"
    ).first
    desc.wait_for(state="visible", timeout=30000)
    desc.fill(desc_text)
    print("✅ Descrizione: (ok)")
    safe_wait_ms(page, int(in_between_time * 1000))

    cat_path_raw = (row.get("category_path") or row.get("category-path") or "").strip()
    cat_path = [x.strip() for x in cat_path_raw.split(">") if x.strip()]
    if cat_path:
        select_category_path(page, cat_path, delay_between_s=inner_time)
        print(f"✅ Categoria: {' > '.join(cat_path)}")
        safe_wait_ms(page, int(in_between_time * 1000))

    brand = (row.get("brand") or "").strip()
    if brand:
        select_brand(page, brand)
        print(f"✅ Brand: {brand}")
        safe_wait_ms(page, int(in_between_time * 1000))

    size = (row.get("size") or "").strip()
    if size:
        select_single_dropdown_value(
            page,
            "input#size, input[data-testid='size-select-dropdown-input'], input[name='size']",
            "size-select-dropdown-content",
            size,
            type_to_filter=True,
        )
        print(f"✅ Taglia: {size}")
        safe_wait_ms(page, int(in_between_time * 1000))

    condition = (row.get("condition") or "").strip()
    if condition:
        select_single_dropdown_value(
            page,
            "input#condition, input[data-testid='condition-select-dropdown-input'], input[name='condition']",
            "condition-select-dropdown-content",
            condition,
            type_to_filter=True,
        )
        print(f"✅ Condizioni: {condition}")
        safe_wait_ms(page, int(in_between_time * 1000))

    colors_raw = (row.get("colors") or "").strip()
    if colors_raw:
        select_colors(page, [c.strip() for c in colors_raw.split(",")])
        print(f"✅ Colori: {colors_raw}")
        safe_wait_ms(page, int(in_between_time * 1000))

    material_raw = (row.get("material") or "").strip()
    if material_raw:
        select_material(page, material_raw)
        print(f"✅ Materiale: {canonicalize_material(material_raw)}")
        safe_wait_ms(page, int(in_between_time * 1000))

    price = (row.get("price") or "").strip()
    if price:
        set_price(page, price)
        print(f"✅ Prezzo: {price}")
        safe_wait_ms(page, int(in_between_time * 1000))

    package_size_raw = (row.get("package_size") or row.get("package-size") or "").strip()
    if package_size_raw:
        select_package_size(page, package_size_raw)
        print(f"✅ Pacco: {canonicalize_package_size(package_size_raw)}")
        safe_wait_ms(page, int(in_between_time * 1000))

    action = (row.get("action") or "draft").strip().lower()
    click_submit(page, action)
    print(f"✅ Azione: {action}")

    return page


# ---------- error logging ----------
def append_error(error_csv: Path, row_idx: int, row: dict, exc: Exception):
    error_csv.parent.mkdir(parents=True, exist_ok=True)
    is_new = not error_csv.exists()
    with error_csv.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["timestamp", "row_index", "title", "brand", "category_path", "error", "traceback"])
        w.writerow(
            [
                datetime.now().isoformat(timespec="seconds"),
                row_idx,
                row.get("title", ""),
                row.get("brand", ""),
                row.get("category_path", row.get("category-path", "")),
                repr(exc),
                traceback.format_exc().replace("\n", "\\n"),
            ]
        )


# ---------- failed-only selection ----------
def load_failed_row_indices(error_log: Path) -> List[int]:
    if not error_log.exists():
        raise RuntimeError(f"Failed log not found: {error_log}")

    out: Set[int] = set()
    with error_log.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return []
        if "row_index" not in reader.fieldnames:
            raise RuntimeError("Error log CSV must contain 'row_index' column.")
        for r in reader:
            raw = (r.get("row_index") or "").strip()
            if not raw:
                continue
            try:
                out.add(int(raw))
            except ValueError:
                continue
    return sorted(out)


def load_rows(csv_path: Path) -> List[dict]:
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)


def pick_failed_rows(all_rows: List[dict], failed_indices: List[int]) -> List[Tuple[int, dict]]:
    picked: List[Tuple[int, dict]] = []
    max_i = len(all_rows)
    for idx in failed_indices:
        if 1 <= idx <= max_i:
            picked.append((idx, all_rows[idx - 1]))
        else:
            print(f"WARNING: failed row_index {idx} out of range (CSV has {max_i} rows). Skipping.")
    return picked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="CSV file with article rows")
    ap.add_argument("--profile-dir", default=str(Path.home() / ".vinted_cdp_profile"))
    ap.add_argument("--cdp-port", type=int, default=9222)
    ap.add_argument("--keep-open", action="store_true")

    ap.add_argument("--in-between-time", type=float, default=5.0)
    ap.add_argument("--inner-time", type=float, default=5.0)
    ap.add_argument("--in-between-articles", type=float, default=5.0)

    ap.add_argument("--max-items", type=int, default=0, help="0 = all selected rows")

    ap.add_argument("--error-log", default="errors_v2_1.csv")

    ap.add_argument("--failed-only", action="store_true")
    ap.add_argument("--failed-log", default="", help="Previous error log CSV (must contain row_index)")

    args = ap.parse_args()

    csv_path = Path(args.csv).expanduser().resolve()
    if not csv_path.exists():
        raise RuntimeError(f"CSV not found: {csv_path}")
    csv_dir = csv_path.parent

    error_log = Path(args.error_log).expanduser().resolve()

    rows_all = load_rows(csv_path)

    if args.failed_only:
        if not args.failed_log:
            raise RuntimeError("--failed-only requires --failed-log <path_to_errors.csv>")
        failed_log_path = Path(args.failed_log).expanduser().resolve()
        failed_indices = load_failed_row_indices(failed_log_path)
        rows_to_process = pick_failed_rows(rows_all, failed_indices)
        print(f"== FAILED-ONLY MODE: {len(rows_to_process)} row(s) selected from {failed_log_path} ==")
    else:
        rows_to_process = [(i, r) for i, r in enumerate(rows_all, start=1)]
        print(f"== FULL MODE: {len(rows_to_process)} row(s) from: {csv_path} ==")

    if args.max_items and args.max_items > 0:
        rows_to_process = rows_to_process[: args.max_items]

    if not rows_to_process:
        print("Nothing to do.")
        return

    chrome = find_chrome()
    profile_dir = Path(args.profile_dir).expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    cdp_http = f"http://127.0.0.1:{args.cdp_port}"

    proc = subprocess.Popen(
        [
            chrome,
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={args.cdp_port}",
            f"--user-data-dir={str(profile_dir)}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    ok = 0
    fail = 0

    try:
        ws_url = wait_for_cdp(cdp_http, timeout_s=15.0)

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws_url)
            context = get_or_create_context(browser)
            page = new_fresh_page(context)

            total = len(rows_to_process)

            for run_i, (orig_idx, row) in enumerate(rows_to_process, start=1):
                print(f"\n========== ARTICLE {run_i}/{total} (orig row_index={orig_idx}) ==========")
                try:
                    page = fill_one_article(page, row, csv_dir, args.in_between_time, args.inner_time)
                    ok += 1
                    print(f"✅ DONE orig row_index={orig_idx}")
                except PlaywrightError as e:
                    fail += 1
                    print(f"❌ FAILED orig row_index={orig_idx}: {e}")
                    append_error(error_log, orig_idx, row, e)

                    if "TargetClosedError" in str(e) or "has been closed" in str(e):
                        page = new_fresh_page(context)

                    try:
                        close_any_open_dropdown(page)
                    except Exception:
                        pass
                    try:
                        safe_goto(page, "about:blank", wait_until="domcontentloaded", retries=2)
                    except Exception:
                        pass

                except Exception as e:
                    fail += 1
                    print(f"❌ FAILED orig row_index={orig_idx}: {e}")
                    append_error(error_log, orig_idx, row, e)
                    try:
                        close_any_open_dropdown(page)
                    except Exception:
                        pass
                    try:
                        safe_goto(page, "about:blank", wait_until="domcontentloaded", retries=2)
                    except Exception:
                        pass

                safe_wait_ms(page, int(args.in_between_articles * 1000))

            print(f"\n=== SUMMARY ===\nOK: {ok}\nFAIL: {fail}\nError log: {error_log}")

    finally:
        if args.keep_open:
            print("Chrome kept open (--keep-open).")
        else:
            try:
                proc.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    main()
