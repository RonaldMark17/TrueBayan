from flask import Flask, render_template, request, jsonify, redirect, url_for, session, flash
from newsapi import NewsApiClient
from deep_translator import GoogleTranslator
from pymongo import MongoClient
from bson.objectid import ObjectId
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import os
from bs4 import BeautifulSoup
import requests
from urllib.parse import urlparse
from textblob import TextBlob  # AI Library
from gtts import gTTS
from io import BytesIO
from flask import send_file

app = Flask(__name__)
app.secret_key = "secret123"

# ---------- CONFIG ----------
newsapi = NewsApiClient(api_key="bd5ad62242af4fd190e21d01df52b53a")

# ---------- MONGODB CONNECTION ----------
# Global client to handle connection pooling
client = MongoClient("mongodb://localhost:27017/")
db = client["truebayan"]

def get_db():
    return db

# ---------- FUNCTIONS ----------

# NEW FUNCTION: Attach database counts to NewsAPI articles
def attach_social_data(articles, current_user_id=None):
    """
    Takes a list of NewsAPI articles and adds 'likes', 'saves', 
    'user_liked', and 'user_saved' properties from the MongoDB.
    """
    if not articles:
        return []

    # Get all URLs from the articles
    urls = [a.get('url') for a in articles if a.get('url')]
    
    if not urls:
        return articles

    db = get_db()

    # 1. Get Global Like Counts
    # SQL: SELECT article_url, COUNT(*) ... GROUP BY article_url
    like_pipeline = [
        {"$match": {"article_url": {"$in": urls}}},
        {"$group": {"_id": "$article_url", "count": {"$sum": 1}}}
    ]
    like_results = list(db.article_likes.aggregate(like_pipeline))
    like_counts = {item['_id']: item['count'] for item in like_results}

    # 2. Get Global Save Counts
    save_pipeline = [
        {"$match": {"url": {"$in": urls}}},
        {"$group": {"_id": "$url", "count": {"$sum": 1}}}
    ]
    save_results = list(db.saved_articles.aggregate(save_pipeline))
    save_counts = {item['_id']: item['count'] for item in save_results}

    # 3. Check User Specific Actions (if logged in)
    user_likes = set()
    user_saves = set()
    
    if current_user_id:
        # Check likes
        liked_docs = db.article_likes.find(
            {"user_id": ObjectId(current_user_id), "article_url": {"$in": urls}},
            {"article_url": 1}
        )
        user_likes = set(doc['article_url'] for doc in liked_docs)
        
        # Check saves
        saved_docs = db.saved_articles.find(
            {"user_id": ObjectId(current_user_id), "url": {"$in": urls}},
            {"url": 1}
        )
        user_saves = set(doc['url'] for doc in saved_docs)

    # Attach data to article objects
    for a in articles:
        url = a.get('url')
        a['likes'] = like_counts.get(url, 0)
        a['saves'] = save_counts.get(url, 0)
        a['user_liked'] = url in user_likes
        a['user_saved'] = url in user_saves

    return articles

def is_philippine_news(article):
    """Check if article is related to Philippines"""
    philippine_keywords = [
        'philippines', 'philippine', 'manila', 'filipino', 'filipina',
        'cebu', 'davao', 'mindanao', 'luzon', 'visayas',
        'duterte', 'marcos', 'quezon', 'makati', 'pasig',
        'senate', 'congress', 'doh', 'dilg', 'pnp',
        'abs-cbn', 'gma', 'pba', 'gilas', 'pacquiao'
    ]
    
    title = article.get('title', '').lower()
    description = article.get('description', '').lower() if article.get('description') else ''
    content = article.get('content', '').lower() if article.get('content') else ''
    
    # Check if any Philippine keyword is in title, description, or content
    text_to_check = f"{title} {description} {content}"
    return any(keyword in text_to_check for keyword in philippine_keywords)

def filter_philippine_news(articles):
    """Filter articles to only include Philippine-related news"""
    return [article for article in articles if is_philippine_news(article)]

