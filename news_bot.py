import os
import re
import sys
import copy
import html
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote, urlparse, parse_qs
import requests
import feedparser
from bs4 import BeautifulSoup

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:
    genai = None
    genai_types = None

GEMINI_HTTP_TIMEOUT_MS = 45_000
NIM_HTTP_TIMEOUT = (10, 45)
IMAGE_CHECK_TIMEOUT_SEC = 6


def build_google_client(key):
    """Creates a Google GenAI client with fail-fast HTTP timeouts when supported."""
    if not key or genai is None:
        return None
    if genai_types is not None and hasattr(genai_types, "HttpOptions"):
        http_kwargs = {"timeout": GEMINI_HTTP_TIMEOUT_MS}
        if hasattr(genai_types, "HttpRetryOptions"):
            try:
                http_kwargs["retry_options"] = genai_types.HttpRetryOptions(attempts=1)
            except Exception:
                pass
        try:
            return genai.Client(
                api_key=key,
                http_options=genai_types.HttpOptions(**http_kwargs),
            )
        except Exception:
            pass
    return genai.Client(api_key=key)


# 1. Load AI Provider Secret Keys
api_key = os.environ.get("GEMINI_API_KEY")
nvidia_api_key = os.environ.get("NVIDIA_API_KEY")
client = build_google_client(api_key) if api_key else None

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
            "title": "हमारे बारे में (About Us) | Aabtak24.in",
            "meta_description": "अबतक24.in (Aabtak24.in) — भारत का आधुनिक डिजिटल हिंदी समाचार पोर्टल। जानें हमारे संपादकीय मानक और संपर्क सूत्र।",
            "source_fragment": "pages/about.html",
            "output_file": "about.html",
            "include_in_sitemap": True,
            "changefreq": "monthly"
        }
    ],
    "header": {
        "enabled": True,
        "theme": "light",
        "layout": "inline",
        "sticky": True,
        "show_logo": True,
        "brand_title": "अबतक24.in (Aabtak24.in)",
        "brand_tagline": "",
        "nav_style": "underline",
        "top_bar_enabled": True,
        "top_bar_show_date": True,
        "top_bar_text": "",
        "edition_tag": "डिजिटल संस्करण • INDIA",
        "hamburger_style": "staggered",
        "mobile_menu_style": "slide-right",
        "mobile_theme": "auto",
        "mobile_menu_label": "मेनू",
        "mobile_show_tagline": True,
        "theme_toggle_enabled": True,
        "default_color_mode": "dark",
        "action_links": [],
        "colors": {}
    },
    "header_menu": [
        {"label": "मुख्य खबरें", "href": "index.html"},
        {"label": "देश-विदेश", "href": "desh-videsh.html"},
        {"label": "टेक्नोलॉजी", "href": "technology.html"},
        {"label": "अर्थव्यवस्था", "href": "economy.html"},
        {"label": "राजनीति", "href": "politics.html"},
        {"label": "मनोरंजन", "href": "entertainment.html"},
        {"label": "हमारे बारे में", "href": "about.html"}
    ],
    "footer": {
        "enabled": True,
        "theme": "dark",
        "show_logo": True,
        "brand_title": "अबतक24.in (Aabtak24.in)",
        "brand_tagline": "भारत का आधुनिक डिजिटल हिंदी समाचार पोर्टल। देश-विदेश, टेक्नोलॉजी, अर्थव्यवस्था, राजनीति और मनोरंजन की सटीक एवं तथ्यपूर्ण खबरें।",
        "contact_label": "संपादकीय संपर्क:",
        "contact_email": "contact@aabtak24.in",
        "columns": [
            {
                "id": "categories",
                "title": "प्रमुख श्रेणियाँ",
                "links": [
                    {"label": "मुख्य खबरें", "href": "index.html"},
                    {"label": "देश-विदेश", "href": "desh-videsh.html"},
                    {"label": "टेक्नोलॉजी", "href": "technology.html"},
                    {"label": "अर्थव्यवस्था", "href": "economy.html"},
                    {"label": "राजनीति", "href": "politics.html"},
                    {"label": "मनोरंजन", "href": "entertainment.html"}
                ]
            },
            {
                "id": "pages",
                "title": "संस्थान एवं नीतियाँ",
                "links": [
                    {"label": "हमारे बारे में (About Us)", "href": "about.html"},
                    {"label": "संपर्क करें (Contact Us)", "href": "contact.html"},
                    {"label": "संपादकीय नीति (Editorial Policy)", "href": "editorial-policy.html"},
                    {"label": "गोपनीयता नीति (Privacy Policy)", "href": "privacy-policy.html"}
                ]
            }
        ],
        "social_links": [
            {"label": "🌐 वेबसाइट", "href": "https://aabtak24.in", "new_tab": True},
            {"label": "✉️ ईमेल संपर्क", "href": "mailto:contact@aabtak24.in", "new_tab": False}
        ],
        "copyright_text": "© {year} अबतक24.in (Aabtak24.in) • सर्वाधिकार सुरक्षित।",
        "bottom_note": "डिजिटल संस्करण • INDIA",
        "colors": {}
    },
    "homepage": {
        "preset": "classic-bento",
        "deduplicate_across_sections": True,
        "sections": [
            {
                "id": "top-stories",
                "enabled": True,
                "title": "प्रमुख सुर्खियां / Top Stories",
                "subtitle": "ताज़ा अपडेट्स",
                "category": "all",
                "layout": "bento",
                "max_articles": 5,
                "show_view_all": False,
                "bg_theme": "auto"
            },
            {
                "id": "more-news",
                "enabled": True,
                "title": "अन्य खबरें / More News",
                "subtitle": "सभी समाचार",
                "category": "all",
                "layout": "grid",
                "max_articles": 12,
                "show_view_all": False,
                "bg_theme": "auto"
            }
        ]
    }
}

HOMEPAGE_LAYOUTS = ("editorial-desk", "bento", "grid", "magazine", "compact-list", "strip")
SECTION_BG_THEMES = ("auto", "default", "sand", "crimson", "sage", "slate", "amber")
AUTO_SECTION_BG_CYCLE = ("sand", "crimson", "slate", "sage", "amber")
VALID_COLOR_MODES = ("light", "dark", "system")

HOMEPAGE_PRESETS = {
    "classic-bento": {
        "name": "क्लासिक बेंटो (Classic Bento)",
        "deduplicate_across_sections": True,
        "sections": [
            {
                "id": "top-stories",
                "enabled": True,
                "title": "प्रमुख सुर्खियां / Top Stories",
                "subtitle": "ताज़ा अपडेट्स",
                "category": "all",
                "layout": "bento",
                "max_articles": 5,
                "show_view_all": False
            },
            {
                "id": "more-news",
                "enabled": True,
                "title": "अन्य खबरें / More News",
                "subtitle": "सभी समाचार",
                "category": "all",
                "layout": "grid",
                "max_articles": 12,
                "show_view_all": False
            }
        ]
    },
    "category-showcase": {
        "name": "न्यूज़पेपर कैटेगरी शोकेस (Category Showcase)",
        "deduplicate_across_sections": True,
        "sections": [
            {
                "id": "top-stories",
                "enabled": True,
                "title": "प्रमुख सुर्खियां / Top Stories",
                "subtitle": "ताज़ा अपडेट्स",
                "category": "all",
                "layout": "bento",
                "max_articles": 5,
                "show_view_all": False
            },
            {
                "id": "sec-desh-videsh",
                "enabled": True,
                "title": "देश-विदेश / National & World",
                "subtitle": "प्रमुख कवरेज",
                "category": "desh-videsh",
                "layout": "grid",
                "max_articles": 6,
                "show_view_all": True
            },
            {
                "id": "sec-technology",
                "enabled": True,
                "title": "टेक्नोलॉजी / Technology & AI",
                "subtitle": "टेक व विज्ञान",
                "category": "technology",
                "layout": "magazine",
                "max_articles": 5,
                "show_view_all": True
            },
            {
                "id": "sec-economy",
                "enabled": True,
                "title": "अर्थव्यवस्था / Economy & Business",
                "subtitle": "बाज़ार और नीति",
                "category": "economy",
                "layout": "compact-list",
                "max_articles": 6,
                "show_view_all": True
            },
            {
                "id": "sec-politics",
                "enabled": True,
                "title": "राजनीति / Politics",
                "subtitle": "सत्ता और संसद",
                "category": "politics",
                "layout": "grid",
                "max_articles": 3,
                "show_view_all": True
            },
            {
                "id": "sec-entertainment",
                "enabled": True,
                "title": "मनोरंजन / Entertainment",
                "subtitle": "सिनेमा व ट्रेंड्स",
                "category": "entertainment",
                "layout": "strip",
                "max_articles": 4,
                "show_view_all": True
            }
        ]
    },
    "magazine-editorial": {
        "name": "मैगज़ीन एडिटोरियल (Magazine Editorial)",
        "deduplicate_across_sections": True,
        "sections": [
            {
                "id": "lead-spotlight",
                "enabled": True,
                "title": "स्पॉटलाइट / Featured Spotlight",
                "subtitle": "विशेष संपादकीय",
                "category": "all",
                "layout": "magazine",
                "max_articles": 5,
                "show_view_all": False
            },
            {
                "id": "quick-strip",
                "enabled": True,
                "title": "तेज़ नज़र / Quick Highlights",
                "subtitle": "प्रमुख झलकियाँ",
                "category": "all",
                "layout": "strip",
                "max_articles": 4,
                "show_view_all": False
            },
            {
                "id": "mag-desh-videsh",
                "enabled": True,
                "title": "देश-विदेश / Big Stories",
                "subtitle": "बड़ी खबरें",
                "category": "desh-videsh",
                "layout": "bento",
                "max_articles": 5,
                "show_view_all": True
            },
            {
                "id": "mag-technology",
                "enabled": True,
                "title": "टेक्नोलॉजी और भविष्य",
                "subtitle": "टेक विशेष",
                "category": "technology",
                "layout": "compact-list",
                "max_articles": 4,
                "show_view_all": True
            },
            {
                "id": "mag-politics",
                "enabled": True,
                "title": "राजनीति और नीति",
                "subtitle": "सियासी हलचल",
                "category": "politics",
                "layout": "magazine",
                "max_articles": 4,
                "show_view_all": True
            },
            {
                "id": "mag-more",
                "enabled": True,
                "title": "अन्य प्रमुख समाचार",
                "subtitle": "सभी श्रेणियाँ",
                "category": "all",
                "layout": "grid",
                "max_articles": 6,
                "show_view_all": False
            }
        ]
    },
    "compact-wire": {
        "name": "हाई-डेंसिटी न्यूज़ वायर (Compact Wire)",
        "deduplicate_across_sections": True,
        "sections": [
            {
                "id": "wire-strip",
                "enabled": True,
                "title": "ताज़ा सुर्खियां / Top Strip",
                "subtitle": "अभी-अभी",
                "category": "all",
                "layout": "strip",
                "max_articles": 4,
                "show_view_all": False
            },
            {
                "id": "wire-latest",
                "enabled": True,
                "title": "लेटेस्ट न्यूज़ वायर / Latest Wire",
                "subtitle": "तेज़ अपडेट्स",
                "category": "all",
                "layout": "compact-list",
                "max_articles": 8,
                "show_view_all": False
            },
            {
                "id": "wire-desh",
                "enabled": True,
                "title": "देश-विदेश / National Desk",
                "subtitle": "ताज़ा रिपोर्ट्स",
                "category": "desh-videsh",
                "layout": "compact-list",
                "max_articles": 6,
                "show_view_all": True
            },
            {
                "id": "wire-tech",
                "enabled": True,
                "title": "टेक्नोलॉजी / Tech Desk",
                "subtitle": "टेक व AI",
                "category": "technology",
                "layout": "compact-list",
                "max_articles": 4,
                "show_view_all": True
            },
            {
                "id": "wire-economy",
                "enabled": True,
                "title": "अर्थव्यवस्था / Economy Desk",
                "subtitle": "बिजनेस व बाज़ार",
                "category": "economy",
                "layout": "grid",
                "max_articles": 6,
                "show_view_all": True
            }
        ]
    },
    "wire-editorial": {
        "name": "द वायर 3-कॉलम डेस्क (The Wire 3-Column Desk)",
        "deduplicate_across_sections": True,
        "sections": [
            {
                "id": "editorial-frontpage",
                "enabled": True,
                "title": "मुख्य पृष्ठ / Frontpage Desk",
                "subtitle": "संपादक की पसंद • राजनीति • ताज़ा खबरें",
                "category": "all",
                "layout": "editorial-desk",
                "max_articles": 9,
                "show_view_all": False,
                "bg_theme": "sand"
            },
            {
                "id": "cat-desh-videsh",
                "enabled": True,
                "title": "देश-विदेश",
                "subtitle": "ताज़ा हाइलाइट्स",
                "category": "desh-videsh",
                "layout": "strip",
                "max_articles": 4,
                "show_view_all": True,
                "bg_theme": "crimson"
            },
            {
                "id": "cat-technology",
                "enabled": True,
                "title": "टेक्नोलॉजी",
                "subtitle": "विशेष कवरेज",
                "category": "technology",
                "layout": "grid",
                "max_articles": 6,
                "show_view_all": True,
                "bg_theme": "slate"
            },
            {
                "id": "cat-economy",
                "enabled": True,
                "title": "अर्थव्यवस्था",
                "subtitle": "संक्षिप्त समाचार",
                "category": "economy",
                "layout": "compact-list",
                "max_articles": 6,
                "show_view_all": True,
                "bg_theme": "sage"
            },
            {
                "id": "cat-politics",
                "enabled": True,
                "title": "राजनीति",
                "subtitle": "प्रमुख रिपोर्ट्स",
                "category": "politics",
                "layout": "grid",
                "max_articles": 6,
                "show_view_all": True,
                "bg_theme": "amber"
            }
        ]
    }
}

