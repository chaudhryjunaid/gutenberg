import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gutenberg_organize as go  # noqa: E402

CATALOG = [
    ["Text#", "Type", "Issued", "Title", "Language", "Authors", "Subjects", "LoCC", "Bookshelves"],
    ["7", "Text", "", "The Mayflower Compact", "en", "", "", "", ""],
    ["55", "Text", "", "The Wonderful Wizard of Oz", "en",
     "Baum, L. Frank (Lyman Frank), 1856-1919; Denslow, W. W., 1856-1915 [Illustrator]", "", "", ""],
    ["84", "Text", "", "Frankenstein; or, the modern prometheus", "en",
     "Shelley, Mary Wollstonecraft, 1797-1851", "", "", ""],
    ["1342", "Text", "", "Pride and Prejudice", "en", "Austen, Jane, 1775-1817", "", "", ""],
    ["2291", "Text", "", "David Elginbrod", "en", "MacDonald, George, 1824-1905", "", "", ""],
    ["10453", "Text", "", "A Practical Physiology: A Text-Book for Higher Schools", "en",
     "Blaisdell, Albert F. (Albert Franklin), 1847-1927", "", "", ""],
    ["40393", "Text", "", "The Letters of a Post-Impressionist\nBeing the Familiar Correspondence", "en",
     "Gogh, Vincent van, 1853-1890; Ludovici, Anthony M., 1882-1971 [Translator]", "", "", ""],
]

# relative path -> content (None = same bytes as the previous entry for that ID)
MIRROR = {
    "0/7/7-0.txt": b"mayflower",
    "5/55/55-0.txt": b"oz utf8",
    "5/55/55.txt": b"oz ascii",
    "5/55/55-8.txt": b"oz latin1",
    "8/84/84-0.txt": b"frankenstein",
    "1/3/4/1342/1342-0.txt": b"pride current",
    "1/3/4/1342/old/1342-0.txt": b"pride older edition",
    "1/3/4/1342/old/old-2024-12-08/1342-0.txt": b"pride current",  # byte-identical
    "2/2/9/2291/old/old-2025-04-12/2291-0.txt": b"elginbrod",       # no current file
    "1/0/4/5/10453/10453-0.txt": b"physiology",
    "1/0/4/5/10453/phys-0.txt": b"physiology draft",
    "9/9/9/9/99999/99999-0.txt": b"not catalogued",
    "cache/stray.txt": b"no id here",
    # One level too high, and newer than the real file: must stay a variant.
    "1/3/4/1342-0.txt": b"pride stray copy",
    # Upstream license boilerplate beside each book: never an ebook.
    "1/3/4/1342/LICENSE.txt": b"license boilerplate",
    "8/84/LICENSE.TXT": b"license boilerplate",
    "LICENSE.txt": b"license boilerplate",
}


class NameFormatting(unittest.TestCase):
    def test_person(self):
        cases = {
            "Austen, Jane, 1775-1817": "Jane Austen",
            "Doyle, Arthur Conan, 1859-1930": "Arthur Conan Doyle",
            "Kennedy, John F. (John Fitzgerald), 1917-1963": "John F. Kennedy",
            "King, Martin Luther, Jr., 1929-1968": "Martin Luther King Jr.",
            "Tolstoy, Leo, graf, 1828-1910": "Leo Tolstoy",
            "Gordon, Thomas Edward, Sir, 1832-1914": "Thomas Edward Gordon",
            "Plato, 427? BCE-347? BCE": "Plato",
            "Elizabeth I, Queen of England, 1533-1603": "Elizabeth I",
            "Goethe, Johann Wolfgang von, 1749-1832": "Johann Wolfgang von Goethe",
            "Leonardo, da Vinci, 1452-1519": "Leonardo da Vinci",
            "Pliny, the Elder, 23-79": "Pliny the Elder",
            "Thomas, à Kempis, 1380-1471": "Thomas à Kempis",
            "Sade, marquis de, 1740-1814": "Sade",
            "Prévost, abbé, 1697-1763": "Prévost",
            "Various": "Various",
            "United States": "United States",
        }
        for raw, want in cases.items():
            self.assertEqual(go.format_person(raw), want, raw)

    def test_authors_drop_contributors(self):
        self.assertEqual(go.format_authors(
            "Twain, Mark, 1835-1910; Braakensiek, Joh., 1858-1940 [Illustrator]"), "Mark Twain")
        self.assertEqual(go.format_authors(
            "Raymond, Eric S., 1957- [Editor]; Steele, Guy L., 1954- [Editor]"), "Eric S. Raymond")
        self.assertEqual(go.format_authors(
            "Woolf, Virginia, 1882-1941; Woolf, Leonard, 1880-1969"), "Virginia Woolf & Leonard Woolf")
        self.assertEqual(go.format_authors("A, B; C, D; E, F"), "B A et al.")
        self.assertEqual(go.format_authors(""), "")

    def test_title(self):
        self.assertEqual(go.format_title("Frankenstein; or, the modern prometheus"), "Frankenstein")
        self.assertEqual(go.format_title("Moby Dick; Or, The Whale"), "Moby Dick")
        self.assertEqual(go.format_title("Callias: A Tale of the Fall of Athens"),
                         "Callias: A Tale of the Fall of Athens")
        self.assertEqual(go.format_title("Main Title\nSubtitle line"), "Main Title")


