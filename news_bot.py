import os
import re
import json
import time
from datetime import datetime, timezone
import requests
import feedparser
from bs4 import BeautifulSoup
from google import genai

# 1. Load Gemini Secret Key
api_key = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=api_key)

# 2. Configuration & Domain Settings
SITE_URL = "https://aabtak24.in"
SITE_NAME = "अबतक24.in (Aabtak24.in)"
RSS_URL = "https://feeds.bbci.co.uk/news/world/asia/india/rss.xml"
SOURCE_NAME = "BBC News"
ARCHIVE_FILE = "articles_db.json"
NEWS_DIR = "news"

# 3. Navigation Categories & Aliases (Matches both new & existing articles)
CATEGORIES = [
    {"name": "देश-विदेश", "file": "desh-videsh.html", "aliases": ["देश-विदेश", "देश", "विदेश", "प्रमुख समाचार", "बड़ी खबर"]},
    {"name": "टेक्नोलॉजी", "file": "technology.html", "aliases": ["टेक्नोलॉजी", "विज्ञान", "AI"]},
    {"name": "अर्थव्यवस्था", "file": "economy.html", "aliases": ["अर्थव्यवस्था", "बिजनेस एवं ट्रेंड्स", "बिजनेस", "व्यापार"]},
    {"name": "राजनीति", "file": "politics.html", "aliases": ["राजनीति", "चुनाव"]},
    {"name": "मनोरंजन", "file": "entertainment.html", "aliases": ["मनोरंजन", "बॉलीवुड", "सिनेमा", "खेल"]},
]

# 4. Model Fallback List
MODELS_TO_TRY = [
    "gemini-3.6-flash",
    "gemini-3.8-flash",
    "gemma-4-31b-it"
]

def clean_slug(slug_text):
    """Ensures the SEO slug only contains clean URL-safe characters."""
    slug = re.sub(r'[^a-zA-Z0-9\-]', '', slug_text.lower().replace(' ', '-'))
    slug = re.sub(r'-+', '-', slug).strip('-')
    return slug or f"news-{int(time.time())}"

def generate_with_fallback(prompt):
    for model_name in MODELS_TO_TRY:
        try:
            print(f"Attempting to write Hindi article using {model_name}...")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config={"response_mime_type": "application/json"}
            )
            print(f"✅ Success with {model_name}!")
            return json.loads(response.text)
        except Exception as e:
            print(f"⚠️ {model_name} busy ({e}). Switching to backup model...")
            time.sleep(2)
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
    return None

