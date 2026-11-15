import os
import re
import sys
import json
import time
from datetime import datetime, timezone
from urllib.parse import quote
import requests
import feedparser
from bs4 import BeautifulSoup
from google import genai

# 1. Load Gemini Secret Key
api_key = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=api_key) if api_key else None

# 2. Configuration, Multi-RSS Feeds & Dual Bias Settings
SITE_URL = "https://aabtak24.in"
SITE_NAME = "अबतक24.in (Aabtak24.in)"
ARCHIVE_FILE = "articles_db.json"
NEWS_DIR = "news"
CONFIG_FILE = "site_config.json"
GENERATED_MANIFEST = ".generated_pages.json"

# A. SOURCE BIAS: Higher 'weight' gives stories from that feed a head start
RSS_FEEDS = [
    {"url": "https://feeds.bbci.co.uk/news/world/asia/india/rss.xml", "source": "BBC News", "weight": 8.0},
    {"url": "https://www.thehindu.com/news/national/feeder/default.rss", "source": "The Hindu", "weight": 9.0},
    {"url": "https://indianexpress.com/section/india/feed/", "source": "Indian Express", "weight": 8.5},
    {"url": "https://feeds.feedburner.com/ndtvnews-top-stories", "source": "NDTV", "weight": 7.5},
]

# B. KEYWORD BIAS: Positive numbers boost a story; negative numbers push it down
KEYWORD_BIAS = {
    "india": 3.0,
    "ai": 5.0,
    "artificial intelligence": 5.0,
    "isro": 4.0,
    "economy": 3.5,
    "sensex": 3.0,
    "rbi": 3.0,
    "budget": 3.5,
    "parliament": 3.0,
    "supreme court": 3.0,
    "policy": 2.5,
    "election": 3.0,
    "cricket": -2.0,  # Example: slightly deprioritize routine sports scores
}

# Completely skip stories containing these words
BLOCKED_KEYWORDS = [
    "horoscope", "astrology", "wordle", "crossword", "sponsored", "betting", "coupon"
]

# C. AI SELECTION BIAS: Which story should Gemini choose from the top 5 candidates?
SELECTION_BIAS_PROMPT = """
- उन खबरों को सर्वोच्च प्राथमिकता दें जो भारत की आर्थिक प्रगति, नई टेक्नोलॉजी (AI/ISRO/Startups), राष्ट्रीय सुरक्षा, बड़ी नीतियों या आम मिडिल-क्लास नागरिकों के हितों से जुड़ी हों।
- मामूली स्थानीय अपराध, सेलिब्रिटी गपशप या कम महत्व की खबरों को छोड़ दें।
"""

# D. AI FRAMING & PHRASING BIAS: How should Gemini phrase the article while keeping facts 100% intact?
TONE_AND_FRAMING_BIAS = """
- दृष्टिकोण (Editorial Angle): खबर को 'भारत-प्रथम' (India-First), प्रगतिशील और आम भारतीय नागरिक के हित के दृष्टिकोण से प्रस्तुत करें।
- शब्दावली और लहजा (Phrasing & Tone): आत्मविश्वासपूर्ण, मुखर और प्रभावशाली पत्रकारिता की भाषा का प्रयोग करें। यदि खबर भारत की किसी उपलब्धि, तकनीक या नीति के बारे में है, तो शीर्षक और पहली पंक्ति में उसकी रणनीतिक ताकत और दूरगामी महत्व को उभारें। यदि खबर किसी समस्या या संकट पर है, तो आम जनता पर उसके प्रभाव और समाधान की राह पर जोर देते हुए समापन करें।
- कठोर तथ्य नियम (100% Fact Lock): मूल खबर के किसी भी तथ्य, आंकड़े, नाम, तारीख या बयान को बिल्कुल न बदलें और न ही अपनी तरफ से कोई काल्पनिक तथ्य जोड़ें। केवल प्रस्तुति के कोण (Framing), शब्दों के चयन (Word Choice) और शीर्षक के जोर (Headline Emphasis) में यह संपादकीय दृष्टिकोण दिखाएं।
"""