def detect_fake_news_advanced(text, url=""):
    """
    Realistic AI Scoring: Separates Risk from Confidence.
    Returns: (label, confidence_percentage, ai_risk_score, reasons)
    """
    if not text or len(text) < 100:
        return "VERIFY", 0, 5.0, ["Content too short for accurate analysis"]
    
    blob = TextBlob(text)
    sentiment = blob.sentiment
    reasons = []
    
    # 1. BASELINE RISK (Start at 50 - Neutral/Unsure)
    risk_score = 50 
    
    # 2. DOMAIN CHECK (The strongest signal)
    trusted_domains = [
        'rappler.com', 'inquirer.net', 'abs-cbn.com', 'gma.network', 'philstar.com', 
        'cnnphilippines.com', 'manilabulletin.com', 'bworldonline.com', 'pna.gov.ph',
        'bbc.com', 'reuters.com', 'apnews.com', 'nytimes.com', 'yugatech.com', 
        'spot.ph', 'spin.ph', 'pep.ph',"gmanetwork.com"
    ]
    
    domain_match = False
    if url:
        domain_match = any(domain in url.lower() for domain in trusted_domains)
    
    if domain_match:
        risk_score -= 40  # Massive trust boost -> Drops to ~10 Risk
        reasons.append("Source is Verified & Trusted")
    else:
        risk_score += 10  # Slight suspicion for unknown sites -> Rises to ~60 Risk
        reasons.append("Source unverified (Proceed with caution)")

    # 3. AI SENTIMENT ANALYSIS (Reality Check)
    if sentiment.subjectivity > 0.6:
        if sentiment.polarity < -0.2:
            risk_score += 20
            reasons.append("High Subjectivity + Negativity Detected")
        else:
            risk_score += 10
            reasons.append("Opinionated Content Detected")
    elif sentiment.subjectivity < 0.3:
        risk_score -= 10
        reasons.append("Objective/Factual Writing Style")

    # Extreme Emotion Check (Too angry or too happy = Clickbait)
    if abs(sentiment.polarity) > 0.8:
        risk_score += 15
        reasons.append("Extreme Emotional Language")

    # 4. KEYWORD & PATTERN ANALYSIS
    high_risk_keywords = [
        "hoax", "conspiracy", "exposed", "secret", "shocking", 
        "censored", "mainstream media", "government lies", "debunked",
        "miracle cure", "bioweapon", "deep state", "wake up", "plot"
    ]
    
    clickbait_keywords = [
        "you won't believe", "shocking truth", "viral", "mind blowing", 
        "this will change everything", "urgent", "breaking news", "omg"
    ]

    text_lower = text.lower()
    
    found_high_risk = [w for w in high_risk_keywords if w in text_lower]
    if found_high_risk:
        risk_score += 25
        reasons.append(f"Suspicious keyword: '{found_high_risk[0]}'")

    found_clickbait = [w for w in clickbait_keywords if w in text_lower]
    if found_clickbait:
        risk_score += 10
        reasons.append(f"Clickbait style: '{found_clickbait[0]}'")

    # 5. REALITY SCORING FORMULA
    # Clamp Risk Score (0 = Safe, 100 = Fake)
    final_risk = min(100, max(0, risk_score))
    
    # AI Score (0-10) for display. 
    # High Score = High Risk of being Fake.
    ai_score = round(final_risk / 10, 1)

    # CONFIDENCE CALCULATION
    confidence = abs(final_risk - 50) * 2
    
    # Boost confidence if we have a lot of text to analyze
    if len(text) > 1000:
        confidence = min(100, confidence + 10)

    # Determine Label based on Risk
    if final_risk >= 80:
        label = "FAKE"
    elif final_risk >= 70:
        label = "SUSPICIOUS"
    elif final_risk >= 50:
        label = "VERIFY"
    else:
        label = "CREDIBLE"

    return label, int(confidence), ai_score, reasons

def detect_fake_news(text, url=""):
    """Wrapper for backward compatibility"""
    label, confidence, ai_score, reasons = detect_fake_news_advanced(text, url)
    return label

def summarize(text):
    if not text:
        return ""
    sentences = text.split(".")
    return ".".join(sentences[:3]) + "." if len(sentences) > 3 else text

def translate_filipino(text):
    if not text:
        return ""
    try:
        return GoogleTranslator(source="auto", target="tl").translate(text)
    except:
        return text

def detect_category(title, description):
    """Detect article category based on keywords"""
    text = f"{title} {description}".lower()
    
    categories = {
        'Politics': ['politics', 'government', 'election', 'senate', 'congress', 'president', 'mayor', 'duterte', 'marcos'],
        'Business': ['business', 'economy', 'stock', 'market', 'company', 'trade', 'investment', 'peso', 'gdp'],
        'Technology': ['technology', 'tech', 'smartphone', 'app', 'software', 'internet', 'digital', 'AI', 'gadget'],
        'Sports': ['sports', 'basketball', 'boxing', 'football', 'pba', 'gilas', 'pacquiao', 'athlete', 'game'],
        'Entertainment': ['entertainment', 'movie', 'celebrity', 'showbiz', 'abs-cbn', 'gma', 'actor', 'actress'],
        'Health': ['health', 'medical', 'hospital', 'doctor', 'covid', 'vaccine', 'disease', 'doh', 'wellness'],
        'Education': ['education', 'school', 'university', 'student', 'teacher', 'deped', 'ched', 'learning'],
        'Environment': ['environment', 'climate', 'weather', 'typhoon', 'flood', 'pollution', 'pagasa', 'nature'],
        'Crime': ['crime', 'police', 'arrest', 'murder', 'theft', 'pnp', 'investigation', 'suspect'],
        'Weather': ['weather', 'typhoon', 'rain', 'storm', 'pagasa', 'forecast', 'temperature'],
        'Lifestyle': ['lifestyle', 'fashion', 'travel', 'culture', 'art', 'tourism', 'food'],
        'Food': ['food', 'restaurant', 'cuisine', 'recipe', 'chef', 'cooking', 'dining']
    }
    
    for category, keywords in categories.items():
        if any(keyword in text for keyword in keywords):
            return category
    
    return 'General'

def translate_text(text, target='tl'):
    try:
        translator = GoogleTranslator(source='auto', target=target)
        result = translator.translate(text)
        return result
    except Exception as e:
        print(f"Translation library error: {e}")
        return text

@app.route('/translate_article', methods=['POST'])
def translate_article():
    try:
        data = request.json
        original_content = data.get('text')
        
        if not original_content:
            return jsonify({'error': 'No text provided'}), 400

        safe_content = original_content[:4500] 
        
        translated_content = translate_text(safe_content, target='tl')
        
        return jsonify({
            'status': 'success',
            'translated_text': translated_content
        })

    except Exception as e:
        print(f"Server Error: {e}")
        return jsonify({'error': str(e)}), 500
    
