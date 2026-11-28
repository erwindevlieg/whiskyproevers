#!/usr/bin/env python3
"""Download bottle images and build small WebP list thumbnails with alpha."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

DEFAULT_CACHE = Path("/workspace/whisky_build/imgcache")
THUMB_HEIGHT = 160
WORK_HEIGHT = 320
WEBP_QUALITY = 70
BORDER_TOL = 22
BORDER_FRAC = 0.86
FLOOD_TOL = 45
FEATHER = 1.6
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Allow sites with slightly odd certs; we only fetch public product photos.
_SSL = ssl.create_default_context()


def url_cache_name(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def _ext_from_url(url: str) -> str:
    path = url.split("?", 1)[0].lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ".bin"


def cache_path(cache_dir: Path, url: str) -> Path:
    return cache_dir / (url_cache_name(url) + _ext_from_url(url))


def download_one(url: str, dest: Path, timeout: int = 25, tries: int = 4) -> tuple[bool, str]:
    """Download met herhaling: sommige winkels (Drankgigant) breken nu en dan een TLS-verbinding af."""
    msg = ""
    for attempt in range(tries):
        ok, msg = _download_once(url, dest, timeout)
        if ok or not re.search(r"(?i)ssl|eof|timed out|reset|temporar", msg):
            return ok, msg
        time.sleep(1.5 * (attempt + 1))
    return False, msg


def _download_once(url: str, dest: Path, timeout: int = 25) -> tuple[bool, str]:
    if dest.is_file() and dest.stat().st_size > 32:
        return True, "cached"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "nl,en;q=0.8",
    }
    # Some shops check a same-site referer.
    try:
        host = urllib.request.urlparse(url).netloc
        scheme = urllib.request.urlparse(url).scheme
        headers["Referer"] = "%s://%s/" % (scheme, host)
    except Exception:
        pass
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as res:
            data = res.read()
            ctype = (res.headers.get("Content-Type") or "").lower()
        if len(data) < 32:
            return False, "empty"
        if "text/html" in ctype or data.lstrip()[:15].lower().startswith(b"<!doctype") or data.lstrip()[:6].lower().startswith(b"<html"):
            return False, "html"
        tmp.write_bytes(data)
        tmp.replace(dest)
        return True, "ok"
    except Exception as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        return False, str(exc)


def download_urls(urls: list[str], cache_dir: Path, workers: int = 8) -> dict[str, Path]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    found: dict[str, Path] = {}
    pending = []
    for url in urls:
        dest = cache_path(cache_dir, url)
        if dest.is_file() and dest.stat().st_size > 32:
            found[url] = dest
        else:
            pending.append((url, dest))
    if not pending:
        return found
    print("Downloaden: %s foto's (%s al in cache)" % (len(pending), len(found)), file=sys.stderr)
    ok = 0
    fail = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(download_one, url, dest): (url, dest) for url, dest in pending}
        for fut in as_completed(futs):
            url, dest = futs[fut]
            success, msg = fut.result()
            if success:
                found[url] = dest
                ok += 1
            else:
                fail += 1
                print("  mislukt: %s (%s)" % (url, msg), file=sys.stderr)
    print("Download klaar: %s nieuw, %s mislukt, %s totaal in cache" % (ok, fail, len(found)), file=sys.stderr)
    return found


def _open_rgb_rgba(path: Path) -> Image.Image:
    with Image.open(path) as im:
        im = im.convert("RGBA")
        # Bake existing opacity but keep alpha; don't fill with white.
        return im.copy()


def _resize_h(im: Image.Image, height: int) -> Image.Image:
    if im.height <= 0:
        return im
    if im.height == height:
        return im
    width = max(1, round(im.width * (height / im.height)))
    return im.resize((width, height), Image.Resampling.LANCZOS)


def border_is_uniform(arr: np.ndarray, tol: float = BORDER_TOL, frac: float = BORDER_FRAC) -> tuple[bool, np.ndarray]:
    h, w = arr.shape[:2]
    if h < 4 or w < 4:
        return False, np.array([255, 255, 255], dtype=np.float64)
    rgb = arr[:, :, :3].astype(np.float64)
    alpha = arr[:, :, 3]
    # Prefer opaque border pixels when the image already has alpha.
    def strip(pixels, alphas):
        if np.any(alphas >= 128):
            return pixels[alphas >= 128]
        return pixels

    parts = [
        strip(rgb[0, :, :], alpha[0, :]),
        strip(rgb[-1, :, :], alpha[-1, :]),
        strip(rgb[:, 0, :], alpha[:, 0]),
        strip(rgb[:, -1, :], alpha[:, -1]),
    ]
    border = np.concatenate(parts, axis=0) if all(len(p) for p in parts) else np.concatenate(
        [rgb[0, :, :], rgb[-1, :, :], rgb[:, 0, :], rgb[:, -1, :]], axis=0
    )
    if border.size == 0:
        return False, np.array([255, 255, 255], dtype=np.float64)
    med = np.median(border, axis=0)
    dist = np.linalg.norm(border - med, axis=1)
    close = float((dist <= tol).mean())
    std = float(border.std(axis=0).mean())
    # Near-uniform studio/white/colour backdrop. A slightly lower
    # match is OK when the border itself has very low variance
    # (soft grey seamless paper that the bottle's shadow touches).
    uniform = (close >= frac and std <= 28) or (close >= 0.72 and std <= 14)
    return uniform, med


def flood_background(arr: np.ndarray, bg: np.ndarray, tol: float = FLOOD_TOL) -> np.ndarray:
    """Return True for background pixels connected to the border."""
    h, w = arr.shape[:2]
    rgb = arr[:, :, :3].astype(np.float64)
    alpha = arr[:, :, 3]
    near = np.linalg.norm(rgb - bg.reshape(1, 1, 3), axis=2) <= tol
    # Treat already-transparent pixels as background seeds.
    near = near | (alpha < 16)
    mask = np.zeros((h, w), dtype=bool)
    # Scan-line flood fill from all border seeds (faster than a huge deque).
    seeds = []
    for x in range(w):
        if near[0, x]:
            seeds.append((0, x))
        if near[h - 1, x]:
            seeds.append((h - 1, x))
    for y in range(h):
        if near[y, 0]:
            seeds.append((y, 0))
        if near[y, w - 1]:
            seeds.append((y, w - 1))
    stack = seeds
    while stack:
        y, x = stack.pop()
        if y < 0 or y >= h or x < 0 or x >= w or mask[y, x] or not near[y, x]:
            continue
        # Walk left/right, queue up/down — classic scan fill.
        left = x
        while left > 0 and near[y, left - 1] and not mask[y, left - 1]:
            left -= 1
        right = x
        while right + 1 < w and near[y, right + 1] and not mask[y, right + 1]:
            right += 1
        mask[y, left : right + 1] = True
        for nx in range(left, right + 1):
            if y > 0 and near[y - 1, nx] and not mask[y - 1, nx]:
                stack.append((y - 1, nx))
            if y + 1 < h and near[y + 1, nx] and not mask[y + 1, nx]:
                stack.append((y + 1, nx))
    return mask


def apply_alpha(arr: np.ndarray, bg_mask: np.ndarray, feather: float = FEATHER) -> np.ndarray:
    alpha = arr[:, :, 3].astype(np.float32)
    # Contract the subject 1px to shave colour fringe, then feather.
    inset = bg_mask.copy()
    if inset.any() and (not inset.all()):
        padded = np.pad(inset, 1, mode="edge")
        dil = (
            padded[:-2, 1:-1]
            | padded[2:, 1:-1]
            | padded[1:-1, :-2]
            | padded[1:-1, 2:]
            | padded[:-2, :-2]
            | padded[:-2, 2:]
            | padded[2:, :-2]
            | padded[2:, 2:]
            | inset
        )
        inset = dil
    alpha[inset] = 0
    out = arr.copy()
    out[:, :, 3] = np.clip(alpha, 0, 255).astype(np.uint8)
    im = Image.fromarray(out, "RGBA")
    if feather and feather > 0:
        band = im.split()[3].filter(ImageFilter.GaussianBlur(radius=feather))
        im.putalpha(band)
    return np.array(im)


def process_image(path: Path) -> tuple[Image.Image, str]:
    """Return (thumb_rgba, kind) where kind is 'clear' or 'photo'."""
    im = _open_rgb_rgba(path)
    # Work at an intermediate size for a stable fill, then down to the thumb.
    if im.height > WORK_HEIGHT:
        work = _resize_h(im, WORK_HEIGHT)
    elif im.height < 32:
        work = im
    else:
        work = im
    arr = np.array(work)
    uniform, bg = border_is_uniform(arr)
    kind = "photo"
    if uniform:
        mask = flood_background(arr, bg)
        frac = float(mask.mean()) if mask.size else 0
        # Keep the photo when the fill ate the subject or barely found a backdrop.
        if 0.08 <= frac <= 0.92:
            arr = apply_alpha(arr, mask)
            kind = "clear"
            work = Image.fromarray(arr, "RGBA")
    thumb = _resize_h(work, THUMB_HEIGHT)
    return thumb, kind


def save_webp(im: Image.Image, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.webp")
    im.save(
        tmp,
        format="WEBP",
        quality=WEBP_QUALITY,
        method=6,
        exact=True,
    )
    tmp.replace(dest)


def build_thumbs(
    items: list[dict],
    thumbs_dir: Path,
    cache_dir: Path = DEFAULT_CACHE,
    report_path: Path | None = None,
) -> dict:
    urls = []
    seen = set()
    for item in items:
        url = item.get("afbeelding") or ""
        if isinstance(url, str) and url.startswith("https://") and url not in seen:
            seen.add(url)
            urls.append(url)
    cached = download_urls(urls, cache_dir)
    stats = {
        "sourceImages": len(urls),
        "downloaded": len(cached),
        "clear": 0,
        "photo": 0,
        "failed": 0,
        "items": {},
        "bytes": 0,
        "count": 0,
    }
    for item in items:
        url = item.get("afbeelding") or ""
        if not (isinstance(url, str) and url.startswith("https://")):
            continue
        src = cached.get(url)
        if not src:
            stats["failed"] += 1
            continue
        dest = thumbs_dir / ("%s.webp" % item["id"])
        try:
            thumb, kind = process_image(src)
            save_webp(thumb, dest)
            size = dest.stat().st_size
            rec = {
                "kind": kind,
                "w": thumb.width,
                "h": thumb.height,
                "bytes": size,
                "path": "assets/thumbs/%s.webp" % item["id"],
            }
            stats["items"][item["id"]] = rec
            stats[kind] += 1
            stats["bytes"] += size
            stats["count"] += 1
        except Exception as exc:
            stats["failed"] += 1
            print("  thumb fout %s: %s" % (item["id"], exc), file=sys.stderr)
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "Thumbs: %s bestanden, %s transparant, %s met achtergrond, %s mislukt, %.1f KB"
        % (stats["count"], stats["clear"], stats["photo"], stats["failed"], stats["bytes"] / 1024),
        file=sys.stderr,
    )
    return stats


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("data", type=Path, help="data.json met items[].afbeelding")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    payload = json.loads(args.data.read_text(encoding="utf-8"))
    items = payload["items"] if isinstance(payload, dict) else payload
    build_thumbs(items, args.out, args.cache, args.report)
