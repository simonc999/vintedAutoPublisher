import argparse
import csv
import difflib
import html
import json
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright
from playwright.sync_api import Error as PlaywrightError


CSV_HEADERS = [
    "photos_dir",
    "title",
    "description",
    "category_path",
    "brand",
    "size",
    "condition",
    "colors",
    "material",
    "price",
    "package_size",
    "action",
    "url",
]

LEAVES_CSV_PATH = (Path(__file__).resolve().parent / "all_leaves.csv")

# -------------------- Chrome CDP helpers --------------------

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


# -------------------- filesystem utils --------------------

def safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def read_existing_csv(csv_path: Path) -> Dict[str, Dict[str, str]]:
    """Returns dict keyed by URL -> row dict."""
    if not csv_path.exists():
        return {}
    out: Dict[str, Dict[str, str]] = {}
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            u = (row.get("url") or "").strip()
            if u:
                out[u] = row
    return out


def item_num_from_photos_dir(photos_dir: str) -> int:
    m = re.search(r"/item(\d+)$", (photos_dir or "").strip())
    return int(m.group(1)) if m else 10**9


def atomic_write_csv(csv_path: Path, rows_by_url: Dict[str, Dict[str, str]]) -> None:
    tmp = csv_path.with_suffix(".tmp")
    rows_sorted = sorted(
        rows_by_url.values(),
        key=lambda r: item_num_from_photos_dir(r.get("photos_dir", "")),
    )
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        w.writeheader()
        for row in rows_sorted:
            w.writerow({k: row.get(k, "") for k in CSV_HEADERS})
    tmp.replace(csv_path)


def count_images_in_dir(p: Path) -> int:
    if not p.exists() or not p.is_dir():
        return 0
    exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    return sum(1 for x in p.iterdir() if x.is_file() and x.suffix.lower() in exts)


# -------------------- parsing utils --------------------

def member_id_from_url(member_url: str) -> str:
    m = re.search(r"/member/(\d+)", member_url)
    if not m:
        raise ValueError(f"Can't extract member id from: {member_url}")
    return m.group(1)


def normalize_price_to_dot(price_text: str) -> str:
    if not price_text:
        return ""
    t = price_text.replace("\xa0", " ").strip()
    t = re.sub(r"[€\s]", "", t)
    if re.search(r"\d+\.\d{3},\d{2}$", t):
        t = t.replace(".", "").replace(",", ".")
    else:
        t = t.replace(",", ".")
    m = re.search(r"(\d+(?:\.\d{1,2})?)", t)
    return m.group(1) if m else ""


# -------------------- leaves CSV + matching --------------------

def load_leaf_paths_or_die() -> List[str]:
    if not LEAVES_CSV_PATH.exists():
        raise SystemExit(
            f"❌ Missing leaves CSV: {LEAVES_CSV_PATH}\n"
            "Run your catalog dumper first to generate it (all_leaves.csv), then re-run this script."
        )

    leaves: List[str] = []
    with LEAVES_CSV_PATH.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)

    if not rows:
        raise SystemExit(f"❌ Leaves CSV is empty: {LEAVES_CSV_PATH}")

    # accept both:
    # - header 'leaf_path'
    # - no header (single column)
    header = [c.strip() for c in rows[0]]
    start_i = 0
    leaf_col = 0
    if header and any(h.lower() == "leaf_path" for h in header):
        leaf_col = header.index("leaf_path") if "leaf_path" in header else header.index("leaf_path".upper())
        start_i = 1

    for r in rows[start_i:]:
        if not r:
            continue
        v = (r[leaf_col] if leaf_col < len(r) else "").strip()
        if v:
            leaves.append(v)

    if not leaves:
        raise SystemExit(f"❌ No leaves found in: {LEAVES_CSV_PATH}")

    return leaves


def _norm(s: str) -> str:
    s = html.unescape(s or "")
    s = s.replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip().casefold()
    # normalize ampersand spacing (helps "&" vs " & ")
    s = s.replace(" & ", " & ").replace("&", "&")
    return s