# 4. Multi-Provider AI Model Fallback Configuration
NVIDIA_NIM_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

MODEL_FALLBACK_CHAIN = [
    # Tier 1: Google Gemini (GEMINI_API_KEY)
    {"provider": "google", "model": "gemini-3.8-flash", "wait_sec": 2},
    {"provider": "google", "model": "gemini-4-hydrogen", "wait_sec": 2},
    {"provider": "google", "model": "gemini-3.6-flash", "wait_sec": 2},
    {"provider": "google", "model": "gemini-3.5-flash-lite", "wait_sec": 2},
    {"provider": "google", "model": "gemini-3.1-flash-lite", "wait_sec": 2},
    {"provider": "google", "model": "gemini-3-flash-preview", "wait_sec": 2},
    # Tier 2: Google Open-Weight Gemma 4 (GEMINI_API_KEY, ~14,400 RPD)
    {"provider": "google", "model": "gemma-4-31b-it", "wait_sec": 2},
    {"provider": "google", "model": "gemma-4-26b-a4b-it", "wait_sec": 2},
    # Tier 3: Non-Google Open-Source Hindi Models via NVIDIA NIM (NVIDIA_API_KEY)
    {"provider": "nvidia_nim", "model": "nvidia/nemotron-3.5-super-vl-120b-a12b", "wait_sec": 2},
    {"provider": "nvidia_nim", "model": "mistralai/mistral-large-4-1t-a52b", "wait_sec": 2},
    {"provider": "nvidia_nim", "model": "nvidia/nemotron-3-ultra-550b-a55b", "wait_sec": 2},
    {"provider": "nvidia_nim", "model": "mistralai/mistral-small-4-119b-2603", "wait_sec": 2},
    {"provider": "nvidia_nim", "model": "qwen/qwen2.5-72b-instruct", "wait_sec": 2},
    {"provider": "nvidia_nim", "model": "openai/gpt-oss-120b", "wait_sec": 2},
    {"provider": "nvidia_nim", "model": "nvidia/nemotron-3.5-lightning-30b-a3b", "wait_sec": 2},
]

MODELS_TO_TRY = [entry["model"] for entry in MODEL_FALLBACK_CHAIN]


def normalize_homepage_config(raw_homepage):
    """Validates and normalizes the homepage layout configuration with safe defaults."""
    default_hp = copy.deepcopy(DEFAULT_SITE_CONFIG["homepage"])
    if not isinstance(raw_homepage, dict):
        return default_hp

    preset = str(raw_homepage.get("preset", "classic-bento") or "classic-bento").strip().lower()
    if preset not in HOMEPAGE_PRESETS and preset != "custom":
        preset = "custom"

    dedupe = raw_homepage.get("deduplicate_across_sections", True)
    dedupe = bool(dedupe)

    raw_sections = raw_homepage.get("sections")
    if not isinstance(raw_sections, list):
        preset_data = HOMEPAGE_PRESETS.get(preset) or HOMEPAGE_PRESETS["classic-bento"]
        raw_sections = copy.deepcopy(preset_data["sections"])

    normalized_sections = []
    for idx, sec in enumerate(raw_sections):
        if not isinstance(sec, dict):
            continue
        sec_id = str(sec.get("id", "") or "").strip() or f"section-{idx + 1}"
        enabled = bool(sec.get("enabled", True))
        title = str(sec.get("title", "") or "").strip() or f"समाचार सेक्शन {idx + 1}"
        subtitle = str(sec.get("subtitle", "") or "").strip()
        category = str(sec.get("category", "all") or "all").strip() or "all"
        layout = str(sec.get("layout", "grid") or "grid").strip().lower()
        if layout not in HOMEPAGE_LAYOUTS:
            layout = "grid"

        default_limit = 9 if layout == "editorial-desk" else (5 if layout == "bento" else 6)
        raw_limit = sec.get("max_articles", default_limit)
        try:
            max_articles = int(raw_limit)
        except (TypeError, ValueError):
            max_articles = default_limit
        max_articles = max(1, min(50, max_articles))

        show_view_all = bool(sec.get("show_view_all", category.lower() != "all"))
        bg_theme = str(sec.get("bg_theme", "auto") or "auto").strip().lower()
        if bg_theme not in SECTION_BG_THEMES:
            bg_theme = "auto"

        normalized_sections.append({
            "id": sec_id,
            "enabled": enabled,
            "title": title,
            "subtitle": subtitle,
            "category": category,
            "layout": layout,
            "max_articles": max_articles,
            "show_view_all": show_view_all,
            "bg_theme": bg_theme,
        })

    if not normalized_sections:
        normalized_sections = copy.deepcopy(default_hp["sections"])

    return {
        "preset": preset,
        "deduplicate_across_sections": dedupe,
        "sections": normalized_sections,
    }


