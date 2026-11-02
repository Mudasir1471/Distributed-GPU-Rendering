"""
Shared protocol helpers + ffmpeg command builder.

NOTE: client/common.py and server/common.py are IDENTICAL copies so that each
folder can be copied to its own computer and run standalone.

Wire format (every message):
    [4-byte big-endian length][JSON header (UTF-8)]
Some headers announce raw binary payload ("size" field) that follows
immediately after the header (used for the input video and rendered output).
"""
import hashlib
import json
import re
import shutil
import socket
import struct
import subprocess

PORT = 5050
PROTOCOL_VERSION = 1
CHUNK_SIZE = 1024 * 1024
MAX_HEADER_BYTES = 1024 * 1024

# ---------------------------------------------------------------- settings
RESOLUTIONS = {"Original": None, "480p": 480, "720p": 720,
               "1080p": 1080, "1440p": 1440, "2160p": 2160}
PRESETS = ["Fast", "Balanced", "Quality"]
NVENC_PRESET = {"Fast": "p2", "Balanced": "p4", "Quality": "p6"}
X264_PRESET = {"Fast": "veryfast", "Balanced": "medium", "Quality": "slow"}
BITRATE_RE = re.compile(r"^\d{1,4}(\.\d+)?[kKmM]$")


def validate_settings(s):
    """Whitelist-validate user supplied render settings (never trust the wire)."""
    res = str(s.get("resolution", "Original"))
    if res not in RESOLUTIONS:
        raise ValueError(f"unknown resolution '{res}'")
    bitrate = str(s.get("bitrate", "4M")).strip()
    if not BITRATE_RE.match(bitrate):
        raise ValueError(f"invalid bitrate '{bitrate}' (use e.g. 4M or 2500k)")
    preset = str(s.get("preset", "Balanced"))
    if preset not in PRESETS:
        raise ValueError(f"unknown preset '{preset}'")
    return {"resolution": res, "bitrate": bitrate, "preset": preset}


def build_ffmpeg_cmd(ffmpeg, in_path, out_path, settings, use_nvenc, progress=True):
    """Build the ffmpeg argv list (never uses a shell)."""
    cmd = [ffmpeg, "-y", "-hide_banner", "-nostats"]
    cmd += ["-progress", "pipe:1"] if progress else ["-loglevel", "error"]
    cmd += ["-i", in_path, "-map", "0:v:0", "-map", "0:a:0?"]
    height = RESOLUTIONS[settings["resolution"]]
    if height:
        cmd += ["-vf", f"scale=-2:{height}"]
    if use_nvenc:
        cmd += ["-c:v", "h264_nvenc", "-preset", NVENC_PRESET[settings["preset"]]]
    else:
        cmd += ["-c:v", "libx264", "-preset", X264_PRESET[settings["preset"]]]
    cmd += ["-b:v", settings["bitrate"], "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out_path]
    return cmd


def find_ffmpeg():
    return shutil.which("ffmpeg")


def nvenc_works(ffmpeg):
    """True only if an actual 0.2s NVENC test encode succeeds (GPU + driver OK)."""
    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             "color=c=black:s=256x256:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------- protocol
class ProtocolError(Exception):
    pass


class ConnectionClosed(ProtocolError):
    pass


def recv_exact(sock, n):
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        r = sock.recv_into(view[got:], min(n - got, 262144))
        if r == 0:
            raise ConnectionClosed("peer closed the connection")
        got += r
    return bytes(buf)


def send_msg(sock, header):
    data = json.dumps(header).encode("utf-8")
    sock.sendall(struct.pack("!I", len(data)) + data)


def recv_msg(sock):
    (n,) = struct.unpack("!I", recv_exact(sock, 4))
    if n > MAX_HEADER_BYTES:
        raise ProtocolError(f"header too large ({n} bytes)")
    try:
        return json.loads(recv_exact(sock, n).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise ProtocolError(f"malformed header: {e}")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


def send_file(sock, path, size, progress_cb=None):
    sent = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            sock.sendall(chunk)
            sent += len(chunk)
            if progress_cb:
                progress_cb(sent, size)


def recv_file(sock, path, size, progress_cb=None):
    """Receive exactly `size` bytes into `path`; returns the sha256 hex digest."""
    h = hashlib.sha256()
    got = 0
    with open(path, "wb") as f:
        while got < size:
            chunk = sock.recv(min(CHUNK_SIZE, size - got))
            if not chunk:
                raise ConnectionClosed(f"connection dropped after {got}/{size} bytes")
            f.write(chunk)
            h.update(chunk)
            got += len(chunk)
            if progress_cb:
                progress_cb(got, size)
    return h.hexdigest()
