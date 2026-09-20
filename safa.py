
from __future__ import annotations

import argparse
import ctypes
import os
import time

try:
    import cv2
    import numpy as np
except ModuleNotFoundError as error:
    raise SystemExit(
        f"Missing dependency: {error.name}\n"
        "Install it with:\n  python -m pip install numpy opencv-python"
    ) from error

WIDTH, HEIGHT, CELL = 1920, 1080, 2
GW, GH = WIDTH // CELL, HEIGHT // CELL
N_BITS, HEADER_BITS = GW * GH, 4096
MAGIC = 0x48505242  # HPRB


def monitor_rects():
    if os.name != "nt":
        return [(0, 0, WIDTH, HEIGHT)]
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        user32.SetProcessDPIAware()
    rects = []
    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
    cb_t = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.POINTER(RECT), wintypes.LPARAM)
    def cb(_m, _d, p, _x):
        r = p.contents
        rects.append((r.left, r.top, r.right, r.bottom))
        return 1
    keep = cb_t(cb)
    if not user32.EnumDisplayMonitors(None, None, keep, 0):
        raise ctypes.WinError()
    return sorted(rects, key=lambda r: (r[0], r[1]))


def place_window(name, rect):
    left, top, right, bottom = rect
    cv2.moveWindow(name, left, top)
    cv2.resizeWindow(name, right-left, bottom-top)
    if os.name != "nt":
        cv2.setWindowProperty(name, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        return
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.FindWindowW.restype = wintypes.HWND
    hwnd = u.FindWindowW(None, name)
    if not hwnd:
        raise RuntimeError("sender window was not found")
    setter = getattr(u, "SetWindowLongPtrW", u.SetWindowLongW)
    setter(hwnd, -16, ctypes.c_ssize_t(0x90000000).value)  # popup|visible
    if not u.SetWindowPos(hwnd, wintypes.HWND(-1), left, top, right-left, bottom-top,
                          0x0020 | 0x0040):
        raise ctypes.WinError()


def header64(seq: int) -> np.ndarray:
    raw = MAGIC.to_bytes(4, "big") + (seq & 0xFFFFFF).to_bytes(3, "big")
    raw += bytes([raw[0] ^ raw[1] ^ raw[2] ^ raw[3] ^ raw[4] ^ raw[5] ^ raw[6]])
    return np.unpackbits(np.frombuffer(raw, dtype=np.uint8), bitorder="big")


def pattern_bits(seq: int) -> np.ndarray:
    # Integer-only deterministic pseudo-random pattern; receiver computes the same bits.
    index = np.arange(N_BITS, dtype=np.uint64)
    values = ((index * 0x9E3779B1 + (seq & 0xFFFFFFFF) * 0x85EBCA6B) & 0xFFFFFFFF).astype(np.uint32)
    values ^= values >> np.uint32(16)
    values *= np.uint32(0x7FEB352D)
    values ^= values >> np.uint32(15)
    bits = (values >> np.uint32(31)).astype(np.uint8)
    bits[:HEADER_BITS] = np.tile(header64(seq), HEADER_BITS // 64)
    return bits


def image_for(seq: int) -> np.ndarray:
    cells = pattern_bits(seq).reshape(GH, GW) * 255
    mono = np.repeat(np.repeat(cells, CELL, 0), CELL, 1).astype(np.uint8)
    return cv2.cvtColor(mono, cv2.COLOR_GRAY2BGR)


def calibration(display: int) -> np.ndarray:
    yy, xx = np.indices((HEIGHT, WIDTH))
    mono = ((((xx // 16) ^ (yy // 16)) & 1) * 255).astype(np.uint8)
    out = cv2.cvtColor(mono, cv2.COLOR_GRAY2BGR)
    cv2.rectangle(out, (0, 0), (WIDTH-1, HEIGHT-1), (0, 0, 255), 16)
    cv2.putText(out, f"HDMI PREFLIGHT DISPLAY {display}", (100, 150),
                cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 0, 255), 6, cv2.LINE_AA)
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--display", type=int, default=1)
    p.add_argument("--fps", type=float, default=8.0)
    p.add_argument("--calibration-seconds", type=float, default=5.0)
    a = p.parse_args()
    if a.fps <= 0 or a.calibration_seconds < 0:
        p.error("fps must be > 0 and calibration-seconds must be >= 0")
    rects = monitor_rects()
    print(f"monitors={rects}", flush=True)
    if not 0 <= a.display < len(rects):
        raise SystemExit(f"--display must be 0..{len(rects)-1}")
    name = "HDMI PREFLIGHT"
    cv2.namedWindow(name, cv2.WINDOW_NORMAL)
    cv2.imshow(name, calibration(a.display)); cv2.waitKey(1)
    place_window(name, rects[a.display])
    deadline = time.perf_counter() + a.calibration_seconds
    while time.perf_counter() < deadline:
        if cv2.waitKey(20) & 0xFF == 27:
            return 0
    seq, period, due = 0, 1.0 / a.fps, time.perf_counter()
    print("phase=prbs", flush=True)
    try:
        while True:
            cv2.imshow(name, image_for(seq))
            key = cv2.waitKey(max(1, int((due + period - time.perf_counter()) * 1000))) & 0xFF
            if key == 27:
                break
            seq = (seq + 1) & 0xFFFFFF
            due += period
            if time.perf_counter() - due > period:
                due = time.perf_counter()
            if seq % max(1, round(a.fps * 5)) == 0:
                print(f"alive sequence={seq}", flush=True)
    finally:
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
