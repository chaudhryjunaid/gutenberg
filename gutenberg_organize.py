#!/usr/bin/env python3
"""Reorganize a Project Gutenberg TXT mirror into a browsable library.

Turns the mirror's deep numeric hierarchy (``txt/1/3/4/1342/1342-0.txt``)
into one level of 1,000-ID buckets with human-readable names::

    txt-organized/01000-01999/1342 - Pride and Prejudice - Jane Austen.txt

Titles and authors come from the official ``pg_catalog.csv``, not from the
text headers. Files are never moved, overwritten, or re-encoded: the source
tree is only read, and every placed file is byte-identical to its source.

Actions::

    --dry-run        scan + join with catalog, write manifest.csv (default)
    --apply          the same, then copy/hardlink into --dest and verify hashes
    --verify         re-hash every placed file listed in an existing manifest
    --fetch-catalog  download the latest pg_catalog.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import stat
import sys
import tempfile
import unicodedata
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_SOURCE = HERE / "txt"
DEFAULT_DEST = HERE / "txt-organized"
DEFAULT_CATALOG = HERE / "metadata" / "pg_catalog.csv"
DEFAULT_MANIFEST = HERE / "manifest.csv"
CATALOG_URL = "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv"

BUCKET_SIZE = 1000
VARIANTS_DIR = "_variants"
# Non-ebook .txt files found in upstream book directories (lowercased).
# Gutenberg now ships a LICENSE.txt beside each book; a '*.txt' rsync filter
# would otherwise file ~50k copies of it as variants.
IGNORED_NAMES = {"license.txt"}
MAX_NAME_CHARS = 150
# ext4/btrfs/xfs cap a filename at 255 *bytes*, and 150 non-Latin characters
# can exceed that in UTF-8, so both limits apply.
MAX_NAME_BYTES = 240

# Gutenberg's encoding suffixes: -0 = UTF-8, (none) = ASCII, -8 = Latin-1.
# The lower the rank, the more preferred as the primary copy.
ENCODING_RANK = {"-0": 0, "": 1, "-8": 2}
ENCODING_LABEL = {"": "ascii", "-8": "latin-1"}

MANIFEST_FIELDS = [
    "ebook_id", "kind", "status", "source_path", "original_filename",
    "dest_path", "new_filename", "size", "sha256", "in_catalog",
    "title", "author", "language", "note",
]


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Book:
    title: str
    author: str
    language: str


NAME_SUFFIXES = {"jr.", "jr", "sr.", "sr", "ii", "iii", "iv"}
EPITHET_HEADS = {
    "king", "queen", "emperor", "empress", "pope", "prince", "princess",
    "duke", "duchess", "sultan", "tsar", "czar", "shah", "khan",
}


def format_person(raw: str) -> str:
    """'Doyle, Arthur Conan, 1859-1930' -> 'Arthur Conan Doyle'."""
    raw = re.sub(r"\s*\[[^\]]*\]", "", raw)  # [Illustrator]
    raw = re.sub(r"\s*\([^)]*\)", "", raw)  # (Arthur Ignatius Conan)
    # Dates ("1859-1930", "427? BCE-347? BCE", "active 1600") all contain digits.
    parts = [p.strip() for p in raw.split(",")]
    parts = [p for p in parts if p and not re.search(r"\d", p)]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    family, given, *rest = parts
    words = given.split()
    if given[0].islower():
        # 'Leonardo, da Vinci' / 'Pliny, the Elder' read in catalog order;
        # 'Sade, marquis de' / 'Prévost, abbé' are noble titles to drop.
        return f"{family} {given}" if words[-1][0].isupper() else family
    # 'Elizabeth I, Queen of England' -- the second part is an epithet.
    if " of " in f" {given} " or (len(words) > 1 and words[0].lower() in EPITHET_HEADS):
        return family
    # Keep generational suffixes; drop honorifics like Sir, Baron, graf.
    suffixes = [p for p in rest if p.lower() in NAME_SUFFIXES]
    return " ".join([given, family, *suffixes])


def format_authors(raw: str) -> str:
    """Primary authors only: contributors tagged [Role] are dropped
    unless nobody else is credited."""
    entries = [e.strip() for e in raw.split(";") if e.strip()]
    primary = [e for e in entries if "[" not in e] or entries[:1]
    names = list(dict.fromkeys(n for n in map(format_person, primary) if n))
    if len(names) <= 1:
        return names[0] if names else ""
    if len(names) == 2:
        return f"{names[0]} & {names[1]}"
    return f"{names[0]} et al."


def format_title(raw: str) -> str:
    """First line only, without Victorian alternate titles:
    'Frankenstein; or, the modern prometheus' -> 'Frankenstein'."""
    first = raw.strip().split("\n")[0].strip()
    return re.split(r";\s*or,", first, maxsplit=1, flags=re.IGNORECASE)[0].strip()


def load_catalog(path: Path) -> dict[int, Book]:
    books: dict[int, Book] = {}
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                ebook_id = int(row["Text#"])
            except (KeyError, ValueError):
                continue
            books[ebook_id] = Book(
                title=format_title(row.get("Title", "")),
                author=format_authors(row.get("Authors", "")),
                language=row.get("Language", "").strip(),
            )
    return books


# --------------------------------------------------------------------------
# Filenames
# --------------------------------------------------------------------------

# Characters illegal on FAT/NTFS (relevant if the library is copied to an
# e-reader or USB stick), plus '/' which is illegal everywhere.
_UNSAFE = str.maketrans({
    "/": "-", "\\": "-", ":": " -", "|": "-", "*": "", "?": "",
    '"': "'", "<": "", ">": "",
})


def clean_component(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    # Drop control characters, but keep format characters such as the
    # zero-width non-joiner that Persian and Indic titles rely on.
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cs", "Co"))
    text = text.translate(_UNSAFE)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .")


def _fits(name: str) -> bool:
    return len(name) <= MAX_NAME_CHARS and len(name.encode("utf-8")) <= MAX_NAME_BYTES


def _truncate(text: str, max_chars: int, max_bytes: int) -> str:
    if len(text) <= max_chars and len(text.encode("utf-8")) <= max_bytes:
        return text
    text = text[: max(max_chars - 1, 0)]
    while text and len((text + "…").encode("utf-8")) > max_bytes:
        text = text[:-1]
    return text.rstrip(" .,;:-") + "…"


def _budget(name: str) -> tuple[int, int]:
    return len(name), len(name.encode("utf-8"))


def build_filename(ebook_id: int, book: Book | None, label: str = "") -> str:
    """'<ID> - <Title> - <Author>[ [label]].txt', within the length limits.

    The ID always survives; the author is shortened before the title
    (capped at a third of the budget), then the title is shortened, first at
    a subtitle boundary and finally by truncation with an ellipsis."""
    head = str(ebook_id)
    tail = (f" [{clean_component(label)}]" if label else "") + ".txt"
    title = book.title if book else ""  # raw, so ': ' boundaries are still visible
    author = clean_component(book.author) if book else ""

    def assemble(t: str, a: str) -> str:
        return " - ".join(p for p in (head, clean_component(t), a) if p) + tail

    if _fits(assemble(title, author)):
        return assemble(title, author)

    if author:
        chars, nbytes = _budget(head + tail)
        max_chars, max_bytes = (MAX_NAME_CHARS - chars) // 3, (MAX_NAME_BYTES - nbytes) // 3
        a_chars, a_bytes = _budget(author)
        if " & " in author and (a_chars > max_chars or a_bytes > max_bytes):
            author = clean_component(author.split(" & ", 1)[0] + " et al.")
        author = _truncate(author, max_chars, max_bytes)
    for sep in (": ", "; "):
        shorter = title.split(sep, 1)[0].strip()
        if _fits(assemble(title, author)) or not shorter:
            break
        title = shorter
    if not _fits(assemble(title, author)):
        chars, nbytes = _budget(assemble("", author))
        sep_len = 3  # " - "
        title = _truncate(clean_component(title), MAX_NAME_CHARS - chars - sep_len,
                          MAX_NAME_BYTES - nbytes - sep_len)
    return assemble(title, author)


def bucket_name(ebook_id: int) -> str:
    lo = ebook_id // BUCKET_SIZE * BUCKET_SIZE
    return f"{lo:05d}-{lo + BUCKET_SIZE - 1:05d}"


# --------------------------------------------------------------------------
# Scanning the mirror
# --------------------------------------------------------------------------

@dataclass
class SourceFile:
    path: Path
    rel: str  # relative to the source root, for the manifest
    ebook_id: int | None
    sub: tuple[str, ...]  # directories between the ID dir and the file
    size: int
    mtime: float
    sha256: str = ""
    misplaced: bool = False  # sits outside its ID dir; ID came from the filename
    ignored: bool = False  # boilerplate, not an ebook (see IGNORED_NAMES)


def find_id_dir(parts: tuple[str, ...]) -> int | None:
    """Index of the ebook-ID directory in a mirror-relative path.

    The mirror files ID N under the digits of N except the last, then N:
    1342 -> 1/3/4/1342, 61 -> 6/61, and 7 -> 0/7. The first component that
    satisfies this is the ID dir; anything below it ('old/', '41471-h/') is
    a variant directory."""
    for i, comp in enumerate(parts):
        if not comp.isdigit():
            return None
        expected = list(comp[:-1]) if len(comp) > 1 else ["0"]
        if list(parts[:i]) == expected:
            return i
    return None


_STD_NAME = re.compile(r"^(\d+)(-0|-8|)\.txt$", re.IGNORECASE)


def locate(parts: tuple[str, ...]) -> tuple[int | None, tuple[str, ...], bool]:
    """(ebook_id, variant subdirs, misplaced) for a mirror-relative path.

    Falls back to the filename for strays like '1/2/4/1/12410-0.txt' (one
    level too high), but only when the directories still spell the ID's
    mirror prefix, so a stray name can't be filed under the wrong book."""
    idx = find_id_dir(parts[:-1])
    if idx is not None:
        return int(parts[idx]), parts[idx + 1:-1], False
    m = _STD_NAME.match(parts[-1])
    if m:
        digits = m.group(1)
        prefix = list(digits[:-1]) if len(digits) > 1 else ["0"]
        if list(parts[:len(prefix)]) == prefix:
            return int(digits), parts[len(prefix):-1], True
    return None, (), False