# Shared Verge x Guardian CSS
SHARED_CSS = """
    :root {
        --bg: #FAF6F0;
        --card: #FFFFFF;
        --card-subtle: #F3EDE2;
        --text-main: #141210;
        --text-muted: #6E655F;
        --accent: #FF4C00;
        --accent-dark: #C83700;
        --accent-tint: #FFE8DC;
        --border: #E2D9CC;
        --border-heavy: #141210;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
        background-color: var(--bg);
        color: var(--text-main);
        font-family: 'Noto Sans Devanagari', sans-serif;
        line-height: 1.7;
        -webkit-font-smoothing: antialiased;
    }
    a { text-decoration: none; color: inherit; }
    .top-bar {
        background: #141210;
        color: #EAE3DC;
        font-size: 12px;
        font-weight: 600;
        letter-spacing: 0.5px;
        padding: 8px 5%;
        display: flex;
        justify-content: space-between;
        align-items: center;
        border-bottom: 3px solid var(--accent);
    }
    .top-bar .edition-tag {
        color: var(--accent);
        text-transform: uppercase;
        font-weight: 700;
    }
    header {
        background: rgba(250, 246, 240, 0.94);
        backdrop-filter: blur(12px);
        border-bottom: 2px solid var(--border-heavy);
        position: sticky;
        top: 0;
        z-index: 100;
        padding: 18px 5%;
        display: flex;
        justify-content: space-between;
        align-items: center;
        flex-wrap: wrap;
        gap: 12px;
    }
    .logo {
        font-family: 'Mukta', sans-serif;
        font-size: 36px;
        font-weight: 800;
        color: var(--text-main);
        letter-spacing: -0.5px;
        line-height: 1;
        display: flex;
        align-items: baseline;
    }
    .logo span { color: var(--accent); }
    .logo .dot-in {
        font-size: 20px;
        color: var(--text-muted);
        font-weight: 700;
        margin-left: 2px;
    }
    .nav-pills { display: flex; gap: 22px; flex-wrap: wrap; }
    .nav-pills a {
        font-weight: 700;
        font-size: 15px;
        color: var(--text-main);
        padding: 4px 0;
        position: relative;
        transition: color 0.2s ease;
    }
    .nav-pills a::after {
        content: '';
        position: absolute;
        bottom: -2px;
        left: 0;
        width: 0%;
        height: 3px;
        background: var(--accent);
        transition: width 0.25s ease;
    }
    .nav-pills a:hover, .nav-pills a.active { color: var(--accent); }
    .nav-pills a:hover::after, .nav-pills a.active::after { width: 100%; }
    .container { max-width: 1240px; margin: 36px auto 60px; padding: 0 20px; }
    .section-rule {
        border-top: 4px solid var(--border-heavy);
        border-bottom: 1px solid var(--border);
        padding: 8px 0;
        margin-bottom: 28px;
        display: flex;
        justify-content: space-between;
        align-items: center;
    }
    .section-rule h2 {
        font-family: 'Mukta', sans-serif;
        font-size: 16px;
        font-weight: 800;
        text-transform: uppercase;
        color: var(--accent);
        letter-spacing: 0.8px;
    }
    .section-rule span { font-size: 12px; color: var(--text-muted); font-weight: 600; }
    .bento-grid {
        display: grid;
        grid-template-columns: 1.75fr 1fr;
        gap: 32px;
        margin-bottom: 48px;
    }
    @media (max-width: 900px) {
        .bento-grid { grid-template-columns: 1fr; }
        .nav-pills { gap: 14px; }
        .logo { font-size: 30px; }
    }
    .hero-card {
        background: var(--card);
        border: 1px solid var(--border);
        border-top: 4px solid var(--accent);
        border-radius: 4px;
        overflow: hidden;
        transition: transform 0.25s ease, box-shadow 0.25s ease;
        display: flex;
        flex-direction: column;
    }
    .hero-card:hover {
        transform: translateY(-3px);
        box-shadow: 0 16px 32px -8px rgba(20, 18, 16, 0.12);
    }
    .img-wrap { overflow: hidden; position: relative; background: #201D1A; }
    .img-wrap img {
        width: 100%;
        height: 410px;
        object-fit: cover;
        display: block;
        transition: transform 0.45s cubic-bezier(0.16, 1, 0.3, 1);
    }
    .hero-card:hover .img-wrap img { transform: scale(1.03); }
    .card-body {
        padding: 28px 30px 32px;
        flex: 1;
        display: flex;
        flex-direction: column;
        justify-content: space-between;
    }
    .badge {
        display: inline-block;
        background: var(--accent-tint);
        color: var(--accent-dark);
        font-size: 12px;
        font-weight: 800;
        text-transform: uppercase;
        padding: 3px 10px;
        border-left: 3px solid var(--accent);
        margin-bottom: 12px;
        letter-spacing: 0.4px;
        width: fit-content;
    }
    .hero-title {
        font-family: 'Mukta', sans-serif;
        font-size: 32px;
        font-weight: 800;
        line-height: 1.25;
        margin-bottom: 16px;
        color: var(--text-main);
        transition: color 0.2s ease;
    }
    .hero-card:hover .hero-title { color: var(--accent-dark); }
    .meta {
        font-size: 13px;
        font-weight: 500;
        color: var(--text-muted);
        display: flex;
        align-items: center;
        gap: 8px;
    }
    .meta-dot {
        width: 4px;
        height: 4px;
        background: var(--accent);
        border-radius: 50%;
        display: inline-block;
    }
    .side-stack { display: flex; flex-direction: column; gap: 16px; }
    .side-card {
        background: var(--card);
        border: 1px solid var(--border);
        border-top: 3px solid var(--border-heavy);
        border-radius: 4px;
        display: grid;
        grid-template-columns: 128px 1fr;
        gap: 16px;
        padding: 16px;
        align-items: center;
        transition: border-color 0.2s ease, background-color 0.2s ease, transform 0.2s ease;
    }
    .side-card:hover {
        border-top-color: var(--accent);
        background: #FFFDFB;
        transform: translateX(3px);
    }
    .side-card img {
        width: 128px;
        height: 94px;
        object-fit: cover;
        border-radius: 2px;
        background: #201D1A;
    }
    .side-title {
        font-family: 'Mukta', sans-serif;
        font-size: 18px;
        line-height: 1.3;
        font-weight: 700;
        color: var(--text-main);
        margin-bottom: 6px;
        transition: color 0.2s ease;
    }
    .side-card:hover .side-title { color: var(--accent); }

    /* Archive & Category Grid */
    .archive-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
        gap: 24px;
    }
    .archive-card {
        background: var(--card);
        border: 1px solid var(--border);
        border-top: 3px solid var(--border-heavy);
        border-radius: 4px;
        overflow: hidden;
        transition: transform 0.2s ease;
        display: flex;
        flex-direction: column;
    }
    .archive-card:hover {
        transform: translateY(-3px);
        border-top-color: var(--accent);
    }
    .archive-card img {
        width: 100%;
        height: 200px;
        object-fit: cover;
        background: #201D1A;
    }
    .archive-card-body { padding: 18px; }

    /* Single Article & Static Page Layout */
    .article-box {
        max-width: 780px;
        margin: 0 auto;
        background: var(--card);
        padding: 48px 52px;
        border-radius: 4px;
        border: 1px solid var(--border);
        border-top: 5px solid var(--accent);
        box-shadow: 0 10px 30px -10px rgba(20, 18, 16, 0.07);
    }
    @media (max-width: 640px) { .article-box { padding: 28px 20px; } }
    .back-btn {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        margin-bottom: 24px;
        color: var(--text-main);
        background: var(--card-subtle);
        padding: 7px 14px;
        border-radius: 3px;
        font-size: 14px;
        font-weight: 700;
        border-left: 3px solid var(--accent);
        transition: background 0.2s ease, color 0.2s ease;
    }
    .back-btn:hover { background: var(--accent); color: #FFFFFF; }
    .single-title {
        font-family: 'Mukta', sans-serif;
        font-size: 38px;
        font-weight: 800;
        line-height: 1.22;
        margin: 8px 0 18px;
        color: var(--text-main);
    }
    .single-meta {
        padding-bottom: 16px;
        border-bottom: 1px solid var(--border);
    }
    .single-figure { margin: 24px 0 28px; }
    .single-figure img {
        width: 100%;
        max-height: 460px;
        object-fit: cover;
        border-radius: 3px;
        display: block;
    }
    .image-credit {
        font-size: 12px;
        color: var(--text-muted);
        text-align: right;
        margin-top: 8px;
        padding-bottom: 8px;
        border-bottom: 1px solid var(--border);
        font-style: italic;
    }
    .article-content {
        margin-top: 24px;
        font-size: 19px;
        line-height: 1.9;
        color: #1F1B18;
    }
    .article-content p { margin-bottom: 20px; }
    .article-content h3 {
        font-family: 'Mukta', sans-serif;
        font-size: 25px;
        font-weight: 800;
        margin: 34px 0 12px;
        padding-left: 12px;
        border-left: 4px solid var(--accent);
        color: var(--text-main);
        line-height: 1.3;
    }
"""

