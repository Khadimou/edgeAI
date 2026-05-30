"""
Image Instagram 1080×1080 pour un value bet — design « match preview pro » :
fond noir profond, cote ÉNORME en néon doré, bandes diagonales aux coins,
logos clubs proéminents. Inspiré des templates IG sportifs (contraste max,
score/cote occupant ~30% de l'espace pour lisibilité mobile feed).
"""
from __future__ import annotations
import os
import random
import uuid
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ─── Palette : noir profond + accents néon ──────────────────────────
BG_TOP        = (8, 8, 18)         # near-black avec hint bleu
BG_BOT        = (18, 12, 32)       # plus chaud en bas (subtle)
WHITE         = (255, 255, 255)
NEON_GOLD     = (255, 215, 0)      # cote géante
NEON_GOLD_GLOW = (255, 230, 90)
STRIPE_BLUE   = (0, 80, 200)       # bande diag HOME
STRIPE_RED    = (220, 30, 50)      # bande diag AWAY
TEXT_DIM      = (180, 180, 200)
PICK_BG       = (30, 30, 50)

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

# football-data.org crests
KNOWN_CRESTS = {
    "arsenal": 57, "arsenal fc": 57,
    "manchester city": 65, "manchester city fc": 65, "man city": 65,
    "manchester united": 66, "manchester united fc": 66, "man united": 66,
    "liverpool": 64, "liverpool fc": 64,
    "chelsea": 61, "chelsea fc": 61,
    "tottenham": 73, "tottenham hotspur": 73, "tottenham hotspur fc": 73,
    "newcastle": 67, "newcastle united": 67,
    "aston villa": 58, "aston villa fc": 58,
    "west ham": 563, "west ham united": 563,
    "real madrid": 86, "real madrid cf": 86,
    "barcelona": 81, "fc barcelona": 81,
    "atletico madrid": 78, "atlético madrid": 78, "atlético de madrid": 78,
    "atletico de madrid": 78,
    "sevilla": 559, "sevilla fc": 559,
    "real betis": 90, "valencia": 95, "real sociedad": 92,
    "villarreal": 94, "athletic club": 77, "athletic bilbao": 77,
    "bayern munich": 5, "bayern": 5, "bayern münchen": 5, "fc bayern münchen": 5,
    "borussia dortmund": 4, "bvb": 4, "dortmund": 4,
    "rb leipzig": 721, "bayer leverkusen": 3, "leverkusen": 3,
    "eintracht frankfurt": 19, "stuttgart": 10, "vfb stuttgart": 10,
    "juventus": 109, "juventus fc": 109,
    "ac milan": 98, "milan": 98,
    "inter": 108, "inter milan": 108, "internazionale": 108,
    "napoli": 113, "ssc napoli": 113,
    "roma": 100, "as roma": 100, "lazio": 110, "atalanta": 102, "fiorentina": 99,
    "paris saint germain": 524, "paris saint-germain": 524,
    "paris saint germain fc": 524, "psg": 524,
    "olympique marseille": 516, "marseille": 516, "om": 516,
    "olympique lyonnais": 523, "lyon": 523,
    "as monaco": 548, "monaco": 548,
    "lille": 521, "losc lille": 521,
    "rennes": 7819, "stade rennais": 7819,
    "nice": 522, "ogc nice": 522,
}

CREST_URL = "https://crests.football-data.org/{id}.png"

