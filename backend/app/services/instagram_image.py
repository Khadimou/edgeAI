"""
Génère l'image d'un value bet pour Instagram (1080x1080).
Design grand public : zéro jargon, focal point sur le pari + gain potentiel.
"""
from __future__ import annotations
import os
import uuid
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

# ─── Palette ──────────────────────────────────────────────────────────
BG_TOP       = (15, 12, 41)      # deep purple
BG_BOTTOM    = (8, 8, 20)        # near black
ACCENT       = (0, 230, 118)     # vivid green (money / win)
ACCENT_SOFT  = (0, 180, 95)
WHITE        = (255, 255, 255)
TEXT_PRIMARY = (245, 245, 250)
TEXT_MUTED   = (165, 170, 195)
CARD_BG      = (28, 24, 60)
DIVIDER      = (60, 56, 100)
RED          = (255, 95, 100)

SIZE = (1080, 1080)
STATIC_DIR = Path(__file__).parent.parent / "static" / "instagram"

OUTCOME_LABELS = {
    "HOME":    "Victoire {home}",
    "DRAW":    "Match nul",
    "AWAY":    "Victoire {away}",
    "OVER":    "Plus de 2.5 buts",
    "UNDER":   "Moins de 2.5 buts",
    "AH_HOME": "{home} avec handicap",
    "AH_AWAY": "{away} avec handicap",
}

_FONT_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]
_FONT_REGULAR_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "C:/Windows/Fonts/arial.ttf",
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


