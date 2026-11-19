import copy
import html as html_lib
import html.parser
import json
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _ensure_optional_test_deps():
    """
    Provides lightweight stdlib fallbacks only when optional runtime packages
    (requests, feedparser, bs4) are absent in offline test environments.
    """
    if "requests" not in sys.modules:
        try:
            import requests  # noqa: F401
        except ImportError:
            req_mod = types.ModuleType("requests")
            exc_mod = types.ModuleType("requests.exceptions")

            class RequestException(Exception):
                pass

            class ConnectionError(RequestException):
                pass

            class Timeout(RequestException):
                pass

            class ProxyError(ConnectionError):
                pass

            exc_mod.RequestException = RequestException
            exc_mod.ConnectionError = ConnectionError
            exc_mod.Timeout = Timeout
            exc_mod.ProxyError = ProxyError

            def _unmocked(*args, **kwargs):
                raise ConnectionError("Offline test environment: requests stub called without mock")

            req_mod.exceptions = exc_mod
            req_mod.get = _unmocked
            req_mod.post = _unmocked
            sys.modules["requests"] = req_mod
            sys.modules["requests.exceptions"] = exc_mod

    if "feedparser" not in sys.modules:
        try:
            import feedparser  # noqa: F401
        except ImportError:
            fp_mod = types.ModuleType("feedparser")
            fp_mod.parse = lambda url: types.SimpleNamespace(entries=[])
            sys.modules["feedparser"] = fp_mod

    if "bs4" not in sys.modules:
        try:
            from bs4 import BeautifulSoup  # noqa: F401
        except ImportError:
            bs4_mod = types.ModuleType("bs4")

            class _SimpleImg(dict):
                pass

            class _SimpleMain:
                def __init__(self, inner_html):
                    self._inner = inner_html

                def decode_contents(self):
                    return self._inner

            class BeautifulSoup:
                def __init__(self, markup, parser="html.parser"):
                    self._markup = str(markup or "")

                def get_text(self):
                    stripped = re.sub(r"<[^>]+>", " ", self._markup)
                    return re.sub(r"\s+", " ", html_lib.unescape(stripped)).strip()

                def find(self, tag):
                    if tag == "main":
                        m = re.search(r"<main\b[^>]*>(.*?)</main>", self._markup, re.DOTALL | re.IGNORECASE)
                        return _SimpleMain(m.group(1)) if m else None
                    if tag == "img":
                        m = re.search(r'<img\b[^>]*\bsrc=["\']([^"\']+)["\']', self._markup, re.IGNORECASE)
                        return _SimpleImg({"src": m.group(1)}) if m else None
                    return None

            bs4_mod.BeautifulSoup = BeautifulSoup
            sys.modules["bs4"] = bs4_mod


_ensure_optional_test_deps()

import news_bot  # noqa: E402
import requests  # noqa: E402


def _make_article(
    slug,
    title="भारत ने नई डिजिटल नीति लागू की",
    category="देश-विदेश",
    image_url="https://example.com/photo.jpg",
    source_name="TestSource",
):
    return {
        "title": title,
        "category": category,
        "slug": slug,
        "content_html": "<p>भारत में आधुनिक तकनीक और डिजिटल नवाचार ने तेज़ी से विकास किया है। यह कदम नागरिकों के लिए उपयोगी है।</p>",
        "image_url": image_url,
        "image_courtesy": "फोटो साभार: परीक्षण डेस्क",
        "original_link": f"https://example.com/{slug}",
        "source_name": source_name,
        "date": "18 Nov 2026, 10:00 AM",
        "iso_date": "2026-11-18T10:00:00+00:00",
    }


def _valid_hindi_json_str(selected_index=0, title="भारत ने अंतरिक्ष मिशन में नया कीर्तिमान रचा"):
    return (
        f'{{"selected_index": {selected_index}, '
        f'"title": "{title}", '
        f'"category": "टेक्नोलॉजी", '
        f'"slug": "india-space-mission-2026", '
        f'"content_html": "<h2>ऐतिहासिक सफलता</h2><p>भारतीय वैज्ञानिकों ने अंतरिक्ष अनुसंधान के क्षेत्र में एक बड़ी और गौरवशाली उपलब्धि हासिल की है।</p>", '
        f'"image_strategy": "stock", '
        f'"stock_search_query": "space rocket"}}'
    )


