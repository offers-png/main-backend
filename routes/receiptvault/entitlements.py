"""
ReceiptKept plan entitlements. (Phase 1 placeholder: every owner may add
stores; plan limits and add-on billing arrive with the new plans.)
"""


def check_can_add_store(supabase, owner_user_id: str, owned_count: int):
    return None


async def sync_store_addon_quantity(supabase, owner_user_id: str):
    return None
