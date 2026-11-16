"""A tiny local HTTP server that answers like a Databricks workspace, for SDK tests.

It answers only the REST calls that dbx-s3-viewer makes. The real
databricks-sdk talks to it over HTTP, so the tests check the request and
response shapes end to end. The answers follow the public API docs; they
are not copies of real workspace traffic.
"""

from __future__ import annotations

import base64
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "dapi-test-token"
COLUMNS = ["path", "name", "size", "modification_time"]
_LITERAL = r"'((?:[^'\\]|\\.)*)'"
STAT_RE = re.compile(
    r"SELECT count\(\*\) AS files, coalesce\(sum\(length\), 0\) AS bytes "
    rf"FROM read_files\({_LITERAL}, format => 'binaryFile'\)$")
READ_RE = re.compile(
    r"SELECT i, base64\(substr\(content, i \* (\d+) \+ 1, \1\)\) AS piece "
    rf"FROM read_files\({_LITERAL}, format => 'binaryFile'\) "
    r"LATERAL VIEW explode\(sequence\(0, greatest\(CAST\(ceil\(length / \1\) AS INT\) - 1, "
    r"0\)\)\) pieces AS i ORDER BY i$")


def _spark_base64(data: bytes) -> str:
    """Encode like Spark 3.3 and later: 76 characters per line, lines joined by CR LF."""
    text = base64.b64encode(data).decode()
    return "\r\n".join(text[i:i + 76] for i in range(0, len(text), 76))


def _unescape(literal: str) -> str:
    return re.sub(r"\\(.)", r"\1", literal)


def _schema(columns):
    return {"column_count": len(columns),
            "columns": [{"name": n, "position": i, "type_name": "STRING" if n in ("path", "name")
                         else "LONG"} for i, n in enumerate(columns)]}


