import os
import json
import time
from datetime import datetime
import requests
import feedparser
from bs4 import BeautifulSoup
from google import genai

# 1. Load Gemini Secret Key
api_key = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=api_key)

# 2. Pick an English News RSS Feed
RSS_URL = "https://feeds.bbci.co.uk/news/world/asia/india/rss.xml"
SOURCE_NAME = "BBC News"
ARCHIVE_FILE = "articles_db.json"

# 3. Model Fallback List
MODELS_TO_TRY = [
    "gemini-3.6-flash",
    "gemini-3.8-flash",
    "gemma-4-31b-it"
]

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

def build_website_html(articles):
    """Generates a complete Astra-style modern Hindi News Portal in index.html"""
    articles_json = json.dumps(articles, ensure_ascii=False)
    today_str = datetime.now().strftime("%d %B %Y")

    html_template = f"""<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>भारत समाचार (Bharat Samachar) | AI हिंदी न्यूज़ पोर्टल</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Mukta:wght@600;700;800&family=Noto+Sans+Devanagari:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg: #F8FAFC;
            --card: #FFFFFF;
            --text-main: #0F172A;
            --text-muted: #64748B;
            --accent: #E11D48;
            --border: #E2E8F0;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background-color: var(--bg);
            color: var(--text-main);
            font-family: 'Noto Sans Devanagari', sans-serif;
            line-height: 1.7;
        }}
        /* Astra-Style Top Utility & Sticky Header */
        .top-bar {{
            background: #0F172A;
            color: #CBD5E1;
            font-size: 13px;
            padding: 6px 5%;
            display: flex;
            justify-content: space-between;
        }}
        header {{
            background: rgba(255, 255, 255, 0.92);
            backdrop-filter: blur(10px);
            border-bottom: 1px solid var(--border);
            position: sticky;
            top: 0;
            z-index: 100;
            padding: 16px 5%;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .logo {{
            font-family: 'Mukta', sans-serif;
            font-size: 30px;
            font-weight: 800;
            color: var(--text-main);
            text-decoration: none;
            cursor: pointer;
        }}
        .logo span {{ color: var(--accent); }}
        .nav-pills span {{
            margin-left: 18px;
            font-weight: 600;
            font-size: 15px;
            color: var(--text-muted);
            cursor: pointer;
        }}
        .nav-pills span:hover {{ color: var(--accent); }}
        .container {{ max-width: 1200px; margin: 32px auto; padding: 0 20px; }}

        /* Astra + Spectra Hero Bento Grid */
        .bento-grid {{
            display: grid;
            grid-template-columns: 1.8fr 1fr;
            gap: 28px;
            margin-bottom: 40px;
        }}
        @media (max-width: 860px) {{ .bento-grid {{ grid-template-columns: 1fr; }} }}
        .card {{
            background: var(--card);
            border-radius: 12px;
            border: 1px solid var(--border);
            overflow: hidden;
            cursor: pointer;
            transition: box-shadow 0.25s ease, transform 0.25s ease;
        }}
        .card:hover {{ box-shadow: 0 12px 24px -6px rgba(15, 23, 42, 0.08); }}
        .img-wrap {{ overflow: hidden; position: relative; background: #e2e8f0; }}
        .img-wrap img {{
            width: 100%;
            height: 380px;
            object-fit: cover;
            display: block;
            transition: transform 0.35s ease;
        }}
        .card:hover .img-wrap img {{ transform: scale(1.04); }}
        .card-body {{ padding: 24px; }}
        .badge {{
            display: inline-block;
            background: #FFE4E6;
            color: var(--accent);
            font-size: 12px;
            font-weight: 700;
            text-transform: uppercase;
            padding: 3px 10px;
            border-radius: 4px;
            margin-bottom: 10px;
        }}
        .hero-title {{
            font-family: 'Mukta', sans-serif;
            font-size: 28px;
            line-height: 1.3;
            margin-bottom: 12px;
        }}
        .meta {{ font-size: 13px; color: var(--text-muted); }}

        /* Right Sidebar Stack */
        .side-stack {{ display: flex; flex-direction: column; gap: 16px; }}
        .side-card {{
            display: grid;
            grid-template-columns: 120px 1fr;
            gap: 14px;
            padding: 14px;
            align-items: center;
        }}
        .side-card img {{
            width: 120px;
            height: 90px;
            object-fit: cover;
            border-radius: 8px;
        }}
        .side-title {{
            font-family: 'Mukta', sans-serif;
            font-size: 17px;
            line-height: 1.35;
            font-weight: 700;
        }}

        /* Single Post Focused Reading View (740px Astra Layout) */
        #article-view {{
            display: none;
            max-width: 740px;
            margin: 0 auto;
            background: var(--card);
            padding: 40px;
            border-radius: 12px;
            border: 1px solid var(--border);
        }}
        .back-btn {{
            display: inline-block;
            margin-bottom: 20px;
            color: var(--accent);
            font-weight: 700;
            cursor: pointer;
        }}
        .single-title {{
            font-family: 'Mukta', sans-serif;
            font-size: 34px;
            line-height: 1.25;
            margin: 10px 0 16px;
        }}
        .single-figure img {{
            width: 100%;
            max-height: 440px;
            object-fit: cover;
            border-radius: 10px;
            margin-top: 16px;
        }}
        .image-credit {{
            font-size: 12px;
            color: var(--text-muted);
            text-align: right;
            margin-top: 6px;
            font-style: italic;
        }}
        .article-content {{
            margin-top: 24px;
            font-size: 18px;
            line-height: 1.85;
        }}
        .article-content p {{ margin-bottom: 18px; }}
        .article-content h3 {{
            font-family: 'Mukta', sans-serif;
            font-size: 23px;
            margin: 28px 0 10px;
        }}
        .source-box {{
            margin-top: 30px;
            padding-top: 16px;
            border-top: 1px solid var(--border);
            font-size: 14px;
            color: var(--text-muted);
        }}
    </style>
</head>
<body>

    <div class="top-bar">
        <span>📅 {today_str} | Live GitHub Actions Preview</span>
        <span></span>
    </div>

    <header>
        <div class="logo" onclick="showHome()">अब<span>तक24</span>.in</div>
        <div class="nav-pills">
            <span onclick="showHome()">मुख्य खबरें</span>
            <span onclick="showHome()">देश-विदेश</span>
            <span onclick="showHome()">टेक्नोलॉजी</span>
        </div>
    </header>

    <div class="container">
        <!-- HOME BENTO GRID -->
        <div id="home-view">
            <div class="bento-grid" id="bento-container"></div>
        </div>

        <!-- SINGLE ARTICLE VIEW -->
        <div id="article-view">
            <div class="back-btn" onclick="showHome()">← मुख्य पृष्ठ पर वापस जाएं (Back to Home)</div>
            <div class="badge" id="art-badge">ताज़ा खबर</div>
            <h1 class="single-title" id="art-title"></h1>
            <div class="meta" id="art-meta"></div>
            <figure class="single-figure">
                <img id="art-img" src="" alt="" />
                <figcaption class="image-credit" id="art-credit"></figcaption>
            </figure>
            <div class="article-content" id="art-body"></div>
            <div class="source-box" id="art-source"></div>
        </div>
    </div>

    <script>
        const articles = {articles_json};

        function renderHome() {{
            const container = document.getElementById('bento-container');
            if (!articles.length) return;

            const hero = articles[0];
            let sideHtml = '';
            articles.slice(1, 5).forEach((art, idx) => {{
                sideHtml += `
                <div class="card side-card" onclick="openArticle(${{idx + 1}})">
                    <img src="${{art.image_url}}" alt="">
                    <div>
                        <span class="badge">${{art.category || 'प्रमुख समाचार'}}</span>
                        <div class="side-title">${{art.title}}</div>
                        <div class="meta">${{art.date}}</div>
                    </div>
                </div>`;
            }});

            container.innerHTML = `
                <div class="card" onclick="openArticle(0)">
                    <div class="img-wrap"><img src="${{hero.image_url}}" alt=""></div>
                    <div class="card-body">
                        <span class="badge">${{hero.category || 'बड़ी खबर'}}</span>
                        <h2 class="hero-title">${{hero.title}}</h2>
                        <div class="meta">प्रकाशित: ${{hero.date}} • 2 मिनट पठन</div>
                    </div>
                </div>
                <div class="side-stack">${{sideHtml}}</div>
            `;
        }}

        function openArticle(index) {{
            const art = articles[index];
            document.getElementById('art-badge').innerText = art.category || 'ताज़ा खबर';
            document.getElementById('art-title').innerText = art.title;
            document.getElementById('art-meta').innerText = `प्रकाशित: ${{art.date}} | SEO Slug: /${{art.slug}}`;
            document.getElementById('art-img').src = art.image_url;
            document.getElementById('art-credit').innerText = art.image_courtesy;
            document.getElementById('art-body').innerHTML = art.content_html;
            document.getElementById('art-source').innerHTML = `<em>स्रोत और साभार: यह रिपोर्ट <a href="${{art.original_link}}" target="_blank" rel="nofollow">${{art.source_name}}</a> की मूल खबर के तथ्यों पर आधारित है।</em>`;

            document.getElementById('home-view').style.display = 'none';
            document.getElementById('article-view').style.display = 'block';
            window.scrollTo(0, 0);
        }}

        function showHome() {{
            document.getElementById('article-view').style.display = 'none';
            document.getElementById('home-view').style.display = 'block';
            window.scrollTo(0, 0);
        }}

        renderHome();
    </script>
</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_template)
    print("✅ Live website generated in index.html!")

def fetch_and_rewrite():
    # Load existing articles so your site builds an archive over time
    articles = []
    if os.path.exists(ARCHIVE_FILE):
        try:
            with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
                articles = json.load(f)
        except Exception:
            articles = []

    existing_links = {a.get("original_link") for a in articles}

    print(f"Fetching latest English news from {SOURCE_NAME}...")
    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        print("No news entries found!")
        return

    # On first run, generate 3 stories to populate the Bento Grid; afterwards 1 per run
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
           - "category": हिंदी श्रेणी (जैसे: "देश-विदेश", "टेक्नोलॉजी", "अर्थव्यवस्था", "राजनीति")
           - "slug": अंग्रेजी SEO URL slug
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
            "category": article_data.get("category", "प्रमुख समाचार"),
            "slug": article_data["slug"],
            "content_html": article_data["content_html"],
            "image_url": final_image_url,
            "image_courtesy": image_courtesy,
            "original_link": entry.link,
            "source_name": SOURCE_NAME,
            "date": datetime.now().strftime("%d %b %Y, %I:%M %p")
        }

        articles.insert(0, new_article)
        added += 1
        time.sleep(2)

    # Keep latest 20 articles & save
    articles = articles[:20]
    with open(ARCHIVE_FILE, "w", encoding="utf-8") as f:
        json.dump(articles, f, ensure_ascii=False, indent=2)

    # Build the live HTML portal
    build_website_html(articles)

if __name__ == "__main__":
    fetch_and_rewrite()