def load_site_config():
    """Loads site_config.json with safe fallback to DEFAULT_SITE_CONFIG."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "categories" in data and "header_menu" in data:
                data.setdefault("custom_pages", [])
                header_cfg = data.get("header")
                if not isinstance(header_cfg, dict):
                    data["header"] = copy.deepcopy(DEFAULT_SITE_CONFIG["header"])
                else:
                    for key, default_val in DEFAULT_SITE_CONFIG["header"].items():
                        if key not in header_cfg:
                            header_cfg[key] = copy.deepcopy(default_val)
                    if str(header_cfg.get("default_color_mode", "dark")).strip().lower() not in VALID_COLOR_MODES:
                        header_cfg["default_color_mode"] = "dark"
                footer_cfg = data.get("footer")
                if not isinstance(footer_cfg, dict):
                    data["footer"] = copy.deepcopy(DEFAULT_SITE_CONFIG["footer"])
                else:
                    for key, default_val in DEFAULT_SITE_CONFIG["footer"].items():
                        if key not in footer_cfg:
                            footer_cfg[key] = copy.deepcopy(default_val)
                data["homepage"] = normalize_homepage_config(data.get("homepage"))
                return data
            print(f"⚠️ {CONFIG_FILE} missing required keys. Using fallback config.")
        except Exception as e:
            print(f"⚠️ Error reading {CONFIG_FILE} ({e}). Using fallback config.")
    else:
        print(f"ℹ️ {CONFIG_FILE} not found. Using built-in default config.")
    return copy.deepcopy(DEFAULT_SITE_CONFIG)


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


IMAGE_FALLBACK_ONERROR = "this.onerror=null;if(this.dataset.fallbackSrc){this.src=this.dataset.fallbackSrc;}"
DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")


def _is_wsrv_url(url):
    """Returns True if the URL already points to the wsrv.nl proxy host."""
    try:
        netloc = urlparse(str(url or "").strip()).netloc.lower().split(":")[0]
        return netloc == "wsrv.nl" or netloc.endswith(".wsrv.nl")
    except Exception:
        return False


def normalize_external_url(url):
    """Strips whitespace and escapes raw spaces/non-ASCII chars while preserving valid encodings."""
    cleaned = str(url or "").strip()
    if not cleaned:
        return cleaned
    return quote(cleaned, safe=":/?#[]@!$&'()*+,;=%")


def _extract_raw_from_wsrv(wsrv_url):
    """Extracts the embedded origin URL from an existing wsrv.nl proxy link, if present."""
    try:
        qs = parse_qs(urlparse(wsrv_url).query)
        origin = (qs.get("url") or [""])[0].strip()
        if origin.startswith(("http://", "https://")):
            return normalize_external_url(origin)
    except Exception:
        pass
    return ""


def optimize_img_url(url, width=800):
    """
    Compresses & converts heavy external images into fast WebP via wsrv.nl CDN.
    Leaves local editor uploads (e.g., uploads/...) and existing wsrv.nl links untouched.
    """
    if not url:
        return url
    cleaned = str(url).strip()
    if not cleaned or not cleaned.startswith(("http://", "https://")) or _is_wsrv_url(cleaned):
        return cleaned
    normalized = normalize_external_url(cleaned)
    return f"https://wsrv.nl/?url={quote(normalized, safe='')}&w={width}&output=webp&q=80"


def should_skip_image_check(explicit=None):
    """Determines whether build-time wsrv.nl health verification should be skipped."""
    if explicit is not None:
        return bool(explicit)
    if "--skip-image-check" in sys.argv:
        return True
    return os.environ.get("SKIP_IMAGE_CHECK", "").strip().lower() in {"1", "true", "yes"}


def check_wsrv_image_health(raw_url, width=800):
    """
    Probes wsrv.nl for a candidate image URL without downloading the body.
    Returns 'healthy', 'failed' (HTTP >= 400 or non-image MIME), or 'transport_error'.
    """
    probe_url = str(raw_url or "").strip()
    if not _is_wsrv_url(probe_url):
        probe_url = optimize_img_url(probe_url, width=width)
    resp = None
    try:
        resp = requests.get(
            probe_url,
            stream=True,
            timeout=IMAGE_CHECK_TIMEOUT_SEC,
            allow_redirects=True,
            headers={"User-Agent": "AutoHindiNewsBot/1.0"},
        )
        status = getattr(resp, "status_code", 0)
        if 200 <= status < 300:
            raw_headers = getattr(resp, "headers", None) or {}
            raw_ctype = ""
            for k, v in raw_headers.items():
                if str(k).lower() == "content-type":
                    raw_ctype = str(v or "")
                    break
            ctype = raw_ctype.split(";")[0].strip().lower()
            if ctype and not (ctype.startswith("image/") or ctype == "application/octet-stream"):
                return "failed"
            return "healthy"
        if status >= 400:
            return "failed"
        return "transport_error"
    except (requests.exceptions.RequestException, TimeoutError, ConnectionError, OSError):
        return "transport_error"
    finally:
        if resp is not None and hasattr(resp, "close"):
            try:
                resp.close()
            except Exception:
                pass


class ImageResolver:
    """
    Caches wsrv.nl health checks across a build run so each unique image is probed at most once,
    falls back directly to raw image URLs on confirmed 404/errors, and trips a transport-error
    circuit breaker in offline/proxied sandboxes without erasing wsrv.nl URLs globally.
    """

    MAX_CONSECUTIVE_TRANSPORT_ERRORS = 3

    def __init__(self, skip_check=False):
        self.skip_check = bool(skip_check)
        self.disabled = bool(skip_check)
        self._cache = {}
        self._lock = threading.Lock()
        self._transport_error_streak = 0
        self._logged_failures = set()

    def _probe_and_cache(self, cache_key, check_target=None, width=800):
        with self._lock:
            if cache_key in self._cache:
                return self._cache[cache_key]
            if self.disabled:
                return "unknown"

        status = check_wsrv_image_health(check_target or cache_key, width=width)

        with self._lock:
            if status in ("healthy", "failed"):
                self._transport_error_streak = 0
                self._cache[cache_key] = status
                if status == "failed" and cache_key not in self._logged_failures:
                    self._logged_failures.add(cache_key)
                    print(f"⚠️ wsrv.nl failed for image — using direct original URL: {cache_key}")
                return status

            # Transport error reaching wsrv.nl itself
            self._transport_error_streak += 1
            if self._transport_error_streak >= self.MAX_CONSECUTIVE_TRANSPORT_ERRORS and not self.disabled:
                self.disabled = True
                print(
                    "⚠️ Skipping remaining build-time wsrv.nl checks after repeated transport errors "
                    "(runtime browser onerror fallback remains active)."
                )
            self._cache[cache_key] = "unknown"
            return "unknown"

    def prewarm(self, raw_urls, max_workers=4):
        """Pre-checks unique external image URLs concurrently before rendering pages."""
        if self.skip_check or self.disabled:
            return
        targets = []
        seen = set()
        for url in raw_urls:
            cleaned = str(url or "").strip()
            if not cleaned.startswith(("http://", "https://")):
                continue
            if _is_wsrv_url(cleaned):
                cache_key = _extract_raw_from_wsrv(cleaned) or cleaned
                check_target = cleaned
            else:
                cache_key = normalize_external_url(cleaned)
                check_target = cache_key
            if cache_key not in seen and cache_key not in self._cache:
                seen.add(cache_key)
                targets.append((cache_key, check_target))

        if not targets:
            return

        workers = min(max_workers, len(targets))
        if workers <= 1:
            for cache_key, check_target in targets:
                if self.disabled:
                    break
                self._probe_and_cache(cache_key, check_target=check_target, width=800)
            return

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(self._probe_and_cache, cache_key, check_target, 800)
                for cache_key, check_target in targets
            ]
            for fut in futures:
                try:
                    fut.result()
                except Exception:
                    pass

    def resolve(self, raw_url, width=800, local_prefix=""):
        """
        Resolves an article image URL to (primary_src, browser_fallback_src).
        Confirmed wsrv.nl 404/failures return (raw_url, '') directly with no wsrv.nl link.
        Healthy or unchecked wsrv.nl links return (wsrv_url, raw_url) for runtime fallback.
        """
        cleaned = str(raw_url or "").strip()
        if not cleaned:
            return "", ""

        if not cleaned.startswith(("http://", "https://")):
            if local_prefix and not cleaned.startswith(("../", "/")):
                return f"{local_prefix}{cleaned.lstrip('./')}", ""
            return cleaned, ""

        if _is_wsrv_url(cleaned):
            raw_origin = _extract_raw_from_wsrv(cleaned)
            cache_key = raw_origin or cleaned
            status = self._probe_and_cache(cache_key, check_target=cleaned, width=width)
            if status == "failed" and raw_origin:
                return raw_origin, ""
            return cleaned, (raw_origin if raw_origin and raw_origin != cleaned else "")

        normalized_raw = normalize_external_url(cleaned)
        wsrv_url = optimize_img_url(normalized_raw, width=width)
        status = self._probe_and_cache(normalized_raw, check_target=normalized_raw, width=width)
        if status == "failed":
            return normalized_raw, ""
        return wsrv_url, normalized_raw


def resolve_image_src(raw_url, width=800, local_prefix="", resolver=None):
    """Convenience helper resolving (src, fallback_src) using an ImageResolver."""
    active_resolver = resolver or ImageResolver(skip_check=should_skip_image_check())
    return active_resolver.resolve(raw_url, width=width, local_prefix=local_prefix)


def build_img_tag(src, alt, width, height, fallback_src="", extra_attrs="", self_closing=False):
    """Builds an <img> tag with escaped attributes and loop-safe browser onerror fallback."""
    attrs = [
        f'src="{html.escape(str(src or ""), quote=True)}"',
        f'alt="{html.escape(str(alt or ""), quote=True)}"',
        f'width="{width}"',
        f'height="{height}"',
    ]
    if fallback_src and fallback_src != src:
        attrs.append(f'data-fallback-src="{html.escape(str(fallback_src), quote=True)}"')
        attrs.append(f'onerror="{IMAGE_FALLBACK_ONERROR}"')
    if extra_attrs and extra_attrs.strip():
        attrs.append(extra_attrs.strip())
    closing = " /" if self_closing else ""
    return f"<img {' '.join(attrs)}{closing}>"


def get_head_links(css_path="style.css", preload_img=None, config=None):
    """Common <head> tags: Anti-FOUC theme init, Non-blocking Google Fonts, optional LCP preload, and CSS."""
    escaped_preload = html.escape(preload_img, quote=True) if preload_img else ""
    preload_tag = f'    <link rel="preload" as="image" href="{escaped_preload}" fetchpriority="high">\n' if escaped_preload else ""
    header_cfg = config.get("header") if isinstance(config, dict) and isinstance(config.get("header"), dict) else {}
    default_mode = str(header_cfg.get("default_color_mode", "dark") or "dark").strip().lower()
    if default_mode not in VALID_COLOR_MODES:
        default_mode = "dark"
    toggle_enabled = bool(header_cfg.get("theme_toggle_enabled", True))
    toggle_js = "true" if toggle_enabled else "false"
    theme_init_script = (
        f"    <script>(function(){{var d='{default_mode}';var e={toggle_js};var s=null;"
        "if(e){try{s=localStorage.getItem('aabtak-theme');}catch(err){}}"
        "var m=(s==='light'||s==='dark')?s:(d==='system'?((window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches)?'dark':'light'):d);"
        "document.documentElement.setAttribute('data-theme',m==='dark'?'dark':'light');"
        "document.documentElement.setAttribute('data-default-theme',d);"
        f"document.documentElement.setAttribute('data-theme-toggle-enabled','{toggle_js}');"
        "})();</script>"
    )
    return f"""    <link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><rect width=%22100%22 height=%22100%22 rx=%2216%22 fill=%22%23141210%22/><text x=%2250%%22 y=%2254%%22 dominant-baseline=%22central%22 text-anchor=%22middle%22 fill=%22%23FF4C00%22 font-family=%22sans-serif%22 font-weight=%22900%22 font-size=%2252%22>अ</text></svg>">
{theme_init_script}
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link rel="preconnect" href="https://wsrv.nl" crossorigin>
{preload_tag}    <link rel="stylesheet" href="{css_path}">
    <link rel="preload" as="style" href="https://fonts.googleapis.com/css2?family=Mukta:wght@700;800&family=Noto+Sans+Devanagari:wght@400;600;700&display=swap" onload="this.onload=null;this.rel='stylesheet'">
    <noscript><link href="https://fonts.googleapis.com/css2?family=Mukta:wght@700;800&family=Noto+Sans+Devanagari:wght@400;600;700&display=swap" rel="stylesheet"></noscript>"""


def parse_ai_json(raw_text):
    """Safely parses JSON from Gemini or open-source models even with think tags, fences, or preamble."""
    if not raw_text or not str(raw_text).strip():
        raise ValueError("Empty AI response text")

    cleaned = str(raw_text).strip()
    cleaned = re.sub(r"<think\b[^>]*>.*?</think>", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()

    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if fence_match:
        fenced_candidate = fence_match.group(1).strip()
        try:
            parsed = json.loads(fenced_candidate, strict=False)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    try:
        parsed = json.loads(cleaned, strict=False)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Extract balanced JSON object candidates while ignoring braces inside string literals
    candidates = []
    n = len(cleaned)
    i = 0
    while i < n:
        if cleaned[i] == "{":
            depth = 0
            in_string = False
            escape = False
            start = i
            j = i
            while j < n:
                ch = cleaned[j]
                if in_string:
                    if escape:
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == '"':
                        in_string = False
                else:
                    if ch == '"':
                        in_string = True
                    elif ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            snippet = cleaned[start : j + 1]
                            try:
                                obj = json.loads(snippet, strict=False)
                                if isinstance(obj, dict):
                                    candidates.append(obj)
                                    i = j
                            except json.JSONDecodeError:
                                pass
                            break
                j += 1
        i += 1

    for obj in candidates:
        if "title" in obj or "content_html" in obj:
            return obj
    if candidates:
        return candidates[0]

    return json.loads(cleaned, strict=False)


def validate_article_payload(article_data):
    """Validates parsed AI output has required article fields and Hindi (Devanagari) text."""
    if not isinstance(article_data, dict):
        raise ValueError("AI response JSON must be an object")

    title = article_data.get("title")
    content_html = article_data.get("content_html")

    if not isinstance(title, str) or not title.strip():
        raise ValueError("AI response missing non-empty 'title'")
    if not DEVANAGARI_RE.search(title):
        raise ValueError("AI response 'title' must be in Hindi (Devanagari)")

    if not isinstance(content_html, str) or not content_html.strip():
        raise ValueError("AI response missing non-empty 'content_html'")

    plain_text = re.sub(r"<[^>]+>", " ", content_html)
    plain_text = re.sub(r"\s+", " ", plain_text).strip()
    if len(plain_text) < 12 or len(DEVANAGARI_RE.findall(plain_text)) < 5:
        raise ValueError("AI response 'content_html' lacks sufficient Hindi prose")

    raw_idx = article_data.get("selected_index", 0)
    if isinstance(raw_idx, bool):
        article_data["selected_index"] = 0
    elif isinstance(raw_idx, str) and raw_idx.strip().isdigit():
        article_data["selected_index"] = int(raw_idx.strip())

    return article_data


class _ProviderHttpError(RuntimeError):
    """HTTP error carrying status code and credential-failure classification."""

    def __init__(self, message, status_code=None, is_auth=False):
        super().__init__(message)
        self.status_code = status_code
        self.is_provider_auth_error = is_auth


def _extract_http_status(exc):
    """Best-effort extraction of HTTP status code from SDK or HTTP exceptions."""
    for attr in ("code", "status_code", "http_status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int) and 100 <= val < 600:
            return val
    resp = getattr(exc, "response", None)
    if resp is not None:
        val = getattr(resp, "status_code", None)
        if isinstance(val, int) and 100 <= val < 600:
            return val
    return None


def _is_provider_auth_error(exc, status_code=None):
    """
    Identifies explicit credential/key failures (HTTP 401 or invalid-key errors) so a provider
    can be circuit-broken without aborting sibling 403/404 model fallbacks on that provider.
    """
    if getattr(exc, "is_provider_auth_error", False):
        return True
    code = status_code if status_code is not None else _extract_http_status(exc)
    if code == 401:
        return True
    msg = str(exc or "").lower()
    auth_markers = (
        "api_key_invalid",
        "api key not valid",
        "invalid api key",
        "invalid_api_key",
        "authentication failed",
        "invalid authentication",
        "token expired",
        "key revoked",
        "unauthenticated",
        "unauthorized",
    )
    return any(marker in msg for marker in auth_markers)


def _resolve_google_client():
    """Resolves the active Google GenAI client using module state and live environment."""
    env_key = os.environ.get("GEMINI_API_KEY")
    if env_key:
        if client is not None and env_key == api_key:
            return client
        return build_google_client(env_key)
    if client is not None and not api_key:
        return client
    if client is not None and api_key and "GEMINI_API_KEY" not in os.environ:
        return None
    return client


def _resolve_nvidia_key():
    """Resolves the active NVIDIA NIM API key using live environment and module fallback."""
    if "NVIDIA_API_KEY" in os.environ:
        return (os.environ.get("NVIDIA_API_KEY") or "").strip() or None
    return (nvidia_api_key or "").strip() or None


def _call_google_model(model_name, prompt, google_client):
    """Invokes a Google Gemini/Gemma model with JSON-mode first, retrying once without config on 400."""
    try:
        response = google_client.models.generate_content(
            model=model_name,
            contents=prompt,
            config={"response_mime_type": "application/json"},
        )
    except Exception as e:
        if _is_provider_auth_error(e):
            raise
        status = _extract_http_status(e)
        err_lower = str(e).lower()
        if status == 400 or "400" in err_lower or "response_mime_type" in err_lower or "json" in err_lower or "invalid_argument" in err_lower:
            print(f"ℹ️ {model_name} rejected strict JSON config; retrying without response_mime_type...")
            response = google_client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
        else:
            raise

    raw_text = getattr(response, "text", None)
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ValueError(f"{model_name} returned empty response text")
    return raw_text


def _call_nvidia_nim_model(model_name, prompt, nim_key):
    """Invokes an NVIDIA NIM OpenAI-compatible chat model with HTTP 400 JSON-mode fallback."""
    headers = {
        "Authorization": f"Bearer {nim_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    base_payload = {
        "model": model_name,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a senior Hindi news editor. Respond ONLY with a valid raw JSON object "
                    "matching the requested schema, with no markdown fences, thinking tags, or commentary."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.4,
        "max_tokens": 4096,
    }

    resp = requests.post(
        NVIDIA_NIM_API_URL,
        headers=headers,
        json={**base_payload, "response_format": {"type": "json_object"}},
        timeout=NIM_HTTP_TIMEOUT,
    )

    if resp.status_code == 400:
        err_preview = resp.text[:300]
        if _is_provider_auth_error(RuntimeError(err_preview), status_code=400):
            raise _ProviderHttpError(f"HTTP 400: {err_preview}", status_code=400, is_auth=True)
        print(f"ℹ️ {model_name} rejected response_format=json_object (HTTP 400); retrying without response_format...")
        resp = requests.post(
            NVIDIA_NIM_API_URL,
            headers=headers,
            json=base_payload,
            timeout=NIM_HTTP_TIMEOUT,
        )

    if resp.status_code >= 400:
        err_preview = resp.text[:300]
        raise _ProviderHttpError(
            f"HTTP {resp.status_code}: {err_preview}",
            status_code=resp.status_code,
            is_auth=_is_provider_auth_error(RuntimeError(err_preview), status_code=resp.status_code),
        )

    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise ValueError(f"{model_name} returned empty choices")
    message = choices[0].get("message") or {}
    raw_content = message.get("content")
    if not isinstance(raw_content, str) or not raw_content.strip():
        finish_reason = choices[0].get("finish_reason")
        raise ValueError(f"{model_name} returned empty content (finish_reason={finish_reason})")
    return raw_content


def generate_with_fallback(prompt):
    """Generates a validated Hindi article payload strictly following MODEL_FALLBACK_CHAIN."""
    google_client = _resolve_google_client()
    nim_key = _resolve_nvidia_key()
    disabled_providers = set()
    missing_logged = set()

    if google_client is None:
        disabled_providers.add("google")
        missing_logged.add("google")
        print("ℹ️ Skipping Google Gemini/Gemma models (GEMINI_API_KEY / client not configured).")
    if not nim_key:
        disabled_providers.add("nvidia_nim")
        missing_logged.add("nvidia_nim")
        print("ℹ️ Skipping NVIDIA NIM models (NVIDIA_API_KEY not configured).")

    last_error = None
    total = len(MODEL_FALLBACK_CHAIN)

    for idx, entry in enumerate(MODEL_FALLBACK_CHAIN):
        provider = entry["provider"]
        model_name = entry["model"]
        wait_sec = entry.get("wait_sec", 2)

        if provider in disabled_providers:
            if provider not in missing_logged:
                print(f"⏭️ Skipping {model_name} (provider '{provider}' disabled after auth error).")
            continue

        try:
            print(f"Attempting to write Hindi article using [{provider}] {model_name}...")
            if provider == "google":
                raw_text = _call_google_model(model_name, prompt, google_client)
            elif provider == "nvidia_nim":
                raw_text = _call_nvidia_nim_model(model_name, prompt, nim_key)
            else:
                raise ValueError(f"Unsupported provider: {provider}")

            article_data = validate_article_payload(parse_ai_json(raw_text))
            print(f"✅ Success with [{provider}] {model_name}!")
            return article_data

        except (json.JSONDecodeError, ValueError) as e:
            last_error = e
            print(f"⚠️ [{provider}] {model_name} returned invalid/non-Hindi JSON ({e}). Switching to backup model...")
        except Exception as e:
            last_error = e
            if _is_provider_auth_error(e):
                disabled_providers.add(provider)
                print(f"🔒 Provider '{provider}' credential error on {model_name} ({e}). Skipping remaining '{provider}' models.")
            else:
                print(f"⚠️ [{provider}] {model_name} busy/failed ({e}). Switching to backup model...")

        # Sleep briefly before trying the next enabled model
        if idx < total - 1 and any(e["provider"] not in disabled_providers for e in MODEL_FALLBACK_CHAIN[idx + 1 :]):
            time.sleep(wait_sec)

    raise RuntimeError(f"All configured AI models failed. Last error: {last_error}")


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


def _build_nav_link(item, prefix="", active_file="", extra_class=""):
    """Formats a single navigation link safely with prefix handling and active state."""
    if not isinstance(item, dict):
        return ""
    label = str(item.get("label", "")).strip()
    raw_href = str(item.get("href", "")).strip()
    if not label or not raw_href:
        return ""
    if raw_href.lower().startswith(("javascript:", "vbscript:", "data:")):
        return ""

    safe_label = html.escape(label, quote=True)
    is_http = raw_href.startswith(("http://", "https://"))
    is_special = raw_href.startswith(("mailto:", "tel:", "#"))

    if is_http or is_special:
        safe_href = html.escape(raw_href, quote=True)
        default_new_tab = is_http
        use_new_tab = item["new_tab"] if isinstance(item.get("new_tab"), bool) else default_new_tab
        target_attr = ' target="_blank" rel="noopener noreferrer"' if use_new_tab else ""
        cls_attr = f' class="{extra_class}"' if extra_class else ""
        return f'<a href="{safe_href}"{cls_attr}{target_attr}>{safe_label}</a>'

    clean_href = raw_href.lstrip("./")
    safe_href = html.escape(f"{prefix}{clean_href}", quote=True)
    is_active = bool(active_file and active_file == clean_href and not extra_class)
    classes = [c for c in [extra_class, "active" if is_active else ""] if c]
    cls_attr = f' class="{" ".join(classes)}"' if classes else ""
    aria_attr = ' aria-current="page"' if is_active else ""
    return f'<a href="{safe_href}"{cls_attr}{aria_attr}>{safe_label}</a>'


def _format_inline_css_vars(colors, var_pairs):
    """Formats validated hex color overrides into an inline CSS custom property style attribute."""
    if not isinstance(colors, dict):
        return ""
    style_vars = []
    for key, css_var in var_pairs:
        val = str(colors.get(key, "") or "").strip()
        if re.match(r"^#[0-9a-fA-F]{3,8}$", val):
            style_vars.append(f"{css_var}: {val}")
    return f' style="{"; ".join(style_vars)};"' if style_vars else ""


_HINDI_BRAND_RE = re.compile(r"अबतक\s*24(?:\.in)?", re.IGNORECASE)
_VOID_HTML_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})


def _protect_hindi_brand_text(escaped_text):
    """Wraps Hindi Aabtak24(.in) brand mentions in escaped plain text with translate='no' so browsers never translate it to 'So far 24'."""
    if not escaped_text or "अबतक" not in escaped_text:
        return escaped_text
    return _HINDI_BRAND_RE.sub(lambda m: f'<span class="notranslate" translate="no">{m.group(0)}</span>', escaped_text)


def _protect_hindi_brand_html(html_fragment):
    """Wraps unprotected Hindi Aabtak24(.in) mentions in HTML text nodes with translate='no' without mutating tags, attributes, or already-protected spans."""
    if not html_fragment or "अबतक" not in html_fragment:
        return html_fragment

    tokens = re.split(r"(<[^>]+>)", html_fragment)
    out = []
    # Stack holds tuples of (tag_name, is_protected, is_raw_text)
    stack = []
    for tok in tokens:
        if not tok:
            continue
        if tok.startswith("<") and tok.endswith(">"):
            out.append(tok)
            if tok.startswith("<!--") or tok.startswith("<!") or tok.startswith("<?"):
                continue
            close_m = re.match(r"^<\s*/\s*([a-zA-Z0-9:-]+)", tok)
            if close_m:
                tag_name = close_m.group(1).lower()
                for idx in range(len(stack) - 1, -1, -1):
                    if stack[idx][0] == tag_name:
                        del stack[idx:]
                        break
                continue
            open_m = re.match(r"^<\s*([a-zA-Z0-9:-]+)", tok)
            if open_m:
                tag_name = open_m.group(1).lower()
                is_self_closing = tok.rstrip().endswith("/>") or tag_name in _VOID_HTML_TAGS
                if not is_self_closing:
                    has_notranslate = bool(
                        re.search(r'\btranslate\s*=\s*["\']?no\b', tok, re.IGNORECASE)
                        or re.search(r'\bclass\s*=\s*["\'][^"\']*\bnotranslate\b', tok, re.IGNORECASE)
                    )
                    is_raw = tag_name in ("script", "style", "title", "textarea")
                    stack.append((tag_name, has_notranslate, is_raw))
        else:
            in_protected = any(item[1] or item[2] for item in stack)
            if not in_protected and "अबतक" in tok:
                tok = _HINDI_BRAND_RE.sub(lambda m: f'<span class="notranslate" translate="no">{m.group(0)}</span>', tok)
            out.append(tok)
    return "".join(out)


def _sanitize_title_brand(title_text):
    """Replaces Hindi Aabtak24(.in) in <title> with English Aabtak24(.in) since <title> is RCDATA and cannot contain <span translate='no'>."""
    text = str(title_text or "")
    text = re.sub(r"अबतक\s*24\.in\s*\(\s*Aabtak24\.in\s*\)", "Aabtak24.in", text, flags=re.IGNORECASE)
    text = re.sub(r"अबतक\s*24\.in", "Aabtak24.in", text, flags=re.IGNORECASE)
    text = re.sub(r"अबतक\s*24", "Aabtak24", text, flags=re.IGNORECASE)
    return text


def get_top_bar_html(config, today_str=""):
    """Generates the customizable top utility bar above the site header."""
    header = config.get("header") if isinstance(config.get("header"), dict) else {}
    if not header.get("enabled", True) or not header.get("top_bar_enabled", True):
        return ""

    theme = str(header.get("theme", "light")).strip().lower()
    theme_class = "top-bar-theme-dark" if theme == "dark" else "top-bar-theme-light"

    show_date = header.get("top_bar_show_date", True)
    top_bar_text = str(header.get("top_bar_text", "") or "").strip()
    edition_tag = str(header.get("edition_tag", "डिजिटल संस्करण • INDIA") or "").strip()

    left_parts = []
    if show_date and today_str:
        left_parts.append(f"<span>📅 {html.escape(str(today_str), quote=True)}</span>")
    if top_bar_text:
        left_parts.append(f'<span class="top-bar-note">{_protect_hindi_brand_text(html.escape(top_bar_text, quote=True))}</span>')

    if not left_parts and not edition_tag:
        return ""

    left_html = f'<div class="top-bar-left">{" ".join(left_parts)}</div>' if left_parts else ""
    edition_html = f'<span class="edition-tag">{_protect_hindi_brand_text(html.escape(edition_tag, quote=True))}</span>' if edition_tag else ""
    style_attr = _format_inline_css_vars(
        header.get("colors"),
        [
            ("topbar_bg", "--topbar-bg"),
            ("topbar_text", "--topbar-text"),
            ("accent", "--header-accent"),
            ("border", "--header-border"),
        ],
    )

    inner_parts = [p for p in (left_html, edition_html) if p]
    inner_html = "\n        ".join(inner_parts)
    return f"""    <div class="top-bar {theme_class}"{style_attr}>
        {inner_html}
    </div>"""


def get_header_html(config, prefix="", active_file=""):
    """Generates the customizable site-wide header, desktop nav, and mobile drawer/hamburger."""
    header = config.get("header") if isinstance(config.get("header"), dict) else {}
    if not header.get("enabled", True):
        return ""

    theme = "dark" if str(header.get("theme", "light")).strip().lower() == "dark" else "light"
    theme_class = f"header-theme-{theme}"

    layout = str(header.get("layout", "inline")).strip().lower()
    if layout not in ("inline", "centered", "compact"):
        layout = "inline"

    nav_style = str(header.get("nav_style", "underline")).strip().lower()
    if nav_style not in ("underline", "pills", "minimal"):
        nav_style = "underline"

    sticky = bool(header.get("sticky", True))
    sticky_class = "header-sticky" if sticky else "header-static"

    show_logo = header.get("show_logo", True)
    raw_brand = str(header.get("brand_title", "अबतक24.in (Aabtak24.in)") or "").strip()
    brand_title = html.escape(raw_brand or "अबतक24.in (Aabtak24.in)", quote=True)
    brand_tagline = str(header.get("brand_tagline", "") or "").strip()
    edition_tag = str(header.get("edition_tag", "डिजिटल संस्करण • INDIA") or "").strip()

    hamburger_style = str(header.get("hamburger_style", "staggered")).strip().lower()
    if hamburger_style not in ("staggered", "bars", "boxed", "pill"):
        hamburger_style = "staggered"

    mobile_menu_style = str(header.get("mobile_menu_style", "slide-right")).strip().lower()
    if mobile_menu_style not in ("slide-right", "slide-left", "fullscreen", "dropdown"):
        mobile_menu_style = "slide-right"

    mobile_theme = str(header.get("mobile_theme", "auto")).strip().lower()
    if mobile_theme not in ("auto", "light", "dark"):
        mobile_theme = "auto"
    resolved_mobile_theme = theme if mobile_theme == "auto" else mobile_theme

    mobile_menu_label = str(header.get("mobile_menu_label", "मेनू") or "").strip() or "मेनू"
    safe_menu_label = html.escape(mobile_menu_label, quote=True)
    mobile_show_tagline = bool(header.get("mobile_show_tagline", True))

    colors = header.get("colors") if isinstance(header.get("colors"), dict) else {}
    header_style_attr = _format_inline_css_vars(
        colors,
        [
            ("bg", "--header-bg"),
            ("text", "--header-text"),
            ("muted", "--header-muted"),
            ("accent", "--header-accent"),
            ("border", "--header-border"),
            ("border", "--header-divider"),
        ],
    )

    drawer_colors = {}
    if mobile_theme == "auto":
        if colors.get("bg"):
            drawer_colors["drawer_bg"] = colors.get("bg")
        if colors.get("text"):
            drawer_colors["drawer_text"] = colors.get("text")
        if colors.get("border"):
            drawer_colors["drawer_border"] = colors.get("border")
    if colors.get("drawer_bg"):
        drawer_colors["drawer_bg"] = colors.get("drawer_bg")
    if colors.get("drawer_text"):
        drawer_colors["drawer_text"] = colors.get("drawer_text")
    if colors.get("accent"):
        drawer_colors["accent"] = colors.get("accent")

    drawer_style_attr = _format_inline_css_vars(
        drawer_colors,
        [
            ("drawer_bg", "--drawer-bg"),
            ("drawer_text", "--drawer-text"),
            ("drawer_border", "--drawer-border"),
            ("accent", "--drawer-accent"),
        ],
    )

    header_logo_file = "logo-light.svg" if theme == "dark" else "logo.svg"
    drawer_logo_file = "logo-light.svg" if resolved_mobile_theme == "dark" else "logo.svg"
    header_adaptive_attrs = (
        f' data-adaptive-logo="true" data-logo-light="{prefix}logo.svg" data-logo-dark="{prefix}logo-light.svg"'
        if theme == "light" else ""
    )
    drawer_adaptive_attrs = (
        f' data-adaptive-logo="true" data-logo-light="{prefix}logo.svg" data-logo-dark="{prefix}logo-light.svg"'
        if resolved_mobile_theme == "light" else ""
    )

    if show_logo:
        header_brand_inner = f'<img src="{prefix}{header_logo_file}" alt="{brand_title}" width="206" height="36" decoding="sync"{header_adaptive_attrs}>'
        drawer_brand_inner = f'<img src="{prefix}{drawer_logo_file}" alt="{brand_title}" width="160" height="28" decoding="async"{drawer_adaptive_attrs}>'
    else:
        header_brand_inner = f'<span class="header-brand-title">{brand_title}</span>'
        drawer_brand_inner = f'<span class="mobile-drawer-title">{brand_title}</span>'

    tagline_html = f'\n                <span class="header-tagline">{_protect_hindi_brand_text(html.escape(brand_tagline, quote=True))}</span>' if brand_tagline else ""
    drawer_tagline_html = (
        f'\n            <p class="mobile-drawer-tagline">{_protect_hindi_brand_text(html.escape(brand_tagline, quote=True))}</p>'
        if (mobile_show_tagline and brand_tagline) else ""
    )
    drawer_foot_html = (
        f'\n            <div class="mobile-drawer-foot"><span>{_protect_hindi_brand_text(html.escape(edition_tag, quote=True))}</span></div>'
        if (mobile_show_tagline and edition_tag) else ""
    )

    link_items = []
    for item in config.get("header_menu", []) or []:
        anchor = _build_nav_link(item, prefix=prefix, active_file=active_file)
        if anchor:
            link_items.append(anchor)

    desktop_nav_links = "\n                ".join(link_items)
    mobile_nav_links = "\n                ".join(link_items)

    action_items = []
    for act in header.get("action_links", []) or []:
        if not isinstance(act, dict):
            continue
        btn_style = "outline" if str(act.get("style", "accent")).strip().lower() == "outline" else "accent"
        anchor = _build_nav_link(
            act,
            prefix=prefix,
            active_file=active_file,
            extra_class=f"header-action-btn header-action-{btn_style}",
        )
        if anchor:
            action_items.append(anchor)

    desktop_actions_html = (
        '\n            <div class="header-actions desktop-actions">\n                '
        + "\n                ".join(action_items)
        + "\n            </div>"
        if action_items else ""
    )
    mobile_actions_html = (
        '\n            <div class="mobile-drawer-actions">\n                '
        + "\n                ".join(action_items)
        + "\n            </div>"
        if action_items else ""
    )

    theme_toggle_enabled = bool(header.get("theme_toggle_enabled", True))
    theme_icons_svg = (
        '<span class="theme-toggle-icon" aria-hidden="true">'
        '<svg class="theme-icon-moon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>'
        '<svg class="theme-icon-sun" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>'
        '</span>'
    )
    desktop_theme_toggle_html = (
        f'\n                <button type="button" class="theme-toggle-btn desktop-theme-toggle" data-theme-toggle aria-label="डार्क मोड चालू करें" aria-pressed="false" title="लाइट / डार्क मोड बदलें">\n'
        f'                    {theme_icons_svg}\n'
        f'                    <span class="theme-toggle-label">डार्क</span>\n'
        f'                </button>'
        if theme_toggle_enabled else ""
    )
    mobile_theme_toggle_html = (
        f'\n            <div class="mobile-drawer-theme">\n'
        f'                <button type="button" class="theme-toggle-btn mobile-theme-toggle" data-theme-toggle aria-label="डार्क मोड चालू करें" aria-pressed="false">\n'
        f'                    <span class="mobile-theme-copy">\n'
        f'                        <span class="mobile-theme-title">डिस्प्ले मोड</span>\n'
        f'                        <span class="mobile-theme-sub" data-theme-status>लाइट मोड सक्रिय</span>\n'
        f'                    </span>\n'
        f'                    <span class="mobile-theme-pill">\n'
        f'                        {theme_icons_svg}\n'
        f'                        <span class="theme-toggle-label">डार्क</span>\n'
        f'                    </span>\n'
        f'                </button>\n'
        f'            </div>'
        if theme_toggle_enabled else ""
    )

    pill_label_html = f'\n                    <span class="hamburger-label">{safe_menu_label}</span>' if hamburger_style == "pill" else ""

    return f"""    <header class="site-header {theme_class} header-layout-{layout} header-nav-{nav_style} {sticky_class}"{header_style_attr}>
        <div class="header-inner">
            <div class="header-brand-wrap">
                <a href="{prefix}index.html" class="logo notranslate" translate="no" aria-label="{brand_title} होमपेज">
                    {header_brand_inner}
                </a>{tagline_html}
            </div>

            <!-- Desktop Navigation -->
            <div class="header-desktop-group">
                <nav class="nav-pills desktop-nav" aria-label="मुख्य नेविगेशन">
                    {desktop_nav_links}
                </nav>{desktop_actions_html}{desktop_theme_toggle_html}
            </div>

            <!-- Mobile Hamburger Button -->
            <div class="mobile-nav-controls">
                <button type="button" class="hamburger-btn hamburger-style-{hamburger_style}" id="site-hamburger-btn" aria-label="{safe_menu_label} खोलें" aria-expanded="false" aria-controls="site-mobile-panel">
                    <span class="hamburger-lines" aria-hidden="true"><span></span><span></span><span></span></span>{pill_label_html}
                </button>
            </div>
        </div>
    </header>

    <!-- Mobile Navigation Drawer / Overlay / Dropdown -->
    <div class="mobile-nav-shell mobile-style-{mobile_menu_style} mobile-theme-{resolved_mobile_theme}" id="site-mobile-shell" aria-hidden="true"{drawer_style_attr}>
        <div class="mobile-nav-backdrop" data-mobile-close aria-hidden="true"></div>
        <div class="mobile-nav-panel" id="site-mobile-panel">
            <div class="mobile-drawer-head">
                <a href="{prefix}index.html" class="mobile-drawer-logo notranslate" translate="no" aria-label="{brand_title} होमपेज">
                    {drawer_brand_inner}
                </a>
                <button type="button" class="mobile-close-btn" data-mobile-close aria-label="मेनू बंद करें">✕</button>
            </div>{drawer_tagline_html}
            <nav class="mobile-nav-links" aria-label="मोबाइल नेविगेशन">
                {mobile_nav_links}
            </nav>{mobile_theme_toggle_html}{mobile_actions_html}{drawer_foot_html}
        </div>
    </div>
    <script>
    (function () {{
        var btn = document.getElementById('site-hamburger-btn');
        var shell = document.getElementById('site-mobile-shell');
        if (btn && shell) {{
            var panel = document.getElementById('site-mobile-panel');
            var header = document.querySelector('header.site-header');
            var isDropdown = shell.classList.contains('mobile-style-dropdown');

            function positionDropdown() {{
                if (!isDropdown || !panel || !header) return;
                var rect = header.getBoundingClientRect();
                panel.style.top = Math.max(8, Math.round(rect.bottom + 8)) + 'px';
            }}

            function setOpen(open, fromKeyboard) {{
                if (open && isDropdown) positionDropdown();
                if (!open && shell.contains(document.activeElement)) {{
                    btn.focus();
                }}
                shell.classList.toggle('is-open', open);
                btn.classList.toggle('is-open', open);
                btn.setAttribute('aria-expanded', open ? 'true' : 'false');
                shell.setAttribute('aria-hidden', open ? 'false' : 'true');
                if (!isDropdown) {{
                    document.body.classList.toggle('nav-drawer-open', open);
                }}
                if (open && !isDropdown && fromKeyboard) {{
                    var closeBtn = shell.querySelector('.mobile-close-btn');
                    if (closeBtn) closeBtn.focus();
                }}
            }}

            btn.addEventListener('click', function (e) {{
                setOpen(!shell.classList.contains('is-open'), e.detail === 0);
            }});

            shell.addEventListener('click', function (e) {{
                if (e.target.closest('[data-mobile-close]') || e.target.closest('.mobile-nav-links a') || e.target.closest('.mobile-drawer-actions a')) {{
                    setOpen(false, false);
                }}
            }});

            document.addEventListener('keydown', function (e) {{
                if (e.key === 'Escape' && shell.classList.contains('is-open')) {{
                    setOpen(false, true);
                    btn.focus();
                }}
            }});

            window.addEventListener('resize', function () {{
                if (window.innerWidth > 900 && shell.classList.contains('is-open')) {{
                    setOpen(false, false);
                }} else if (isDropdown && shell.classList.contains('is-open')) {{
                    positionDropdown();
                }}
            }});

            window.addEventListener('scroll', function () {{
                if (isDropdown && shell.classList.contains('is-open')) {{
                    positionDropdown();
                }}
            }}, {{ passive: true }});
        }}

        function syncThemeUI() {{
            var mode = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
            var isDark = mode === 'dark';
            var toggles = document.querySelectorAll('[data-theme-toggle]');
            for (var i = 0; i < toggles.length; i++) {{
                var t = toggles[i];
                t.setAttribute('aria-pressed', isDark ? 'true' : 'false');
                t.setAttribute('aria-label', isDark ? 'लाइट मोड चालू करें' : 'डार्क मोड चालू करें');
                var lbl = t.querySelector('.theme-toggle-label');
                if (lbl) lbl.textContent = isDark ? 'लाइट' : 'डार्क';
                var status = t.querySelector('[data-theme-status]');
                if (status) status.textContent = isDark ? 'डार्क मोड सक्रिय' : 'लाइट मोड सक्रिय';
            }}
            var logos = document.querySelectorAll('img[data-adaptive-logo="true"]');
            for (var j = 0; j < logos.length; j++) {{
                var targetSrc = isDark ? logos[j].getAttribute('data-logo-dark') : logos[j].getAttribute('data-logo-light');
                if (targetSrc && logos[j].getAttribute('src') !== targetSrc) {{
                    logos[j].setAttribute('src', targetSrc);
                }}
            }}
        }}

        function applyTheme(nextMode, persist) {{
            var mode = nextMode === 'dark' ? 'dark' : 'light';
            document.documentElement.setAttribute('data-theme', mode);
            if (persist) {{
                try {{ localStorage.setItem('aabtak-theme', mode); }} catch (err) {{}}
            }}
            syncThemeUI();
        }}

        document.addEventListener('click', function (e) {{
            var toggleBtn = e.target.closest('[data-theme-toggle]');
            if (!toggleBtn) return;
            var current = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
            applyTheme(current === 'dark' ? 'light' : 'dark', true);
        }});

        window.addEventListener('storage', function (e) {{
            if (e.key === 'aabtak-theme' && (e.newValue === 'dark' || e.newValue === 'light')) {{
                applyTheme(e.newValue, false);
            }}
        }});

        if (window.matchMedia) {{
            var mq = window.matchMedia('(prefers-color-scheme: dark)');
            var onSystemChange = function (e) {{
                if (document.documentElement.getAttribute('data-default-theme') !== 'system') return;
                var saved = null;
                try {{ saved = localStorage.getItem('aabtak-theme'); }} catch (err) {{}}
                if (saved !== 'light' && saved !== 'dark') {{
                    applyTheme(e.matches ? 'dark' : 'light', false);
                }}
            }};
            if (mq.addEventListener) mq.addEventListener('change', onSystemChange);
            else if (mq.addListener) mq.addListener(onSystemChange);
        }}

        syncThemeUI();
        if (document.readyState === 'loading') {{
            document.addEventListener('DOMContentLoaded', syncThemeUI);
        }}

        var topBar = document.querySelector('.top-bar');
        if (topBar) {{
            var scrolledPastTopBar = false;
            var descendTimer = null;

            function updateTopBarDescent() {{
                var y = window.scrollY || window.pageYOffset || 0;
                var barHeight = topBar.offsetHeight || 40;
                var primeThreshold = Math.max(80, barHeight + 40);
                var revealThreshold = Math.max(12, barHeight);

                if (y > primeThreshold) {{
                    if (!scrolledPastTopBar) {{
                        scrolledPastTopBar = true;
                        if (descendTimer) {{
                            clearTimeout(descendTimer);
                            descendTimer = null;
                        }}
                        topBar.classList.remove('top-bar-descending');
                        topBar.classList.add('top-bar-primed');
                    }}
                }} else if (scrolledPastTopBar && y <= revealThreshold) {{
                    scrolledPastTopBar = false;
                    void topBar.offsetHeight;
                    topBar.classList.add('top-bar-descending');
                    topBar.classList.remove('top-bar-primed');
                    if (descendTimer) clearTimeout(descendTimer);
                    descendTimer = setTimeout(function () {{
                        topBar.classList.remove('top-bar-descending');
                        descendTimer = null;
                    }}, 620);
                }}
            }}

            topBar.addEventListener('transitionend', function (e) {{
                if (e.target === topBar && e.propertyName === 'transform' && topBar.classList.contains('top-bar-descending')) {{
                    topBar.classList.remove('top-bar-descending');
                    if (descendTimer) {{
                        clearTimeout(descendTimer);
                        descendTimer = null;
                    }}
                }}
            }});

            window.addEventListener('scroll', updateTopBarDescent, {{ passive: true }});
            updateTopBarDescent();
        }}
    }})();
    </script>"""


def _build_footer_link(item, prefix="", active_file="", extra_class=""):
    """Formats a single footer link safely with prefix handling and active state."""
    return _build_nav_link(item, prefix=prefix, active_file=active_file, extra_class=extra_class)


def get_footer_html(config, prefix="", active_file=""):
    """Generates a customizable site-wide editorial footer from site_config.json."""
    footer = config.get("footer") or {}
    if not footer.get("enabled", True):
        return ""

    theme = str(footer.get("theme", "dark")).strip().lower()
    theme_class = "footer-theme-light" if theme == "light" else "footer-theme-dark"

    color_map = {
        "bg": "--footer-bg",
        "bottom_bg": "--footer-bottom-bg",
        "text": "--footer-text",
        "muted": "--footer-muted",
        "accent": "--footer-accent",
        "border": "--footer-border",
        "pill_bg": "--footer-pill-bg",
    }
    colors = footer.get("colors") if isinstance(footer.get("colors"), dict) else {}
    style_vars = []
    for key, css_var in color_map.items():
        val = str(colors.get(key, "") or "").strip()
        if re.match(r"^#[0-9a-fA-F]{3,8}$", val):
            style_vars.append(f"{css_var}: {val}")
    style_attr = f' style="{"; ".join(style_vars)};"' if style_vars else ""

    brand_title = html.escape(str(footer.get("brand_title", "अबतक24.in (Aabtak24.in)")).strip(), quote=True)
    brand_tagline = html.escape(str(footer.get("brand_tagline", "")).strip(), quote=True)
    contact_label = html.escape(str(footer.get("contact_label", "संपादकीय संपर्क:")).strip(), quote=True)
    contact_email = str(footer.get("contact_email", "")).strip()

    show_logo = footer.get("show_logo", True)
    logo_file = "logo.svg" if theme == "light" else "logo-light.svg"
    footer_adaptive_attrs = (
        f' data-adaptive-logo="true" data-logo-light="{prefix}logo.svg" data-logo-dark="{prefix}logo-light.svg"'
        if theme == "light" else ""
    )
    if show_logo:
        brand_head_html = (
            f'<a href="{prefix}index.html" class="footer-logo notranslate" translate="no" aria-label="अबतक24.in होमपेज">\n'
            f'                    <img src="{prefix}{logo_file}" alt="अबतक24.in" width="194" height="34" loading="lazy" decoding="async"{footer_adaptive_attrs}>\n'
            f'                </a>'
        )
    else:
        brand_head_html = f'<a href="{prefix}index.html" class="footer-brand-title notranslate" translate="no">{brand_title}</a>'

    tagline_html = f'\n                <p class="footer-tagline">{_protect_hindi_brand_text(brand_tagline)}</p>' if brand_tagline else ""

    contact_html = ""
    if contact_email:
        safe_email = html.escape(contact_email, quote=True)
        label_prefix = f"<span>{contact_label}</span> " if contact_label else ""
        contact_html = f'\n                <p class="footer-contact">{label_prefix}<a href="mailto:{safe_email}">{safe_email}</a></p>'

    pills = []
    for s_item in footer.get("social_links", []) or []:
        if isinstance(s_item, dict):
            pill = _build_footer_link(s_item, prefix=prefix, active_file=active_file, extra_class="footer-pill")
            if pill:
                pills.append(pill)
    pills_html = (
        '\n                <div class="footer-pills">\n                    '
        + "\n                    ".join(pills)
        + "\n                </div>"
        if pills else ""
    )

    col_blocks = []
    for col in footer.get("columns", []) or []:
        if not isinstance(col, dict):
            continue
        col_title = str(col.get("title", "")).strip()
        col_links = []
        for link_item in col.get("links", []) or []:
            if isinstance(link_item, dict):
                anchor = _build_footer_link(link_item, prefix=prefix, active_file=active_file)
                if anchor:
                    col_links.append(f"<li>{anchor}</li>")
        if not col_title and not col_links:
            continue
        title_html = f'<h2 class="footer-col-title">{html.escape(col_title, quote=True)}</h2>' if col_title else ""
        links_ul = (
            '<ul class="footer-links">\n                        '
            + "\n                        ".join(col_links)
            + "\n                    </ul>"
            if col_links else ""
        )
        col_blocks.append(f'<div class="footer-col">\n                    {title_html}\n                    {links_ul}\n                </div>')

    columns_html = ""
    inner_cls = "footer-inner footer-single"
    if col_blocks:
        inner_cls = "footer-inner"
        columns_html = '\n            <div class="footer-columns">\n                ' + "\n                ".join(col_blocks) + "\n            </div>"

    year_str = str(datetime.now().year)
    raw_copyright = str(footer.get("copyright_text", f"© {year_str} अबतक24.in (Aabtak24.in) • सर्वाधिकार सुरक्षित।")).strip()
    copyright_text = _protect_hindi_brand_text(html.escape(raw_copyright.replace("{year}", year_str), quote=True))
    bottom_note = str(footer.get("bottom_note", "")).strip()
    bottom_note_html = f'\n                <span class="footer-bottom-note">{_protect_hindi_brand_text(html.escape(bottom_note, quote=True))}</span>' if bottom_note else ""

    return f"""    <footer class="site-footer {theme_class}"{style_attr}>
        <div class="{inner_cls}">
            <div class="footer-brand">
                {brand_head_html}{tagline_html}{contact_html}{pills_html}
            </div>{columns_html}
        </div>
        <div class="footer-bottom">
            <div class="footer-bottom-inner">
                <p class="footer-copyright">{copyright_text}</p>{bottom_note_html}
            </div>
        </div>
    </footer>"""


def build_individual_article_page(art, today_str, config, image_resolver=None):
    """Creates a dedicated SEO-optimized HTML page for a single article inside /news/."""
    os.makedirs(NEWS_DIR, exist_ok=True)
    slug = art["slug"]
    canonical_url = f"{SITE_URL}/news/{slug}.html"

    # Normalize any old <h3> subheadings in existing articles to <h2> for sequential accessibility
    body_html = art["content_html"].replace("<h3>", "<h2>").replace("</h3>", "</h2>")
    plain_text = BeautifulSoup(body_html, "html.parser").get_text().strip()
    meta_desc = plain_text[:155].replace('"', "'") + "..."

    # Handle relative local image paths if uploaded via editor vs external URLs
    resolver = image_resolver or ImageResolver(skip_check=should_skip_image_check())
    raw_img = str(art.get("image_url", "") or "").strip()
    article_img_src, article_img_fallback = resolver.resolve(raw_img, width=900, local_prefix="../")
    og_img_url = f"{SITE_URL}/{raw_img.lstrip('./')}" if not raw_img.startswith(("http://", "https://")) else raw_img
    article_img_tag = build_img_tag(
        article_img_src,
        art["title"],
        width=800,
        height=450,
        fallback_src=article_img_fallback,
        extra_attrs='fetchpriority="high" decoding="sync"',
        self_closing=True,
    )

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
    <title>{_sanitize_title_brand(art['title'])} | Aabtak24.in</title>
    <meta name="description" content="{meta_desc}">
    <link rel="canonical" href="{canonical_url}">
    
    <!-- OpenGraph / WhatsApp / Social Preview Tags -->
    <meta property="og:type" content="article">
    <meta property="og:title" content="{art['title']}">
    <meta property="og:description" content="{meta_desc}">
    <meta property="og:image" content="{og_img_url}">
    <meta property="og:url" content="{canonical_url}">
    <meta property="og:site_name" content="Aabtak24.in">
    <meta name="twitter:card" content="summary_large_image">

    <!-- Google News JSON-LD Schema -->
    <script type="application/ld+json">{schema_json}</script>

{get_head_links(css_path="../style.css", preload_img=article_img_src, config=config)}
</head>
<body>
{get_top_bar_html(config, today_str)}

{get_header_html(config, prefix="../")}

    <main class="container">
        <article class="article-box">
            <a href="../index.html" class="back-btn">← मुख्य पृष्ठ पर वापस जाएं</a>
            <div><span class="badge">{art.get('category', 'ताज़ा खबर')}</span></div>
            <h1 class="single-title">{_protect_hindi_brand_html(art['title'])}</h1>
            <div class="meta single-meta">प्रकाशित: {art['date']} <span class="meta-dot"></span> <span class="notranslate" translate="no">अबतक24</span> न्यूज़ डेस्क</div>
            <figure class="single-figure">
                {article_img_tag}
                <figcaption class="image-credit">{_protect_hindi_brand_html(str(art.get('image_courtesy', '')))}</figcaption>
            </figure>
            <div class="article-content">
                {_protect_hindi_brand_html(body_html)}
            </div>
        </article>
    </main>

{get_footer_html(config, prefix="../")}
</body>
</html>"""

    filepath = os.path.join(NEWS_DIR, f"{slug}.html")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(article_html)


