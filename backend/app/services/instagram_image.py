"""
Génère l'image d'un value bet pour Instagram (1080x1080).
"""
from __future__ import annotations
import os
import uuid
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

BG_COLOR     = (13, 17, 35)
ACCENT       = (0, 210, 110)
TEXT_PRIMARY = (240, 240, 255)
TEXT_MUTED   = (140, 150, 175)
CARD_BG      = (20, 28, 52)
CARD_BORDER  = (0, 180, 100)

SIZE = (1080, 1080)
STATIC_DIR = Path(__file__).parent.parent / "static" / "instagram"

OUTCOME_LABELS = {
    "HOME":    "VICTOIRE DOMICILE",
    "DRAW":    "MATCH NUL",
    "AWAY":    "VICTOIRE EXTÉRIEUR",
    "OVER":    "PLUS DE 2.5 BUTS",
    "UNDER":   "MOINS DE 2.5 BUTS",
    "AH_HOME": "ASIAN HANDICAP DOM.",
    "AH_AWAY": "ASIAN HANDICAP EXT.",
}

_FONT_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/calibrib.ttf",
]
_FONT_REGULAR_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/calibri.ttf",
]


def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = _FONT_BOLD_CANDIDATES if bold else _FONT_REGULAR_CANDIDATES
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _center(draw: ImageDraw.ImageDraw, text: str, y: int,
            font, color=TEXT_PRIMARY, width: int = SIZE[0]) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    x = (width - (bbox[2] - bbox[0])) // 2
    draw.text((x, y), text, fill=color, font=font)


def _prob_for_outcome(bet: dict) -> float:
    outcome = bet.get("outcome", "")
    mapping = {"HOME": "prob_home", "DRAW": "prob_draw", "AWAY": "prob_away",
               "OVER": "prob_over", "UNDER": "prob_under",
               "AH_HOME": "prob_ah_home", "AH_AWAY": "prob_ah_away"}
    key = mapping.get(outcome)
    if key and bet.get(key) is not None:
        return float(bet[key])
    edge = bet.get("edge", 0) or 0
    odds = bet.get("odds", 1) or 1
    return (1 + edge) / odds


