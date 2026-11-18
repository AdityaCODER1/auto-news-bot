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


def load_site_config():
    """Loads site_config.json with safe fallback to DEFAULT_SITE_CONFIG."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "categories" in data and "header_menu" in data:
                data.setdefault("custom_pages", [])
                footer_cfg = data.get("footer")
                if not isinstance(footer_cfg, dict):
                    data["footer"] = copy.deepcopy(DEFAULT_SITE_CONFIG["footer"])
                else:
                    for key, default_val in DEFAULT_SITE_CONFIG["footer"].items():
                        if key not in footer_cfg:
                            footer_cfg[key] = copy.deepcopy(default_val)
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


def get_head_links(css_path="style.css", preload_img=None):
    """Common <head> tags: Non-blocking Google Fonts, optional LCP preload, and CSS."""
    escaped_preload = html.escape(preload_img, quote=True) if preload_img else ""
    preload_tag = f'    <link rel="preload" as="image" href="{escaped_preload}" fetchpriority="high">\n' if escaped_preload else ""
    return f"""    <link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><rect width=%22100%22 height=%22100%22 rx=%2216%22 fill=%22%23141210%22/><text x=%2250%%22 y=%2254%%22 dominant-baseline=%22central%22 text-anchor=%22middle%22 fill=%22%23FF4C00%22 font-family=%22sans-serif%22 font-weight=%22900%22 font-size=%2252%22>अ</text></svg>">
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


def _build_footer_link(item, prefix="", active_file="", extra_class=""):
    """Formats a single footer link safely with prefix handling and active state."""
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
        target_attr = ' target="_blank" rel="noopener noreferrer"' if item.get("new_tab", default_new_tab) else ""
        cls_attr = f' class="{extra_class}"' if extra_class else ""
        return f'<a href="{safe_href}"{cls_attr}{target_attr}>{safe_label}</a>'

    clean_href = raw_href.lstrip("./")
    safe_href = html.escape(f"{prefix}{clean_href}", quote=True)
    is_active = bool(active_file and active_file == clean_href and not extra_class)
    classes = [c for c in [extra_class, "active" if is_active else ""] if c]
    cls_attr = f' class="{" ".join(classes)}"' if classes else ""
    aria_attr = ' aria-current="page"' if is_active else ""
    return f'<a href="{safe_href}"{cls_attr}{aria_attr}>{safe_label}</a>'


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
    if show_logo:
        brand_head_html = (
            f'<a href="{prefix}index.html" class="footer-logo notranslate" translate="no" aria-label="अबतक24.in होमपेज">\n'
            f'                    <img src="{prefix}{logo_file}" alt="अबतक24.in" width="194" height="34" loading="lazy" decoding="async">\n'
            f'                </a>'
        )
    else:
        brand_head_html = f'<a href="{prefix}index.html" class="footer-brand-title notranslate" translate="no">{brand_title}</a>'

    tagline_html = f'\n                <p class="footer-tagline">{brand_tagline}</p>' if brand_tagline else ""

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
    copyright_text = html.escape(raw_copyright.replace("{year}", year_str), quote=True)
    bottom_note = str(footer.get("bottom_note", "")).strip()
    bottom_note_html = f'\n                <span class="footer-bottom-note">{html.escape(bottom_note, quote=True)}</span>' if bottom_note else ""

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
                {article_img_tag}
                <figcaption class="image-credit">{art['image_courtesy']}</figcaption>
            </figure>
            <div class="article-content">
                {body_html}
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

{get_footer_html(config, prefix="", active_file=output_file)}
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

    hero = articles[0]
    hero_link = f"news/{hero['slug']}.html"
    hero_img, hero_fallback = resolver.resolve(hero.get("image_url", ""), width=800)
    hero_img_tag = build_img_tag(
        hero_img,
        hero["title"],
        width=800,
        height=410,
        fallback_src=hero_fallback,
        extra_attrs='fetchpriority="high" loading="eager" decoding="sync"',
    )

    side_html = ""
    for art in articles[1:5]:
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
        side_html += f"""
        <a href="{art_link}" class="side-card">
            {thumb_img_tag}
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
            card_img, card_fallback = resolver.resolve(art.get("image_url", ""), width=480)
            card_img_tag = build_img_tag(
                card_img,
                art["title"],
                width=400,
                height=200,
                fallback_src=card_fallback,
                extra_attrs='loading="lazy" decoding="async"',
            )
            cards += f"""
            <a href="{art_link}" class="archive-card">
                {card_img_tag}
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
                <div class="img-wrap">{hero_img_tag}</div>
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

{get_footer_html(config, prefix="", active_file="index.html")}
</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_template)

    cat_files = build_category_pages(articles, today_str, config, image_resolver=resolver)
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