class _ImgAndPreloadCollector(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.preloads = []
        self.imgs = []
        self._in_main = False

    def handle_starttag(self, tag, attrs):
        attr_map = dict(attrs)
        if tag == "main":
            self._in_main = True
        elif tag == "link" and attr_map.get("rel") == "preload" and attr_map.get("as") == "image":
            self.preloads.append(attr_map)
        elif tag == "img" and self._in_main:
            self.imgs.append(attr_map)

    def handle_endtag(self, tag):
        if tag == "main":
            self._in_main = False


class TestModelFallbackChain(unittest.TestCase):
    def test_exact_15_model_priority_order(self):
        expected_chain = [
            ("google", "gemini-3.8-flash"),
            ("google", "gemini-4-hydrogen"),
            ("google", "gemini-3.6-flash"),
            ("google", "gemini-3.5-flash-lite"),
            ("google", "gemini-3.1-flash-lite"),
            ("google", "gemini-3-flash-preview"),
            ("google", "gemma-4-31b-it"),
            ("google", "gemma-4-26b-a4b-it"),
            ("nvidia_nim", "nvidia/nemotron-3.5-super-vl-120b-a12b"),
            ("nvidia_nim", "mistralai/mistral-large-4-1t-a52b"),
            ("nvidia_nim", "nvidia/nemotron-3-ultra-550b-a55b"),
            ("nvidia_nim", "mistralai/mistral-small-4-119b-2603"),
            ("nvidia_nim", "qwen/qwen2.5-72b-instruct"),
            ("nvidia_nim", "openai/gpt-oss-120b"),
            ("nvidia_nim", "nvidia/nemotron-3.5-lightning-30b-a3b"),
        ]
        actual = [(e["provider"], e["model"]) for e in news_bot.MODEL_FALLBACK_CHAIN]
        self.assertEqual(actual, expected_chain)
        self.assertEqual(news_bot.MODELS_TO_TRY, [m for _, m in expected_chain])

    def test_executes_all_google_tiers_then_fails_over_to_nvidia_nim(self):
        google_calls = []
        nim_calls = []

        mock_client = MagicMock()

        def fake_google_generate(model, contents, config=None):
            google_calls.append(model)
            raise RuntimeError(f"503 UNAVAILABLE for {model}")

        mock_client.models.generate_content.side_effect = fake_google_generate

        def fake_nim_post(url, headers=None, json=None, timeout=None):
            nim_calls.append(json["model"])
            resp = MagicMock()
            if json["model"] != "qwen/qwen2.5-72b-instruct":
                resp.status_code = 503
                resp.text = "busy"
                return resp
            resp.status_code = 200
            resp.json.return_value = {
                "choices": [{"message": {"content": _valid_hindi_json_str()}, "finish_reason": "stop"}]
            }
            return resp

        with patch.object(news_bot, "client", mock_client), \
             patch.object(news_bot, "api_key", None), \
             patch.object(news_bot, "nvidia_api_key", "nim-test-key"), \
             patch.dict(os.environ, {}, clear=True), \
             patch("news_bot.requests.post", side_effect=fake_nim_post), \
             patch("news_bot.time.sleep"):
            result = news_bot.generate_with_fallback("test prompt")

        self.assertEqual(result["slug"], "india-space-mission-2026")
        self.assertEqual(
            google_calls,
            [
                "gemini-3.8-flash",
                "gemini-4-hydrogen",
                "gemini-3.6-flash",
                "gemini-3.5-flash-lite",
                "gemini-3.1-flash-lite",
                "gemini-3-flash-preview",
                "gemma-4-31b-it",
                "gemma-4-26b-a4b-it",
            ],
        )
        self.assertEqual(
            nim_calls,
            [
                "nvidia/nemotron-3.5-super-vl-120b-a12b",
                "mistralai/mistral-large-4-1t-a52b",
                "nvidia/nemotron-3-ultra-550b-a55b",
                "mistralai/mistral-small-4-119b-2603",
                "qwen/qwen2.5-72b-instruct",
            ],
        )

    def test_skips_missing_google_key_and_uses_nvidia_nim_directly(self):
        nim_calls = []

        def fake_nim_post(url, headers=None, json=None, timeout=None):
            nim_calls.append((url, headers["Authorization"], json["max_tokens"], json["model"]))
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "choices": [{"message": {"content": _valid_hindi_json_str()}, "finish_reason": "stop"}]
            }
            return resp

        with patch.object(news_bot, "client", None), \
             patch.object(news_bot, "api_key", None), \
             patch.object(news_bot, "nvidia_api_key", None), \
             patch.dict(os.environ, {"NVIDIA_API_KEY": "nim-secret"}, clear=True), \
             patch("news_bot.requests.post", side_effect=fake_nim_post), \
             patch("news_bot.time.sleep"):
            result = news_bot.generate_with_fallback("prompt")

        self.assertEqual(result["title"], "भारत ने अंतरिक्ष मिशन में नया कीर्तिमान रचा")
        self.assertEqual(
            nim_calls,
            [(news_bot.NVIDIA_NIM_API_URL, "Bearer nim-secret", 4096, "nvidia/nemotron-3.5-super-vl-120b-a12b")],
        )

    def test_auth_401_circuit_breaks_provider_while_403_and_404_continue_siblings(self):
        google_calls = []
        nim_calls = []

        mock_client = MagicMock()

        def fake_google_generate(model, contents, config=None):
            google_calls.append(model)
            if model == "gemini-3.8-flash":
                # Single model 403/unprovisioned WITHOUT invalid-key text should NOT kill Google siblings
                err = RuntimeError("403 Forbidden: preview model access restricted")
                err.status_code = 403
                raise err
            # Invalid API key (401) should circuit-break remaining 6 Google models immediately
            err = RuntimeError("401 Unauthorized")
            err.status_code = 401
            raise err

        mock_client.models.generate_content.side_effect = fake_google_generate

        def fake_nim_post(url, headers=None, json=None, timeout=None):
            nim_calls.append(json["model"])
            resp = MagicMock()
            if json["model"] == "nvidia/nemotron-3.5-super-vl-120b-a12b":
                # Model 404 should continue to next NIM model (not kill NIM)
                resp.status_code = 404
                resp.text = "Not found"
                return resp
            resp.status_code = 200
            resp.json.return_value = {
                "choices": [{"message": {"content": _valid_hindi_json_str()}, "finish_reason": "stop"}]
            }
            return resp

        with patch.object(news_bot, "client", mock_client), \
             patch.object(news_bot, "api_key", None), \
             patch.object(news_bot, "nvidia_api_key", "nim-ok"), \
             patch.dict(os.environ, {}, clear=True), \
             patch("news_bot.requests.post", side_effect=fake_nim_post), \
             patch("news_bot.time.sleep"):
            res = news_bot.generate_with_fallback("prompt")

        self.assertIn("title", res)
        self.assertEqual(google_calls, ["gemini-3.8-flash", "gemini-4-hydrogen"])
        self.assertEqual(
            nim_calls,
            ["nvidia/nemotron-3.5-super-vl-120b-a12b", "mistralai/mistral-large-4-1t-a52b"],
        )

    def test_nim_http_400_retries_once_without_response_format_and_handles_null_content(self):
        payloads = []

        def fake_nim_post(url, headers=None, json=None, timeout=None):
            payloads.append(copy.deepcopy(json))
            resp = MagicMock()
            if json["model"] == "nvidia/nemotron-3.5-super-vl-120b-a12b":
                # First model exhausts reasoning tokens -> content is null -> fail over
                resp.status_code = 200
                resp.json.return_value = {
                    "choices": [{"message": {"content": None}, "finish_reason": "length"}]
                }
                return resp
            # Second model rejects response_format with 400 then succeeds without it
            if "response_format" in json:
                resp.status_code = 400
                resp.text = "response_format json_object unsupported for this model"
                return resp
            resp.status_code = 200
            resp.json.return_value = {
                "choices": [{"message": {"content": _valid_hindi_json_str()}, "finish_reason": "stop"}]
            }
            return resp

        with patch.object(news_bot, "client", None), \
             patch.object(news_bot, "api_key", None), \
             patch.object(news_bot, "nvidia_api_key", "nim-ok"), \
             patch.dict(os.environ, {}, clear=True), \
             patch("news_bot.requests.post", side_effect=fake_nim_post), \
             patch("news_bot.time.sleep"):
            result = news_bot.generate_with_fallback("prompt")

        self.assertEqual(result["category"], "टेक्नोलॉजी")
        self.assertEqual(len(payloads), 3)
        self.assertIn("response_format", payloads[1])
        self.assertNotIn("response_format", payloads[2])

    def test_google_400_retries_without_json_config(self):
        calls = []
        mock_client = MagicMock()

        def fake_google_generate(model, contents, config=None):
            calls.append((model, config))
            if config is not None:
                err = RuntimeError("400 INVALID_ARGUMENT: JSON mode is not enabled for this model")
                err.status_code = 400
                raise err
            return MagicMock(text=_valid_hindi_json_str())

        mock_client.models.generate_content.side_effect = fake_google_generate

        with patch.object(news_bot, "client", mock_client), \
             patch.object(news_bot, "api_key", None), \
             patch.object(news_bot, "nvidia_api_key", None), \
             patch.dict(os.environ, {}, clear=True), \
             patch("news_bot.time.sleep"):
            res = news_bot.generate_with_fallback("prompt")

        self.assertEqual(res["slug"], "india-space-mission-2026")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], ("gemini-3.8-flash", {"response_mime_type": "application/json"}))
        self.assertEqual(calls[1], ("gemini-3.8-flash", None))

    def test_google_400_api_key_invalid_circuit_breaks_without_retry(self):
        google_calls = []
        nim_calls = []
        mock_client = MagicMock()

        def fake_google_generate(model, contents, config=None):
            google_calls.append(model)
            err = RuntimeError("400 INVALID_ARGUMENT: API key not valid. Please pass a valid API key.")
            err.status_code = 400
            raise err

        mock_client.models.generate_content.side_effect = fake_google_generate

        def fake_nim_post(url, headers=None, json=None, timeout=None):
            nim_calls.append(json["model"])
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "choices": [{"message": {"content": _valid_hindi_json_str()}, "finish_reason": "stop"}]
            }
            return resp

        with patch.object(news_bot, "client", mock_client), \
             patch.object(news_bot, "api_key", None), \
             patch.object(news_bot, "nvidia_api_key", "nim-ok"), \
             patch.dict(os.environ, {}, clear=True), \
             patch("news_bot.requests.post", side_effect=fake_nim_post), \
             patch("news_bot.time.sleep"):
            res = news_bot.generate_with_fallback("prompt")

        self.assertEqual(res["slug"], "india-space-mission-2026")
        self.assertEqual(google_calls, ["gemini-3.8-flash"])
        self.assertEqual(nim_calls, ["nvidia/nemotron-3.5-super-vl-120b-a12b"])


class TestAiParsingAndValidation(unittest.TestCase):
    def test_parses_think_tags_fences_preamble_and_raw_newlines(self):
        raw = (
            "<think>\nLet's draft {some scratch note} before answering.\n</think>\n"
            "Here is the Hindi JSON article:\n"
            "```json\n"
            "{\n"
            '  "selected_index": "2",\n'
            '  "title": "भारत में नई तकनीकी क्रांति की शुरुआत",\n'
            '  "category": "टेक्नोलॉजी",\n'
            '  "slug": "india-tech-revolution",\n'
            '  "content_html": "<p>भारत में डिजिटल क्रांति तेजी से आगे बढ़ रही है।\nनए शोध और {AI} से रोजगार बढ़ेंगे।</p>",\n'
            '  "image_strategy": "original",\n'
            '  "stock_search_query": "digital india"\n'
            "}\n"
            "```\n"
            "Hope that helps!"
        )
        parsed = news_bot.validate_article_payload(news_bot.parse_ai_json(raw))
        self.assertEqual(parsed["selected_index"], 2)
        self.assertIn("भारत में", parsed["title"])

    def test_rejects_english_only_or_empty_payload_and_fails_over(self):
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = [
            MagicMock(text='{"selected_index": 0, "title": "English Headline Only", "content_html": "<p>This is only English prose without Devanagari.</p>"}'),
            MagicMock(text='{"selected_index": 0, "title": "हिंदी शीर्षक", "content_html": "<p></p>"}'),
            MagicMock(text=_valid_hindi_json_str(selected_index=1, title="राष्ट्रीय स्तर पर बड़ी आर्थिक बढ़त")),
        ]

        with patch.object(news_bot, "client", mock_client), \
             patch.object(news_bot, "api_key", None), \
             patch.object(news_bot, "nvidia_api_key", None), \
             patch.dict(os.environ, {}, clear=True), \
             patch("news_bot.time.sleep"):
            result = news_bot.generate_with_fallback("prompt")

        self.assertEqual(result["title"], "राष्ट्रीय स्तर पर बड़ी आर्थिक बढ़त")
        self.assertEqual(mock_client.models.generate_content.call_count, 3)


