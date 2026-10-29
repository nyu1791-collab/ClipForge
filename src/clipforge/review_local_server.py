"""Local-only review prototype. Never expose this server to the internet."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import urlsplit
import zipfile

from .review_package import inspect_archive
from .storage import PipelineError, fingerprint, write_json


def prepare(package):
    with zipfile.ZipFile(package) as bundle:
        selection = json.loads(bundle.read("final-selection.json"))
        transcripts = json.loads(bundle.read("selected-transcripts.json"))
    inspect_archive(package, selection["source_url"])
    with zipfile.ZipFile(package) as bundle:
        page = bundle.read("review.html").decode("utf-8")
    match = re.search(r'<script type="application/json" id="editor-data">(.*?)</script>', page, re.S)
    if not match:
        raise PipelineError("The review page is not editable")
    data = json.loads(match.group(1))
    expected = fingerprint({"selection": selection, "transcripts": transcripts})
    if data.get("fingerprint") != expected:
        raise PipelineError("The review page does not match the package")
    return page, data


def validate_patch(patch, data):
    if not isinstance(patch, dict) or set(patch) != {"reason", "input_fingerprint", "clips"}:
        raise ValueError("Invalid patch")
    if patch["input_fingerprint"] != data["fingerprint"] or not isinstance(patch["reason"], str) or not isinstance(patch["clips"], list):
        raise ValueError("Invalid patch")
    seen_clips = set()
    for row in patch["clips"]:
        if not isinstance(row, dict) or set(row) - {"clip", "segments", "add_segments"}:
            raise ValueError("Invalid clip")
        index = row.get("clip")
        if type(index) is not int or not 1 <= index <= len(data["clips"]) or index in seen_clips:
            raise ValueError("Invalid clip")
        seen_clips.add(index)
        clip = data["clips"][index - 1]
        segments, additions = row.get("segments", []), row.get("add_segments", [])
        if not isinstance(segments, list) or not isinstance(additions, list):
            raise ValueError("Invalid segments")
        seen_segments = set()
        for item in segments + additions:
            if not isinstance(item, dict):
                raise ValueError("Invalid segment")
            added = item in additions
            fields = {"start", "end", "text"} | (set() if added else {"segment"})
            if set(item) != fields:
                raise ValueError("Invalid segment")
            if not added:
                number = item["segment"]
                if type(number) is not int or not 1 <= number <= len(clip["segments"]) or number in seen_segments:
                    raise ValueError("Invalid segment")
                seen_segments.add(number)
            start, end, text = item["start"], item["end"], item["text"]
            if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end)
                or not 0 <= start < end <= clip["duration"]
                or not isinstance(text, str) or len(text) > 10000 or (added and not text.strip())):
                raise ValueError("Invalid subtitle")
    return patch


def serve(package, output, port):
    package, output = Path(package).resolve(), Path(output).resolve()
    page, data = prepare(package)
    token = secrets.token_urlsafe(32)
    lock = threading.Lock()
    script = """<script>
const localToken = TOKEN;
async function saveToServer(){try{const res=await fetch('/patch',{method:'POST',headers:{'Content-Type':'application/json','X-Review-Token':localToken},body:JSON.stringify(buildPatch())});if(!res.ok)throw Error();document.getElementById('local-status').textContent='保存しました。描画はまだ開始していません。'}catch(e){document.getElementById('local-status').textContent='保存に失敗しました。画面を閉じずに再度保存してください。'}}
async function restoreFromServer(){try{const res=await fetch('/patch');if(res.status===204)return;if(!res.ok)throw Error();const patch=await res.json();await loadEdits({files:[new File([JSON.stringify(patch)],'patch.json')]});document.getElementById('local-status').textContent='保存済みの修正を読み込みました。'}catch(e){document.getElementById('local-status').textContent='保存済みの修正を読み込めませんでした。'}}
restoreFromServer();</script>""".replace("TOKEN", json.dumps(token))
    page = page.replace('onclick="saveEdits()">修正ファイルを保存', 'onclick="saveToServer()">修正を保存', 1)
    page = page.replace('修正は自動送信されません。保存したJSONファイルをこの会話に送ってください。画面を閉じる前に保存してください。', '修正したら「修正を保存」を押してください。描画は開始しません。', 1)
    page = page.replace('</script></html>', '</script><p id="local-status" role="status">描画は開始しません。</p>' + script + '</html>', 1)
    if 'onclick="saveToServer()"' not in page or 'id="local-status"' not in page:
        raise PipelineError("Unsupported review page")
    page_bytes = page.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def headers_common(self):
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")

        def authorized_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            if not self.authorized_host():
                self.send_error(403)
                return
            path = urlsplit(self.path).path
            if path == "/":
                self.send_response(200)
                self.headers_common()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page_bytes)))
                self.end_headers()
                self.wfile.write(page_bytes)
                return
            if path == "/patch":
                with lock:
                    raw = output.read_bytes() if output.exists() else None
                self.send_response(200 if raw is not None else 204)
                self.headers_common()
                if raw is not None:
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                if raw is not None:
                    self.wfile.write(raw)
                return
            match = re.fullmatch(r"/clip-(\d{3})\.mp4", path)
            if not match or not 1 <= int(match.group(1)) <= len(data["clips"]):
                self.send_error(404)
                return
            with zipfile.ZipFile(package) as bundle:
                name = path[1:]
                size = bundle.getinfo(name).file_size
                start, end, status = 0, size - 1, 200
                requested = self.headers.get("Range")
                if requested:
                    span = re.fullmatch(r"bytes=(\d+)-(\d*)", requested)
                    if not span or int(span.group(1)) >= size:
                        self.send_response(416)
                        self.headers_common()
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                    start = int(span.group(1))
                    end = min(int(span.group(2)), size - 1) if span.group(2) else size - 1
                    if end < start:
                        self.send_error(416)
                        return
                    status = 206
                self.send_response(status)
                self.headers_common()
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                with bundle.open(name) as stream:
                    stream.seek(start)
                    left = end - start + 1
                    while left:
                        chunk = stream.read(min(left, 1024 * 1024))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)

        def do_POST(self):
            if (not self.authorized_host() or self.headers.get("Origin") != f"http://127.0.0.1:{self.server.server_port}"
                or not secrets.compare_digest(self.headers.get("X-Review-Token", ""), token)
                or self.headers.get("Content-Type") != "application/json"):
                self.send_error(403)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1024 * 1024:
                    raise ValueError("Invalid size")
                patch = validate_patch(json.loads(self.rfile.read(size)), data)
                with lock:
                    write_json(output, patch)
            except (ValueError, TypeError, OSError):
                self.send_error(400)
                return
            self.send_response(204)
            self.headers_common()
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Local review prototype: http://127.0.0.1:{server.server_port}/", flush=True)
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--patch-output", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    serve(args.package, args.patch_output, args.port)


if __name__ == "__main__":
    main()
