from lumibot.entities import Asset
from bot.strategy import DebbieLaSMC

CRYPTO_WATCHLIST = ["BTC", "ETH", "SOL", "LINK", "LTC", "BCH"]


class DebbieLaCrypto(DebbieLaSMC):
    """
    Crypto variant of DebbieLaSMC running 24/7 on Alpaca.
    Same institutional SMC logic, crypto asset type, no EOD close.
    """

    def initialize(self, symbols: list = None, cash_at_risk: float = 0.02,
                   timeframe_htf: str = "4 hours", timeframe_ltf: str = "15 minutes"):
        # Crypto is more volatile — default to 2% total risk (0.33% per symbol)
        super().initialize(
            symbols=symbols or CRYPTO_WATCHLIST,
            cash_at_risk=cash_at_risk,
            timeframe_htf=timeframe_htf,
            timeframe_ltf=timeframe_ltf,
        )
        self.set_market("24/7")  # crypto never sleeps

    def _make_asset(self, symbol):
        return Asset(symbol, asset_type=Asset.AssetType.CRYPTO)

    def before_closing_bell(self):
        # Crypto trades 24/7 — no EOD forced close
        pass