@app.route('/api/speak', methods=['POST'])
def api_speak():
    try:
        data = request.json
        text = data.get('text', '')
        lang = data.get('lang', 'en')

        if not text:
            return jsonify({'error': 'No text provided'}), 400

        tts = gTTS(text=text, lang=lang)
        
        audio_fp = BytesIO()
        tts.write_to_fp(audio_fp)
        audio_fp.seek(0)
        
        return send_file(audio_fp, mimetype='audio/mp3')
    except Exception as e:
        print(f"TTS Error: {e}")
        return jsonify({'error': str(e)}), 500
    
# ---------- LIKE / UNLIKE SYSTEM ----------
@app.route("/toggle_like", methods=["POST"])
def toggle_like():
    if 'user_id' not in session:
        return jsonify({"status": "error", "message": "Not logged in"}), 401
    
    data = request.get_json()
    url = data.get('url')
    user_id = session['user_id']
    
    if not url:
        return jsonify({"status": "error", "message": "URL is required"}), 400

    db = get_db()
    
    # Check if already liked
    existing_like = db.article_likes.find_one({
        "user_id": ObjectId(user_id),
        "article_url": url
    })
    
    if existing_like:
        db.article_likes.delete_one({"_id": existing_like['_id']})
        action = "unliked"
    else:
        db.article_likes.insert_one({
            "user_id": ObjectId(user_id),
            "article_url": url,
            "created_at": datetime.now()
        })
        action = "liked"
        
    return jsonify({"status": "success", "action": action})

@app.route("/check_likes", methods=["POST"])
def check_likes():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({"liked_urls": []})
    
    urls = request.get_json().get('urls', [])
    if not urls:
        return jsonify({"liked_urls": []})

    db = get_db()
    
    # Find likes matching user and list of URLs
    likes = db.article_likes.find({
        "user_id": ObjectId(user_id),
        "article_url": {"$in": urls}
    })
    
    liked_urls = [like['article_url'] for like in likes]
    
    return jsonify({"liked_urls": liked_urls})

# ---------- SUMMARIZER API ----------
@app.route('/api/summarize', methods=['POST'])
def api_summarize():
    try:
        data = request.json
        text = data.get('text', '')

        if not text or len(text) < 100:
            return jsonify({'summary': "Content is too short to summarize effectively."})

        summary_text = summarize_advanced(text)
        
        return jsonify({
            'status': 'success',
            'summary': summary_text
        })
    except Exception as e:
        print(f"Summarizer Error: {e}")
        return jsonify({'error': str(e)}), 500

def summarize_advanced(text, count=3):
    text = " ".join(text.split())
    import re
    sentences = re.split(r'(?<=[.!?]) +', text)
    
    if len(sentences) <= count:
        return text
    
    summary = " ".join(sentences[:count])
    return summary

# ---------- TOGGLE SAVE/UNSAVE ----------
@app.route("/toggle_save", methods=["POST"])
def toggle_save():
    if 'user_id' not in session:
        return jsonify({"status": "error", "message": "Not logged in"}), 401
    
    title = request.json.get('title')
    url = request.json.get('url')
    user_id = session['user_id']
    
    db = get_db()
    
    existing = db.saved_articles.find_one({
        "user_id": ObjectId(user_id),
        "url": url
    })
    
    if existing:
        db.saved_articles.delete_one({"_id": existing['_id']})
        return jsonify({"status": "success", "action": "unsaved", "message": "Removed from saved"})
    else:
        db.saved_articles.insert_one({
            "user_id": ObjectId(user_id),
            "title": title,
            "url": url,
            "saved_at": datetime.now()
        })
        return jsonify({"status": "success", "action": "saved", "message": "Added to saved"})

@app.route("/check_saved", methods=["POST"])
def check_saved():
    if 'user_id' not in session:
        return jsonify({"saved": False})
    
    url = request.json.get('url')
    
    db = get_db()
    is_saved = db.saved_articles.find_one({
        "user_id": ObjectId(session['user_id']),
        "url": url
    })
    
    return jsonify({"saved": is_saved is not None})

# ---------- USER STATS ENDPOINT (NEW) ----------
@app.route("/get_user_stats")
def get_user_stats():
    if 'user_id' not in session:
        return jsonify({"saved_count": 0, "liked_count": 0})
    
    db = get_db()
    user_id_obj = ObjectId(session['user_id'])
    
    saved_count = db.saved_articles.count_documents({"user_id": user_id_obj})
    liked_count = db.article_likes.count_documents({"user_id": user_id_obj})
    
    return jsonify({"saved_count": saved_count, "liked_count": liked_count})


# ---------- ARTICLE READER ----------
@app.route("/read_article")
def read_article():
    if 'user_id' not in session:
        return redirect(url_for('landing'))
    
    url = request.args.get('url')
    title = request.args.get('title', 'Article')
    
    if not url:
        flash("No article URL provided", "danger")
        return redirect(url_for('dashboard'))
    
    db = get_db()
    cached = db.article_cache.find_one({"url": url})
    
    if cached:
        article = cached
    else:
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            response = requests.get(url, headers=headers, timeout=10)
            soup = BeautifulSoup(response.content, 'html.parser')
            
            paragraphs = soup.find_all('p')
            content = '\n\n'.join([p.get_text().strip() for p in paragraphs if len(p.get_text().strip()) > 50])
            
            img_tag = soup.find('meta', property='og:image')
            image_url = img_tag['content'] if img_tag else None
            
            category = detect_category(title, content[:500])
            
            article_doc = {
                "url": url,
                "title": title,
                "content": content,
                "image_url": image_url,
                "category": category,
                "cached_at": datetime.now()
            }
            
            db.article_cache.insert_one(article_doc)
            
            article = article_doc
            
        except Exception as e:
            flash(f"Could not load article. Opening original link...", "warning")
            return redirect(url)
    
    save_reading_history(session['user_id'], article['title'], url)
    
    return render_template('article_reader.html', article=article, username=session.get('username'))

