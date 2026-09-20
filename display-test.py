

from __future__ import annotations

import argparse
import ctypes
import hashlib
import math
import os
import struct
import sys
import time
import traceback
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
HEADER_BYTES, CRC_BYTES = 256, 4
PAYLOAD_BYTES = FRAME_BYTES - HEADER_BYTES - CRC_BYTES
MAGIC, VERSION = b"HDMIXFR1", 1
HEADER_STRUCT = struct.Struct("<8sBBHQIIIIIQHH32s")
WHITEN = np.resize(np.array([0xAA, 0x55], dtype=np.uint8), FRAME_BYTES)
FPS, PASSES = 8.0, 3


def configure_dpi() -> None:
    if os.name != "nt":
        raise SystemExit("This sender requires Windows.")
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    try:
        fn = user32.SetProcessDpiAwarenessContext
        fn.argtypes, fn.restype = [ctypes.c_void_p], wintypes.BOOL
        if fn(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return
    except AttributeError:
        pass
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        fn = shcore.SetProcessDpiAwareness
        fn.argtypes, fn.restype = [ctypes.c_int], ctypes.c_long
        if fn(2) == 0:
            return
    except (AttributeError, OSError):
        pass
    fn = user32.SetProcessDPIAware
    fn.argtypes, fn.restype = [], wintypes.BOOL
    fn()


def active_monitors() -> list[dict[str, object]]:
    """Return active Win32 monitors with stable device, primary flag and rect."""
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                    ("rcWork", RECT), ("dwFlags", wintypes.DWORD),
                    ("szDevice", wintypes.WCHAR * 32)]

    hmonitor_t = getattr(wintypes, "HMONITOR", wintypes.HANDLE)
    hdc_t = getattr(wintypes, "HDC", wintypes.HANDLE)
    callback_t = ctypes.WINFUNCTYPE(
        wintypes.BOOL, hmonitor_t, hdc_t, ctypes.POINTER(RECT), wintypes.LPARAM
    )
    get_info = user32.GetMonitorInfoW
    get_info.argtypes = [hmonitor_t, ctypes.POINTER(MONITORINFOEXW)]
    get_info.restype = wintypes.BOOL
    found: list[dict[str, object]] = []

    def callback(handle, _dc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if not get_info(handle, ctypes.byref(info)):
            return 0
        r = info.rcMonitor
        found.append({
            "handle": handle,
            "device": str(info.szDevice),
            "primary": bool(info.dwFlags & 1),
            "rect": (r.left, r.top, r.right, r.bottom),
        })
        return 1

    callback_ref = callback_t(callback)
    enum = user32.EnumDisplayMonitors
    enum.argtypes = [hdc_t, ctypes.POINTER(RECT), callback_t, wintypes.LPARAM]
    enum.restype = wintypes.BOOL
    if not enum(None, None, callback_ref, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return sorted(found, key=lambda item: str(item["device"]))


def choose_external_monitor(monitors: list[dict[str, object]]) -> dict[str, object]:
    description = "; ".join(
        f"{m['device']} primary={m['primary']} rect={m['rect']}" for m in monitors
    ) or "(none)"
    external = [m for m in monitors if not m["primary"]]
    if len(external) != 1:
        raise SystemExit(
            "Refusing to display: expected exactly one active non-primary monitor "
            f"in Windows Extend mode, found {len(external)}.\nDetected: {description}\n"
            "Open Settings > System > Display, select Extend, and leave only the "
            "HDMI capture output as the non-primary display."
        )
    chosen = external[0]
    left, top, right, bottom = chosen["rect"]  # type: ignore[misc]
    if (right - left, bottom - top) != (WIDTH, HEIGHT):
        raise SystemExit(
            f"Refusing to display: external monitor {chosen['device']} is "
            f"{right-left}x{bottom-top}, not {WIDTH}x{HEIGHT}. Set it to 1920x1080.\n"
            f"Detected: {description}"
        )
    return chosen


def bounded_filename(name: str) -> bytes:
    raw = name.encode("utf-8")
    while len(raw) > 168:
        name = name[:-1]
        raw = name.encode("utf-8")
    return raw


def packet_image(session: int, sequence: int, chunk_index: int,
                 chunk_count: int, payload: bytes, file_size: int,
                 digest: bytes, filename: bytes) -> np.ndarray:
    fixed = HEADER_STRUCT.pack(
        MAGIC, VERSION, 0, HEADER_BYTES, session, sequence, chunk_index,
        chunk_count, PAYLOAD_BYTES, len(payload), file_size, len(filename), 0, digest,
    )
    header = fixed + filename + bytes(HEADER_BYTES - len(fixed) - len(filename))
    body = header + payload + bytes(PAYLOAD_BYTES - len(payload))
    packet = body + struct.pack("<I", zlib.crc32(body))
    encoded = np.frombuffer(packet, dtype=np.uint8) ^ WHITEN
    bits = np.unpackbits(encoded, bitorder="big").reshape(GRID_H, GRID_W)
    mono = np.repeat(np.repeat(bits, CELL, axis=0), CELL, axis=1) * 255
    return cv2.cvtColor(mono.astype(np.uint8), cv2.COLOR_GRAY2BGR)


def position_and_verify(window: str, monitor: dict[str, object]) -> None:
    """Make a borderless window and prove it occupies only the chosen monitor."""
    from ctypes import wintypes
    left, top, right, bottom = monitor["rect"]  # type: ignore[misc]
    width, height = right - left, bottom - top
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    find = user32.FindWindowW
    find.argtypes, find.restype = [wintypes.LPCWSTR, wintypes.LPCWSTR], wintypes.HWND
    hwnd = find(None, window)
    if not hwnd:
        raise RuntimeError("Could not locate sender window before displaying data")

    GWL_STYLE, WS_POPUP, WS_VISIBLE = -16, 0x80000000, 0x10000000
    try:
        set_style = user32.SetWindowLongPtrW
        set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        set_style.restype = ctypes.c_ssize_t
        style = WS_POPUP | WS_VISIBLE
    except AttributeError:
        set_style = user32.SetWindowLongW
        set_style.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
        set_style.restype = wintypes.LONG
        style = ctypes.c_long(WS_POPUP | WS_VISIBLE).value
    ctypes.set_last_error(0)
    previous = set_style(hwnd, GWL_STYLE, style)
    if previous == 0 and ctypes.get_last_error():
        raise ctypes.WinError(ctypes.get_last_error())

    set_pos = user32.SetWindowPos
    set_pos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                        ctypes.c_int, ctypes.c_int, wintypes.UINT]
    set_pos.restype = wintypes.BOOL
    if not set_pos(hwnd, wintypes.HWND(-1), left, top, width, height, 0x20 | 0x40):
        raise ctypes.WinError(ctypes.get_last_error())

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
    actual = RECT()
    get_rect = user32.GetWindowRect
    get_rect.argtypes, get_rect.restype = [wintypes.HWND, ctypes.POINTER(RECT)], wintypes.BOOL
    if not get_rect(hwnd, ctypes.byref(actual)):
        raise ctypes.WinError(ctypes.get_last_error())
    actual_rect = (actual.left, actual.top, actual.right, actual.bottom)
    if actual_rect != monitor["rect"]:
        raise RuntimeError(f"Window placement verification failed: {actual_rect} != {monitor['rect']}")

    nearest = user32.MonitorFromWindow
    nearest.argtypes, nearest.restype = [wintypes.HWND, wintypes.DWORD], wintypes.HANDLE
    if nearest(hwnd, 2) != monitor["handle"]:  # MONITOR_DEFAULTTONEAREST
        raise RuntimeError("Window is not on the selected external monitor")


def sha256_file(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.digest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Send one file over the HDMI capture display.")
    parser.add_argument("file", type=Path, help="file to transfer")
    args = parser.parse_args()
    path = args.file.expanduser().resolve()
    if not path.is_file():
        parser.error(f"file not found or not a regular file: {path}")

    configure_dpi()
    monitors = active_monitors()
    monitor = choose_external_monitor(monitors)  # Must succeed before any UI exists.
    file_size = path.stat().st_size
    chunk_count = max(1, math.ceil(file_size / PAYLOAD_BYTES))
    if chunk_count > 0xFFFFFFFF:
        raise SystemExit("File is too large for this protocol.")
    digest = sha256_file(path)
    filename = bounded_filename(path.name)
    session = int.from_bytes(os.urandom(8), "little")
    total_frames = chunk_count * PASSES
    estimated = total_frames / FPS
    print(
        f"SEND session_id={session} file={path.name!r} bytes={file_size} sha256={digest.hex()}\n"
        f"external_device={monitor['device']} rect={monitor['rect']} "
        f"chunks={chunk_count} passes={PASSES} fps={FPS:g} "
        f"estimated_seconds={estimated:.1f}", flush=True,
    )

    window = "HDMI File Sender"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    # namedWindow creates the native HWND; placement is verified before imshow,
    # so no data window is ever shown on the primary display.
    position_and_verify(window, monitor)
    started = time.perf_counter()
    sequence = 0
    next_at = started
    aborted = False
    try:
        for pass_number in range(1, PASSES + 1):
            with path.open("rb") as stream:
                for chunk_index in range(chunk_count):
                    payload = stream.read(PAYLOAD_BYTES)
                    image = packet_image(
                        session, sequence, chunk_index, chunk_count, payload,
                        file_size, digest, filename,
                    )
                    cv2.imshow(window, image)
                    if cv2.waitKey(1) & 0xFF == 27:
                        aborted = True
                        break
                    sequence = (sequence + 1) & 0xFFFFFFFF
                    next_at += 1.0 / FPS
                    delay = next_at - time.perf_counter()
                    if delay > 0:
                        time.sleep(delay)
                    else:
                        next_at = time.perf_counter()
                if aborted:
                    break
            print(f"progress pass={pass_number}/{PASSES} frames={sequence}/{total_frames}", flush=True)
    finally:
        cv2.destroyAllWindows()

    elapsed = time.perf_counter() - started
    if aborted:
        print(f"ABORTED frames={sequence}/{total_frames} elapsed_seconds={elapsed:.2f}", flush=True)
        return 2
    print(
        f"SEND_COMPLETE session_id={session} file={path.name!r} bytes={file_size} sha256={digest.hex()} "
        f"frames={sequence} passes={PASSES} elapsed_seconds={elapsed:.2f}", flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        report = traceback.format_exc()
        log_path = Path(__file__).with_name("send_file_error.log")
        log_path.write_text(report, encoding="utf-8")
        print(report, file=sys.stderr, flush=True)
        print(f"Error log: {log_path}", file=sys.stderr, flush=True)
        raise SystemExit(1)