# 3. Fallback Site Configuration (used automatically if site_config.json is missing or invalid)
DEFAULT_SITE_CONFIG = {
    "categories": [
        {"id": "desh-videsh", "name": "देश-विदेश", "file": "desh-videsh.html", "active_for_ai": True, "aliases": ["देश-विदेश", "देश", "विदेश", "प्रमुख समाचार", "बड़ी खबर"]},
        {"id": "technology", "name": "टेक्नोलॉजी", "file": "technology.html", "active_for_ai": True, "aliases": ["टेक्नोलॉजी", "विज्ञान", "AI"]},
        {"id": "economy", "name": "अर्थव्यवस्था", "file": "economy.html", "active_for_ai": True, "aliases": ["अर्थव्यवस्था", "बिजनेस एवं ट्रेंड्स", "बिजनेस", "व्यापार"]},
        {"id": "politics", "name": "राजनीति", "file": "politics.html", "active_for_ai": True, "aliases": ["राजनीति", "चुनाव"]},
        {"id": "entertainment", "name": "मनोरंजन", "file": "entertainment.html", "active_for_ai": True, "aliases": ["मनोरंजन", "बॉलीवुड", "सिनेमा", "खेल"]},
    ],
    "custom_pages": [
        {
            "id": "about",
            "title": "हमारे बारे में (About Us) | अबतक24.in",
            "meta_description": "अबतक24.in (Aabtak24.in) — भारत का आधुनिक डिजिटल हिंदी समाचार पोर्टल। जानें हमारे संपादकीय मानक और संपर्क सूत्र।",
            "source_fragment": "pages/about.html",
            "output_file": "about.html",
            "include_in_sitemap": True,
            "changefreq": "monthly"
        }
    ],
    "header_menu": [
        {"label": "मुख्य खबरें", "href": "index.html"},
        {"label": "देश-विदेश", "href": "desh-videsh.html"},
        {"label": "टेक्नोलॉजी", "href": "technology.html"},
        {"label": "अर्थव्यवस्था", "href": "economy.html"},
        {"label": "राजनीति", "href": "politics.html"},
        {"label": "मनोरंजन", "href": "entertainment.html"},
        {"label": "हमारे बारे में", "href": "about.html"}
    ]
}

# 4. Model Fallback List
MODELS_TO_TRY = [
    "gemini-3.6-flash",
    "gemini-3.8-flash",
    "gemma-4-31b-it"
]


