import ipaddress
import json
import pathlib
import re
import socket
import urllib.parse
import urllib.request

OUT = pathlib.Path("out")
OUT.mkdir(exist_ok=True)
MAX_BYTES = 5 * 1024 * 1024
TIMEOUT = 25


def validate_url(url: str) -> urllib.parse.ParseResult:
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError("only http/https URLs are allowed")
    if not p.hostname:
        raise ValueError("missing hostname")
    if p.username or p.password:
        raise ValueError("credentials in URL are not allowed")
    infos = socket.getaddrinfo(
        p.hostname,
        p.port or (443 if p.scheme == "https" else 80),
        type=socket.SOCK_STREAM,
    )
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f"blocked non-public address: {ip}")
    return p


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, prefix: str):
    validate_url(url)
    opener = urllib.request.build_opener(SafeRedirect())
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
            "Accept": "*/*",
        },
    )
    with opener.open(req, timeout=TIMEOUT) as resp:
        data = resp.read(MAX_BYTES + 1)
        truncated = len(data) > MAX_BYTES
        if truncated:
            data = data[:MAX_BYTES]
        final_url = resp.geturl()
        headers = dict(resp.headers.items())
        pathlib.Path(f"out/{prefix}.bin").write_bytes(data)
        pathlib.Path(f"out/{prefix}-headers.json").write_text(
            json.dumps(
                {"status": resp.status, "final_url": final_url, "headers": headers},
                ensure_ascii=False,
                indent=2,
            ),
            "utf-8",
        )
        ctype = headers.get("Content-Type", "")
        text_preview = None
        if any(x in ctype.lower() for x in ("text/", "json", "xml", "javascript")):
            charset = resp.headers.get_content_charset() or "utf-8"
            try:
                text_preview = data.decode(charset, errors="replace")
            except Exception:
                text_preview = data.decode("utf-8", errors="replace")
            pathlib.Path(f"out/{prefix}.txt").write_text(text_preview, "utf-8")
        return {
            "ok": True,
            "status": resp.status,
            "final_url": final_url,
            "content_type": ctype,
            "bytes": len(data),
            "truncated": truncated,
            "text_file": f"{prefix}.txt" if text_preview is not None else None,
            "body_file": f"{prefix}.bin",
        }


def youtube_id(url: str):
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower()
    if host in {"youtu.be", "www.youtu.be"}:
        return p.path.strip("/").split("/")[0] or None
    if host.endswith("youtube.com"):
        qs = urllib.parse.parse_qs(p.query)
        if qs.get("v"):
            return qs["v"][0]
        m = re.search(r"/(?:shorts|embed)/([A-Za-z0-9_-]{6,})", p.path)
        if m:
            return m.group(1)
    return None


def main():
    target = pathlib.Path("fetch-target.txt").read_text("utf-8").strip()
    summary = {"target": target, "direct": None, "youtube": None}
    try:
        summary["direct"] = fetch(target, "direct")
    except Exception as e:
        summary["direct"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    vid = youtube_id(target)
    if vid:
        y = {"video_id": vid}
        oembed = "https://www.youtube.com/oembed?" + urllib.parse.urlencode(
            {"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"}
        )
        try:
            y["oembed_fetch"] = fetch(oembed, "youtube-oembed")
            raw = pathlib.Path("out/youtube-oembed.txt").read_text("utf-8")
            y["oembed"] = json.loads(raw)
        except Exception as e:
            y["oembed_fetch"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        try:
            y["thumbnail"] = fetch(thumb, "youtube-thumbnail")
        except Exception as e:
            y["thumbnail"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        summary["youtube"] = y

    pathlib.Path("out/summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), "utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