# ---------- SUBMIT FAKE URL ----------
@app.route("/submit_fake_url", methods=["POST"])
def submit_fake_url():
    if 'user_id' not in session:
        return jsonify({"status": "error", "message": "Not logged in"}), 401
    
    url = request.json.get('url')
    
    if not url:
        return jsonify({"status": "error", "message": "URL is required"}), 400
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(response.content, 'html.parser')
        
        title = soup.find('title').get_text().strip() if soup.find('title') else "Unknown Title"
        
        meta_desc = soup.find('meta', attrs={'name': 'description'}) or \
                    soup.find('meta', attrs={'property': 'og:description'})
        description = meta_desc['content'].strip() if meta_desc else ""
        
        paragraphs = soup.find_all('p')
        article_body = " ".join([p.get_text().strip() for p in paragraphs[:15]])
        
        full_text_to_analyze = f"{title}. {description}. {article_body}"
        
        label, confidence, ai_score, reasons = detect_fake_news_advanced(full_text_to_analyze, url)
        
        db = get_db()
        
        # Insert Report
        db.fake_news_reports.insert_one({
            "user_id": ObjectId(session['user_id']),
            "article_url": url,
            "article_title": title,
            "source_url": url,
            "detection_label": label,
            "confidence_score": confidence,
            "ai_score": ai_score,
            "reasons": '; '.join(reasons),
            "reported_at": datetime.now()
        })
        
        try:
            domain = urlparse(url).netloc
        except:
            domain = url
        
        # Update Source
        source = db.fake_news_sources.find_one({"domain": domain})
        
        if source:
            # We need to calculate new average.
            # Avg = (Total + New) / (Count + 1)
            new_total = source.get('total_confidence', 0) + confidence
            new_count = source.get('report_count', 0) + 1
            new_avg = new_total / new_count
            
            db.fake_news_sources.update_one(
                {"domain": domain},
                {
                    "$set": {
                        "report_count": new_count,
                        "total_confidence": new_total,
                        "avg_confidence": new_avg,
                        "last_reported": datetime.now()
                    }
                }
            )
        else:
            db.fake_news_sources.insert_one({
                "source_url": url,
                "domain": domain,
                "report_count": 1,
                "total_confidence": confidence,
                "avg_confidence": confidence,
                "last_reported": datetime.now(),
                "is_blacklisted": 0  # Default value
            })
        
        return jsonify({
            "status": "success",
            "title": title,
            "label": label,
            "confidence": confidence,
            "ai_score": ai_score,
            "reasons": reasons
        })

    except Exception as e:
        print(f"Error: {e}") 
        return jsonify({"status": "error", "message": "Could not analyze URL. The site might be blocking our scanner."}), 500

# ---------- CHATBOT ----------
@app.route("/chatbot")
def chatbot_page():
    if 'user_id' not in session:
        return redirect(url_for('landing'))
    
    db = get_db()
    conversations = list(db.chatbot_conversations.find(
        {"user_id": ObjectId(session['user_id'])}
    ).sort("created_at", -1).limit(20))
    
    return render_template('chatbot.html', conversations=list(reversed(conversations)), username=session.get('username'))

@app.route("/chat", methods=["POST"])
def chat():
    if 'user_id' not in session:
        return jsonify({"status": "error"}), 401
    
    message = request.json.get('message', '').lower()
    response = ""
    
    if any(word in message for word in ['hello', 'hi', 'hey', 'kumusta']):
        response = "Hello! I'm True Bayan AI Assistant. I can help you with news, fake news detection, and navigating the app. What would you like to know?"
    elif 'fake news' in message or 'check' in message or 'verify' in message:
        response = "To check if news is fake, you can:\n\n1️⃣ Use the 'Report' button on any article\n2️⃣ Submit a URL in the Fake News Tracker\n3️⃣ Check the confidence score and AI rating shown on articles\n\nOur system analyzes suspicious keywords, source credibility, and writing patterns!"
    elif 'save' in message or 'bookmark' in message:
        response = "You can save articles by clicking the 'Save' button on any news card. The button will turn green when saved. Click it again to unsave. View all your saved articles in the 'Saved' section! 📚"
    elif 'category' in message or 'categories' in message or 'preferences' in message:
        response = "You can customize your news feed in the Preferences page! We have 12 categories:\n\n📰 Politics • 💼 Business • 💻 Technology • ⚽ Sports\n🎬 Entertainment • ❤️ Health • 🎓 Education • 🌿 Environment\n⚖️ Crime • ⛅ Weather • 🎨 Lifestyle • 🍔 Food\n\nSelect your interests and your feed will be personalized!"
    elif 'read' in message or 'article' in message:
        response = "Click the 'Read' button on any article to read it within True Bayan! No ads, no distractions - just clean, easy reading. You can also print or share articles from the reader! 📖"
    elif 'admin' in message:
        response = "The admin dashboard lets admins manage users, review fake news reports, and blacklist suspicious sources. Ask your administrator for access if you need it! 🔐"
    elif 'help' in message:
        response = "Here's what I can help with:\n\n✅ Finding Philippine news\n✅ Detecting fake news\n✅ Saving & organizing articles\n✅ Setting preferences\n✅ Reading articles in-app\n✅ Understanding credibility scores\n\nWhat would you like to know more about?"
    elif any(word in message for word in ['thank', 'thanks', 'salamat']):
        response = "You're very welcome! Glad I could help. Feel free to ask if you need anything else! 😊"
    elif 'latest' in message or 'news' in message:
        response = "Check out the Dashboard for the latest Philippine news! We have:\n\n🔥 Top Headlines\n⭐ Personalized for You\n🕐 Latest News\n\nAll filtered to show only Philippine-related stories!"
    else:
        response = "I can help you with:\n\n• News browsing\n• Fake news detection\n• Saving articles\n• App navigation\n• Setting preferences\n\nCould you please ask about a specific topic?"
    
    db = get_db()
    db.chatbot_conversations.insert_one({
        "user_id": ObjectId(session['user_id']),
        "message": message,
        "response": response,
        "created_at": datetime.now()
    })
    
    return jsonify({"response": response})

