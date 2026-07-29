import requests
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from supabase_client import supabase
from datetime import datetime, timezone
import time
from urllib3.util import Retry
from requests.adapters import HTTPAdapter
import os 

TMDB_TOKEN = os.environ.get("TMDB_TOKEN")
# --- 1. SETUP RESILIENT SESSION ---
def create_resilient_session():
    session = requests.Session()
    session.headers.update({
        "accept": "application/json",
        "Authorization": f"Bearer {TMDB_TOKEN}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RecSysApp/1.0"
    })
    
    retries = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False
    )
    
    adapter = HTTPAdapter(max_retries=retries, pool_connections=15, pool_maxsize=15)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

session = create_resilient_session()

def fetch_credits(item_id, media_type):
    url = f"https://api.themoviedb.org/3/{media_type}/{item_id}/credits"
    for attempt in range(3):
        try:
            response = session.get(url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                cast = [actor['name'] for actor in data.get('cast', [])[:5]]
                crew = data.get('crew', [])
                director = [m['name'] for m in crew if m.get('job') in ('Director', 'Executive Producer')]
                return ", ".join(cast), (director[0] if director else "Unknown")
            elif response.status_code == 429:
                time.sleep(2 * (attempt + 1))
        except Exception:
            time.sleep(1)
    return "", "Unknown"

def fetch_new_content():
    new_items = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    print("Fetching Trending...")
    for page in range(1, 3): 
        for attempt in range(3):
            try:
                res = session.get(f"https://api.themoviedb.org/3/trending/all/day?language=en-US&page={page}", timeout=10)
                if res.status_code == 200:
                    new_items.extend(res.json().get('results', []))
                    break
            except Exception:
                time.sleep(1)
        
    print("Fetching Upcoming...")
    for page in range(1, 5): 
        url = f"https://api.themoviedb.org/3/discover/movie?include_adult=false&include_video=false&language=en-US&page={page}&primary_release_date.gte={today}&sort_by=popularity.desc"
        for attempt in range(3):
            try:
                res = session.get(url, timeout=10)
                if res.status_code == 200:
                    results = res.json().get('results', [])
                    for item in results:
                        item['media_type'] = 'movie' 
                    new_items.extend(results)
                    break
            except Exception:
                time.sleep(1)

    # Clean and format data
    formatted_data = []
    for item in new_items:
        tmdb_id = item.get("id")
        media_type = item.get("media_type", "movie")
        
        if media_type not in ["movie", "tv"]:
            continue
            
        release_date = item.get("release_date") or item.get("first_air_date")
        if not release_date: 
            continue

        is_upcoming = release_date > today
        if not is_upcoming and item.get("vote_count", 0) < 50:
            continue
            
        cast, director = fetch_credits(tmdb_id, media_type)
        
        # REMOVED origin_country ENTIRELY
        formatted_data.append({
            "tmdb_id": int(tmdb_id),
            "title": item.get("title") or item.get("name"),
            "media_type": media_type,
            "release_date": release_date,
            "genre_ids": str(item.get("genre_ids", [])),
            "popularity": item.get("popularity", 0),
            "vote_average": item.get("vote_average", 0),
            "vote_count": item.get("vote_count", 0),
            "overview": item.get("overview", ""),
            "poster_path": item.get("poster_path", ""),
            "original_language": item.get("original_language", ""),
            "cast": cast,
            "director": director
        })
        time.sleep(0.1) 
        
    return pd.DataFrame(formatted_data).drop_duplicates(subset=["tmdb_id"])

def update_database_and_vectors():
    print("1. Scraping new daily content...")
    new_df = fetch_new_content()
    print(f"Found {len(new_df)} trending/upcoming items.")
    
    # --- NEW GUARD CLAUSE ---
    if new_df.empty or "tmdb_id" not in new_df.columns:
        print("✅ No valid new items to process today. Stopping early.")
        return
    # ------------------------
    
    print("2. Downloading ALL media from Supabase (bypassing 1000-row limit)...")
    all_data = []
    start = 0
    step = 1000
    while True:
        res = supabase.table("media").select("tmdb_id, genre_ids, cast, director").range(start, start + step - 1).execute()
        data = res.data
        if not data: break
        all_data.extend(data)
        if len(data) < step: break
        start += step

    db_df = pd.DataFrame(all_data)
    
    existing_ids = set(db_df['tmdb_id'].astype(int))
    new_df_filtered = new_df[~new_df['tmdb_id'].astype(int).isin(existing_ids)]
    
    if new_df_filtered.empty:
        print("No new items to add today!")
        return

    print(f"3. Adding {len(new_df_filtered)} brand new items to Supabase...")
    combined_df = pd.concat([db_df, new_df_filtered], ignore_index=True)
    combined_df['combined_features'] = (
        combined_df['genre_ids'].astype(str) + " " + 
        combined_df['cast'].fillna("") + " " + 
        combined_df['director'].fillna("")
    )
    
    tfidf = TfidfVectorizer(max_features=300, stop_words='english')
    matrix = tfidf.fit_transform(combined_df['combined_features']).toarray()
    
    print("4. Pushing new rows and vectors to Database...")
    success_count = 0
    for idx, row in new_df_filtered.iterrows():
        matrix_index = combined_df[combined_df['tmdb_id'] == row['tmdb_id']].index[0]
        vector_str = f"[{','.join(map(str, matrix[matrix_index].tolist()))}]"
        
        row_dict = row.to_dict()
        row_dict["feature_vector"] = vector_str
        
        try:
            res = supabase.table("media").upsert(row_dict).execute()
            if res.data: success_count += 1
        except Exception as e:
            print(f"Error inserting {row['title']}: {e}")

    print(f"✅ Daily update complete! Added {success_count} new movies.")

if __name__ == "__main__":
    update_database_and_vectors()