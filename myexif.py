#!/usr/bin/env python3
""" copy basic exif """

import sys
import os
import json
import re
import subprocess
#from pprint import pprint
import xmltodict

EXTENSIONS = ['JPG', 'RW2', 'ORF']
EXIF_KEYS = [
    'ExifIFD:ExposureMode',
    'ExifIFD:SensingMethod',
    'ExifIFD:MeteringMode',
    'ExifIFD:ColorSpace',
    'ExifIFD:CreateDate',
    'ExifIFD:DateTimeOriginal',
    'ExifIFD:ExposureCompensation',
    'ExifIFD:ExposureProgram',
    'ExifIFD:ExposureTime',
    'ExifIFD:FNumber',
    'ExifIFD:FocalLength',
    'ExifIFD:ISO', # IFD0 Panasonic:ISO
    'ExifIFD:LensModel', # Olympus:LensModel
    'ExifIFD:MeteringMode',
    'ExifIFD:SensitivityType',
    'GPS:GPSLatitude',
    'GPS:GPSLatitudeRef',
    'GPS:GPSLongitude',
    'GPS:GPSLongitudeRef',
    'GPS:GPSPosition',
    'GPS:GPSVersionID',
    'IFD0:Copyright',
    'IFD0:Make',
    'IFD0:Model',
    'IFD0:ModifyDate',
    'IFD0:Orientation', # IFD1:Orientation Panasonic:CameraOrientation
]

def find_source_image(filename):
    parent_directory = os.path.dirname(os.path.dirname(os.path.abspath(filename)))
    print(f'parent directory: {parent_directory}')
    base_name = os.path.splitext(os.path.basename(os.path.abspath(filename)))[0]
    test_name = re.sub(r'(?<!^)_\w+', '', base_name)
    print(f'test_name: {test_name}')

    for ext in EXTENSIONS:
        potential_file = os.path.join(parent_directory, f'{test_name}.{ext}')
        if os.path.exists(potential_file):
            print(f'found: {potential_file}')
            return potential_file
        potential_files = [f for f in os.listdir(parent_directory)
                           if f.lower() == os.path.basename(potential_file).lower()]
        if potential_files:
            return os.path.join(parent_directory, potential_files[0])

    print(f'none found for {filename}')
    sys.exit(1)

def read_metadata(image_path):
    cmd = ['exiftool', '-e', '-X', image_path]
    with subprocess.Popen(cmd, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        stdout, stderr = proc.communicate()
        if proc.returncode != 0:
            print(f'Error reading metadata: {stderr.decode()}:{stdout.decode}')
            sys.exit(1)

    return xmltodict.parse(stdout)['rdf:RDF']['rdf:Description']

def edit_metadata(old_metadata):
    metadata = {}
    for key in EXIF_KEYS:
        if key in old_metadata:
            metadata[key] = old_metadata[key]
    if 'LensModel' not in metadata:
        for lens in ['LensType', 'LensID', 'Panasonic:LensType', 'ExifIFD:LensModel']:
            if lens in old_metadata and old_metadata[lens]:
                metadata['LensModel'] = old_metadata[lens]
                print(f'lens found in {lens}: {old_metadata[lens]}')
    if 'FocalLength' in metadata and ' ' in metadata['FocalLength']:
        metadata['FocalLength'] = metadata['FocalLength'].split(' ')[0]
    #pprint(metadata); exit()
    for metakey, metaval in metadata.items():
        if isinstance(metaval, list):
            metadata[metakey] = metaval[0]
    return [metadata]

def apply_metadata(source_metadata, target_image):
    metadata_json = json.dumps(source_metadata)
    cmd = ['exiftool', '-v1', '-j=-', '-overwrite_original', target_image]
    print(f'cmd: {cmd}')
    with subprocess.Popen(cmd, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        print(f'metadata json: {metadata_json}')
        stdout, stderr = proc.communicate(input=metadata_json.encode())
        if proc.returncode != 0:
            print(f'Error applying metadata: {proc.returncode}:{stderr.decode()}')
            sys.exit(1)

        print(f'done: {stdout.decode()}')

if __name__ == '__main__':
    if len(sys.argv) != 2:
        print('args?!')
        sys.exit(1)

    TARGET_IMAGE_PATH = sys.argv[1]
    source_image_path = find_source_image(TARGET_IMAGE_PATH)
    edited_metadata = edit_metadata(read_metadata(source_image_path))
    # pprint(edited_metadata)
    apply_metadata(edited_metadata, TARGET_IMAGE_PATH)
