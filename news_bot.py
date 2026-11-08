import os
import json
import feedparser
from bs4 import BeautifulSoup
from google import genai

# 1. Connect to Google Gemini using your Secret Key
api_key = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=api_key)

# 2. Pick a free RSS News Feed (Example: BBC World or Google News India)
RSS_URL = "https://feeds.bbci.co.uk/news/world/asia/india/rss.xml"
SOURCE_NAME = "BBC News"

def fetch_and_rewrite():
    print(f"Fetching latest news from {SOURCE_NAME}...")
    feed = feedparser.parse(RSS_URL)
    
    if not feed.entries:
        print("No news entries found!")
        return

    # Grab the #1 latest news story
    latest_story = feed.entries[0]
    original_title = latest_story.title
    original_summary = BeautifulSoup(latest_story.summary, "html.parser").get_text()
    original_link = latest_story.link

    # Try to grab image URL from the RSS feed if available
    image_url = "https://images.unsplash.com/photo-1504711434969-e33886168f5c?auto=format&fit=crop&w=1200&q=80"
    image_courtesy = "Photo Courtesy: Unsplash (Royalty-Free News Archive)"
    
    if "media_thumbnail" in latest_story and len(latest_story.media_thumbnail) > 0:
        image_url = latest_story.media_thumbnail[0]["url"]
        image_courtesy = f"Image Courtesy: {SOURCE_NAME} (via RSS)"

    # 3. Ask Gemini AI to paraphrase into an original news article
    prompt = f"""
    You are a professional news editor. Rewrite the following news report into an original, 
    engaging 350-word news article. 
    - Keep all facts 100% accurate.
    - Write a fresh, catchy headline.
    - Format the body using clean HTML paragraphs (<p>, <h3>).
    - Return ONLY valid JSON with two keys: "title" and "content_html".

    Original Title: {original_title}
    Original Details: {original_summary}
    """

    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
        config={"response_mime_type": "application/json"}
    )

    article_data = json.loads(response.text)

    # 4. Add Image Courtesy & Source Attribution at the bottom
    final_html = f"""
    <figure>
        <img src="{image_url}" alt="{article_data['title']}" />
        <figcaption class="image-credit"><em>{image_courtesy}</em></figcaption>
    </figure>
    {article_data['content_html']}
    <hr>
    <p><em>Reporting adapted with attribution from <a href="{original_link}" target="_blank" rel="nofollow">{SOURCE_NAME}</a>.</em></p>
    """

    print("\n================ SUCCESS! GENERATED ARTICLE ================")
    print("NEW HEADLINE:", article_data["title"])
    print("\nFINAL HTML FOR WORDPRESS:\n", final_html)
    print("============================================================")

if __name__ == "__main__":
    fetch_and_rewrite()
