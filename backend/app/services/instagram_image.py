"""
Image Instagram 1080×1080 pour un value bet — vibrant IG, logos des clubs.
Gradient diagonal violet→rose vif, logos club proéminents, badge cote circulaire.
"""
from __future__ import annotations
import os
import random
import uuid
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ─── Palette ──────────────────────────────────────────────────────────
BG_TL        = (36, 0, 70)
BG_BR        = (255, 20, 130)
WHITE        = (255, 255, 255)
COTE_INK     = (74, 0, 110)
ACCENT_GOLD  = (255, 215, 0)
TEXT_DIM     = (235, 220, 245)

SIZE = (1080, 1080)
STATIC_DIR = Path(__file__).parent.parent / "static" / "instagram"
LOGO_CACHE_DIR = Path(__file__).parent.parent / "static" / "logos"

OUTCOME_LABELS = {
    "HOME":    "Victoire {home}",
    "DRAW":    "Match nul",
    "AWAY":    "Victoire {away}",
    "OVER":    "Plus de 2.5 buts",
    "UNDER":   "Moins de 2.5 buts",
    "AH_HOME": "{home} avec handicap",
    "AH_AWAY": "{away} avec handicap",
}

# ─── Mapping clubs → ID football-data.org pour les crests ───
# CDN stable : https://crests.football-data.org/{id}.png
KNOWN_CRESTS = {
    # Premier League
    "arsenal": 57, "arsenal fc": 57,
    "manchester city": 65, "manchester city fc": 65, "man city": 65,
    "manchester united": 66, "manchester united fc": 66, "man united": 66, "manchester utd": 66,
    "liverpool": 64, "liverpool fc": 64,
    "chelsea": 61, "chelsea fc": 61,
    "tottenham": 73, "tottenham hotspur": 73, "tottenham hotspur fc": 73, "spurs": 73,
    "newcastle": 67, "newcastle united": 67,
    "aston villa": 58, "aston villa fc": 58,
    "west ham": 563, "west ham united": 563,
    # La Liga
    "real madrid": 86, "real madrid cf": 86,
    "barcelona": 81, "fc barcelona": 81, "fc barcelone": 81,
    "atletico madrid": 78, "atlético madrid": 78, "atlético de madrid": 78,
    "atletico de madrid": 78, "club atlético de madrid": 78,
    "sevilla": 559, "sevilla fc": 559,
    "real betis": 90, "real betis balompié": 90,
    "valencia": 95, "valencia cf": 95,
    "real sociedad": 92,
    "villarreal": 94, "villarreal cf": 94,
    "athletic club": 77, "athletic bilbao": 77,
    # Bundesliga
    "bayern munich": 5, "bayern": 5, "bayern münchen": 5, "fc bayern münchen": 5, "fc bayern": 5,
    "borussia dortmund": 4, "bvb": 4, "dortmund": 4,
    "rb leipzig": 721, "leipzig": 721,
    "bayer leverkusen": 3, "leverkusen": 3,
    "eintracht frankfurt": 19,
    "stuttgart": 10, "vfb stuttgart": 10,
    # Serie A
    "juventus": 109, "juventus fc": 109, "juve": 109,
    "ac milan": 98, "milan": 98,
    "inter": 108, "inter milan": 108, "internazionale": 108,
    "napoli": 113, "ssc napoli": 113,
    "roma": 100, "as roma": 100,
    "lazio": 110, "ss lazio": 110,
    "atalanta": 102, "atalanta bc": 102,
    "fiorentina": 99, "acf fiorentina": 99,
    # Ligue 1
    "paris saint germain": 524, "paris saint-germain": 524,
    "paris saint germain fc": 524, "paris saint-germain fc": 524, "psg": 524,
    "olympique marseille": 516, "marseille": 516, "om": 516, "olympique de marseille": 516,
    "olympique lyonnais": 523, "lyon": 523, "ol": 523,
    "as monaco": 548, "monaco": 548, "as monaco fc": 548,
    "lille": 521, "losc lille": 521, "lille osc": 521,
    "rennes": 7819, "stade rennais": 7819, "stade rennais fc 1901": 7819,
    "nice": 522, "ogc nice": 522,
}