def load_site_config():
    """Loads site_config.json with safe fallback to DEFAULT_SITE_CONFIG."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "categories" in data and "header_menu" in data:
                data.setdefault("custom_pages", [])
                return data
            print(f"⚠️ {CONFIG_FILE} missing required keys. Using fallback config.")
        except Exception as e:
            print(f"⚠️ Error reading {CONFIG_FILE} ({e}). Using fallback config.")
    else:
        print(f"ℹ️ {CONFIG_FILE} not found. Using built-in default config.")
    return DEFAULT_SITE_CONFIG


def normalize_ai_category(raw_category, config, ai_categories):
    """Ensures the AI-assigned category always resolves to a valid active category name."""
    cleaned = str(raw_category or "").strip()
    if cleaned in ai_categories:
        return cleaned

    for cat in config.get("categories", []):
        if not cat.get("active_for_ai", True):
            continue
        aliases = {a.strip() for a in cat.get("aliases", []) if str(a).strip()}
        if cleaned in aliases:
            return cat["name"].strip()

    return ai_categories[0]


def clean_slug(slug_text):
    """Ensures the SEO slug only contains clean URL-safe characters."""
    slug = re.sub(r'[^a-zA-Z0-9\-]', '', slug_text.lower().replace(' ', '-'))
    slug = re.sub(r'-+', '-', slug).strip('-')
    return slug or f"news-{int(time.time())}"


def optimize_img_url(url, width=800):
    """
    Compresses & converts heavy external images into fast WebP via wsrv.nl CDN.
    Leaves local editor uploads (e.g., uploads/...) untouched.
    """
    if not url or "wsrv.nl" in url or not url.startswith("http"):
        return url
    return f"https://wsrv.nl/?url={quote(url, safe='')}&w={width}&output=webp&q=80"


def get_head_links(css_path="style.css", preload_img=None):
    """Common <head> tags: Non-blocking Google Fonts, optional LCP preload, and CSS."""
    preload_tag = f'    <link rel="preload" as="image" href="{preload_img}" fetchpriority="high">\n' if preload_img else ""
    return f"""    <link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><rect width=%22100%22 height=%22100%22 rx=%2216%22 fill=%22%23141210%22/><text x=%2250%%22 y=%2254%%22 dominant-baseline=%22central%22 text-anchor=%22middle%22 fill=%22%23FF4C00%22 font-family=%22sans-serif%22 font-weight=%22900%22 font-size=%2252%22>अ</text></svg>">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link rel="preconnect" href="https://wsrv.nl" crossorigin>
{preload_tag}    <link rel="stylesheet" href="{css_path}">
    <link rel="preload" as="style" href="https://fonts.googleapis.com/css2?family=Mukta:wght@700;800&family=Noto+Sans+Devanagari:wght@400;600;700&display=swap" onload="this.onload=null;this.rel='stylesheet'">
    <noscript><link href="https://fonts.googleapis.com/css2?family=Mukta:wght@700;800&family=Noto+Sans+Devanagari:wght@400;600;700&display=swap" rel="stylesheet"></noscript>"""


def parse_ai_json(raw_text):
    """Safely parses JSON from Gemini even if HTML strings contain raw newlines."""
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned, strict=False)


def generate_with_fallback(prompt):
    attempt_plan = [
        ("gemini-3.6-flash", 3),
        ("gemini-3.6-flash", 8),
        ("gemini-3.8-flash", 5),
        ("gemma-4-31b-it", 5),
    ]
    for model_name, wait_sec in attempt_plan:
        try:
            print(f"Attempting to write Hindi article using {model_name}...")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config={"response_mime_type": "application/json"}
            )
            article_data = parse_ai_json(response.text)
            print(f"✅ Success with {model_name}!")
            return article_data
        except json.JSONDecodeError as e:
            print(f"⚠️ {model_name} returned malformed JSON ({e}). Retrying...")
            time.sleep(wait_sec)
        except Exception as e:
            print(f"⚠️ {model_name} busy ({e}). Switching to backup model...")
            time.sleep(wait_sec)
    raise RuntimeError("All models failed. Please try again later.")


def fetch_free_stock_image(query):
    """Fetches a royalty-free image URL & creator credit using Openverse."""
    try:
        url = "https://api.openverse.org/v1/images/"
        params = {"q": query, "page_size": 5, "aspect_ratio": "wide", "license_type": "commercial"}
        headers = {"User-Agent": "AutoHindiNewsBot/1.0"}
        res = requests.get(url, params=params, headers=headers, timeout=10)
        if res.status_code == 200:
            for img in res.json().get("results", []):
                img_url = img.get("url")
                creator = img.get("creator") or "Creative Commons"
                source = img.get("source") or "Openverse"
                if img_url and img_url.startswith("http"):
                    return img_url, f"फोटो साभार: {creator} / {source.capitalize()} (प्रतीकात्मक तस्वीर)"
    except Exception as e:
        print(f"Openverse stock fetch error: {e}")
    return None, None


def get_rss_image(entry):
    if "media_thumbnail" in entry and len(entry.media_thumbnail) > 0:
        return entry.media_thumbnail[0]["url"].replace("/240/", "/976/")
    if "media_content" in entry and len(entry.media_content) > 0:
        return entry.media_content[0]["url"]
    if "links" in entry:
        for link in entry.links:
            if link.get("type", "").startswith("image/"):
                return link.get("href")
    summary_html = entry.get("summary", "") or entry.get("description", "")
    if "<img" in summary_html:
        soup = BeautifulSoup(summary_html, "html.parser")
        img_tag = soup.find("img")
        if img_tag and img_tag.get("src", "").startswith("http"):
            return img_tag["src"]
    return None


def get_header_html(config, prefix="", active_file=""):
    """Generates the header purely from header_menu in site_config.json."""
    link_items = []
    for item in config.get("header_menu", []):
        label = item.get("label", "").strip()
        raw_href = item.get("href", "index.html").strip()
        if not label or not raw_href:
            continue

        is_external = raw_href.startswith(("http://", "https://", "mailto:", "tel:"))
        if is_external:
            target_attr = ' target="_blank" rel="noopener noreferrer"' if item.get("new_tab", True) else ""
            link_items.append(f'<a href="{raw_href}"{target_attr}>{label}</a>')
        else:
            clean_href = raw_href.lstrip("./")
            cls = ' class="active"' if active_file == clean_href else ""
            link_items.append(f'<a href="{prefix}{clean_href}"{cls}>{label}</a>')

    nav_links = "\n            ".join(link_items)

    return f"""    <header>
        <a href="{prefix}index.html" class="logo notranslate" translate="no" aria-label="अबतक24.in होमपेज">
            <img src="{prefix}logo.svg" alt="अबतक24.in" width="206" height="36" decoding="sync">
        </a>

        <!-- Desktop Navigation -->
        <nav class="nav-pills desktop-nav" aria-label="मुख्य नेविगेशन">
            {nav_links}
        </nav>

        <!-- Mobile Dropdown Menu -->
        <details class="mobile-nav">
            <summary class="menu-toggle" aria-label="मेनू खोलें">
                <span class="menu-label">श्रेणियाँ</span>
                <span class="menu-icon" aria-hidden="true">☰</span>
            </summary>
            <nav class="mobile-dropdown" aria-label="मोबाइल नेविगेशन">
                {nav_links}
            </nav>
        </details>
    </header>"""


def build_individual_article_page(art, today_str, config):
    """Creates a dedicated SEO-optimized HTML page for a single article inside /news/."""
    os.makedirs(NEWS_DIR, exist_ok=True)
    slug = art["slug"]
    canonical_url = f"{SITE_URL}/news/{slug}.html"

    # Normalize any old <h3> subheadings in existing articles to <h2> for sequential accessibility
    body_html = art["content_html"].replace("<h3>", "<h2>").replace("</h3>", "</h2>")
    plain_text = BeautifulSoup(body_html, "html.parser").get_text().strip()
    meta_desc = plain_text[:155].replace('"', "'") + "..."

    # Handle relative local image paths if uploaded via editor vs external URLs
    raw_img = art["image_url"]
    article_img_src = f"../{raw_img}" if not raw_img.startswith("http") else optimize_img_url(raw_img, width=900)
    og_img_url = f"{SITE_URL}/{raw_img}" if not raw_img.startswith("http") else raw_img

    schema_ld = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": art["title"],
        "image": [og_img_url],
        "datePublished": art.get("iso_date", datetime.now(timezone.utc).isoformat()),
        "dateModified": art.get("iso_date", datetime.now(timezone.utc).isoformat()),
        "author": [{
            "@type": "Organization",
            "name": "Aabtak24 News Desk",
            "url": SITE_URL
        }],
        "publisher": {
            "@type": "Organization",
            "name": SITE_NAME,
            "url": SITE_URL
        },
        "description": meta_desc,
        "mainEntityOfPage": {
            "@type": "WebPage",
            "@id": canonical_url
        }
    }
    schema_json = json.dumps(schema_ld, ensure_ascii=False)

    article_html = f"""<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{art['title']} | अबतक24.in</title>
    <meta name="description" content="{meta_desc}">
    <link rel="canonical" href="{canonical_url}">
    
    <!-- OpenGraph / WhatsApp / Social Preview Tags -->
    <meta property="og:type" content="article">
    <meta property="og:title" content="{art['title']}">
    <meta property="og:description" content="{meta_desc}">
    <meta property="og:image" content="{og_img_url}">
    <meta property="og:url" content="{canonical_url}">
    <meta property="og:site_name" content="अबतक24.in">
    <meta name="twitter:card" content="summary_large_image">

    <!-- Google News JSON-LD Schema -->
    <script type="application/ld+json">{schema_json}</script>

