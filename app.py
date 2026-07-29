import streamlit as st
import pandas as pd
import math
import json
import logging
from datetime import datetime, UTC, timedelta
from supabase_client import supabase

from sql_utils import (
    signup_user, login_user, get_user_id,
    add_user_movie_history, create_tables,
    get_user_history, update_user_profile_vector,
    get_tracker_data, get_friend_id
)

# --- LOGGING SETUP ---
logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

st.set_page_config(page_title="🎬 2-Player Watchlist", page_icon="🍿", layout="wide")

# --- UI Beautification (Custom CSS) ---
st.markdown("""
<style>
    .block-container { padding-top: 1.5rem; padding-bottom: 2rem; }
    h1, h2, h3 { color: #ff4b4b; font-family: 'Helvetica Neue', sans-serif; margin-bottom: 0px; }
    .stButton > button { border-radius: 8px; font-weight: bold; transition: 0.3s; }
    .stButton > button:hover { border-color: #ff4b4b; color: #ff4b4b; }
    img { border-radius: 12px; transition: transform 0.2s ease-in-out; }
    img:hover { transform: scale(1.03); }
    .cast-chip {
        display: inline-block;
        background-color: #262730;
        color: #00d46a;
        padding: 4px 10px;
        border-radius: 16px;
        margin-right: 6px;
        margin-bottom: 6px;
        font-size: 0.85em;
    }
    .title-card {
        font-weight: 700;
        text-align: center;
        height: 2.8em;
        overflow: hidden;
        margin-top: 4px;
        font-size: 0.88em;
        line-height: 1.3em;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
    }
    .star-label {
        font-size: 0.78em;
        color: #aaa;
        margin-left: 4px;
    }
</style>
""", unsafe_allow_html=True)

# --- Config ---
TOTAL_TO_SHOW = 96
PAGE_SIZE = 12
POSTER_W = 200
# Local SVG fallback instead of unreliable via.placeholder.com
PLACEHOLDER_POSTER = "https://placehold.co/200x300/1a1a2e/ff4b4b?text=No+Image"

STAR_LABELS = {0: "", 1: "Poor", 2: "Fair", 3: "Good", 4: "Great", 5: "Must Watch"}

# TMDB Genre Mapping
TMDB_GENRES = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
    27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance",
    878: "Science Fiction", 10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western",
    10759: "Action & Adventure", 10765: "Sci-Fi & Fantasy", 10768: "War & Politics"
}

LANG_CODE_MAP = {
    "English": "en",
    "Korean": "ko",
    "Japanese": "ja",
    "Chinese": "zh",
    "Spanish": "es",
    "Hindi": "hi",
    "Thai": "th",
    "Filipino": "tl"
}

create_tables()

