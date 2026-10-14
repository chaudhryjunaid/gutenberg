# Gutenberg library organizer

`gutenberg_organize.py` turns the Project Gutenberg TXT mirror in `txt/` into a
browsable library in `txt-organized/`. It needs only Python 3.10+ and has no
dependencies.

```
txt/1/3/4/1342/1342-0.txt
  -> txt-organized/01000-01999/1342 - Pride and Prejudice - Jane Austen.txt
```

## Layout

```
gutenberg/
├── metadata/pg_catalog.csv          official catalog (titles, authors, languages)
├── txt/                             the mirror, read-only to this tool
├── txt-organized/
│   ├── 00000-00999/                 one file per ebook, 1,000 IDs per folder
│   │   └── 84 - Frankenstein - Mary Wollstonecraft Shelley.txt
│   ├── 01000-01999/
│   └── _variants/                   distinct older revisions and alternates
│       └── 01000-01999/
│           └── 1342 - Pride and Prejudice - Jane Austen [old 2024-12-08].txt
└── manifest.csv                     one row per source file: what happened to it
```

## Usage

```sh
python3 gutenberg_organize.py --dry-run          # default: write manifest.csv only
python3 gutenberg_organize.py --apply            # copy files, then verify hashes
python3 gutenberg_organize.py --apply --mode hardlink   # same, without using extra disk
python3 gutenberg_organize.py --verify           # re-hash everything in the manifest
python3 gutenberg_organize.py --fetch-catalog    # refresh metadata/pg_catalog.csv
```

Add `--current-only` to leave old revisions out of the library. They're still
listed in the manifest with the status `skipped`.

To add newly mirrored ebooks, run `--fetch-catalog` and then `--apply` again.
Files already in the library are recognized by hash (`exists`) and left alone.

## How files are matched

- **ID from the path, not the filename.** The mirror stores ebook N under the
  digits of N except the last, then N itself: `1342` lives in `1/3/4/1342/` and
  `7` in `0/7/`. Anything below that directory (`old/`, `old/old-2025-01-12/`,
  `41471-h/`) is a variant of the same ebook. A few strays sit one level too
  high (`1/2/4/1/12410-0.txt`). For those, the ID is taken from the filename,
  but only if the directories still spell that ID's prefix.
- **Primary file per ebook:** the standard `<ID>-0.txt` (UTF-8) directly in its
  ID directory. Failing that, plain `<ID>.txt` (ASCII), then `<ID>-8.txt`
  (Latin-1). When only old revisions exist, the newest is promoted.
- **Titles** are the catalog's first title line, with Victorian alternate titles
  removed ("Frankenstein; or, the modern prometheus" becomes "Frankenstein").
  Colon subtitles are kept unless the name is too long.
- **Authors:** "Doyle, Arthur Conan, 1859-1930" becomes "Arthur Conan Doyle".
  Illustrators, translators and editors are dropped unless nobody else is
  credited. Two authors are joined with `&`; three or more become "et al".
- **Names** are at most 150 characters *and* 240 UTF-8 bytes (Linux allows 255
  bytes, and non-Latin titles hit that limit before 150 characters). The ID is
  never truncated. Characters that are illegal on FAT/NTFS are replaced, so the
  library can be copied to an e-reader or USB stick.

## Safety

- The mirror is only ever read.
- Nothing is overwritten. Copies are written to a temp file and published with
  `link()`, which fails if the target exists, so this is guaranteed atomically
  rather than by a separate check before writing.
- Every copy is hashed while it is being written and compared with the
  manifest. `--apply` then re-verifies every placed file.
- Bytes, permissions and mtimes are preserved. Files are never re-encoded.

## Manifest statuses

| status      | meaning |
| ----------- | ------- |
| `planned`   | dry run: would be placed at `dest_path` |
| `copied` / `linked` | placed by `--apply` |
| `exists`    | identical file already at `dest_path` |
| `rename` / `renamed` | identical file already in the library under an outdated name (e.g. `79764.txt` placed before the catalog had a title) |
| `duplicate` | byte-identical to another file of the same ebook; not placed |
| `skipped`   | variant left out by `--current-only` |
| `conflict`  | the target or the ID is already taken by *different* content; nothing touched |
| `unparsed`  | no ebook ID could be determined |
| `error`     | I/O failure; see `note` |

## Retiring the mirror

Keep `txt/` if you still `rsync` from Gutenberg, because the mirror layout is
what rsync syncs against. Otherwise, run `--verify` once more (ideally after a
reboot, so hashes come from disk and not the page cache). Then
`txt-organized/` can become the main library.

With `--mode hardlink`, deleting `txt/` frees no space until the library files
are the only links left, and that's expected. Avoid editing books in place in
hardlink mode, because both trees share the same bytes.

## Tests

```sh
python3 -m unittest discover -s tests
```
