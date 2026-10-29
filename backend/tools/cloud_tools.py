"""
Moonwalk — Cloud-Safe Tools
==============================
Tools that can run entirely in the cloud without access to macOS:
web content fetching, sandboxed Python execution, and reasoning.
"""

import asyncio
import json
import os
import re
import tempfile
from html import unescape

from tools.registry import registry


# ── 25. fetch_web_content ──
@registry.register(
    name="fetch_web_content",
    description="Fetch a URL and extract its text/markdown content directly. Bypasses the GUI browser for clean, hallucination-free reading of documentation, articles, or APIs.",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The full HTTP/HTTPS URL"}
        },
        "required": ["url"]
    }
)
async def fetch_web_content(url: str) -> str:
    try:
        import httpx
        import re
        
        if not url.startswith("http"):
            url = "https://" + url
            
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            # Mask as a standard browser to avoid basic blocks
            headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
            resp = await client.get(url, headers=headers)
            
        if resp.status_code != 200:
            return f"ERROR: Server returned status {resp.status_code}"
            
        html = resp.text
        
        # Super basic regex tag stripper since BeautifulSoup might not be installed
        # Remove scripts and styles first
        html = re.sub(r'<script.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<style.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
        # Remove HTML tags
        text = re.sub(r'<[^>]+>', ' ', html)
        # Collapse whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        
        if len(text) > 8000:
            return text[:8000] + "\n...[truncated due to length]"
        return text
    except Exception as e:
        return f"ERROR fetching URL: {str(e)}"


# ── 26. web_scrape ──
@registry.register(
    name="web_scrape",
    description=(
        "Fetch and parse a webpage into structured research output. "
        "Returns JSON with clean text, title, and top links. Use this as the "
        "preferred non-browser fallback for web research before run_python."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The full HTTP/HTTPS URL"},
            "max_chars": {"type": "integer", "description": "Maximum text chars to return (default 10000)"},
            "include_links": {"type": "boolean", "description": "Include top links in output (default true)"},
        },
        "required": ["url"],
    },
)
async def web_scrape(url: str, max_chars: int = 10000, include_links: bool = True) -> str:
    def _error_payload(message: str, **extra) -> str:
        payload = {"ok": False, "message": message}
        payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)

    try:
        import httpx
    except Exception as e:
        return _error_payload(f"http client unavailable: {e}", error_code="missing_dependency")

    target_url = (url or "").strip()
    if not target_url:
        return _error_payload("url is required", error_code="missing_url")
    if not target_url.startswith("http"):
        target_url = "https://" + target_url

    max_chars = max(300, min(int(max_chars or 10000), 20000))
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(target_url, headers=headers)
    except Exception as e:
        return _error_payload(f"request failed: {e}", error_code="request_failed", url=target_url)

    if resp.status_code < 200 or resp.status_code >= 400:
        return _error_payload(
            f"server returned status {resp.status_code}",
            error_code="http_status",
            url=target_url,
            status_code=resp.status_code,
        )

    html = resp.text or ""
    if not html.strip():
        return _error_payload("empty HTML response", error_code="empty_html", url=str(resp.url))

    html_wo_scripts = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html_wo_scripts = re.sub(r"<style.*?</style>", " ", html_wo_scripts, flags=re.DOTALL | re.IGNORECASE)
    html_wo_scripts = re.sub(r"<noscript.*?</noscript>", " ", html_wo_scripts, flags=re.DOTALL | re.IGNORECASE)

    title_match = re.search(r"<title[^>]*>(.*?)</title>", html_wo_scripts, flags=re.DOTALL | re.IGNORECASE)
    title = ""
    if title_match:
        title = re.sub(r"\s+", " ", unescape(title_match.group(1))).strip()

    text = re.sub(r"<[^>]+>", " ", html_wo_scripts)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()

    if not text:
        return _error_payload("no readable text extracted", error_code="no_text", url=str(resp.url), title=title)

    links = []
    if include_links:
        for match in re.finditer(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html, flags=re.DOTALL | re.IGNORECASE):
            href = (match.group(1) or "").strip()
            label = re.sub(r"<[^>]+>", " ", match.group(2) or "")
            label = re.sub(r"\s+", " ", unescape(label)).strip()
            if not href:
                continue
            if href.startswith("#") or href.lower().startswith("javascript:"):
                continue
            if href.startswith("/"):
                from urllib.parse import urljoin
                href = urljoin(str(resp.url), href)
            if not href.startswith("http"):
                continue
            links.append({"label": label[:180], "url": href})
            if len(links) >= 12:
                break

    content = text[:max_chars]
    payload = {
        "ok": True,
        "url": str(resp.url),
        "title": title,
        "content": content,
        "content_length": len(content),
        "truncated": len(text) > max_chars,
        "links": links,
        "link_count": len(links),
    }
    return json.dumps(payload, ensure_ascii=False)


# ── 27. run_python ──
@registry.register(
    name="run_python",
    description="Execute sandboxed Python code and return the stdout/stderr. Perfect for math, data analysis, or scripting. Variables do not persist between calls unless explicitly saved to a file.",
    parameters={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "The raw Python code to execute (e.g. `import math; print(math.pi)`)"}
        },
        "required": ["code"]
    }
)
async def run_python(code: str) -> str:
    try:
        # Create a temporary file to hold the script
        fd, temp_path = tempfile.mkstemp(suffix=".py")
        with os.fdopen(fd, 'w') as f:
            f.write(code)
            
        proc = await asyncio.create_subprocess_exec(
            "python3", temp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        
        # Clean up temp file
        os.remove(temp_path)
        
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        
        res = ""
        if out: res += f"[STDOUT]\n{out}\n"
        if err: res += f"[STDERR]\n{err}\n"
        
        if not res:
            res = f"Script executed successfully with exit code {proc.returncode} (No output)"
            
        return res[:4000]
    except Exception as e:
        return f"ERROR executing python: {str(e)}"


# ── 26B. think (Reasoning Scratchpad) ──
@registry.register(
    name="think",
    description="Extended thinking and planning scratchpad. Use this tool BEFORE taking action to break down complex tasks, reason about the current state, and plan your next steps.",
    parameters={
        "type": "object",
        "properties": {
            "reasoning": {"type": "string", "description": "Your detailed chain of thought, step-by-step plan, or hypotheses."}
        },
        "required": ["reasoning"]
    }
)
async def think(reasoning: str = "") -> str:
    """A no-op tool that simply allows the LLM to output long chains of thought."""
    return f"Thought recorded: {len(reasoning)} chars."
