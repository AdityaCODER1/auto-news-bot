import re
import time
import json
from datetime import datetime, timezone
import streamlit as st
from streamlit_quill import st_quill
from github import Github

st.set_page_config(page_title="अबतक24 संपादक पोर्टल | Editor CMS", page_icon="📰", layout="centered")

# --- 1. PASSWORD PROTECTION ---
def check_password():
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
    if st.session_state.authenticated:
        return True

    st.markdown("<h2 style='text-align:center;'>🔒 अबतक24.in — संपादक लॉगिन (Editor Login)</h2>", unsafe_allow_html=True)
    pwd = st.text_input("पासवर्ड दर्ज करें (Enter Password):", type="password")
    if st.button("लॉगिन करें (Login)", use_container_width=True):
        if pwd == st.secrets["ADMIN_PASSWORD"]:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("❌ गलत पासवर्ड! कृपया पुनः प्रयास करें।")
    return False

if not check_password():
    st.stop()

# --- 2. HELPER FOR CLEAN SEO SLUG ---
def make_slug(text):
    slug = re.sub(r'[^a-zA-Z0-9\-]', '', text.lower().replace(' ', '-'))
    slug = re.sub(r'-+', '-', slug).strip('-')
    return slug or f"special-report-{int(time.time())}"

# --- 3. EDITOR INTERFACE ---
st.title("📝 नया समाचार लिखें (Create New Article)")
st.caption("यहाँ से लिखा गया लेख सीधे **aabtak24.in** पर प्रकाशित होगा।")

title = st.text_input("1. समाचार का शीर्षक (Hindi Headline) *", placeholder="उदाहरण: भारत में नई टेक्नोलॉजी नीति की घोषणा...")

col1, col2 = st.columns(2)
with col1:
    category = st.selectbox(
        "2. श्रेणी चुनें (Category) *",
        ["देश-विदेश", "टेक्नोलॉजी", "अर्थव्यवस्था", "राजनीति", "मनोरंजन"]
    )
with col2:
    seo_slug_input = st.text_input(
        "3. English URL Link (Optional)",
        placeholder="e.g. india-tech-policy-2026 (खाली छोड़ने पर अपने आप बनेगा)"
    )

st.markdown("---")
st.subheader("🖼️ 4. फ़ोटो और साभार (Picture & Courtesy)")
img_mode = st.radio("फ़ोटो कैसे जोड़ना चाहते हैं?", ["फ़ोन/कंप्यूटर से फ़ोटो अपलोड करें (Upload File)", "फ़ोटो का डायरेक्ट लिंक डालें (Image URL)"], horizontal=True)

uploaded_file = None
image_url_input = ""

if "Upload" in img_mode:
    uploaded_file = st.file_uploader("फ़ोटो चुनें (JPG, PNG, WEBP)", type=["jpg", "jpeg", "png", "webp"])
else:
    image_url_input = st.text_input("Image URL यहाँ पेस्ट करें:", placeholder="https://example.com/photo.jpg")

image_courtesy = st.text_input("फ़ोटो साभार (Image Courtesy) *", value="फोटो साभार: अबतक24 न्यूज़ डेस्क")

st.markdown("---")
st.subheader("✍️ 5. मुख्य समाचार (Article Content)")
st.info("💡 **सब-हेडिंग (Sub-heading) कैसे बनाएं:** किसी भी लाइन को माउस या उंगली से सिलेक्ट (Highlight) करें और टूलबार में **[ H3 ]** (या Header 3) बटन दबाएं। वह अपने आप वेबसाइट के नारंगी बॉर्डर वाले सब-हेडिंग में बदल जाएगा!")

# Rich Text Editor with H3 Subheading & Bold/Italic/List buttons
toolbar = [
    [{"header": [3, False]}],  # Gives dropdown/button for H3 Subheading vs Normal Text
    ["bold", "italic", "underline", "blockquote"],
    [{"list": "ordered"}, {"list": "bullet"}],
    ["link", "clean"]
]

content_html = st_quill(
    placeholder="यहाँ अपना समाचार लिखें या पेस्ट करें...",
    html=True,
    toolbar=toolbar,
    key="quill_editor"
)

st.markdown("---")

if st.button("🚀 वेबसाइट पर प्रकाशित करें (Publish Article Now)", type="primary", use_container_width=True):
    if not title.strip() or not content_html or len(content_html.strip()) < 20:
        st.warning("⚠️ कृपया शीर्षक (Title) और समाचार की सामग्री (Content) दोनों भरें!")
        st.stop()

    with st.spinner("आपका लेख aabtak24.in पर अपलोड और प्रकाशित हो रहा है... कृपया 10 सेकंड प्रतीक्षा करें..."):
        try:
            g = Github(st.secrets["GITHUB_TOKEN"])
            repo = g.get_repo(st.secrets["GITHUB_REPO"]) # e.g. "your-username/auto-news-bot"
            slug = make_slug(seo_slug_input) if seo_slug_input.strip() else f"report-{int(time.time())}"

            # 1. Handle Picture Upload or URL
            final_image_url = "https://images.unsplash.com/photo-1504711434969-e33886168f5c?auto=format&fit=crop&w=1200&q=80"
            if uploaded_file is not None:
                ext = uploaded_file.name.split(".")[-1].lower()
                img_path = f"uploads/{slug}-{int(time.time())}.{ext}"
                repo.create_file(
                    path=img_path,
                    message=f"Upload image for {slug}",
                    content=uploaded_file.getvalue(),
                    branch="main"
                )
                final_image_url = f"https://aabtak24.in/{img_path}"
            elif image_url_input.strip():
                final_image_url = image_url_input.strip()

            # 2. Convert any accidental H1/H2 from Quill into H3 so it matches your RSS subheading CSS
            cleaned_html = re.sub(r'<h[12]>', '<h3>', content_html)
            cleaned_html = re.sub(r'</h[12]>', '</h3>', cleaned_html)

            # 3. Fetch articles_db.json and insert new article at #1 spot
            db_file = repo.get_contents("articles_db.json", ref="main")
            articles = json.loads(db_file.decoded_content.decode("utf-8"))

            new_article = {
                "title": title.strip(),
                "category": category,
                "slug": slug,
                "content_html": cleaned_html,
                "image_url": final_image_url,
                "image_courtesy": image_courtesy.strip(),
                "original_link": f"https://aabtak24.in/news/{slug}.html",
                "source_name": "अबतक24 विशेष संवाददाता",
                "date": datetime.now().strftime("%d %b %Y, %I:%M %p"),
                "iso_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
            }

            articles.insert(0, new_article)
            updated_json = json.dumps(articles[:100], ensure_ascii=False, indent=2)

            repo.update_file(
                path="articles_db.json",
                message=f"Editor published: {title.strip()}",
                content=updated_json,
                sha=db_file.sha,
                branch="main"
            )

            # 4. Trigger GitHub Action to rebuild HTML & Sitemap immediately
            workflow = repo.get_workflow("schedule.yml")
            workflow.create_dispatch("main")

            st.success("🎉 बधाई हो! आपका लेख सफलतापूर्वक प्रकाशित हो गया है! 40 सेकंड में यह वेबसाइट पर लाइव दिखेगा।")
            st.markdown(f"🔗 **लेख का लिंक:** [https://aabtak24.in/news/{slug}.html](https://aabtak24.in/news/{slug}.html)")
        except Exception as e:
            st.error(f"त्रुटि (Error): {e}")
          