def build_category_pages(articles, today_str, config, image_resolver=None):
    """Creates a dedicated HTML page for every category defined in site_config.json."""
    resolver = image_resolver or ImageResolver(skip_check=should_skip_image_check())
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
                card_img, card_fallback = resolver.resolve(art.get("image_url", ""), width=480)
                load_attr = 'fetchpriority="high"' if idx == 0 else 'loading="lazy"'
                card_img_tag = build_img_tag(
                    card_img,
                    art["title"],
                    width=400,
                    height=200,
                    fallback_src=card_fallback,
                    extra_attrs=f'{load_attr} decoding="async"',
                )
                cards_html += f"""
                <a href="{art_link}" class="archive-card">
                    {card_img_tag}
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
    <title>{cat_name} समाचार | Aabtak24.in</title>
    <meta name="description" content="Aabtak24.in पर पढ़ें {cat_name} से जुड़ी ताज़ा और प्रमुख हिंदी खबरें।">
    <link rel="canonical" href="{SITE_URL}/{cat_file}">
{get_head_links(css_path="style.css", config=config)}
</head>
<body>
{get_top_bar_html(config, today_str)}

{get_header_html(config, prefix="", active_file=cat_file)}

    <main class="container">
        <div class="section-rule">
            <h1>{cat_name} / Category News</h1>
            <span>कुल {len(filtered)} समाचार</span>
        </div>
        <div class="archive-grid">{_protect_hindi_brand_html(cards_html)}</div>
    </main>

{get_footer_html(config, prefix="", active_file=cat_file)}
</body>
</html>"""

        with open(cat_file, "w", encoding="utf-8") as f:
            f.write(page_html)
        generated_files.append(cat_file)

    print(f"✅ Generated {len(generated_files)} category HTML pages!")
    return generated_files


