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

## Usage

```sh
cpmyphotos -s SRCDIR -d DSTDIR [-r] [-n DATE] [-g GPX]... [--tz OFFSET] [--geosync SHIFT]
           [--require-gps] [-C COPYRIGHT] [-L LENS] [--mode OCTAL | --preserve-mode]
           [--require-src-mount] [--require-dst-mount] [-D]
```

| Option | Meaning |
| --- | --- |
| `-s`, `-d` | source (card) and destination (archive) directories; both must exist |
| `-r` | recurse into the source and keep its directory structure |
| `-n DATE` | only files modified after DATE (anything `dateparser` understands) |
| `-g GPX` | geotag from a GPX track; repeatable |
| `--tz OFFSET` | camera clock timezone, `Z` (default, UTC) or `+HH:MM` |
| `--geosync SHIFT` | camera/GPS clock correction, e.g. `+00:00:25` |
| `--require-gps` | a photo without a track match is an error instead of a warning |
| `-C`, `-L` | EXIF copyright and lens model (JPEG, TIFF, PNG, WebP only) |
| `--mode`, `--preserve-mode` | destination file mode (default 0644), or keep the source's |
| `--require-src-mount`, `--require-dst-mount` | refuse to run unless the dir is a mount point |
| `-D` | show skipped files and ExifTool commands |

It prints `COPY`, `CONFLICT`, `WARNING` and `ERROR` lines and a final
`SUMMARY: scanned= copied= identical= conflicts= unsupported= old= failed= metadata_changed= time=`.
The exit code is 1 if there were conflicts or failures, 2 for usage errors (bad arguments,
missing directories, not enough space).

### Examples

Yesterday's photos with a manual lens and today's GPS log, camera clock at +02:00:

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

Refuse to run if the card or the archive disk is not mounted, so nothing lands on `/`:

```sh
cpmyphotos -s /run/media/${USER}/LUMIX1/DCIM/109_PANA -d /mnt/photos \
  --require-src-mount --require-dst-mount
```

## Tests

The tests use pytest, a tiny real camera JPEG and a synthetic GPX track, and need `exiftool`:

```sh
uv run --with pytest --with dateparser pytest -q
```

`tests/test_real_gpx.py` is an opt-in smoke test against your newest real track:
`REAL_GPX_DIR=~/path/to/gpslogger uv run --with pytest --with dateparser pytest -q -m real_gpx`.
