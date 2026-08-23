import os
import sqlite3
import urllib.parse
import json
import time
import feedparser
import requests
from groq import Groq

# 1. Environment Secrets & Config
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MODEL_NAME = "openai/gpt-oss-120b"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

client = Groq(api_key=GROQ_API_KEY)

# 2. SQLite Database for zero duplicate alerts
conn = sqlite3.connect("gcc_leads.db")
cursor = conn.cursor()
cursor.execute("""
    CREATE TABLE IF NOT EXISTS seen_gccs (
        link TEXT PRIMARY KEY,
        company TEXT,
        city TEXT,
        date_added TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")
conn.commit()

# 3. Targeted Daily Queries (Strictly Last 24 Hours)
QUERIES = [
    '("Global Capability Center" OR "GCC" OR "Technology Center" OR "Tech Center") ("Bangalore" OR "Hyderabad" OR "Pune" OR "Chennai" OR "Gurgaon" OR "Noida" OR "Mumbai") ("launch" OR "set up" OR "expand" OR "opens" OR "invests" OR "leases") when:24h',
    '("Technology Center" OR "R&D Center" OR "Innovation Center") ("India") ("inaugurate" OR "establish" OR "workforce" OR "hire") when:24h',
    '("Global Capability Center" OR "GCC") ("leases" OR "office space" OR "sq ft") when:24h'
]

GCC_TRIGGER_WORDS = ["gcc", "capability center", "tech center", "technology center", "r&d center", "innovation center", "leases", "office space", "sq ft", "set up", "launch", "facility", "expand"]

def parse_date(entry):
    if hasattr(entry, 'published_parsed') and entry.published_parsed:
        return time.strftime("%d-%b-%Y", entry.published_parsed)
    elif hasattr(entry, 'published'):
        return entry.published[:16]
    return "Today"

def is_processed(link):
    cursor.execute("SELECT 1 FROM seen_gccs WHERE link = ?", (link,))
    return cursor.fetchone() is not None

def mark_processed(link, company, city):
    cursor.execute("INSERT OR IGNORE INTO seen_gccs (link, company, city) VALUES (?, ?, ?)", (link, company, city))
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
        "company": "Company Name",
        "city": "Bangalore/Hyderabad/Pune/Chennai/NCR/Mumbai/India",
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
    city = lead_data.get("city", "Tier-1 City")
    summary = lead_data.get("plan_summary", "")

    # Precise Headhunting Search Shortcuts
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
            "text": "GCC Founding Radar • Tier-1 Senior Permanent Hiring"
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
    print(f"🔍 Scanning daily GCC announcements using {MODEL_NAME}...")
    articles = []

    for q in QUERIES:
        encoded_query = urllib.parse.quote(q)
        rss_url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en-IN&gl=IN&ceid=IN:en"
        feed = feedparser.parse(rss_url)
        for entry in feed.entries[:15]:
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

        if is_processed(link) or not is_likely_gcc(title):
            continue

        time.sleep(0.6)  # Prevent rate limits
        analysis = analyze_with_llm(title)

        if analysis.get("is_gcc") and analysis.get("company"):
            company = analysis["company"].strip()
            
            if company.lower() in seen_in_run or company.lower() in ["unknown", "india", "gcc"]:
                continue
            seen_in_run.add(company.lower())

            print(f"✅ Verified GCC: {company} ({analysis.get('city')})")
            send_discord_alert(analysis, link, art["date"], title)
            new_leads_count += 1
            mark_processed(link, company, analysis.get("city", "India"))

    print(f"🏁 Finished. Sent {new_leads_count} verified GCC leads to Discord.")

if __name__ == "__main__":
    main()