# ---------- USER PREFERENCES ----------
def get_user_preferences(user_id):
    db = get_db()
    prefs = db.user_preferences.find_one({"user_id": ObjectId(user_id)})
    return prefs

def get_personalized_news(user_id):
    prefs = get_user_preferences(user_id)
    
    if not prefs:
        return get_recommended_news()
    
    interests = []
    if prefs.get('category_politics'): interests.append('(politics OR government OR election OR senate OR congress) AND Philippines')
    if prefs.get('category_business'): interests.append('(business OR economy OR trade OR investment OR company) AND Philippines')
    if prefs.get('category_technology'): interests.append('(technology OR tech OR startup OR digital OR IT) AND Philippines')
    if prefs.get('category_sports'): interests.append('(sports OR basketball OR boxing OR PBA OR Gilas OR Pacquiao) AND Philippines')
    if prefs.get('category_entertainment'): interests.append('(entertainment OR celebrity OR movie OR showbiz OR ABS-CBN OR GMA) AND Philippines')
    if prefs.get('category_health'): interests.append('(health OR medical OR hospital OR COVID OR DOH) AND Philippines')
    if prefs.get('category_education'): interests.append('(education OR school OR university OR DepEd OR CHED) AND Philippines')
    if prefs.get('category_environment'): interests.append('(environment OR climate OR typhoon OR PAGASA) AND Philippines')
    if prefs.get('category_crime'): interests.append('(crime OR police OR PNP OR investigation) AND Philippines')
    if prefs.get('category_weather'): interests.append('(weather OR typhoon OR storm OR PAGASA OR forecast) AND Philippines')
    if prefs.get('category_lifestyle'): interests.append('(lifestyle OR fashion OR travel OR culture OR tourism) AND Philippines')
    if prefs.get('category_food'): interests.append('(food OR restaurant OR cuisine OR Filipino food) AND Philippines')
    
    if not interests:
        query = 'Philippines OR Manila OR "Philippine news"'
    else:
        query = ' OR '.join([f'({interest})' for interest in interests])
    
    try:
        recommended = newsapi.get_everything(
            q=query,
            language="en",
            sort_by="publishedAt",
            page_size=18
        )["articles"]
        
        recommended = filter_philippine_news(recommended)[:9]

        for article in recommended:
            desc = article.get("description") or ""
            article["summary"] = summarize(desc)
            article["label"] = detect_fake_news(desc)
            article["category"] = detect_category(article.get("title", ""), desc)

        # ATTACH SOCIAL DATA
        recommended = attach_social_data(recommended, user_id)

        return recommended
    except:
        return get_recommended_news()

def save_reading_history(user_id, article_title, article_url):
    db = get_db()
    db.reading_history.insert_one({
        "user_id": ObjectId(user_id),
        "article_title": article_title,
        "article_url": article_url,
        "read_at": datetime.now()
    })

def get_latest_news(user_id=None):
    latest = newsapi.get_everything(
        q='Philippines OR Manila OR Cebu OR Davao OR Duterte OR Marcos OR Philippine',
        language="en",
        sort_by="publishedAt",
        page_size=12
    )["articles"]
    
    latest = filter_philippine_news(latest)[:6]

    for article in latest:
        desc = article.get("description") or ""
        article["summary"] = summarize(desc)

    # ATTACH SOCIAL DATA
    latest = attach_social_data(latest, user_id)

    return latest

def get_recommended_news(user_id=None):
    topics = '(Philippines OR Manila OR Cebu OR Davao OR Mindanao OR Luzon OR Visayas OR "Philippine government" OR "Filipino" OR Quezon OR Makati OR Pasig)'

    recommended = newsapi.get_everything(
        q=topics,
        language="en",
        sort_by="publishedAt",
        page_size=12
    )["articles"]
    
    recommended = filter_philippine_news(recommended)[:6]

    for article in recommended:
        desc = article.get("description") or ""
        article["summary"] = summarize(desc)
    
    # ATTACH SOCIAL DATA
    recommended = attach_social_data(recommended, user_id)

    return recommended

