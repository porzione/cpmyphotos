#!/usr/bin/env python3
""" import photos from card """

import argparse
import shutil
import subprocess
import json
from datetime import datetime
from timeit import default_timer as timer
from pathlib import Path
import dateparser
import piexif

T_LENS_MODEL = 42036
T_COPYRIGHT = 33432

script_dir = Path(__file__).resolve().parent
ext_path = script_dir / 'ext.json'
with open(ext_path, 'r', encoding="utf-8") as file:
    ext_conf = json.load(file)

def tz_diff():
    # ±HHMM[SS[.ffffff]]
    tz_str = datetime.now().astimezone().strftime('%z')
    if tz_str[0] not in ['+', '-']:
        raise ValueError("Invalid timezone format. Must start with '+' or '-'.")

    sign = tz_str[0]

    main_part = tz_str[1:].split('.')[0]

    if len(main_part) not in [4, 6]:
        raise ValueError("Invalid timezone format. Must be HHMM or HHMMSS.")

    tz_res = f"{sign}{main_part[:2]}:{main_part[2:4]}"

    if len(main_part) == 6:
        tz_res += f":{main_part[4:6]}"
    else:
        tz_res += ":00"

    return tz_res

def can_exif(file_ext):
    if file_ext in ['jpg', 'jpeg', 'tif', 'tiff', 'png', 'webp']:
        return True
    return False

def exif_write(image_path, lens_name, copr):
    exif_dict = piexif.load(str(image_path))
    if not exif_dict:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}
    if lens_name:
        exif_dict['Exif'][T_LENS_MODEL] = (lens_name + '\x00').encode()
    if copr:
        exif_dict['0th'][T_COPYRIGHT] = (copr + '\x00').encode('utf-8')
    exif_bytes = piexif.dump(exif_dict)
    piexif.insert(exif_bytes, str(image_path))

parser = argparse.ArgumentParser(description='Photo Copy Script')
parser.add_argument('-D', '--debug', action='store_true', help='Enable debug output')
parser.add_argument('-s', '--srcdir', help='Source directory')
parser.add_argument('-d', '--dstdir', help='Destination directory')
parser.add_argument('-n', '--newer', help='Newer than')
parser.add_argument('-g', '--gpx', action='append', help='GPX file')
parser.add_argument('--tz', default=tz_diff(),
                    help='Time zone / exiftool shift (%(default)s)')

parser.add_argument('-C', dest='exif_copr', help='EXIF Copyright')
parser.add_argument('-L', dest='exif_lens', help='EXIF Lens Model')

args = parser.parse_args()

if args.srcdir is None:
    raise ValueError('Need source directory')
if args.dstdir is None:
    raise ValueError('Need target directory')

src_dir = Path(args.srcdir)
dst_dir = Path(args.dstdir)

if not src_dir.exists():
    raise ValueError(f"Source dir '{src_dir}' doesn't exist")
if not dst_dir.exists():
    raise ValueError(f"Destination dir '{dst_dir}' doesn't exist")

# pylint: disable=invalid-name
parsed_date = None
if args.newer:
    parsed_date = dateparser.parse(args.newer)
    if parsed_date is None:
        raise ValueError(f"Invalid date format for --newer: '{args.newer}'.")

TOTAL_TIME = 0.0
out_files = []
for src_file in src_dir.iterdir():
    if src_file.is_file():
        ext = src_file.suffix[1:].lower()
        if ext not in ext_conf['img'] and ext not in ext_conf['raw']:
            print(f'not an image: {src_file.name}')
            continue

        mod_time = src_file.stat().st_mtime
        if args.newer and parsed_date and mod_time < parsed_date.timestamp():
            print(f'old: {src_file.name} {mod_time}')
            continue

        dst_file = dst_dir / src_file.name
        if dst_file.exists():
            src_size = src_file.stat().st_size
            dst_size = dst_file.stat().st_size
            if src_size == dst_size:
                continue
            print(f"size differs: {src_size} {dst_size}")
            continue

        print(f"COPY {src_file} {dst_file}", end="")
        start_time = timer()
        shutil.copy2(src_file, dst_file)
        dst_file.chmod(0o644)
        if can_exif(ext) and (args.exif_copr or args.exif_lens):
            exif_write(dst_file, args.exif_lens, args.exif_copr)
        end_time = timer() - start_time
        print(f" {end_time:.2f}s")
        TOTAL_TIME += end_time
        if args.gpx:
            out_files.append(str(dst_file))

if out_files:
    cmd = ["exiftool"]

    for gpx_file in args.gpx:
        cmd.extend(["-geotag", gpx_file])

    cmd.extend([f"-geosync={args.tz}", "-overwrite_original_in_place", "-v2", "-P"])

    cmd.extend(out_files)

    print(f'CMD: {" ".join(cmd)}')  # Just for display purposes

    start_time = timer()
    try:
        # Execute the command with shell=False (the default)
        process = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        print(f'OK: {process.stdout}')
        if process.returncode != 0:
            print(f"ERR: e:{process.stderr} rc:{process.returncode}")
    except subprocess.CalledProcessError as e:
        print(f"ERR: {e.stderr}")

    end_time = timer() - start_time
    print(f"processing time: {end_time:.2f}s")
    TOTAL_TIME += end_time

print(f"total time: {TOTAL_TIME:.2f}s")