CREST_URL = "https://crests.football-data.org/{id}.png"

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


# ─── Logos clubs (cache local, fetch football-data.org) ──────────────

def _find_team_id(team_name: str) -> int | None:
    name = team_name.strip().lower()
    # Exact match
    if name in KNOWN_CRESTS:
        return KNOWN_CRESTS[name]
    # Match partiel (alias inclus dans nom long ou inverse)
    for k, v in KNOWN_CRESTS.items():
        if k in name or name in k:
            return v
    return None


def _get_team_logo(team_name: str, max_size: int = 220) -> Image.Image | None:
    """Renvoie le logo redimensionné en RGBA, ou None si introuvable."""
    fd_id = _find_team_id(team_name)
    if fd_id is None:
        return None
    LOGO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = LOGO_CACHE_DIR / f"{fd_id}.png"
    if not cache.exists():
        try:
            import httpx  # lazy : disponible dans le conteneur backend, optionnel en local
            r = httpx.get(CREST_URL.format(id=fd_id), timeout=10, follow_redirects=True)
            if r.status_code != 200 or len(r.content) < 100:
                return None
            cache.write_bytes(r.content)
        except Exception:
            return None
    try:
        logo = Image.open(cache).convert("RGBA")
        logo.thumbnail((max_size, max_size), Image.LANCZOS)
        return logo
    except Exception:
        return None