def _drop_home(segs: List[str]) -> List[str]:
    # In Italian UI, first breadcrumb is often "Casa" meaning "Home", not category.
    if segs and _norm(segs[0]) == "casa" and len(segs) > 1:
        return segs[1:]
    return segs


def build_leaf_index(leaves: List[str]) -> Tuple[Dict[str, List[Tuple[str, str]]], List[Tuple[str, str]]]:
    """
    Returns:
      - index: root_token -> list of (leaf_original, leaf_norm)
      - all_norm: list of (leaf_original, leaf_norm)
    """
    idx: Dict[str, List[Tuple[str, str]]] = {}
    all_norm: List[Tuple[str, str]] = []
    for leaf in leaves:
        parts = [p.strip() for p in leaf.split(">") if p.strip()]
        root = _norm(parts[0]) if parts else ""
        ln = _norm(leaf)
        tup = (leaf, ln)
        all_norm.append(tup)
        idx.setdefault(root, []).append(tup)
    return idx, all_norm


def best_leaf_match(breadcrumb_segs: List[str], leaf_idx: Dict[str, List[Tuple[str, str]]], all_leaves_norm: List[Tuple[str, str]]) -> Tuple[str, float, str]:
    """
    Returns (best_leaf_original, score, raw_breadcrumb_string_used)
    """
    if not breadcrumb_segs:
        return ("", 0.0, "")

    raw_full = ">".join(breadcrumb_segs)
    raw_drop = ">".join(_drop_home(breadcrumb_segs))

    candidates_full = None
    # choose candidates by root token AFTER dropping home if present
    segs_for_root = _drop_home(breadcrumb_segs)
    root_tok = _norm(segs_for_root[0]) if segs_for_root else _norm(breadcrumb_segs[0])

    candidates_full = leaf_idx.get(root_tok)
    candidates = candidates_full if candidates_full else all_leaves_norm

    def score_for(raw: str) -> Tuple[str, float]:
        a = _norm(raw)
        best_leaf = ""
        best_score = -1.0
        # difflib is slower; keep candidates reduced by root when possible
        for leaf_orig, leaf_norm in candidates:
            s = difflib.SequenceMatcher(None, a, leaf_norm).ratio()
            if s > best_score:
                best_score = s
                best_leaf = leaf_orig
        return best_leaf, best_score

    leaf1, sc1 = score_for(raw_full)
    leaf2, sc2 = score_for(raw_drop) if raw_drop != raw_full else (leaf1, sc1)

    if sc2 > sc1:
        return leaf2, sc2, raw_drop
    return leaf1, sc1, raw_full


# -------------------- Playwright robustness layer --------------------

def get_or_create_context(browser):
    if browser.contexts:
        return browser.contexts[0]
    return browser.new_context()


def get_or_create_page(context):
    """
    Always use a fresh tab to avoid TargetClosedError when Chrome had old tabs/windows.
    """
    try:
        page = context.new_page()
        page.bring_to_front()
        return page
    except Exception:
        # last resort: new context (may lose cookies, but member/item pages are public)
        new_ctx = context.browser.new_context()
        page = new_ctx.new_page()
        page.bring_to_front()
        return page


def safe_goto(page, url: str, wait_until: str = "domcontentloaded", retries: int = 3):
    last_err = None
    for _ in range(retries):
        try:
            page.goto(url, wait_until=wait_until)
            return page
        except Exception as e:
            last_err = e
            msg = str(e)
            if "TargetClosedError" in msg or "has been closed" in msg or "Target page" in msg:
                ctx = page.context
                page = get_or_create_page(ctx)
                time.sleep(0.2)
                continue
            raise
    raise RuntimeError(f"safe_goto failed for {url} (last error: {last_err})")


# -------------------- scraping helpers --------------------

