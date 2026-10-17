"""Bank status bulletin PNG (FR-8) via Pillow."""
from __future__ import annotations
import time
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

W, H = 900, 500
_COL = {"degraded": (217, 119, 6), "outage": (200, 30, 30), "operational": (22, 140, 70)}


def _font(size: int, bold: bool = False):
    names = (["arialbd.ttf", "DejaVuSans-Bold.ttf"] if bold else ["arial.ttf", "DejaVuSans.ttf"])
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _hms(ts: int) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(int(ts)))


def render_bulletin(bank: str, headline: str, status: str, claimed_start_ts: int,
                    claimed_end_ts: int, out_path) -> str:
    img = Image.new("RGB", (W, H), (248, 249, 251))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 90], fill=(20, 33, 61))
    d.text((40, 24), f"{bank} - Service Status Bulletin", font=_font(34, True), fill="white")
    col = _COL.get(status.lower(), (90, 90, 90))
    d.rounded_rectangle([40, 120, 40 + 30 + 17 * len(status), 170], radius=10, fill=col)
    d.text((55, 128), status.upper(), font=_font(30, True), fill="white")
    d.text((40, 200), headline, font=_font(30, True), fill=(30, 30, 30))
    d.text((40, 260), "Affected services: UPI, IMPS, Net Banking", font=_font(26), fill=(60, 60, 60))
    d.text((40, 320), f"Claimed window (UTC): {_hms(claimed_start_ts)} - {_hms(claimed_end_ts)}",
           font=_font(28, True), fill=(30, 30, 30))
    d.text((40, 440), "Issued by bank operations. Times are indicative.", font=_font(20), fill=(120, 120, 120))
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    return str(out)


def extract_fields_from_meta(meta: dict) -> dict:
    """Contract bulletin.extracted shape from the bulletin's generation metadata."""
    return {
        "bank": meta["bank"],
        "status": meta["status"],
        "claimed_start_ts": int(meta["claimed_start_ts"]),
        "claimed_end_ts": int(meta["claimed_end_ts"]),
    }