# ---------- AUTHENTICATION ----------
@app.route("/register", methods=["GET", "POST"])
def register():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    
    if request.method == "POST":
        username = request.form.get("username")
        email = request.form.get("email")
        password = request.form.get("password")
        
        db = get_db()
        
        if db.users.find_one({"$or": [{"email": email}, {"username": username}]}):
            flash("User already exists!", "danger")
            return redirect(url_for('register'))
        
        hashed_password = generate_password_hash(password)
        
        result = db.users.insert_one({
            "username": username,
            "email": email,
            "password": hashed_password,
            "created_at": datetime.now(),
            "is_admin": 0 
        })
        
        user_id = result.inserted_id
        
        db.user_preferences.insert_one({"user_id": user_id})
        
        flash("Registration successful! Please login.", "success")
        return redirect(url_for('login'))
    
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")
        
        db = get_db()
        user = db.users.find_one({"email": email})
        
        if user and check_password_hash(user['password'], password):
            # MongoDB stores _id as ObjectId, need to cast to str for session
            session['user_id'] = str(user['_id'])
            session['username'] = user['username']
            flash("Login successful!", "success")
            return redirect(url_for('dashboard'))
        else:
            flash("Invalid credentials!", "danger")
    
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out successfully!", "success")
    return redirect(url_for('landing'))

# ---------- LANDING PAGE ----------
@app.route("/")
def landing():
    if 'user_id' not in session:
        latest_news = get_latest_news()
        
        headlines = newsapi.get_everything(
            q='Philippines OR Manila OR "Philippine news" OR Duterte OR Marcos',
            language="en",
            sort_by="publishedAt",
            page_size=12
        )["articles"]
        
        headlines = filter_philippine_news(headlines)[:6]
        
        for h in headlines:
            desc = h.get("description") or ""
            h["label"] = detect_fake_news(desc)
        
        # ATTACH SOCIAL DATA (No user ID, so just global counts)
        headlines = attach_social_data(headlines)

        return render_template(
            "landing.html",
            all_headlines=headlines,
            latest_news=latest_news
        )
    
    return redirect(url_for('dashboard'))

# ---------- DASHBOARD (PAGINATION) ----------
@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('landing'))
    
    user_id = session['user_id']
    recommended = get_personalized_news(user_id)
    prefs = get_user_preferences(user_id) # Added to fix undefined prefs error

    page = request.args.get('page', 1, type=int)
    category = request.args.get('category')  # Handle category filter
    per_page = 9
    
    all_articles = []
    latest_news = []
    total_pages = 0

    if request.method == "POST":
        keyword = request.form.get("keyword")

        search_query = f'({keyword}) AND (Philippines OR Manila OR Filipino OR "Philippine news")'

        raw_news = newsapi.get_everything(
            q=search_query,
            language="en",
            sort_by="relevancy",
            page_size=50
        )["articles"]
        
        all_articles = filter_philippine_news(raw_news)
        
        for article in all_articles:
            desc = article.get("description") or ""
            article["label"] = detect_fake_news(desc)
            article["summary"] = summarize(desc)
            article["filipino"] = translate_filipino(desc)
            article["category"] = detect_category(article.get("title", ""), desc)

        # ATTACH SOCIAL DATA
        all_articles = attach_social_data(all_articles, user_id)

        total_items = len(all_articles)
        total_pages = (total_items + per_page - 1) // per_page
        
        start = (page - 1) * per_page
        end = start + per_page
        paginated_articles = all_articles[start:end]

        return render_template(
            "home.html",
            all_articles=paginated_articles,
            keyword=keyword,
            latest_news=[],
            recommended_news=recommended,
            username=session.get('username'),
            page=page,
            total_pages=total_pages,
            prefs=prefs  # Pass prefs to template
        )

    # GET MODE
    if category:
        # Define keywords for each category
        category_keywords = {
            'Politics': 'politics OR government OR election OR senate OR congress',
            'Business': 'business OR economy OR market OR trade',
            'Technology': 'technology OR tech OR digital OR cyber',
            'Sports': 'sports OR basketball OR PBA OR boxing',
            'Entertainment': 'entertainment OR showbiz OR celebrity',
            'Health': 'health OR covid OR medical OR virus',
            'World': 'world news',
        }
        
        # Get keywords for selected category, default to general if not found
        keywords = category_keywords.get(category, category)
        
        # Construct query combining category keywords with Philippines context
        if category == 'World':
             query = f'({keywords})' # World news might not always have "Philippines" in text
        else:
             query = f'({keywords}) AND (Philippines OR Manila)'

        headlines = newsapi.get_everything(
            q=query,
            language="en",
            sort_by="publishedAt",
            page_size=18
        )["articles"]
    else:
        # Default Headlines if no category selected
        headlines = newsapi.get_everything(
            q='Philippines OR Manila OR "Philippine news" OR "Metro Manila" OR Cebu OR Davao',
            language="en",
            sort_by="publishedAt",
            page_size=18 
        )["articles"]

    headlines = filter_philippine_news(headlines)[:9]
    for h in headlines:
        desc = h.get("description") or ""
        h["label"] = detect_fake_news(desc)
        h["category"] = detect_category(h.get("title", ""), desc)
    
    # ATTACH SOCIAL DATA
    headlines = attach_social_data(headlines, user_id)

    raw_latest = newsapi.get_everything(
        q='Philippines OR Manila OR Cebu OR Davao OR Duterte OR Marcos OR Philippine',
        language="en",
        sort_by="publishedAt",
        page_size=50
    )["articles"]
    
    latest_news = filter_philippine_news(raw_latest)

    for article in latest_news:
        desc = article.get("description") or ""
        article["summary"] = summarize(desc)
        article["label"] = detect_fake_news(desc)
        article["category"] = detect_category(article.get("title", ""), desc)

    # ATTACH SOCIAL DATA
    latest_news = attach_social_data(latest_news, user_id)

    total_items = len(latest_news)
    total_pages = (total_items + per_page - 1) // per_page
    
    start = (page - 1) * per_page
    end = start + per_page
    paginated_latest = latest_news[start:end]

    return render_template(
        "home.html",
        all_headlines=headlines,
        latest_news=paginated_latest,
        recommended_news=recommended,
        username=session.get('username'),
        page=page,
        total_pages=total_pages,
        prefs=prefs  # Pass prefs to template
    )