def collect_profile_item_urls(page, profile_url: str, max_scrolls: int = 200, wait_ms: int = 800) -> List[str]:
    page = safe_goto(page, profile_url, wait_until="domcontentloaded", retries=3)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)

    seen: List[str] = []
    seen_set = set()

    def snapshot():
        links = page.locator("a.new-item-box__overlay, a[data-testid$='--overlay-link']").all()
        for a in links:
            try:
                href = a.get_attribute("href") or ""
            except Exception:
                continue
            if not href:
                continue
            if not re.search(r"^/items/\d+$", href):
                continue
            full = urljoin(profile_url, href)
            if full not in seen_set:
                seen_set.add(full)
                seen.append(full)

    snapshot()

    stable = 0
    last = len(seen)
    for _ in range(max_scrolls):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(wait_ms)
        snapshot()

        if len(seen) == last:
            stable += 1
        else:
            stable = 0
            last = len(seen)

        if stable >= 4:
            break

    return seen


def click_expand_description_if_present(page) -> None:
    btn = page.locator("div[itemprop='description'] button:has-text('... altro')").first
    try:
        if btn.count() and btn.is_visible():
            btn.click()
            page.wait_for_timeout(200)
    except Exception:
        pass


def extract_description_clean(page) -> str:
    click_expand_description_if_present(page)
    loc = page.locator("div[itemprop='description'] span.web_ui__Text__format").first
    if loc.count() == 0:
        loc = page.locator("div[itemprop='description']").first
    try:
        txt = loc.inner_text().strip()
    except Exception:
        txt = ""
    txt = re.sub(r"\.\.\.\s*altro\s*$", "", txt, flags=re.IGNORECASE).strip()
    return txt


def extract_title(page) -> str:
    h1 = page.locator("h1.web_ui__Text__title, h1[itemprop='name'], h1").first
    try:
        return h1.inner_text().strip()
    except Exception:
        return ""


def extract_photos(page) -> List[str]:
    imgs = page.locator(
        "section.item-photos__container img[data-testid$='--img'], section.item-photos__container img"
    ).all()
    out: List[str] = []
    for im in imgs:
        try:
            src = im.get_attribute("src") or ""
        except Exception:
            src = ""
        if src and src not in out:
            out.append(src)
    return out


def extract_detail_value_by_testid(page, testid: str) -> str:
    loc = page.locator(f"[data-testid='{testid}'] .web_ui__Text__bold").first
    if loc.count() == 0:
        loc = page.locator(f"[data-testid='{testid}']").first
    try:
        t = loc.inner_text().strip()
    except Exception:
        return ""
    return t.splitlines()[0].strip()


def extract_brand(page) -> str:
    loc = page.locator(
        "xpath=//div[contains(@class,'details-list__item')][.//*[normalize-space()='Brand']]//span[@itemprop='name']"
    ).first
    if loc.count():
        try:
            return html.unescape(loc.inner_text().strip())
        except Exception:
            pass

    loc2 = page.locator(
        "css=div.details-list--details span[itemprop='name'], div.details-list--main-info a[href^='/brand/'] span[itemprop='name']"
    ).first
    if loc2.count():
        try:
            return html.unescape(loc2.inner_text().strip())
        except Exception:
            pass

    return ""


def extract_price(page) -> str:
    loc = page.locator("[data-testid='item-price'] p, [data-testid='item-price']").first
    try:
        t = loc.inner_text().strip()
    except Exception:
        t = ""
    return normalize_price_to_dot(t)


def extract_breadcrumb_segs(page) -> List[str]:
    # <ul class="breadcrumbs"> ... <span itemprop="title">...</span>
    spans = page.locator("ul.breadcrumbs span[itemprop='title']").all()
    segs: List[str] = []
    for sp in spans:
        try:
            t = sp.inner_text().strip()
        except Exception:
            t = ""
        t = html.unescape(t).strip()
        if t:
            segs.append(t)
    return segs