def _team_initials_badge(team_name: str, size: int = 200) -> Image.Image:
    """Fallback : cercle blanc avec les initiales de l'équipe."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([0, 0, size, size], fill=(255, 255, 255, 230))
    words = team_name.split()
    initials = "".join(w[0].upper() for w in words[:2]) if words else "?"
    f = _font(int(size * 0.4), bold=True)
    tw = _text_w(d, initials, f)
    d.text(((size - tw) // 2, size // 2 - int(size * 0.28)),
           initials, fill=COTE_INK, font=f)
    return img


def _wrap_team_name(name: str, max_chars: int = 13) -> list[str]:
    if len(name) <= max_chars:
        return [name]
    words = name.split()
    if len(words) <= 1:
        return [name]
    mid = len(words) // 2
    return [" ".join(words[:mid]), " ".join(words[mid:])]


# ─── Background gradient diagonal ────────────────────────────────────

def _diagonal_gradient(size: tuple[int, int], c1: tuple, c2: tuple) -> Image.Image:
    w, h = size
    diag = int(((w * w + h * h) ** 0.5)) + 20
    strip = Image.new("RGB", (1, diag))
    for y in range(diag):
        t = y / max(1, diag - 1)
        strip.putpixel((0, y),
                       (int(c1[0] * (1 - t) + c2[0] * t),
                        int(c1[1] * (1 - t) + c2[1] * t),
                        int(c1[2] * (1 - t) + c2[2] * t)))
    big = strip.resize((diag, diag))
    rotated = big.rotate(-45, resample=Image.BILINEAR, expand=False)
    left = (rotated.width - w) // 2
    top = (rotated.height - h) // 2
    return rotated.crop((left, top, left + w, top + h))


def _dots_overlay(size, count=80, alpha=22) -> Image.Image:
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    rng = random.Random(42)
    for _ in range(count):
        x = rng.randint(0, size[0]); y = rng.randint(0, size[1])
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


def _circle_badge(img: Image.Image, cx: int, cy: int, r: int,
                  text: str, font, text_color=COTE_INK):
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([cx - r - 35, cy - r - 35, cx + r + 35, cy + r + 35],
                                  fill=(255, 255, 255, 100))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(radius=22)))
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse([cx - r + 8, cy - r + 14, cx + r + 8, cy + r + 14],
                                    fill=(0, 0, 0, 140))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(radius=14)))
    badge = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(badge).ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 255))
    img.alpha_composite(badge)
    d = ImageDraw.Draw(img)
    tw = _text_w(d, text, font)
    d.text((cx - tw // 2, cy - font.size // 2 - 12), text, fill=text_color, font=font)


def _arrow_chevron(draw, x, y, size=50, color=WHITE):
    s = size
    spacing = s // 3
    for i in range(2):
        ox = x + i * (s // 2 + spacing)
        draw.polygon([(ox, y), (ox + s // 2, y + s // 2), (ox, y + s)], fill=color)


# ─── Génération principale ──────────────────────────────────────────

def generate_value_bet_image(bet: dict) -> Path:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)

    # 1) Fond gradient diagonal + texture
    bg = _diagonal_gradient(SIZE, BG_TL, BG_BR)
    img = bg.convert("RGBA")
    img.alpha_composite(_dots_overlay(SIZE, count=80, alpha=22))

    # 2) Voiles top/bottom pour lisibilité
    veil = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    vd = ImageDraw.Draw(veil)
    vd.rectangle([0, 0, SIZE[0], 110], fill=(0, 0, 0, 80))
    vd.rectangle([0, SIZE[1] - 70, SIZE[0], SIZE[1]], fill=(0, 0, 0, 90))
    img.alpha_composite(veil)
    d = ImageDraw.Draw(img)

    # ─── Polices XXL calibrées pour ne pas déborder en 1080px ───
    f_brand    = _font(50, bold=True)
    f_league   = _font(40, bold=True)
    f_date     = _font(34, bold=False)
    f_team     = _font(68, bold=True)    # single line (ARSENAL)
    f_team_sm  = _font(50, bold=True)    # 2 lignes (PARIS\nSAINT GERMAIN)
    f_vs       = _font(48, bold=True)
    f_pick     = _font(60, bold=True)
    f_cote     = _font(150, bold=True)
    f_ex_big   = _font(80, bold=True)
    f_footer   = _font(28, bold=False)

    cxc = SIZE[0] // 2

    # ─── 1. Brand top compact ───
    _center(d, "@edgebetfr", 22, f_brand, WHITE)

    # ─── 2. Pill compétition (+ date juste en dessous) ───
    league_raw = (bet.get("league") or "").upper()
    tw_league = _text_w(d, league_raw, f_league) + 70
    px_league = (SIZE[0] - tw_league) // 2
    py_league = 110
    d.rounded_rectangle([px_league, py_league, px_league + tw_league, py_league + 70],
                        radius=35, fill=(0, 0, 0, 130))
    _center(d, league_raw, py_league + 14, f_league, WHITE)
    _center(d, _format_date(bet.get("match_date", "")), 205, f_date, TEXT_DIM)

    # ─── 3. Logos + équipes côte à côte ───
    home_raw = bet.get("home_team") or ""
    away_raw = bet.get("away_team") or ""
    logo_size = 200
    home_logo = _get_team_logo(home_raw, max_size=logo_size) or _team_initials_badge(home_raw, logo_size)
    away_logo = _get_team_logo(away_raw, max_size=logo_size) or _team_initials_badge(away_raw, logo_size)

    left_cx = 215
    right_cx = SIZE[0] - 215
    logo_y = 270

    img.alpha_composite(home_logo, (left_cx - home_logo.width // 2, logo_y))
    img.alpha_composite(away_logo, (right_cx - away_logo.width // 2, logo_y))
    d = ImageDraw.Draw(img)

    # Noms d'équipes sous logos (gros)
    name_y = logo_y + logo_size + 18
    home_lines = _wrap_team_name(home_raw.upper(), 11)
    away_lines = _wrap_team_name(away_raw.upper(), 11)
    f_h = f_team_sm if len(home_lines) > 1 else f_team
    f_a = f_team_sm if len(away_lines) > 1 else f_team
    line_step = 56
    for i, line in enumerate(home_lines):
        w = _text_w(d, line, f_h)
        d.text((left_cx - w // 2, name_y + i * line_step), line, fill=WHITE, font=f_h)
    for i, line in enumerate(away_lines):
        w = _text_w(d, line, f_a)
        d.text((right_cx - w // 2, name_y + i * line_step), line, fill=WHITE, font=f_a)

    # "VS" doré au niveau des logos
    vs_y = logo_y + logo_size // 2
    vs_r = 64
    vs_layer = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    ImageDraw.Draw(vs_layer).ellipse(
        [cxc - vs_r, vs_y - vs_r, cxc + vs_r, vs_y + vs_r], fill=(255, 215, 0, 235))
    img.alpha_composite(vs_layer)
    d = ImageDraw.Draw(img)
    tw_vs = _text_w(d, "VS", f_vs)
    d.text((cxc - tw_vs // 2, vs_y - f_vs.size // 2 - 8), "VS",
           fill=(60, 0, 90), font=f_vs)

    # ─── 4. Pick text (très gros, pas de label) ───
    pick_template = OUTCOME_LABELS.get(bet.get("outcome", ""), "—")
    pick_text = pick_template.format(
        home=home_raw.title(), away=away_raw.title(),
    )
    pick_y = 605
    _center(d, pick_text, pick_y, f_pick, WHITE)

    # ─── 5. Cote en CAPSULE blanche horizontale (énorme) ───
    odds = float(bet.get("odds") or 0)
    cote_str = f"x{odds:.2f}".replace(".", ",")
    # Calcule largeur capsule selon le texte cote
    cote_w = _text_w(d, cote_str, f_cote) + 120
    cote_h = 200
    cap_y = 710
    cap_x = (SIZE[0] - cote_w) // 2
    # Ombre + lueur
    shadow = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [cap_x + 8, cap_y + 14, cap_x + cote_w + 8, cap_y + cote_h + 14],
        radius=cote_h // 2, fill=(0, 0, 0, 140))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(radius=16)))
    glow = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    ImageDraw.Draw(glow).rounded_rectangle(
        [cap_x - 25, cap_y - 25, cap_x + cote_w + 25, cap_y + cote_h + 25],
        radius=cote_h // 2, fill=(255, 255, 255, 90))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(radius=22)))
    # Capsule blanche
    cap = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    ImageDraw.Draw(cap).rounded_rectangle(
        [cap_x, cap_y, cap_x + cote_w, cap_y + cote_h],
        radius=cote_h // 2, fill=(255, 255, 255, 255))
    img.alpha_composite(cap)
    d = ImageDraw.Draw(img)
    # Cote text huge, centré dans la capsule
    tw_c = _text_w(d, cote_str, f_cote)
    d.text((cxc - tw_c // 2, cap_y + cote_h // 2 - f_cote.size // 2 - 18),
           cote_str, fill=COTE_INK, font=f_cote)

    # ─── 6. Exemple concret : MISE / chevrons / GAIN (sous la capsule) ───
    mise = 10.0
    encaisse = mise * odds
    mise_str = _format_eur(mise)
    enc_str = _format_eur(encaisse)

    ex_y = cap_y + cote_h + 30  # sous la capsule, plus serré
    # Une ligne : "10€  ▶  24,50€"
    w_m = _text_w(d, mise_str, f_ex_big)
    w_e = _text_w(d, enc_str, f_ex_big)
    arrow_w = 60
    gap = 50
    total_w = w_m + gap + arrow_w + gap + w_e
    start_x = (SIZE[0] - total_w) // 2

    d.text((start_x, ex_y), mise_str, fill=WHITE, font=f_ex_big)
    # Chevrons dorés
    arrow_x = start_x + w_m + gap
    s = 60
    sp = s // 3
    for i in range(2):
        ox = arrow_x + i * (s // 2 + sp)
        d.polygon([(ox, ex_y + 20), (ox + s // 2, ex_y + 20 + s // 2),
                   (ox, ex_y + 20 + s)], fill=ACCENT_GOLD)
    d.text((start_x + w_m + gap + arrow_w + gap, ex_y), enc_str,
           fill=ACCENT_GOLD, font=f_ex_big)

    # ─── 7. Footer ───
    _center(d, "Jeu responsable · 18+ · Joue ce que tu peux perdre",
            1050, f_footer, TEXT_DIM)

    # ─── Save ───
    final = img.convert("RGB")
    filepath = STATIC_DIR / f"vb_{uuid.uuid4().hex[:12]}.jpg"
    final.save(filepath, "JPEG", quality=92, optimize=True, progressive=False)
    return filepath
