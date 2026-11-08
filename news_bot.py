import os
import json
import time
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

# 3. Model Fallback List
MODELS_TO_TRY = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
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
    """
    Fetches a royalty-free image URL & creator credit using Openverse (WordPress's 
    open-source Creative Commons search engine) - NO API KEY REQUIRED!
    """
    try:
        url = "https://api.openverse.org/v1/images/"
        params = {
            "q": query,
            "page_size": 5,
            "aspect_ratio": "wide",
            "license_type": "commercial"
        }
        headers = {"User-Agent": "AutoHindiNewsBot/1.0"}
        res = requests.get(url, params=params, headers=headers, timeout=10)
        if res.status_code == 200:
            results = res.json().get("results", [])
            for img in results:
                img_url = img.get("url")
                creator = img.get("creator") or "Creative Commons"
                source = img.get("source") or "Openverse"
                if img_url and img_url.startswith("http"):
                    courtesy = f"फोटो साभार: {creator} / {source.capitalize()} (प्रतीकात्मक तस्वीर)"
                    return img_url, courtesy
    except Exception as e:
        print(f"Openverse stock fetch error: {e}")
    return None, None

def get_rss_image(entry):
    """Extracts the original news thumbnail from the RSS entry if available."""
    if "media_thumbnail" in entry and len(entry.media_thumbnail) > 0:
        return entry.media_thumbnail[0]["url"].replace("/240/", "/976/")
    if "media_content" in entry and len(entry.media_content) > 0:
        return entry.media_content[0]["url"]
    return None

def fetch_and_rewrite():
    print(f"Fetching latest English news from {SOURCE_NAME}...")
    feed = feedparser.parse(RSS_URL)
    
    if not feed.entries:
        print("No news entries found!")
        return

    latest_story = feed.entries[0]
    original_title = latest_story.title
    original_summary = BeautifulSoup(latest_story.summary, "html.parser").get_text()
    original_link = latest_story.link
    rss_image_url = get_rss_image(latest_story)

    # 4. Ask Gemini AI to write the Hindi article AND decide the Image Strategy
    prompt = f"""
    आप एक वरिष्ठ हिंदी समाचार संपादक (Senior Hindi News Editor) हैं।
    नीचे दिए गए अंग्रेजी समाचार को पढ़कर एक मौलिक, आकर्षक और प्रभावशाली हिंदी समाचार लेख (लगभग 300-400 शब्द) लिखें।

    दिशानिर्देश (Strict Rules):
    1. मशीनी अनुवाद (literal translation) न करें। खबर को सहज, प्रवाहपूर्ण हिंदी पत्रकारिता की भाषा में दोबारा लिखें।
    2. सभी तथ्य, नाम और आंकड़े 100% सटीक रखें।
    3. एक आकर्षक हिंदी शीर्षक (Headline) और 4-5 शब्दों का अंग्रेजी SEO URL slug बनाएं।
    4. फोटो का निर्णय (Image Decision - बहुत महत्वपूर्ण):
       - यदि खबर किसी विशिष्ट राजनेता (Politician), सेलिब्रिटी, खिलाड़ी, विशेष दुर्घटना या ऐसी घटना के बारे में है जहाँ असली व्यक्ति/घटना की फोटो जरूरी है, तो "image_strategy" को "original" रखें।
       - यदि खबर सामान्य विषयों जैसे टेक्नोलॉजी (AI, स्मार्टफोन, सर्वर), शेयर बाजार, अर्थव्यवस्था, मौसम, स्वास्थ्य, विज्ञान या नीतिगत विषयों पर है जहाँ एक सामान्य प्रतीकात्मक फोटो (generic stock photo) अच्छी लगेगी, तो "image_strategy" को "stock" रखें और "stock_search_query" में फोटो सर्च के लिए 2 अंग्रेजी शब्द लिखें (जैसे: "server room", "stock market", "solar panels")।
    5. केवल वैध JSON में उत्तर दें जिसमें ये 5 keys हों:
       - "title": हिंदी शीर्षक
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

    print(f"🧠 AI Photo Decision: Strategy = '{strategy.upper()}' | Search Query = '{search_query}'")

    # 5. Execute the AI's Image Decision
    final_image_url = None
    image_courtesy = ""

    if strategy == "stock":
        final_image_url, image_courtesy = fetch_free_stock_image(search_query)

    # If AI chose 'original' OR if stock search didn't return an image, use the RSS photo
    if not final_image_url and rss_image_url:
        final_image_url = rss_image_url
        image_courtesy = f"फोटो साभार: {SOURCE_NAME} (RSS फ़ीड के माध्यम से)"

    # Ultimate fallback
    if not final_image_url:
        final_image_url = "https://images.unsplash.com/photo-1504711434969-e33886168f5c?auto=format&fit=crop&w=1200&q=80"
        image_courtesy = "फोटो साभार: Unsplash (प्रतीकात्मक तस्वीर)"

    # 6. Assemble Final HTML
    final_html = f"""
    <figure>
        <img src="{final_image_url}" alt="{article_data['title']}" />
        <figcaption class="image-credit"><em>{image_courtesy}</em></figcaption>
    </figure>
    {article_data['content_html']}
    <hr>
    <p><em>स्रोत और साभार: यह रिपोर्ट <a href="{original_link}" target="_blank" rel="nofollow">{SOURCE_NAME}</a> की मूल खबर के तथ्यों पर आधारित है।</em></p>
    """

    print("\n================ SUCCESS! GENERATED HINDI ARTICLE ================")
    print("HINDI HEADLINE:", article_data["title"])
    print("AI IMAGE STRATEGY:", strategy)
    print("CHOSEN IMAGE URL:", final_image_url)
    print("COURTESY CAPTION:", image_courtesy)
    print("\nFINAL HTML FOR WORDPRESS:\n", final_html)
    print("==================================================================")

if __name__ == "__main__":
    fetch_and_rewrite()