def get_header_html(prefix="", active_file=""):
    """Generates the header with working links from both root ('') and /news/ ('../')."""
    home_cls = ' class="active"' if active_file == "index.html" else ""
    nav_links = f'<a href="{prefix}index.html"{home_cls}>मुख्य खबरें</a>\n'
    
    for cat in CATEGORIES:
        cls = ' class="active"' if active_file == cat["file"] else ""
        nav_links += f'            <a href="{prefix}{cat["file"]}"{cls}>{cat["name"]}</a>\n'
        
    about_cls = ' class="active"' if active_file == "about.html" else ""
    nav_links += f'            <a href="{prefix}about.html"{about_cls}>हमारे बारे में</a>'

    return f"""    <header>
        <a href="{prefix}index.html" class="logo">अब<span>तक24</span><span class="dot-in">.in</span></a>
        <nav class="nav-pills">
            {nav_links}
        </nav>
    </header>"""

def build_individual_article_page(art, today_str):
    """Creates a dedicated SEO-optimized HTML page for a single article inside /news/."""
    os.makedirs(NEWS_DIR, exist_ok=True)
    slug = art["slug"]
    canonical_url = f"{SITE_URL}/news/{slug}.html"
    
    plain_text = BeautifulSoup(art["content_html"], "html.parser").get_text().strip()
    meta_desc = plain_text[:155].replace('"', "'") + "..."

    schema_ld = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": art["title"],
        "image": [art["image_url"]],
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
    <meta property="og:image" content="{art['image_url']}">
    <meta property="og:url" content="{canonical_url}">
    <meta property="og:site_name" content="अबतक24.in">
    <meta name="twitter:card" content="summary_large_image">

    <!-- Google News JSON-LD Schema -->
    <script type="application/ld+json">{schema_json}</script>

    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Mukta:wght@600;700;800&family=Noto+Sans+Devanagari:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>{SHARED_CSS}</style>