def build_custom_pages(today_str, config):
    """Wraps body fragments in pages/*.html with the live header, footer, and head tags so navigation never drifts."""
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
            body_fragment = _protect_hindi_brand_html(f.read().strip())

        page_title = _sanitize_title_brand(page.get("title", f"{output_file} | Aabtak24.in"))
        meta_desc = page.get("meta_description", "Aabtak24.in — डिजिटल हिंदी समाचार पोर्टल।").replace('"', "'")

        full_html = f"""<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{page_title}</title>
    <meta name="description" content="{meta_desc}">
    <link rel="canonical" href="{SITE_URL}/{output_file}">
{get_head_links(css_path="style.css", config=config)}
</head>
<body>
{get_top_bar_html(config, today_str)}

{get_header_html(config, prefix="", active_file=output_file)}

    <main class="container">
        {body_fragment}
    </main>

{get_footer_html(config, prefix="", active_file=output_file)}
</body>
</html>"""

        with open(output_file, "w", encoding="utf-8") as f:
            f.write(full_html)
        generated_files.append(output_file)

    print(f"✅ Generated {len(generated_files)} custom HTML pages!")
    return generated_files


def build_404_page(articles, today_str, config, image_resolver=None):
    """Generates a branded, editorial custom 404.html page with root-relative paths for GitHub Pages."""
    resolver = image_resolver or ImageResolver(skip_check=should_skip_image_check())

    cat_pills = []
    for cat in config.get("categories", []) or []:
        if not isinstance(cat, dict):
            continue
        cat_name = str(cat.get("name", "") or "").strip()
        cat_file = os.path.basename(str(cat.get("file", "") or "").strip())
        if not cat_name or not cat_file:
            continue
        cat_pills.append(
            f'<a href="/{html.escape(cat_file, quote=True)}" class="error-404-cat-pill">{html.escape(cat_name, quote=True)}</a>'
        )

    cats_html = ""
    if cat_pills:
        cats_html = f"""
            <div class="error-404-cats">
                <p class="error-404-cats-label">प्रमुख श्रेणियाँ चुनें / Browse Categories</p>
                <div class="error-404-cat-list">
                    {" ".join(cat_pills)}
                </div>
            </div>"""

    recent_articles = list(articles[:6]) if articles else []
    recent_section_html = ""
    if recent_articles:
        cards = ""
        for art in recent_articles:
            art_slug = clean_slug(art.get("slug", "news-story"))
            art_link = f"/news/{art_slug}.html"
            art_title = str(art.get("title", "") or "").strip()
            art_cat = str(art.get("category", "समाचार") or "समाचार").strip()
            art_date = str(art.get("date", "") or "").strip()
            card_img, card_fallback = resolver.resolve(art.get("image_url", ""), width=480, local_prefix="/")
            card_img_tag = build_img_tag(
                card_img,
                art_title,
                width=400,
                height=200,
                fallback_src=card_fallback,
                extra_attrs='loading="lazy" decoding="async"',
            )
            cards += f"""
            <a href="{html.escape(art_link, quote=True)}" class="archive-card">
                {card_img_tag}
                <div class="archive-card-body">
                    <span class="badge">{html.escape(art_cat, quote=True)}</span>
                    <h3 class="side-title">{html.escape(art_title, quote=True)}</h3>
                    <div class="meta">{html.escape(art_date, quote=True)}</div>
                </div>
            </a>"""
        recent_section_html = f"""
        <div class="section-rule">
            <h2>प्रमुख ताज़ा खबरें / Latest Top Stories</h2>
            <span>ताज़ा अपडेट्स</span>
        </div>
        <div class="archive-grid">{cards}</div>"""

    page_html = f"""<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>पृष्ठ नहीं मिला (404) | Aabtak24.in</title>
    <meta name="description" content="क्षमा करें, आप जिस पृष्ठ को खोज रहे हैं वह उपलब्ध नहीं है या हटाया जा चुका है। Aabtak24.in के मुख्य पृष्ठ पर जाएं और ताज़ा हिंदी खबरें पढ़ें।">
    <meta name="robots" content="noindex, follow">
{get_head_links(css_path="/style.css", config=config)}
</head>
<body>
{get_top_bar_html(config, today_str)}

{get_header_html(config, prefix="/")}

    <main class="container">
        <section class="error-404-box" aria-labelledby="error-404-heading">
            <div class="error-404-code-wrap">
                <span class="badge">त्रुटि 404 • Page Not Found</span>
                <div class="error-404-number" aria-hidden="true">404</div>
            </div>
            <h1 class="error-404-title" id="error-404-heading">क्षमा करें, यह पृष्ठ या खबर उपलब्ध नहीं है</h1>
            <p class="error-404-desc">हो सकता है कि यह लिंक पुराना हो गया हो, समाचार हटा दिया गया हो या वेब पता गलत टाइप हुआ हो। आप नीचे दिए गए विकल्पों से सीधे मुख्य पृष्ठ या अपनी पसंदीदा श्रेणी पर जा सकते हैं।</p>
            <p class="error-404-path" id="error-404-path" hidden>अनुरोधित पता: <code id="error-404-path-code"></code></p>
            <div class="error-404-actions">
                <a href="/index.html" class="btn-404-primary">मुख्य पृष्ठ पर जाएं (Home)</a>
                <a href="/index.html" class="btn-404-secondary" id="error-404-back-btn">← पिछले पृष्ठ पर लौटें</a>
            </div>{cats_html}
        </section>
{_protect_hindi_brand_html(recent_section_html)}
    </main>

{get_footer_html(config, prefix="/")}
    <script>
    (function () {{
        var pathWrap = document.getElementById('error-404-path');
        var pathCode = document.getElementById('error-404-path-code');
        if (pathWrap && pathCode && window.location && window.location.pathname) {{
            var cleanPath = window.location.pathname;
            if (cleanPath && cleanPath !== '/404.html') {{
                pathCode.textContent = cleanPath;
                pathWrap.hidden = false;
            }}
        }}
        var backBtn = document.getElementById('error-404-back-btn');
        if (backBtn) {{
            backBtn.addEventListener('click', function (e) {{
                if (window.history.length > 1 && document.referrer) {{
                    e.preventDefault();
                    window.history.back();
                }}
            }});
        }}
    }})();
    </script>
</body>
</html>"""

    with open("404.html", "w", encoding="utf-8") as f:
        f.write(page_html)
    print("✅ Generated custom 404.html page!")
    return "404.html"


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
    protected = {"index.html", "404.html"}

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


