import json
from supabase_client import supabase

def create_tables():
    pass

def _username_to_email(username):
    clean_name = username.lower().replace(" ", "")
    # Use example.com to pass Supabase's strict email validation
    return f"{clean_name}@watchlist.example.com"

def signup_user(username, password, dob):
    email = _username_to_email(username)
    try:
        res = supabase.auth.sign_up({"email": email, "password": password})
        if res.user:
            supabase.table("profiles").insert({
                "id": res.user.id,
                "username": username
            }).execute()
            return True, "Signup successful!", res.user.id
        return False, "Signup failed", None
    except Exception as e:
        return False, f"Error: {e}", None

def login_user(username, password):
    email = _username_to_email(username)
    try:
        res = supabase.auth.sign_in_with_password({"email": email, "password": password})
        if res.user:
            return True, res.user.id 
        return False, None
    except Exception as e:
        return False, None

def get_user_id(username):
    res = supabase.table("profiles").select("id").eq("username", username).execute()
    if res.data:
        return res.data[0]["id"]
    return None

def add_user_movie_history(uid, tmdb_id, rating, liked, watched_on):
    status = "completed" if liked else "dropped"
    data = {
        "user_id": uid,
        "media_id": tmdb_id,
        "rating": int(rating),
        "status": status
    }
    supabase.table("interactions").upsert(data, on_conflict="user_id,media_id").execute()

def add_user_movie_history(uid, tmdb_id, rating, status):
    """Upgraded to handle explicit statuses instead of just guessing from ratings."""
    data = {
        "user_id": uid,
        "media_id": tmdb_id,
        "rating": rating if rating > 0 else None, # Allow 0 for unrated 'Plan to Watch'
        "status": status
    }
    supabase.table("interactions").upsert(data, on_conflict="user_id,media_id").execute()

import pandas as pd
from supabase_client import supabase # Make sure this matches your import

def get_tracker_data(uid):
    """Fetches a user's tracked movies in chunks to avoid URL length limits."""
    
    # 1. Fetch user's interactions (ratings, status, etc.)
    history_res = supabase.table("interactions").select("*").eq("user_id", uid).execute()
    
    if not history_res.data:
        return pd.DataFrame()
        
    history_data = history_res.data
    
    # Extract the IDs (checking for both media_id or tmdb_id depending on your exact schema)
    tmdb_ids = [item.get('media_id') or item.get('tmdb_id') for item in history_data]
    tmdb_ids = [tid for tid in tmdb_ids if tid is not None] # Remove any empty ones
    
    if not tmdb_ids:
        return pd.DataFrame()

    # 2. CHUNK the query into batches of 50 to prevent connection errors
    chunk_size = 50
    all_media_data = []
    
    for i in range(0, len(tmdb_ids), chunk_size):
        chunk = tmdb_ids[i:i + chunk_size]
        
        # Fetch just this chunk of movie details
        media_res = supabase.table("media").select("*").in_("tmdb_id", chunk).execute()
        
        if media_res.data:
            all_media_data.extend(media_res.data)
            
    if not all_media_data:
        return pd.DataFrame()

    # 3. Merge the tracking status with the movie details
    history_df = pd.DataFrame(history_data)
    media_df = pd.DataFrame(all_media_data)
    
    # Standardize the column name to tmdb_id so we can merge them
    if 'media_id' in history_df.columns:
        history_df = history_df.rename(columns={'media_id': 'tmdb_id'})
        
    # Merge and return
    merged_df = pd.merge(history_df, media_df, on='tmdb_id', how='inner')
    
    return merged_df
def get_friend_id(uid):
    """Finds the 'other' user in the 2-player app."""
    res = supabase.table("profiles").select("id, username").neq("id", uid).limit(1).execute()
    if res.data:
        return res.data[0]["id"], res.data[0]["username"]
    return None, None

def get_user_history(uid):
    res = supabase.table("interactions").select("media_id, rating").eq("user_id", uid).execute()
    return [(row["media_id"], row["rating"]) for row in res.data]

# --- THE NEW VECTOR MATH FUNCTION ---
def update_user_profile_vector(uid, tmdb_id, weight):
    try:
        # 1. Get movie vector
        movie_res = supabase.table("media").select("feature_vector").eq("tmdb_id", tmdb_id).execute()
        if not movie_res.data or not movie_res.data[0].get("feature_vector"):
            return False, "No ML vector found for this movie in the database."
            
        movie_vec = movie_res.data[0]["feature_vector"]
        if isinstance(movie_vec, str):
            movie_vec = json.loads(movie_vec)

        # 2. Get user vector
        user_res = supabase.table("profiles").select("preference_vector").eq("id", uid).execute()
        user_vec = user_res.data[0].get("preference_vector") if user_res.data else None
        
        if isinstance(user_vec, str):
            user_vec = json.loads(user_vec)

        if not user_vec or len(user_vec) != 300:
            user_vec = [0.0] * 300

        # 3. Add the weighted movie vector to the user vector
        # We wrap m in float() just in case the JSON parser left it as a string
        new_vec = [u + (float(m) * weight) for u, m in zip(user_vec, movie_vec)]

        # 4. Strict String Formatting for pgvector
        # Supabase pgvector guarantees it will accept this exact string format "[v1,v2,v3]"
        new_vec_str = f"[{','.join(map(str, new_vec))}]"

        # 5. Save back to the database
        supabase.table("profiles").update({"preference_vector": new_vec_str}).eq("id", uid).execute()
        return True, "Success"
        
    except Exception as e:
        # We now RETURN the error instead of just printing it
        return False, str(e)