class Filenames(unittest.TestCase):
    def test_spec_examples(self):
        b = go.Book("Pride and Prejudice", "Jane Austen", "en")
        self.assertEqual(go.build_filename(1342, b), "1342 - Pride and Prejudice - Jane Austen.txt")
        self.assertEqual(go.bucket_name(84), "00000-00999")
        self.assertEqual(go.bucket_name(1342), "01000-01999")
        self.assertEqual(go.bucket_name(80999), "80000-80999")

    def test_unsafe_characters(self):
        b = go.Book('AC/DC: "Why?" <Live>', "A | B", "en")
        name = go.build_filename(1, b)
        self.assertEqual(name, "1 - AC-DC - 'Why' Live - A - B.txt")
        for ch in '/\\:*?"<>|':
            self.assertNotIn(ch, name)

    def test_no_catalog_entry(self):
        self.assertEqual(go.build_filename(99999, None), "99999.txt")
        self.assertEqual(go.build_filename(5, None, "old 2020-01-01"), "5 [old 2020-01-01].txt")

    def test_length_limits(self):
        long_latin = go.Book("Volume of Things: " + "word " * 80, "Someone " * 20, "en")
        long_cjk = go.Book("紅樓夢" * 120, "曹雪芹" * 30, "zh")
        for book in (long_latin, long_cjk):
            name = go.build_filename(12345, book, "old 2020-01-01")
            self.assertLessEqual(len(name), go.MAX_NAME_CHARS, name)
            self.assertLessEqual(len(name.encode()), go.MAX_NAME_BYTES, name)
            self.assertTrue(name.startswith("12345 - "))
            self.assertTrue(name.endswith(" [old 2020-01-01].txt"))
        # A colon subtitle is dropped before resorting to truncation, and the
        # author is capped so it can't crowd out the title.
        name = go.build_filename(12345, long_latin)
        self.assertEqual(name.split(" - ")[1], "Volume of Things")
        self.assertTrue(name.split(" - ")[2].endswith("….txt"))
        # Two long authors collapse to 'et al' rather than being cut mid-name.
        pair = go.Book("Short", "Washington Irving & " + "Benjamin Bonneville " * 6, "en")
        self.assertEqual(go.build_filename(1372, pair), "1372 - Short - Washington Irving et al.txt")