def _resolve_section_category(category_key, config):
    """Resolves a homepage section's category setting ('all', category id, name, or alias) to a category dict or None."""
    cleaned = str(category_key or "all").strip()
    if not cleaned or cleaned.lower() == "all":
        return None
    lower_key = cleaned.lower()
    for cat in config.get("categories", []) or []:
        if not isinstance(cat, dict):
            continue
        if str(cat.get("id", "") or "").strip().lower() == lower_key:
            return cat
    for cat in config.get("categories", []) or []:
        if not isinstance(cat, dict):
            continue
        cat_name = str(cat.get("name", "") or "").strip()
        aliases = {str(a).strip() for a in (cat.get("aliases") or []) if str(a).strip()} | ({cat_name} if cat_name else set())
        if cleaned in aliases:
            return cat
    return None


def _filter_articles_for_section(articles, category_key, config, max_articles, seen_slugs, deduplicate):
    """Selects up to max_articles matching a section's category while optionally skipping already-shown articles."""
    cleaned = str(category_key or "all").strip()
    matched_cat = _resolve_section_category(cleaned, config)

    if not cleaned or cleaned.lower() == "all":
        candidates = list(articles)
    elif matched_cat is not None:
        cat_name = str(matched_cat.get("name", "") or "").strip()
        valid_aliases = {str(a).strip() for a in (matched_cat.get("aliases") or []) if str(a).strip()} | ({cat_name} if cat_name else set())
        candidates = [a for a in articles if str(a.get("category", "") or "").strip() in valid_aliases]
    else:
        candidates = [a for a in articles if str(a.get("category", "") or "").strip() == cleaned]

    if deduplicate:
        candidates = [a for a in candidates if a.get("slug") not in seen_slugs]

    selected = candidates[:max_articles]
    for art in selected:
        if art.get("slug"):
            seen_slugs.add(art["slug"])
    return selected, matched_cat