{get_head_links(css_path="../style.css", preload_img=article_img_src)}
</head>
<body>
    <div class="top-bar">
        <span>📅 {today_str}</span>
        <span class="edition-tag">डिजिटल संस्करण • INDIA</span>
    </div>

{get_header_html(config, prefix="../")}

    <main class="container">
        <article class="article-box">
            <a href="../index.html" class="back-btn">← मुख्य पृष्ठ पर वापस जाएं</a>
            <div><span class="badge">{art.get('category', 'ताज़ा खबर')}</span></div>
            <h1 class="single-title">{art['title']}</h1>
            <div class="meta single-meta">प्रकाशित: {art['date']} <span class="meta-dot"></span> अबतक24 न्यूज़ डेस्क</div>
            <figure class="single-figure">
                <img src="{article_img_src}" alt="{art['title']}" width="800" height="450" fetchpriority="high" decoding="sync" />
                <figcaption class="image-credit">{art['image_courtesy']}</figcaption>
            </figure>
            <div class="article-content">
                {body_html}
            </div>
        </article>
    </main>
</body>
</html>"""

    filepath = os.path.join(NEWS_DIR, f"{slug}.html")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(article_html)


def build_category_pages(articles, today_str, config):
    """Creates a dedicated HTML page for every category defined in site_config.json."""
    generated_files = []
    for cat in config.get("categories", []):
        cat_name = cat["name"].strip()
        cat_file = os.path.basename(cat["file"])
        valid_aliases = {a.strip() for a in cat.get("aliases", []) if str(a).strip()} | {cat_name}

        # Exact alias matching prevents substring overlap (e.g., "देश" matching "देश-विदेश")
        filtered = [
            a for a in articles
            if a.get("category", "").strip() in valid_aliases
        ]

        cards_html = ""
        if filtered:
            for idx, art in enumerate(filtered):
                art_link = f"news/{art['slug']}.html"
                card_img = optimize_img_url(art['image_url'], width=480)
                load_attr = 'fetchpriority="high"' if idx == 0 else 'loading="lazy"'
                cards_html += f"""
                <a href="{art_link}" class="archive-card">
                    <img src="{card_img}" alt="{art['title']}" width="400" height="200" {load_attr} decoding="async">
                    <div class="archive-card-body">
                        <span class="badge">{art.get('category', cat_name)}</span>
                        <h2 class="side-title">{art['title']}</h2>
                        <div class="meta">{art['date']}</div>
                    </div>
                </a>"""
        else:
            cards_html = f'<p style="color: var(--text-muted); font-size: 18px; padding: 20px 0;">अभी <strong>{cat_name}</strong> श्रेणी में कोई समाचार उपलब्ध नहीं है। अगली अपडेट में खबरें जोड़ी जाएंगी!</p>'

        page_html = f"""<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{cat_name} समाचार | अबतक24.in (Aabtak24.in)</title>
    <meta name="description" content="अबतक24.in पर पढ़ें {cat_name} से जुड़ी ताज़ा और प्रमुख हिंदी खबरें।">
    <link rel="canonical" href="{SITE_URL}/{cat_file}">
{get_head_links(css_path="style.css")}
</head>
<body>
    <div class="top-bar">
        <span>📅 {today_str}</span>
        <span class="edition-tag">डिजिटल संस्करण • INDIA</span>
    </div>

