"""
Static server with HTTP Range support, for the PMTiles viewer.

Python's SimpleHTTPRequestHandler ignores Range requests, and PMTiles is built
entirely on them -- a single archive is read by byte range rather than as tile
files. Without this the viewer loads the whole 125 MB archive on every tile
fetch, or fails outright.

This mirrors what a real static host (S3, Netlify, Cloudflare Pages) does, so
the deployed behaviour matches local behaviour.

Usage:  python scripts/serve.py [--port 8000] [--root .]
"""

import argparse
import base64
import binascii
import os
import re
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class RangeHandler(SimpleHTTPRequestHandler):
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".pmtiles": "application/octet-stream",
        ".geojson": "application/geo+json",
        ".parquet": "application/vnd.apache.parquet",
    }

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_head(self):
        rng = self.headers.get("Range")
        if not rng:
            return super().send_head()

        m = RANGE_RE.match(rng.strip())
        if not m:
            self.send_error(400, "Malformed Range header")
            return None

        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None

        size = os.fstat(f.fileno()).st_size
        start_s, end_s = m.group(1), m.group(2)
        if start_s == "":                      # suffix range: bytes=-N
            length = int(end_s or 0)
            start = max(0, size - length)
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
        end = min(end, size - 1)

        if start > end or start >= size:
            f.close()
            self.send_response(416, "Requested Range Not Satisfiable")
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None

        length = end - start + 1
        self.send_response(206, "Partial Content")
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.end_headers()

        f.seek(start)
        self._remaining = length
        return _Limited(f, length)

    # --- dev-only screenshot sink --------------------------------------------
    # Lets the page POST a rendered PNG of itself to disk, for the README and
    # for sharing the view. Localhost only, one fixed directory, name
    # sanitised to a stem: it cannot write outside web/screenshots/.
    # Not part of the deployed static site -- a static host has no POST.
    CAPTURE_DIR = Path("web/screenshots")

    def do_POST(self):
        if self.path.split("?")[0] != "/__capture":
            self.send_error(404, "Not found")
            return
        name = re.sub(r"[^A-Za-z0-9_-]", "", self.headers.get("X-Capture-Name", "capture"))
        if not name:
            name = "capture"
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self.send_error(400, "Bad Content-Length")
            return
        if length <= 0 or length > 40 * 1024 * 1024:
            self.send_error(413, "Payload too large or empty")
            return

        body = self.rfile.read(length)
        prefix = b"data:image/png;base64,"
        if not body.startswith(prefix):
            self.send_error(400, "Expected a base64 PNG data URL")
            return
        try:
            raw = base64.b64decode(body[len(prefix):], validate=True)
        except (binascii.Error, ValueError):
            self.send_error(400, "Malformed base64")
            return

        self.CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        out = self.CAPTURE_DIR / f"{name}.png"
        out.write_bytes(raw)
        msg = f"wrote {out} ({len(raw):,} bytes)".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(msg)))
        self.end_headers()
        self.wfile.write(msg)
        sys.stderr.write(msg.decode() + "\n")

    def log_message(self, fmt, *args):          # one line per request, quieter
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


class _Limited:
    """File wrapper that yields at most `length` bytes, for copyfile()."""

    def __init__(self, f, length):
        self.f, self.remaining = f, length

    def read(self, n=-1):
        if self.remaining <= 0:
            return b""
        if n is None or n < 0:
            n = self.remaining
        data = self.f.read(min(n, self.remaining))
        self.remaining -= len(data)
        return data

    def close(self):
        self.f.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--root", default=".")
    args = ap.parse_args()

    handler = partial(RangeHandler, directory=os.path.abspath(args.root))
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"serving {os.path.abspath(args.root)} at http://127.0.0.1:{args.port}/web/")
    print("Range requests: enabled")
    srv.serve_forever()


if __name__ == "__main__":
    main()