</head>
<body>
    <div class="top-bar">
        <span>📅 {today_str}</span>
        <span class="edition-tag">डिजिटल संस्करण • INDIA</span>
    </div>

{get_header_html(prefix="../")}

    <main class="container">
        <article class="article-box">
            <a href="../index.html" class="back-btn">← मुख्य पृष्ठ पर वापस जाएं</a>
            <div><span class="badge">{art.get('category', 'ताज़ा खबर')}</span></div>
            <h1 class="single-title">{art['title']}</h1>
            <div class="meta single-meta">प्रकाशित: {art['date']} <span class="meta-dot"></span> अबतक24 न्यूज़ डेस्क</div>
            <figure class="single-figure">
                <img src="{art['image_url']}" alt="{art['title']}" />
                <figcaption class="image-credit">{art['image_courtesy']}</figcaption>
            </figure>
            <div class="article-content">
                {art['content_html']}
            </div>
        </article>
    </main>
</body>
</html>"""

    filepath = os.path.join(NEWS_DIR, f"{slug}.html")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(article_html)

def build_category_pages(articles, today_str):
    """Creates a dedicated HTML page for each category in the header."""
    for cat in CATEGORIES:
        cat_name = cat["name"]
        cat_file = cat["file"]
        aliases = cat["aliases"]
        
        filtered = [
            a for a in articles
            if a.get("category", "").strip() in aliases
            or any(alias in a.get("category", "") for alias in aliases)
        ]
        
        cards_html = ""
        if filtered:
            for art in filtered:
                art_link = f"news/{art['slug']}.html"
                cards_html += f"""
                <a href="{art_link}" class="archive-card">
                    <img src="{art['image_url']}" alt="{art['title']}">
                    <div class="archive-card-body">
                        <span class="badge">{art.get('category', cat_name)}</span>
                        <h3 class="side-title">{art['title']}</h3>
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
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Mukta:wght@600;700;800&family=Noto+Sans+Devanagari:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>{SHARED_CSS}</style>
</head>
<body>
    <div class="top-bar">
        <span>📅 {today_str}</span>
        <span class="edition-tag">डिजिटल संस्करण • INDIA</span>
    </div>

{get_header_html(prefix="", active_file=cat_file)}

    <main class="container">
        <div class="section-rule">
            <h2>{cat_name} / Category News</h2>
            <span>कुल {len(filtered)} समाचार</span>
        </div>
        <div class="archive-grid">{cards_html}</div>
    </main>
