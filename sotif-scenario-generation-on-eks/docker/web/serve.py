"""Static server for web/: like `python -m http.server`, but pages, scripts and diagrams are revalidated on every load."""

import http.server
import os

REVALIDATE = (".html", ".js", ".svg", ".css", "/")


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        if self.path.split("?", 1)[0].endswith(REVALIDATE):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()


if __name__ == "__main__":
    # "" = every interface of the container (like `python -m http.server`); the port is reachable only through the ALB.
    http.server.ThreadingHTTPServer(("", int(os.environ.get("PORT", "4173"))), Handler).serve_forever()