def scan(source: Path) -> list[SourceFile]:
    files = []
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames.sort()
        for name in sorted(filenames):
            if not name.lower().endswith(".txt"):
                continue
            path = Path(dirpath, name)
            ebook_id, sub, misplaced = locate(path.relative_to(source).parts)
            st = path.stat()
            files.append(SourceFile(
                path=path,
                rel=str(path.relative_to(source)),
                ebook_id=ebook_id,
                sub=sub,
                size=st.st_size,
                mtime=st.st_mtime,
                misplaced=misplaced,
                ignored=name.lower() in IGNORED_NAMES,
            ))
    return files


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def hash_all(files: list[SourceFile], workers: int) -> None:
    # hashlib releases the GIL on large buffers, so threads parallelize I/O
    # and hashing well enough without multiprocessing.
    with ThreadPoolExecutor(workers) as pool:
        for sf, digest in zip(files, pool.map(lambda s: sha256_file(s.path), files)):
            sf.sha256 = digest


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------

@dataclass
class Entry:
    src: SourceFile
    kind: str = ""  # primary | variant
    status: str = ""  # planned | exists | duplicate | conflict | unparsed | ...
    dest: Path | None = None
    book: Book | None = None
    note: str = ""
    rename_from: Path | None = None  # existing library file to rename to dest

    def row(self, source_root: Path, dest_root: Path) -> dict[str, str]:
        book = self.book
        return {
            "ebook_id": "" if self.src.ebook_id is None else str(self.src.ebook_id),
            "kind": self.kind,
            "status": self.status,
            "source_path": str(source_root / self.src.rel),
            "original_filename": self.src.path.name,
            "dest_path": str(self.dest) if self.dest else "",
            "new_filename": self.dest.name if self.dest else "",
            "size": str(self.src.size),
            "sha256": self.src.sha256,
            "in_catalog": "yes" if book else "no",
            "title": book.title if book else "",
            "author": book.author if book else "",
            "language": book.language if book else "",
            "note": self.note,
        }


