"""
Registers every ReceiptKept router on the FastAPI app in one place, so
app.py and the test suite build exactly the same set of routes.
"""

from fastapi import Depends, FastAPI

from routes.receiptvault.routes import receipt_routes, require_active_subscription
from routes.receiptvault.invoice_routes import invoice_routes
from routes.receiptvault.inventory_routes import inventory_routes
from routes.receiptvault.mileage_routes import mileage_routes
from routes.receiptvault.team_routes import team_routes
from routes.receiptvault.billing_routes import billing_routes
from routes.receiptvault.money_routes import money_routes
from routes.receiptvault.pnl_routes import pnl_routes
from routes.receiptvault.dropbox_routes import dropbox_routes, tax_payments_routes, feedback_routes
from routes.receiptvault.store_routes import store_routes


def register_receiptkept(app: FastAPI):
    gated = [Depends(require_active_subscription)]
    app.include_router(receipt_routes)
    app.include_router(invoice_routes, dependencies=gated)
    app.include_router(inventory_routes, dependencies=gated)
    app.include_router(mileage_routes, dependencies=gated)
    app.include_router(team_routes, dependencies=gated)
    app.include_router(billing_routes)
    app.include_router(store_routes)
    app.include_router(money_routes, dependencies=gated)
    app.include_router(pnl_routes, dependencies=gated)
    app.include_router(dropbox_routes)
    app.include_router(tax_payments_routes, dependencies=gated)
    # Feedback is never gated — it must work for everyone.
    app.include_router(feedback_routes)
