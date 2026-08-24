import os
from dotenv import load_dotenv

load_dotenv()

# --- API base URLs ---
RESEARCH_CLOUD_TOKEN = os.getenv("RESEARCH_CLOUD_TOKEN")
CATALOG_BASE_URL = os.getenv("CATALOG_BASE_URL", "https://gw.live.surfresearchcloud.nl/v1/application-market/")
USER_BASE_URL = os.getenv("USER_BASE_URL", "https://gw.live.surfresearchcloud.nl/v1/user/")
WALLET_BASE_URL = os.getenv("WALLET_BASE_URL", "https://gw.live.surfresearchcloud.nl/v1/wallet/")
WORKSPACE_BASE_URL = os.getenv("WORKSPACE_BASE_URL", "https://gw.live.surfresearchcloud.nl/v1/workspace/")

# --- Collaboration (CO) is determined by the user ---
CO_NAME = os.getenv("CO_NAME")

# --- Resource selection by name ---
WALLET_NAME = os.getenv("WALLET_NAME")
CATALOG_ITEM_NAME = os.getenv("CATALOG_ITEM_NAME")       # Application Offering name (e.g. "Ubuntu Desktop")
CLOUD_NAME = os.getenv("CLOUD_NAME", "SURF HPC Cloud")   # Subscription name (cloud provider)
OS_FLAVOUR_NAME = os.getenv("OS_FLAVOUR_NAME", "Ubuntu 24.04")           # e.g. "Ubuntu 24.04"
SIZE_FLAVOUR_NAME = os.getenv("SIZE_FLAVOUR_NAME", "1 Core - 8 GB RAM")        # e.g. "1 Core - 8 GB RAM"
NETWORK_NAME = os.getenv("NETWORK_NAME")  # Private-network application name/offer (optional; first found is used if unset)

# --- Workspace settings ---
HOST_NAME_BASE = os.getenv("HOST_NAME_BASE", "ws")
WORKSPACE_NAME = os.getenv("WORKSPACE_NAME")
WORKSPACE_DESCRIPTION = os.getenv("WORKSPACE_DESCRIPTION", "")
WORKSPACE_END_TIME = os.getenv("WORKSPACE_END_TIME")     # Optional ISO 8601 end time; defaults to now + 3 days when unset

# --- Optional: attach existing resources by ID ---
STORAGE_IDS = [s for s in os.getenv("STORAGE_IDS", "").split(",") if s]
NETWORK_IDS = [n for n in os.getenv("NETWORK_IDS", "").split(",") if n]
IP_IDS = [i for i in os.getenv("IP_IDS", "").split(",") if i]
DATASET_NAMES = [d for d in os.getenv("DATASET_NAMES", "").split(",") if d]
DATASET_IDS = [d for d in os.getenv("DATASET_IDS", "").split(",") if d]