class TestImageOptimizationAndFallback(unittest.TestCase):
    def test_optimize_img_url_encoding_and_passthru(self):
        # Local upload untouched
        self.assertEqual(news_bot.optimize_img_url("uploads/pic.jpg", width=800), "uploads/pic.jpg")

        # Existing wsrv.nl untouched (no double wrapping)
        wsrv = "https://wsrv.nl/?url=https%3A%2F%2Fexample.com%2Fa.jpg&w=800&output=webp&q=80"
        self.assertEqual(news_bot.optimize_img_url(wsrv, width=800), wsrv)

        # Preserves valid %2C encodings on Wikimedia URLs
        wiki = "https://upload.wikimedia.org/wikipedia/commons/7/7a/Indian_Military_Academy%2C_Dehradun.jpg"
        wrapped_wiki = news_bot.optimize_img_url(wiki, width=900)
        self.assertIn("Indian_Military_Academy%252C_Dehradun.jpg", wrapped_wiki)
        self.assertTrue(wrapped_wiki.startswith("https://wsrv.nl/?url="))

        # Trims outer whitespace & escapes raw spaces
        raw_spaced = "  https://example.com/my photo.jpg?im=crop,w=1200&q=80  "
        wrapped_spaced = news_bot.optimize_img_url(raw_spaced, width=480)
        self.assertNotIn(" ", wrapped_spaced)
        self.assertIn("my%2520photo.jpg", wrapped_spaced)

    def test_build_and_runtime_wsrv_fallback_across_all_5_sites_and_preloads(self):
        broken_404_hero = "https://cdn.example.com/hero_404.jpg?a=1&b=2"
        healthy_side = "https://cdn.example.com/side_ok.jpg"
        broken_mime_archive = "https://cdn.example.com/archive_json_err.jpg"
        local_upload = "uploads/editor_photo.png"

        articles = [
            _make_article("hero-story", title='भारत "डिजिटल" मिशन', category="देश-विदेश", image_url=broken_404_hero),
            _make_article("side-1", category="टेक्नोलॉजी", image_url=healthy_side),
            _make_article("side-2", category="अर्थव्यवस्था", image_url=local_upload),
            _make_article("side-3", category="राजनीति", image_url=healthy_side),
            _make_article("side-4", category="मनोरंजन", image_url=healthy_side),
            _make_article("arch-1", category="देश-विदेश", image_url=broken_mime_archive),
            _make_article("arch-2", category="देश-विदेश", image_url=healthy_side),
        ]

        probed_urls = []

        def fake_get(url, stream=True, timeout=6, allow_redirects=True, headers=None):
            probed_urls.append(url)
            resp = MagicMock()
            if "hero_404.jpg" in url:
                resp.status_code = 404
                resp.headers = {"Content-Type": "text/html"}
            elif "archive_json_err.jpg" in url:
                resp.status_code = 200
                resp.headers = {"Content-Type": "application/json; charset=utf-8"}
            else:
                resp.status_code = 200
                resp.headers = {"Content-Type": "image/webp"}
            return resp

        config = copy.deepcopy(news_bot.DEFAULT_SITE_CONFIG)
        config["custom_pages"] = []

        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch("news_bot.requests.get", side_effect=fake_get):
                    news_bot.build_website_html(articles, config=config, skip_image_check=False)

                # Deduplication: only 3 unique external URLs across all pages (local upload excluded)
                self.assertEqual(len(probed_urls), 3)

                # 1. Verify index.html: hero direct original URL (no wsrv.nl) + healthy side wsrv.nl w/ onerror + broken archive direct URL
                index_html = Path(tmpdir, "index.html").read_text(encoding="utf-8")
                collector = _ImgAndPreloadCollector()
                collector.feed(index_html)

                self.assertEqual(len(collector.preloads), 1)
                self.assertEqual(collector.preloads[0]["href"], broken_404_hero)
                self.assertNotIn("wsrv.nl", collector.preloads[0]["href"])

                # 7 images on index (hero + 4 side + 2 archive)
                self.assertEqual(len(collector.imgs), 7)
                hero_img = collector.imgs[0]
                self.assertEqual(hero_img["src"], broken_404_hero)
                self.assertEqual(hero_img["alt"], 'भारत "डिजिटल" मिशन')
                self.assertNotIn("data-fallback-src", hero_img)
                self.assertNotIn("onerror", hero_img)

                side_healthy_img = collector.imgs[1]
                self.assertTrue(side_healthy_img["src"].startswith("https://wsrv.nl/?url="))
                self.assertEqual(side_healthy_img["data-fallback-src"], healthy_side)
                self.assertIn("this.onerror=null", side_healthy_img["onerror"])

                side_local_img = collector.imgs[2]
                self.assertEqual(side_local_img["src"], local_upload)
                self.assertNotIn("onerror", side_local_img)

                archive_broken_img = collector.imgs[5]
                self.assertEqual(archive_broken_img["src"], broken_mime_archive)
                self.assertNotIn("wsrv.nl", archive_broken_img["src"])
                self.assertNotIn("onerror", archive_broken_img)

                archive_healthy_img = collector.imgs[6]
                self.assertTrue(archive_healthy_img["src"].startswith("https://wsrv.nl/?url="))
                self.assertEqual(archive_healthy_img["data-fallback-src"], healthy_side)
                self.assertIn("this.onerror=null", archive_healthy_img["onerror"])

                # 2. Verify category page (desh-videsh.html contains hero 404, broken mime, healthy)
                cat_html = Path(tmpdir, "desh-videsh.html").read_text(encoding="utf-8")
                cat_col = _ImgAndPreloadCollector()
                cat_col.feed(cat_html)
                self.assertEqual(len(cat_col.imgs), 3)
                self.assertEqual(cat_col.imgs[0]["src"], broken_404_hero)
                self.assertEqual(cat_col.imgs[1]["src"], broken_mime_archive)
                self.assertTrue(cat_col.imgs[2]["src"].startswith("https://wsrv.nl/?url="))
                self.assertEqual(cat_col.imgs[2]["data-fallback-src"], healthy_side)
                self.assertIn("this.onerror=null", cat_col.imgs[2]["onerror"])

                # 3. Verify individual article pages (hero 404 + healthy + local upload)
                hero_page = Path(tmpdir, "news", "hero-story.html").read_text(encoding="utf-8")
                hp_col = _ImgAndPreloadCollector()
                hp_col.feed(hero_page)
                self.assertEqual(hp_col.preloads[0]["href"], broken_404_hero)
                self.assertEqual(hp_col.imgs[0]["src"], broken_404_hero)
                self.assertNotIn("wsrv.nl", hero_page.split("</head>")[1])

                healthy_page = Path(tmpdir, "news", "side-1.html").read_text(encoding="utf-8")
                sp_col = _ImgAndPreloadCollector()
                sp_col.feed(healthy_page)
                self.assertTrue(sp_col.preloads[0]["href"].startswith("https://wsrv.nl/?url="))
                self.assertTrue(sp_col.imgs[0]["src"].startswith("https://wsrv.nl/?url="))
                self.assertEqual(sp_col.imgs[0]["data-fallback-src"], healthy_side)

                local_page = Path(tmpdir, "news", "side-2.html").read_text(encoding="utf-8")
                lp_col = _ImgAndPreloadCollector()
                lp_col.feed(local_page)
                self.assertEqual(lp_col.preloads[0]["href"], f"../{local_upload}")
                self.assertEqual(lp_col.imgs[0]["src"], f"../{local_upload}")
                self.assertNotIn("onerror", lp_col.imgs[0])
            finally:
                os.chdir(old_cwd)

    def test_transport_error_circuit_breaker_preserves_wsrv_and_onerror(self):
        articles = [
            _make_article(f"art-{i}", image_url=f"https://cdn.example.com/img-{i}.jpg")
            for i in range(8)
        ]
        probe_count = 0

        def fake_offline_get(*args, **kwargs):
            nonlocal probe_count
            probe_count += 1
            raise requests.exceptions.ConnectionError("proxy offline")

        config = copy.deepcopy(news_bot.DEFAULT_SITE_CONFIG)
        config["custom_pages"] = []

        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch("news_bot.requests.get", side_effect=fake_offline_get):
                    resolver = news_bot.ImageResolver(skip_check=False)
                    resolver.prewarm([a["image_url"] for a in articles], max_workers=1)
                    news_bot.build_website_html(articles, config=config, image_resolver=resolver)

                # Circuit breaker stops probing after 3 consecutive transport errors
                self.assertEqual(probe_count, 3)

                index_html = Path(tmpdir, "index.html").read_text(encoding="utf-8")
                col = _ImgAndPreloadCollector()
                col.feed(index_html)
                # All 8 images retain wsrv.nl optimization AND runtime onerror fallback
                for idx, img_attrs in enumerate(col.imgs):
                    self.assertTrue(img_attrs["src"].startswith("https://wsrv.nl/?url="))
                    self.assertEqual(img_attrs["data-fallback-src"], f"https://cdn.example.com/img-{idx}.jpg")
                    self.assertIn("this.onerror=null", img_attrs["onerror"])
            finally:
                os.chdir(old_cwd)

    def test_skip_image_check_makes_zero_probes_and_keeps_onerror(self):
        articles = [_make_article("hero", image_url="https://cdn.example.com/only.jpg")]
        config = copy.deepcopy(news_bot.DEFAULT_SITE_CONFIG)
        config["custom_pages"] = []

        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch("news_bot.requests.get") as mock_get:
                    news_bot.build_website_html(articles, config=config, skip_image_check=True)
                mock_get.assert_not_called()

                index_html = Path(tmpdir, "index.html").read_text(encoding="utf-8")
                col = _ImgAndPreloadCollector()
                col.feed(index_html)
                self.assertTrue(col.imgs[0]["src"].startswith("https://wsrv.nl/?url="))
                self.assertEqual(col.imgs[0]["data-fallback-src"], "https://cdn.example.com/only.jpg")
                self.assertIn("this.onerror=null", col.imgs[0]["onerror"])
            finally:
                os.chdir(old_cwd)


