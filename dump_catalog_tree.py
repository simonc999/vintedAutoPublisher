import argparse
import csv
import json
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Tuple, Set

from playwright.sync_api import sync_playwright


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


def close_any_open_dropdown(page):
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    try:
        page.locator("body").click(position={"x": 5, "y": 5})
    except Exception:
        pass


def open_category_dropdown(page, timeout_ms: int = 30000):
    """
    Robustly opens the category dropdown, even if the click handler is on the wrapper/icon.
    """
    root = page.locator("label[for='category']").first.locator(
        "xpath=ancestor::div[contains(@class,'c-input')][1]"
    )
    root.wait_for(state="visible", timeout=timeout_ms)
    root.scroll_into_view_if_needed()

    cat_input = root.locator("input#category, input[data-testid='catalog-select-dropdown-input']").first
    wrapper_click = root.locator(".c-input__content").first
    icon = root.locator(
        "[data-testid='catalog-select-dropdown-chevron-down'], [data-testid='catalog-select-dropdown-chevron-up']"
    ).first

    dd = page.locator(
        "[data-testid='catalog-select-dropdown-content'], .input-dropdown:has(#catalog-search-input)"
    ).first

    def is_open() -> bool:
        try:
            return dd.is_visible()
        except Exception:
            return False

    if is_open():
        return dd

    last_err = None
    for _ in range(6):
        try:
            try:
                wrapper_click.click(timeout=2000, force=True)
            except Exception:
                pass
            try:
                dd.wait_for(state="attached", timeout=2500)
            except Exception:
                pass
            if is_open():
                return dd

            try:
                icon.click(timeout=2000, force=True)
            except Exception:
                pass
            if is_open():
                return dd

            try:
                cat_input.click(timeout=2000, force=True)
            except Exception:
                pass
            if is_open():
                return dd

            try:
                cat_input.focus()
                page.keyboard.press("Enter")
            except Exception:
                pass
            if is_open():
                return dd

            try:
                cat_input.focus()
                page.keyboard.press("Space")
            except Exception:
                pass
            if is_open():
                return dd

            close_any_open_dropdown(page)
            page.wait_for_timeout(250)

        except Exception as e:
            last_err = e
            close_any_open_dropdown(page)
            page.wait_for_timeout(250)

    raise RuntimeError(
        "Category dropdown did not open (catalog-select-dropdown-content never became visible). "
        f"Last error: {last_err}"
    )


def click_title_in_dropdown(dd, title_text: str):
    rx = re.compile(rf"^{re.escape(title_text)}$")
    title = dd.locator(".web_ui__Cell__title", has_text=rx).first
    title.wait_for(state="visible", timeout=30000)
    cell = title.locator("xpath=ancestor::div[contains(@class,'web_ui__Cell__cell')][1]")
    cell.scroll_into_view_if_needed()
    cell.click()


def navigate_to_path(page, path: List[str], inner_wait_s: float = 0.5):
    """
    Opens the dropdown and clicks each segment in order.
    If path is empty => just opens dropdown at root level.
    """
    dd = open_category_dropdown(page, timeout_ms=30000)
    for seg in path:
        click_title_in_dropdown(dd, seg)
        page.wait_for_timeout(int(inner_wait_s * 1000))
    return dd


def _get_scroll_container(dd):
    c = dd.locator(".category-scrollable-container").first
    if c.count() == 0:
        c = dd.locator(".u-overflow-auto").first
    c.wait_for(state="visible", timeout=15000)
    return c


def collect_all_level_items(dd, scroll_wait_ms: int = 350, max_scrolls: int = 80) -> Dict[str, Tuple[str, bool]]:
    """
    Returns dict: {catalog_id: (title, is_branch)}

    IMPORTANT: Based on your real HTML:
    <div class="u-overflow-auto category-scrollable-container">
      <ul> <li> <div id="catalog-5" class="web_ui__Cell__cell ... navigating with-chevron ..."> ... </div>

    So we scope to category-scrollable-container and collect div#catalog-*.
    """
    container = _get_scroll_container(dd)

    seen: Dict[str, Tuple[str, bool]] = {}

    def snapshot():
        # scope to container (NOT whole dropdown)
        cells = container.locator("div.web_ui__Cell__cell[id^='catalog-']").all()
        for cell in cells:
            cid = (cell.get_attribute("id") or "").strip()
            if not cid.startswith("catalog-"):
                continue
            if cid.startswith("catalog-suggestion-"):
                continue

            title_loc = cell.locator(".web_ui__Cell__title").first
            try:
                title = title_loc.inner_text().strip()
            except Exception:
                continue
            if not title:
                continue

            cls = (cell.get_attribute("class") or "")
            is_branch = ("web_ui__Cell__navigating" in cls) or ("web_ui__Cell__with-chevron" in cls)

            # if it has a radio, it's a leaf
            try:
                has_radio = cell.locator("input[type='radio'], label.web_ui__Radio__radio").count() > 0
            except Exception:
                has_radio = False
            if has_radio:
                is_branch = False

            seen[cid] = (title, is_branch)

    snapshot()

    last_count = -1
    for _ in range(max_scrolls):
        container.evaluate("el => { el.scrollTop = el.scrollHeight; }")
        dd.page.wait_for_timeout(scroll_wait_ms)
        snapshot()

        if len(seen) == last_count:
            container.evaluate("el => { el.scrollTop = el.scrollTop + 200; }")
            dd.page.wait_for_timeout(scroll_wait_ms)
            snapshot()
            if len(seen) == last_count:
                break

        last_count = len(seen)

    return seen


