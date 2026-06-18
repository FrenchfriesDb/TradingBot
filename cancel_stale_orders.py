import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient

load_dotenv()

client = TradingClient(
    os.getenv("ALPACA_API_KEY"),
    os.getenv("ALPACA_API_SECRET"),
    paper=True,
)

stale_ids = [
    "eef5a32e-af0f-49fa-9d86-fc5ccb4d85ad",
    "372fd2f5-5918-46a0-b387-ae620f2ecbbd",
    "bc26366d-453d-4047-b985-f2971f9083ba",
    "f40477f4-1df4-4fde-9d33-809f07575967",
    "cea85ee0-f74f-40b5-a9ac-a5740c5adfdb",
    "e6e0b8c0-1d28-4056-8069-62b6bd0ce8ab",
]

for oid in stale_ids:
    try:
        client.cancel_order_by_id(oid)
        print(f"Cancelled {oid}")
    except Exception as e:
        print(f"Skip {oid}: {e}")
