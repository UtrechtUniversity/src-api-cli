import os
from dotenv import load_dotenv

load_dotenv()

# --- API base URLs ---
RESEARCH_CLOUD_TOKEN = os.getenv("RESEARCH_CLOUD_TOKEN")
CATALOG_BASE_URL = os.getenv("CATALOG_BASE_URL", "https://gw.live.surfresearchcloud.nl/v1/application-market/")
USER_BASE_URL = os.getenv("USER_BASE_URL", "https://gw.live.surfresearchcloud.nl/v1/user/")
WALLET_BASE_URL = os.getenv("WALLET_BASE_URL", "https://gw.live.surfresearchcloud.nl/v1/wallet/")
WORKSPACE_BASE_URL = os.getenv("WORKSPACE_BASE_URL", "https://gw.live.surfresearchcloud.nl/v1/workspace/")
