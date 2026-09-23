import concurrent.futures
import ipaddress
import json
import pathlib
import re
import socket
import time
import urllib.parse
import urllib.request

OUT = pathlib.Path("out")
OUT.mkdir(exist_ok=True)
MAX_BYTES = 5 * 1024 * 1024
RANGE_BYTES = 64 * 1024
TIMEOUT = 15
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36"


def validate_url(url: str) -> urllib.parse.ParseResult:
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError("only http/https URLs are allowed")
    if not p.hostname:
        raise ValueError("missing hostname")
    if p.username or p.password:
        raise ValueError("credentials in URL are not allowed")
    infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f"blocked non-public address: {ip}")
    return p


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def opener():
    return urllib.request.build_opener(SafeRedirect())


def base_headers():
    return {
        "User-Agent": UA,
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Accept": "*/*",
    }


def fetch(url: str, prefix: str, method="GET", body=None, headers=None):
    validate_url(url)
    req_headers = base_headers()
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    started = time.time()
    with opener().open(req, timeout=TIMEOUT) as resp:
        data = resp.read(MAX_BYTES + 1)
        elapsed = round(time.time() - started, 3)
        truncated = len(data) > MAX_BYTES
        if truncated:
            data = data[:MAX_BYTES]
        final_url = resp.geturl()
        h = dict(resp.headers.items())
        (OUT / f"{prefix}.bin").write_bytes(data)
        (OUT / f"{prefix}-headers.json").write_text(json.dumps({"status": resp.status, "final_url": final_url, "headers": h}, ensure_ascii=False, indent=2), "utf-8")
        ctype = h.get("Content-Type", "")
        text_preview = None
        if any(x in ctype.lower() for x in ("text/", "json", "xml", "javascript")):
            charset = resp.headers.get_content_charset() or "utf-8"
            try:
                text_preview = data.decode(charset, errors="replace")
            except Exception:
                text_preview = data.decode("utf-8", errors="replace")
            (OUT / f"{prefix}.txt").write_text(text_preview, "utf-8")
        return {
            "ok": True,
            "status": resp.status,
            "final_url": final_url,
            "content_type": ctype,
            "bytes": len(data),
            "truncated": truncated,
            "seconds": elapsed,
            "text_file": f"{prefix}.txt" if text_preview is not None else None,
            "body_file": f"{prefix}.bin",
        }


def probe_range(url: str, prefix: str):
    """Read only the first 64 KiB of a public media URL; never expose the signed URL."""
    p = validate_url(url)
    headers = base_headers()
    headers["Range"] = f"bytes=0-{RANGE_BYTES - 1}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    started = time.time()
    with opener().open(req, timeout=TIMEOUT) as resp:
        data = resp.read(RANGE_BYTES)
        h = dict(resp.headers.items())
        (OUT / f"{prefix}-sample.bin").write_bytes(data)
        result = {
            "ok": True,
            "status": resp.status,
            "host": p.hostname,
            "content_type": h.get("Content-Type", ""),
            "content_length": h.get("Content-Length"),
            "content_range": h.get("Content-Range"),
            "accept_ranges": h.get("Accept-Ranges"),
            "sample_bytes": len(data),
            "seconds": round(time.time() - started, 3),
        }
        (OUT / f"{prefix}-sample.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
        return result


def safe_fetch(name, url, method="GET", body=None, headers=None):
    try:
        return name, fetch(url, name, method=method, body=body, headers=headers)
    except Exception as e:
        return name, {"ok": False, "error": f"{type(e).__name__}: {e}"}


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


def extract_player_response():
    path = OUT / "direct.txt"
    if not path.exists():
        return None, None
    html = path.read_text("utf-8", errors="replace")
    decoder = json.JSONDecoder()
    for marker in ("var ytInitialPlayerResponse = ", "ytInitialPlayerResponse = ", 'window["ytInitialPlayerResponse"] = ', 'ytInitialPlayerResponse":'):
        pos = html.find(marker)
        if pos < 0:
            continue
        start = html.find("{", pos + len(marker))
        if start < 0:
            continue
        try:
            obj, _ = decoder.raw_decode(html[start:])
            return obj, marker
        except Exception:
            pass
    return None, None


def parse_and_probe_player_response():
    obj, marker = extract_player_response()
    if not obj:
        return {"found": False}
    ps = obj.get("playabilityStatus") or {}
    vd = obj.get("videoDetails") or {}
    sd = obj.get("streamingData") or {}
    caps = ((obj.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {})
    formats = (sd.get("formats") or []) + (sd.get("adaptiveFormats") or [])
    direct = [f for f in formats if f.get("url")]
    ciphered = [f for f in formats if f.get("signatureCipher") or f.get("cipher")]
    selected = []
    progressive = [f for f in (sd.get("formats") or []) if f.get("url")]
    video_only = [f for f in (sd.get("adaptiveFormats") or []) if f.get("url") and str(f.get("mimeType", "")).startswith("video/")]
    audio_only = [f for f in (sd.get("adaptiveFormats") or []) if f.get("url") and str(f.get("mimeType", "")).startswith("audio/")]
    for label, pool in (("progressive", progressive), ("video", video_only), ("audio", audio_only)):
        if pool:
            f = pool[0]
            try:
                probe = probe_range(f["url"], f"media-{label}")
            except Exception as e:
                probe = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            selected.append({
                "kind": label,
                "itag": f.get("itag"),
                "mimeType": f.get("mimeType"),
                "width": f.get("width"),
                "height": f.get("height"),
                "fps": f.get("fps"),
                "audioQuality": f.get("audioQuality"),
                "probe": probe,
            })
    result = {
        "found": True,
        "marker": marker,
        "playability": {"status": ps.get("status"), "reason": ps.get("reason"), "playableInEmbed": ps.get("playableInEmbed")},
        "video": {"videoId": vd.get("videoId"), "title": vd.get("title"), "author": vd.get("author"), "lengthSeconds": vd.get("lengthSeconds")},
        "streamingData_present": bool(sd),
        "format_count": len(formats),
        "direct_url_formats": len(direct),
        "ciphered_formats": len(ciphered),
        "captions": len(caps.get("captionTracks") or []),
        "media_probes": selected,
    }
    (OUT / "player-summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    return result


def build_routes(target, vid):
    q = urllib.parse.quote(target, safe="")
    routes = {
        "direct": target,
        "jina": "https://r.jina.ai/" + target,
        "allorigins": "https://api.allorigins.win/raw?url=" + q,
        "codetabs": "https://api.codetabs.com/v1/proxy?quest=" + q,
        "microlink": "https://api.microlink.io/?url=" + q,
        "wayback-cdx": "https://web.archive.org/cdx/search/cdx?url=" + q + "&output=json&filter=statuscode:200&limit=3",
        "commoncrawl-short": "https://index.commoncrawl.org/CC-MAIN-2026-30-index?url=" + q + "&output=json",
    }
    if vid:
        watch = f"https://www.youtube.com/watch?v={vid}"
        qw = urllib.parse.quote(watch, safe="")
        routes.update({
            "youtube-oembed": "https://www.youtube.com/oembed?" + urllib.parse.urlencode({"url": watch, "format": "json"}),
            "youtube-embed": f"https://www.youtube.com/embed/{vid}",
            "youtube-nocookie": f"https://www.youtube-nocookie.com/embed/{vid}",
            "youtube-timedtext-list": f"https://www.youtube.com/api/timedtext?v={vid}&type=list",
            "youtube-thumb-default": f"https://i.ytimg.com/vi/{vid}/default.jpg",
            "youtube-thumb-mq": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
            "youtube-thumb-hq": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "youtube-thumb-sd": f"https://i.ytimg.com/vi/{vid}/sddefault.jpg",
            "youtube-thumb-maxres": f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",
            "noembed": "https://noembed.com/embed?url=" + qw,
            "archive-id-search": "https://archive.org/advancedsearch.php?" + urllib.parse.urlencode({"q": vid, "fl[]": ["identifier", "title", "description", "date"], "rows": 5, "page": 1, "output": "json"}, doseq=True),
            "commoncrawl-watch": "https://index.commoncrawl.org/CC-MAIN-2026-30-index?url=" + qw + "&output=json",
            "piped-adminforge": f"https://pipedapi.adminforge.de/streams/{vid}",
            "piped-kavin": f"https://pipedapi.kavin.rocks/streams/{vid}",
            "invidious-yewtu": f"https://yewtu.be/api/v1/videos/{vid}",
            "invidious-nadeko": f"https://inv.nadeko.net/api/v1/videos/{vid}",
        })
    return routes


def main():
    target = pathlib.Path("fetch-target.txt").read_text("utf-8").strip()
    validate_url(target)
    vid = youtube_id(target)
    routes = build_routes(target, vid)
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(safe_fetch, name, url): name for name, url in routes.items()}
        for fut in concurrent.futures.as_completed(futs):
            name, result = fut.result()
            results[name] = result
            print(name, "OK" if result.get("ok") else "FAIL", result.get("status", ""), result.get("bytes", ""), result.get("error", ""))

    extra = {"player_response": parse_and_probe_player_response() if vid else None}
    if vid and results.get("youtube-oembed", {}).get("ok") and (OUT / "youtube-oembed.txt").exists():
        try:
            extra["oembed"] = json.loads((OUT / "youtube-oembed.txt").read_text("utf-8"))
        except Exception as e:
            extra["oembed_error"] = str(e)

    summary = {
        "target": target,
        "video_id": vid,
        "success_count": sum(1 for x in results.values() if x.get("ok")),
        "failure_count": sum(1 for x in results.values() if not x.get("ok")),
        "routes": dict(sorted(results.items())),
        "extra": extra,
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