def _primary_rank(sf: SourceFile) -> tuple:
    """Lower sorts first. Prefer a file directly in its ID dir with the
    standard '<ID>[-0|-8].txt' name, best encoding first."""
    m = _STD_NAME.match(sf.path.name)
    standard = bool(m and int(m.group(1)) == sf.ebook_id)
    enc = ENCODING_RANK.get(m.group(2), 9) if standard else 9
    nested = 1 if (sf.sub or sf.misplaced) else 0
    return (nested, 0 if standard else 1, enc, -sf.mtime, sf.rel)


def variant_label(sf: SourceFile) -> str:
    """A short, stable tag distinguishing a variant from the primary."""
    m = _STD_NAME.match(sf.path.name)
    standard = bool(m and int(m.group(1)) == sf.ebook_id)
    if sf.sub and sf.sub[0].lower().startswith("old"):
        date = datetime.fromtimestamp(sf.mtime, timezone.utc).strftime("%Y-%m-%d")
        return f"old {date}"
    if sf.misplaced and not sf.sub:
        return "misplaced"
    if not sf.sub and standard:
        return ENCODING_LABEL.get(m.group(2), "alt")
    return " ".join([*sf.sub, sf.path.stem])


_LIBRARY_NAME = re.compile(r"^(\d+)(?: - | \[|\.txt$)")