class TestHeaderCustomization(unittest.TestCase):
    def test_load_site_config_backfills_missing_header_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir, "site_config.json")
            cfg_path.write_text(
                '{"categories": [], "header_menu": [], "header": {"theme": "dark", "hamburger_style": "boxed"}}',
                encoding="utf-8",
            )
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                loaded = news_bot.load_site_config()
            finally:
                os.chdir(old_cwd)

        self.assertEqual(loaded["header"]["theme"], "dark")
        self.assertEqual(loaded["header"]["hamburger_style"], "boxed")
        self.assertEqual(loaded["header"]["mobile_menu_style"], "slide-right")
        self.assertTrue(loaded["header"]["sticky"])
        self.assertIn("action_links", loaded["header"])
        self.assertIn("colors", loaded["header"])

    def test_top_bar_customization_and_disable(self):
        cfg = copy.deepcopy(news_bot.DEFAULT_SITE_CONFIG)
        cfg["header"].update(
            {
                "theme": "dark",
                "top_bar_enabled": True,
                "top_bar_show_date": False,
                "top_bar_text": 'ब्रेकिंग: <b>लाइव</b>',
                "edition_tag": "DELHI EDITION",
                "colors": {"topbar_bg": "#111827", "topbar_text": "#F9FAFB", "accent": "not-a-hex"},
            }
        )
        bar_html = news_bot.get_top_bar_html(cfg, "18 Nov 2026")
        self.assertIn('class="top-bar top-bar-theme-dark"', bar_html)
        self.assertIn("--topbar-bg: #111827; --topbar-text: #F9FAFB;", bar_html)
        self.assertNotIn("not-a-hex", bar_html)
        self.assertNotIn("18 Nov 2026", bar_html)
        self.assertIn("&lt;b&gt;लाइव&lt;/b&gt;", bar_html)
        self.assertIn("DELHI EDITION", bar_html)

        cfg["header"]["top_bar_enabled"] = False
        self.assertEqual(news_bot.get_top_bar_html(cfg, "18 Nov 2026"), "")

        cfg["header"]["top_bar_enabled"] = True
        cfg["header"]["enabled"] = False
        self.assertEqual(news_bot.get_top_bar_html(cfg, "18 Nov 2026"), "")

    def test_header_themes_logos_layouts_nav_styles_and_disable(self):
        cfg = copy.deepcopy(news_bot.DEFAULT_SITE_CONFIG)
        cfg["header"].update(
            {
                "theme": "dark",
                "layout": "centered",
                "nav_style": "pills",
                "sticky": False,
                "show_logo": True,
                "brand_tagline": "सटीक और निष्पक्ष खबरें",
            }
        )
        dark_html = news_bot.get_header_html(cfg, prefix="../", active_file="technology.html")
        self.assertIn("header-theme-dark", dark_html)
        self.assertIn("header-layout-centered", dark_html)
        self.assertIn("header-nav-pills", dark_html)
        self.assertIn("header-static", dark_html)
        self.assertIn('src="../logo-light.svg"', dark_html)
        self.assertIn("header-tagline", dark_html)
        self.assertIn("mobile-drawer-tagline", dark_html)
        self.assertIn('class="active" aria-current="page"', dark_html)

        cfg["header"].update(
            {
                "theme": "light",
                "layout": "unknown-layout",
                "nav_style": "unknown-nav",
                "sticky": True,
                "show_logo": False,
                "brand_title": "मेरा पोर्टल",
                "mobile_show_tagline": False,
            }
        )
        text_brand_html = news_bot.get_header_html(cfg, prefix="")
        self.assertIn("header-theme-light", text_brand_html)
        self.assertIn("header-layout-inline", text_brand_html)
        self.assertIn("header-nav-underline", text_brand_html)
        self.assertIn("header-sticky", text_brand_html)
        self.assertIn('<span class="header-brand-title">मेरा पोर्टल</span>', text_brand_html)
        self.assertIn('<span class="mobile-drawer-title">मेरा पोर्टल</span>', text_brand_html)
        self.assertNotIn("logo.svg", text_brand_html)
        self.assertNotIn("mobile-drawer-tagline", text_brand_html)
        self.assertNotIn("mobile-drawer-foot", text_brand_html)

        cfg["header"]["enabled"] = False
        self.assertEqual(news_bot.get_header_html(cfg), "")

    def test_mobile_hamburger_styles_drawer_modes_and_independent_mobile_theme(self):
        cfg = copy.deepcopy(news_bot.DEFAULT_SITE_CONFIG)
        cfg["header"].update(
            {
                "theme": "light",
                "mobile_theme": "dark",
                "mobile_menu_label": "नेविगेशन",
                "colors": {"bg": "#FFF7ED", "drawer_bg": "#0F172A", "drawer_text": "#F8FAFC"},
            }
        )

        for hb_style in ("staggered", "bars", "boxed", "pill"):
            for drawer_style in ("slide-right", "slide-left", "fullscreen", "dropdown"):
                cfg["header"]["hamburger_style"] = hb_style
                cfg["header"]["mobile_menu_style"] = drawer_style
                out = news_bot.get_header_html(cfg, prefix="")
                self.assertIn(f"hamburger-style-{hb_style}", out)
                self.assertIn(f"mobile-style-{drawer_style}", out)
                self.assertIn("mobile-theme-dark", out)
                # Header stays light with logo.svg, dark mobile drawer gets logo-light.svg
                self.assertIn("header-theme-light", out)
                self.assertIn('src="logo.svg"', out)
                self.assertIn('src="logo-light.svg"', out)
                # Drawer sits AFTER </header> so backdrop-filter does not trap position:fixed
                self.assertLess(out.index("</header>"), out.index('id="site-mobile-shell"'))
                if hb_style == "pill":
                    self.assertIn('<span class="hamburger-label">नेविगेशन</span>', out)
                else:
                    self.assertNotIn("hamburger-label", out)

        # Light drawer even when desktop header is dark
        cfg["header"]["theme"] = "dark"
        cfg["header"]["mobile_theme"] = "light"
        mixed = news_bot.get_header_html(cfg)
        self.assertIn("header-theme-dark", mixed)
        self.assertIn("mobile-theme-light", mixed)
        self.assertIn('src="logo-light.svg"', mixed)
        self.assertIn('src="logo.svg"', mixed)

    def test_action_links_xss_filtering_and_color_overrides(self):
        cfg = copy.deepcopy(news_bot.DEFAULT_SITE_CONFIG)
        cfg["header"]["action_links"] = [
            {"label": "E-Paper", "href": "https://aabtak24.in/epaper", "new_tab": True, "style": "accent"},
            {"label": "संपर्क", "href": "contact.html", "style": "outline"},
            {"label": "Bad Link", "href": "javascript:alert(1)", "style": "accent"},
        ]
        cfg["header"]["colors"] = {
            "bg": "#1A202C",
            "text": "#FFFFFF",
            "accent": "#38BDF8",
            "border": "#334155",
            "drawer_bg": "expression(evil)",
        }
        out = news_bot.get_header_html(cfg, prefix="../")
        self.assertIn("header-actions desktop-actions", out)
        self.assertIn("mobile-drawer-actions", out)
        self.assertIn("header-action-btn header-action-accent", out)
        self.assertIn("header-action-btn header-action-outline", out)
        self.assertIn('href="../contact.html"', out)
        self.assertIn('target="_blank" rel="noopener noreferrer"', out)
        self.assertNotIn("Bad Link", out)
        self.assertNotIn("javascript:", out)
        self.assertIn("--header-bg: #1A202C", out)
        self.assertIn("--header-accent: #38BDF8", out)
        self.assertNotIn("expression(evil)", out)

    def test_build_website_html_propagates_custom_header_across_all_pages(self):
        articles = [_make_article("hero-story", category="देश-विदेश")]
        config = copy.deepcopy(news_bot.DEFAULT_SITE_CONFIG)
        config["header"].update(
            {
                "theme": "dark",
                "layout": "compact",
                "nav_style": "pills",
                "hamburger_style": "boxed",
                "mobile_menu_style": "slide-left",
                "mobile_theme": "dark",
                "top_bar_text": "24x7 हिंदी न्यूज़",
                "action_links": [{"label": "संपर्क करें", "href": "contact.html", "style": "accent"}],
            }
        )

        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                Path(tmpdir, "pages").mkdir(parents=True, exist_ok=True)
                Path(tmpdir, "pages", "about.html").write_text("<article class='article-box'><h1>About</h1></article>", encoding="utf-8")
                news_bot.build_website_html(articles, config=config, skip_image_check=True)

                for rel_path, expected_logo in [
                    ("index.html", "logo-light.svg"),
                    ("desh-videsh.html", "logo-light.svg"),
                    ("about.html", "logo-light.svg"),
                    ("news/hero-story.html", "../logo-light.svg"),
                ]:
                    content = Path(tmpdir, rel_path).read_text(encoding="utf-8")
                    self.assertIn("top-bar-theme-dark", content)
                    self.assertIn("24x7 हिंदी न्यूज़", content)
                    self.assertIn("header-theme-dark header-layout-compact header-nav-pills", content)
                    self.assertIn("hamburger-style-boxed", content)
                    self.assertIn("mobile-style-slide-left mobile-theme-dark", content)
                    self.assertIn(expected_logo, content)
                    self.assertIn("site-mobile-shell", content)

                news_html = Path(tmpdir, "news", "hero-story.html").read_text(encoding="utf-8")
                self.assertIn('href="../contact.html"', news_html)
                about_html = Path(tmpdir, "about.html").read_text(encoding="utf-8")
                self.assertIn('<a href="about.html" class="active" aria-current="page">', about_html)
            finally:
                os.chdir(old_cwd)