</body>
</html>"""

        with open(cat_file, "w", encoding="utf-8") as f:
            f.write(page_html)
    print("✅ Generated all category HTML pages!")

def build_static_pages(today_str):
    """Creates static pages like About Us (about.html) ONLY if they don't already exist."""
    if os.path.exists("about.html"):
        print("ℹ️ about.html already exists — skipping so custom edits are preserved!")
        return
    about_html = f"""<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>हमारे बारे में (About Us) | अबतक24.in</title>
    <meta name="description" content="अबतक24.in (Aabtak24.in) — भारत का आधुनिक डिजिटल हिंदी समाचार पोर्टल। जानें हमारे संपादकीय मानक और संपर्क सूत्र।">
    <link rel="canonical" href="{SITE_URL}/about.html">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Mukta:wght@600;700;800&family=Noto+Sans+Devanagari:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>{SHARED_CSS}</style>
</head>
<body>
    <div class="top-bar">
        <span>📅 {today_str}</span>
        <span class="edition-tag">डिजिटल संस्करण • INDIA</span>
    </div>

{get_header_html(prefix="", active_file="about.html")}

    <main class="container">
        <article class="article-box">
            <a href="index.html" class="back-btn">← मुख्य पृष्ठ पर वापस जाएं</a>
            <div><span class="badge">हमारे बारे में / About Us</span></div>
            <h1 class="single-title">अबतक24.in (Aabtak24.in) — सटीक और निष्पक्ष डिजिटल हिंदी पत्रकारिता</h1>
            <div class="article-content">
                <p><strong>अबतक24.in (Aabtak24.in)</strong> भारत का एक आधुनिक डिजिटल हिंदी समाचार पोर्टल है। हमारा उद्देश्य देश-विदेश, टेक्नोलॉजी, अर्थव्यवस्था, राजनीति और मनोरंजन की प्रमुख खबरों को सरल, स्पष्ट और तथ्यपूर्ण हिंदी भाषा में पाठकों तक पहुँचाना है।</p>
                <h3>हमारा संपादकीय मानक (Editorial Policy)</h3>
                <p>हम विश्वसनीय राष्ट्रीय और अंतरराष्ट्रीय समाचार स्रोतों से तथ्यों का संकलन कर उन्हें पाठकों के लिए सहज और प्रवाहपूर्ण पत्रकारिता शैली में प्रस्तुत करते हैं तथा प्रत्येक रिपोर्ट के साथ उचित फोटो और स्रोत साभार (Attribution) प्रदान करते हैं।</p>
                <h3>संपर्क करें (Contact Us)</h3>
                <p>किसी भी सुझाव, प्रतिक्रिया या संपादकीय प्रश्न के लिए आप हमसे संपर्क कर सकते हैं:<br>
                <strong>वेबसाइट:</strong> <a href="{SITE_URL}" style="color: var(--accent); font-weight: 700;">aabtak24.in</a><br>
                <strong>ईमेल:</strong> contact@aabtak24.in</p>
            </div>
        </article>
    </main>
</body>
</html>"""

    with open("about.html", "w", encoding="utf-8") as f:
        f.write(about_html)
    print("✅ Generated about.html static page!")

def build_seo_files(articles):
    """Generates sitemap.xml and robots.txt for Google Search Console."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    urls_xml = f"""  <url>
    <loc>{SITE_URL}/</loc>
    <lastmod>{now_iso}</lastmod>
    <changefreq>hourly</changefreq>
    <priority>1.0</priority>
  </url>\n"""

    # Include Category Pages & About Page in Sitemap
    for cat in CATEGORIES:
        urls_xml += f"""  <url>
    <loc>{SITE_URL}/{cat['file']}</loc>
    <lastmod>{now_iso}</lastmod>
    <changefreq>hourly</changefreq>
    <priority>0.9</priority>
  </url>\n"""

    urls_xml += f"""  <url>
    <loc>{SITE_URL}/about.html</loc>
    <lastmod>{now_iso}</lastmod>
    <changefreq>monthly</changefreq>
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
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
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

