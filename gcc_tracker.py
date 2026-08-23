import os
import sqlite3
import urllib.parse
import json
import time
import re
import feedparser
import requests
from groq import Groq

# 1. Environment Secrets & Config
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MODEL_NAME = "openai/gpt-oss-120b"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

client = Groq(api_key=GROQ_API_KEY)

# 2. SQLite Database with Auto-Migration
conn = sqlite3.connect("gcc_leads.db")
cursor = conn.cursor()

# Auto-migrate table if old schema exists
cursor.execute("PRAGMA table_info(seen_gccs)")
existing_columns = [col[1] for col in cursor.fetchall()]

if not existing_columns or "brand_key" not in existing_columns:
    cursor.execute("DROP TABLE IF EXISTS seen_gccs")
    cursor.execute("""
        CREATE TABLE seen_gccs (
            brand_key TEXT PRIMARY KEY,
            company_name TEXT,
            city TEXT,
            date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()

# 3. Targeted RSS Queries Across All Tier-1 Indian Hubs & Gujarat
QUERIES = [
    # Gujarat & West Hubs (Ahmedabad, GIFT City, Pune, Mumbai)
    '("Global Capability Center" OR "GCC" OR "Technology Center" OR "Tech Center") ("Ahmedabad" OR "GIFT City" OR "Gandhinagar" OR "Gujarat" OR "Pune" OR "Mumbai") ("launch" OR "set up" OR "expand" OR "opens" OR "invests" OR "leases") when:2d',
    
    # South Hubs (Bangalore, Hyderabad, Chennai)
    '("Global Capability Center" OR "GCC" OR "Technology Center" OR "Tech Center") ("Bangalore" OR "Bengaluru" OR "Hyderabad" OR "Chennai") ("launch" OR "set up" OR "expand" OR "opens" OR "invests" OR "leases") when:2d',
    
    # North Hubs (Delhi-NCR, Gurgaon, Noida)
    '("Global Capability Center" OR "GCC" OR "Offshore Center") ("Gurgaon" OR "Gurugram" OR "Noida" OR "Delhi" OR "NCR") ("launch" OR "set up" OR "expand" OR "opens" OR "leases") when:2d',
    
    # Large Space Leases & SEZ Allocations
    '("Global Capability Center" OR "GCC" OR "R&D Center") ("leases" OR "office space" OR "sq ft" OR "workforce" OR "inaugurate") ("India" OR "Gujarat" OR "Maharashtra" OR "Karnataka" OR "Telangana") when:2d'
]

GCC_TRIGGER_WORDS = [
    "gcc", "capability center", "tech center", "technology center", "r&d center", 
    "innovation center", "leases", "office space", "sq ft", "set up", "launch", 
    "facility", "expand", "gift city", "ahmedabad", "pune", "mumbai"
]

def normalize_brand(name):
    """Normalizes brand names to prevent duplicate alerts (e.g., 'Nestle Business Services' -> 'nestle')"""
    clean = re.sub(r'(?i)\b(pharmaceuticals|business services|business solutions|services|solutions|group|technologies|technology|tech|india|pvt|ltd|limited|inc|corp|corporation)\b', '', name)
    clean = re.sub(r'[^a-zA-Z0-9]', '', clean).lower()
    return clean if len(clean) >= 3 else name.lower()

def parse_date(entry):
    if hasattr(entry, 'published_parsed') and entry.published_parsed:
        return time.strftime("%d-%b-%Y", entry.published_parsed)
    elif hasattr(entry, 'published'):
        return entry.published[:16]
    return "Recent"

def is_recent_article(entry, max_age_hours=48):
    """Enforces a hard date cutoff: Discards anything older than 48 hours"""
    if hasattr(entry, 'published_parsed') and entry.published_parsed:
        article_timestamp = time.mktime(entry.published_parsed)
        age_in_hours = (time.time() - article_timestamp) / 3600
        return age_in_hours <= max_age_hours
    return True

def is_brand_processed(brand_key):
    cursor.execute("SELECT 1 FROM seen_gccs WHERE brand_key = ?", (brand_key,))
    return cursor.fetchone() is not None

def mark_brand_processed(brand_key, company_name, city):
    cursor.execute("INSERT OR IGNORE INTO seen_gccs (brand_key, company_name, city) VALUES (?, ?, ?)", (brand_key, company_name, city))
    conn.commit()

def is_likely_gcc(title):
    t = title.lower()
    return any(w in t for w in GCC_TRIGGER_WORDS)

def analyze_with_llm(title, max_retries=3):
    prompt = f"""
    Analyze this headline: "{title}"
    
    Task:
    1. Determine if this represents a foreign or domestic company setting up, launching, expanding, or leasing space for a GCC/Tech/R&D center in India.
    2. Exclude: generic guides, education/school news, politics, food safety, or unrelated items.
    
    Return ONLY a JSON object:
    {{
        "is_gcc": true/false,
        "company": "Clean Core Brand Name (e.g. Align, PUMA, JLL, Columbia Group)",
        "city": "Ahmedabad/GIFT City/Pune/Mumbai/Bangalore/Hyderabad/Chennai/NCR/India",
        "plan_summary": "1-sentence summary of the expansion or hiring plan"
    }}
    """
    for attempt in range(max_retries):
        try:
            res = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1
            )
            content = res.choices[0].message.content.strip()
            if content.startswith("```json"):
                content = content[7:-3].strip()
            elif content.startswith("```"):
                content = content[3:-3].strip()
            return json.loads(content)
        except Exception as e:
            if "429" in str(e):
                time.sleep(3 * (attempt + 1))
            else:
                return {"is_gcc": False}
    return {"is_gcc": False}

def send_discord_alert(lead_data, article_url, date_str, original_title):
    if not DISCORD_WEBHOOK_URL:
        print("⚠️ No DISCORD_WEBHOOK_URL configured.")
        return

    company = lead_data.get("company", "Target Entity")
    city = lead_data.get("city", "Tier-1 Hub")
    summary = lead_data.get("plan_summary", "")

    # Clean Google Headhunting URLs
    dork_india_head = f'https://www.google.com/search?q=site:linkedin.com/in+"{urllib.parse.quote(company)}"+("Managing+Director"+OR+"Site+Leader"+OR+"Head+of+India"+OR+"Director+of+Engineering")+"{city}"'
    dork_global_exec = f'https://www.google.com/search?q=site:linkedin.com/in+"{urllib.parse.quote(company)}"+("Global+Head+of+Talent"+OR+"VP+Engineering"+OR+"Chief+Technology+Officer")'

    # Discord Rich Embed Card
    embed = {
        "title": f"🎯 NEW GCC SETUP DETECTED: {company}",
        "url": article_url,
        "color": 0x5865F2,  # Discord Blurple
        "fields": [
            {"name": "🏢 Company", "value": f"**{company}**", "inline": True},
            {"name": "📍 Target Hub", "value": f"**{city}**", "inline": True},
            {"name": "📅 Date", "value": f"{date_str}", "inline": True},
            {"name": "📋 Expansion Scope", "value": summary, "inline": False},
            {
                "name": "⚡ Executive Search Shortcuts",
                "value": f"• [👤 Find India MD / Site Leader]({dork_india_head})\n• [💼 Find Global CTO / TA Head]({dork_global_exec})",
                "inline": False
            },
            {
                "name": "💼 BD Strategy",
                "value": "Pitch founding leadership search (**₹80L – ₹1.5Cr CTC**).",
                "inline": False
            }
        ],
        "footer": {
            "text": "GCC Founding Radar • Tier-1 & Gujarat Senior Hiring"
        }
    }

    payload = {
        "username": "GCC Founding Radar",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/3281/3281329.png",
        "embeds": [embed]
    }

    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        if res.status_code == 204:
            print(f"🚀 Sent to Discord: {company}")
        else:
            print(f"Discord Response: {res.status_code}, {res.text}")
    except Exception as e:
        print(f"Failed to post to Discord: {e}")

def main():
    print(f"🔍 Scanning fresh GCC announcements across Tier-1 & Gujarat hubs using {MODEL_NAME}...")
    articles = []

    for q in QUERIES:
        encoded_query = urllib.parse.quote(q)
        rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-IN&gl=IN&ceid=IN:en"
        feed = feedparser.parse(rss_url)
        for entry in feed.entries[:20]:
            # Hard 48-Hour Date Filter
            if not is_recent_article(entry, max_age_hours=48):
                continue
                
            articles.append({
                "title": entry.title,
                "link": entry.link,
                "date": parse_date(entry)
            })

    new_leads_count = 0
    seen_in_run = set()

    for art in articles:
        link = art["link"]
        title = art["title"]

        if not is_likely_gcc(title):
            continue

        time.sleep(0.6)  # Prevent rate limits
        analysis = analyze_with_llm(title)

        if analysis.get("is_gcc") and analysis.get("company"):
            company = analysis["company"].strip()
            brand_key = normalize_brand(company)
            
            # Global & Run-level Deduplication
            if brand_key in seen_in_run or is_brand_processed(brand_key) or brand_key in ["unknown", "india", "gcc"]:
                continue

            seen_in_run.add(brand_key)
            print(f"✅ Verified Fresh GCC: {company} ({analysis.get('city')})")
            send_discord_alert(analysis, link, art["date"], title)
            new_leads_count += 1
            mark_brand_processed(brand_key, company, analysis.get("city", "India"))

    print(f"🏁 Finished. Sent {new_leads_count} fresh, unique GCC leads to Discord.")

if __name__ == "__main__":
    main()