def _extract_article_excerpt(content_html, max_chars=145):
    """Extracts a clean plain-text excerpt from article HTML for featured magazine lead cards."""
    plain = BeautifulSoup(str(content_html or ""), "html.parser").get_text().strip()
    plain = re.sub(r"\s+", " ", plain)
    if len(plain) <= max_chars:
        return plain
    trimmed = plain[:max_chars].rsplit(" ", 1)[0].strip() or plain[:max_chars].strip()
    return f"{trimmed}..."


def _is_manual_article(art):
    """Returns True if an article was written manually (not fetched from an external RSS source)."""
    if not isinstance(art, dict):
        return False
    source_name = str(art.get("source_name", "") or "").strip()
    source_lower = source_name.lower()
    if (
        "अबतक24" in source_name
        or "aabtak24" in source_lower
        or source_lower in ("manual", "editor", "news desk", "संपादकीय डेस्क")
    ):
        return True
    orig_link = str(art.get("original_link", "") or "").strip()
    if not orig_link or "aabtak24.in" in orig_link.lower() or not orig_link.startswith(("http://", "https://")):
        return True
    return False


def _estimate_read_minutes(content_html):
    """Estimates reading time in minutes from article HTML."""
    plain = BeautifulSoup(str(content_html or ""), "html.parser").get_text().strip()
    words = [w for w in re.split(r"\s+", plain) if w]
    return max(2, min(15, round(len(words) / 140) or 2))


def _select_editorial_desk_articles(articles, section, config, seen_slugs, deduplicate):
    """Selects articles for the 3-column Editorial Desk layout:
    - Center (1 article): Latest Politics ('राजनीति') article (or section category if set), with fallback.
    - Left (up to 2-3 articles): Manually written articles first, backfilled with latest posts if needed.
    - Right (remaining up to max_articles): Latest available articles for 'ताज़ा खबरें' (Just In).
    """
    max_articles = max(1, int(section.get("max_articles", 9) or 9))
    category_key = str(section.get("category", "all") or "all").strip()
    matched_cat = _resolve_section_category(category_key, config)

    pool = [a for a in articles if not (deduplicate and a.get("slug") in seen_slugs)]
    if not pool:
        return None, matched_cat

    target_cat_key = category_key if (category_key and category_key.lower() != "all") else "politics"
    center_cat_obj = _resolve_section_category(target_cat_key, config)
    politics_aliases = {"राजनीति", "चुनाव", "politics", "Politics"}
    if center_cat_obj:
        cname = str(center_cat_obj.get("name", "") or "").strip()
        if cname:
            politics_aliases.add(cname)
        for al in center_cat_obj.get("aliases") or []:
            if str(al).strip():
                politics_aliases.add(str(al).strip())
    elif target_cat_key.lower() != "politics":
        politics_aliases = {target_cat_key}

    center_article = next(
        (a for a in pool if str(a.get("category", "") or "").strip() in politics_aliases),
        None,
    )
    if center_article is None:
        center_article = pool[0]

    used_local = {id(center_article)}
    if center_article.get("slug"):
        used_local.add(center_article["slug"])

    def _is_unused(art):
        slug = art.get("slug")
        return id(art) not in used_local and (not slug or slug not in used_local)

    def _mark_used(art):
        used_local.add(id(art))
        if art.get("slug"):
            used_local.add(art["slug"])

    remaining_budget = max(0, max_articles - 1)
    if remaining_budget == 0:
        left_target = 0
    elif remaining_budget <= 3:
        left_target = 1
    elif remaining_budget >= 10:
        left_target = 3
    else:
        left_target = 2

    left_articles = []
    for art in pool:
        if len(left_articles) >= left_target:
            break
        if _is_unused(art) and _is_manual_article(art):
            left_articles.append(art)
            _mark_used(art)

    if len(left_articles) < left_target:
        for art in pool:
            if len(left_articles) >= left_target:
                break
            if _is_unused(art):
                left_articles.append(art)
                _mark_used(art)

    right_target = max(0, remaining_budget - len(left_articles))
    right_articles = []
    for art in pool:
        if len(right_articles) >= right_target:
            break
        if _is_unused(art):
            right_articles.append(art)
            _mark_used(art)

    all_selected = [center_article] + left_articles + right_articles
    for art in all_selected:
        if art.get("slug"):
            seen_slugs.add(art["slug"])

    return {
        "center": center_article,
        "left": left_articles,
        "right": right_articles,
        "all": all_selected,
    }, matched_cat


def _resolve_section_bg_theme(section, section_index=0):
    """Resolves a section's background color theme ('sand', 'crimson', 'slate', 'sage', 'amber', or 'default')."""
    raw_bg = str(section.get("bg_theme", "auto") or "auto").strip().lower()
    if raw_bg not in SECTION_BG_THEMES or raw_bg == "auto":
        return AUTO_SECTION_BG_CYCLE[section_index % len(AUTO_SECTION_BG_CYCLE)]
    return raw_bg