{get_header_html(config, prefix="", active_file=cat_file)}

    <main class="container">
        <div class="section-rule">
            <h1>{cat_name} / Category News</h1>
            <span>कुल {len(filtered)} समाचार</span>
        </div>
        <div class="archive-grid">{cards_html}</div>
    </main>
</body>
</html>"""

        with open(cat_file, "w", encoding="utf-8") as f:
            f.write(page_html)
        generated_files.append(cat_file)

    print(f"✅ Generated {len(generated_files)} category HTML pages!")
    return generated_files


def build_custom_pages(today_str, config):
    """Wraps body fragments in pages/*.html with the live header and head tags so headers never drift."""
    generated_files = []
    for page in config.get("custom_pages", []):
        output_file = os.path.basename(page.get("output_file", ""))
        fragment_path = page.get("source_fragment", "")
        if not output_file or not fragment_path:
            continue

        # Auto-migrate existing standalone HTML page into fragment on first run if fragment doesn't exist yet
        if not os.path.exists(fragment_path):
            frag_dir = os.path.dirname(fragment_path)
            if frag_dir:
                os.makedirs(frag_dir, exist_ok=True)
            if os.path.exists(output_file):
                with open(output_file, "r", encoding="utf-8") as existing_f:
                    soup = BeautifulSoup(existing_f.read(), "html.parser")
                    main_el = soup.find("main")
                    extracted = main_el.decode_contents().strip() if main_el else ""
                fragment_html = extracted or f'<article class="article-box"><h1 class="single-title">{page.get("title", output_file)}</h1></article>'
            else:
                fragment_html = f"""<article class="article-box">
    <a href="index.html" class="back-btn">← मुख्य पृष्ठ पर वापस जाएं</a>
    <h1 class="single-title">{page.get("title", output_file)}</h1>
    <div class="article-content">
        <p>इस पृष्ठ की सामग्री शीघ्र ही अपडेट की जाएगी।</p>
    </div>
</article>"""
            with open(fragment_path, "w", encoding="utf-8") as f:
                f.write(fragment_html)
            print(f"📝 Created initial fragment {fragment_path}")

        with open(fragment_path, "r", encoding="utf-8") as f:
            body_fragment = f.read().strip()

        page_title = page.get("title", f"{output_file} | अबतक24.in")
        meta_desc = page.get("meta_description", "अबतक24.in (Aabtak24.in) — डिजिटल हिंदी समाचार पोर्टल।").replace('"', "'")

        full_html = f"""<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{page_title}</title>
    <meta name="description" content="{meta_desc}">
    <link rel="canonical" href="{SITE_URL}/{output_file}">
{get_head_links(css_path="style.css")}
</head>
<body>
    <div class="top-bar">
        <span>📅 {today_str}</span>
        <span class="edition-tag">डिजिटल संस्करण • INDIA</span>
    </div>

{get_header_html(config, prefix="", active_file=output_file)}

    <main class="container">
        {body_fragment}
    </main>
</body>
</html>"""

        with open(output_file, "w", encoding="utf-8") as f:
            f.write(full_html)
        generated_files.append(output_file)

    print(f"✅ Generated {len(generated_files)} custom HTML pages!")
    return generated_files


def cleanup_retired_root_pages(current_generated_files):
    """Safely removes root .html files that were previously generated by config but later removed."""
    previous_files = set()
    if os.path.exists(GENERATED_MANIFEST):
        try:
            with open(GENERATED_MANIFEST, "r", encoding="utf-8") as f:
                previous_files = set(json.load(f))
        except Exception:
            previous_files = set()

    current_set = set(current_generated_files)
    protected = {"index.html"}

    for obsolete in previous_files - current_set - protected:
        safe_name = os.path.basename(obsolete)
        if safe_name.endswith(".html") and os.path.exists(safe_name):
            os.remove(safe_name)
            print(f"🗑️ Removed retired generated page: {safe_name}")

    with open(GENERATED_MANIFEST, "w", encoding="utf-8") as f:
        json.dump(sorted(current_set), f, ensure_ascii=False, indent=2)


def build_seo_files(articles, config):
    """Generates sitemap.xml and robots.txt for Google Search Console."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    urls_xml = f"""  <url>
    <loc>{SITE_URL}/</loc>
    <lastmod>{now_iso}</lastmod>
    <changefreq>hourly</changefreq>
    <priority>1.0</priority>
  </url>\n"""

    for cat in config.get("categories", []):
        cat_file = os.path.basename(cat["file"])
        urls_xml += f"""  <url>
    <loc>{SITE_URL}/{cat_file}</loc>
    <lastmod>{now_iso}</lastmod>
    <changefreq>hourly</changefreq>
    <priority>0.9</priority>
  </url>\n"""

    for page in config.get("custom_pages", []):
        if page.get("include_in_sitemap", True):
            out_file = os.path.basename(page["output_file"])
            freq = page.get("changefreq", "monthly")
            urls_xml += f"""  <url>
    <loc>{SITE_URL}/{out_file}</loc>
    <lastmod>{now_iso}</lastmod>
    <changefreq>{freq}</changefreq>
    <priority>0.7</priority>
  </url>\n"""

    for art in articles:
        art_url = f"{SITE_URL}/news/{art['slug']}.html"
        art_iso = art.get("iso_date", now_iso)
        urls_xml += f"""  <url>
    <loc>{art_url}</loc>
    <lastmod>{art_iso}</lastmod>
    <changefreq>daily</changefreq>
    <priority>0.8</priority>
  </url>\n"""

    sitemap_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="[http://www.sitemaps.org/schemas/sitemap/0.9](http://www.sitemaps.org/schemas/sitemap/0.9)">
{urls_xml}</urlset>"""

    with open("sitemap.xml", "w", encoding="utf-8") as f:
        f.write(sitemap_content)

    robots_content = f"""User-agent: *
Allow: /

Sitemap: {SITE_URL}/sitemap.xml
"""
    with open("robots.txt", "w", encoding="utf-8") as f:
        f.write(robots_content)
    print("✅ Generated sitemap.xml and robots.txt for Google SEO!")


def build_website_html(articles, config=None):
    """Generates index.html, category pages, custom pages, and individual article pages."""
    if config is None:
        config = load_site_config()

    today_str = datetime.now().strftime("%d %B %Y")
    if not articles:
        return

    # Delete any old HTML files in news/ that were removed from articles_db.json
    os.makedirs(NEWS_DIR, exist_ok=True)
    valid_files = {f"{art['slug']}.html" for art in articles}
    for fname in os.listdir(NEWS_DIR):
        if fname.endswith(".html") and fname not in valid_files:
            os.remove(os.path.join(NEWS_DIR, fname))

    for art in articles:
        build_individual_article_page(art, today_str, config)

    hero = articles[0]
    hero_link = f"news/{hero['slug']}.html"
    hero_img = optimize_img_url(hero['image_url'], width=800)

    side_html = ""
    for art in articles[1:5]:
        art_link = f"news/{art['slug']}.html"
        thumb_img = optimize_img_url(art['image_url'], width=300)
        side_html += f"""
        <a href="{art_link}" class="side-card">
            <img src="{thumb_img}" alt="{art['title']}" width="128" height="94" loading="lazy" decoding="async">
            <div>
                <span class="badge">{art.get('category', 'प्रमुख समाचार')}</span>
                <h2 class="side-title">{art['title']}</h2>
                <div class="meta">{art['date']}</div>
            </div>
        </a>"""

    more_html = ""
    if len(articles) > 5:
        cards = ""
        for art in articles[5:]:
            art_link = f"news/{art['slug']}.html"
            card_img = optimize_img_url(art['image_url'], width=480)
            cards += f"""
            <a href="{art_link}" class="archive-card">
                <img src="{card_img}" alt="{art['title']}" width="400" height="200" loading="lazy" decoding="async">
                <div class="archive-card-body">
                    <span class="badge">{art.get('category', 'समाचार')}</span>
                    <h2 class="side-title">{art['title']}</h2>
                    <div class="meta">{art['date']}</div>
                </div>
            </a>"""
        more_html = f"""
        <div class="section-rule">
            <h2>अन्य खबरें / More News</h2>
            <span>सभी समाचार</span>
        </div>
        <div class="archive-grid">{cards}</div>
        """

    html_template = f"""<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>अबतक24.in (Aabtak24.in) | ताज़ा हिंदी समाचार और देश-दुनिया की खबरें</title>
    <meta name="description" content="अबतक24.in (Aabtak24.in) पर पढ़ें देश-विदेश, टेक्नोलॉजी, अर्थव्यवस्था, राजनीति और मनोरंजन की ताज़ा हिंदी खबरें।">
    <link rel="canonical" href="{SITE_URL}/">
    <meta property="og:type" content="website">
    <meta property="og:title" content="अबतक24.in | हिंदी न्यूज़ पोर्टल">
    <meta property="og:url" content="{SITE_URL}/">
{get_head_links(css_path="style.css", preload_img=hero_img)}
</head>
<body>
    <div class="top-bar">
        <span>📅 {today_str}</span>
        <span class="edition-tag">डिजिटल संस्करण • INDIA</span>
    </div>

{get_header_html(config, prefix="", active_file="index.html")}

    <main class="container">
        <div class="section-rule">
            <h2>प्रमुख सुर्खियां / Top Stories</h2>
            <span>ताज़ा अपडेट्स</span>
        </div>
        <div class="bento-grid">
            <a href="{hero_link}" class="hero-card">
                <div class="img-wrap"><img src="{hero_img}" alt="{hero['title']}" width="800" height="410" fetchpriority="high" loading="eager" decoding="sync"></div>
                <div class="card-body">
                    <div>
                        <span class="badge">{hero.get('category', 'बड़ी खबर')}</span>
                        <h1 class="hero-title">{hero['title']}</h1>
                    </div>
                    <div class="meta">प्रकाशित: {hero['date']} <span class="meta-dot"></span> 2 मिनट पठन</div>
                </div>
            </a>
            <div class="side-stack">{side_html}</div>
        </div>
        {more_html}
    </main>
</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_template)

    cat_files = build_category_pages(articles, today_str, config)
    custom_files = build_custom_pages(today_str, config)
    cleanup_retired_root_pages(cat_files + custom_files)
    build_seo_files(articles, config)
    print("✅ Live multi-page SEO & PageSpeed-optimized website generated!")


def collect_ranked_candidates(existing_links):
    """Pools unread stories from all feeds and ranks them using Source + Keyword Bias."""
    candidates = []
    for feed_info in RSS_FEEDS:
        source_name = feed_info["source"]
        feed_url = feed_info["url"]
        base_weight = feed_info.get("weight", 5.0)

        print(f"📡 Scanning {source_name}...")
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:10]:
                link = entry.get("link", "")
                if not link or link in existing_links:
                    continue

                title = entry.get("title", "").strip()
                raw_summary = entry.get("summary", "") or entry.get("description", "")
                summary = BeautifulSoup(raw_summary, "html.parser").get_text().strip()
                combined_text = f"{title} {summary}".lower()

                # 1. Hard filter out blocked topics
                if any(bad in combined_text for bad in BLOCKED_KEYWORDS):
                    continue

                # 2. Calculate Selection Bias Score
                score = base_weight
                for kw, kw_weight in KEYWORD_BIAS.items():
                    if re.search(r'\b' + re.escape(kw.lower()) + r'\b', combined_text):
                        score += kw_weight

                rss_img = get_rss_image(entry)
                if rss_img:
                    score += 1.0  # Slight bonus for stories with an original RSS photo

                candidates.append({
                    "title": title,
                    "summary": summary[:700],
                    "link": link,
                    "source_name": source_name,
                    "rss_image_url": rss_img,
                    "score": round(score, 2)
                })
        except Exception as e:
            print(f"⚠️ Failed to fetch {source_name}: {e}")

    # Sort highest bias score first
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def fetch_and_rewrite():
    config = load_site_config()
    ai_categories = [c["name"].strip() for c in config.get("categories", []) if c.get("active_for_ai", True)]
    if not ai_categories:
        ai_categories = [c["name"].strip() for c in config.get("categories", [])] or ["देश-विदेश"]
    category_choices_str = ", ".join(f'"{name}"' for name in ai_categories)

    articles = []
    if os.path.exists(ARCHIVE_FILE):
        try:
            with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
                articles = json.load(f)
        except Exception:
            articles = []

    for a in articles:
        a["slug"] = clean_slug(a.get("slug", "news-story"))
        if "iso_date" not in a:
            a["iso_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    existing_links = {a.get("original_link") for a in articles}
    target_count = 3 if len(articles) == 0 else 1
    added = 0

    for _ in range(target_count):
        candidates = collect_ranked_candidates(existing_links)
        if not candidates:
            print("ℹ️ All matching stories across all RSS feeds have already been published.")
            break

        # Shortlist the top 5 highest-scoring stories for Gemini to evaluate
        shortlist = candidates[:5]
        candidates_text = ""
        for idx, c in enumerate(shortlist):
            candidates_text += (
                f"\n[{idx}] Source: {c['source_name']} (Bias Score: {c['score']})\n"
                f"Title: {c['title']}\n"
                f"Summary: {c['summary']}\n"
            )

        prompt = f"""
        आप एक वरिष्ठ राष्ट्रवादी हिंदी समाचार संपादक (Senior Hindi News Editor) हैं।
        नीचे {len(shortlist)} अलग-अलग अंग्रेजी समाचारों के विकल्प ([0] से [{len(shortlist)-1}]) दिए गए हैं।

        1. समाचार चयन प्राथमिकता (Selection Bias):
        {SELECTION_BIAS_PROMPT}

        2. लेखन कोण और भाषा शैली (Framing & Phrasing Bias):
        {TONE_AND_FRAMING_BIAS}

        दिशानिर्देश (Instructions):
        - चरण 1: 'समाचार चयन प्राथमिकता' के आधार पर सबसे उपयुक्त 1 खबर चुनें और उसका नंबर "selected_index" में दें।
        - चरण 2: चुनी गई खबर को 'लेखन कोण और भाषा शैली' (Pro-Government, Pro-Right-Wing, Pro-India Framing) के अनुसार लगभग 300-350 शब्दों के प्रभावशाली और मौलिक हिंदी समाचार लेख में लिखें। मशीनी अनुवाद न करें।
        - चरण 3 (100% तथ्य सुरक्षा): मूल खबर के सभी तथ्य, नाम, संख्याएं और घटनाएं 100% सटीक और अपरिवर्तित रखें। कोई भी काल्पनिक तथ्य न जोड़ें।
        - चरण 4 (फोटो निर्णय): यदि खबर किसी राजनेता, व्यक्ति या विशेष घटना पर है, तो "image_strategy" को "original" रखें। यदि सामान्य टेक, AI, अर्थव्यवस्था, स्वास्थ्य आदि पर है, तो "stock" रखें।
        - केवल वैध JSON में उत्तर दें जिसमें ये 7 keys हों:
           - "selected_index": चुनी गई खबर का नंबर (integer, जैसे 0 या 1)
           - "title": राष्ट्रवादी और सरकार-समर्थक दृष्टिकोण को दर्शाता आकर्षक हिंदी शीर्षक
           - "category": केवल इन {len(ai_categories)} में से एक चुनें: {category_choices_str}
           - "slug": 4-5 शब्दों का अंग्रेजी SEO URL slug (केवल lowercase letters और hyphens, जैसे: "india-new-ai-policy-2026")
           - "content_html": हिंदी लेख का HTML कोड (<p>, <h2> टैग्स के साथ)
           - "image_strategy": "original" या "stock"
           - "stock_search_query": 2 शब्दों का अंग्रेजी सर्च कीवर्ड

        समाचार विकल्प (Candidates):
        {candidates_text}
        """

        article_data = generate_with_fallback(prompt)

        chosen_idx = article_data.get("selected_index", 0)
        if not isinstance(chosen_idx, int) or chosen_idx < 0 or chosen_idx >= len(shortlist):
            chosen_idx = 0
        chosen = shortlist[chosen_idx]

        print(f"🎯 Selected [{chosen_idx}] ({chosen['source_name']} | Score {chosen['score']}): {chosen['title']}")

        strategy = article_data.get("image_strategy", "original")
        search_query = article_data.get("stock_search_query", "technology")
        rss_image_url = chosen["rss_image_url"]
        source_name = chosen["source_name"]

        final_image_url, image_courtesy = (None, "")
        if strategy == "stock":
            final_image_url, image_courtesy = fetch_free_stock_image(search_query)
        if not final_image_url and rss_image_url:
            final_image_url = rss_image_url
            image_courtesy = f"फोटो साभार: {source_name} (RSS फ़ीड के माध्यम से)"
        if not final_image_url:
            final_image_url = "https://images.unsplash.com/photo-1504711434969-e33886168f5c?auto=format&fit=crop&w=1200&q=80"
            image_courtesy = "फोटो साभार: Unsplash (प्रतीकात्मक तस्वीर)"

        validated_category = normalize_ai_category(
            article_data.get("category"),
            config,
            ai_categories
        )

        new_article = {
            "title": article_data["title"],
            "category": validated_category,
            "slug": clean_slug(article_data.get("slug", "hindi-news")),
            "content_html": article_data["content_html"],
            "image_url": final_image_url,
            "image_courtesy": image_courtesy,
            "original_link": chosen["link"],
            "source_name": source_name,
            "date": datetime.now().strftime("%d %b %Y, %I:%M %p"),
            "iso_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        }

        articles.insert(0, new_article)
        existing_links.add(chosen["link"])
        added += 1
        time.sleep(2)

    if added == 0:
        print("ℹ️ All stories across all RSS feeds have already been published. No new articles to generate.")

    articles = articles[:100]
    with open(ARCHIVE_FILE, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)

    build_website_html(articles, config)


if __name__ == "__main__":
    if "--rebuild-only" in sys.argv:
        if os.path.exists(ARCHIVE_FILE):
            with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
                articles = json.load(f)
            for a in articles:
                a["slug"] = clean_slug(a.get("slug", "news-story"))
                if "iso_date" not in a:
                    a["iso_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
            build_website_html(articles)
        else:
            print(f"ℹ️ {ARCHIVE_FILE} not found — nothing to rebuild.")
    else:
        fetch_and_rewrite()
