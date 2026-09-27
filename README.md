# cpmyphotos

Safely import photos from a camera card into a photo archive, optionally adding copyright and
lens EXIF tags and geotagging from GPX tracks.

- **Never overwrites.** A file that already exists with the same content is reported as
  identical; different content is reported as a conflict and left untouched. Re-running an
  import is always safe.
- **Verified copies.** Every copy is checked with SHA-256, and a file that changes on the card
  during the copy is rejected. Timestamps are preserved; the mode defaults to 0644.
- **Metadata without risk to the archive.** EXIF and GPS are written to a hidden temporary
  copy, which is published only when the work succeeded. One corrupt file fails only itself,
  and temporary files are removed on every exit, including Ctrl-C.
- **Geotagging** from one or more GPX tracks via ExifTool, with camera timezone and clock
  correction, and an optional hard failure for photos without a track match.
- **Short everyday command.** With a small YAML config, `cpmyphotos Cyprus` finds the mounted
  card, copies what is new since the last import from that card into
  `.../{year}/Cyprus/...`, adds your copyright and geotags from the GPX logs of those days.
- Filters by modification date (`-n '1 day ago'`), recurses with `-r` keeping the card's
  directory layout, checks free space before copying, and works on destinations without hard
  links (exFAT, FAT32).
- Standard and RAW formats; the list of extensions is in `ext.json`.

## Requirements