@app.route("/preferences", methods=["GET", "POST"])
def preferences():
    if 'user_id' not in session:
        return redirect(url_for('landing'))
    
    if request.method == "POST":
        db = get_db()
        
        update_fields = {
            "category_politics": 1 if request.form.get('politics') else 0,
            "category_business": 1 if request.form.get('business') else 0,
            "category_technology": 1 if request.form.get('technology') else 0,
            "category_sports": 1 if request.form.get('sports') else 0,
            "category_entertainment": 1 if request.form.get('entertainment') else 0,
            "category_health": 1 if request.form.get('health') else 0,
            "category_education": 1 if request.form.get('education') else 0,
            "category_environment": 1 if request.form.get('environment') else 0,
            "category_crime": 1 if request.form.get('crime') else 0,
            "category_weather": 1 if request.form.get('weather') else 0,
            "category_lifestyle": 1 if request.form.get('lifestyle') else 0,
            "category_food": 1 if request.form.get('food') else 0
        }

        db.user_preferences.update_one(
            {"user_id": ObjectId(session['user_id'])},
            {"$set": update_fields}
        )
        
        flash("Preferences updated successfully!", "success")
        return redirect(url_for('dashboard'))
    
    prefs = get_user_preferences(session['user_id'])
    return render_template("preferences.html", prefs=prefs, username=session.get('username'))

@app.route("/save", methods=["POST"])
def save_article():
    if 'user_id' not in session:
        return jsonify({"status": "error", "message": "Not logged in"}), 401
    
    title = request.form["title"]
    url = request.form["url"]

    db = get_db()

    existing = db.saved_articles.find_one({
        "user_id": ObjectId(session['user_id']),
        "url": url
    })
    
    if existing:
        return jsonify({"status": "info", "message": "Already saved!"})

    db.saved_articles.insert_one({
        "user_id": ObjectId(session['user_id']),
        "title": title,
        "url": url,
        "saved_at": datetime.now()
    })

    return jsonify({"status": "success", "message": "Saved!"})

@app.route("/saved")
def saved():
    if 'user_id' not in session:
        return redirect(url_for('landing'))
    
    db = get_db()
    articles = list(db.saved_articles.find(
        {"user_id": ObjectId(session['user_id'])}
    ).sort("saved_at", -1))
    
    return render_template("saved.html", articles=articles, username=session.get('username'))

@app.route("/delete_saved", methods=["POST"])
def delete_saved():
    if 'user_id' not in session:
        return jsonify({"status": "error", "message": "Not logged in"}), 401
    
    article_id = request.json.get('article_id')
    
    db = get_db()
    result = db.saved_articles.delete_one({
        "_id": ObjectId(article_id),
        "user_id": ObjectId(session['user_id'])
    })
    
    if result.deleted_count > 0:
        return jsonify({"status": "success", "message": "Article removed"})
    else:
        return jsonify({"status": "error", "message": "Article not found"}), 404

@app.route("/history")
def history():
    if 'user_id' not in session:
        return redirect(url_for('landing'))
    
    db = get_db()
    history = list(db.reading_history.find(
        {"user_id": ObjectId(session['user_id'])}
    ).sort("read_at", -1).limit(50))
    
    return render_template("history.html", history=history, username=session.get('username'))

@app.route("/track_read", methods=["POST"])
def track_read():
    if 'user_id' not in session:
        return jsonify({"status": "error"}), 401
    
    title = request.json.get('title')
    url = request.json.get('url')
    
    save_reading_history(session['user_id'], title, url)
    return jsonify({"status": "success"})

@app.route("/report_fake", methods=["POST"])
def report_fake():
    if 'user_id' not in session:
        return jsonify({"status": "error", "message": "Not logged in"}), 401
    
    data = request.json
    article_url = data.get('url')
    article_title = data.get('title')
    description = data.get('description', '')
    
    label, confidence, ai_score, reasons = detect_fake_news_advanced(description, article_url)
    
    db = get_db()
    
    db.fake_news_reports.insert_one({
        "user_id": ObjectId(session['user_id']),
        "article_url": article_url,
        "article_title": article_title,
        "source_url": article_url,
        "detection_label": label,
        "confidence_score": confidence,
        "ai_score": ai_score,
        "reasons": '; '.join(reasons),
        "reported_at": datetime.now()
    })
    
    from urllib.parse import urlparse
    try:
        domain = urlparse(article_url).netloc
    except:
        domain = article_url
    
    source = db.fake_news_sources.find_one({"domain": domain})
    
    if source:
        new_total = source.get('total_confidence', 0) + confidence
        new_count = source.get('report_count', 0) + 1
        new_avg = new_total / new_count
        
        db.fake_news_sources.update_one(
            {"domain": domain},
            {
                "$set": {
                    "report_count": new_count,
                    "total_confidence": new_total,
                    "avg_confidence": new_avg,
                    "last_reported": datetime.now()
                }
            }
        )
    else:
        db.fake_news_sources.insert_one({
            "source_url": article_url,
            "domain": domain,
            "report_count": 1,
            "total_confidence": confidence,
            "avg_confidence": confidence,
            "last_reported": datetime.now(),
            "is_blacklisted": 0
        })
    
    return jsonify({
        "status": "success",
        "label": label,
        "confidence": confidence,
        "ai_score": ai_score,
        "reasons": reasons
    })