def generate_value_bet_image(bet: dict) -> Path:
    """
    Génère une image JPEG 1080×1080 pour un value bet.
    (Instagram Graph API n'accepte QUE le JPEG pour les posts feed — PNG = 400.)
    Retourne le chemin du fichier créé.

    Champs attendus dans bet:
      home_team, away_team, league, match_date, outcome,
      odds, edge, kelly_stake, recommended_amount,
      prob_home / prob_draw / prob_away (optionnel)
    """
    STATIC_DIR.mkdir(parents=True, exist_ok=True)

    img = Image.new("RGB", SIZE, color=BG_COLOR)
    d = ImageDraw.Draw(img)

    f72 = _font(72)
    f54 = _font(54)
    f44 = _font(44)
    f36 = _font(36)
    f28 = _font(28, bold=False)
    f24 = _font(24, bold=False)
    f48 = _font(48)

    # ── Header ──────────────────────────────────────────────────────
    d.rectangle([0, 0, SIZE[0], 105], fill=(8, 12, 25))
    _center(d, "EDGEBET", 22, f48, ACCENT)

    match_date = bet.get("match_date", "")
    try:
        if isinstance(match_date, str):
            dt = datetime.fromisoformat(match_date.replace("Z", "+00:00"))
        else:
            dt = match_date
        date_str = dt.strftime("%d/%m/%Y  %H:%M")
    except Exception:
        date_str = str(match_date)[:16]
    d.text((SIZE[0] - 300, 38), date_str, fill=TEXT_MUTED, font=f24)

    # ── VALUE BET title ─────────────────────────────────────────────
    _center(d, "● VALUE BET ●", 130, f44, ACCENT)
    d.rectangle([360, 188, 720, 191], fill=ACCENT)

    # ── League ──────────────────────────────────────────────────────
    _center(d, (bet.get("league") or "").upper(), 208, f24, TEXT_MUTED)

    # ── Teams ───────────────────────────────────────────────────────
    home = (bet.get("home_team") or "").upper()
    away = (bet.get("away_team") or "").upper()
    _center(d, home, 260, f54)
    _center(d, "vs", 338, f28, TEXT_MUTED)
    _center(d, away, 385, f54)

    # ── Separator ───────────────────────────────────────────────────
    d.rectangle([80, 464, SIZE[0] - 80, 466], fill=(35, 45, 75))

    # ── Card ────────────────────────────────────────────────────────
    cx, cy, cw, ch = 70, 490, SIZE[0] - 140, 395
    d.rounded_rectangle([cx + 5, cy + 5, cx + cw + 5, cy + ch + 5],
                        radius=22, fill=(5, 8, 18))
    d.rounded_rectangle([cx, cy, cx + cw, cy + ch],
                        radius=22, fill=CARD_BG, outline=CARD_BORDER, width=3)

    # Outcome
    outcome_label = OUTCOME_LABELS.get(bet.get("outcome", ""), bet.get("outcome", "—"))
    _center(d, outcome_label, cy + 20, f36)

    d.rectangle([cx + 50, cy + 78, cx + cw - 50, cy + 80], fill=(35, 45, 75))

    # Metrics grid
    odds = float(bet.get("odds") or 0)
    edge = float(bet.get("edge") or 0)
    kelly = float(bet.get("kelly_stake") or 0)
    prob_market = round((1 / odds * 100) if odds > 1 else 0, 1)
    prob_model  = round(_prob_for_outcome(bet) * 100, 1)
    edge_pct    = round(edge * 100, 1)
    units       = round(kelly * 100, 1)

    col_l = cx + 70
    col_r = cx + cw // 2 + 50
    row1_label = cy + 98
    row1_val   = cy + 130
    row2_label = cy + 218
    row2_val   = cy + 250

    # Left column
    d.text((col_l, row1_label), "COTE BOOKMAKER", fill=TEXT_MUTED, font=f24)
    d.text((col_l, row1_val),   f"{odds:.2f}", fill=TEXT_PRIMARY, font=f72)

    d.text((col_l, row2_label), "PROB. MARCHÉ (BRUTE)", fill=TEXT_MUTED, font=f24)
    d.text((col_l, row2_val),   f"{prob_market:.1f}%", fill=TEXT_PRIMARY, font=f44)

    # Right column
    d.text((col_r, row1_label), "EDGE DÉTECTÉ", fill=TEXT_MUTED, font=f24)
    d.text((col_r, row1_val),   f"+{edge_pct:.1f}%", fill=ACCENT, font=f72)

    d.text((col_r, row2_label), "MISE (KELLY)", fill=TEXT_MUTED, font=f24)
    d.text((col_r, row2_val),   f"{units:.1f}u", fill=TEXT_PRIMARY, font=f44)

    # Card footer
    d.rectangle([cx + 50, cy + 330, cx + cw - 50, cy + 332], fill=(35, 45, 75))
    _center(d, f"Probabilité estimée par EdgeBet : {prob_model:.0f}%",
            cy + 347, f24, TEXT_MUTED)

    # ── Footer ──────────────────────────────────────────────────────
    fy = 910
    d.rectangle([0, fy, SIZE[0], SIZE[1]], fill=(8, 12, 25))
    d.rectangle([80, fy, SIZE[0] - 80, fy + 2], fill=(35, 45, 75))
    _center(d, "Investir, pas parier.", fy + 22, f28, TEXT_MUTED)
    _center(d, "@edgebet", fy + 65, f48, ACCENT)

    filepath = STATIC_DIR / f"vb_{uuid.uuid4().hex[:12]}.jpg"
    img.save(filepath, "JPEG", quality=92, optimize=True, progressive=False)
    return filepath
