import os
import random

class CheckerConfig:
    def __init__(self):
        # Load from environment variables for Railway deployment
        self.PROXY_LIST = os.getenv("PROXY_LIST", "").split(",") if os.getenv("PROXY_LIST") else []

        self.PRODUCT_URLS = os.getenv("PRODUCT_URLS", "https://kwpremiumdigitalassets.com/products/sony-fx3-slog3-footage-car-rig").split(",")

        self.IS_SHIPPABLE = os.getenv("IS_SHIPPABLE", "false").lower() == "true"

        self.DEFAULT_EMAIL = os.getenv("DEFAULT_EMAIL", None)

        self.CONCURRENCY_LIMIT = int(os.getenv("CONCURRENCY_LIMIT", "5"))

        self.BOT_TOKEN = os.getenv("BOT_TOKEN", "")

        self.ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

    def _get_rnd_product_url(self):
        url = random.choice(self.PRODUCT_URLS)
        return url