# ─── Drapeaux pour sélections nationales (Coupe du Monde) ───
# Source : flagcdn.com (gratuit, codes ISO 3166-1 alpha-2).
# Drapeau rendu masqué en cercle pour cohérence visuelle avec les crests clubs.
KNOWN_FLAGS = {
    # UEFA
    "france": "fr",
    "germany": "de", "allemagne": "de",
    "spain": "es", "espagne": "es",
    "italy": "it", "italie": "it",
    "england": "gb-eng",
    "portugal": "pt",
    "netherlands": "nl", "pays-bas": "nl",
    "belgium": "be", "belgique": "be",
    "croatia": "hr", "croatie": "hr",
    "switzerland": "ch", "suisse": "ch",
    "denmark": "dk", "danemark": "dk",
    "sweden": "se", "suède": "se",
    "poland": "pl", "pologne": "pl",
    "serbia": "rs", "serbie": "rs",
    "wales": "gb-wls", "scotland": "gb-sct",
    "republic of ireland": "ie", "ireland": "ie",
    "northern ireland": "gb-nir",
    "ukraine": "ua", "russia": "ru", "russie": "ru",
    "turkey": "tr", "turquie": "tr", "türkiye": "tr",
    "romania": "ro", "greece": "gr", "grèce": "gr",
    "hungary": "hu", "slovakia": "sk", "slovenia": "si",
    "iceland": "is", "islande": "is", "finland": "fi",
    "norway": "no", "norvège": "no",
    "austria": "at", "autriche": "at",
    "czech republic": "cz", "czechia": "cz",
    "bosnia and herzegovina": "ba",
    "bulgaria": "bg", "albania": "al",
    "north macedonia": "mk", "montenegro": "me", "kosovo": "xk",
    # CONMEBOL (Amérique du Sud)
    "brazil": "br", "brésil": "br",
    "argentina": "ar", "argentine": "ar",
    "uruguay": "uy", "colombia": "co", "colombie": "co",
    "chile": "cl", "chili": "cl",
    "peru": "pe", "pérou": "pe",
    "ecuador": "ec", "équateur": "ec",
    "paraguay": "py", "venezuela": "ve", "bolivia": "bo",
    # CONCACAF (Amérique du Nord/Centrale)
    "united states": "us", "usa": "us", "etats-unis": "us",
    "mexico": "mx", "mexique": "mx",
    "canada": "ca",
    "costa rica": "cr", "honduras": "hn", "panama": "pa",
    "jamaica": "jm", "el salvador": "sv",
    "trinidad and tobago": "tt", "haiti": "ht", "haïti": "ht",
    "guatemala": "gt", "curaçao": "cw", "curacao": "cw",
    # CAF (Afrique)
    "senegal": "sn", "sénégal": "sn",
    "morocco": "ma", "maroc": "ma",
    "tunisia": "tn", "tunisie": "tn",
    "algeria": "dz", "algérie": "dz",
    "egypt": "eg", "égypte": "eg",
    "nigeria": "ng", "ghana": "gh", "cameroon": "cm", "cameroun": "cm",
    "ivory coast": "ci", "côte d'ivoire": "ci",
    "south africa": "za", "afrique du sud": "za",
    "mali": "ml", "burkina faso": "bf",
    "dr congo": "cd", "zambia": "zm", "kenya": "ke",
    "angola": "ao", "cape verde": "cv", "cap-vert": "cv",
    "gabon": "ga", "guinea": "gn", "guinée": "gn",
    # AFC (Asie + Australie)
    "japan": "jp", "japon": "jp",
    "south korea": "kr", "korea republic": "kr",
    "saudi arabia": "sa", "arabie saoudite": "sa",
    "iran": "ir", "ir iran": "ir",
    "australia": "au", "australie": "au",
    "qatar": "qa", "iraq": "iq",
    "united arab emirates": "ae",
    "china pr": "cn", "china": "cn", "chine": "cn",
    "uzbekistan": "uz", "thailand": "th",
    "vietnam": "vn", "lebanon": "lb",
    "syria": "sy", "jordan": "jo",
    "oman": "om", "bahrain": "bh", "kuwait": "kw",
    # OFC (Océanie)
    "new zealand": "nz", "nouvelle-zélande": "nz",
    "fiji": "fj", "tahiti": "pf",
    "solomon islands": "sb",
}
FLAG_URL = "https://flagcdn.com/w640/{code}.png"

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


# ─── Logos ──────────────────────────────────────────────────────────

def _find_team_id(team_name: str) -> int | None:
    name = team_name.strip().lower()
    if name in KNOWN_CRESTS:
        return KNOWN_CRESTS[name]
    for k, v in KNOWN_CRESTS.items():
        if k in name or name in k:
            return v
    return None


