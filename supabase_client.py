import os
from supabase import create_client, Client
import streamlit as st

# In Streamlit, you should store these in .streamlit/secrets.toml
# For now, you can paste them directly if you are testing locally.
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

@st.cache_resource
def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

supabase = get_supabase()