def _render_homepage_section(section, sec_articles, matched_cat, resolver, is_first_section=False, section_index=0):
    """Renders one configurable homepage section in any of the 6 supported layouts and returns (html, lcp_img_url)."""
    if not sec_articles:
        return "", None

    layout = section.get("layout", "grid")
    if layout not in HOMEPAGE_LAYOUTS:
        layout = "grid"

    resolved_bg = _resolve_section_bg_theme(section, section_index=section_index)
    sec_id = html.escape(str(section.get("id", "section")), quote=True)
    sec_title = html.escape(str(section.get("title", "समाचार")), quote=True)
    sec_subtitle = str(section.get("subtitle", "") or "").strip()
    show_view_all = bool(section.get("show_view_all", False))

    view_all_html = ""
    if show_view_all and matched_cat and matched_cat.get("file"):
        cat_file = html.escape(os.path.basename(str(matched_cat["file"]).strip()), quote=True)
        cat_label = html.escape(str(matched_cat.get("name", "") or "सभी").strip(), quote=True)
        view_all_html = f'<a href="{cat_file}" class="section-view-all" aria-label="{cat_label} के सभी समाचार देखें">सभी देखें →</a>'

    right_parts = []
    if sec_subtitle:
        right_parts.append(f'<span class="section-subtitle">{html.escape(sec_subtitle, quote=True)}</span>')
    if view_all_html:
        right_parts.append(view_all_html)
    right_html = f'<div class="section-rule-right">{" ".join(right_parts)}</div>' if right_parts else ""

    # Preserve single semantic <h1> per page: hero/magazine/editorial-desk lead gets <h1> on first section; other layouts promote section heading
    uses_card_h1 = is_first_section and layout in ("editorial-desk", "bento", "magazine")
    rule_heading_tag = "h1" if (is_first_section and not uses_card_h1) else "h2"
    rule_html = "" if layout == "editorial-desk" else f"""        <div class="section-rule">
            <{rule_heading_tag}>{sec_title}</{rule_heading_tag}>
            {right_html}
        </div>\n"""

    lcp_img = None
    body_html = ""

    if layout == "editorial-desk":
        if isinstance(sec_articles, dict) and "center" in sec_articles:
            center_art = sec_articles["center"]
            left_articles = sec_articles.get("left") or []
            right_articles = sec_articles.get("right") or []
        else:
            desk_bundle, _ = _select_editorial_desk_articles(
                list(sec_articles), section, {"categories": []}, set(), False
            )
            if not desk_bundle:
                return "", None
            center_art = desk_bundle["center"]
            left_articles = desk_bundle["left"]
            right_articles = desk_bundle["right"]

        # 1. Left Column: संपादक की पसंद (Manually written posts first)
        left_cards_html = ""
        for art in left_articles:
            art_link = f"news/{art['slug']}.html"
            pick_img, pick_fallback = resolver.resolve(art.get("image_url", ""), width=420)
            pick_img_tag = build_img_tag(
                pick_img,
                art.get("title", ""),
                width=400,
                height=225,
                fallback_src=pick_fallback,
                extra_attrs='loading="lazy" decoding="async"',
            )
            art_cat = html.escape(str(art.get("category", "संपादकीय") or "संपादकीय"), quote=True)
            art_title = html.escape(str(art.get("title", "")), quote=True)
            read_mins = _estimate_read_minutes(art.get("content_html", ""))
            is_manual_badge = ' data-manual-post="true"' if _is_manual_article(art) else ""
            left_cards_html += f"""
                    <a href="{art_link}" class="desk-pick-card"{is_manual_badge}>
                        <div class="desk-pick-img">{pick_img_tag}</div>
                        <div class="desk-pick-body">
                            <span class="desk-kicker">{art_cat}</span>
                            <h3 class="desk-pick-title">{art_title}</h3>
                            <div class="desk-meta-row">
                                <span class="desk-meta-author notranslate" translate="no">Aabtak24</span>
                                <span class="desk-meta-time">{read_mins} मिनट पठन</span>
                            </div>
                        </div>
                    </a>"""

        left_col_html = ""
        if left_cards_html:
            left_col_html = f"""            <div class="desk-col desk-col-left">
                <div class="desk-col-header">
                    <h2 class="desk-col-title">संपादक की पसंद</h2>
                </div>
                <div class="desk-pick-stack">{left_cards_html}
                </div>
            </div>\n"""

        # 2. Center Column: राजनीति (Politics Featured Lead in Shaded Box)
        center_link = f"news/{center_art['slug']}.html"
        center_img, center_fallback = resolver.resolve(center_art.get("image_url", ""), width=800)
        if is_first_section:
            lcp_img = center_img
        center_load_attrs = 'fetchpriority="high" loading="eager" decoding="sync"' if is_first_section else 'loading="lazy" decoding="async"'
        center_img_tag = build_img_tag(
            center_img,
            center_art.get("title", ""),
            width=800,
            height=440,
            fallback_src=center_fallback,
            extra_attrs=center_load_attrs,
        )
        center_h_tag = "h1" if is_first_section else "h2"
        center_cat = html.escape(str(center_art.get("category", "राजनीति") or "राजनीति"), quote=True)
        center_title = html.escape(str(center_art.get("title", "")), quote=True)
        center_date = html.escape(str(center_art.get("date", "")), quote=True)
        center_read_mins = _estimate_read_minutes(center_art.get("content_html", ""))
        center_excerpt = html.escape(_extract_article_excerpt(center_art.get("content_html", ""), max_chars=170), quote=True)
        center_excerpt_html = f'\n                    <div class="desk-lead-bottom"><p class="desk-lead-excerpt">{center_excerpt}</p></div>' if center_excerpt else ""

        center_col_html = f"""            <div class="desk-col desk-col-center">
                <a href="{center_link}" class="desk-lead-card">
                    <div class="desk-lead-top">
                        <span class="desk-kicker">{center_cat}</span>
                        <{center_h_tag} class="desk-lead-title">{center_title}</{center_h_tag}>
                        <div class="desk-meta-row">
                            <span class="desk-meta-author notranslate" translate="no">Aabtak24</span>
                            <span class="desk-meta-time">{center_read_mins} मिनट पठन • {center_date}</span>
                        </div>
                    </div>
                    <div class="desk-lead-img">{center_img_tag}</div>{center_excerpt_html}
                </a>
            </div>"""

        # 3. Right Column: ● ताज़ा खबरें (Just In Latest Posts)
        right_items_html = ""
        for art in right_articles:
            art_link = f"news/{art['slug']}.html"
            art_cat = html.escape(str(art.get("category", "ताज़ा खबर") or "ताज़ा खबर"), quote=True)
            art_title = html.escape(str(art.get("title", "")), quote=True)
            art_date = html.escape(str(art.get("date", "")), quote=True)
            right_items_html += f"""
                    <a href="{art_link}" class="desk-wire-item">
                        <span class="desk-kicker">{art_cat}</span>
                        <h3 class="desk-wire-title">{art_title}</h3>
                        <div class="desk-wire-meta">{art_date}</div>
                    </a>"""

        right_col_html = ""
        if right_items_html:
            right_col_html = f"""\n            <div class="desk-col desk-col-right">
                <div class="desk-col-header desk-just-in-header">
                    <h2 class="desk-col-title"><span class="just-in-dot" aria-hidden="true"></span>ताज़ा खबरें</h2>
                    <span class="just-in-arrow" aria-hidden="true">→</span>
                </div>
                <div class="desk-wire-list">{right_items_html}
                </div>
            </div>"""

        desk_cls = "editorial-desk-grid"
        if not left_col_html and not right_col_html:
            desk_cls += " editorial-desk-single"
        elif not right_col_html:
            desk_cls += " editorial-desk-two-col"

        body_html = f"""        <div class="{desk_cls}">
{left_col_html}{center_col_html}{right_col_html}
        </div>"""

    elif layout == "bento":
        hero = sec_articles[0]
        hero_link = f"news/{hero['slug']}.html"
        hero_img, hero_fallback = resolver.resolve(hero.get("image_url", ""), width=800)
        if is_first_section:
            lcp_img = hero_img
        hero_load_attrs = 'fetchpriority="high" loading="eager" decoding="sync"' if is_first_section else 'loading="lazy" decoding="async"'
        hero_img_tag = build_img_tag(
            hero_img,
            hero["title"],
            width=800,
            height=410,
            fallback_src=hero_fallback,
            extra_attrs=hero_load_attrs,
        )
        hero_h_tag = "h1" if is_first_section else "h2"
        hero_cat = html.escape(str(hero.get("category", "बड़ी खबर") or "बड़ी खबर"), quote=True)
        hero_title = html.escape(str(hero.get("title", "")), quote=True)
        hero_date = html.escape(str(hero.get("date", "")), quote=True)

        side_articles = sec_articles[1:]
        side_html = ""
        for art in side_articles:
            art_link = f"news/{art['slug']}.html"
            thumb_img, thumb_fallback = resolver.resolve(art.get("image_url", ""), width=300)
            thumb_img_tag = build_img_tag(
                thumb_img,
                art["title"],
                width=128,
                height=94,
                fallback_src=thumb_fallback,
                extra_attrs='loading="lazy" decoding="async"',
            )
            art_cat = html.escape(str(art.get("category", "प्रमुख समाचार") or "प्रमुख समाचार"), quote=True)
            art_title = html.escape(str(art.get("title", "")), quote=True)
            art_date = html.escape(str(art.get("date", "")), quote=True)
            side_html += f"""
            <a href="{art_link}" class="side-card">
                {thumb_img_tag}
                <div>
                    <span class="badge">{art_cat}</span>
                    <h2 class="side-title">{art_title}</h2>
                    <div class="meta">{art_date}</div>
                </div>
            </a>"""

        bento_cls = "bento-grid" if side_articles else "bento-grid bento-single"
        side_stack_html = f'\n            <div class="side-stack">{side_html}\n            </div>' if side_articles else ""
        body_html = f"""        <div class="{bento_cls}">
            <a href="{hero_link}" class="hero-card">
                <div class="img-wrap">{hero_img_tag}</div>
                <div class="card-body">
                    <div>
                        <span class="badge">{hero_cat}</span>
                        <{hero_h_tag} class="hero-title">{hero_title}</{hero_h_tag}>
                    </div>
                    <div class="meta">प्रकाशित: {hero_date} <span class="meta-dot"></span> 2 मिनट पठन</div>
                </div>
            </a>{side_stack_html}
        </div>"""

    elif layout == "magazine":
        lead = sec_articles[0]
        lead_link = f"news/{lead['slug']}.html"
        lead_img, lead_fallback = resolver.resolve(lead.get("image_url", ""), width=800)
        if is_first_section:
            lcp_img = lead_img
        lead_load_attrs = 'fetchpriority="high" loading="eager" decoding="sync"' if is_first_section else 'loading="lazy" decoding="async"'
        lead_img_tag = build_img_tag(
            lead_img,
            lead["title"],
            width=760,
            height=420,
            fallback_src=lead_fallback,
            extra_attrs=lead_load_attrs,
        )
        lead_h_tag = "h1" if is_first_section else "h2"
        lead_cat = html.escape(str(lead.get("category", "विशेष खबर") or "विशेष खबर"), quote=True)
        lead_title = html.escape(str(lead.get("title", "")), quote=True)
        lead_date = html.escape(str(lead.get("date", "")), quote=True)
        lead_excerpt = html.escape(_extract_article_excerpt(lead.get("content_html", ""), max_chars=150), quote=True)
        excerpt_html = f'<p class="magazine-excerpt">{lead_excerpt}</p>' if lead_excerpt else ""

        rest_articles = sec_articles[1:]
        list_html = ""
        for art in rest_articles:
            art_link = f"news/{art['slug']}.html"
            thumb_img, thumb_fallback = resolver.resolve(art.get("image_url", ""), width=260)
            thumb_img_tag = build_img_tag(
                thumb_img,
                art["title"],
                width=112,
                height=82,
                fallback_src=thumb_fallback,
                extra_attrs='loading="lazy" decoding="async"',
            )
            art_cat = html.escape(str(art.get("category", "समाचार") or "समाचार"), quote=True)
            art_title = html.escape(str(art.get("title", "")), quote=True)
            art_date = html.escape(str(art.get("date", "")), quote=True)
            list_html += f"""
            <a href="{art_link}" class="magazine-item">
                {thumb_img_tag}
                <div class="magazine-item-body">
                    <span class="badge">{art_cat}</span>
                    <h2 class="magazine-item-title">{art_title}</h2>
                    <div class="meta">{art_date}</div>
                </div>
            </a>"""

        mag_cls = "magazine-grid" if rest_articles else "magazine-grid magazine-single"
        mag_list_html = f'\n            <div class="magazine-list">{list_html}\n            </div>' if rest_articles else ""
        body_html = f"""        <div class="{mag_cls}">
            <a href="{lead_link}" class="magazine-lead-card">
                <div class="magazine-lead-img">{lead_img_tag}</div>
                <div class="magazine-lead-body">
                    <span class="badge">{lead_cat}</span>
                    <{lead_h_tag} class="magazine-lead-title">{lead_title}</{lead_h_tag}>
                    {excerpt_html}
                    <div class="meta">प्रकाशित: {lead_date} <span class="meta-dot"></span> पूरा लेख पढ़ें →</div>
                </div>
            </a>{mag_list_html}
        </div>"""

    elif layout == "compact-list":
        items_html = ""
        for idx, art in enumerate(sec_articles):
            art_link = f"news/{art['slug']}.html"
            thumb_img, thumb_fallback = resolver.resolve(art.get("image_url", ""), width=300)
            if is_first_section and idx == 0:
                lcp_img = thumb_img
            load_attrs = 'fetchpriority="high" loading="eager" decoding="sync"' if (is_first_section and idx == 0) else 'loading="lazy" decoding="async"'
            thumb_img_tag = build_img_tag(
                thumb_img,
                art["title"],
                width=128,
                height=94,
                fallback_src=thumb_fallback,
                extra_attrs=load_attrs,
            )
            art_cat = html.escape(str(art.get("category", "समाचार") or "समाचार"), quote=True)
            art_title = html.escape(str(art.get("title", "")), quote=True)
            art_date = html.escape(str(art.get("date", "")), quote=True)
            items_html += f"""
            <a href="{art_link}" class="side-card compact-card">
                {thumb_img_tag}
                <div>
                    <span class="badge">{art_cat}</span>
                    <h2 class="side-title">{art_title}</h2>
                    <div class="meta">{art_date}</div>
                </div>
            </a>"""
        body_html = f'        <div class="compact-list-grid">{items_html}\n        </div>'

    elif layout == "strip":
        cards_html = ""
        for idx, art in enumerate(sec_articles):
            art_link = f"news/{art['slug']}.html"
            card_img, card_fallback = resolver.resolve(art.get("image_url", ""), width=420)
            if is_first_section and idx == 0:
                lcp_img = card_img
            load_attrs = 'fetchpriority="high" loading="eager" decoding="sync"' if (is_first_section and idx == 0) else 'loading="lazy" decoding="async"'
            card_img_tag = build_img_tag(
                card_img,
                art["title"],
                width=320,
                height=180,
                fallback_src=card_fallback,
                extra_attrs=load_attrs,
            )
            art_cat = html.escape(str(art.get("category", "समाचार") or "समाचार"), quote=True)
            art_title = html.escape(str(art.get("title", "")), quote=True)
            art_date = html.escape(str(art.get("date", "")), quote=True)
            cards_html += f"""
            <a href="{art_link}" class="strip-card">
                {card_img_tag}
                <div class="strip-card-body">
                    <span class="badge">{art_cat}</span>
                    <h2 class="strip-title">{art_title}</h2>
                    <div class="meta">{art_date}</div>
                </div>
            </a>"""
        body_html = f'        <div class="strip-grid">{cards_html}\n        </div>'

    else:
        # Standard 3-Column Responsive Card Grid ("grid")
        cards_html = ""
        for idx, art in enumerate(sec_articles):
            art_link = f"news/{art['slug']}.html"
            card_img, card_fallback = resolver.resolve(art.get("image_url", ""), width=480)
            if is_first_section and idx == 0:
                lcp_img = card_img
            load_attrs = 'fetchpriority="high" loading="eager" decoding="sync"' if (is_first_section and idx == 0) else 'loading="lazy" decoding="async"'
            card_img_tag = build_img_tag(
                card_img,
                art["title"],
                width=400,
                height=200,
                fallback_src=card_fallback,
                extra_attrs=load_attrs,
            )
            art_cat = html.escape(str(art.get("category", "समाचार") or "समाचार"), quote=True)
            art_title = html.escape(str(art.get("title", "")), quote=True)
            art_date = html.escape(str(art.get("date", "")), quote=True)
            cards_html += f"""
            <a href="{art_link}" class="archive-card">
                {card_img_tag}
                <div class="archive-card-body">
                    <span class="badge">{art_cat}</span>
                    <h2 class="side-title">{art_title}</h2>
                    <div class="meta">{art_date}</div>
                </div>
            </a>"""
        body_html = f'        <div class="archive-grid">{cards_html}\n        </div>'

    section_html = f"""    <section class="homepage-section homepage-layout-{layout} section-bg-{resolved_bg}" data-section-id="{sec_id}" data-section-layout="{layout}" data-section-bg="{resolved_bg}">
{rule_html}{body_html}
    </section>"""
    return section_html, lcp_img


def build_website_html(articles, config=None, skip_image_check=None, image_resolver=None):
    """Generates index.html, category pages, custom pages, and individual article pages."""
    if config is None:
        config = load_site_config()

    today_str = datetime.now().strftime("%d %B %Y")
    if not articles:
        return

    resolver = image_resolver or ImageResolver(skip_check=should_skip_image_check(skip_image_check))
    resolver.prewarm([a.get("image_url", "") for a in articles])

    # Delete any old HTML files in news/ that were removed from articles_db.json
    os.makedirs(NEWS_DIR, exist_ok=True)
    valid_files = {f"{art['slug']}.html" for art in articles}
    for fname in os.listdir(NEWS_DIR):
        if fname.endswith(".html") and fname not in valid_files:
            os.remove(os.path.join(NEWS_DIR, fname))

    for art in articles:
        build_individual_article_page(art, today_str, config, image_resolver=resolver)

    homepage_cfg = normalize_homepage_config(config.get("homepage"))
    dedupe = bool(homepage_cfg.get("deduplicate_across_sections", True))
    sections_cfg = [s for s in homepage_cfg.get("sections", []) if s.get("enabled", True)]

    def _render_sections_list(sec_list):
        rendered_blocks = []
        seen_slugs = set()
        lcp_preload = None
        for sec in sec_list:
            if sec.get("layout") == "editorial-desk":
                sec_articles, matched_cat = _select_editorial_desk_articles(
                    articles,
                    sec,
                    config,
                    seen_slugs,
                    dedupe,
                )
            else:
                sec_articles, matched_cat = _filter_articles_for_section(
                    articles,
                    sec.get("category", "all"),
                    config,
                    sec.get("max_articles", 6),
                    seen_slugs,
                    dedupe,
                )
            if not sec_articles:
                continue
            sec_idx = len(rendered_blocks)
            is_first = sec_idx == 0
            sec_html, sec_lcp = _render_homepage_section(
                sec,
                sec_articles,
                matched_cat,
                resolver,
                is_first_section=is_first,
                section_index=sec_idx,
            )
            if sec_html:
                rendered_blocks.append(sec_html)
                if is_first and sec_lcp:
                    lcp_preload = sec_lcp
        return rendered_blocks, lcp_preload

    section_blocks, lcp_img = _render_sections_list(sections_cfg)
    if not section_blocks:
        # Safety net: never leave homepage empty if all custom sections were disabled or matched 0 articles
        section_blocks, lcp_img = _render_sections_list(DEFAULT_SITE_CONFIG["homepage"]["sections"])

    main_sections_html = _protect_hindi_brand_html("\n\n".join(section_blocks))

    html_template = f"""<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Aabtak24.in | ताज़ा हिंदी समाचार और देश-दुनिया की खबरें</title>
    <meta name="description" content="Aabtak24.in पर पढ़ें देश-विदेश, टेक्नोलॉजी, अर्थव्यवस्था, राजनीति और मनोरंजन की ताज़ा हिंदी खबरें।">
    <link rel="canonical" href="{SITE_URL}/">
    <meta property="og:type" content="website">
    <meta property="og:title" content="Aabtak24.in | हिंदी न्यूज़ पोर्टल">
    <meta property="og:url" content="{SITE_URL}/">
{get_head_links(css_path="style.css", preload_img=lcp_img, config=config)}
</head>
<body>
{get_top_bar_html(config, today_str)}

{get_header_html(config, prefix="", active_file="index.html")}

    <main class="container">
{main_sections_html}
    </main>

{get_footer_html(config, prefix="", active_file="index.html")}
</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_template)

    cat_files = build_category_pages(articles, today_str, config, image_resolver=resolver)
    custom_files = build_custom_pages(today_str, config)
    build_404_page(articles, today_str, config, image_resolver=resolver)
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
        if isinstance(chosen_idx, bool) or not isinstance(chosen_idx, int) or chosen_idx < 0 or chosen_idx >= len(shortlist):
            chosen_idx = 0
        chosen = shortlist[chosen_idx]

        print(f"🎯 Selected [{chosen_idx}] ({chosen['source_name']} | Score {chosen['score']}): {chosen['title']}")

        strategy = str(article_data.get("image_strategy", "original") or "original").strip().lower()
        search_query = str(article_data.get("stock_search_query", "technology") or "technology").strip()
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
