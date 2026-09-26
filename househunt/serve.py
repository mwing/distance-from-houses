import functools
import http.server
import threading
import webbrowser
from pathlib import Path


def serve(directory: Path, port: int = 8765, open_browser: bool = True) -> None:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        url = f"http://localhost:{httpd.server_address[1]}/listings.html"
        print(f"Serving {directory} at {url} (Ctrl-C to stop)", flush=True)
        if open_browser:
            threading.Timer(0.5, webbrowser.open, args=(url,)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print()
