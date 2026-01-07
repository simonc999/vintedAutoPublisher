# Vinted CDP Automation Toolkit (Playwright + Chrome)

A set of Python scripts to help you:

- **Dump** Vinted category *leaf paths* to a CSV (useful for later matching)
- **Scrape** a member closet (items + metadata) into a CSV and **download photos**
- **Bulk-create listings** from a CSV (as **draft** or **publish**)
- **Convert** a draft CSV into a publish CSV
- **Delete** listings from a member page (with safety gates)

> ⚠️ **Disclaimer**: Browser automation may violate Vinted’s Terms of Service and may risk account restrictions. Use responsibly, with conservative delays, and at your own risk.

---

## Contents

- [Requirements](#requirements)
- [Install](#install)
- [How it works (CDP / real Chrome profile)](#how-it-works-cdp--real-chrome-profile)
- [Quick start](#quick-start)
- [Scripts](#scripts)
- [CSV format](#csv-format)
- [Troubleshooting](#troubleshooting)
- [Safety notes](#safety-notes)

---

## Requirements

- Python **3.10+** recommended
- Google Chrome / Chromium available in `PATH` as one of:
  `google-chrome`, `google-chrome-stable`, `chromium`, `chromium-browser`
- Python package:
  - `playwright`

---

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install playwright
python -m playwright install
```

---

## How it works (CDP / real Chrome profile)

All scripts that open a browser do so by launching **Chrome with remote debugging enabled** (CDP) and connecting via Playwright.

Defaults (override with CLI flags):
- CDP port: **9222** (`--cdp-port`)
- Persistent profile dir: `~/.vinted_cdp_profile` (`--profile-dir`)

This lets you log in once and reuse cookies/session across runs.

---

## Quick start

### 1) Dump category leaves to `all_leaves.csv`

The scraper expects an `all_leaves.csv` file **next to** `scrape_member_items.py`.

```bash
python3 dump_catalog_tree.py --out-csv all_leaves.csv
```

Optional: crawl only a subtree root:

```bash
python3 dump_catalog_tree.py --root-path "Women>Clothes" --out-csv all_leaves.csv
```

### 2) Scrape a member closet to CSV + download photos

Replace placeholders:
- `<MEMBER_URL>` → member profile URL (closet page)
- `<OUT_ROOT>` → folder where output will be created (often `.`)

```bash
python3 scrape_member_items.py   --member-url "<MEMBER_URL>"   --out-root "<OUT_ROOT>"
```

Output structure:

```
<member_id>/
  items.csv
  item1/  (photos)
  item2/
  ...
```

> The `<member_id>` folder name is derived from the member URL by the script.

### 3) (Optional) Convert draft CSV → publish CSV

If `items.csv` contains `action=draft` in every row:

```bash
python3 make_publish_csv.py <member_id>/items.csv
# writes <member_id>/items_publish.csv
```

### 4) Bulk-create listings from CSV (draft or publish)

```bash
python3 batch_from_csv_v2.py --csv <member_id>/items_publish.csv
```

Rerun only failed rows (based on `row_index` stored in the error log):

```bash
python3 batch_from_csv_v2.py   --csv <member_id>/items_publish.csv   --failed-only --failed-log errors.csv --error-log errors_retry.csv
```

### 5) (Optional) Delete listings from a member page

Dry run first:

```bash
python3 delete_active_items.py --closet-url "<MEMBER_URL>" --dry-run
```

Actually delete (requires explicit flag):

```bash
python3 delete_active_items.py --closet-url "<MEMBER_URL>" --confirm-delete
```

---

## Scripts

### `dump_catalog_tree.py`

Dumps **leaf category paths** by crawling the category dropdown UI on the “new item” page.

**Key options**
- `--root-path "Women>Clothes"`: start from a subtree (empty means crawl all roots)
- `--out-csv category_leaves.csv`: output file (default: `category_leaves.csv`)
- `--inner-wait`, `--between-nodes`: timing knobs
- `--profile-dir`, `--cdp-port`, `--keep-open`

**Output**
- CSV with header `leaf_path` containing `>`-separated paths (e.g. `Women>Clothes>Sweaters`)

---

### `scrape_member_items.py`

Scrapes a member closet:

- scrolls the member page to collect item URLs (`/items/<id>`)
- visits each item page and extracts fields:
  - title, description, brand, size, condition, colors, material, price, package size
  - breadcrumb category path, then matches to the nearest **leaf** using `all_leaves.csv`
- downloads all item photos into per-item folders
- writes/updates `items.csv` incrementally (keyed by item URL)

**Important**
- Expects `all_leaves.csv` to exist at:
  `./all_leaves.csv` (same directory as the script)

---

### `make_publish_csv.py`

Converts a CSV where every row has `action=draft` into a new CSV where every row has `action=publish`.

**Usage**
```bash
python3 make_publish_csv.py path/to/items.csv
# outputs path/to/items_publish.csv
```

Optional:
```bash
python3 make_publish_csv.py path/to/items.csv --out path/to/custom_name.csv
```

**Validation**
- Requires headers listed in [CSV format](#csv-format)
- Fails if any row is not `draft` (case-insensitive)

---

### `batch_from_csv_v2.py`

Bulk creates listings from CSV on the “new item” page:

- uploads photos from `photos_dir`
- fills title/description
- selects category path (clicks each level in order)
- selects brand (if not found, clicks “use as custom brand” option)
- selects size, condition, up to 2 colors, material, price, package size
- clicks **Save draft** or **Publish** depending on `action`

**Usage**
```bash
python3 batch_from_csv_v2.py --csv <path_to_csv>
```

**Key options**
- `--in-between-time`: delay between form sections
- `--inner-time`: delay between category clicks
- `--in-between-articles`: delay after each listing
- `--error-log`: error log path
- `--failed-only --failed-log <errors.csv>`: process only failed row indices

**CSV conveniences**
- Accepts `category_path` or `category-path`
- Accepts `package_size` or `package-size`

**Photos path resolution**
- `photos_dir` may be absolute or relative; relative paths are resolved against the CSV directory.

---

### `delete_active_items.py`

Deletes listings from a member page.

**Safety**
- Without `--confirm-delete`, it will *not* delete; it prints a DRY RUN.
- Recommended: always run with `--dry-run` first.

**Usage**
```bash
python3 delete_active_items.py --closet-url "<MEMBER_URL>" --dry-run
python3 delete_active_items.py --closet-url "<MEMBER_URL>" --confirm-delete
```

**Key options**
- `--delay`: delay between delete steps
- `--between-items`: delay between items
- `--max-items` (0 = all): limit deletions

---

## CSV format

### Required headers (for `make_publish_csv.py`)

The publish helper requires this exact set of columns:

- `photos_dir`
- `title`
- `description`
- `category_path`
- `brand`
- `size`
- `condition`
- `colors`
- `material`
- `price`
- `package_size`
- `action` (`draft` or `publish`)
- `url`

### Notes

- `colors` should be comma-separated; the uploader selects **up to 2** colors.
- `photos_dir` should contain image files (`.jpg`, `.jpeg`, `.png`, `.webp`, `.gif`).
- If you plan to use `make_publish_csv.py`, ensure every row has `action=draft` first.

---

## Troubleshooting

- **Chrome not found**: install Chrome/Chromium and ensure it’s available in `PATH`.
- **Not logged in**: run a script with `--keep-open`, log in manually in the opened Chrome window, then rerun without `--keep-open`.
- **Scraper can’t match categories**: regenerate `all_leaves.csv` with `dump_catalog_tree.py` and ensure it’s placed next to `scrape_member_items.py`.
- **Some listings fail**: use the generated error log and rerun with `--failed-only`.

---

## Safety notes

- Start with **small batches** (`--max-items`) to verify selectors still match the website UI.
- Use **conservative delays** to reduce the risk of rate-limiting.
- Keep your profile directory private: it stores browser session data.
