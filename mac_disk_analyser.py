#!/usr/bin/env python3
"""
mac_disk_analyser.py

Analyse macOS disk usage and generate:
  - an interactive Plotly treemap (HTML)
  - CSV reports for files and directories
  - a ranked terminal summary

Examples:
    python3 mac_disk_analyser.py ~
    python3 mac_disk_analyser.py /Users /Applications
    sudo python3 mac_disk_analyser.py / --top 50
    python3 mac_disk_analyser.py ~ --min-size 100 --exclude ~/Library/Caches

Install the optional treemap dependency:
    python3 -m pip install plotly

The scanner does not follow symbolic links. Sizes are based on allocated
blocks where the platform provides st_blocks (as macOS does).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class FileRecord:
    path: str
    size_bytes: int


@dataclass(frozen=True)
class DirectoryRecord:
    path: str
    size_bytes: int


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            return f"{value:,.2f} {unit}"
        value /= 1024
    return f"{value:,.2f} PB"


def allocated_size(stat_result: os.stat_result) -> int:
    """Return allocated bytes, using logical size only if st_blocks is absent."""
    blocks = getattr(stat_result, "st_blocks", None)
    if blocks is None:
        return stat_result.st_size
    return blocks * 512


def is_excluded(path: str, exclusions: tuple[str, ...]) -> bool:
    """Return True if path is an excluded directory or lies beneath one."""
    for excluded in exclusions:
        try:
            if os.path.commonpath((path, excluded)) == excluded:
                return True
        except ValueError:
            # Different volumes/drives.
            continue
    return False


def normalise_roots(paths: Iterable[str]) -> list[str]:
    roots: list[str] = []
    for raw in paths:
        path = os.path.abspath(os.path.expanduser(raw))
        if not os.path.isdir(path):
            print(f"Skipping non-directory or inaccessible root: {path}",
                  file=sys.stderr)
            continue
        if path not in roots:
            roots.append(path)

    # Avoid scanning a child root twice when its parent is already included.
    roots.sort(key=lambda p: (p.count(os.sep), len(p)))
    result: list[str] = []
    for path in roots:
        if not any(
            os.path.commonpath((path, existing)) == existing
            for existing in result
        ):
            result.append(path)
    return result


def scan_root(
    root: str,
    exclusions: tuple[str, ...],
    one_file_system: bool,
) -> tuple[list[FileRecord], dict[str, int], int]:
    """
    Walk one root without following symlinks.

    Returns file records, inclusive directory sizes, and an error count.
    Directory sizes include all descendant files.
    """
    files: list[FileRecord] = []
    directory_sizes: dict[str, int] = defaultdict(int)
    errors = 0

    try:
        root_device = os.stat(root, follow_symlinks=False).st_dev
    except OSError as exc:
        print(f"Cannot stat {root}: {exc}", file=sys.stderr)
        return files, directory_sizes, 1

    # os.walk is top-down; directory totals are accumulated into every
    # ancestor as each file is encountered.
    def on_walk_error(_error: OSError) -> None:
        nonlocal errors
        errors += 1

    for current, dirnames, filenames in os.walk(
        root, topdown=True, followlinks=False, onerror=on_walk_error
    ):
        current = os.path.abspath(current)

        if is_excluded(current, exclusions):
            dirnames[:] = []
            continue

        # Prune symlinks, excluded paths, and (optionally) other filesystems.
        kept_dirs: list[str] = []
        for dirname in dirnames:
            full_path = os.path.join(current, dirname)
            try:
                if os.path.islink(full_path):
                    continue
                if is_excluded(full_path, exclusions):
                    continue
                if one_file_system:
                    st = os.stat(full_path, follow_symlinks=False)
                    if st.st_dev != root_device:
                        continue
                kept_dirs.append(dirname)
            except OSError:
                errors += 1
        dirnames[:] = kept_dirs

        # Ensure even empty directories appear in the directory report.
        directory_sizes.setdefault(current, 0)

        for filename in filenames:
            full_path = os.path.join(current, filename)
            try:
                if os.path.islink(full_path):
                    continue
                st = os.stat(full_path, follow_symlinks=False)
                if not os.path.isfile(full_path):
                    continue
                if one_file_system and st.st_dev != root_device:
                    continue

                size = allocated_size(st)
                files.append(FileRecord(full_path, size))

                # Add each file's size to its containing directory and
                # every ancestor up to the selected scan root.
                parent = current
                while True:
                    directory_sizes[parent] += size
                    if parent == root:
                        break
                    next_parent = os.path.dirname(parent)
                    if next_parent == parent:
                        break
                    parent = next_parent

            except (PermissionError, FileNotFoundError, OSError):
                errors += 1

    return files, directory_sizes, errors


def write_csv(path: str, rows: list[tuple[str, int]], kind: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["type", "path", "size_bytes", "size_human"])
        for item_path, size in rows:
            writer.writerow([kind, item_path, size, human_size(size)])


def make_treemap(
    files: list[FileRecord],
    output_html: str,
    max_depth: int | None = None,
) -> None:
    """Build a hierarchical treemap whose leaves are files."""
    try:
        import plotly.graph_objects as go
    except ImportError as exc:
        raise RuntimeError(
            "Plotly is required for the treemap. Install it with:\n"
            "  python3 -m pip install plotly"
        ) from exc

    # Node IDs are absolute paths. Directory values are inclusive totals;
    # file values are allocated sizes. Plotly branchvalues='total' expects
    # each directory's value to equal the sum of its immediate children.
    node_values: dict[str, int] = defaultdict(int)
    node_labels: dict[str, str] = {}
    node_parents: dict[str, str] = {}
    node_is_file: dict[str, bool] = {}

    for record in files:
        file_path = record.path
        parent = os.path.dirname(file_path)

        node_labels[file_path] = os.path.basename(file_path) or file_path
        node_parents[file_path] = parent
        node_values[file_path] = record.size_bytes
        node_is_file[file_path] = True

        # Build all ancestor directory nodes.
        current = parent
        while current:
            node_labels.setdefault(current, os.path.basename(current) or current)
            node_parents.setdefault(current, os.path.dirname(current))
            node_is_file.setdefault(current, False)
            if current == os.path.dirname(current):
                break
            current = os.path.dirname(current)

        # Add this file's size to each ancestor directory.
        current = parent
        while current:
            node_values[current] += record.size_bytes
            if current == os.path.dirname(current):
                break
            current = os.path.dirname(current)

    # Empty scans still produce a useful HTML page.
    if not node_values:
        fig = go.Figure()
        fig.add_annotation(
            text="No files were found (or all files were inaccessible).",
            showarrow=False,
        )
    else:
        ids = list(node_values)
        labels = [node_labels[i] for i in ids]
        parents = [
            node_parents[i]
            if node_parents[i] in node_values and node_parents[i] != i
            else ""
            for i in ids
        ]
        values = [node_values[i] for i in ids]
        customdata = [
            [human_size(node_values[i]), "File" if node_is_file[i] else "Directory", i]
            for i in ids
        ]

        fig = go.Figure(go.Treemap(
            ids=ids,
            labels=labels,
            parents=parents,
            values=values,
            branchvalues="total",
            maxdepth=max_depth or -1,
            customdata=customdata,
            hovertemplate=(
                "<b>%{label}</b><br>"
                "%{customdata[1]}<br>"
                "Allocated size: %{customdata[0]}<br>"
                "<extra></extra>"
            ),
            root_color="lightgrey",
        ))
        fig.update_layout(
            title="Macintosh Disk Usage — click a directory to drill down",
            margin=dict(t=55, l=10, r=10, b=10),
        )

    fig.write_html(output_html, include_plotlyjs=True, full_html=True)


def print_ranked(title: str, rows: list[tuple[str, int]], top: int) -> None:
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)
    if not rows:
        print("No entries.")
        return
    for index, (path, size) in enumerate(rows[:top], start=1):
        print(f"{index:3}. {human_size(size):>12}  {path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyse macOS disk usage and create an interactive treemap."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Directories to scan (default: current user's home directory).",
    )
    parser.add_argument("--top", type=int, default=40,
                        help="Number of terminal results per section (default: 40).")
    parser.add_argument("--min-size", type=float, default=0,
                        help="Minimum size in MB to include in reports.")
    parser.add_argument("--exclude", action="append", default=[],
                        help="Directory to exclude; may be repeated.")
    parser.add_argument("--one-file-system", action="store_true",
                        help="Do not cross filesystem/device boundaries.")
    parser.add_argument("--prefix", default="mac_disk_report",
                        help="Output filename prefix (default: mac_disk_report).")
    parser.add_argument("--treemap-max-depth", type=int, default=None,
                        help="Initial treemap depth; click to drill further.")
    args = parser.parse_args()

    raw_roots = args.paths or [str(Path.home())]
    roots = normalise_roots(raw_roots)
    if not roots:
        parser.error("No accessible directory roots were supplied.")

    exclusions = tuple(
        os.path.abspath(os.path.expanduser(p)) for p in args.exclude
    )
    minimum_bytes = int(args.min_size * 1024 * 1024)

    all_files: list[FileRecord] = []
    all_directory_sizes: dict[str, int] = defaultdict(int)
    total_errors = 0

    for root in roots:
        print(f"\nScanning {root} ...", flush=True)
        files, directory_sizes, errors = scan_root(
            root, exclusions, args.one_file_system
        )
        all_files.extend(files)
        for path, size in directory_sizes.items():
            all_directory_sizes[path] += size
        total_errors += errors
        print(
            f"  Found {len(files):,} files and "
            f"{len(directory_sizes):,} directories; "
            f"{errors:,} access/stat errors.",
            flush=True,
        )

    file_rows = sorted(
        ((f.path, f.size_bytes) for f in all_files
         if f.size_bytes >= minimum_bytes),
        key=lambda row: row[1],
        reverse=True,
    )
    directory_rows = sorted(
        ((path, size) for path, size in all_directory_sizes.items()
         if size >= minimum_bytes),
        key=lambda row: row[1],
        reverse=True,
    )

    files_csv = f"{args.prefix}_files.csv"
    dirs_csv = f"{args.prefix}_directories.csv"
    treemap_html = f"{args.prefix}_treemap.html"

    write_csv(files_csv, file_rows, "file")
    write_csv(dirs_csv, directory_rows, "directory")

    print_ranked("LARGEST DIRECTORIES (inclusive)", directory_rows, args.top)
    print_ranked("LARGEST FILES", file_rows, args.top)

    print("\nGenerating interactive treemap ...")
    try:
        make_treemap(
            all_files,
            treemap_html,
            max_depth=args.treemap_max_depth,
        )
    except RuntimeError as exc:
        print(f"\nTreemap not generated: {exc}", file=sys.stderr)
        print("CSV reports were still generated.")
        return 2

    print("\n" + "=" * 100)
    print("REPORTS")
    print("=" * 100)
    print(f"Treemap:     {os.path.abspath(treemap_html)}")
    print(f"Files CSV:   {os.path.abspath(files_csv)}")
    print(f"Folders CSV: {os.path.abspath(dirs_csv)}")
    print(f"Files:       {len(all_files):,}")
    print(f"File space:  {human_size(sum(f.size_bytes for f in all_files))}")
    print(f"Access/stat errors: {total_errors:,}")
    print("\nOpen the treemap in your browser, then click directory rectangles to drill down.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