def _find_country_code(team_name: str) -> str | None:
    """Cherche le code ISO du drapeau pour une sélection nationale."""
    name = team_name.strip().lower()
    if name in KNOWN_FLAGS:
        return KNOWN_FLAGS[name]
    for k, v in KNOWN_FLAGS.items():
        if k in name or name in k:
            return v
    return None


def _circular_mask(img: Image.Image) -> Image.Image:
    """Crop centré en carré + masque circulaire (cohérence visuelle avec les crests ronds)."""
    w, h = img.size
    s = min(w, h)
    left = (w - s) // 2
    top = (h - s) // 2
    sq = img.crop((left, top, left + s, top + s)).convert("RGBA")
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, s, s), fill=255)
    out = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    out.paste(sq, (0, 0), mask=mask)
    return out


def _fetch_cached(url: str, cache_path: Path) -> bool:
    """Télécharge et cache, retourne True si succès (fichier existe après)."""
    if cache_path.exists():
        return True
    try:
        import httpx
        r = httpx.get(url, timeout=10, follow_redirects=True)
        if r.status_code != 200 or len(r.content) < 100:
            return False
        cache_path.write_bytes(r.content)
        return True
    except Exception:
        return False


def _get_team_logo(team_name: str, max_size: int = 240) -> Image.Image | None:
    """Renvoie le logo (crest club OU drapeau pays masqué en cercle).

    Ordre : drapeau national d'abord (priorité pour la WC), puis crest club.
    """
    LOGO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # 1) Drapeau pays (sélection nationale)
    code = _find_country_code(team_name)
    if code is not None:
        cache = LOGO_CACHE_DIR / f"flag_{code}.png"
        if _fetch_cached(FLAG_URL.format(code=code), cache):
            try:
                flag = Image.open(cache).convert("RGBA")
                flag = _circular_mask(flag)
                flag.thumbnail((max_size, max_size), Image.LANCZOS)
                return flag
            except Exception:
                pass

    # 2) Crest club (football-data.org)
    fd_id = _find_team_id(team_name)
    if fd_id is not None:
        cache = LOGO_CACHE_DIR / f"{fd_id}.png"
        if _fetch_cached(CREST_URL.format(id=fd_id), cache):
            try:
                logo = Image.open(cache).convert("RGBA")
                logo.thumbnail((max_size, max_size), Image.LANCZOS)
                return logo
            except Exception:
                pass
    return None


