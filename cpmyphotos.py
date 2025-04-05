#!/usr/bin/env python3
""" import photos from card """

import argparse
import os
import shutil
import subprocess
import json
from datetime import datetime
from timeit import default_timer as timer
#from pprint import pprint
import dateparser
import piexif

T_LENS_MODEL = 42036
T_COPYRIGHT = 33432

script_dir = os.path.dirname(os.path.realpath(__file__))
ext_path = os.path.join(script_dir, 'ext.json')
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
    exif_dict = piexif.load(image_path)
    if not exif_dict:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}
    if lens_name:
        exif_dict['Exif'][T_LENS_MODEL] = (lens_name + '\x00').encode()
    if copr:
        exif_dict['0th'][T_COPYRIGHT] = (copr + '\x00').encode('utf-8')
    exif_bytes = piexif.dump(exif_dict)
    piexif.insert(exif_bytes, image_path)

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
if not os.path.exists(args.srcdir):
    raise ValueError(f"Source dir '{args.srcdir}' doesn't exist")
if not os.path.exists(args.dstdir):
    raise ValueError(f"Destination dir '{args.dstdir}' doesn't exist")

# pylint: disable=invalid-name
parsed_date = None
if args.newer:
    parsed_date = dateparser.parse(args.newer)
    if parsed_date is None:
        raise ValueError(f"Invalid date format for --neewer: '{args.newer}'.")

TOTAL_TIME = 0.0
out_files = []
for filename in os.listdir(args.srcdir):
    src_file = os.path.join(args.srcdir, filename)
    if os.path.isfile(src_file):
        ext = os.path.splitext(filename)[1][1:].lower()
        if ext not in ext_conf['img'] and ext not in ext_conf['raw']:
            print(f'not an image: {filename}')
            continue
        mod_time = os.path.getmtime(src_file)
        if args.newer and parsed_date and mod_time < parsed_date.timestamp():
            print(f'old: {filename} {mod_time}')
            continue
        dst_file = os.path.join(args.dstdir, filename)
        if os.path.exists(dst_file):
            src_size = os.path.getsize(src_file)
            dst_size = os.path.getsize(dst_file)
            if src_size == dst_size:
                continue
            print(f"size differs: {src_size} {dst_size}")
            continue
        print(f"COPY {src_file} {dst_file}", end="")
        start_time = timer()
        shutil.copy2(src_file, dst_file)
        os.chmod(dst_file, 0o644)
        if can_exif(ext) and (args.exif_copr or args.exif_lens):
            exif_write(dst_file, args.exif_lens, args.exif_copr)
        end_time = timer() - start_time
        print(f" {end_time:.2f}s")
        TOTAL_TIME += end_time
        if args.gpx:
            out_files.append(dst_file)

if out_files:
    #GPX_ARGS = ' '.join([f"-g {g}" for g in args.gpx])
    #cmd = f"gpscorrelate {GPX_ARGS} -m 600 -z {args.tz} -t -M -v {' '.join(out_files)}"
    GPX_ARGS = ' '.join([f"-geotag {g}" for g in args.gpx])
    cmd = (f"exiftool {GPX_ARGS} -geosync={args.tz} -overwrite_original_in_place "
           f"-v2 -P {' '.join(out_files)}")
    print(f'CMD: {cmd}')
    start_time = timer()
    try:
        process = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, check=True)
        print(f'OK: {process.stdout}')
        if process.returncode != 0:
            print(f"ERR: e:{process.stderr} rc:{process.returncode}")
    except subprocess.CalledProcessError as e:
        print(f"ERR: {e.output}")
    #except Exception as e:
    #    print(f"An unexpected error occurred: {e}")

    end_time = timer() - start_time
    print(f"processing time: {end_time:.2f}s")
    TOTAL_TIME += end_time

print(f"total time: {TOTAL_TIME:.2f}s")
