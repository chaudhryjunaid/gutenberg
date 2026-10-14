# Syncing from Project Gutenberg

Source: Project Gutenberg's **[Mirroring How-To](https://www.gutenberg.org/help/mirroring.html)**
(retrieved 2026-10-14). Everything up to [Syncing this library](#syncing-this-library)
restates that page, with its commands copied verbatim. The last section is
specific to this repo and is not from the Gutenberg page.

## Connectivity

A public mirror needs at least a static IP address and a permanent ~1.5 Mb
symmetric (T1) connection. A private mirror works fine on DSL or cable.

## Collections and metadata

Gutenberg offers two collections:

| Collection | rsync module | Contents |
| --- | --- | --- |
| **main** | `gutenberg` | hand-curated HTML and plain text, their zip archives, audio files |
| **generated** | `gutenberg-epub` | EPUB, MOBI (Kindle), generated HTML, other generated files |

- Sizes of each collection are in
  [metadata.json](https://www.gutenberg.org/dirs/metadata.json) at the base of
  the main collection. Together they hold about 6 million files in more than
  60 languages and dozens of formats.
- A private mirror can take a subset, for example only the zip files or no
  audio, using rsync's `--exclude` option.
- The main collection changes daily. The generated collection is sometimes
  rebuilt in large batches, which causes traffic spikes when you sync.

### Directory structure

- **Main:** a hierarchy of small directories. eBook #12345 is in
  `1/2/3/4/12345/`.
- **Generated:** one huge root directory with a subdirectory per book, such as
  `epub/12345/`. Configure your web server **not** to autoindex that root. An
  `index.html` is included to make it harder to enumerate.

There's a historical quirk: inside the main collection the generated content
sits at `cache/epub`, but the catalog metadata uses `cache/generated`. To keep
everything in one tree, make catalog paths such as
`cache/generated/1001/1001.epub` resolve to `cache/epub/1001/1001.epub`.
Gutenberg uses this Apache rule (in `apache.conf`, though it can go elsewhere):

```apache
AllowOverride FileInfo
RewriteEngine on
RewriteRule ^cache/generated(/.*)?$ cache/epub$1 [L]
```

## Software

Use **rsync** to create and refresh a mirror. wget and cURL aren't suitable,
because they have to look at every file to find the recently updated ones.
rsync runs on all Unix-like systems, including macOS, and on Windows through
Cygwin.

### Using rsync

ibiblio is Project Gutenberg's main home, and `gutenberg.pglaf.org` is an
alternate server. The last argument is the local destination directory.

Main collection:

```sh
rsync -av --del rsync.ibiblio.org::gutenberg /var/www/gutenberg
rsync -av --del gutenberg.pglaf.org::gutenberg /var/www/gutenberg
```

Generated collection:

```sh
rsync -av --del rsync.ibiblio.org::gutenberg-epub /var/www/gutenberg-generated
rsync -av --del gutenberg.pglaf.org::gutenberg-epub /var/www/gutenberg-generated
```

### Refreshing the mirror

Run a daily job to pick up updated files: cron on Linux/Unix, Task Scheduler
on Windows. Put one or both of the commands above in a shell script and call
that script from your crontab. Gutenberg offers help with setup if you ask.

Here is the full crontab from one of their mirrors. **Don't copy it blindly.**
At minimum, change the destination path and choose a different time of day to
spread the load on their servers.

```crontab
# Main collection
# Note: we use '--exclude=cache' because we rsync the generated collection into a
#       cache subdirectory in the next crontab so we don't want it deleted here.
15  4 * * * /usr/bin/rsync -avHS --timeout 600 --delete --exclude=cache gutenberg.pglaf.org::gutenberg /data/htdocs/gutenberg

# Generated collection
15 16 * * * /usr/bin/rsync -avHS --timeout 600 --delete gutenberg.pglaf.org::gutenberg-epub /data/htdocs/gutenberg/cache/epub
```

## Running a public mirror

- You can host the main collection, the generated one, or both. Check the
  [mirror list](https://www.gutenberg.org/MIRRORS.ALL) first to see whether
  your location would add coverage.
- Serve it however you like (HTTP, FTP, rsync, Tor, BitTorrent, p2p), but
  **create and update your copy with rsync** from Gutenberg.
- Suggested `rsyncd.conf` for offering public rsync:

  ```ini
  [gutenberg]
      comment = Project Gutenberg main collection
      path = /data/mirrors/gutenberg
      read only = yes
      open noatime = true
      refuse options = c
      exclude = /cache/***

  [gutenberg-epub]
      comment = Project Gutenberg generated collection
      path = /data/mirrors/gutenberg/cache/epub
      read only = yes
      open noatime = true
      dont compress = *
      refuse options = c
  ```

- Once it's installed and tested,
  [contact Project Gutenberg](https://www.gutenberg.org/about/contact_information.html).
  After about a month of confirmed stability and up-to-date content, they add
  you to the mirror list and announce it in their newsletter.
- Only the book directories are offered for mirroring. The XML/RDF catalog is
  in the root of the generated collection, if you want to build your own
  search. Gutenberg doesn't package its website or search software, but most
  of its tools are on [GitHub](https://github.com/gutenbergtools).

---

## Syncing this library

*This section is not from the Gutenberg page. It's tailored to this repo.*

`txt/` is a **filtered copy of the main collection** that holds only the
UTF-8 text files (`*-0.txt`). Every one of its ~70k files matches that
pattern. Keep that filter when you sync. Upstream, each book folder now also
contains a `LICENSE.txt`, and a broader `*.txt` filter would download about
50k of them. `gutenberg_organize.py` skips them (status `ignored`), but there's
no reason to download them in the first place.

**1. Preview** what would change (`-n` = dry run, nothing is downloaded or deleted):

```sh
cd ~/media/gutenberg
rsync -n -av --del --timeout=600 \
  --include='*/' --include='*-0.txt' --exclude='*' \
  gutenberg.pglaf.org::gutenberg txt/
```

**2. Sync** by running the same command without `-n`.

**3. Update the library:**

```sh
python3 gutenberg_organize.py --fetch-catalog && python3 gutenberg_organize.py --apply --mode hardlink
```

Books that are already in place are skipped, so only new or changed books are
added. See [RUN.md](RUN.md).

### Notes

- **Filter order matters.** rsync applies the first matching rule:
  `--include='*/'` lets it descend into every folder, `--include='*-0.txt'`
  keeps the text, and `--exclude='*'` drops everything else. That's also why
  `txt/` contains empty folders such as `cache/`, `etext*/` and `*-h/images/`.
  They're harmless.
- **`--del` only affects `txt/`.** Files Gutenberg removes upstream disappear
  from `txt/`. Your hardlinked library keeps its copies, and
  `txt-organized/` is outside the rsync destination, so rsync never touches it.
- **Never add `--inplace`.** By default rsync replaces changed files with new
  ones, which leaves your library's hardlinked copy unchanged. `--inplace`
  would rewrite the shared file instead (see the cautions in [RUN.md](RUN.md)).
- **Mind the trailing slash.** `txt/` means "into this directory". The
  source has no trailing slash, so the module's contents land directly in it.

### Automating it (weekly)

Gutenberg recommends daily refreshes for public mirrors. For a personal
library, weekly is enough, and it matches how often `pg_catalog.csv` is
updated. Add this with `crontab -e`. It runs Sundays at 03:37, which is off
the hour as the page asks:

```crontab
37 3 * * 0  cd "$HOME/media/gutenberg" && ( /usr/bin/rsync -a --del --timeout=600 --include='*/' --include='*-0.txt' --exclude='*' gutenberg.pglaf.org::gutenberg txt/ && /usr/bin/python3 gutenberg_organize.py --fetch-catalog && /usr/bin/python3 gutenberg_organize.py --apply --mode hardlink ) >> sync.log 2>&1
```

Cron runs with a minimal `PATH`, which is why the command uses full paths
(`/usr/bin/rsync`, `/usr/bin/python3`). Check `sync.log` (git-ignored) after
the first run. The steps are chained with `&&`, so if the rsync fails, the
library isn't updated that week, and the next run catches up.
