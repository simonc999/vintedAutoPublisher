#!/usr/bin/env python3
import argparse
import json
import re
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from typing import List, Set
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright._impl._errors import TargetClosedError


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


def origin_from_url(u: str) -> str:
    p = urlparse(u)
    return f"{p.scheme}://{p.netloc}"


# -------------------- Playwright robustness layer --------------------

def get_or_create_context(browser):
    if browser.contexts:
        return browser.contexts[0]
    return browser.new_context()


def get_or_create_page(context):
    try:
        page = context.new_page()
        page.bring_to_front()
        return page
    except Exception:
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
        except TargetClosedError:
            page = get_or_create_page(page.context)
            time.sleep(0.2)
        except PlaywrightError as e:
            last_err = e
            msg = str(e)
            # sometimes Vinted/SPA redirects cause net::ERR_ABORTED
            if "net::ERR_ABORTED" in msg or "frame was detached" in msg:
                time.sleep(0.5)
                continue
            raise
    raise RuntimeError(f"safe_goto failed for {url} (last error: {last_err})")


def safe_sleep(page, seconds: float):
    try:
        page.wait_for_timeout(int(seconds * 1000))
    except TargetClosedError:
        # caller will recreate page when needed
        raise


# -------------------- Model --------------------

@dataclass(frozen=True)
class ItemLink:
    item_id: str
    href: str  # absolute


# Accept:
# /items/123
# /items/123-slug
# https://www.vinted.it/items/123-slug
ITEM_ID_RE = re.compile(r"/items/(\d+)(?:-[^/?#]+)?(?:/edit)?(?:[/?#].*)?$")


def extract_item_id(href: str) -> str | None:
    m = ITEM_ID_RE.search(href)
    return m.group(1) if m else None


def collect_item_links_from_member_page(page, member_url: str, max_scrolls: int = 250, wait_ms: int = 800) -> List[ItemLink]:
    base = origin_from_url(member_url)

    page = safe_goto(page, member_url, wait_until="domcontentloaded", retries=3)
    page.wait_for_load_state("networkidle")
    safe_sleep(page, 0.8)

    # try to ensure some items are visible
    try:
        page.wait_for_selector("a[href*='/items/']", timeout=8000)
    except Exception:
        pass

    seen: Set[str] = set()
    out: List[ItemLink] = []

    def snapshot():
        anchors = page.locator("a[href*='/items/']").all()
        for a in anchors:
            try:
                href = (a.get_attribute("href") or "").strip()
            except Exception:
                continue
            if not href:
                continue

            # normalize absolute
            if href.startswith("http"):
                abs_href = href
            else:
                abs_href = urljoin(base, href)

            item_id = extract_item_id(abs_href)
            if not item_id:
                continue
            if item_id in seen:
                continue
            seen.add(item_id)
            out.append(ItemLink(item_id=item_id, href=abs_href))

    snapshot()

    stable = 0
    last = len(out)

    for _ in range(max_scrolls):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(wait_ms)
        snapshot()

        if len(out) == last:
            stable += 1
        else:
            stable = 0
            last = len(out)

        if stable >= 4:
            break

    return out


# -------------------- Delete flow (CORRECT: item page, not /edit) --------------------

def delete_one_item_from_item_page(page, item_url: str, delay_s: float = 1.0) -> None:
    """
    Open item page, click delete, confirm in modal.
    Uses the EXACT selectors:
      - button[data-testid="item-delete-button"]
      - div[data-testid="item-delete-modal"]
      - button[data-testid="item-delete-confirmation-button"]
    With 1s delay between actions.
    """
    page = safe_goto(page, item_url, wait_until="domcontentloaded", retries=3)
    page.wait_for_load_state("networkidle")

    # 1 second after opening the item page
    safe_sleep(page, delay_s)

    delete_btn = page.locator("button[data-testid='item-delete-button']").first
    delete_btn.wait_for(state="visible", timeout=15000)
    delete_btn.scroll_into_view_if_needed()
    delete_btn.click()

    # 1 second after clicking delete
    safe_sleep(page, delay_s)

    modal = page.locator("[data-testid='item-delete-modal']").first
    modal.wait_for(state="visible", timeout=15000)

    confirm_btn = page.locator("button[data-testid='item-delete-confirmation-button']").first
    confirm_btn.wait_for(state="visible", timeout=15000)
    confirm_btn.click()

    # 1 second after confirming delete
    safe_sleep(page, delay_s)

    # optional: wait modal disappears
    try:
        modal.wait_for(state="hidden", timeout=15000)
    except Exception:
        pass


# -------------------- main --------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--closet-url", required=True, help="Member page URL, e.g. https://www.vinted.it/member/135971823")
    ap.add_argument("--profile-dir", default=str((__import__('pathlib').Path.home() / ".vinted_cdp_profile")))
    ap.add_argument("--cdp-port", type=int, default=9222)
    ap.add_argument("--keep-open", action="store_true")

    ap.add_argument("--delay", type=float, default=1.0, help="Seconds delay between actions (you asked 1s).")
    ap.add_argument("--between-items", type=float, default=1.0, help="Seconds after each deletion.")
    ap.add_argument("--max-items", type=int, default=0, help="0 = all items, else delete only first N.")
    ap.add_argument("--dry-run", action="store_true", help="Only print what would be deleted.")
    ap.add_argument("--confirm-delete", action="store_true", help="Actually delete (required).")

    args = ap.parse_args()
    member_url = args.closet_url

    chrome = find_chrome()
    cdp_http = f"http://127.0.0.1:{args.cdp_port}"

    profile_dir = __import__("pathlib").Path(args.profile_dir).expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [
            chrome,
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={args.cdp_port}",
            f"--user-data-dir={str(profile_dir)}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            member_url,
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

            links = collect_item_links_from_member_page(page, member_url)
            items = links

            print(f"DEBUG: collected total={len(items)}")
            if args.max_items and args.max_items > 0:
                items = items[: args.max_items]

            print(f"Found {len(items)} items to delete from member page.")

            if args.dry_run or not args.confirm_delete:
                print("\nDRY RUN (no deletion). Pass --confirm-delete to actually delete.\n")
                for it in items[:60]:
                    print(f" - would delete item_id={it.item_id} -> {it.href}")
                if len(items) > 60:
                    print(f" ... and {len(items)-60} more")
                print("\nDone.")
                return

            base = origin_from_url(member_url)

            for i, it in enumerate(items, start=1):
                # IMPORTANT: open the ITEM PAGE, not /edit
                item_url = urljoin(base, f"/items/{it.item_id}")
                print(f"[{i}/{len(items)}] Deleting item {it.item_id} -> {item_url}")

                try:
                    delete_one_item_from_item_page(page, item_url, delay_s=args.delay)
                    print(f"✅ Deleted {it.item_id}")
                except TargetClosedError:
                    print("❌ TargetClosedError -> recreating tab and continuing.")
                    page = get_or_create_page(context)
                except PlaywrightError as e:
                    print(f"❌ Playwright error on {it.item_id}: {e}")
                except Exception as e:
                    print(f"❌ Error on {it.item_id}: {e}")

                try:
                    safe_sleep(page, args.between_items)
                except TargetClosedError:
                    page = get_or_create_page(context)

            print("\nDone.")

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
