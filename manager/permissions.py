"""Permission catalogue and the system roles seeded on install.

Permissions are code constants, checked by the backend on every manager request
(``manager.permissions.has_perm``). The frontend never decides access; it only
hides controls it knows the backend will refuse.
"""

SUPER_ADMIN = "super-admin"
GENERAL_MANAGER = "general-manager"
FINANCE_MANAGER = "finance-manager"
OPERATIONS_MANAGER = "operations-manager"
SUPPORT_AGENT = "support-agent"
MARKETING_MANAGER = "marketing-manager"
DATA_ANALYST = "data-analyst"
RISK_OFFICER = "risk-officer"
SYSTEM_ADMIN = "system-admin"

# code: (human label, category)
PERMISSIONS = {
    # Dashboards and analysis
    "analytics.view": ("View management dashboards", "Analytics"),
    "analytics.financial": ("View financial intelligence", "Analytics"),
    "analytics.customer": ("View customer intelligence", "Analytics"),
    "analytics.artisan": ("View artisan intelligence", "Analytics"),
    "analytics.build": ("Build and save Data Explorer reports", "Analytics"),
    "analytics.snapshot": ("Inspect snapshot engine state", "Analytics"),
    # Configuration
    "config.view": ("View platform configuration", "Configuration"),
    "config.finance": ("Change fees, commissions and settlement rules", "Configuration"),
    "config.operational": ("Change booking and notification rules", "Configuration"),
    "config.approve": ("Approve configuration changes", "Configuration"),
    "config.snapshot_interval": ("Change snapshot intervals", "Configuration"),
    # Money movement
    "refund.view": ("View refunds", "Finance"),
    "refund.request": ("Request a refund", "Finance"),
    "refund.approve": ("Approve a refund", "Finance"),
    "payout.view": ("View payouts", "Finance"),
    "payout.approve": ("Approve or release a payout", "Finance"),
    "finance.reconcile": ("Run reconciliation", "Finance"),
    "finance.adjust": ("Post a controlled financial adjustment", "Finance"),
    # People
    "customer.view": ("View customer records", "People"),
    "customer.sensitive": ("View sensitive customer information", "People"),
    "customer.manage": ("Suspend or restore a customer account", "People"),
    "artisan.view": ("View artisan records", "People"),
    "artisan.sensitive": ("View identity verification information", "People"),
    "artisan.manage": ("Suspend or restore an artisan account", "People"),
    # Support and risk
    "support.view": ("View support cases", "Support"),
    "support.manage": ("Assign, respond to and resolve cases", "Support"),
    "dispute.view": ("View disputes", "Support"),
    "dispute.manage": ("Manage disputes and evidence", "Support"),
    "risk.view": ("View risk signals", "Risk"),
    "risk.manage": ("Review risk signals and record decisions", "Risk"),
    # Marketing
    "campaign.view": ("View campaigns", "Marketing"),
    "campaign.create": ("Create campaigns", "Marketing"),
    "campaign.approve": ("Approve campaigns", "Marketing"),
    "campaign.send": ("Dispatch approved campaigns", "Marketing"),
    # Governance
    "staff.view": ("View staff accounts and roles", "Governance"),
    "staff.manage": ("Create staff, assign roles and permissions", "Governance"),
    "audit.view": ("View audit history", "Governance"),
    "export.data": ("Export data", "Governance"),
    "export.sensitive": ("Export sensitive or contact data", "Governance"),
    "privacy.view": ("View privacy requests and retention policy", "Governance"),
    "privacy.manage": ("Change retention policy and process data requests", "Governance"),
    "system.health": ("View technical health", "Governance"),
    "system.manage": ("Change technical or security settings", "Governance"),
}

# Operations that require a recent password + second factor, single use.
STEP_UP_SCOPES = {
    "config:finance": "Changing a financial rule",
    "config:approve": "Approving a financial rule change",
    "refund:approve": "Approving a refund",
    "payout:approve": "Approving a payout",
    "staff:manage": "Changing staff permissions",
    "privacy:manage": "Changing privacy or retention settings",
    "export:sensitive": "Exporting sensitive data",
    "system:manage": "Changing security settings",
}

SYSTEM_ROLES = {
    SUPER_ADMIN: {
        "name": "Super Administrator",
        "description": "Unrestricted access to every console area.",
        "permissions": sorted(PERMISSIONS),
    },
    GENERAL_MANAGER: {
        "name": "General Manager",
        "description": "Runs the marketplace: all analytics, operations, support, risk and staff visibility.",
        "permissions": sorted(
            set(PERMISSIONS)
            - {"config.approve", "staff.manage", "privacy.manage", "export.sensitive", "system.manage",
               "finance.adjust", "analytics.build"}
        ),
    },
    FINANCE_MANAGER: {
        "name": "Finance Manager",
        "description": "Financial configuration, reconciliation, refunds and payouts.",
        "permissions": [
            "analytics.view", "analytics.financial", "config.view", "config.finance",
            "refund.view", "refund.request", "refund.approve", "payout.view", "payout.approve",
            "finance.reconcile", "finance.adjust", "audit.view", "export.data",
            "customer.view", "artisan.view", "support.view", "dispute.view",
        ],
    },
    OPERATIONS_MANAGER: {
        "name": "Operations Manager",
        "description": "Booking lifecycle, service performance, support and disputes.",
        "permissions": [
            "analytics.view", "analytics.financial", "analytics.customer", "analytics.artisan",
            "analytics.snapshot", "config.view", "config.operational", "customer.view", "customer.manage",
            "artisan.view", "support.view", "support.manage", "dispute.view", "dispute.manage",
            "risk.view", "audit.view", "export.data", "campaign.view",
        ],
    },
    SUPPORT_AGENT: {
        "name": "Customer Support Agent",
        "description": "Case handling only. No financial configuration, payouts or exports.",
        "permissions": [
            "support.view", "support.manage", "dispute.view", "customer.view", "artisan.view",
            "refund.view", "refund.request",
        ],
    },
    MARKETING_MANAGER: {
        "name": "Marketing Manager",
        "description": "Campaigns, audiences and marketing performance, with aggregate data only.",
        "permissions": [
            "analytics.view", "analytics.build", "campaign.view", "campaign.create", "campaign.send",
            "export.data",
        ],
    },
    DATA_ANALYST: {
        "name": "Data & Analytics Officer",
        "description": "Aggregated analytics, saved reports and governed exports.",
        "permissions": [
            "analytics.view", "analytics.financial", "analytics.customer", "analytics.artisan",
            "analytics.build", "analytics.snapshot", "export.data", "audit.view", "campaign.view",
        ],
    },
    RISK_OFFICER: {
        "name": "Risk & Compliance Officer",
        "description": "Risk signals, disputes and audit review.",
        "permissions": [
            "analytics.view", "risk.view", "risk.manage", "dispute.view", "dispute.manage",
            "audit.view", "customer.view", "customer.sensitive", "artisan.view", "export.data",
            "privacy.view",
        ],
    },
    SYSTEM_ADMIN: {
        "name": "System Administrator",
        "description": "Technical health, staff accounts and security settings.",
        "permissions": [
            "analytics.view", "analytics.snapshot", "system.health", "system.manage",
            "staff.view", "staff.manage", "audit.view", "config.view", "config.snapshot_interval",
        ],
    },
}
