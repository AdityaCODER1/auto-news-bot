import copy
import html as html_lib
import html.parser
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
):
    return {
        "title": title,
        "category": category,
        "slug": slug,
        "content_html": "<p>भारत में आधुनिक तकनीक और डिजिटल नवाचार ने तेज़ी से विकास किया है। यह कदम नागरिकों के लिए उपयोगी है।</p>",
        "image_url": image_url,
        "image_courtesy": "फोटो साभार: परीक्षण डेस्क",
        "original_link": f"https://example.com/{slug}",
        "source_name": "TestSource",
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


if __name__ == "__main__":
    unittest.main()