@app.route("/fake_news_tracker")
def fake_news_tracker():
    # 1. Security Check
    if 'user_id' not in session:
        return redirect(url_for('landing'))
    
    db = get_db()
    
    # 2. Get the current user's ID object
    user_id_obj = ObjectId(session['user_id'])
    
    # 3. GET USER'S HISTORY (The Fix)
    # We add {"user_id": user_id_obj} to the find() query.
    # This ensures we only fetch reports created by this specific account.
    recent_reports = list(db.fake_news_reports.find(
        {"user_id": user_id_obj} 
    ).sort("reported_at", -1).limit(50))
    
    # 4. Global Trends (Optional: Keep this global to show what's viral)
    # If you want this to be user-only too, add the {"user_id": user_id_obj} filter here as well.
    trending_sources = list(db.fake_news_sources.find().sort([
        ("report_count", -1), 
        ("avg_confidence", -1)
    ]).limit(20))
    
    # 5. User-Specific Statistics
    # Count only this user's reports
    total_reports = db.fake_news_reports.count_documents({"user_id": user_id_obj})
    
    # Calculate average confidence for THIS user only
    avg_result = list(db.fake_news_reports.aggregate([
        {"$match": {"user_id": user_id_obj}},
        {"$group": {"_id": None, "avg": {"$avg": "$confidence_score"}}}
    ]))
    avg_confidence = avg_result[0]['avg'] if avg_result else 0
    
    # Count high risk links found by THIS user
    high_risk_count = db.fake_news_reports.count_documents({
        "user_id": user_id_obj, 
        "confidence_score": {"$gte": 70}
    })
    
    return render_template(
        "fake_news_tracker.html",
        recent_reports=recent_reports,   # Now contains ONLY user's history
        trending_sources=trending_sources,
        total_reports=total_reports,
        avg_confidence=round(avg_confidence, 1),
        high_risk_count=high_risk_count,
        username=session.get('username')
    )

@app.route("/admin/dashboard")
def admin_dashboard():
    if 'user_id' not in session:
        return redirect(url_for('landing'))
    
    db = get_db()
    user = db.users.find_one({"_id": ObjectId(session['user_id'])})
    
    if not user or not user.get('is_admin'):
        flash("Access denied. Admin privileges required.", "danger")
        return redirect(url_for('dashboard'))
    
    users = list(db.users.find().sort("created_at", -1))
    
    # Simulating the Join for reports and users
    # In MongoDB, we use aggregation $lookup for joins
    pipeline = [
        {
            "$lookup": {
                "from": "users",
                "localField": "user_id",
                "foreignField": "_id",
                "as": "user_info"
            }
        },
        {"$unwind": "$user_info"},
        {"$sort": {"reported_at": -1}},
        {
            "$project": {
                "user_id": 1,
                "article_url": 1,
                "article_title": 1,
                "source_url": 1,
                "detection_label": 1,
                "confidence_score": 1,
                "ai_score": 1,
                "reasons": 1,
                "reported_at": 1,
                "username": "$user_info.username"
            }
        }
    ]
    all_reports = list(db.fake_news_reports.aggregate(pipeline))
    
    all_sources = list(db.fake_news_sources.find().sort([
        ("report_count", -1), 
        ("avg_confidence", -1)
    ]))
    
    total_users = db.users.count_documents({})
    total_reports = db.fake_news_reports.count_documents({})
    blacklisted_sources = db.fake_news_sources.count_documents({"is_blacklisted": 1})
    total_saved = db.saved_articles.count_documents({})
    
    return render_template(
        "admin_dashboard.html",
        users=users,
        all_reports=all_reports,
        all_sources=all_sources,
        total_users=total_users,
        total_reports=total_reports,
        blacklisted_sources=blacklisted_sources,
        total_saved=total_saved,
        username=session.get('username')
    )

@app.route("/admin/blacklist_source", methods=["POST"])
def blacklist_source():
    if 'user_id' not in session:
        return jsonify({"status": "error"}), 401
    
    source_id = request.json.get('source_id')
    action = request.json.get('action') 
    
    db = get_db()
    
    update_val = 1 if action == 'blacklist' else 0
    
    db.fake_news_sources.update_one(
        {"_id": ObjectId(source_id)},
        {"$set": {"is_blacklisted": update_val}}
    )
    
    return jsonify({"status": "success"})

@app.route("/api/news")
def api_news():
    news = newsapi.get_everything(
        q='Philippines OR Manila OR "Philippine news"',
        language="en",
        sort_by="publishedAt",
        page_size=15
    )["articles"]
    
    news = filter_philippine_news(news)[:10]

    # ATTACH SOCIAL DATA (Global only)
    news = attach_social_data(news)

    return jsonify(news)

if __name__ == "__main__":
    app.run(debug=True, port=5001)