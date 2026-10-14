# Running the organizer

Run these from `~/media/gutenberg`. See [README.md](README.md) for how matching
and naming work.

## Build the library (hardlink mode)

```sh
python3 gutenberg_organize.py --fetch-catalog && python3 gutenberg_organize.py --apply --mode hardlink
```

1. `--fetch-catalog` downloads the latest `metadata/pg_catalog.csv`. Without
   it, books newer than the catalog are named by ID only (`79764.txt`).
2. `--apply --mode hardlink` creates `txt-organized/` and checks every file.
   It takes about a minute and ends with a summary and a line like
   `verified … 0 problem(s)`.

Hardlinks don't copy any data: each library file is a second name for the
same file in `txt/`, so the library uses almost no extra disk space. This only
works because `txt/` and `txt-organized/` are on the same filesystem.

### Without the old revisions

To keep only the current text of each book and leave out the `_variants/` folder:

```sh
python3 gutenberg_organize.py --apply --mode hardlink --current-only
```

## Check that it worked

```sh
python3 gutenberg_organize.py --verify
ls -l txt-organized/01000-01999/ | head    # 2nd column should be 2 = hardlinked
du -sh txt txt-organized                  # shared data is counted only once
```

## Undo

```sh
rm -rf txt-organized
```

The mirror in `txt/` is never modified.

## Updating later

Re-running `--apply` is safe. Books already in place are recognized and
skipped, so you can run it again after each `rsync` or catalog refresh, and
it only adds or renames what changed.

## Cautions

- **Don't edit books in place.** A hardlinked file and its source are the same
  file, so an edit through either name changes both.
- **rsync:** a plain `rsync` replaces changed files with new ones, which breaks
  the link, so your library keeps the old text. `rsync --inplace` writes into
  the shared file instead, changing the library copy too. `--verify` would then
  report a hash mismatch for that book.
