"""
Image Instagram 1080×1080 pour un value bet — style vibrant IG.
Gradient diagonal violet→rose vif, badge circulaire pour la cote, zéro jargon.
"""
from __future__ import annotations
import os
import random
import uuid
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ─── Palette ──────────────────────────────────────────────────────────
BG_TL        = (36, 0, 70)        # deep violet (top-left)
BG_BR        = (255, 20, 130)     # vivid pink (bottom-right)
WHITE        = (255, 255, 255)
COTE_INK     = (74, 0, 110)       # texte cote sur badge blanc
PICK_INK     = (255, 255, 255)    # blanc pur
ACCENT_GOLD  = (255, 215, 0)
TEXT_SOFT    = (255, 255, 255)
TEXT_DIM     = (235, 220, 245)    # blanc cassé violacé pour secondaires
DIM_OVERLAY  = (0, 0, 0, 90)      # voile noir transparent pour lisibilité

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


def _font(size: int, bold: bool = True):
    candidates = _FONT_BOLD_CANDIDATES if bold else _FONT_REGULAR_CANDIDATES
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _text_w(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _center(draw, text, y, font, color=WHITE, width=SIZE[0]):
    x = (width - _text_w(draw, text, font)) // 2
    draw.text((x, y), text, fill=color, font=font)


def _wrap_team_name(name: str, max_chars: int = 14) -> list[str]:
    """Découpe sur 1 ou 2 lignes pour les noms longs."""
    if len(name) <= max_chars:
        return [name]
    words = name.split()
    if len(words) <= 1:
        return [name]
    mid = len(words) // 2
    return [" ".join(words[:mid]), " ".join(words[mid:])]


# ─── Background gradient diagonal ────────────────────────────────────

def _diagonal_gradient(size: tuple[int, int], c1: tuple, c2: tuple) -> Image.Image:
    """Gradient diagonal (top-left c1 → bottom-right c2) via rotation d'un strip vertical."""
    w, h = size
    # Diagonale + marge
    diag = int(((w * w + h * h) ** 0.5)) + 20
    # Strip 1 px de large, hauteur diagonale
    strip = Image.new("RGB", (1, diag))
    for y in range(diag):
        t = y / max(1, diag - 1)
        r = int(c1[0] * (1 - t) + c2[0] * t)
        g = int(c1[1] * (1 - t) + c2[1] * t)
        b = int(c1[2] * (1 - t) + c2[2] * t)
        strip.putpixel((0, y), (r, g, b))
    # Étire en largeur, puis tourne -45° pour diagonal, puis crop au centre
    big = strip.resize((diag, diag))
    rotated = big.rotate(-45, resample=Image.BILINEAR, expand=False)
    left = (rotated.width - w) // 2
    top = (rotated.height - h) // 2
    return rotated.crop((left, top, left + w, top + h))


def _dots_overlay(size: tuple[int, int], count: int = 60, alpha: int = 28) -> Image.Image:
    """Petits points blancs semi-transparents pour texture (random fixed seed)."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    rng = random.Random(42)
    for _ in range(count):
        x = rng.randint(0, size[0])
        y = rng.randint(0, size[1])
        r = rng.choice([3, 4, 5, 6, 8])
        draw.ellipse([x - r, y - r, x + r, y + r],
                     fill=(255, 255, 255, alpha))
    return layer


def _format_eur(v: float) -> str:
    return f"{v:.2f}".replace(".", ",") + "€"


def _format_date(match_date) -> str:
    try:
        if isinstance(match_date, str):
            dt = datetime.fromisoformat(match_date.replace("Z", "+00:00"))
        else:
            dt = match_date
        days = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
        months = ["jan", "fév", "mars", "avr", "mai", "juin",
                  "juil", "août", "sept", "oct", "nov", "déc"]
        return f"{days[dt.weekday()]} {dt.day} {months[dt.month-1]}  ·  {dt.hour:02d}h{dt.minute:02d}"
    except Exception:
        return str(match_date)[:16]


# ─── Composants visuels ──────────────────────────────────────────────

def _pill(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, font,
          fg=WHITE, bg=DIM_OVERLAY, pad_x: int = 28, pad_y: int = 14) -> tuple[int, int]:
    """Pill arrondi avec texte centré. Retourne (largeur, hauteur)."""
    tw = _text_w(draw, text, font)
    th = font.size
    w = tw + 2 * pad_x
    h = th + 2 * pad_y
    draw.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=bg)
    draw.text((x + pad_x, y + pad_y - 3), text, fill=fg, font=font)
    return w, h


def _circle_badge(img: Image.Image, cx: int, cy: int, r: int,
                  text: str, font, text_color=COTE_INK) -> None:
    """Badge circulaire blanc avec ombre/lueur et texte centré."""
    # Lueur extérieure (cercle plus grand, blanc translucide, flou)
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([cx - r - 30, cy - r - 30, cx + r + 30, cy + r + 30],
               fill=(255, 255, 255, 90))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=20))
    img.alpha_composite(glow)

    # Ombre portée (cercle noir flou décalé)
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.ellipse([cx - r + 8, cy - r + 14, cx + r + 8, cy + r + 14],
               fill=(0, 0, 0, 130))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=12))
    img.alpha_composite(shadow)

    # Badge blanc
    badge = Image.new("RGBA", img.size, (0, 0, 0, 0))
    bd = ImageDraw.Draw(badge)
    bd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 255))
    img.alpha_composite(badge)

    # Texte centré
    final_draw = ImageDraw.Draw(img)
    tw = _text_w(final_draw, text, font)
    th = font.size
    final_draw.text((cx - tw // 2, cy - th // 2 - 8), text,
                    fill=text_color, font=font)


def _arrow_chevron(draw: ImageDraw.ImageDraw, x: int, y: int, size: int = 36,
                   color=WHITE) -> int:
    """Dessine 2 chevrons ▶▶ pointant à droite. Retourne la largeur dessinée."""
    s = size
    spacing = s // 3
    for i in range(2):
        ox = x + i * (s // 2 + spacing)
        poly = [(ox, y), (ox + s // 2, y + s // 2), (ox, y + s)]
        draw.polygon(poly, fill=color)
    return s + spacing + 2


# ─── Génération principale ──────────────────────────────────────────

def generate_value_bet_image(bet: dict) -> Path:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)

    # 1) Fond : gradient diagonal en RGB
    bg = _diagonal_gradient(SIZE, BG_TL, BG_BR)
    # On passe en RGBA pour pouvoir composer des layers
    img = bg.convert("RGBA")

    # 2) Texture : dots semi-transparents
    img.alpha_composite(_dots_overlay(SIZE, count=80, alpha=22))

    # 3) Voile sombre subtil top + bottom pour lisibilité texte
    veil = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    vd = ImageDraw.Draw(veil)
    vd.rectangle([0, 0, SIZE[0], 100], fill=(0, 0, 0, 70))           # bandeau top
    vd.rectangle([0, SIZE[1] - 70, SIZE[0], SIZE[1]], fill=(0, 0, 0, 80))  # bandeau bottom
    img.alpha_composite(veil)

    d = ImageDraw.Draw(img)

    # ─── Polices ───
    f_brand   = _font(40, bold=True)
    f_league  = _font(26, bold=True)
    f_date    = _font(28, bold=False)
    f_team    = _font(70, bold=True)
    f_team_sm = _font(54, bold=True)
    f_vs      = _font(36, bold=True)
    f_pick_lbl= _font(24, bold=False)
    f_pick    = _font(48, bold=True)
    f_cote    = _font(118, bold=True)
    f_ex_big  = _font(64, bold=True)
    f_ex_lbl  = _font(26, bold=False)
    f_sub     = _font(26, bold=False)
    f_footer  = _font(22, bold=False)

    # ─── 1. Brand top ───
    _center(d, "@edgebetfr", 30, f_brand, WHITE)
    # petit underline sous le handle (3 dots horizontaux)
    cxc = SIZE[0] // 2
    for i, dx in enumerate([-22, 0, 22]):
        d.ellipse([cxc + dx - 4, 84, cxc + dx + 4, 92], fill=(255, 255, 255, 200))

    # ─── 2. Pill compétition + date ───
    league_raw = (bet.get("league") or "").upper()
    # Pill league centré
    tw_league = _text_w(d, league_raw, f_league) + 56
    px_league = (SIZE[0] - tw_league) // 2
    py_league = 140
    d.rounded_rectangle([px_league, py_league, px_league + tw_league, py_league + 52],
                        radius=26, fill=(0, 0, 0, 110))
    _center(d, league_raw, py_league + 12, f_league, WHITE)

    _center(d, _format_date(bet.get("match_date", "")), 212, f_date, TEXT_DIM)

    # ─── 3. Match : équipes côte à côte avec VS au centre ───
    home_raw = (bet.get("home_team") or "").upper()
    away_raw = (bet.get("away_team") or "").upper()
    home_lines = _wrap_team_name(home_raw, 12)
    away_lines = _wrap_team_name(away_raw, 12)

    team_y = 290
    line_h = 60
    f_h = f_team_sm if len(home_lines) > 1 else f_team
    f_a = f_team_sm if len(away_lines) > 1 else f_team

    # Zone gauche (centre x ~ 250) et droite (centre x ~ 830)
    left_cx = 250
    right_cx = SIZE[0] - 250

    for i, line in enumerate(home_lines):
        w = _text_w(d, line, f_h)
        d.text((left_cx - w // 2, team_y + i * line_h), line, fill=WHITE, font=f_h)
    for i, line in enumerate(away_lines):
        w = _text_w(d, line, f_a)
        d.text((right_cx - w // 2, team_y + i * line_h), line, fill=WHITE, font=f_a)

    # "VS" central dans un petit cercle
    vs_y = team_y + 18
    vs_r = 42
    vs_layer = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    vsd = ImageDraw.Draw(vs_layer)
    vsd.ellipse([cxc - vs_r, vs_y - vs_r, cxc + vs_r, vs_y + vs_r],
                fill=(255, 215, 0, 230))   # cercle doré
    img.alpha_composite(vs_layer)
    d = ImageDraw.Draw(img)  # recharge le draw après alpha_composite
    tw_vs = _text_w(d, "VS", f_vs)
    d.text((cxc - tw_vs // 2, vs_y - f_vs.size // 2 - 4), "VS",
           fill=(60, 0, 90), font=f_vs)

    # ─── 4. Pick label ───
    pick_template = OUTCOME_LABELS.get(bet.get("outcome", ""), "—")
    pick_text = pick_template.format(
        home=(bet.get("home_team") or "").title(),
        away=(bet.get("away_team") or "").title(),
    )
    pick_y = 470
    _center(d, "NOTRE PARI", pick_y, f_pick_lbl, TEXT_DIM)
    _center(d, pick_text, pick_y + 36, f_pick, WHITE)

    # ─── 5. Badge circulaire pour la COTE (focal point) ───
    odds = float(bet.get("odds") or 0)
    cote_str = f"x{odds:.2f}".replace(".", ",")
    badge_cy = 700
    _circle_badge(img, cxc, badge_cy, r=140, text=cote_str, font=f_cote, text_color=COTE_INK)
    d = ImageDraw.Draw(img)  # refresh draw

    # Mini label sous le badge
    _center(d, "COTE BOOKMAKER", badge_cy + 165, f_pick_lbl, TEXT_DIM)

    # ─── 6. Exemple concret avec chevrons dessinés ───
    mise = 10.0
    gain_net = mise * (odds - 1)
    encaisse = mise * odds

    ey = 920
    # Layout : "10€   ▶▶   24,50€" centré
    mise_str = _format_eur(mise)
    enc_str = _format_eur(encaisse)
    w_mise = _text_w(d, mise_str, f_ex_big)
    w_enc = _text_w(d, enc_str, f_ex_big)
    gap = 80
    arrow_w = 70
    total_w = w_mise + gap + arrow_w + gap + w_enc
    start_x = (SIZE[0] - total_w) // 2

    d.text((start_x, ey), mise_str, fill=WHITE, font=f_ex_big)
    # Chevrons dorés
    arrow_x = start_x + w_mise + gap
    _arrow_chevron(d, arrow_x, ey + 8, size=50, color=ACCENT_GOLD)
    # Encaisse en couleur dorée (gain)
    d.text((start_x + w_mise + gap + arrow_w + gap, ey), enc_str,
           fill=ACCENT_GOLD, font=f_ex_big)

    # Sous-ligne
    _center(d, f"soit +{_format_eur(gain_net)} de gain si ça passe",
            ey + 80, f_sub, TEXT_DIM)

    # ─── 7. Footer ───
    _center(d, "Jeu responsable · 18+ · Mise ce que tu peux perdre",
            1030, f_footer, TEXT_DIM)

    # ─── Save ───
    final = img.convert("RGB")
    filepath = STATIC_DIR / f"vb_{uuid.uuid4().hex[:12]}.jpg"
    final.save(filepath, "JPEG", quality=92, optimize=True, progressive=False)
    return filepath