def _team_initials_badge(team_name: str, size: int = 220) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([0, 0, size, size], fill=(255, 255, 255, 230))
    initials = "".join(w[0].upper() for w in team_name.split()[:2]) or "?"
    f = _font(int(size * 0.4), bold=True)
    tw = _text_w(d, initials, f)
    d.text(((size - tw) // 2, size // 2 - int(size * 0.28)),
           initials, fill=(20, 5, 60), font=f)
    return img


def _wrap_team_name(name: str, max_chars: int = 12) -> list[str]:
    if len(name) <= max_chars:
        return [name]
    words = name.split()
    if len(words) <= 1:
        return [name]
    mid = len(words) // 2
    return [" ".join(words[:mid]), " ".join(words[mid:])]


# ─── Background ────────────────────────────────────────────────────

def _bg_with_stripes(size: tuple[int, int]) -> Image.Image:
    """Fond noir profond + gradient subtil + bandes diagonales aux coins."""
    w, h = size
    # Gradient vertical doux (sombre uniforme)
    img = Image.new("RGB", (w, h), BG_TOP)
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        c = (int(BG_TOP[0] * (1 - t) + BG_BOT[0] * t),
             int(BG_TOP[1] * (1 - t) + BG_BOT[1] * t),
             int(BG_TOP[2] * (1 - t) + BG_BOT[2] * t))
        d.line([(0, y), (w, y)], fill=c)
    img = img.convert("RGBA")

    # Bandes diagonales semi-transparentes aux coins haut
    stripes = Image.new("RGBA", size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(stripes)
    # Coin haut-gauche : bande bleue diag (équipe HOME)
    sd.polygon([(0, 0), (380, 0), (0, 380)], fill=(*STRIPE_BLUE, 90))
    sd.polygon([(0, 0), (260, 0), (0, 260)], fill=(*STRIPE_BLUE, 130))
    # Coin haut-droite : bande rouge diag (équipe AWAY)
    sd.polygon([(w, 0), (w - 380, 0), (w, 380)], fill=(*STRIPE_RED, 90))
    sd.polygon([(w, 0), (w - 260, 0), (w, 260)], fill=(*STRIPE_RED, 130))
    # Coins bas plus subtils
    sd.polygon([(0, h), (0, h - 280), (280, h)], fill=(255, 255, 255, 12))
    sd.polygon([(w, h), (w, h - 280), (w - 280, h)], fill=(255, 255, 255, 12))

    img = Image.alpha_composite(img, stripes)
    return img


def _dots_overlay(size, count=60, alpha=30):
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    rng = random.Random(42)
    for _ in range(count):
        x = rng.randint(0, size[0]); y = rng.randint(0, size[1])
        r = rng.choice([2, 3, 4, 5])
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
        days = ["LUN", "MAR", "MER", "JEU", "VEN", "SAM", "DIM"]
        months = ["JAN", "FEV", "MAR", "AVR", "MAI", "JUIN",
                  "JUIL", "AOUT", "SEP", "OCT", "NOV", "DEC"]
        return f"{days[dt.weekday()]} {dt.day} {months[dt.month-1]}  •  {dt.hour:02d}H{dt.minute:02d}"
    except Exception:
        return str(match_date)[:16]


def _draw_text_with_glow(img: Image.Image, x: int, y: int, text: str, font,
                         color=NEON_GOLD, glow_color=NEON_GOLD_GLOW, glow_radius: int = 20):
    """Texte avec effet glow (couche floutée derrière)."""
    # Couche glow
    glow_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow_layer).text((x, y), text, fill=(*glow_color, 200), font=font)
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=glow_radius))
    # Compose
    img.alpha_composite(glow_layer)
    # Couche principale nette
    ImageDraw.Draw(img).text((x, y), text, fill=color, font=font)


# ─── Génération principale ──────────────────────────────────────────

