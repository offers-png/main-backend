from fastapi import APIRouter, HTTPException, Depends, Header
from typing import Optional
import os
from supabase import create_client, Client

receipt_routes = APIRouter(prefix="/api", tags=["receiptvault"])

# Lazy Supabase initialization to prevent module-level crashes
_supabase_client = None

def get_supabase() -> Client:
    """Lazy initialize Supabase client"""
    global _supabase_client
    if _supabase_client is None:
        supabase_url = os.getenv("SUPABASE_URL", "https://wzcuzyouymauokijaqjk.supabase.co")
        # Try multiple env var names
        supabase_key = (
            os.getenv("SUPABASE_ANON_KEY") or 
            os.getenv("SUPABASE_KEY") or 
            os.getenv("SUPABASE_SERVICE_KEY")
        )
        
        if not supabase_key:
            raise HTTPException(
                status_code=500,
                detail="Server configuration error: Supabase key not found in environment"
            )
        
        _supabase_client = create_client(supabase_url, supabase_key)
    
    return _supabase_client

async def get_current_user(authorization: Optional[str] = Header(None)):
    """Extract user from Authorization header"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")
    
    token = authorization.replace("Bearer ", "")
    
    try:
        supabase = get_supabase()
        # Verify the token with Supabase
        user = supabase.auth.get_user(token)
        return user
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")

@receipt_routes.get("/user")
async def get_user(current_user = Depends(get_current_user)):
    """Get current authenticated user details"""
    return {
        "id": current_user.user.id,
        "email": current_user.user.email,
        "user_metadata": current_user.user.user_metadata,
        "created_at": current_user.user.created_at
    }