def _existing_files(dest: Path) -> dict[tuple[bool, int], list[Path]]:
    """Library files already in --dest, keyed by (is_variant, ebook_id), so a
    re-run after a catalog change renames instead of adding a second copy."""
    found: dict[tuple[bool, int], list[Path]] = defaultdict(list)
    for is_variant, base in ((False, dest), (True, dest / VARIANTS_DIR)):
        if not base.is_dir():
            continue
        for bucket in base.iterdir():
            if not bucket.is_dir() or bucket.name == VARIANTS_DIR:
                continue
            for f in bucket.iterdir():
                m = _LIBRARY_NAME.match(f.name)
                if m and f.is_file():
                    found[(is_variant, int(m.group(1)))].append(f)
    return found


def plan(files: list[SourceFile], catalog: dict[int, Book], dest: Path) -> list[Entry]:
    entries: list[Entry] = []
    by_id: dict[int, list[SourceFile]] = defaultdict(list)
    for sf in files:
        if sf.ignored:
            entries.append(Entry(sf, status="ignored",
                                 note="license boilerplate, not an ebook"))
        elif sf.ebook_id is None:
            entries.append(Entry(sf, status="unparsed",
                                 note="could not determine ebook ID from path"))
        else:
            by_id[sf.ebook_id].append(sf)

    present = _existing_files(dest)
    claimed: set[Path] = set()
    renamed: set[Path] = set()
    dest_hash: dict[Path, str] = {}

    def same_content(p: Path, sf: SourceFile) -> bool:
        if p.stat().st_size != sf.size:
            return False
        if p not in dest_hash:
            dest_hash[p] = sha256_file(p)
        return dest_hash[p] == sf.sha256

    for ebook_id in sorted(by_id):
        group = sorted(by_id[ebook_id], key=_primary_rank)
        book = catalog.get(ebook_id)
        seen: dict[str, Path] = {}  # sha256 -> first source with that content
        for i, sf in enumerate(group):
            e = Entry(sf, kind="primary" if i == 0 else "variant", book=book)
            entries.append(e)
            notes = []
            if book is None:
                notes.append("ebook ID not in catalog")
            if i == 0 and (sf.sub or sf.misplaced):
                notes.append("no current file; promoted newest variant")
            if sf.misplaced:
                notes.append("outside its ID directory; ID taken from filename")

            if sf.sha256 in seen:
                e.status = "duplicate"
                notes.append(f"identical to {seen[sf.sha256]}")
                e.note = "; ".join(notes)
                continue
            seen[sf.sha256] = sf.path

            label = "" if i == 0 else variant_label(sf)
            base_dir = dest if i == 0 else dest / VARIANTS_DIR
            target = base_dir / bucket_name(ebook_id) / build_filename(ebook_id, book, label)
            n = 2
            while target in claimed:  # two variants with the same label
                target = target.with_name(
                    build_filename(ebook_id, book, f"{label} {n}".strip()))
                n += 1
            claimed.add(target)
            e.dest = target

            others = [p for p in present.get((i > 0, ebook_id), [])
                      if p != target and p not in renamed]
            if target.exists():
                if same_content(target, sf):
                    e.status = "exists"
                else:
                    e.status = "conflict"
                    notes.append("destination exists with different content")
            elif match := next((p for p in others if same_content(p, sf)), None):
                # Same bytes already in the library under an older name
                # (e.g. '79764.txt' placed before the catalog knew the title).
                e.status, e.rename_from = "rename", match
                renamed.add(match)
                notes.append(f"rename from {match.name}")
            elif i == 0 and others:
                e.status = "conflict"
                notes.append("ID already in library as "
                             + ", ".join(p.name for p in others))
            else:
                e.status = "planned"
            e.note = "; ".join(notes)
    return entries


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------

