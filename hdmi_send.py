"""HDMI高密度ファイル転送・送信側（この1ファイルだけを対象PCへ渡す）。

対象PCでの準備（初回のみ）:
    python -m pip install numpy opencv-python

16 MiBのランダムデータを外付けディスプレイへ送信:
    python hdmi_send.py --generate-mib 16 --display 1 --fps 8

実在するファイルを送信:
    python hdmi_send.py --file "C:\\path\\to\\file.bin" --display 1 --fps 8

使い方の確認:
    python hdmi_send.py --help

前提と操作:
    - HDMI出力先を1920x1080にする。
    - 送信元PCのスリープと画面電源オフを無効にする。
    - 外付けディスプレイが2台目なら --display 1、複製表示などで
      1台だけ列挙される場合は --display 0 を使う。
    - 白黒ノイズ状の送信画面を全画面表示したままにする。
    - Escキーで終了する。
    - このファイルは他のローカルモジュールや設定ファイルを必要としない。
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import math
import os
import struct
import time
import zlib
from pathlib import Path

try:
    import cv2
    import numpy as np
except ModuleNotFoundError as error:
    raise SystemExit(
        f"Missing dependency: {error.name}\n"
        "Install it with:\n  python -m pip install numpy opencv-python"
    ) from error


WIDTH, HEIGHT, CELL = 1920, 1080, 2
GRID_W, GRID_H = WIDTH // CELL, HEIGHT // CELL
FRAME_BYTES = GRID_W * GRID_H // 8
HEADER_BYTES = 256
CRC_BYTES = 4
PAYLOAD_BYTES = FRAME_BYTES - HEADER_BYTES - CRC_BYTES
MAGIC = b"HDMIXFR1"
VERSION = 1
HEADER_STRUCT = struct.Struct("<8sBBHQIIIIIQHH32s")
WHITEN = np.resize(np.array([0xAA, 0x55], dtype=np.uint8), FRAME_BYTES)


def monitor_rects() -> list[tuple[int, int, int, int]]:
    if os.name != "nt":
        return [(0, 0, WIDTH, HEIGHT)]
    user32 = ctypes.windll.user32
    user32.SetProcessDPIAware()
    rects: list[tuple[int, int, int, int]] = []

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(RECT), ctypes.c_void_p
    )

    def callback(_monitor, _dc, rect, _data):
        r = rect.contents
        rects.append((r.left, r.top, r.right, r.bottom))
        return 1

    user32.EnumDisplayMonitors(None, None, callback_type(callback), None)
    return sorted(rects, key=lambda r: (r[0], r[1]))


def make_header(
    session_id: int,
    sequence: int,
    chunk_index: int,
    chunk_count: int,
    payload_len: int,
    file_size: int,
    digest: bytes,
    filename: str,
) -> bytes:
    filename_bytes = filename.encode("utf-8")[:168]
    fixed = HEADER_STRUCT.pack(
        MAGIC, VERSION, 0, HEADER_BYTES, session_id, sequence,
        chunk_index, chunk_count, PAYLOAD_BYTES, payload_len,
        file_size, len(filename_bytes), 0, digest,
    )
    return fixed + filename_bytes + bytes(HEADER_BYTES - len(fixed) - len(filename_bytes))


def encode_frame_bytes(header: bytes, payload: bytes) -> np.ndarray:
    body = header + payload + bytes(PAYLOAD_BYTES - len(payload))
    packet = body + struct.pack("<I", zlib.crc32(body))
    return np.frombuffer(packet, dtype=np.uint8) ^ WHITEN


def bytes_to_image(packet: np.ndarray) -> np.ndarray:
    bits = np.unpackbits(packet, bitorder="big").reshape(GRID_H, GRID_W)
    mono = np.repeat(np.repeat(bits, CELL, axis=0), CELL, axis=1) * 255
    return cv2.cvtColor(mono.astype(np.uint8), cv2.COLOR_GRAY2BGR)


def load_payload(args: argparse.Namespace) -> tuple[bytes, str]:
    if args.generate_mib is not None:
        size = args.generate_mib * 1024 * 1024
        # Incompressible payload is a fair transport benchmark.
        return os.urandom(size), f"generated_{args.generate_mib}MiB.bin"
    path = args.file.resolve()
    return path.read_bytes(), path.name


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path)
    source.add_argument("--generate-mib", type=int)
    parser.add_argument("--display", type=int, default=0)
    parser.add_argument("--fps", type=float, default=8.0)
    parser.add_argument("--windowed", action="store_true")
    args = parser.parse_args()

    data, filename = load_payload(args)
    digest = hashlib.sha256(data).digest()
    chunk_count = math.ceil(len(data) / PAYLOAD_BYTES)
    session_id = int.from_bytes(os.urandom(8), "little")
    print(
        f"file={filename} bytes={len(data)} chunks={chunk_count} "
        f"payload_per_frame={PAYLOAD_BYTES} sha256={digest.hex()}",
        flush=True,
    )

    rects = monitor_rects()
    print(f"monitors={rects}", flush=True)
    if not 0 <= args.display < len(rects):
        raise SystemExit(f"display must be between 0 and {len(rects) - 1}")
    left, top, right, bottom = rects[args.display]
    if (right - left, bottom - top) != (WIDTH, HEIGHT):
        raise SystemExit(
            f"Selected display is {right-left}x{bottom-top}; set it to {WIDTH}x{HEIGHT}"
        )

    window = "HDMI Binary Sender - press ESC to stop"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.moveWindow(window, left, top)
    cv2.resizeWindow(window, WIDTH, HEIGHT)
    if not args.windowed:
        cv2.setWindowProperty(window, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    sequence = 0
    interval = 1.0 / args.fps
    next_frame_at = time.perf_counter()
    try:
        while True:
            chunk_index = sequence % chunk_count
            start = chunk_index * PAYLOAD_BYTES
            payload = data[start:start + PAYLOAD_BYTES]
            header = make_header(
                session_id, sequence, chunk_index, chunk_count,
                len(payload), len(data), digest, filename,
            )
            image = bytes_to_image(encode_frame_bytes(header, payload))
            cv2.imshow(window, image)
            if cv2.waitKey(1) & 0xFF == 27:
                break
            sequence += 1
            if sequence % chunk_count == 0:
                print(f"completed_pass={sequence // chunk_count} frames={sequence}", flush=True)
            next_frame_at += interval
            delay = next_frame_at - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                next_frame_at = time.perf_counter()
    finally:
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
