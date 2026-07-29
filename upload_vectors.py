import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from supabase_client import supabase
from tqdm import tqdm

def upload_feature_vectors():
    print("1. Downloading ALL media from Supabase (bypassing 1000-row limit)...")
    all_data = []
    start = 0
    step = 1000
    
    # Loop to fetch every single row in chunks of 1000
    while True:
        # UPDATE 1: Added original_language to the select query
        res = supabase.table("media").select("tmdb_id, genre_ids, cast, director, original_language").range(start, start + step - 1).execute()
        data = res.data
        if not data:
            break
        all_data.extend(data)
        if len(data) < step:
            break
        start += step

    df = pd.DataFrame(all_data)
    # UPDATE 2: Updated the print statement to reflect the new international data
    print(f"✅ Downloaded {len(df)} total rows (including Global Dramas & Anime!).")
    
    # UPDATE 3: Added original_language to the combined features
    df['combined_features'] = (
        df['genre_ids'].astype(str) + " " + 
        df['cast'].fillna("") + " " + 
        df['director'].fillna("") + " " +
        df['original_language'].fillna("") 
    )
    
    print("2. Building TF-IDF Math Matrix (300 Dimensions)...")
    tfidf = TfidfVectorizer(max_features=300, stop_words='english')
    matrix = tfidf.fit_transform(df['combined_features']).toarray()
    
    print("3. Uploading vectors back to Supabase...")
    success_count = 0
    
    for i, row in tqdm(df.iterrows(), total=len(df)):
        vector_str = f"[{','.join(map(str, matrix[i].tolist()))}]"
        tmdb_id_clean = int(row['tmdb_id'])
        
        try:
            update_res = supabase.table("media").update({
                "feature_vector": vector_str
            }).eq("tmdb_id", tmdb_id_clean).execute()
            
            if update_res.data:
                success_count += 1
        except Exception as e:
            print(f"\nError on ID {tmdb_id_clean}: {e}")

    print(f"\n✅ Upload complete! Successfully saved {success_count} vectors to the database.")

if __name__ == "__main__":
    upload_feature_vectors()