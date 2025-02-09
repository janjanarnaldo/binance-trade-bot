from datetime import datetime
from typing import Dict, List

from sqlalchemy.orm import Session

from .binance_api_manager import BinanceAPIManager
from .config import Config
from .database import Database
from .logger import Logger
from .models import Coin, CoinValue, Pair
from .utils import get_market_ticker_price_from_list


class AutoTrader:
    def __init__(self, binance_manager: BinanceAPIManager, database: Database, logger: Logger, config: Config):
        self.manager = binance_manager
        self.db = database
        self.logger = logger
        self.config = config

    def transaction_through_bridge(self, pair: Pair, all_tickers):
        """
        Jump from the source coin to the destination coin through bridge coin
        """
        if self.manager.sell_alt(pair.from_coin, self.config.BRIDGE) is None:
            self.logger.info("Couldn't sell, going back to scouting mode...")
            return False

        result = self.manager.buy_alt(pair.to_coin, self.config.BRIDGE, all_tickers)
        if result is not None:
            self.update_trade_threshold(pair.to_coin, float(result["price"]), all_tickers)
            return True
        self.logger.info("Couldn't buy, going back to scouting mode...")
        return False

    def update_trade_threshold(self, current_coin: Coin, current_coin_price: float, all_tickers):
        """
        Update all the coins with the threshold of buying the current held coin
        """

        if current_coin_price is None:
            self.logger.info("Skipping update... current coin {} not found".format(current_coin + self.config.BRIDGE))
            return

        session: Session
        with self.db.db_session() as session:
            for pair in session.query(Pair).filter(Pair.to_coin == current_coin):
                from_coin_price = get_market_ticker_price_from_list(all_tickers, pair.from_coin + self.config.BRIDGE)

                if from_coin_price is None:
                    self.logger.info(
                        "Skipping update for coin {} not found".format(pair.from_coin + self.config.BRIDGE)
                    )
                    continue

                pair.ratio = from_coin_price / current_coin_price

    def initialize_trade_thresholds(self):
        """
        Initialize the buying threshold of all the coins for trading between them
        """
        all_tickers = self.manager.get_all_market_tickers()

        session: Session
        with self.db.db_session() as session:
            for pair in session.query(Pair).filter(Pair.ratio.is_(None)).all():
                if not pair.from_coin.enabled or not pair.to_coin.enabled:
                    continue
                self.logger.info(f"Initializing {pair.from_coin} vs {pair.to_coin}")

                from_coin_price = get_market_ticker_price_from_list(all_tickers, pair.from_coin + self.config.BRIDGE)
                if from_coin_price is None:
                    self.logger.info(
                        "Skipping initializing {}, symbol not found".format(pair.from_coin + self.config.BRIDGE)
                    )
                    continue

                to_coin_price = get_market_ticker_price_from_list(all_tickers, pair.to_coin + self.config.BRIDGE)
                if to_coin_price is None:
                    self.logger.info(
                        "Skipping initializing {}, symbol not found".format(pair.to_coin + self.config.BRIDGE)
                    )
                    continue

                pair.ratio = from_coin_price / to_coin_price

    def scout(self):
        """
        Scout for potential jumps from the current coin to another coin
        """
        all_tickers = self.manager.get_all_market_tickers()

        for coin in self.db.get_coins():
            current_coin_balance = self.manager.get_currency_balance(coin.symbol)
            coin_price = get_market_ticker_price_from_list(all_tickers, coin + self.config.BRIDGE)

            if coin_price is None:
                self.logger.info("Skipping scouting... current coin {} not found".format(coin + self.config.BRIDGE))
                return

            if coin_price * current_coin_balance < self.manager.get_min_notional(
                coin.symbol, self.config.BRIDGE.symbol
            ):
                continue

            # Display on the console, the current coin+Bridge, so users can see *some* activity and not think the bot
            # has stopped. Not logging though to reduce log size.
            self.logger.info(f"Scouting for best trades. Current ticker: {coin + self.config.BRIDGE} ")

            # save ratio so we can pick the best option, not necessarily the first
            ratio_dict = self._get_ratios(coin, coin_price, all_tickers)

            # keep only ratios bigger than zero
            ratio_dict = {k: v for k, v in ratio_dict.items() if v > 0}

            # if we have any viable options, pick the one with the biggest ratio
            if ratio_dict:
                best_pair = max(ratio_dict, key=ratio_dict.get)
                self.logger.info(f"Will be jumping from {coin} to {best_pair.to_coin_id}")
                self.transaction_through_bridge(best_pair, all_tickers)

        self.bridge_scout()

    def _get_ratios(self, coin: Coin, coin_price, all_tickers):
        ratio_dict: Dict[Pair, float] = {}

        for pair in self.db.get_pairs_from(coin):
            optional_coin_price = get_market_ticker_price_from_list(all_tickers, pair.to_coin + self.config.BRIDGE)

            if optional_coin_price is None:
                self.logger.info(
                    "Skipping scouting... optional coin {} not found".format(pair.to_coin + self.config.BRIDGE)
                )
                continue

            self.db.log_scout(pair, pair.ratio, coin_price, optional_coin_price)

            # Obtain (current coin)/(optional coin)
            coin_opt_coin_ratio = coin_price / optional_coin_price

            ratio_dict[pair] = (
                coin_opt_coin_ratio
                - self.config.SCOUT_TRANSACTION_FEE * self.config.SCOUT_MULTIPLIER * coin_opt_coin_ratio
            ) - pair.ratio
        return ratio_dict

    def bridge_scout(self):
        bridge_balance = self.manager.get_currency_balance(self.config.BRIDGE.symbol)
        all_tickers = self.manager.get_all_market_tickers()

        for coin in self.db.get_coins():
            current_coin_price = get_market_ticker_price_from_list(all_tickers, coin + self.config.BRIDGE)

            if current_coin_price is None:
                return

            ratio_dict = self._get_ratios(coin, current_coin_price, all_tickers)
            if not any(v > 0 for v in ratio_dict.values()):
                # There will only be one coin where all the ratios are negative. When we find it, buy it if we can
                if bridge_balance > self.manager.get_min_notional(coin.symbol, self.config.BRIDGE.symbol):
                    self.logger.info(f"Will be purchasing {coin} using bridge coin")
                    self.manager.buy_alt(coin, self.config.BRIDGE, all_tickers)
                return

    def update_values(self):
        """
        Log current value state of all altcoin balances against BTC and USDT in DB.
        """
        all_ticker_values = self.manager.get_all_market_tickers()

        now = datetime.now()

        session: Session
        with self.db.db_session() as session:
            coins: List[Coin] = session.query(Coin).all()
            for coin in coins:
                balance = self.manager.get_currency_balance(coin.symbol)
                if balance == 0:
                    continue
                usd_value = get_market_ticker_price_from_list(all_ticker_values, coin + "USDT")
                btc_value = get_market_ticker_price_from_list(all_ticker_values, coin + "BTC")
                cv = CoinValue(coin, balance, usd_value, btc_value, datetime=now)
                session.add(cv)
                self.db.send_update(cv)
