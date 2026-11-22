
#!/usr/bin/env python3
"""
mac_disk_analyser.py

Find the largest files and directories on a Macintosh disk.

Examples:
    python3 mac_disk_analyser.py ~
    python3 mac_disk_analyser.py /Applications
    sudo python3 mac_disk_analyser.py /
    python3 mac_disk_analyser.py /Users /Applications --top 50
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class DirectoryInfo:
    path: str
    size: int = 0


@dataclass
class FileInfo:
    path: str
    size: int


def human_size(size: int) -> str:
    """Convert bytes into a human-readable size."""
    value = float(size)

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:,.2f} {unit}"
        value /= 1024

    return f"{value:.2f} TB"


def disk_size(stat: os.stat_result) -> int:
    """
    Return allocated disk space rather than logical file size.

    st_blocks is available on macOS and measures allocated
    512-byte blocks. This is closer to the output of du.
    """
    return getattr(stat, "st_blocks", 0) * 512 or stat.st_size


def is_excluded(path: str, exclusions: list[str]) -> bool:
    """Check whether a path is inside an excluded directory."""
    return any(
        path == excluded or path.startswith(excluded + os.sep)
        for excluded in exclusions
    )


def analyse_directory(
    root: str,
    exclusions: list[str],
    follow_mounts: bool = True,
) -> tuple[list[DirectoryInfo], list[FileInfo], int]:

    directories: list[DirectoryInfo] = []
    files: list[FileInfo] = []

    root_stat = os.stat(root)
    root_device = root_stat.st_dev

    directory_sizes: dict[str, int] = {}
    errors = 0

    # Iterative post-order traversal.
    # This avoids recursion-depth limitations on deep directory trees.
    stack = [(root, False)]

    while stack:
        current, visited = stack.pop()

        if is_excluded(current, exclusions):
            continue

        if not visited:
            try:
                current_stat = os.stat(current)

                if not follow_mounts and current_stat.st_dev != root_device:
                    continue

                stack.append((current, True))

                with os.scandir(current) as entries:
                    children = []

                    for entry in entries:
                        path = entry.path

                        if is_excluded(path, exclusions):
                            continue

                        try:
                            # Do not follow symbolic links.
                            if entry.is_symlink():
                                continue

                            if entry.is_dir(follow_symlinks=False):
                                children.append(path)

                            elif entry.is_file(follow_symlinks=False):
                                stat = entry.stat(
                                    follow_symlinks=False
                                )
                                size = disk_size(stat)

                                files.append(
                                    FileInfo(path=path, size=size)
                                )

                                directory_sizes[current] = (
                                    directory_sizes.get(current, 0)
                                    + size
                                )

                        except (PermissionError, OSError):
                            errors += 1

                    for child in children:
                        stack.append((child, False))

            except (PermissionError, OSError):
                errors += 1

        else:
            # All child directories have now been processed.
            total = directory_sizes.get(current, 0)

            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        if is_excluded(entry.path, exclusions):
                            continue

                        if entry.is_symlink():
                            continue

                        if entry.is_dir(follow_symlinks=False):
                            total += directory_sizes.get(
                                entry.path, 0
                            )

            except (PermissionError, OSError):
                errors += 1

            directory_sizes[current] = total

            directories.append(
                DirectoryInfo(path=current, size=total)
            )

    return directories, files, errors


def print_results(
    directories: list[DirectoryInfo],
    files: list[FileInfo],
    top: int,
    minimum: int,
) -> None:

    print("\n" + "=" * 85)
    print("LARGEST DIRECTORIES")
    print("=" * 85)

    ranked_dirs = sorted(
        (d for d in directories if d.size >= minimum),
        key=lambda d: d.size,
        reverse=True,
    )

    for index, directory in enumerate(ranked_dirs[:top], 1):
        print(
            f"{index:3}. {human_size(directory.size):>12}  "
            f"{directory.path}"
        )

    print("\n" + "=" * 85)
    print("LARGEST FILES")
    print("=" * 85)

    ranked_files = sorted(
        (f for f in files if f.size >= minimum),
        key=lambda f: f.size,
        reverse=True,
    )

    for index, file in enumerate(ranked_files[:top], 1):
        print(
            f"{index:3}. {human_size(file.size):>12}  "
            f"{file.path}"
        )

    print("\n" + "=" * 85)
    print("SUMMARY")
    print("=" * 85)

    print(f"Directories analysed: {len(directories):,}")
    print(f"Files analysed:       {len(files):,}")
    print(f"Total file space:     {human_size(sum(f.size for f in files))}")
    print(f"Directory count:      {len(directories):,}")


def main() -> None:

    parser = argparse.ArgumentParser(
        description="Find the largest files and directories on macOS."
    )

    parser.add_argument(
        "paths",
        nargs="*",
        default=[str(Path.home())],
        help="Directories to scan (default: home directory).",
    )

    parser.add_argument(
        "--top",
        type=int,
        default=30,
        help="Number of results per category (default: 30).",
    )

    parser.add_argument(
        "--min-size",
        type=float,
        default=0,
        help="Minimum size in MB to display.",
    )

    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Directory to exclude. Can be specified multiple times.",
    )

    parser.add_argument(
        "--one-file-system",
        action="store_true",
        help="Do not cross filesystem/device boundaries.",
    )

    args = parser.parse_args()

    exclusions = [
        os.path.abspath(os.path.expanduser(p))
        for p in args.exclude
    ]

    minimum = int(args.min_size * 1024 * 1024)

    all_directories = []
    all_files = []
    total_errors = 0

    for path in args.paths:

        root = os.path.abspath(os.path.expanduser(path))

        if not os.path.isdir(root):
            print(f"Skipping invalid directory: {root}")
            continue

        print(f"\nScanning: {root}", flush=True)

        directories, files, errors = analyse_directory(
            root,
            exclusions,
            follow_mounts=not args.one_file_system,
        )

        all_directories.extend(directories)
        all_files.extend(files)
        total_errors += errors

        print(
            f"Finished: {len(directories):,} directories, "
            f"{len(files):,} files",
            flush=True,
        )

    print_results(
        all_directories,
        all_files,
        args.top,
        minimum,
    )

    print(f"Filesystem access errors: {total_errors:,}")


if __name__ == "__main__":
    main()