def extract_main_fields(page, leaf_idx, all_leaves_norm) -> Dict[str, str]:
    # category raw -> best leaf match (string from all_leaves.csv)
    breadcrumb_segs = extract_breadcrumb_segs(page)
    best_leaf, score, used_raw = best_leaf_match(breadcrumb_segs, leaf_idx, all_leaves_norm)

    return {
        "title": extract_title(page),
        "description": extract_description_clean(page),
        "brand": extract_brand(page),
        "size": extract_detail_value_by_testid(page, "item-attributes-size"),
        "condition": extract_detail_value_by_testid(page, "item-attributes-status"),
        "colors": extract_detail_value_by_testid(page, "item-attributes-color"),
        "price": extract_price(page),
        "category_path_best": best_leaf,
        "category_match_score": f"{score:.4f}",
        "category_raw_used": used_raw,
    }


def download_photos(photo_urls: List[str], item_dir: Path) -> int:
    safe_mkdir(item_dir)
    downloaded = 0
    for i, url in enumerate(photo_urls, start=1):
        ext = ".webp"
        m = re.search(r"\.(jpg|jpeg|png|webp|gif)(?:\?|$)", url, flags=re.IGNORECASE)
        if m:
            ext = "." + m.group(1).lower()
        out = item_dir / f"{i:02d}{ext}"
        if out.exists() and out.stat().st_size > 0:
            continue
        try:
            urllib.request.urlretrieve(url, out)
            downloaded += 1
        except Exception:
            try:
                if out.exists():
                    out.unlink()
            except Exception:
                pass
    return downloaded


