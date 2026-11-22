I would use a Python equivalent of `du`, but with a more useful report: traverse the macOS filesystem, calculate the size of every directory and file, and rank the largest items.

The script below is designed for macOS and provides:

- **Largest directories** – ranked by total disk usage, including their contents.
- **Largest individual files** – useful for finding large DMGs, archives, videos, caches, etc.
- **Recursive traversal** – discovers large directories several levels deep.
- **Human-readable sizes** – KB, MB, GB and TB.
- **Permission handling** – skips inaccessible directories rather than terminating.
- **Configurable scan location** – scan your home directory, Applications, or the entire filesystem.
- **No third-party dependencies** – uses only the Python standard library.

## **Python script:&#x20;**`mac_disk_analyser.py`

```
        )    print("\n" + "=" * 85)    print("SUMMARY")    print("=" * 85)    print(f"Directories analysed: {len(directories):,}")    print(f"Files analysed:       {len(files):,}")    print(f"Total file space:     {human_size(sum(f.size for f in files))}")    print(f"Directory count:      {len(directories):,}")def main() -> None:    parser = argparse.ArgumentParser(        description="Find the largest files and directories on macOS."    )    parser.add_argument(        "paths",        nargs="*",        default=[str(Path.home())],        help="Directories to scan (default: home directory).",    )    parser.add_argument(        "--top",        type=int,        default=30,        help="Number of results per category (default: 30).",    )    parser.add_argument(        "--min-size",        type=float,        default=0,        help="Minimum size in MB to display.",    )    parser.add_argument(        "--exclude",        action="append",        default=[],        help="Directory to exclude. Can be specified multiple times.",    )    parser.add_argument(        "--one-file-system",        action="store_true",        help="Do not cross filesystem/device boundaries.",    )    args = parser.parse_args()    exclusions = [        os.path.abspath(os.path.expanduser(p))        for p in args.exclude    ]    minimum = int(args.min_size * 1024 * 1024)    all_directories = []    all_files = []    total_errors = 0    for path in args.paths:        root = os.path.abspath(os.path.expanduser(path))        if not os.path.isdir(root):            print(f"Skipping invalid directory: {root}")            continue
```

## **Running it**

Save the script as `mac_disk_analyser.py`.

**1. Scan your home directory**

```
python3 mac_disk_analyser.py ~
```

**2. Scan Applications**

```
python3 mac_disk_analyser.py /Applications --top 50
```

**3. Scan the entire Macintosh filesystem**

```
sudo python3 mac_disk_analyser.py / --top 50
```

For the most complete results, give Terminal (or your terminal application) Full Disk Access under macOS Privacy & Security settings. Some protected locations may still be inaccessible.

**4. Only show items larger than 500 MB**

```
python3 mac_disk_analyser.py ~ --min-size 500
```

**5. Exclude directories**

```
python3 mac_disk_analyser.py / \
    --exclude /Volumes \
    --exclude /System/Volumes/VM \
    --top 100
```

## **Example output**

```
=====================================================================================
LARGEST DIRECTORIES
=====================================================================================
  1.     85.42 GB  /Users/mark/Library
  2.     42.18 GB  /Users/mark/Library/Developer
  3.     31.52 GB  /Applications
  4.     18.24 GB  /Users/mark/Downloads
  5.     12.64 GB  /Users/mark/Library/Caches

=====================================================================================
LARGEST FILES
=====================================================================================
  1.      8.42 GB  /Users/mark/Downloads/archive.dmg
  2.      5.18 GB  /Users/mark/Library/Developer/Xcode/...
  3.      3.25 GB  /Users/mark/Movies/video.mov
```

*Illustrative output, not actual scan results.*

## **A couple of important details**

1. Directory sizes are inclusive: `/Users/mark/Library` includes its subdirectories. Don't add parent and child directory sizes together.
2. The script uses allocated disk space (`st_blocks`) rather than just logical file size, making it closer to `du`.
3. Symbolic links are not followed, avoiding accidental traversal loops.
4. Scanning `/` on modern APFS Macs can encounter overlapping views of the Data volume and mounted volumes. For an initial investigation, I recommend scanning `/Users`, `/Applications` and `/Library` separately.

**One improvement I'd recommend for a second iteration:** generate a CSV report and an interactive treemap showing disk consumption by directory. That would make it considerably easier to identify which parts of the Macintosh disk are consuming the most space.
