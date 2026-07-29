import requests
import pandas as pd
import time
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib3.util import Retry
from requests.adapters import HTTPAdapter
import os

# --- UPDATED CONFIGURATION ---
TMDB_TOKEN = os.environ.get("TMDB_TOKEN")
CATEGORIES = [
    # Original
    {"type": "movie", "lang": "en", "name": "Hollywood_Movies", "pages": 50},
    {"type": "tv", "lang": "en", "name": "Hollywood_Series", "pages": 50},
    {"type": "movie", "lang": "hi", "name": "Bollywood_Movies", "pages": 50},
    {"type": "tv", "lang": "hi", "name": "Bollywood_Series", "pages": 50},
    {"type": "tv", "lang": "ko", "name": "K_Dramas", "pages": 50},
    {"type": "movie", "lang": "ko", "name": "Korean_Movies", "pages": 20},
    
    # New Drama Regions
    {"type": "tv", "lang": "zh", "name": "Chinese_Dramas", "pages": 30},
    {"type": "tv", "lang": "th", "name": "Thai_Dramas", "pages": 20},
    {"type": "tv", "lang": "es", "name": "Spanish_Series", "pages": 30},
    {"type": "tv", "lang": "tr", "name": "Turkish_Dramas", "pages": 20},
    {"type": "tv", "lang": "tl", "name": "Filipino_Series", "pages": 10},
    
    # Anime (Japanese Animation) - Note the added 'genre' key
    {"type": "tv", "lang": "ja", "name": "Anime_Series", "pages": 50, "genre": "16"}
]

def create_resilient_session():
    """Creates a persistent requests Session with automatic retries and connection pooling."""
    session = requests.Session()
    session.headers.update({
        "accept": "application/json",
        "Authorization": f"Bearer {TMDB_TOKEN}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RecSysApp/1.0"
    })
    
    # Configure retries for dropped connections or server-side 5xx / 429 errors
    retries = Retry(
        total=5,
        backoff_factor=1,  # Waits 1s, 2s, 4s, 8s between retries
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False
    )
    
    adapter = HTTPAdapter(max_retries=retries, pool_connections=15, pool_maxsize=15)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

# Initialize global resilient session
session = create_resilient_session()

def fetch_discover_page(media_type, language, page, genre=None):
    """Fetches a single page of top-rated/popular content safely."""
    url = f"https://api.themoviedb.org/3/discover/{media_type}"
    params = {
        "language": "en-US",
        "with_original_language": language,
        "sort_by": "popularity.desc",
        "vote_count.gte": 50,
        "page": page
    }
    
    # If a specific genre is required (like 16 for Anime), add it to params
    if genre:
        params["with_genres"] = genre
        
    for attempt in range(3):
        try:
            response = session.get(url, params=params, timeout=10)
            if response.status_code == 200:
                results = response.json().get("results", [])
                # Also capture original_language so we can save it!
                return results
            elif response.status_code == 429:
                time.sleep(2 * (attempt + 1))
        except (requests.exceptions.RequestException, Exception):
            time.sleep(1.5 * (attempt + 1))
            
    return []

def fetch_credits(item_id, media_type):
    """Fetches cast and crew for a specific movie or TV show safely."""
    url = f"https://api.themoviedb.org/3/{media_type}/{item_id}/credits"
    
    for attempt in range(3):
        try:
            response = session.get(url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                
                # Get Top 5 Cast Members
                cast = [actor['name'] for actor in data.get('cast', [])[:5]]
                
                # Get Director / Executive Producer
                crew = data.get('crew', [])
                director = [
                    member['name'] for member in crew 
                    if member.get('job') in ('Director', 'Executive Producer', 'Series Director')
                ]
                
                return {
                    "id": item_id,
                    "cast": ", ".join(cast),
                    "director": director[0] if director else "Unknown"
                }
            elif response.status_code == 429:
                time.sleep(2 * (attempt + 1))
        except Exception:
            time.sleep(1)
            
    return {"id": item_id, "cast": "", "director": "Unknown"}

def build_dataset():
    all_items = []
    
    print("Step 1: Fetching Base Data from /discover...")
    for cat in CATEGORIES:
        print(f"Fetching {cat['name']}...")
        for page in tqdm(range(1, cat["pages"] + 1)):
            # Pass the genre if it exists for this category
            results = fetch_discover_page(cat["type"], cat["lang"], page, cat.get("genre"))
            for item in results:
                all_items.append({
                    "tmdb_id": item["id"],
                    "title": item.get("title") or item.get("name"),
                    "media_type": cat["type"],
                    "origin_category": cat["name"],
                    "release_date": item.get("release_date") or item.get("first_air_date"),
                    "genre_ids": item.get("genre_ids", []),
                    "popularity": item.get("popularity", 0),
                    "vote_average": item.get("vote_average", 0),
                    "vote_count": item.get("vote_count", 0),
                    "overview": item.get("overview", ""),
                    "poster_path": item.get("poster_path", ""),
                    "original_language": item.get("original_language", cat["lang"]) # NEW!
                })
            time.sleep(0.08)  # Gentle pacing to keep connections smooth

    df = pd.DataFrame(all_items)
    df = df.drop_duplicates(subset=["tmdb_id"])
    print(f"\nCollected {len(df)} total baseline items.")

    print("\nStep 2: Fetching Cast & Crew in Parallel...")
    credits_data = []
    
    # Cap workers at 10 to keep Windows socket usage within stable limits
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_id = {
            executor.submit(fetch_credits, row.tmdb_id, row.media_type): row.tmdb_id 
            for row in df.itertuples()
        }
        
        for future in tqdm(as_completed(future_to_id), total=len(future_to_id)):
            credits_data.append(future.result())

    # Merge credits back with primary metadata
    credits_df = pd.DataFrame(credits_data)
    final_df = pd.merge(df, credits_df, left_on="tmdb_id", right_on="id").drop(columns=["id"])

    # Export to CSV
    final_df.to_csv("tmdb_baseline_dataset.csv", index=False)
    print("\n✅ Dataset successfully saved to tmdb_baseline_dataset.csv!")

if __name__ == "__main__":
    build_dataset()