class TestCustom404Page(unittest.TestCase):
    def test_build_website_html_generates_root_relative_404_with_latest_six_stories(self):
        articles = [
            _make_article(f"story-{i}", title=f"ताज़ा खबर {i}", category="देश-विदेश", image_url="uploads/editor_photo.png" if i == 0 else "https://cdn.example.com/ok.jpg")
            for i in range(8)
        ]
        config = copy.deepcopy(news_bot.DEFAULT_SITE_CONFIG)
        config["custom_pages"] = []
        config["header"]["theme"] = "dark"

        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                news_bot.build_website_html(articles, config=config, skip_image_check=True)
                page_path = Path(tmpdir, "404.html")
                self.assertTrue(page_path.exists())
                html_404 = page_path.read_text(encoding="utf-8")

                # SEO & root-relative asset/navigation paths for nested missing URLs
                self.assertIn('<meta name="robots" content="noindex, follow">', html_404)
                self.assertIn('href="/style.css"', html_404)
                self.assertIn('src="/logo-light.svg"', html_404)
                self.assertIn('href="/index.html"', html_404)
                self.assertIn('src="/uploads/editor_photo.png"', html_404)

                # Category quick-links
                for cat in config["categories"]:
                    self.assertIn(f'href="/{cat["file"]}"', html_404)

                # Shows at most 6 latest news articles with root-relative /news/ URLs
                for i in range(6):
                    self.assertIn(f'href="/news/story-{i}.html"', html_404)
                self.assertNotIn('/news/story-6.html', html_404)
                self.assertNotIn('/news/story-7.html', html_404)

                # Excluded from sitemap.xml
                sitemap = Path(tmpdir, "sitemap.xml").read_text(encoding="utf-8")
                self.assertNotIn("404.html", sitemap)
            finally:
                os.chdir(old_cwd)

    def test_build_404_page_handles_empty_articles_and_cleanup_protection(self):
        config = copy.deepcopy(news_bot.DEFAULT_SITE_CONFIG)
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                news_bot.build_404_page([], "18 November 2026", config)
                html_404 = Path(tmpdir, "404.html").read_text(encoding="utf-8")
                self.assertIn("error-404-box", html_404)
                self.assertIn('href="/index.html"', html_404)
                self.assertNotIn('class="archive-grid"', html_404)

                # Simulate stale manifest trying to clean up 404.html
                Path(tmpdir, news_bot.GENERATED_MANIFEST).write_text('["404.html", "old-cat.html"]', encoding="utf-8")
                Path(tmpdir, "old-cat.html").write_text("old", encoding="utf-8")
                news_bot.cleanup_retired_root_pages(["desh-videsh.html"])
                self.assertTrue(Path(tmpdir, "404.html").exists())
                self.assertFalse(Path(tmpdir, "old-cat.html").exists())
            finally:
                os.chdir(old_cwd)


