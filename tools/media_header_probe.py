import json, pathlib, urllib.request, urllib.error, urllib.parse

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36"
OUT = pathlib.Path("out")


def extract():
    html = (OUT / "direct.txt").read_text("utf-8", errors="replace")
    dec = json.JSONDecoder()
    for marker in ("var ytInitialPlayerResponse = ", "ytInitialPlayerResponse = ", 'window["ytInitialPlayerResponse"] = ', 'ytInitialPlayerResponse":'):
        p = html.find(marker)
        if p < 0:
            continue
        s = html.find("{", p + len(marker))
        if s < 0:
            continue
        try:
            return dec.raw_decode(html[s:])[0]
        except Exception:
            pass
    return None


def attempt(url, name, headers):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read(65536)
            return {"name": name, "ok": True, "status": r.status, "content_type": r.headers.get("Content-Type"), "content_range": r.headers.get("Content-Range"), "bytes": len(data)}
    except urllib.error.HTTPError as e:
        return {"name": name, "ok": False, "status": e.code, "content_type": e.headers.get("Content-Type"), "error": str(e)}
    except Exception as e:
        return {"name": name, "ok": False, "error": f"{type(e).__name__}: {e}"}


obj = extract()
result = {"found_player_response": bool(obj), "attempts": []}
if obj:
    sd = obj.get("streamingData") or {}
    fmts = [f for f in ((sd.get("formats") or []) + (sd.get("adaptiveFormats") or [])) if f.get("url")]
    result["direct_url_formats"] = len(fmts)
    if fmts:
        f = fmts[0]
        url = f["url"]
        result["format"] = {k: f.get(k) for k in ("itag", "mimeType", "width", "height", "fps", "audioQuality")}
        base = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"}
        variants = [
            ("range-basic", {**base, "Range": "bytes=0-65535"}),
            ("range-referer", {**base, "Range": "bytes=0-65535", "Referer": "https://www.youtube.com/", "Origin": "https://www.youtube.com"}),
            ("no-range-referer", {**base, "Referer": "https://www.youtube.com/", "Origin": "https://www.youtube.com"}),
            ("range-watch-referer", {**base, "Range": "bytes=0-65535", "Referer": "https://www.youtube.com/watch?v=GKNS4cNavZU"}),
        ]
        result["attempts"] = [attempt(url, n, h) for n, h in variants]

(OUT / "media-header-probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
print("=== MEDIA HEADER PROBE ===")
print(json.dumps(result, ensure_ascii=False, indent=2))