def dump_leaves(
    page,
    root_path: List[str],
    inner_wait_s: float,
    between_nodes_s: float,
    max_leaves: int,
    max_depth: int,
) -> List[List[str]]:
    """
    DFS over the category tree.
    If root_path == [] => crawls ALL top-level roots (Donna, Uomo, Bambini, Casa, ...).
    """
    leaves: List[List[str]] = []
    visited: Set[Tuple[str, ...]] = set()

    def crawl(current_path: List[str], depth: int):
        if max_leaves and len(leaves) >= max_leaves:
            return
        if max_depth and depth > max_depth:
            return

        key = tuple(current_path)
        if key in visited:
            return
        visited.add(key)

        dd = navigate_to_path(page, current_path, inner_wait_s=inner_wait_s)
        items = collect_all_level_items(dd)

        if not items:
            # if we’re not at root and nothing appears => treat as leaf
            if current_path:
                leaves.append(current_path[:])
                print("LEAF:", " > ".join(current_path))
            close_any_open_dropdown(page)
            return

        children = list(items.values())  # (title, is_branch)
        children.sort(key=lambda x: x[0].lower())

        close_any_open_dropdown(page)

        for title, is_branch in children:
            if max_leaves and len(leaves) >= max_leaves:
                return

            next_path = current_path + [title]

            if is_branch:
                crawl(next_path, depth + 1)
            else:
                leaves.append(next_path)
                print("LEAF:", " > ".join(next_path))

            page.wait_for_timeout(int(between_nodes_s * 1000))

    crawl(root_path, depth=0)
    return leaves


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument("--url", default="https://www.vinted.it/items/new")
    ap.add_argument("--profile-dir", default=str(Path.home() / ".vinted_cdp_profile"))
    ap.add_argument("--cdp-port", type=int, default=9222)
    ap.add_argument("--keep-open", action="store_true")

    # IMPORTANT: default is empty => crawl all roots
    ap.add_argument(
        "--root-path",
        default="",
        help='Optional. Start path, e.g. "Uomo" or "Uomo>Vestiti". If omitted/empty => dump ALL roots.',
    )
    ap.add_argument("--max-leaves", type=int, default=0, help="0 = no limit")
    ap.add_argument("--max-depth", type=int, default=0, help="0 = no limit")

    ap.add_argument("--inner-wait", type=float, default=0.6, help="Wait after each click in path")
    ap.add_argument("--between-nodes", type=float, default=0.05, help="Wait between exploring nodes")

    ap.add_argument("--out-csv", default="category_leaves.csv")

    args = ap.parse_args()

    root_path = [x.strip() for x in args.root_path.split(">") if x.strip()]

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
            args.url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    try:
        ws_url = wait_for_cdp(cdp_http, timeout_s=15.0)

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[-1] if context.pages else context.new_page()

            page.bring_to_front()
            page.goto(args.url, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(800)

            if root_path:
                print(f"== Dumping leaves from: {' > '.join(root_path)} ==")
            else:
                print("== Dumping leaves from: ALL ROOT CATEGORIES ==")

            leaves = dump_leaves(
                page=page,
                root_path=root_path,
                inner_wait_s=args.inner_wait,
                between_nodes_s=args.between_nodes,
                max_leaves=args.max_leaves,
                max_depth=args.max_depth,
            )

            out = Path(args.out_csv).expanduser().resolve()
            with out.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["leaf_path"])
                for path in leaves:
                    w.writerow([">".join(path)])

            print(f"\n✅ Total leaves: {len(leaves)}")
            print(f"✅ Saved to: {out}")

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