- [uv](https://docs.astral.sh/uv/) — the script declares its Python dependencies inline
  (PEP 723) and runs with `uv run --script`; no virtualenv to manage.
- [ExifTool](https://exiftool.org/) (`libimage-exiftool-perl` on Debian/Ubuntu) for `-C`, `-L`
  and `-g`.

Install by linking the script into your `PATH`; `ext.json` is found next to the real script:

```sh
ln -s "$PWD/cpmyphotos.py" ~/bin/cpmyphotos
```

## Configuration

Copy [`cpmyphotos.example.yaml`](cpmyphotos.example.yaml) to `~/.config/cpmyphotos.yaml`
(`$XDG_CONFIG_HOME`), or pass `--config FILE`. The keys are `copyright`, `lens`, `tz`,
`card_root`, `archive`, `gpx_dir` and `gpx_name`, all optional; an unknown key is an error.
Command-line options override them, and `-C ''` / `-L ''` turn the config's tag off:

```yaml
copyright: MYNAME
archive: /home/ftp/images/{year}/{place}/MYNAME
gpx_dir: ~/SyncPhone/gpslog
```

With it, the everyday import is:

```sh
cpmyphotos -N Cyprus               # dry run: show the card, dates, destination and tracks
cpmyphotos Cyprus
cpmyphotos Cyprus --all            # first import from a new or reformatted card
cpmyphotos Cyprus -n '3 days ago'  # an explicit date instead of the last import
```

- **Card.** Without `-s`, the single mounted dir under `card_root` (default
  `/run/media/$USER` and `/media/$USER`) that contains `DCIM` is used, and it must be a mount
  point. All its `DCIM/*` folders (`109_PANA`, `100OLYMP`, ...) are copied flat into the
  destination. `-s CARD_ROOT` does the same for a chosen card; `-s` on any other dir copies
  that dir, and `-r` copies a tree as it is.
- **Since the last import.** For a card, only files newer than the newest file imported from
  it last time are taken. This is remembered per card mount name (the volume label, or a FAT
  serial such as `4621-0000`) in `~/.local/state/cpmyphotos/cards.json` (`$XDG_STATE_HOME`),
  and updated only after a run without conflicts or failures, never backwards. The first
  import from a card needs `--all` (or `-n DATE`), so an old card is never dumped into the
  current trip by accident. A card reformatted in the camera may get a new serial and then
  counts as new.
- **Old dates.** Card files with mtimes over a year old stop the run before anything else,
  even with `-N`, because they usually mean a camera clock reset; see the last example.
- **Destination.** `{place}` is the command's argument and `{year}` the year of each photo, so a
  trip over New Year is split correctly. The part before `{year}` must exist. `-d` still
  works and may use the same placeholders.
- **GPX.** With `gpx_dir`, the tracks named `gpx_name` (default `%Y%m%d.gpx`) for each photo
  date and a day either side are used, together with any `-g`: the photo date comes from its
  mtime, while GPS loggers such as GPSLogger name tracks by local date. Photo dates without a
  track nearby give one `WARNING`. `--no-gps` turns it off.

## Usage

```sh
cpmyphotos [PLACE] [-s SRCDIR] [-d DSTDIR] [-N] [-r] [-n DATE | --all] [--allow-old-dates]
           [-g GPX... | --no-gps]
           [--tz OFFSET] [--geosync SHIFT] [--require-gps] [-C COPYRIGHT] [-L LENS]
           [--mode OCTAL | --preserve-mode] [--require-src-mount] [--require-dst-mount]
           [--config FILE] [-D]
```

| Option | Meaning |
| --- | --- |
| `PLACE` | value for `{place}` in the destination |
| `-s`, `-d` | source (card) and destination (archive); default: detected card, `archive` |
| `-N`, `--dry-run` | run all checks and list what would be copied, write nothing |
| `-r` | recurse into the source and keep its directory structure |
| `-n DATE` | only files modified after DATE (anything `dateparser` understands) |
| `--all` | the whole card, ignoring its last import |
| `--allow-old-dates` | allow card photos with mtimes over a year old (for old trips or a reset camera clock) |
| `-g GPX` | geotag from a GPX track; repeatable |
| `--no-gps` | no geotagging, even with `gpx_dir` in the config |
| `--tz OFFSET` | camera clock timezone, `Z` (default, UTC) or `+HH:MM` |
| `--geosync SHIFT` | camera/GPS clock correction, e.g. `+00:00:25` or `+5945 05:23:39` for a day-sized error |
| `--require-gps` | a photo without a track match is an error instead of a warning |
| `-C`, `-L` | EXIF copyright and lens model (JPEG, TIFF, PNG, WebP only); `''` for none |
| `--mode`, `--preserve-mode` | destination file mode (default 0644), or keep the source's |
| `--require-src-mount`, `--require-dst-mount` | refuse to run unless the dir is a mount point |
| `--config FILE` | config file instead of `~/.config/cpmyphotos.yaml` |
| `-D` | show skipped files and ExifTool commands |

It first prints the resolved `SOURCE`, `NEWER`, `DEST` (per directory, with counts), `GPX`
and `EXIF`, then `COPY`, `SKIP ...` (with `-D`), `CONFLICT`, `WARNING` and `ERROR` lines and a
final
`SUMMARY: scanned= copied= identical= conflicts= unsupported= old= failed= metadata_changed= time=`,
where `unsupported` counts files with extensions not in `ext.json`. After a card import it
prints `IMPORTED: CARD up to ...`. With `-N` it lists `NEW:` files and a `DRY-RUN:` count
line instead of copying.
The exit code is 1 if there were conflicts or failures, 2 for usage errors (bad arguments,
missing directories, not enough space).

### Examples

Without a config, yesterday's photos with a manual lens and today's GPS log, camera clock at +02:00:

```sh
cpmyphotos -C 'MYNAME' -L '7Artisans 35mm f/0.95' \
  -s /run/media/${USER}/LUMIX1/DCIM/109_PANA -d /home/ftp/images/$(date '+%Y')/lev1/lev2/ \
  -n '1 day ago' -g ~/SyncPhone/gpslog/$(date '+%Y%m%d').gpx --tz '+02:00'
```

Whole card with its `DCIM/...` layout, several tracks, camera clock 25 seconds behind GPS:

```sh
cpmyphotos -r -s /run/media/${USER}/LUMIX1 -d ~/import \
  -g day1.gpx -g day2.gpx --geosync +00:00:25
```

The same with the camera clock at +04:00, and a photo without a track match is an error:

```sh
cpmyphotos -r -s /run/media/${USER}/LUMIX1 -d ~/import -g day1.gpx --tz +04:00 --require-gps
```

Refuse to run if the card or the archive disk is not mounted, so nothing lands on `/`:

```sh
cpmyphotos -s /run/media/${USER}/LUMIX1/DCIM/109_PANA -d /mnt/photos \
  --require-src-mount --require-dst-mount
```

If the camera clock reset, card files over a year old stop the import. After finding the
correct time difference, use `--allow-old-dates` to continue, `--all` (or an explicit `-n`)
to bypass the saved mtime threshold, and `-g` because automatic track selection uses the
incorrect file date. For example, if GPS time is 5945 days, 5 hours, 23 minutes and 39 seconds
ahead of the camera clock:

```sh
cpmyphotos Cyprus --all --allow-old-dates -g correct-day.gpx \
  --geosync '+5945 05:23:39' --require-gps
```

This corrects GPS matching only. The copied image's capture date and mtime, and therefore
its `{year}` archive directory, still reflect the camera's incorrect date. Old photos
already left on a card also require `--allow-old-dates` on later imports.

## Tests

The tests use pytest, a tiny real camera JPEG and a synthetic GPX track, and need `exiftool`:

```sh
uv run --with pytest --with dateparser --with pyyaml pytest -q
```

`tests/test_real_gpx.py` is an opt-in smoke test against your newest real track:
`REAL_GPX_DIR=~/path/to/gpslogger uv run --with pytest --with dateparser --with pyyaml pytest -q -m real_gpx`.

CI also runs pylint with the repository's `.pylintrc`:

```sh
uv run --with pylint --with pytest --with dateparser --with pyyaml \
  pylint --rcfile .pylintrc cpmyphotos.py tests/*.py
```
