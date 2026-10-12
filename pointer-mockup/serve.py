"""Static dev server for the mockup.

`python -m http.server` on Windows takes MIME types from the registry and often serves
.js as text/plain, which browsers refuse for ES modules. This pins the correct types.

Usage:  python serve.py [port]      (default 8000) -> http://localhost:8000
"""
import http.server
import sys
from pathlib import Path


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".css": "text/css",
        ".html": "text/html",
        ".json": "application/json",
        ".svg": "image/svg+xml",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(Path(__file__).parent), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")   # always pick up edits on reload
        super().end_headers()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler) as httpd:
        print(f"Serving mockup at http://localhost:{port}  (Ctrl+C to stop)")
        httpd.serve_forever()
