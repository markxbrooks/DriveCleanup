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


DEFAULT_TREEMAP_MAX_NODES = 30000

# How plotly.js reaches the report, as write_html's include_plotlyjs expects it.
PLOTLYJS_MODES = {"inline": True, "directory": "directory", "cdn": "cdn"}


def build_treemap_nodes(
    files: list[FileRecord],
) -> tuple[dict[str, int], dict[str, str], dict[str, str]]:
    """
    Expand file records into treemap nodes keyed by absolute path.

    Directory values are inclusive totals, so every node's value equals the
    sum of its immediate children -- what Plotly branchvalues='total' expects.
    """
    values: dict[str, int] = defaultdict(int)
    labels: dict[str, str] = {}
    parents: dict[str, str] = {}

    for record in files:
        parent = os.path.dirname(record.path)
        labels[record.path] = os.path.basename(record.path) or record.path
        parents[record.path] = parent
        values[record.path] = record.size_bytes

        # Build every ancestor directory and add this file's size to it.
        current = parent
        while current:
            labels.setdefault(current, os.path.basename(current) or current)
            parents.setdefault(current, os.path.dirname(current))
            values[current] += record.size_bytes
            if current == os.path.dirname(current):
                break
            current = os.path.dirname(current)

    return values, labels, parents


def select_largest_nodes(
    values: dict[str, int],
    max_nodes: int,
) -> set[str]:
    """
    Keep the max_nodes biggest nodes, ancestors included.

    A directory's value is the total of its descendants, so it is never
    smaller than any node below it; taking the largest values therefore keeps
    a node's ancestors too. Equal values are broken by depth so the ancestor
    still sorts first.
    """
    if max_nodes <= 0 or len(values) <= max_nodes:
        return set(values)
    ranked = sorted(values, key=lambda p: (-values[p], p.count(os.sep)))
    return set(ranked[:max_nodes])