class MockWorkspace:
    """Start with `with MockWorkspace() as ws:`, then point the SDK at `ws.url`."""

    def __init__(self):
        self.statements: list[dict] = []
        self.polls = 0
        self.cancelled: list[str] = []
        self.warehouse_state = "RUNNING"
        self.locations_status = 200
        #: How many GET polls a statement on a path with "slow" in it takes.
        self.slow_polls = 2
        #: Files that read_files can read: {s3 path: bytes}.
        self.files: dict[str, bytes] = {}
        #: Headers of each request to a result link.
        self.link_requests: list[dict[str, str]] = []
        self._link_chunks: list[list[list[str]]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    # -- answers ------------------------------------------------------------

    def answer_statement(self, body):
        self.statements.append(body)
        statement = body["statement"]
        stat = STAT_RE.match(statement)
        if stat:
            path = _unescape(stat.group(1))
            if path in self.files:
                row = ["1", str(len(self.files[path]))]
            elif any(f.startswith(path + "/") for f in self.files):
                inside = [f for f in self.files if f.startswith(path + "/")]
                row = [str(len(inside)), str(sum(len(self.files[f]) for f in inside))]
            else:
                return 200, {"statement_id": "s-stat", "status": {"state": "FAILED", "error": {
                    "error_code": "BAD_REQUEST",
                    "message": f"[PATH_NOT_FOUND] Path does not exist: {path}."}}}
            return 200, self._succeeded("s-stat", ["files", "bytes"], [row])
        read = READ_RE.match(statement)
        if read:
            size, path = int(read.group(1)), _unescape(read.group(2))
            data = self.files[path]
            count = max(1, -(-len(data) // size))
            rows = [[str(i), _spark_base64(data[i * size:(i + 1) * size])]
                    for i in range(count)]
            if body.get("disposition") == "EXTERNAL_LINKS":
                self._link_chunks = [rows[i:i + 2] for i in range(0, len(rows), 2)]
                response = self._succeeded("s-read", ["i", "piece"], None)
                response["result"] = {"external_links": [self._link(0)]}
                return 200, response
            return 200, self._succeeded("s-read", ["i", "piece"], rows)
        match = re.match(r"LIST '((?:[^'\\]|\\.)*)' LIMIT (\d+)$", statement)
        if not match:
            if statement.startswith("LIST :url"):
                return 200, {"statement_id": "s-marker", "status": {
                    "state": "FAILED", "error": {"error_code": "BAD_REQUEST",
                    "message": "[PARSE_SYNTAX_ERROR] Syntax error at or near ':'."}}}
            return 400, {"error_code": "INVALID_PARAMETER_VALUE", "message": "bad statement"}
        path = re.sub(r"\\(.)", r"\1", match.group(1))
        limit = int(match.group(2))
        if "denied" in path:
            return 200, {"statement_id": "s-denied", "status": {
                "state": "FAILED", "sql_state": "42501", "error": {
                    "error_code": "BAD_REQUEST",
                    "message": "[INSUFFICIENT_PERMISSIONS] Insufficient privileges: User does "
                               "not have READ FILES on External Location 'secret'."}}}
        if "slow" in path:
            return 200, {"statement_id": "s-slow", "status": {"state": "PENDING"}}
        rows = [[f"{path}dir/", "dir/", "0", "0"]]
        rows += [[f"{path}file{i}.csv", f"file{i}.csv", str(100 * i), "1706105124000"]
                 for i in range(5)]
        rows = rows[:limit]
        first, second = rows[:3], rows[3:]
        result = {"chunk_index": 0, "row_offset": 0, "row_count": len(first),
                  "data_array": first}
        if second:
            result["next_chunk_index"] = 1
        self._chunk = {"chunk_index": 1, "row_offset": 3, "row_count": len(second),
                       "data_array": second}
        return 200, {"statement_id": "s-ok", "status": {"state": "SUCCEEDED"},
                     "manifest": {"format": "JSON_ARRAY", "schema": _schema(COLUMNS),
                                  "total_row_count": len(rows),
                                  "total_chunk_count": 2 if second else 1, "truncated": False},
                     "result": result}

    def _succeeded(self, statement_id, columns, rows):
        response = {"statement_id": statement_id, "status": {"state": "SUCCEEDED"},
                    "manifest": {"format": "JSON_ARRAY", "schema": _schema(columns)}}
        if rows is not None:
            response["result"] = {"chunk_index": 0, "row_offset": 0, "row_count": len(rows),
                                  "data_array": rows}
        return response

    def _link(self, index):
        link = {"chunk_index": index, "external_link": f"{self.url}/external/{index}",
                "http_headers": {"x-amz-server-side-encryption": "test"},
                "expiration": "2099-01-01T00:00:00Z"}
        if index + 1 < len(self._link_chunks):
            link["next_chunk_index"] = index + 1
        return link

    def answer_poll(self, statement_id):
        self.polls += 1
        if self.polls < self.slow_polls:
            return 200, {"statement_id": statement_id, "status": {"state": "RUNNING"}}
        return 200, {"statement_id": statement_id, "status": {"state": "SUCCEEDED"},
                     "manifest": {"format": "JSON_ARRAY", "schema": _schema(COLUMNS)},
                     "result": {"chunk_index": 0, "data_array": [
                         ["s3://b/slow/x.csv", "x.csv", "1", "2"]]}}

    def _handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self):
                if self.headers.get("Authorization") == f"Bearer {TOKEN}":
                    return True
                self._send(401, {"error_code": "UNAUTHENTICATED",
                                 "message": "Credential was not sent or was of an unsupported "
                                            "type for this API."})
                return False

            def do_POST(self):
                if not self._authorized():
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                path = self.path.split("?")[0]
                if path == "/api/2.0/sql/statements":
                    self._send(*mock.answer_statement(body))
                elif path.endswith("/cancel"):
                    mock.cancelled.append(path.split("/")[-2])
                    self._send(200, {})
                else:
                    self._send(404, {"error_code": "NOT_FOUND", "message": path})

            def do_GET(self):
                path = self.path.split("?")[0]
                if path.startswith("/external/"):
                    # A result link: cloud storage, which needs no Databricks login.
                    mock.link_requests.append(dict(self.headers))
                    self._send(200, mock._link_chunks[int(path.rsplit("/", 1)[1])])
                    return
                if not self._authorized():
                    return
                if path.startswith("/api/2.0/sql/statements/s-read/result/chunks/"):
                    self._send(200, {"external_links": [mock._link(int(path.rsplit("/", 1)[1]))]})
                elif path.startswith("/api/2.0/sql/statements/") and "/result/chunks/" in path:
                    self._send(200, mock._chunk)
                elif path.startswith("/api/2.0/sql/statements/"):
                    self._send(*mock.answer_poll(path.rsplit("/", 1)[-1]))
                elif path == "/api/2.0/sql/warehouses/wh-missing":
                    self._send(404, {"error_code": "RESOURCE_DOES_NOT_EXIST",
                                     "message": "Warehouse wh-missing does not exist."})
                elif path.startswith("/api/2.0/sql/warehouses/"):
                    self._send(200, {"id": path.rsplit("/", 1)[-1],
                                     "state": mock.warehouse_state})
                elif path == "/api/2.1/unity-catalog/external-locations":
                    if mock.locations_status == 403:
                        self._send(403, {"error_code": "PERMISSION_DENIED",
                                         "message": "User does not have permission."})
                    else:
                        self._send(200, {"external_locations": [
                            {"name": "landing", "url": "s3://landing-bucket/"},
                            {"name": "curated", "url": "s3://curated-bucket/data/"}]})
                else:
                    self._send(404, {"error_code": "NOT_FOUND", "message": path})

        return Handler