def _copy_verified(src: Path, dst: Path, expected: str) -> None:
    """Copy src to dst, never overwriting, and confirm the bytes match.

    Data goes to a temp file in the destination directory first and is
    published with os.link(), which fails if dst already exists -- so the
    no-overwrite guarantee is atomic rather than check-then-write."""
    fd, tmp = tempfile.mkstemp(dir=dst.parent, prefix=".", suffix=".part")
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as fout, open(src, "rb") as fin:
            while chunk := fin.read(1 << 20):
                digest.update(chunk)
                fout.write(chunk)
            fout.flush()
            os.fsync(fout.fileno())
        if digest.hexdigest() != expected:
            raise OSError(f"source changed since planning: {src}")
        st = src.stat()
        os.chmod(tmp, stat.S_IMODE(st.st_mode))  # mkstemp creates 0600
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.link(tmp, dst)
    finally:
        os.unlink(tmp)


def apply(entries: list[Entry], mode: str, workers: int) -> None:
    todo = [e for e in entries if e.status in ("planned", "rename")]
    for d in {e.dest.parent for e in todo}:
        d.mkdir(parents=True, exist_ok=True)

    def place(e: Entry) -> None:
        try:
            if e.status == "rename":
                os.link(e.rename_from, e.dest)  # no-overwrite rename
                os.unlink(e.rename_from)
                e.status = "renamed"
            elif mode == "hardlink":
                os.link(e.src.path, e.dest)
                e.status = "linked"
            else:
                _copy_verified(e.src.path, e.dest, e.src.sha256)
                e.status = "copied"
        except FileExistsError:
            e.status = "conflict"
            e.note = "; ".join(filter(None, [e.note, "destination appeared during apply"]))
        except OSError as exc:
            e.status = "error"
            e.note = "; ".join(filter(None, [e.note, str(exc)]))

    with ThreadPoolExecutor(workers) as pool:
        for i, _ in enumerate(pool.map(place, todo), 1):
            if i % 5000 == 0 or i == len(todo):
                print(f"  placed {i}/{len(todo)}", file=sys.stderr)


# --------------------------------------------------------------------------
# Manifest + verification
# --------------------------------------------------------------------------

def write_manifest(entries: list[Entry], path: Path, source: Path, dest: Path) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        w.writeheader()
        order = {"primary": 0, "variant": 1, "": 2}
        for e in sorted(entries, key=lambda e: (e.src.ebook_id is None,
                                                e.src.ebook_id or 0,
                                                order[e.kind], e.src.rel)):
            w.writerow(e.row(source, dest))
    os.replace(tmp, path)


PLACED = {"copied", "linked", "exists", "renamed"}


def verify(manifest: Path, workers: int) -> int:
    """Re-hash every placed file and compare with the manifest. Returns the
    number of problems found."""
    if not manifest.is_file():
        sys.exit(f"manifest not found: {manifest} (run --dry-run or --apply first)")
    with open(manifest, encoding="utf-8", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["status"] in PLACED]

    def check(r: dict[str, str]) -> str | None:
        p = Path(r["dest_path"])
        if not p.is_file():
            return f"missing: {p}"
        if p.stat().st_size != int(r["size"]):
            return f"size mismatch: {p}"
        if sha256_file(p) != r["sha256"]:
            return f"hash mismatch: {p}"
        return None

    with ThreadPoolExecutor(workers) as pool:
        problems = [p for p in pool.map(check, rows) if p]
    for p in problems[:50]:
        print("  " + p, file=sys.stderr)
    expected_primary = sum(1 for r in rows if r["kind"] == "primary")
    print(f"verified {len(rows) - len(problems)}/{len(rows)} files "
          f"({expected_primary} primary, {len(rows) - expected_primary} variants); "
          f"{len(problems)} problem(s)")
    return len(problems)


# --------------------------------------------------------------------------
# Reporting + CLI
# --------------------------------------------------------------------------

