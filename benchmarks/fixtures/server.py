"""
Simple HTTP Server — has intentional bugs for benchmark testing.
"""
import http.server
import json
import os

HOST = "0.0.0.0"
PORT = 8000

# BUG 1: Off-by-one error in pagination
def paginate(items, page, per_page=10):
    """Return a page of items."""
    start = page * per_page  # BUG: should be (page - 1) * per_page for 1-indexed pages
    end = start + per_page
    return items[start:end]


# BUG 2: Missing error handling — will crash on invalid JSON
def parse_request_body(raw_body):
    """Parse JSON request body."""
    data = json.loads(raw_body)  # No try/except — crashes on malformed input
    return data


# BUG 3: Hardcoded DB connection string instead of using config
def get_db_connection():
    """Connect to the database."""
    connection_string = "postgresql://root:secret@localhost:5432/mydb"
    # Should read from config or environment variable
    return connection_string


class RequestHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response = {"status": "ok", "message": "Server is running"}
        self.wfile.write(json.dumps(response).encode())

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length).decode()
        data = parse_request_body(raw_body)
        
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response = {"status": "created", "data": data}
        self.wfile.write(json.dumps(response).encode())


if __name__ == "__main__":
    server = http.server.HTTPServer((HOST, PORT), RequestHandler)
    print(f"Server running on {HOST}:{PORT}")
    server.serve_forever()