class TestHomepageCustomizationAndThemeToggle(unittest.TestCase):
    def test_load_site_config_backfills_homepage_and_theme_defaults(self):
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                # Legacy site_config.json without homepage or theme toggle fields
                Path(tmpdir, "site_config.json").write_text(
                    json.dumps(
                        {
                            "categories": news_bot.DEFAULT_SITE_CONFIG["categories"],
                            "header": {"theme": "light"},
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                cfg = news_bot.load_site_config()
                self.assertIn("homepage", cfg)
                self.assertEqual(cfg["homepage"]["preset"], "classic-bento")
                self.assertTrue(cfg["homepage"]["deduplicate_across_sections"])
                self.assertEqual(len(cfg["homepage"]["sections"]), 2)
                self.assertTrue(cfg["header"]["theme_toggle_enabled"])
                self.assertEqual(cfg["header"]["default_color_mode"], "dark")

                # Invalid homepage values are safely normalized & clamped
                norm = news_bot.normalize_homepage_config(
                    {
                        "preset": "unknown-preset",
                        "deduplicate_across_sections": False,
                        "sections": [
                            {"id": "s1", "title": "Clamp High", "layout": "invalid", "max_articles": 999, "category": "all", "bg_theme": "unknown-bg"},
                            {"id": "s2", "title": "Clamp Low", "layout": "strip", "max_articles": 0, "category": "technology", "bg_theme": "crimson"},
                        ],
                    }
                )
                self.assertEqual(norm["preset"], "custom")
                self.assertFalse(norm["deduplicate_across_sections"])
                self.assertEqual(norm["sections"][0]["layout"], "grid")
                self.assertEqual(norm["sections"][0]["max_articles"], 50)
                self.assertEqual(norm["sections"][0]["bg_theme"], "auto")
                self.assertEqual(norm["sections"][1]["layout"], "strip")
                self.assertEqual(norm["sections"][1]["max_articles"], 1)
                self.assertEqual(norm["sections"][1]["bg_theme"], "crimson")
                self.assertTrue(norm["sections"][1]["show_view_all"])
            finally:
                os.chdir(old_cwd)

    def test_editorial_desk_three_column_layout_manual_politics_justin_and_section_bgs(self):
        manual_post = _make_article(
            "manual-1",
            title="मैनुअल संपादकीय विशेष रिपोर्ट",
            category="देश-विदेश",
            source_name="अबतक24 न्यूज़ डेस्क",
        )
        manual_post["original_link"] = "https://aabtak24.in/news/manual-1.html"

        politics_post = _make_article(
            "pol-lead",
            title="संसद सत्र और राजनीति की बड़ी खबर",
            category="राजनीति",
            source_name="The Hindu",
        )
        fetched_posts = [
            _make_article(f"wire-{i}", title=f"ताज़ा फ़ीड समाचार {i}", category="देश-विदेश", source_name="BBC News")
            for i in range(1, 12)
        ]
        # Order: latest fetched post first, then politics post, more fetched posts, and manual post deeper in list
        articles = [fetched_posts[0], politics_post] + fetched_posts[1:5] + [manual_post] + fetched_posts[5:]

        config = copy.deepcopy(news_bot.DEFAULT_SITE_CONFIG)
        config["custom_pages"] = []
        config["homepage"] = copy.deepcopy(news_bot.HOMEPAGE_PRESETS["wire-editorial"])

        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                news_bot.build_website_html(articles, config=config, skip_image_check=True)
                index_html = Path(tmpdir, "index.html").read_text(encoding="utf-8")

                # Default color mode is dark
                self.assertIn("var d='dark';", index_html)
                # Single semantic <h1> across the entire page
                self.assertEqual(len(re.findall(r"<h1\b", index_html)), 1)

                # Extract top editorial-desk section
                desk_match = re.search(
                    r'<section class="homepage-section homepage-layout-editorial-desk section-bg-sand"[^>]*>(.*?)</section>',
                    index_html,
                    flags=re.S,
                )
                self.assertIsNotNone(desk_match)
                desk_html = desk_match.group(1)

                # Left column ("संपादक की पसंद") prioritizes manual post + backfills second slot
                left_match = re.search(r'<div class="desk-col desk-col-left">(.*?)</div>\s*<div class="desk-col desk-col-center">', desk_html, flags=re.S)
                self.assertIsNotNone(left_match)
                left_html = left_match.group(1)
                self.assertIn("संपादक की पसंद", left_html)
                self.assertIn("news/manual-1.html", left_html)
                self.assertIn('data-manual-post="true"', left_html)
                self.assertEqual(len(re.findall(r'class="desk-pick-card"', left_html)), 2)

                # Center column ("राजनीति") has the Politics article in the shaded lead box with <h1> and "Aabtak24" author label
                center_match = re.search(r'<div class="desk-col desk-col-center">(.*?)</div>\s*<div class="desk-col desk-col-right">', desk_html, flags=re.S)
                self.assertIsNotNone(center_match)
                center_html = center_match.group(1)
                self.assertIn("news/pol-lead.html", center_html)
                self.assertIn('<h1 class="desk-lead-title">', center_html)
                self.assertIn("संसद सत्र और राजनीति की बड़ी खबर", center_html)
                self.assertIn(">Aabtak24</span>", center_html)
                self.assertIn(">Aabtak24</span>", left_html)
                self.assertNotIn("The Hindu", desk_html)
                self.assertNotIn("BBC News", desk_html)

                # Right column ("ताज़ा खबरें") has the 6 latest remaining posts and does not duplicate left/center
                right_match = re.search(r'<div class="desk-col desk-col-right">(.*)', desk_html, flags=re.S)
                self.assertIsNotNone(right_match)
                right_html = right_match.group(1)
                self.assertIn("ताज़ा खबरें", right_html)
                self.assertIn('class="just-in-dot"', right_html)
                self.assertNotIn("news/manual-1.html", right_html)
                self.assertNotIn("news/pol-lead.html", right_html)
                self.assertEqual(len(re.findall(r'class="desk-wire-item"', right_html)), 6)

                # Verify lower sections have distinct background color classes
                self.assertIn("section-bg-crimson", index_html)
            finally:
                os.chdir(old_cwd)

    def test_all_five_homepage_layouts_and_article_limits(self):
        articles = [
            _make_article(f"art-{i:02d}", title=f"ताज़ा खबर संख्या {i}", category="देश-विदेश")
            for i in range(1, 25)
        ]
        config = copy.deepcopy(news_bot.DEFAULT_SITE_CONFIG)
        config["custom_pages"] = []
        config["homepage"] = {
            "preset": "custom",
            "deduplicate_across_sections": True,
            "sections": [
                {"id": "sec-bento", "title": "बेंटो", "subtitle": "टॉप", "category": "all", "layout": "bento", "max_articles": 3, "show_view_all": False, "enabled": True},
                {"id": "sec-mag", "title": "मैगज़ीन", "subtitle": "फ़ीचर", "category": "all", "layout": "magazine", "max_articles": 4, "show_view_all": False, "enabled": True},
                {"id": "sec-strip", "title": "स्ट्रिप", "subtitle": "हाइलाइट्स", "category": "all", "layout": "strip", "max_articles": 4, "show_view_all": False, "enabled": True},
                {"id": "sec-compact", "title": "कॉम्पैक्ट", "subtitle": "वायर", "category": "all", "layout": "compact-list", "max_articles": 4, "show_view_all": False, "enabled": True},
                {"id": "sec-grid", "title": "ग्रिड", "subtitle": "अन्य", "category": "all", "layout": "grid", "max_articles": 3, "show_view_all": False, "enabled": True},
            ],
        }

        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                news_bot.build_website_html(articles, config=config, skip_image_check=True)
                index_html = Path(tmpdir, "index.html").read_text(encoding="utf-8")

                # Exactly one semantic <h1> across the entire homepage
                self.assertEqual(len(re.findall(r"<h1\b", index_html)), 1)

                # Verify each layout container and strict per-section article limit
                sec_blocks = dict(
                    re.findall(
                        r'<section class="homepage-section [^"]+" data-section-id="([^"]+)"[^>]*>(.*?)</section>',
                        index_html,
                        flags=re.S,
                    )
                )
                self.assertEqual(set(sec_blocks.keys()), {"sec-bento", "sec-mag", "sec-strip", "sec-compact", "sec-grid"})

                self.assertIn('class="bento-grid"', sec_blocks["sec-bento"])
                self.assertEqual(len(re.findall(r'href="news/art-\d+\.html"', sec_blocks["sec-bento"])), 3)

                self.assertIn('class="magazine-grid"', sec_blocks["sec-mag"])
                self.assertIn('class="magazine-excerpt"', sec_blocks["sec-mag"])
                self.assertEqual(len(re.findall(r'href="news/art-\d+\.html"', sec_blocks["sec-mag"])), 4)

                self.assertIn('class="strip-grid"', sec_blocks["sec-strip"])
                self.assertEqual(len(re.findall(r'href="news/art-\d+\.html"', sec_blocks["sec-strip"])), 4)

                self.assertIn('class="compact-list-grid"', sec_blocks["sec-compact"])
                self.assertEqual(len(re.findall(r'href="news/art-\d+\.html"', sec_blocks["sec-compact"])), 4)

                self.assertIn('class="archive-grid"', sec_blocks["sec-grid"])
                self.assertEqual(len(re.findall(r'href="news/art-\d+\.html"', sec_blocks["sec-grid"])), 3)

                # Total unique articles shown = 3 + 4 + 4 + 4 + 3 = 18; art-19..24 must not appear
                self.assertIn("news/art-18.html", index_html)
                self.assertNotIn("news/art-19.html", index_html)

                # Single-article edge cases for bento & magazine add single-column modifier classes
                config["homepage"]["sections"] = [
                    {"id": "solo-bento", "title": "सोलो बेंटो", "category": "all", "layout": "bento", "max_articles": 1, "enabled": True},
                    {"id": "solo-mag", "title": "सोलो मैगज़ीन", "category": "all", "layout": "magazine", "max_articles": 1, "enabled": True},
                ]
                news_bot.build_website_html(articles, config=config, skip_image_check=True)
                solo_html = Path(tmpdir, "index.html").read_text(encoding="utf-8")
                self.assertIn('class="bento-grid bento-single"', solo_html)
                self.assertIn('class="magazine-grid magazine-single"', solo_html)
            finally:
                os.chdir(old_cwd)

    def test_category_filtering_aliases_view_all_and_deduplication(self):
        articles = [
            _make_article("tech-1", title="एआई खबर 1", category="टेक्नोलॉजी"),
            _make_article("tech-2", title="विज्ञान खबर 2", category="विज्ञान"),  # alias of technology
            _make_article("tech-3", title="एआई खबर 3", category="AI"),          # alias of technology
            _make_article("pol-1", title="राजनीति खबर 1", category="राजनीति"),
            _make_article("pol-2", title="चुनाव खबर 2", category="चुनाव"),      # alias of politics
        ]
        config = copy.deepcopy(news_bot.DEFAULT_SITE_CONFIG)
        config["custom_pages"] = []
        config["homepage"] = {
            "preset": "custom",
            "deduplicate_across_sections": True,
            "sections": [
                {"id": "top", "title": "शीर्ष", "category": "all", "layout": "strip", "max_articles": 2, "show_view_all": False, "enabled": True},
                {"id": "tech", "title": "टेक्नोलॉजी डेस्क", "category": "technology", "layout": "grid", "max_articles": 5, "show_view_all": True, "enabled": True},
                {"id": "pol", "title": "राजनीति डेस्क", "category": "राजनीति", "layout": "compact-list", "max_articles": 2, "show_view_all": True, "enabled": True},
            ],
        }

        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                news_bot.build_website_html(articles, config=config, skip_image_check=True)
                dedup_html = Path(tmpdir, "index.html").read_text(encoding="utf-8")
                sec_blocks = dict(
                    re.findall(
                        r'<section class="homepage-section [^"]+" data-section-id="([^"]+)"[^>]*>(.*?)</section>',
                        dedup_html,
                        flags=re.S,
                    )
                )
                # Top took tech-1 & tech-2; with deduplicate=True, tech section gets only tech-3 (via alias "AI")
                self.assertIn("news/tech-1.html", sec_blocks["top"])
                self.assertIn("news/tech-2.html", sec_blocks["top"])
                self.assertNotIn("news/tech-1.html", sec_blocks["tech"])
                self.assertIn("news/tech-3.html", sec_blocks["tech"])
                self.assertIn('href="technology.html" class="section-view-all"', sec_blocks["tech"])
                self.assertNotIn("section-view-all", sec_blocks["top"])
                self.assertIn("news/pol-1.html", sec_blocks["pol"])
                self.assertIn("news/pol-2.html", sec_blocks["pol"])
                self.assertIn('href="politics.html" class="section-view-all"', sec_blocks["pol"])

                # With deduplicate=False, tech section gets all 3 technology + alias articles
                config["homepage"]["deduplicate_across_sections"] = False
                news_bot.build_website_html(articles, config=config, skip_image_check=True)
                nodedup_html = Path(tmpdir, "index.html").read_text(encoding="utf-8")
                sec_blocks2 = dict(
                    re.findall(
                        r'<section class="homepage-section [^"]+" data-section-id="([^"]+)"[^>]*>(.*?)</section>',
                        nodedup_html,
                        flags=re.S,
                    )
                )
                self.assertEqual(len(re.findall(r'href="news/tech-\d+\.html"', sec_blocks2["tech"])), 3)
            finally:
                os.chdir(old_cwd)

    def test_all_four_ready_made_presets_and_empty_sections_fallback(self):
        cats = ["देश-विदेश", "टेक्नोलॉजी", "अर्थव्यवस्था", "राजनीति", "मनोरंजन"]
        articles = [
            _make_article(f"p-{i:02d}", title=f"प्रीसेट लेख {i}", category=cats[i % len(cats)])
            for i in range(25)
        ]

        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                for preset_name, preset_cfg in news_bot.HOMEPAGE_PRESETS.items():
                    config = copy.deepcopy(news_bot.DEFAULT_SITE_CONFIG)
                    config["custom_pages"] = []
                    config["homepage"] = copy.deepcopy(preset_cfg)
                    news_bot.build_website_html(articles, config=config, skip_image_check=True)
                    out = Path(tmpdir, "index.html").read_text(encoding="utf-8")
                    self.assertIn('class="homepage-section', out, msg=f"Preset {preset_name} failed to render sections")
                    self.assertEqual(len(re.findall(r"<h1\b", out)), 1, msg=f"Preset {preset_name} must render exactly one <h1>")
                    self.assertIn('<link rel="preload" as="image"', out, msg=f"Preset {preset_name} missing LCP preload")

                # Safety net: if all configured sections are disabled, fallback to default classic layout
                config = copy.deepcopy(news_bot.DEFAULT_SITE_CONFIG)
                config["custom_pages"] = []
                config["homepage"] = {
                    "preset": "custom",
                    "deduplicate_across_sections": True,
                    "sections": [
                        {"id": "hidden", "title": "बंद", "category": "all", "layout": "grid", "max_articles": 5, "enabled": False}
                    ],
                }
                news_bot.build_website_html(articles, config=config, skip_image_check=True)
                fallback_html = Path(tmpdir, "index.html").read_text(encoding="utf-8")
                self.assertIn('data-section-id="top-stories"', fallback_html)
                self.assertIn('class="bento-grid"', fallback_html)
            finally:
                os.chdir(old_cwd)

    def test_reader_theme_toggle_desktop_header_mobile_drawer_and_all_pages(self):
        articles = [_make_article("theme-story", title="थीम परीक्षण खबर", category="देश-विदेश")]
        config = copy.deepcopy(news_bot.DEFAULT_SITE_CONFIG)
        config["header"]["theme"] = "light"
        config["header"]["theme_toggle_enabled"] = True
        config["header"]["default_color_mode"] = "system"

        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                Path(tmpdir, "pages").mkdir(parents=True, exist_ok=True)
                Path(tmpdir, "pages", "about.html").write_text("<article class='article-box'><h1>About</h1></article>", encoding="utf-8")
                config["custom_pages"] = [config["custom_pages"][0]]
                news_bot.build_website_html(articles, config=config, skip_image_check=True)

                for rel_path in ["index.html", "desh-videsh.html", "about.html", "404.html", "news/theme-story.html"]:
                    page_html = Path(tmpdir, rel_path).read_text(encoding="utf-8")
                    # Anti-FOUC head script initializes data-theme before paint
                    self.assertIn("localStorage.getItem('aabtak-theme')", page_html, msg=rel_path)
                    self.assertIn("data-default-theme", page_html, msg=rel_path)
                    self.assertIn("var d='system';var e=true;", page_html, msg=rel_path)
                    # Desktop toggle lives inside .header-desktop-group; mobile toggle inside #site-mobile-panel
                    desktop_group = re.search(r'<div class="header-desktop-group">(.*?)</div>\s*(?:<!--[^>]*-->\s*)?<div class="mobile-nav-controls">', page_html, flags=re.S)
                    self.assertIsNotNone(desktop_group, msg=rel_path)
                    self.assertIn("desktop-theme-toggle", desktop_group.group(1), msg=rel_path)
                    self.assertNotIn("mobile-theme-toggle", desktop_group.group(1), msg=rel_path)
                    mobile_panel = re.search(r'<div class="mobile-nav-panel" id="site-mobile-panel">(.*?)</div>\s*</div>\s*<script>', page_html, flags=re.S)
                    self.assertIsNotNone(mobile_panel, msg=rel_path)
                    self.assertIn("mobile-theme-toggle", mobile_panel.group(1), msg=rel_path)
                    self.assertNotIn("desktop-theme-toggle", mobile_panel.group(1), msg=rel_path)
                    # Adaptive logo swapping enabled on light-surface header/drawer
                    self.assertIn('data-adaptive-logo="true"', page_html, msg=rel_path)

                # When theme_toggle_enabled is False, toggle buttons are omitted from header & mobile drawer
                config["header"]["theme_toggle_enabled"] = False
                config["header"]["default_color_mode"] = "dark"
                header_disabled_toggle = news_bot.get_header_html(config)
                self.assertNotIn("desktop-theme-toggle", header_disabled_toggle)
                self.assertNotIn("mobile-theme-toggle", header_disabled_toggle)
                head_html = news_bot.get_head_links(config=config)
                self.assertIn("var d='dark';var e=false;", head_html)
                self.assertIn("'data-theme-toggle-enabled','false'", head_html)
            finally:
                os.chdir(old_cwd)

    def test_hindi_brand_notranslate_titles_and_uppercase_nav_links(self):
        art = _make_article("brand-check", title="अबतक24.in की विशेष रिपोर्ट", category="देश-विदेश")
        art["image_courtesy"] = "फोटो साभार: अबतक24 न्यूज़ डेस्क"
        art["content_html"] = "<p>अबतक24.in पर पढ़ें विशेष समाचार।</p>"

        config = copy.deepcopy(news_bot.DEFAULT_SITE_CONFIG)
        config["footer"]["copyright_text"] = "© {year} अबतक24.in • सर्वाधिकार सुरक्षित।"

        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                Path(tmpdir, "pages").mkdir(parents=True, exist_ok=True)
                Path(tmpdir, "pages", "about.html").write_text(
                    '<article class="article-box"><h1>अबतक24.in परिचय</h1><p><strong>अबतक24.in</strong> पोर्टल।</p></article>',
                    encoding="utf-8",
                )
                config["custom_pages"] = [
                    {
                        "id": "about",
                        "title": "हमारे बारे में | अबतक24.in",
                        "meta_description": "परिचय",
                        "source_fragment": "pages/about.html",
                        "output_file": "about.html",
                        "include_in_sitemap": True,
                        "changefreq": "monthly",
                    }
                ]
                news_bot.build_website_html([art], config=config, skip_image_check=True)

                for rel_path in ["index.html", "desh-videsh.html", "about.html", "404.html", "news/brand-check.html"]:
                    page_html = Path(tmpdir, rel_path).read_text(encoding="utf-8")
                    # 1. No <title> should contain Hindi अबतक24 (would translate to "So far 24")
                    title_m = re.search(r"<title>(.*?)</title>", page_html, flags=re.S)
                    self.assertIsNotNone(title_m, msg=rel_path)
                    self.assertNotIn("अबतक", title_m.group(1), msg=f"Hindi brand leaked into <title> of {rel_path}")
                    self.assertIn("Aabtak24.in", title_m.group(1), msg=rel_path)

                    # 2. Every visible body occurrence of अबतक24(.in) must be inside translate="no"
                    from html.parser import HTMLParser
                    body_m = re.search(r"<body[^>]*>(.*)</body>", page_html, flags=re.S)
                    self.assertIsNotNone(body_m, msg=rel_path)

                    class _NotranslateAuditor(HTMLParser):
                        def __init__(self):
                            super().__init__()
                            self.stack = []
                            self.unprotected = []

                        def handle_starttag(self, tag, attrs):
                            t = tag.lower()
                            if t in news_bot._VOID_HTML_TAGS:
                                return
                            attr_dict = dict(attrs)
                            is_no_tr = str(attr_dict.get("translate", "")).lower() == "no"
                            self.stack.append((t, is_no_tr, t in ("script", "style")))

                        def handle_endtag(self, tag):
                            t = tag.lower()
                            for idx in range(len(self.stack) - 1, -1, -1):
                                if self.stack[idx][0] == t:
                                    del self.stack[idx:]
                                    break

                        def handle_data(self, data):
                            if "अबतक" in data:
                                if any(item[2] for item in self.stack):
                                    return
                                if not any(item[1] for item in self.stack):
                                    self.unprotected.append(data)

                    auditor = _NotranslateAuditor()
                    auditor.feed(body_m.group(1))
                    self.assertEqual(
                        auditor.unprotected,
                        [],
                        msg=f"Unprotected Hindi brand mentions in {rel_path}: {auditor.unprotected}",
                    )
            finally:
                os.chdir(old_cwd)

        # 3. Verify style.css forces uppercase on header & footer navigation links
        css_text = (Path(__file__).resolve().parent.parent / "style.css").read_text(encoding="utf-8")
        for selector in [r"^\.nav-pills a\s*\{", r"^\.header-action-btn\s*\{", r"^\.mobile-nav-links a\s*\{", r"^\.footer-links a\s*\{", r"^\.footer-pill\s*\{"]:
            m = re.search(selector + r"([^}]+)\}", css_text, flags=re.M)
            self.assertIsNotNone(m, msg=f"Missing selector {selector}")
            self.assertIn("text-transform: uppercase", m.group(1), msg=f"Selector {selector} missing text-transform: uppercase")

    def test_top_bar_soft_descent_on_scroll_to_top_without_affecting_sticky_header(self):
        config = copy.deepcopy(news_bot.DEFAULT_SITE_CONFIG)
        header_html = news_bot.get_header_html(config)

        # Inline script wires passive scroll-to-top descent exclusively for .top-bar
        self.assertIn("document.querySelector('.top-bar')", header_html)
        self.assertIn("top-bar-primed", header_html)
        self.assertIn("top-bar-descending", header_html)
        self.assertIn("updateTopBarDescent", header_html)
        self.assertIn("header-sticky", header_html)
        # Sticky header is never hidden or transformed by the top-bar descent script
        self.assertNotIn("header.classList.add('top-bar", header_html)

        css_text = (Path(__file__).resolve().parent.parent / "style.css").read_text(encoding="utf-8")
        primed_m = re.search(r"^\.top-bar\.top-bar-primed\s*\{([^}]+)\}", css_text, flags=re.M)
        self.assertIsNotNone(primed_m)
        self.assertIn("translateY(-100%)", primed_m.group(1))
        self.assertIn("opacity: 0", primed_m.group(1))

        desc_m = re.search(r"^\.top-bar\.top-bar-descending\s*\{([^}]+)\}", css_text, flags=re.M)
        self.assertIsNotNone(desc_m)
        self.assertIn("translateY(0)", desc_m.group(1))
        self.assertIn("opacity: 1", desc_m.group(1))
        self.assertIn("transition:", desc_m.group(1))

        # Sticky header rules remain intact
        sticky_m = re.search(r"^\.site-header\.header-sticky\s*\{([^}]+)\}", css_text, flags=re.M)
        self.assertIsNotNone(sticky_m)
        self.assertIn("position: sticky", sticky_m.group(1))
        self.assertIn("top: 0", sticky_m.group(1))
        self.assertIn("prefers-reduced-motion: reduce", css_text)

    def test_multi_editor_vault_and_internal_author_metadata_preserved_without_public_leak(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                cfg_payload = copy.deepcopy(news_bot.DEFAULT_SITE_CONFIG)
                cfg_payload["admin_lock"] = {
                    "v": 2,
                    "encrypted_token": "ZW5jcnlwdGVkLWFkbWluLWxvY2s=",
                    "updated_at": "2026-11-19T10:00:00.000Z",
                }
                cfg_payload["editors_vault"] = [
                    {
                        "id": "ed-1",
                        "user_hash": "a" * 64,
                        "permission": "own_only",
                        "active": True,
                        "encrypted_lock": "ZW5jcnlwdGVkLWVkaXRvci1sb2Nr",
                        "token_envelope": "dG9rZW4tZW52ZWxvcGU=",
                        "admin_meta": "YWRtaW4tbWV0YQ==",
                    }
                ]
                with open(news_bot.CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(cfg_payload, f, ensure_ascii=False, indent=2)

                loaded = news_bot.load_site_config()
                self.assertEqual(loaded["admin_lock"], cfg_payload["admin_lock"])
                self.assertEqual(loaded["editors_vault"], cfg_payload["editors_vault"])

                sub_editor_art = _make_article(
                    "sub-editor-exclusive-report",
                    title="संपादक द्वारा लिखित विशेष समाचार रिपोर्ट",
                    source_name="अबतक24 न्यूज़ डेस्क",
                )
                sub_editor_art["author_username"] = "reporter.one@example.com"
                sub_editor_art["author_name"] = "राहुल शर्मा"
                sub_editor_art["updated_by"] = "reporter.one@example.com"

                self.assertTrue(news_bot._is_manual_article(sub_editor_art))
                self.assertTrue(
                    news_bot._is_manual_article(
                        {
                            "title": "Author tagged post",
                            "source_name": "External Wire",
                            "original_link": "https://external.example.com/1",
                            "author_username": "editor@example.com",
                        }
                    )
                )

                articles = [sub_editor_art]
                news_bot.build_website_html(articles, config=loaded, skip_image_check=True)

                # Internal author metadata remains intact in the article object
                self.assertEqual(articles[0]["author_username"], "reporter.one@example.com")
                self.assertEqual(articles[0]["author_name"], "राहुल शर्मा")
                self.assertEqual(articles[0]["updated_by"], "reporter.one@example.com")

                # Public article HTML keeps standard Aabtak24 byline and never leaks private email or vault fields
                article_html = Path("news/sub-editor-exclusive-report.html").read_text(encoding="utf-8")
                index_html = Path("index.html").read_text(encoding="utf-8")
                self.assertIn("न्यूज़ डेस्क", article_html)
                self.assertIn("Aabtak24 News Desk", article_html)
                for public_html in (article_html, index_html):
                    self.assertNotIn("reporter.one@example.com", public_html)
                    self.assertNotIn("ZW5jcnlwdGVkLWFkbWluLWxvY2s=", public_html)
                    self.assertNotIn("editors_vault", public_html)
                    self.assertNotIn("admin_lock", public_html)
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()