def make_treemap(
    files: list[FileRecord],
    output_html: str,
    max_depth: int | None = None,
    max_nodes: int = DEFAULT_TREEMAP_MAX_NODES,
    plotlyjs: bool | str = True,
) -> int:
    """
    Build a hierarchical treemap whose leaves are files, and return the node
    count written.

    Only the largest max_nodes nodes are drawn. Whatever a kept directory
    loses that way is rolled into a single "smaller items" rectangle, so the
    totals still add up while the page stays small enough for a browser to
    open. A full home directory is millions of files, and every one of them
    emitted as its own node produces a multi-hundred-megabyte page that only
    renders as a blank screen.
    """
    try:
        import plotly.graph_objects as go
    except ImportError as exc:
        raise RuntimeError(
            "Plotly is required for the treemap. Install it with:\n"
            "  python3 -m pip install plotly"
        ) from exc

    values, labels, parents = build_treemap_nodes(files)

    # Empty scans still produce a useful HTML page.
    if not values:
        fig = go.Figure()
        fig.add_annotation(
            text="No files were found (or all files were inaccessible).",
            showarrow=False,
        )
        fig.write_html(output_html, include_plotlyjs=plotlyjs, full_html=True)
        return 0

    kept = select_largest_nodes(values, max_nodes)

    # What each kept directory keeps of its own total, and how many of its
    # children were dropped, so the shortfall can be shown as one rectangle.
    kept_child_total: dict[str, int] = defaultdict(int)
    dropped_children: dict[str, int] = defaultdict(int)
    for path in values:
        parent = parents[path]
        if parent not in kept or parent == path:
            continue
        if path in kept:
            kept_child_total[parent] += values[path]
        else:
            dropped_children[parent] += 1

    # Node IDs are array indices rather than paths: paths are long, Plotly's
    # JSON encoder escapes every "/" as \u002f, and each path would otherwise
    # be serialised once as an ID and again in each child's parent entry.
    order = sorted(kept, key=lambda p: (-values[p], p))
    index = {path: str(i) for i, path in enumerate(order)}

    ids: list[str] = []
    node_labels: list[str] = []
    node_parents: list[str] = []
    node_values: list[int] = []
    customdata: list[list[str]] = []

    for path in order:
        parent = parents[path]
        ids.append(index[path])
        node_labels.append(labels[path])
        node_parents.append(index[parent] if parent in kept and parent != path else "")
        node_values.append(values[path])
        customdata.append([human_size(values[path])])

    for parent, dropped in dropped_children.items():
        remainder = values[parent] - kept_child_total[parent]
        # A directory with no kept children is drawn as a leaf showing its own
        # total, so it needs no rollup rectangle.
        if remainder <= 0 or kept_child_total[parent] <= 0:
            continue
        ids.append(f"rollup-{index[parent]}")
        node_labels.append(f"({dropped:,} smaller items)")
        node_parents.append(index[parent])
        node_values.append(remainder)
        customdata.append([human_size(remainder)])

    fig = go.Figure(go.Treemap(
        ids=ids,
        labels=node_labels,
        parents=node_parents,
        values=node_values,
        branchvalues="total",
        maxdepth=-1 if max_depth is None else max_depth,
        customdata=customdata,
        # %{currentPath} rebuilds the full path from the ancestors' labels, so
        # the path does not have to be stored a third time per node.
        hovertemplate=(
            "<b>%{label}</b><br>"
            "%{currentPath}<br>"
            "Allocated size: %{customdata[0]}<br>"
            "<extra></extra>"
        ),
        root_color="lightgrey",
    ))
    fig.update_layout(
        title="Macintosh Disk Usage \u2014 click a directory to drill down",
        margin=dict(t=55, l=10, r=10, b=10),
    )

    fig.write_html(output_html, include_plotlyjs=plotlyjs, full_html=True)
    return len(ids)


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
    parser.add_argument("--treemap-max-nodes", type=int,
                        default=DEFAULT_TREEMAP_MAX_NODES,
                        help=("Largest rectangles to draw; the rest are grouped "
                              f"per directory (default: {DEFAULT_TREEMAP_MAX_NODES:,}). "
                              "Use 0 for every file, which a browser is unlikely "
                              "to be able to open."))
    parser.add_argument("--treemap-plotlyjs", choices=("inline", "directory", "cdn"),
                        default="directory",
                        help=("Where the report gets plotly.js (about 4.8 MB). "
                              "'directory' writes plotly.min.js alongside the HTML, "
                              "keeping the report small and still usable offline; "
                              "'inline' embeds it in a single self-contained file; "
                              "'cdn' downloads it when the page is opened, which "
                              "needs internet access (default: directory)."))
    parser.add_argument("--treemap-cdn", action="store_true",
                        help="Deprecated alias for --treemap-plotlyjs cdn.")
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
        node_count = make_treemap(
            all_files,
            treemap_html,
            max_depth=args.treemap_max_depth,
            max_nodes=args.treemap_max_nodes,
            plotlyjs=PLOTLYJS_MODES[
                "cdn" if args.treemap_cdn else args.treemap_plotlyjs
            ],
        )
    except RuntimeError as exc:
        print(f"\nTreemap not generated: {exc}", file=sys.stderr)
        print("CSV reports were still generated.")
        return 2

    print("\n" + "=" * 100)
    print("REPORTS")
    print("=" * 100)
    print(f"Treemap:     {os.path.abspath(treemap_html)}")
    if not args.treemap_cdn and args.treemap_plotlyjs == "directory":
        sidecar = os.path.join(os.path.dirname(os.path.abspath(treemap_html)),
                               "plotly.min.js")
        print(f"Plotly.js:   {sidecar} (keep beside the treemap)")
    print(f"Files CSV:   {os.path.abspath(files_csv)}")
    print(f"Folders CSV: {os.path.abspath(dirs_csv)}")
    print(f"Files:       {len(all_files):,}")
    print(f"Treemap nodes: {node_count:,}")
    print(f"File space:  {human_size(sum(f.size_bytes for f in all_files))}")
    print(f"Access/stat errors: {total_errors:,}")
    print("\nOpen the treemap in your browser, then click directory rectangles to drill down.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
