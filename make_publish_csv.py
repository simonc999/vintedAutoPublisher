#!/usr/bin/env python3
"""
make_publish_csv.py

Reads an input CSV that must contain these headers:
photos_dir,title,description,category_path,brand,size,condition,colors,material,price,package_size,action,url

Validates that EVERY row has action == "draft" (case-insensitive, trimmed).
If validation passes, writes a new CSV named "<input_basename>_publish.csv"
with action set to "publish" for every row.
"""

import argparse
import csv
import os
import sys
from typing import List

REQUIRED_HEADERS = [
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


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def sniff_dialect(path: str) -> csv.Dialect:
    # Sniff delimiter/quoting to preserve input style.
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(8192)
        f.seek(0)
        try:
            return csv.Sniffer().sniff(sample)
        except csv.Error:
            # Reasonable default
            return csv.excel


def normalize_action(val: str) -> str:
    return (val or "").strip().lower()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path", help="Path to the input CSV")
    ap.add_argument(
        "--out",
        default=None,
        help="Optional output path. Default: <input_basename>_publish.csv",
    )
    args = ap.parse_args()

    in_path = args.csv_path
    if not os.path.isfile(in_path):
        die(f"Input file not found: {in_path}")

    root, ext = os.path.splitext(in_path)
    if ext.lower() != ".csv":
        # still allow it, but name output nicely
        root = in_path

    out_path = args.out or f"{root}_publish.csv"

    dialect = sniff_dialect(in_path)

    # Read + validate
    with open(in_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, dialect=dialect)
        if reader.fieldnames is None:
            die("CSV has no header row.")

        headers = reader.fieldnames

        missing = [h for h in REQUIRED_HEADERS if h not in headers]
        if missing:
            die(f"Missing required headers: {missing}")

        # Optional: warn about order differences (not fatal)
        if headers[: len(REQUIRED_HEADERS)] != REQUIRED_HEADERS and set(headers) >= set(REQUIRED_HEADERS):
            # Only a warning; we preserve original header order when writing.
            print("WARNING: Header order differs from the expected list (not fatal).", file=sys.stderr)

        bad_rows: List[str] = []
        rows = []

        for idx, row in enumerate(reader, start=2):  # start=2 because header is line 1
            act = normalize_action(row.get("action", ""))
            if act != "draft":
                bad_rows.append(f"line {idx}: action='{row.get('action','')}'")
            rows.append(row)

        if bad_rows:
            die(
                "Not all items have action='draft'. Fix these rows first:\n  "
                + "\n  ".join(bad_rows)
            )

    # Write output with action=publish everywhere
    with open(out_path, "w", encoding="utf-8", newline="") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=headers, dialect=dialect)
        writer.writeheader()
        for row in rows:
            row["action"] = "publish"
            writer.writerow(row)

    print(f"OK: wrote {out_path} with action='publish' for {len(rows)} items.")


if __name__ == "__main__":
    main()