def build_website_html(articles):
    """Generates index.html, category pages, about page, and individual article pages."""
    today_str = datetime.now().strftime("%d %B %Y")
    if not articles:
        return

    # 1. Build every individual article page first
    for art in articles:
        build_individual_article_page(art, today_str)

    # 2. Build Homepage Hero + Side Stack HTML with real <a href> links
    hero = articles[0]
    hero_link = f"news/{hero['slug']}.html"

    side_html = ""
    for art in articles[1:5]:
        art_link = f"news/{art['slug']}.html"
        side_html += f"""
        <a href="{art_link}" class="side-card">
            <img src="{art['image_url']}" alt="{art['title']}">
            <div>
                <span class="badge">{art.get('category', 'प्रमुख समाचार')}</span>
                <h3 class="side-title">{art['title']}</h3>
                <div class="meta">{art['date']}</div>
            </div>
        </a>"""

    more_html = ""
    if len(articles) > 5:
        cards = ""
        for art in articles[5:]:
            art_link = f"news/{art['slug']}.html"
            cards += f"""
            <a href="{art_link}" class="archive-card">
                <img src="{art['image_url']}" alt="{art['title']}">
                <div class="archive-card-body">
                    <span class="badge">{art.get('category', 'समाचार')}</span>
                    <h3 class="side-title">{art['title']}</h3>
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
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Mukta:wght@600;700;800&family=Noto+Sans+Devanagari:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>{SHARED_CSS}</style>
</head>
<body>
    <div class="top-bar">
        <span>📅 {today_str}</span>
        <span class="edition-tag">डिजिटल संस्करण • INDIA</span>
    </div>

{get_header_html(prefix="", active_file="index.html")}

    <main class="container">
        <div class="section-rule">
            <h2>प्रमुख सुर्खियां / Top Stories</h2>
            <span>ताज़ा अपडेट्स</span>
        </div>
        <div class="bento-grid">
            <a href="{hero_link}" class="hero-card">
                <div class="img-wrap"><img src="{hero['image_url']}" alt="{hero['title']}"></div>
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

    # 3. Generate Category Pages, Static Pages, and SEO Sitemap
    build_category_pages(articles, today_str)
    build_static_pages(today_str)
    build_seo_files(articles)
    print("✅ Live multi-page SEO website generated!")

def fetch_and_rewrite():
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

    print(f"Fetching latest English news from {SOURCE_NAME}...")
    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        print("No news entries found!")
        return

    target_count = 3 if len(articles) == 0 else 1
    added = 0

    for entry in feed.entries:
        if entry.link in existing_links:
            continue
        if added >= target_count:
            break

        original_title = entry.title
        original_summary = BeautifulSoup(entry.summary, "html.parser").get_text()
        rss_image_url = get_rss_image(entry)

        prompt = f"""
        आप एक वरिष्ठ हिंदी समाचार संपादक (Senior Hindi News Editor) हैं।
        नीचे दिए गए अंग्रेजी समाचार को पढ़कर एक मौलिक, आकर्षक और प्रभावशाली हिंदी समाचार लेख (लगभग 300-350 शब्द) लिखें।

        दिशानिर्देश:
        1. मशीनी अनुवाद न करें। खबर को सहज हिंदी पत्रकारिता की भाषा में दोबारा लिखें।
        2. सभी तथ्य 100% सटीक रखें।
        3. फोटो का निर्णय: यदि खबर किसी राजनेता, व्यक्ति या विशेष घटना पर है, तो "image_strategy" को "original" रखें। यदि सामान्य टेक, AI, अर्थव्यवस्था, स्वास्थ्य आदि पर है, तो "stock" रखें।
        4. केवल वैध JSON में उत्तर दें जिसमें ये 6 keys हों:
           - "title": हिंदी शीर्षक
           - "category": केवल इन 5 में से एक चुनें: "देश-विदेश", "टेक्नोलॉजी", "अर्थव्यवस्था", "राजनीति", "मनोरंजन"
           - "slug": 4-5 शब्दों का अंग्रेजी SEO URL slug (केवल lowercase letters और hyphens, जैसे: "india-new-ai-policy-2026")
           - "content_html": हिंदी लेख का HTML कोड (<p>, <h3> टैग्स के साथ)
           - "image_strategy": "original" या "stock"
           - "stock_search_query": 2 शब्दों का अंग्रेजी सर्च कीवर्ड

        Original English Title: {original_title}
        Original English Details: {original_summary}
        """

        article_data = generate_with_fallback(prompt)
        strategy = article_data.get("image_strategy", "original")
        search_query = article_data.get("stock_search_query", "technology")

        final_image_url, image_courtesy = (None, "")
        if strategy == "stock":
            final_image_url, image_courtesy = fetch_free_stock_image(search_query)
        if not final_image_url and rss_image_url:
            final_image_url = rss_image_url
            image_courtesy = f"फोटो साभार: {SOURCE_NAME} (RSS फ़ीड के माध्यम से)"
        if not final_image_url:
            final_image_url = "https://images.unsplash.com/photo-1504711434969-e33886168f5c?auto=format&fit=crop&w=1200&q=80"
            image_courtesy = "फोटो साभार: Unsplash (प्रतीकात्मक तस्वीर)"

        new_article = {
            "title": article_data["title"],
            "category": article_data.get("category", "देश-विदेश"),
            "slug": clean_slug(article_data.get("slug", "hindi-news")),
            "content_html": article_data["content_html"],
            "image_url": final_image_url,
            "image_courtesy": image_courtesy,
            "original_link": entry.link,
            "source_name": SOURCE_NAME,
            "date": datetime.now().strftime("%d %b %Y, %I:%M %p"),
            "iso_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        }

        articles.insert(0, new_article)
        added += 1
        time.sleep(2)

    articles = articles[:100]
    with open(ARCHIVE_FILE, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)

    build_website_html(articles)

if __name__ == "__main__":
    fetch_and_rewrite()