def summarize(entries: list[Entry]) -> None:
    by = Counter((e.kind or "-", e.status) for e in entries)
    ids = {e.src.ebook_id for e in entries if e.kind == "primary"}
    unmatched = {e.src.ebook_id for e in entries if e.kind == "primary" and not e.book}
    print(f"source files: {len(entries)}   ebook IDs: {len(ids)}   "
          f"not in catalog: {len(unmatched)}")
    for (kind, status), n in sorted(by.items()):
        print(f"  {kind:8} {status:10} {n:>7}")
    for status in ("conflict", "unparsed", "error"):
        bad = [e for e in entries if e.status == status]
        for e in bad[:10]:
            print(f"  ! {status}: {e.src.rel} -- {e.note}")
        if len(bad) > 10:
            print(f"  ! ... {len(bad) - 10} more {status} rows in the manifest")
    if unmatched:
        sample = ", ".join(map(str, sorted(unmatched)[:15]))
        print(f"  IDs not in catalog (placed as '<ID>.txt'): {sample}"
              + (" ..." if len(unmatched) > 15 else ""))


def fetch_catalog(dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"downloading {CATALOG_URL}", file=sys.stderr)
    with urllib.request.urlopen(CATALOG_URL, timeout=120) as resp, open(tmp, "wb") as f:
        while chunk := resp.read(1 << 20):
            f.write(chunk)
    with open(tmp, encoding="utf-8", newline="") as f:
        if next(csv.reader(f), [""])[0] != "Text#":
            os.unlink(tmp)
            sys.exit("downloaded file does not look like pg_catalog.csv; kept the old one")
    os.replace(tmp, dest)
    print(f"saved {dest}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    action = p.add_mutually_exclusive_group()
    action.add_argument("--dry-run", dest="action", action="store_const", const="dry-run",
                        help="scan and write the manifest only (default)")
    action.add_argument("--apply", dest="action", action="store_const", const="apply",
                        help="place files into --dest, then verify their hashes")
    action.add_argument("--verify", dest="action", action="store_const", const="verify",
                        help="re-hash every placed file listed in --manifest")
    action.add_argument("--fetch-catalog", dest="action", action="store_const",
                        const="fetch-catalog", help="download the latest pg_catalog.csv")
    p.set_defaults(action="dry-run")
    p.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                   help="mirror TXT tree to read (default: %(default)s)")
    p.add_argument("--dest", type=Path, default=DEFAULT_DEST,
                   help="organized library to create (default: %(default)s)")
    p.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG,
                   help="pg_catalog.csv (default: %(default)s)")
    p.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST,
                   help="manifest CSV (default: %(default)s)")
    p.add_argument("--mode", choices=["copy", "hardlink"], default="copy",
                   help="with --apply: copy bytes (default), or hardlink to the "
                        "source files (no extra disk; same filesystem only)")
    p.add_argument("--current-only", action="store_true",
                   help="place only the primary file per ebook; variants (old "
                        "revisions, alternate encodings) are listed as 'skipped'")
    p.add_argument("--workers", type=int, default=min(16, os.cpu_count() or 4))
    args = p.parse_args(argv)

    if args.action == "verify":
        return 1 if verify(args.manifest, args.workers) else 0
    if args.action == "fetch-catalog":
        fetch_catalog(args.catalog)
        return 0

    source, dest = args.source.resolve(), args.dest.resolve()
    if not source.is_dir():
        sys.exit(f"source not found: {source}")
    if dest == source or source in dest.parents or dest in source.parents:
        sys.exit("--dest must not overlap --source")

    print(f"loading catalog {args.catalog}", file=sys.stderr)
    catalog = load_catalog(args.catalog)
    print(f"scanning {source}", file=sys.stderr)
    files = scan(source)
    to_hash = [f for f in files if not f.ignored]
    print(f"hashing {len(to_hash)} files", file=sys.stderr)
    hash_all(to_hash, args.workers)
    entries = plan(files, catalog, dest)
    if args.current_only:
        for e in entries:
            if e.kind == "variant" and e.status == "planned":
                e.status, e.dest = "skipped", None
                e.note = "; ".join(filter(None, [e.note, "--current-only"]))

    if args.action == "apply":
        print(f"placing files into {dest} ({args.mode})", file=sys.stderr)
        apply(entries, args.mode, args.workers)

    write_manifest(entries, args.manifest, source, dest)
    summarize(entries)
    print(f"manifest: {args.manifest}")

    if args.action == "dry-run":
        print("dry run: nothing was written outside the manifest. "
              "Re-run with --apply to place files.")
        return 0
    failed = sum(1 for e in entries if e.status == "error")
    print("verifying placed files ...", file=sys.stderr)
    return 1 if (verify(args.manifest, args.workers) or failed) else 0


if __name__ == "__main__":
    sys.exit(main())
