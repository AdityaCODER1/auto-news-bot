import os
import json
import time
import feedparser
from bs4 import BeautifulSoup
from google import genai
from google.genai import errors

# 1. Connect to Google Gemini using your Secret Key
api_key = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=api_key)

# 2. Pick an English News RSS Feed (It will automatically rewrite into Hindi!)
RSS_URL = "https://feeds.bbci.co.uk/news/world/asia/india/rss.xml"
SOURCE_NAME = "BBC News"

# 3. Model Fallback List (Tries 3.8 Flash first, falls back to 3.6 Flash if busy)
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
            print(f"⚠️ {model_name} busy or unavailable ({e}). Switching to backup model...")
            time.sleep(2)
    raise RuntimeError("All models failed. Please try again later.")

def fetch_and_rewrite():
    print(f"Fetching latest English news from {SOURCE_NAME}...")
    feed = feedparser.parse(RSS_URL)
    
    if not feed.entries:
        print("No news entries found!")
        return

    # Grab the #1 latest news story
    latest_story = feed.entries[0]
    original_title = latest_story.title
    original_summary = BeautifulSoup(latest_story.summary, "html.parser").get_text()
    original_link = latest_story.link

    # Grab image URL from RSS feed or use fallback
    image_url = "https://images.unsplash.com/photo-1504711434969-e33886168f5c?auto=format&fit=crop&w=1200&q=80"
    image_courtesy = "फोटो साभार: Unsplash (प्रतीकात्मक तस्वीर)"
    
    if "media_thumbnail" in latest_story and len(latest_story.media_thumbnail) > 0:
        image_url = latest_story.media_thumbnail[0]["url"]
        image_courtesy = f"फोटो साभार: {SOURCE_NAME} (RSS फ़ीड के माध्यम से)"

    # 4. Ask Gemini AI to translate + paraphrase into high-quality journalistic Hindi
    prompt = f"""
    आप एक वरिष्ठ हिंदी समाचार संपादक (Senior Hindi News Editor - BBC Hindi / Dainik Jagran style) हैं।
    नीचे दिए गए अंग्रेजी समाचार को पढ़कर एक मौलिक, आकर्षक और प्रभावशाली हिंदी समाचार लेख (लगभग 300-400 शब्द) लिखें।

    दिशानिर्देश (Strict Rules):
    1. मशीनी अनुवाद (literal translation) बिल्कुल न करें। खबर को पूरी तरह समझकर सहज, प्रवाहपूर्ण और पेशेवर हिंदी पत्रकारिता की भाषा में दोबारा लिखें (Paraphrase + Write in natural Hindi)।
    2. सभी तथ्य, नाम और आंकड़े 100% सटीक रखें।
    3. एक आकर्षक और क्लिक-योग्य हिंदी शीर्षक (Headline) बनाएं।
    4. लेख के मुख्य भाग को साफ HTML टैग्स (<p>, <h3>, <ul>, <li>) में फॉर्मेट करें।
    5. WordPress URL के लिए 4-5 शब्दों का एक छोटा अंग्रेजी SEO Slug भी बनाएं (जैसे: "india-new-tech-policy-2026")।
    6. केवल वैध JSON (Valid JSON) में उत्तर दें जिसमें ये तीन keys हों:
       - "title": हिंदी शीर्षक
       - "slug": अंग्रेजी SEO URL slug
       - "content_html": हिंदी लेख का HTML कोड

    Original English Title: {original_title}
    Original English Details: {original_summary}
    """

    article_data = generate_with_fallback(prompt)

    # 5. Add Hindi Image Courtesy & Source Attribution at the bottom
    final_html = f"""
    <figure>
        <img src="{image_url}" alt="{article_data['title']}" />
        <figcaption class="image-credit"><em>{image_courtesy}</em></figcaption>
    </figure>
    {article_data['content_html']}
    <hr>
    <p><em>स्रोत और साभार: यह रिपोर्ट <a href="{original_link}" target="_blank" rel="nofollow">{SOURCE_NAME}</a> की मूल खबर के तथ्यों पर आधारित है।</em></p>
    """

    print("\n================ SUCCESS! GENERATED HINDI ARTICLE ================")
    print("HINDI HEADLINE:", article_data["title"])
    print("SEO URL SLUG:", article_data["slug"])
    print("\nFINAL HTML FOR WORDPRESS:\n", final_html)
    print("==================================================================")

if __name__ == "__main__":
    fetch_and_rewrite()