# --- SESSION & SANITIZATION ---
def init_session():
    defaults = {
        "logged_in": False,
        "username": None,
        "user_id": None,
        "friend_name": "Friend",
        "friend_id_cache": None,      # cache friend_id to avoid double DB calls
        "page": 1,
        "search_query": "",
        "search_type": "general",
        "selected_tmdb_id": None,
        "history_stack": [],
        "view": "home",
        "active_filter": "All",
        "seen_media": set(),
        "rec_cache": None,            # cache recommendations for session
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_session()

def sanitize_df(df):
    """Normalize a raw DataFrame into a consistent media schema. Call once per source — do not call on already-sanitized data."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["tmdb_id", "title", "poster_path", "media_type",
                                     "original_language", "popularity", "vote_average",
                                     "cast", "director", "genre_ids"])
    df = df.copy()
    if "media_type" not in df.columns:
        df["media_type"] = "movie"
    if "original_language" not in df.columns:
        df["original_language"] = "en"
    if "title" not in df.columns and "name" in df.columns:
        df["title"] = df["name"]
    return df

def escape_ilike(value: str) -> str:
    """Escape % and _ wildcards in user input before using in ilike queries."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

def log_impressions(tmdb_ids):
    if not st.session_state.logged_in:
        return
    new_ids = [tid for tid in tmdb_ids if tid not in st.session_state.seen_media]
    if new_ids:
        st.session_state.seen_media.update(new_ids)
        try:
            supabase.rpc("log_media_impressions", {
                "user_id_input": st.session_state.user_id,
                "tmdb_ids": new_ids
            }).execute()
        except Exception as e:
            logger.warning("log_impressions failed: %s", e)

def get_cached_friend_id():
    """Return (friend_id, friend_name) — cached in session to avoid double DB round-trips."""
    if st.session_state.friend_id_cache is not None:
        return st.session_state.friend_id_cache, st.session_state.friend_name
    fid, fname = get_friend_id(st.session_state.user_id)
    st.session_state.friend_id_cache = fid
    if fname:
        st.session_state.friend_name = fname
    return fid, st.session_state.friend_name

# --- SUPABASE DATA FETCHERS ---

@st.cache_data(ttl=300, show_spinner=False)
def load_movies_search(query, search_type="general", limit=TOTAL_TO_SHOW):
    if not query or not query.strip():
        return pd.DataFrame()
    cleaned_query = query.strip()
    safe_query = escape_ilike(cleaned_query)

    if search_type == "cast":
        res = supabase.table("media").select("*").ilike("cast", f"%{safe_query}%").order("popularity", desc=True).limit(limit).execute()
        return sanitize_df(pd.DataFrame(res.data) if res.data else pd.DataFrame())
    if search_type == "director":
        res = supabase.table("media").select("*").ilike("director", f"%{safe_query}%").order("popularity", desc=True).limit(limit).execute()
        return sanitize_df(pd.DataFrame(res.data) if res.data else pd.DataFrame())

    # Fuzzy RPC first
    try:
        res = supabase.rpc("fuzzy_search_movies", {"search_term": cleaned_query, "limit_count": limit}).execute()
        if res.data and len(res.data) > 0:
            return sanitize_df(pd.DataFrame(res.data))
    except Exception as e:
        logger.warning("fuzzy_search_movies RPC failed: %s", e)

    # OR filter fallback
    search_filter = f"title.ilike.%{safe_query}%,cast.ilike.%{safe_query}%,director.ilike.%{safe_query}%"
    res = supabase.table("media").select("*").or_(search_filter).order("popularity", desc=True).limit(limit).execute()
    if res.data and len(res.data) > 0:
        return sanitize_df(pd.DataFrame(res.data))

    # Short prefix fallback
    fallback_query = escape_ilike(cleaned_query[:4] if len(cleaned_query) >= 4 else cleaned_query)
    fb_filter = f"title.ilike.%{fallback_query}%,cast.ilike.%{fallback_query}%,director.ilike.%{fallback_query}%"
    res = supabase.table("media").select("*").or_(fb_filter).order("popularity", desc=True).limit(limit).execute()
    return sanitize_df(pd.DataFrame(res.data) if res.data else pd.DataFrame())


@st.cache_data(ttl=300, show_spinner=False)
def load_filtered_media(category="all", language="all", limit=TOTAL_TO_SHOW):
    query = supabase.table("media").select("*")
    today_str = datetime.now(UTC).strftime("%Y-%m-%d")

    if category == "movies":
        query = query.eq("media_type", "movie").lte("release_date", today_str)
    elif category == "series":
        query = query.eq("media_type", "tv").lte("release_date", today_str)
    elif category == "anime":
        query = query.ilike("genre_ids", "%16%").eq("original_language", "ja").lte("release_date", today_str)
    elif category == "drama":
        query = query.eq("media_type", "tv").in_("original_language", ["ko", "zh", "th", "tl", "es", "tr"]).lte("release_date", today_str)
    elif category == "upcoming":
        query = query.gte("release_date", today_str).order("release_date", desc=False)

    if language != "all" and language in LANG_CODE_MAP:
        query = query.eq("original_language", LANG_CODE_MAP[language])
    if category != "upcoming":
        query = query.order("popularity", desc=True)

    res = query.limit(limit).execute()
    return sanitize_df(pd.DataFrame(res.data) if res.data else pd.DataFrame())


@st.cache_data(ttl=600, show_spinner=False)
def load_discover_media(limit=TOTAL_TO_SHOW):
    res = supabase.table("media").select("*").gte("vote_average", 7.0).lte("popularity", 60).limit(limit).execute()
    df = sanitize_df(pd.DataFrame(res.data) if res.data else pd.DataFrame())
    if not df.empty:
        df["rec_source"] = "novelty"
    return df


@st.cache_data(ttl=300, show_spinner=False)
def load_movies_by_genre(genre_id, limit=TOTAL_TO_SHOW):
    res = supabase.table("media").select("*").ilike("genre_ids", f"%{genre_id}%").order("popularity", desc=True).limit(limit).execute()
    return sanitize_df(pd.DataFrame(res.data) if res.data else pd.DataFrame())


def load_similar_movies(movie, limit=8):
    if not movie:
        return sanitize_df(pd.DataFrame())
    tmdb_id = movie.get("tmdb_id")
    director = movie.get("director")
    genre_ids = movie.get("genre_ids")
    results = []

    if director and director != "N/A":
        safe_dir = escape_ilike(str(director))
        try:
            res = supabase.table("media").select("*").ilike("director", f"%{safe_dir}%").neq("tmdb_id", tmdb_id).limit(6).execute()
            if res.data:
                results.extend(res.data)
        except Exception as e:
            logger.warning("load_similar_movies director query failed: %s", e)

    if genre_ids:
        first_genre = str(genre_ids).split(",")[0].replace("[", "").replace("]", "").strip()
        if first_genre:
            try:
                res = supabase.table("media").select("*").ilike("genre_ids", f"%{first_genre}%").neq("tmdb_id", tmdb_id).order("popularity", desc=True).limit(10).execute()
                if res.data:
                    results.extend(res.data)
            except Exception as e:
                logger.warning("load_similar_movies genre query failed: %s", e)

    if not results:
        try:
            res = supabase.table("media").select("*").neq("tmdb_id", tmdb_id).order("popularity", desc=True).limit(limit).execute()
            if res.data:
                results.extend(res.data)
        except Exception as e:
            logger.warning("load_similar_movies fallback failed: %s", e)

    seen = set()
    unique_results = []
    for item in results:
        if item.get("tmdb_id") not in seen and item.get("tmdb_id") != tmdb_id:
            seen.add(item.get("tmdb_id"))
            unique_results.append(item)

    return sanitize_df(pd.DataFrame(unique_results[:limit]))


def load_friend_must_watch(uid, limit=TOTAL_TO_SHOW):
    friend_id, _ = get_cached_friend_id()
    if not friend_id:
        return sanitize_df(pd.DataFrame())

    my_tracker = get_tracker_data(uid)
    my_watched_ids = set(my_tracker['tmdb_id'].tolist()) if not my_tracker.empty else set()

    try:
        res = supabase.table("interactions").select("media_id").eq("user_id", friend_id).eq("rating", 5).execute()
        friend_5_star_ids = [row["media_id"] for row in (res.data or []) if row.get("media_id") not in my_watched_ids]
    except Exception as e:
        logger.warning("load_friend_must_watch 5-star fetch failed: %s", e)
        friend_5_star_ids = []

    friend_recs_ids = []
    if len(friend_5_star_ids) < limit:
        try:
            f_rec_res = supabase.rpc("recommend_movies", {"user_id_input": friend_id, "match_count": limit + 20}).execute()
            if f_rec_res.data:
                for row in f_rec_res.data:
                    mid = row.get("tmdb_id")
                    if mid not in my_watched_ids and mid not in friend_5_star_ids:
                        friend_recs_ids.append(mid)
        except Exception as e:
            logger.warning("load_friend_must_watch collab RPC failed: %s", e)

    final_ids = (friend_5_star_ids + friend_recs_ids)[:limit]
    if not final_ids:
        return sanitize_df(pd.DataFrame())

    try:
        media_res = supabase.table("media").select("*").in_("tmdb_id", final_ids).execute()
        if media_res.data:
            df = pd.DataFrame(media_res.data)
            df['sort_order'] = df['tmdb_id'].apply(lambda x: final_ids.index(x) if x in final_ids else 999)
            df = df.sort_values('sort_order').drop(columns=['sort_order'])
            df['rec_source'] = df['tmdb_id'].apply(
                lambda x: "friend_5star" if x in friend_5_star_ids else "friend_collab"
            )
            return sanitize_df(df)
    except Exception as e:
        logger.warning("load_friend_must_watch media fetch failed: %s", e)

    return sanitize_df(pd.DataFrame())


def get_movie_by_tmdb_id(tmdb_id):
    try:
        res = supabase.table("media").select("*").eq("tmdb_id", tmdb_id).execute()
        return res.data[0] if res.data and len(res.data) > 0 else None
    except Exception as e:
        logger.warning("get_movie_by_tmdb_id failed for %s: %s", tmdb_id, e)
        return None


def get_recommendations(user_id):
    # Return cached value if already fetched this session
    if st.session_state.rec_cache is not None:
        return st.session_state.rec_cache
    try:
        res = supabase.rpc("recommend_movies", {"user_id_input": user_id, "match_count": 200}).execute()
        df = sanitize_df(pd.DataFrame(res.data)) if res.data else sanitize_df(pd.DataFrame())
    except Exception as e:
        logger.warning("get_recommendations RPC failed: %s", e)
        df = sanitize_df(pd.DataFrame())
    st.session_state.rec_cache = df
    return df

# --- NAVIGATION & TRACKING LOGIC ---
def set_selected_movie(tmdb_id):
    if not st.session_state.history_stack or st.session_state.history_stack[-1] != tmdb_id:
        st.session_state.history_stack.append(tmdb_id)
    st.session_state.selected_tmdb_id = tmdb_id

def set_view(target_view):
    st.session_state.view = target_view
    st.session_state.page = 1
    st.session_state.selected_tmdb_id = None
    st.session_state.history_stack.clear()
    st.rerun()

def save_tracker_for_selected(tmdb_id, rating_value, status_value):
    if not st.session_state.logged_in:
        st.error("You must be logged in to track media.")
        return
    uid = st.session_state.user_id

    if rating_value > 0 and status_value in ["plan_to_watch", "watching"]:
        status_value = "completed"
        st.info("💡 Automatically moved to 'Completed' because you gave it a rating.")

    try:
        add_user_movie_history(uid, tmdb_id, rating=rating_value, status=status_value)
    except Exception as e:
        logger.error("save_tracker_for_selected add_history failed: %s", e)
        st.error("Failed to save. Please try again.")
        return

    if rating_value > 0 and status_value == "completed":
        weight = {5.0: 2.0, 4.0: 1.0, 3.0: 0.5, 2.0: -1.0, 1.0: -2.0}.get(float(rating_value), 0.0)
        try:
            update_user_profile_vector(uid, tmdb_id, weight)
        except Exception as e:
            logger.warning("save_tracker_for_selected vector update failed: %s", e)

    # Bust recommendation cache so next home visit re-fetches
    st.session_state.rec_cache = None
    st.toast(f"Saved to {status_value.replace('_', ' ').title()}! ✔️", icon="✅")

# --- PLAYLIST DIALOG ---
@st.dialog("Manage Playlists 📂")
def add_to_playlist_dialog(tmdb_id):
    uid = st.session_state.user_id

    try:
        playlists = supabase.table("playlists").select("*").eq("user_id", uid).execute().data or []
        p_ids = [p['id'] for p in playlists]
        existing = supabase.table("playlist_items").select("playlist_id").eq("tmdb_id", tmdb_id).in_("playlist_id", p_ids).execute().data if p_ids else []
    except Exception as e:
        logger.warning("add_to_playlist_dialog fetch failed: %s", e)
        playlists, existing = [], []

    current_in_playlists = {item['playlist_id'] for item in existing}

    st.write("Add or remove this media from your custom lists:")

    for p in playlists:
        pid = p['id']
        pname = p['name']
        is_in = pid in current_in_playlists
        toggled = st.checkbox(f"📁 {pname}", value=is_in, key=f"cb_{pid}")

        if toggled and not is_in:
            try:
                supabase.table("playlist_items").insert({"playlist_id": pid, "tmdb_id": tmdb_id}).execute()
                save_tracker_for_selected(tmdb_id, 0, "plan_to_watch")
            except Exception as e:
                logger.warning("playlist insert failed (possible duplicate): %s", e)
            st.rerun()
        elif not toggled and is_in:
            try:
                supabase.table("playlist_items").delete().eq("playlist_id", pid).eq("tmdb_id", tmdb_id).execute()
            except Exception as e:
                logger.warning("playlist delete failed: %s", e)
            st.rerun()

    st.markdown("---")
    new_pl = st.text_input("Create New Playlist", placeholder="Name... (e.g. Action Night)")
    if st.button("➕ Create & Add", use_container_width=True):
        if new_pl.strip():
            try:
                ins = supabase.table("playlists").insert({"user_id": uid, "name": new_pl.strip()}).execute()
                if ins.data:
                    new_pid = ins.data[0]["id"]
                    supabase.table("playlist_items").insert({"playlist_id": new_pid, "tmdb_id": tmdb_id}).execute()
                    save_tracker_for_selected(tmdb_id, 0, "plan_to_watch")
                    st.toast(f"Created '{new_pl.strip()}' and added!", icon="✅")
                    st.rerun()
            except Exception as e:
                logger.error("create playlist failed: %s", e)
                st.error("Could not create playlist. Try again.")

# --- AUTH UI ---
def login_signup_page():
    st.title("🎬 2-Player Watchlist")
    st.markdown("---")
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        tab1, tab2 = st.tabs(["🔐 Login", "📝 Sign Up"])
        with tab1:
            with st.container(border=True):
                with st.form("login_form"):
                    login_user_input = st.text_input("Username")
                    login_pass = st.text_input("Password", type="password")
                    if st.form_submit_button("Login", use_container_width=True):
                        success, uid = login_user(login_user_input, login_pass)
                        if success:
                            st.session_state.logged_in = True
                            st.session_state.username = login_user_input
                            st.session_state.user_id = uid
                            st.rerun()
                        else:
                            st.error("Invalid credentials.")
        with tab2:
            with st.container(border=True):
                with st.form("signup_form"):
                    new_user = st.text_input("Choose a username")
                    new_pass = st.text_input("Choose a password (min 6 chars)", type="password")
                    dob = st.date_input("Date of Birth", value=datetime.now(UTC) - timedelta(days=365 * 25))
                    if st.form_submit_button("Sign Up", use_container_width=True):
                        ok, msg, uid = signup_user(new_user, new_pass, str(dob))
                        if ok:
                            st.session_state.logged_in = True
                            st.session_state.username = new_user
                            st.session_state.user_id = uid
                            st.rerun()
                        else:
                            st.error(msg)

# --- UI COMPONENTS ---
def render_media_card(row, col, idx):
    poster = f"https://image.tmdb.org/t/p/w500{row.get('poster_path')}" if row.get('poster_path') else PLACEHOLDER_POSTER
    tmdb_id = row.get("tmdb_id")
    fname = st.session_state.friend_name

    with col.container(border=True):
        st.image(poster, use_container_width=True)
        st.markdown(
            f"<div class='title-card'>{row.get('title', 'Unknown')}</div>",
            unsafe_allow_html=True
        )

        source = row.get("rec_source")
        sim = row.get("similarity")
        if source in ["friend_5star", "friend"]:
            st.markdown(f"<div style='text-align:center; color:#f1c40f; font-size:0.8em;'><b>👑 {fname}'s Pick</b></div>", unsafe_allow_html=True)
        elif source == "friend_collab":
            st.markdown(f"<div style='text-align:center; color:#3498db; font-size:0.8em;'><b>🤝 {fname} Match</b></div>", unsafe_allow_html=True)
        elif source == "novelty":
            st.markdown("<div style='text-align:center; color:#9b59b6; font-size:0.8em;'><b>✨ Discovery</b></div>", unsafe_allow_html=True)
        elif sim is not None and not pd.isna(sim):
            st.markdown(f"<div style='text-align:center; color:#2ecc71; font-size:0.8em;'><b>{round(sim * 100)}% Match</b></div>", unsafe_allow_html=True)
        else:
            st.markdown("<div style='height: 1.2em;'></div>", unsafe_allow_html=True)

        c1, c2 = st.columns([1, 1])
        c1.button("👀", key=f"v_{tmdb_id}_{idx}", on_click=set_selected_movie, args=(tmdb_id,), use_container_width=True, help="Details")
        if c2.button("➕", key=f"q_{tmdb_id}_{idx}", use_container_width=True, help="Add to Playlist"):
            if not st.session_state.logged_in:
                st.toast("You must be logged in to save media.", icon="⚠️")
            else:
                add_to_playlist_dialog(tmdb_id)

def render_horizontal_row(title, df, fallback_category, limit=8):
    df = sanitize_df(df)

    # Filter by language first, then decide if fallback is needed
    if st.session_state.active_filter != "All" and "original_language" in df.columns:
        target_lang = LANG_CODE_MAP.get(st.session_state.active_filter)
        if target_lang:
            df = df[df["original_language"] == target_lang]

    if len(df) < limit and fallback_category != "must_watch":
        db_df = load_filtered_media(category=fallback_category, language=st.session_state.active_filter, limit=limit)
        df = pd.concat([df, db_df]).drop_duplicates(subset=["tmdb_id"])

    if df.empty:
        return
    df = df.head(limit).reset_index(drop=True)
    log_impressions(df["tmdb_id"].tolist())

    header_col, btn_col = st.columns([8, 2])
    with header_col:
        st.markdown(f"### {title}")
    with btn_col:
        if st.button("View All ➡️", key=f"btn_{fallback_category}", use_container_width=True):
            set_view(fallback_category)

    st.markdown("<hr style='margin: 0px; padding-bottom: 15px;'/>", unsafe_allow_html=True)

    rows = [df.iloc[0:4], df.iloc[4:8]]
    for row_df in rows:
        if row_df.empty:
            continue
        cols = st.columns(4, gap="large")
        for idx, (_, row) in enumerate(row_df.iterrows()):
            render_media_card(row, cols[idx % 4], f"{fallback_category}_{idx}")

def render_full_grid(title, df):
    st.header(title)
    df = sanitize_df(df)
    if st.session_state.active_filter != "All" and "original_language" in df.columns:
        if st.session_state.active_filter in LANG_CODE_MAP:
            df = df[df["original_language"] == LANG_CODE_MAP[st.session_state.active_filter]]
    if df.empty:
        st.info("No media matches the current language filter in this category.")
        return

    df = df.head(TOTAL_TO_SHOW).reset_index(drop=True)
    total_pages = max(1, math.ceil(len(df) / PAGE_SIZE))
    if st.session_state.page > total_pages:
        st.session_state.page = total_pages

    start = (st.session_state.page - 1) * PAGE_SIZE
    page_df = df.iloc[start:start + PAGE_SIZE].reset_index(drop=True)
    log_impressions(page_df["tmdb_id"].tolist())

    cols = st.columns(4, gap="large")
    for i, row in page_df.iterrows():
        render_media_card(row, cols[i % 4], f"grid_{i}")

    st.markdown("---")
    pag_col1, pag_col2, pag_col3 = st.columns([1, 2, 1])
    with pag_col1:
        if st.button("⬅️ Previous", use_container_width=True) and st.session_state.page > 1:
            st.session_state.page -= 1
            st.rerun()
    with pag_col2:
        st.markdown(f"<div style='text-align:center;'>Page <b>{st.session_state.page}</b> of {total_pages}</div>", unsafe_allow_html=True)
    with pag_col3:
        if st.button("Next ➡️", use_container_width=True) and st.session_state.page < total_pages:
            st.session_state.page += 1
            st.rerun()

def show_details_pane(tmdb_id):
    movie = get_movie_by_tmdb_id(tmdb_id)
    if not movie:
        st.warning("Movie details not found in DB.")
        return

    poster = f"https://image.tmdb.org/t/p/w500{movie.get('poster_path')}" if movie.get('poster_path') else PLACEHOLDER_POSTER

    left, right = st.columns([1, 2])
    with left:
        st.image(poster, use_container_width=True)

    with right:
        st.markdown(f"## {movie.get('title')}")
        lang = str(movie.get('original_language', '')).upper()
        st.markdown(f"**Released:** {movie.get('release_date')} | **Language:** {lang} | **TMDb Rating:** ⭐ {movie.get('vote_average')}")

        director = movie.get('director')
        if director and director != "N/A":
            st.markdown("**Director:**")
            if st.button(f"🎬 {director}", key=f"dir_{tmdb_id}"):
                st.session_state.search_query = director
                st.session_state.search_type = "director"
                set_view("search_results")

        cast_raw = movie.get('cast')
        if cast_raw and cast_raw != "N/A":
            st.markdown("**Cast:**")
            cast_list = [c.strip() for c in str(cast_raw).split(",") if c.strip()][:5]
            cast_cols = st.columns(len(cast_list))
            for c_idx, actor in enumerate(cast_list):
                if cast_cols[c_idx].button(f"👤 {actor}", key=f"cast_{tmdb_id}_{c_idx}"):
                    st.session_state.search_query = actor
                    st.session_state.search_type = "cast"
                    set_view("search_results")

        # Overview moved directly under cast — no tracker interruption
        if movie.get('overview'):
            st.markdown(
                f"<div style='font-size:1.05em; color:#ddd; margin-top:15px;'>{movie.get('overview')}</div>",
                unsafe_allow_html=True
            )

    # Tracker section below the fold — cleaner reading flow
    st.markdown("---")
    st.markdown("### 📝 Track This")
    with st.container(border=True):
        status_map = {
            "📅 Plan to Watch": "plan_to_watch",
            "👀 Watching": "watching",
            "✅ Completed": "completed",
            "🛑 Dropped": "dropped"
        }
        selected_status_ui = st.selectbox("Current Status", options=list(status_map.keys()), index=0)
        actual_status = status_map[selected_status_ui]

        star_options = ["Unrated"] + ["⭐" * i for i in range(1, 6)]
        star_map = {"Unrated": 0, "⭐": 1, "⭐⭐": 2, "⭐⭐⭐": 3, "⭐⭐⭐⭐": 4, "⭐⭐⭐⭐⭐": 5}

        col_r, col_label = st.columns([3, 1])
        with col_r:
            selected_star = st.radio("Your Rating", options=star_options, index=0, horizontal=True)
        user_rating = star_map[selected_star]
        with col_label:
            if user_rating > 0:
                st.markdown(
                    f"<div style='padding-top:32px; color:#aaa; font-size:0.85em;'>{STAR_LABELS[user_rating]}</div>",
                    unsafe_allow_html=True
                )

        if st.button("Save to Tracker", key=f"save_{tmdb_id}", type="primary"):
            save_tracker_for_selected(tmdb_id, user_rating, actual_status)

    # Similar titles
    st.markdown("---")
    st.markdown("### 🍿 More Like This")
    with st.spinner("Finding similar titles..."):
        similar_df = load_similar_movies(movie, limit=8)
    if not similar_df.empty:
        sim_cols = st.columns(4, gap="large")
        for s_idx, (_, s_row) in enumerate(similar_df.head(8).iterrows()):
            render_media_card(s_row, sim_cols[s_idx % 4], f"sim_{tmdb_id}_{s_idx}")

def render_tracker_mini_card(row, col, target_uid, idx_prefix):
    poster = f"https://image.tmdb.org/t/p/w500{row['poster_path']}" if row.get('poster_path') else PLACEHOLDER_POSTER
    rating = int(row['rating']) if row.get('rating') and row['rating'] > 0 else 0

    with col.container(border=True):
        st.image(poster, use_container_width=True)
        st.markdown(
            f"<div class='title-card'>{row.get('title', 'Unknown')}</div>",
            unsafe_allow_html=True
        )
        if rating > 0:
            st.markdown(
                f"{'⭐' * rating} <span class='star-label'>{STAR_LABELS[rating]}</span>",
                unsafe_allow_html=True
            )
        else:
            st.markdown("<div style='height: 1.5em;'></div>", unsafe_allow_html=True)

        unique_key = f"trk_{target_uid}_{row.get('tmdb_id')}_{idx_prefix}"
        st.button("View", key=unique_key, on_click=set_selected_movie, args=(row.get('tmdb_id'),), use_container_width=True)

# --- TRACKER PAGE ---
def tracker_page():
    uid = st.session_state.user_id
    fname = st.session_state.friend_name
    friend_id, _ = get_cached_friend_id()

    st.header("📋 2-Player Watchlist Hub")

    with st.spinner("Loading tracker..."):
        my_df = sanitize_df(get_tracker_data(uid))
        f_df = sanitize_df(get_tracker_data(friend_id)) if friend_id else pd.DataFrame()

    # Mutual watchlist
    my_ptw = my_df[my_df['status'] == 'plan_to_watch'] if not my_df.empty and 'status' in my_df.columns else pd.DataFrame()
    f_ptw = f_df[f_df['status'] == 'plan_to_watch'] if not f_df.empty and 'status' in f_df.columns else pd.DataFrame()

    if not my_ptw.empty and not f_ptw.empty:
        mutual_ids = set(my_ptw['tmdb_id']).intersection(set(f_ptw['tmdb_id']))
        if mutual_ids:
            st.markdown("### 🤝 Mutual Watchlist")
            st.info(f"**You and {fname} both want to watch these.** 🍿")
            mutual_df = my_ptw[my_ptw['tmdb_id'].isin(mutual_ids)]
            cols = st.columns(4)
            for idx, row in mutual_df.reset_index().iterrows():
                render_tracker_mini_card(row, cols[idx % 4], uid, f"mut_{idx}")
            st.markdown("---")

    tabs = st.tabs([f"👤 My Tracker", f"👥 {fname}'s Tracker"] if friend_id else ["My Tracker"])

    def render_user_tracker(target_uid, target_df):
        if target_df.empty:
            st.info("No media tracked yet. Browse media to add them here!")
            return

        try:
            playlists = supabase.table("playlists").select("id, name").eq("user_id", target_uid).execute().data or []
            p_ids = [p['id'] for p in playlists]
            playlist_items = supabase.table("playlist_items").select("playlist_id, tmdb_id").in_("playlist_id", p_ids).execute().data if p_ids else []
        except Exception as e:
            logger.warning("render_user_tracker playlist fetch failed: %s", e)
            playlists, playlist_items = [], []

        pl_name_map = {p['id']: p['name'] for p in playlists}
        pl_items_map = {name: [] for name in pl_name_map.values()}
        tracked_in_playlists = set()

        for pi in playlist_items:
            name = pl_name_map.get(pi['playlist_id'])
            if name:
                pl_items_map[name].append(pi['tmdb_id'])
                tracked_in_playlists.add(pi['tmdb_id'])

        status_tabs = st.tabs(["👀 Watching", "📅 Plan to Watch", "✅ Completed", "🛑 Dropped"])
        statuses = ["watching", "plan_to_watch", "completed", "dropped"]

        for i, status in enumerate(statuses):
            with status_tabs[i]:
                status_df = target_df[target_df['status'] == status] if 'status' in target_df.columns else pd.DataFrame()
                if status_df.empty:
                    st.write(f"Nothing in '{status.replace('_', ' ').title()}' yet.")
                    continue

                if status == "plan_to_watch":
                    for pl_name, t_ids in pl_items_map.items():
                        pl_df = status_df[status_df['tmdb_id'].isin(t_ids)]
                        if not pl_df.empty:
                            st.markdown(f"#### 📂 {pl_name}")
                            first_4 = pl_df.head(4)
                            cols = st.columns(4)
                            for idx, row in first_4.reset_index().iterrows():
                                render_tracker_mini_card(row, cols[idx], target_uid, f"pl_{pl_name}_{idx}")
                            if len(pl_df) > 4:
                                with st.expander(f"Show remaining {len(pl_df) - 4} items in '{pl_name}'..."):
                                    rest_df = pl_df.iloc[4:]
                                    r_cols = st.columns(4)
                                    for idx, row in rest_df.reset_index().iterrows():
                                        render_tracker_mini_card(row, r_cols[idx % 4], target_uid, f"pl_exp_{pl_name}_{idx}")
                            st.markdown("<hr style='margin-top: 5px; margin-bottom: 20px;'/>", unsafe_allow_html=True)

                    uncat_df = status_df[~status_df['tmdb_id'].isin(tracked_in_playlists)]
                    if not uncat_df.empty:
                        st.markdown("#### 📌 Uncategorized")
                        u_cols = st.columns(4)
                        for idx, row in uncat_df.reset_index().iterrows():
                            render_tracker_mini_card(row, u_cols[idx % 4], target_uid, f"uncat_{idx}")
                else:
                    cols = st.columns(4)
                    for idx, row in status_df.reset_index().iterrows():
                        render_tracker_mini_card(row, cols[idx % 4], target_uid, f"std_{status}_{idx}")

    with tabs[0]:
        render_user_tracker(uid, my_df)
    if friend_id:
        with tabs[1]:
            render_user_tracker(friend_id, f_df)

def genre_page():
    st.header("🎭 Browse by Genre")
    genre_names = list(TMDB_GENRES.values())
    selected_name = st.selectbox("Select a genre:", genre_names)
    if selected_name:
        selected_id = list(TMDB_GENRES.keys())[list(TMDB_GENRES.values()).index(selected_name)]
        with st.spinner(f"Loading {selected_name}..."):
            df = load_movies_by_genre(selected_id)
        render_full_grid(f"Genre: {selected_name}", df)

# --- MAIN APP ROUTER ---
def main():
    if not st.session_state.logged_in:
        login_signup_page()
        return

    # Hydrate friend info once per session
    if st.session_state.friend_id_cache is None:
        get_cached_friend_id()

    fname = st.session_state.friend_name

    with st.sidebar:
        st.title(f"👋 {st.session_state.username}")
        if st.button("🏠 Discover Hub", use_container_width=True, type="primary"):
            set_view("home")
        if st.button("🎭 Browse Genres", use_container_width=True):
            set_view("genre")
        if st.button("🗓️ Upcoming Releases", use_container_width=True):
            set_view("upcoming")
        if st.button("📋 Shared Tracker", use_container_width=True):
            set_view("tracker")

        st.markdown("---")
        st.markdown("**🌍 Language Filter**")
        st.session_state.active_filter = st.selectbox(
            "Select Language",
            ["All", "English", "Korean", "Japanese", "Chinese", "Spanish", "Hindi", "Thai", "Filipino"],
            label_visibility="collapsed"
        )
        st.markdown("---")
        if st.button("Logout", use_container_width=True):
            st.session_state.clear()
            st.rerun()

    # Search bar — only show when not in detail view
    if not st.session_state.selected_tmdb_id:
        with st.form("search_form"):
            c1, c2 = st.columns([8.5, 1.5])
            with c1:
                query = st.text_input(
                    "🔍 Search",
                    value=st.session_state.search_query,
                    label_visibility="collapsed",
                    placeholder="Search by title, cast, or director..."
                )
            with c2:
                if st.form_submit_button("Search", use_container_width=True):
                    if query.strip():
                        st.session_state.search_query = query
                        st.session_state.search_type = "general"
                        set_view("search_results")

    # --- ROUTING ---
    if st.session_state.selected_tmdb_id:
        if st.button("⬅️ Back"):
            if len(st.session_state.history_stack) > 1:
                st.session_state.history_stack.pop()
                st.session_state.selected_tmdb_id = st.session_state.history_stack[-1]
            else:
                st.session_state.history_stack.clear()
                st.session_state.selected_tmdb_id = None
            st.rerun()
        show_details_pane(st.session_state.selected_tmdb_id)

    elif st.session_state.view == "search_results" and st.session_state.search_query:
        if st.button("Clear Search"):
            st.session_state.search_query = ""
            st.session_state.search_type = "general"
            set_view("home")

        search_title = f"🔍 Results for '{st.session_state.search_query}'"
        if st.session_state.search_type == "cast":
            search_title = f"👤 Media featuring '{st.session_state.search_query}'"
        elif st.session_state.search_type == "director":
            search_title = f"🎬 Directed by '{st.session_state.search_query}'"

        with st.spinner("Searching..."):
            results_df = load_movies_search(st.session_state.search_query, search_type=st.session_state.search_type)
        render_full_grid(search_title, results_df)

    elif st.session_state.view == "home":
        with st.spinner("Loading recommendations..."):
            rec_df = get_recommendations(st.session_state.user_id)

        render_horizontal_row("🍿 Recommended Movies", rec_df[rec_df["media_type"] == "movie"], "movies")
        render_horizontal_row(
            "📺 Recommended Series",
            rec_df[(rec_df["media_type"] == "tv") & (~rec_df["original_language"].isin(["ja", "ko", "zh"]))],
            "series"
        )
        render_horizontal_row(
            "🌏 Recommended Dramas",
            rec_df[(rec_df["media_type"] == "tv") & (rec_df["original_language"].isin(["ko", "zh", "th", "tl", "es"]))],
            "drama"
        )
        render_horizontal_row(
            "⚔️ Recommended Anime",
            rec_df[rec_df["original_language"] == "ja"],
            "anime"
        )
        render_horizontal_row(f"👑 {fname}'s Masterpieces", load_friend_must_watch(st.session_state.user_id), "must_watch")
        render_horizontal_row("✨ Discover (Hidden Gems)", load_discover_media(), "discover")
        render_horizontal_row("🗓️ Upcoming Releases", load_filtered_media("upcoming"), "upcoming")

    elif st.session_state.view == "movies":
        with st.spinner("Loading movies..."):
            render_full_grid("🍿 Movies", load_filtered_media("movies", language=st.session_state.active_filter))
    elif st.session_state.view == "series":
        with st.spinner("Loading series..."):
            render_full_grid("📺 Series", load_filtered_media("series", language=st.session_state.active_filter))
    elif st.session_state.view == "drama":
        with st.spinner("Loading dramas..."):
            render_full_grid("🌏 Dramas", load_filtered_media("drama", language=st.session_state.active_filter))
    elif st.session_state.view == "anime":
        with st.spinner("Loading anime..."):
            render_full_grid("⚔️ Anime", load_filtered_media("anime", language=st.session_state.active_filter))
    elif st.session_state.view == "discover":
        with st.spinner("Finding hidden gems..."):
            render_full_grid("✨ Discover (Hidden Gems)", load_discover_media())
    elif st.session_state.view == "upcoming":
        with st.spinner("Loading upcoming releases..."):
            render_full_grid("🗓️ Upcoming Releases", load_filtered_media("upcoming", language=st.session_state.active_filter))
    elif st.session_state.view == "must_watch":
        with st.spinner(f"Loading {fname}'s picks..."):
            render_full_grid(f"👑 {fname}'s Masterpieces", load_friend_must_watch(st.session_state.user_id))
    elif st.session_state.view == "genre":
        genre_page()
    elif st.session_state.view == "tracker":
        tracker_page()

if __name__ == "__main__":
    main()