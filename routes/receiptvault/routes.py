from fastapi import APIRouter, HTTPException, Depends, Header
from typing import Optional
import os
from supabase import create_client, Client

router = APIRouter(prefix="/api", tags=["receiptvault"])

# Initialize Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://aws-0-us-west-2.pooler.supabase.com:6543")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

async def get_current_user(authorization: Optional[str] = Header(None)):
    """Extract user from Authorization header"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")
    
    token = authorization.replace("Bearer ", "")
    
    try:
        # Verify the token with Supabase
        user = supabase.auth.get_user(token)
        return user
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")

@router.get("/user")
async def get_user(current_user = Depends(get_current_user)):
    """Get current authenticated user details"""
    return {
        "id": current_user.user.id,
        "email": current_user.user.email,
        "user_metadata": current_user.user.user_metadata,
        "created_at": current_user.user.created_at
    }