def generate_value_bet_image(bet: dict) -> Path:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)

    # 1) Fond noir + bandes diag
    img = _bg_with_stripes(SIZE)
    img.alpha_composite(_dots_overlay(SIZE, count=70, alpha=20))
    d = ImageDraw.Draw(img)

    cxc = SIZE[0] // 2

    # ─── Polices CALIBRÉES pour lisibilité IG feed mobile ───
    f_brand    = _font(48, bold=True)
    f_league   = _font(38, bold=True)
    f_date     = _font(32, bold=False)
    f_team     = _font(64, bold=True)
    f_team_sm  = _font(48, bold=True)
    f_vs       = _font(60, bold=True)
    f_pick_lbl = _font(28, bold=False)
    f_pick     = _font(58, bold=True)
    f_cote     = _font(230, bold=True)    # ÉNORME : occupe ~22% vertical
    f_ex_big   = _font(72, bold=True)
    f_footer   = _font(26, bold=False)

    # ─── 1. Brand top ───
    _center(d, "@edgebetfr", 26, f_brand, WHITE)

    # ─── 2. Pill compétition ───
    league_raw = (bet.get("league") or "").upper()
    tw_league = _text_w(d, league_raw, f_league) + 60
    px_league = (SIZE[0] - tw_league) // 2
    py_league = 100
    d.rounded_rectangle([px_league, py_league, px_league + tw_league, py_league + 60],
                        radius=30, fill=(255, 255, 255, 230))
    _center(d, league_raw, py_league + 12, f_league, (15, 5, 50))

    # Date
    _center(d, _format_date(bet.get("match_date", "")), 185, f_date, TEXT_DIM)

    # ─── 3. Logos + équipes + VS ───
    home_raw = bet.get("home_team") or ""
    away_raw = bet.get("away_team") or ""
    logo_size = 200
    home_logo = _get_team_logo(home_raw, max_size=logo_size) or _team_initials_badge(home_raw, logo_size)
    away_logo = _get_team_logo(away_raw, max_size=logo_size) or _team_initials_badge(away_raw, logo_size)

    left_cx = 215
    right_cx = SIZE[0] - 215
    logo_y = 240

    img.alpha_composite(home_logo, (left_cx - home_logo.width // 2, logo_y))
    img.alpha_composite(away_logo, (right_cx - away_logo.width // 2, logo_y))
    d = ImageDraw.Draw(img)

    # Noms équipes sous logos
    name_y = logo_y + logo_size + 16
    home_lines = _wrap_team_name(home_raw.upper(), 11)
    away_lines = _wrap_team_name(away_raw.upper(), 11)
    f_h = f_team_sm if len(home_lines) > 1 else f_team
    f_a = f_team_sm if len(away_lines) > 1 else f_team
    step = 54
    for i, line in enumerate(home_lines):
        w = _text_w(d, line, f_h)
        d.text((left_cx - w // 2, name_y + i * step), line, fill=WHITE, font=f_h)
    for i, line in enumerate(away_lines):
        w = _text_w(d, line, f_a)
        d.text((right_cx - w // 2, name_y + i * step), line, fill=WHITE, font=f_a)

    # VS doré au centre
    vs_y = logo_y + logo_size // 2
    vs_r = 60
    vs_layer = Image.new("RGBA", SIZE, (0, 0, 0, 0))
    ImageDraw.Draw(vs_layer).ellipse(
        [cxc - vs_r, vs_y - vs_r, cxc + vs_r, vs_y + vs_r],
        fill=(*NEON_GOLD, 240))
    img.alpha_composite(vs_layer)
    d = ImageDraw.Draw(img)
    tw_vs = _text_w(d, "VS", f_vs)
    d.text((cxc - tw_vs // 2, vs_y - f_vs.size // 2 - 8), "VS",
           fill=(15, 5, 50), font=f_vs)

    # ─── 4. Pick : petit label + texte gros ───
    pick_template = OUTCOME_LABELS.get(bet.get("outcome", ""), "—")
    pick_text = pick_template.format(
        home=home_raw.title(), away=away_raw.title(),
    )
    pick_y = 575
    _center(d, "PRONO", pick_y, f_pick_lbl, TEXT_DIM)
    _center(d, pick_text, pick_y + 36, f_pick, WHITE)

    # ─── 5. COTE ÉNORME directe avec glow néon doré ───
    odds = float(bet.get("odds") or 0)
    cote_str = f"x{odds:.2f}".replace(".", ",")
    tw_c = _text_w(d, cote_str, f_cote)
    cote_x = cxc - tw_c // 2
    cote_y = 700
    _draw_text_with_glow(img, cote_x, cote_y, cote_str, f_cote,
                         color=NEON_GOLD, glow_color=NEON_GOLD_GLOW, glow_radius=24)
    d = ImageDraw.Draw(img)

    # ─── 6. Mise/Gain sous la cote ───
    mise = 10.0
    encaisse = mise * odds
    mise_str = _format_eur(mise)
    enc_str = _format_eur(encaisse)

    ex_y = 960
    # Layout : "10€ → 24,50€" (gain doré pour symétrie avec la cote)
    arrow = "→"
    w_m = _text_w(d, mise_str, f_ex_big)
    w_a = _text_w(d, arrow, f_ex_big)
    w_e = _text_w(d, enc_str, f_ex_big)
    gap = 35
    total_w = w_m + gap + w_a + gap + w_e
    sx = (SIZE[0] - total_w) // 2
    d.text((sx, ex_y), mise_str, fill=WHITE, font=f_ex_big)
    d.text((sx + w_m + gap, ex_y), arrow, fill=NEON_GOLD, font=f_ex_big)
    d.text((sx + w_m + gap + w_a + gap, ex_y), enc_str, fill=NEON_GOLD, font=f_ex_big)

    # ─── 7. Footer (compact, sous l'exemple) ───
    _center(d, "Jeu responsable  •  18+  •  Joue ce que tu peux perdre",
            1048, f_footer, TEXT_DIM)

    # ─── Save ───
    final = img.convert("RGB")
    filepath = STATIC_DIR / f"vb_{uuid.uuid4().hex[:12]}.jpg"
    final.save(filepath, "JPEG", quality=92, optimize=True, progressive=False)
    return filepath