# -------------------- main --------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--member-url", required=True)

    ap.add_argument("--default-material", default="Cotone")
    ap.add_argument("--default-package-size", default="Piccola")
    ap.add_argument("--default-action", default="draft")

    ap.add_argument("--delay-between-items", type=float, default=5.0)

    # LIMITER:
    # 0 = all items
    ap.add_argument("--max-items", type=int, default=0, help="0 = scrape all. Otherwise scrape only the oldest N.")

    ap.add_argument("--profile-dir", default=str(Path.home() / ".vinted_cdp_profile"))
    ap.add_argument("--cdp-port", type=int, default=9222)
    ap.add_argument("--keep-open", action="store_true")

    ap.add_argument("--out-root", default=".")
    args = ap.parse_args()

    # --- load leaves (hardcoded path) ---
    leaves = load_leaf_paths_or_die()
    leaf_idx, all_leaves_norm = build_leaf_index(leaves)

    member_id = member_id_from_url(args.member_url)
    out_root = Path(args.out_root).expanduser().resolve()
    member_dir = out_root / member_id
    safe_mkdir(member_dir)

    csv_path = member_dir / "items.csv"
    rows_by_url = read_existing_csv(csv_path)

    chrome = find_chrome()
    profile_dir = Path(args.profile_dir).expanduser().resolve()
    safe_mkdir(profile_dir)
    cdp_http = f"http://127.0.0.1:{args.cdp_port}"

    # Always request a new window
    proc = subprocess.Popen(
        [
            chrome,
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={args.cdp_port}",
            f"--user-data-dir={str(profile_dir)}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            args.member_url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    try:
        ws_url = wait_for_cdp(cdp_http, timeout_s=15.0)

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws_url)
            context = get_or_create_context(browser)
            page = get_or_create_page(context)

            # Collect URLs (Vinted grid typically returns newest -> oldest)
            item_urls = collect_profile_item_urls(page, args.member_url)

            # Process oldest -> newest (so item1 is oldest)
            item_urls = list(reversed(item_urls))

            # LIMITER: keep oldest N
            if args.max_items and args.max_items > 0:
                item_urls = item_urls[: args.max_items]

            print(f"== Will scrape {len(item_urls)} items (oldest -> newest) ==")

            # determine used item numbers from existing CSV
            used_item_numbers = set()
            for r in rows_by_url.values():
                photos_dir = (r.get("photos_dir") or "").strip()
                m = re.search(r"/item(\d+)$", photos_dir)
                if m:
                    used_item_numbers.add(int(m.group(1)))

            def next_item_number() -> int:
                n = 1
                while n in used_item_numbers:
                    n += 1
                used_item_numbers.add(n)
                return n

            total = len(item_urls)
            for idx, item_url in enumerate(item_urls, start=1):
                print(f"\n[{idx}/{total}] {item_url}")

                prev = rows_by_url.get(item_url, {})
                photos_dir_rel = (prev.get("photos_dir") or "").strip()

                if photos_dir_rel:
                    item_dir = out_root / photos_dir_rel
                else:
                    n = next_item_number()
                    photos_dir_rel = f"{member_id}/item{n}"
                    item_dir = member_dir / f"item{n}"

                safe_mkdir(item_dir)

                try:
                    page = safe_goto(page, item_url, wait_until="domcontentloaded", retries=3)
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(300)

                    photo_urls = extract_photos(page)
                    if len(photo_urls) == 0:
                        print("⚠️ Skipping: no photos detected (not a valid item page).")
                        continue

                    fields = extract_main_fields(page, leaf_idx, all_leaves_norm)

                    already = count_images_in_dir(item_dir)
                    if already > 0:
                        downloaded = 0
                        print(f"🟡 Photos already present ({already}) -> skip download.")
                    else:
                        downloaded = download_photos(photo_urls, item_dir)

                    row = dict(prev)  # merge on top of existing row

                    # Only fill category_path if missing (per your “fill missing things” logic)
                    cat_existing = (row.get("category_path") or "").strip()
                    cat_best = (fields.get("category_path_best") or "").strip()
                    if not cat_existing and cat_best:
                        row["category_path"] = cat_best

                    # brand/size/etc: prefer extracted if non-empty, else keep previous
                    def prefer(extracted: str, prev_val: str) -> str:
                        e = (extracted or "").strip()
                        p = (prev_val or "").strip()
                        return e if e else p

                    row.update(
                        {
                            "photos_dir": photos_dir_rel,
                            "title": prefer(fields.get("title", ""), row.get("title", "")),
                            "description": prefer(fields.get("description", ""), row.get("description", "")),
                            "brand": prefer(fields.get("brand", ""), row.get("brand", "")),
                            "size": prefer(fields.get("size", ""), row.get("size", "")),
                            "condition": prefer(fields.get("condition", ""), row.get("condition", "")),
                            "colors": prefer(fields.get("colors", ""), row.get("colors", "")),
                            "price": prefer(fields.get("price", ""), row.get("price", "")),
                            "material": (row.get("material") or "").strip() or args.default_material,
                            "package_size": (row.get("package_size") or "").strip() or args.default_package_size,
                            "action": (row.get("action") or "").strip() or args.default_action,
                            "url": item_url,
                        }
                    )

                    rows_by_url[item_url] = row
                    atomic_write_csv(csv_path, rows_by_url)

                    print(
                        f"✅ Extracted: title={row['title']!r} | brand={row['brand']!r} | "
                        f"cat_score={fields.get('category_match_score')} | "
                        f"photos={len(photo_urls)} (downloaded {downloaded})"
                    )
                    if not (prev.get("category_path") or "").strip():
                        # show debug only when we newly fill (or attempt) category
                        raw_used = fields.get("category_raw_used", "")
                        best_leaf = fields.get("category_path_best", "")
                        print(f"   🧭 Breadcrumb: {raw_used}")
                        print(f"   🧩 Best leaf:   {best_leaf}")

                except PlaywrightError as e:
                    msg = str(e)
                    if "TargetClosedError" in msg or "has been closed" in msg or "Target page" in msg:
                        print(f"❌ TargetClosedError on {item_url} -> recreating tab and continuing.")
                        page = get_or_create_page(context)
                        continue
                    print(f"❌ Playwright error on {item_url}: {e}")

                except Exception as e:
                    print(f"❌ Error on {item_url}: {e}")

                finally:
                    try:
                        page.wait_for_timeout(int(args.delay_between_items * 1000))
                    except Exception:
                        page = get_or_create_page(context)

            atomic_write_csv(csv_path, rows_by_url)
            print(f"\n✅ Saved CSV: {csv_path}")
            print(f"✅ Total rows in CSV: {len(rows_by_url)}")

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