class IdDirectory(unittest.TestCase):
    def test_find_id_dir(self):
        self.assertEqual(go.find_id_dir(("0", "7")), 1)
        self.assertEqual(go.find_id_dir(("6", "61")), 1)
        self.assertEqual(go.find_id_dir(("1", "3", "4", "1342")), 3)
        self.assertEqual(go.find_id_dir(("1", "3", "4", "1342", "old", "old-2024")), 3)
        # A nested dir that repeats the ID is a variant, not a new ID dir.
        self.assertEqual(go.find_id_dir(("6", "7", "5", "1", "67514", "67514")), 4)
        self.assertIsNone(go.find_id_dir(("cache",)))
        self.assertIsNone(go.find_id_dir(("1", "3", "1342")))  # wrong prefix

    def test_locate_strays(self):
        self.assertEqual(go.locate(("1", "2", "4", "1", "12410-0.txt")), (12410, (), True))
        self.assertEqual(go.locate(("7", "4", "4", "6", "work", "74469", "74469-0.txt")),
                         (74469, ("work", "74469"), True))
        # The directories must still spell the filename's mirror prefix.
        self.assertEqual(go.locate(("9", "9", "12410-0.txt")), (None, (), False))
        self.assertEqual(go.locate(("cache", "stray.txt")), (None, (), False))


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src, self.dest = root / "txt", root / "txt-organized"
        self.catalog, self.manifest = root / "catalog.csv", root / "manifest.csv"
        with open(self.catalog, "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows(CATALOG)
        for i, (rel, data) in enumerate(MIRROR.items()):
            p = self.src / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            os.chmod(p, 0o664)
            os.utime(p, (1_600_000_000 + i * 86400,) * 2)

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, *extra):
        args = ["--source", str(self.src), "--dest", str(self.dest),
                "--catalog", str(self.catalog), "--manifest", str(self.manifest),
                "--workers", "2", *extra]
        with open(os.devnull, "w") as null:
            out, err = sys.stdout, sys.stderr
            sys.stdout = sys.stderr = null
            try:
                return go.main(args)
            finally:
                sys.stdout, sys.stderr = out, err

    def rows(self):
        with open(self.manifest, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))

    def by_source(self):
        return {Path(r["source_path"]).relative_to(self.src).as_posix(): r for r in self.rows()}

    def library(self):
        return sorted(p.relative_to(self.dest).as_posix()
                      for p in self.dest.rglob("*") if p.is_file())

    def test_dry_run_writes_only_manifest(self):
        self.assertEqual(self.run_tool(), 0)
        self.assertFalse(self.dest.exists())
        rows = self.by_source()
        self.assertEqual(len(rows), len(MIRROR))
        self.assertEqual(rows["1/3/4/1342/1342-0.txt"]["status"], "planned")
        self.assertTrue(rows["1/3/4/1342/1342-0.txt"]["dest_path"].endswith(
            "txt-organized/01000-01999/1342 - Pride and Prejudice - Jane Austen.txt"))

    def test_apply_layout(self):
        self.assertEqual(self.run_tool("--apply"), 0)
        self.assertEqual(self.library(), [
            "00000-00999/55 - The Wonderful Wizard of Oz - L. Frank Baum.txt",
            "00000-00999/7 - The Mayflower Compact.txt",
            "00000-00999/84 - Frankenstein - Mary Wollstonecraft Shelley.txt",
            "01000-01999/1342 - Pride and Prejudice - Jane Austen.txt",
            "02000-02999/2291 - David Elginbrod - George MacDonald.txt",
            "10000-10999/10453 - A Practical Physiology - A Text-Book for Higher Schools"
            " - Albert F. Blaisdell.txt",
            "99000-99999/99999.txt",
            "_variants/00000-00999/55 - The Wonderful Wizard of Oz - L. Frank Baum [ascii].txt",
            "_variants/00000-00999/55 - The Wonderful Wizard of Oz - L. Frank Baum [latin-1].txt",
            "_variants/01000-01999/1342 - Pride and Prejudice - Jane Austen [misplaced].txt",
            "_variants/01000-01999/1342 - Pride and Prejudice - Jane Austen [old 2020-09-19].txt",
            "_variants/10000-10999/10453 - A Practical Physiology - A Text-Book for Higher"
            " Schools - Albert F. Blaisdell [phys-0].txt",
        ])
        rows = self.by_source()
        self.assertEqual(rows["1/3/4/1342/old/old-2024-12-08/1342-0.txt"]["status"], "duplicate")
        self.assertEqual(rows["cache/stray.txt"]["status"], "unparsed")
        self.assertIn("promoted", rows["2/2/9/2291/old/old-2025-04-12/2291-0.txt"]["note"])
        self.assertEqual(rows["9/9/9/9/99999/99999-0.txt"]["in_catalog"], "no")
        self.assertEqual(rows["5/55/55-0.txt"]["kind"], "primary")  # UTF-8 preferred

    def test_apply_preserves_bytes_mtime_and_mode(self):
        self.run_tool("--apply")
        for r in self.rows():
            if r["status"] == "copied":
                src, dst = Path(r["source_path"]), Path(r["dest_path"])
                self.assertEqual(src.read_bytes(), dst.read_bytes())
                self.assertEqual(src.stat().st_mtime_ns, dst.stat().st_mtime_ns)
                self.assertEqual(oct(dst.stat().st_mode & 0o777), "0o664")
                self.assertNotEqual(src.stat().st_ino, dst.stat().st_ino)
        self.assertEqual(list(self.dest.rglob(".*.part")), [])

    def test_rerun_is_idempotent(self):
        self.run_tool("--apply")
        before = self.library()
        self.assertEqual(self.run_tool("--apply"), 0)
        self.assertEqual(self.library(), before)
        statuses = {r["status"] for r in self.rows()}
        self.assertEqual(statuses, {"exists", "duplicate", "unparsed", "ignored"})

    def test_never_overwrites(self):
        target = self.dest / "01000-01999" / "1342 - Pride and Prejudice - Jane Austen.txt"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"someone else's file")
        self.run_tool("--apply")
        self.assertEqual(target.read_bytes(), b"someone else's file")
        self.assertEqual(self.by_source()["1/3/4/1342/1342-0.txt"]["status"], "conflict")

    def test_id_already_present_under_other_name(self):
        old = self.dest / "00000-00999" / "84 - Frankenstein (old title) - M. Shelley.txt"
        old.parent.mkdir(parents=True)
        # Different bytes: an older revision the user kept. Leave it alone.
        old.write_bytes(b"frankenstein, 1818 text")
        self.run_tool("--apply")
        row = self.by_source()["8/84/84-0.txt"]
        self.assertEqual(row["status"], "conflict")
        self.assertIn("already in library", row["note"])
        self.assertEqual([p.name for p in old.parent.glob("84 *")], [old.name])
        # Same bytes: just an outdated name, so it is renamed in place.
        old.write_bytes(b"frankenstein")
        self.run_tool("--apply")
        self.assertEqual([p.name for p in old.parent.glob("84 *")],
                         ["84 - Frankenstein - Mary Wollstonecraft Shelley.txt"])

    def test_license_files_ignored(self):
        self.assertEqual(self.run_tool("--apply"), 0)
        rows = self.by_source()
        for rel in ("1/3/4/1342/LICENSE.txt", "8/84/LICENSE.TXT", "LICENSE.txt"):
            self.assertEqual(rows[rel]["status"], "ignored", rel)
            self.assertEqual(rows[rel]["dest_path"], "", rel)
            self.assertEqual(rows[rel]["sha256"], "", rel)  # not even hashed
        self.assertFalse(any("LICENSE" in p for p in self.library()))

    def test_stray_never_outranks_real_file(self):
        self.run_tool()
        rows = self.by_source()
        self.assertEqual(rows["1/3/4/1342/1342-0.txt"]["kind"], "primary")
        self.assertEqual(rows["1/3/4/1342-0.txt"]["kind"], "variant")
        self.assertIn("outside its ID directory", rows["1/3/4/1342-0.txt"]["note"])

    def test_placeholder_renamed_when_catalog_learns_title(self):
        self.run_tool("--apply")
        placeholder = self.dest / "99000-99999" / "99999.txt"
        self.assertTrue(placeholder.is_file())
        with open(self.catalog, "a", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow(["99999", "Text", "", "Late Arrival", "en", "Doe, Jane", "", "", ""])
        self.assertEqual(self.run_tool("--apply"), 0)
        self.assertFalse(placeholder.exists())
        self.assertEqual(sorted(p.name for p in placeholder.parent.iterdir()),
                         ["99999 - Late Arrival - Jane Doe.txt"])
        self.assertEqual(self.by_source()["9/9/9/9/99999/99999-0.txt"]["status"], "renamed")

    def test_hardlink_mode(self):
        self.assertEqual(self.run_tool("--apply", "--mode", "hardlink"), 0)
        r = self.by_source()["1/3/4/1342/1342-0.txt"]
        self.assertEqual(r["status"], "linked")
        self.assertEqual(Path(r["source_path"]).stat().st_ino, Path(r["dest_path"]).stat().st_ino)

    def test_current_only(self):
        self.run_tool("--apply", "--current-only")
        self.assertFalse((self.dest / go.VARIANTS_DIR).exists())
        self.assertEqual(self.by_source()["5/55/55.txt"]["status"], "skipped")
        self.assertEqual(len(self.library()), 7)

    def test_verify_detects_tampering(self):
        self.run_tool("--apply")
        self.assertEqual(self.run_tool("--verify"), 0)
        victim = Path(self.by_source()["8/84/84-0.txt"]["dest_path"])
        victim.write_bytes(b"frankenstein, edited")
        self.assertEqual(self.run_tool("--verify"), 1)
        victim.unlink()
        self.assertEqual(self.run_tool("--verify"), 1)

    def test_dest_inside_source_rejected(self):
        with self.assertRaises(SystemExit):
            self.run_tool("--dest", str(self.src / "organized"))


if __name__ == "__main__":
    unittest.main()
