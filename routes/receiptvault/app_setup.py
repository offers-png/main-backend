"""
Registers every ReceiptKept router on the FastAPI app in one place, so
app.py and the test suite build exactly the same set of routes.
"""

from fastapi import Depends, FastAPI

from routes.receiptvault.routes import receipt_routes, require_active_subscription, plan_feature
from routes.receiptvault.invoice_routes import invoice_routes
from routes.receiptvault.inventory_routes import inventory_routes
from routes.receiptvault.mileage_routes import mileage_routes
from routes.receiptvault.team_routes import team_routes
from routes.receiptvault.billing_routes import billing_routes
from routes.receiptvault.money_routes import money_routes
from routes.receiptvault.pnl_routes import pnl_routes
from routes.receiptvault.dropbox_routes import dropbox_routes, tax_payments_routes, feedback_routes
from routes.receiptvault.store_routes import store_routes
from routes.receiptvault.bank_routes import bank_routes
from routes.receiptvault.statement_routes import statement_routes
from routes.receiptvault.connect_routes import connect_routes
from routes.receiptvault.inbound_email_routes import inbound_routes, inbound_webhook_routes
from routes.receiptvault.export_routes import export_routes
from routes.receiptvault.account_routes import account_routes


def register_receiptkept(app: FastAPI):
    # Every store-scoped router: view-only stores can be read, not changed.
    gated = [Depends(require_active_subscription)]
    # Paid features: invoices/inventory/team stay VIEWABLE after a
    # downgrade (data is never hidden or deleted) but changes need the plan.
    def paid(feature, writes_only=True):
        return gated + [Depends(plan_feature(feature, writes_only=writes_only))]

    app.include_router(receipt_routes)
    app.include_router(invoice_routes, dependencies=paid("invoices"))
    app.include_router(inventory_routes, dependencies=paid("inventory"))
    app.include_router(mileage_routes, dependencies=gated)
    app.include_router(team_routes, dependencies=paid("team"))
    app.include_router(billing_routes)
    app.include_router(store_routes)
    app.include_router(money_routes, dependencies=gated)
    app.include_router(pnl_routes, dependencies=gated)
    app.include_router(dropbox_routes)
    app.include_router(tax_payments_routes, dependencies=paid("tax", writes_only=False))
    # Matching, sorting and the Personal button are Basic and up; the list
    # itself stays viewable after a downgrade.
    app.include_router(bank_routes, dependencies=paid("auto_categorize"))
    app.include_router(statement_routes, dependencies=paid("statement_upload"))
    app.include_router(connect_routes, dependencies=paid("bank_connection"))
    app.include_router(inbound_routes, dependencies=paid("email_forwarding", writes_only=False))
    app.include_router(inbound_webhook_routes)  # Resend, signed
    app.include_router(export_routes, dependencies=gated)  # CSV export: every plan
    # Export my data / Delete my account: always allowed, even on a view-only store
    app.include_router(account_routes)
    # Feedback is never gated — it must work for everyone.
    app.include_router(feedback_routes)
