from supabase import create_client, Client
from ..utils.config import settings

class SupabaseService:
    def __init__(self):
        self.client: Client = create_client(settings.supabase_url, settings.supabase_key)

    def table(self, table_name: str):
        return self.client.table(table_name)

supabase_service = SupabaseService()