def _text_w(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _center(draw: ImageDraw.ImageDraw, text: str, y: int,
            font, color=TEXT_PRIMARY, width: int = SIZE[0]) -> None:
    x = (width - _text_w(draw, text, font)) // 2
    draw.text((x, y), text, fill=color, font=font)


def _wrap_team_name(name: str, max_chars: int = 18) -> str:
    """Coupe les noms trop longs (ex 'Paris Saint Germain') sur 2 lignes."""
    if len(name) <= max_chars:
        return name
    words = name.split()
    if len(words) <= 1:
        return name
    mid = len(words) // 2
    return " ".join(words[:mid]) + "\n" + " ".join(words[mid:])


def _gradient_bg(img: Image.Image) -> None:
    """Dégradé vertical top→bottom (deep purple → near black)."""
    h = img.height
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(BG_TOP[0] * (1 - t) + BG_BOTTOM[0] * t)
        g = int(BG_TOP[1] * (1 - t) + BG_BOTTOM[1] * t)
        b = int(BG_TOP[2] * (1 - t) + BG_BOTTOM[2] * t)
        for x in range(img.width):
            px[x, y] = (r, g, b)


def _format_eur(v: float) -> str:
    """Format français : 1.50 → '1,50€'."""
    return f"{v:.2f}".replace(".", ",") + "€"


def _format_date(match_date) -> str:
    try:
        if isinstance(match_date, str):
            dt = datetime.fromisoformat(match_date.replace("Z", "+00:00"))
        else:
            dt = match_date
        months = ["jan", "fév", "mars", "avr", "mai", "juin",
                  "juil", "août", "sept", "oct", "nov", "déc"]
        return f"{dt.day} {months[dt.month-1]} · {dt.hour:02d}h{dt.minute:02d}"
    except Exception:
        return str(match_date)[:16]


def generate_value_bet_image(bet: dict) -> Path:
    """
    Image Instagram 1080x1080 grand public — sans jargon technique.
    Met l'accent sur : match, choix simple, cote, et gain concret pour 10€ misés.
    """
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", SIZE)
    _gradient_bg(img)
    d = ImageDraw.Draw(img)

    # ─── Polices ───
    f_brand   = _font(40, bold=True)
    f_league  = _font(28, bold=False)
    f_date    = _font(28, bold=False)
    f_team    = _font(74, bold=True)
    f_team_sm = _font(58, bold=True)   # quand le nom est long et wrappé
    f_vs      = _font(34, bold=False)
    f_pick_lbl= _font(26, bold=False)
    f_pick    = _font(50, bold=True)
    f_cote_lbl= _font(26, bold=False)
    f_cote    = _font(115, bold=True)
    f_h       = _font(36, bold=True)
    f_ex_big  = _font(64, bold=True)
    f_ex_lbl  = _font(28, bold=False)
    f_footer  = _font(24, bold=False)

    # ─── Top brand bar ───
    d.rectangle([0, 0, SIZE[0], 90], fill=(0, 0, 0, 0))
    _center(d, "@edgebetfr", 24, f_brand, WHITE)
    # Petit dot accent sous le handle
    d.ellipse([SIZE[0] // 2 - 5, 78, SIZE[0] // 2 + 5, 88], fill=ACCENT)

    # ─── Compétition + date ───
    league_raw = (bet.get("league") or "").upper()
    _center(d, league_raw, 120, f_league, TEXT_MUTED)
    _center(d, _format_date(bet.get("match_date", "")), 160, f_date, TEXT_MUTED)

    # ─── Affiche du match (hero) ───
    home = (bet.get("home_team") or "").upper()
    away = (bet.get("away_team") or "").upper()
    home_w = _wrap_team_name(home, 14)
    away_w = _wrap_team_name(away, 14)
    f_home = f_team_sm if "\n" in home_w else f_team
    f_away = f_team_sm if "\n" in away_w else f_team

    # On dessine HOME en haut, "VS" au milieu, AWAY en bas
    y_home_start = 230
    for i, line in enumerate(home_w.split("\n")):
        _center(d, line, y_home_start + i * 70, f_home, WHITE)
    y_vs = y_home_start + (70 * len(home_w.split("\n"))) + 6
    _center(d, "vs", y_vs, f_vs, TEXT_MUTED)
    y_away_start = y_vs + 56
    for i, line in enumerate(away_w.split("\n")):
        _center(d, line, y_away_start + i * 70, f_away, WHITE)

    # ─── Carte du pari (focal) ───
    cx, cy, cw, ch = 60, 540, SIZE[0] - 120, 290
    # Ombre douce
    d.rounded_rectangle([cx + 4, cy + 6, cx + cw + 4, cy + ch + 6],
                        radius=28, fill=(4, 4, 12))
    # Carte
    d.rounded_rectangle([cx, cy, cx + cw, cy + ch],
                        radius=28, fill=CARD_BG, outline=ACCENT, width=3)

    _center(d, "NOTRE PARI", cy + 22, f_pick_lbl, TEXT_MUTED)
    pick_template = OUTCOME_LABELS.get(bet.get("outcome", ""), "—")
    pick_text = pick_template.format(
        home=(bet.get("home_team") or "").title(),
        away=(bet.get("away_team") or "").title(),
    )
    _center(d, pick_text, cy + 60, f_pick, WHITE)

    # Cote géante avec petit "x" devant — calibrée pour rester dans la carte
    odds = float(bet.get("odds") or 0)
    cote_str = f"x{odds:.2f}".replace(".", ",")
    _center(d, "COTE", cy + 132, f_cote_lbl, TEXT_MUTED)
    _center(d, cote_str, cy + 158, f_cote, ACCENT)

    # ─── Exemple concret ───
    ey = 870  # sous la carte (qui finit à 540+290=830)
    mise = 10.0
    gain_net = mise * (odds - 1)
    encaisse = mise * odds

    # Ligne pédagogique : "TU MISES 10€  →  TU ENCAISSES 24,50€"
    left_lbl  = "TU MISES"
    right_lbl = "TU ENCAISSES"
    mid_arrow = "→"

    _center(d, left_lbl, ey, f_ex_lbl, TEXT_MUTED, width=SIZE[0] // 2)
    _center(d, _format_eur(mise), ey + 30, f_ex_big, WHITE, width=SIZE[0] // 2)
    arrow_x = SIZE[0] // 2 - _text_w(d, mid_arrow, f_ex_big) // 2
    d.text((arrow_x, ey + 30), mid_arrow, fill=ACCENT, font=f_ex_big)
    right_x_offset = SIZE[0] // 2
    bbox_r = d.textbbox((0, 0), right_lbl, font=f_ex_lbl)
    d.text((right_x_offset + (SIZE[0] // 2 - (bbox_r[2] - bbox_r[0])) // 2, ey),
           right_lbl, fill=TEXT_MUTED, font=f_ex_lbl)
    bbox_v = d.textbbox((0, 0), _format_eur(encaisse), font=f_ex_big)
    d.text((right_x_offset + (SIZE[0] // 2 - (bbox_v[2] - bbox_v[0])) // 2, ey + 30),
           _format_eur(encaisse), fill=ACCENT, font=f_ex_big)

    # Sous-ligne : "soit +X€ de gain" — au-dessus du footer pour ne pas être barrée
    _center(d, f"soit +{_format_eur(gain_net)} de gain si ça passe",
            ey + 105, f_ex_lbl, TEXT_MUTED)

    # ─── Footer ───
    fy = 1030  # poussé en bas pour laisser de l'air sous la sous-ligne
    _center(d, "Jeu responsable · 18+ · Mise ce que tu peux perdre",
            fy, f_footer, TEXT_MUTED)

    # ─── Save ───
    filepath = STATIC_DIR / f"vb_{uuid.uuid4().hex[:12]}.jpg"
    img.save(filepath, "JPEG", quality=92, optimize=True, progressive=False)
    return filepath
