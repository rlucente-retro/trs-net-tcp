#!/usr/bin/env python3
"""
jv1_to_dsk.py - Convert TRS-80 raw JV1 floppy disk images to DiskDISK (.dsk) format.

Prepends the 256-byte DiskDISK header required by TRS-OS / LS-DOS 6 and trs-netd:
  - Magic signature: 'DiskDISK'
  - Jump vector: 0xC3 0x08 0x2E
  - 7 DCT parameters:
      DCT+3: Density / write-protect flags (0x00 = single-density 5.25", WP off)
      DCT+4: Side / step flags (0x10 = single-sided 5.25")
      DCT+5: Current cylinder (0x00)
      DCT+6: Max cylinder count minus 1 (e.g. 34 for 35 tracks, 39 for 40 tracks)
      DCT+7: Heads & sectors/track: ((heads - 1) << 5) | (sec/trk - 1) (0x09 for 1 head, 10 sec/trk)
      DCT+8: Grans/track & sectors/granule: ((grans/trk - 1) << 5) | (sec/gran - 1) (0x24 for 2 grans/trk, 5 sec/gran)
      DCT+9: Directory cylinder (0x11 for Track 17)
  - Granule size byte (5 sectors per granule)
  - Patches the GAT sector (Track 17 Sector 0) with the '\\x03LSI' + DCT signature
    expected by LS-DOS 6.3.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


def convert_jv1_to_dsk(
    jv1_path: Path,
    dsk_path: Path,
    tracks: int = 35,
    sectors_per_track: int = 10,
    dir_track: int = 17,
) -> None:
    data = bytearray(jv1_path.read_bytes())
    expected_size = tracks * sectors_per_track * 256
    if len(data) != expected_size:
        raise ValueError(
            f"File size {len(data):,} bytes does not match expected {expected_size:,} bytes "
            f"for {tracks} tracks x {sectors_per_track} sectors/track x 256B"
        )

    # 1. Construct 256-byte DiskDISK header
    header = bytearray(256)
    header[0:8] = b"DiskDISK"
    header[8:11] = bytes([0xC3, 0x08, 0x2E])

    # 7 DCT parameters (DCT+3 .. DCT+9):
    # DCT+3: Density / write-protect flags (0x00 = single-density 5.25", WP off)
    # DCT+4: Drive type & side flags (0x10 = single-sided 5.25")
    # DCT+5: Current cylinder (0x00)
    # DCT+6: Max cylinder index (tracks - 1, e.g. 34 for 35 tracks)
    # DCT+7: Heads (bits 7..5) and sectors/track (bits 4..0), both 0-offset:
    #        ((heads - 1) << 5) | (sectors_per_track - 1)
    # DCT+8: Granules/track (bits 7..5) and sectors/granule (bits 4..0), both 0-offset:
    #        ((granules_per_track - 1) << 5) | (sectors_per_granule - 1)
    # DCT+9: Directory cylinder / track (e.g. 17 = 0x11)
    heads = 1
    sectors_per_granule = 5 if sectors_per_track == 10 else (sectors_per_track // 2)
    granules_per_track = sectors_per_track // sectors_per_granule

    dct3 = 0x00  # Single-density 5.25"
    dct4 = 0x10  # Single-sided 5.25"
    dct5 = 0x00  # Current cylinder
    dct6 = tracks - 1  # Max cylinder index (34 for 35 tracks)
    dct7 = ((heads - 1) << 5) | (sectors_per_track - 1)  # 0x09 for 1 head, 10 sec/trk
    dct8 = ((granules_per_track - 1) << 5) | (sectors_per_granule - 1)  # 0x24 for 2 grans/trk, 5 sec/gran
    dct9 = dir_track  # Directory cylinder (17 = 0x11)
    granule_size = sectors_per_granule

    header[11:18] = bytes([dct3, dct4, dct5, dct6, dct7, dct8, dct9])
    header[18] = granule_size

    # 2. Patch GAT sector (Track 17, Sector 0) with LS-DOS 6 LSI signature
    gat_sector = dir_track * sectors_per_track
    gat_offset = gat_sector * 256
    lsi_block = b"\x03LSI" + bytes([dct3, dct4, dct5, dct6, dct7, dct8, dct9])
    data[gat_offset + 245 : gat_offset + 256] = lsi_block

    # 3. Write out DiskDISK file (256-byte header + sector data)
    dsk_data = header + data
    dsk_path.write_bytes(dsk_data)
    print(
        f"Converted '{jv1_path.name}' ({len(data):,} bytes) -> "
        f"'{dsk_path.name}' ({len(dsk_data):,} bytes)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert raw JV1 TRS-80 disk image to DiskDISK (.dsk) format for TRS-NET."
    )
    parser.add_argument("input", type=Path, help="Input .JV1 disk image file")
    parser.add_argument(
        "output",
        type=Path,
        nargs="?",
        help="Output .dsk file (defaults to input with .dsk extension)",
    )
    parser.add_argument(
        "--tracks",
        "-t",
        type=int,
        default=35,
        help="Number of tracks (default: 35)",
    )
    parser.add_argument(
        "--sectors",
        "-s",
        type=int,
        default=10,
        help="Sectors per track (default: 10)",
    )
    parser.add_argument(
        "--dir-track",
        "-d",
        type=int,
        default=17,
        help="Directory track (default: 17)",
    )

    args = parser.parse_args()

    if not args.input.is_file():
        print(f"Error: input file '{args.input}' not found", file=sys.stderr)
        sys.exit(1)

    out_path = args.output or args.input.with_suffix(".dsk")
    convert_jv1_to_dsk(
        args.input,
        out_path,
        tracks=args.tracks,
        sectors_per_track=args.sectors,
        dir_track=args.dir_track,
    )


if __name__ == "__main__":
    main()
