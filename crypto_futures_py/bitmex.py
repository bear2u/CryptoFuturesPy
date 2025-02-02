"""
    This module contains an implementation for Bitmex (BitmexExchangeHandler)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import logging
import pandas as pd
import typing
import urllib
import traceback
import urllib.parse
from datetime import datetime, timedelta
import collections

import bitmex
import websocket

from . import AbstractExchangeHandler


class BitmexExchangeHandler(AbstractExchangeHandler):
    domen = "wss://www.bitmex.com/realtime"

    # Generates an API signature.
    # A signature is HMAC_SHA256(secret, verb + path + nonce + data), hex encoded.
    # Verb must be uppercased, url is relative, nonce must be an increasing 64-bit integer
    # and the data, if present, must be JSON without whitespace between keys.
    @staticmethod
    def bitmex_signature(apiSecret, verb, url, nonce, postdict=None):
        """Given an API Secret key and data, create a BitMEX-compatible signature."""
        data = ""
        if postdict:
            # separators remove spaces from json
            # BitMEX expects signatures from JSON built without spaces
            data = json.dumps(postdict, separators=(",", ":"))
        parsedURL = urllib.parse.urlparse(url)
        path = parsedURL.path
        if parsedURL.query:
            path = path + "?" + parsedURL.query
        # print("Computing HMAC: %s" % verb + path + str(nonce) + data)
        message = (verb + path + str(nonce) + data).encode("utf-8")

        signature = hmac.new(
            apiSecret.encode("utf-8"), message, digestmod=hashlib.sha256
        ).hexdigest()
        return signature

    def __init__(self, public_key, private_key):
        super().__init__(public_key, private_key)
        self._client = bitmex.bitmex(
            test=False, api_key=self._public_key, api_secret=self._private_key
        )
        self.logger = logging.Logger(__name__)
        self._order_table: typing.Dict[str, typing.Any] = {}

    @staticmethod
    def get_pairs_list() -> typing.List[str]:
        return (
            bitmex.bitmex(test=False)
            .Instrument.Instrument_getActiveIntervals()
            .result()[0]["symbols"]
        )

    def start_kline_socket(
        self,
        on_update: typing.Callable[[AbstractExchangeHandler.KlineCallback], None],
        candle_type: str,
        pair_name: str,
    ) -> None:
        def __on_message(ws, msg):
            msg = json.loads(msg)
            if "action" in msg and msg["action"] == "insert":
                data = msg["data"][0]
                data = {
                    "time": datetime.strptime(
                        data["timestamp"], "%Y-%m-%dT%H:%M:%S.000Z"
                    ),
                    "open": data["open"],
                    "high": data["high"],
                    "low": data["low"],
                    "close": data["close"],
                    "volume": data["volume"],
                    "final": True,
                    "message": msg,
                }
                on_update(AbstractExchangeHandler.KlineCallback(**data))

        def __on_error(ws, error):
            self.logger.error("Error occured in %s: %s", ws, error)

        def __on_close(ws):
            self.logger.warning("Websocket is restarting, might have lost some data")
            self.start_kline_socket(on_update, candle_type, pair_name)

        self.logger.info("Starting kline websocket")

        ws = websocket.WebSocketApp(
            f"{self.domen}/realtime?subscribe=tradeBin{candle_type}:{pair_name}",
            on_message=__on_message,
            on_error=__on_error,
            on_close=__on_close,
        )
        ws.run_forever()

    def start_price_socket(
        self,
        on_update: typing.Callable[[AbstractExchangeHandler.PriceCallback], None],
        pair_name: str,
    ) -> None:
        def __on_message(ws, msg):
            msg = json.loads(msg)
            if "data" in msg and "lastPriceProtected" in msg["data"][0]:
                price = msg["data"][0]["lastPriceProtected"]
                on_update(AbstractExchangeHandler.PriceCallback(price=price))

        def __on_error(ws, error):
            self.logger.error("Error occured in %s: %s", ws, error)

        def __on_close(ws):
            self.logger.warning("Websocket is restarting, might have lost some data")
            self.start_price_socket(on_update, pair_name)

        self.logger.info("Starting price websocket")

        ws = websocket.WebSocketApp(
            f"{self.domen}/realtime?subscribe=instrument:{pair_name}",
            on_message=__on_message,
            on_error=__on_error,
            on_close=__on_close,
        )
        ws.run_forever()

    def start_user_update_socket(
        self, on_update: typing.Callable[[AbstractExchangeHandler.UserUpdate], None]
    ) -> None:
        self.logger.info("Starting user update socket")

        super().start_user_update_socket(on_update)

        # Switch these comments to use testnet instead.
        # BITMEX_URL = "wss://testnet.bitmex.com"
        BITMEX_URL = self.domen

        VERB = "GET"
        ENDPOINT = "/realtime"

        # These are not real keys - replace them with your keys.
        API_KEY, API_SECRET = self._public_key, self._private_key

        # This is up to you, most use microtime but you may have your own scheme so long as it's increasing
        # and doesn't repeat.
        expires = int(time.time()) + 60 * 60
        # See signature generation reference at https://www.bitmex.com/app/apiKeys
        signature = BitmexExchangeHandler.bitmex_signature(
            API_SECRET, VERB, ENDPOINT, expires
        )

        # Initial connection - BitMEX sends a welcome message.
        ws = websocket.create_connection(BITMEX_URL + ENDPOINT)
        result = ws.recv()

        # Send API Key with signed message.
        request = {"op": "authKeyExpires", "args": [API_KEY, expires, signature]}
        ws.send(json.dumps(request))
        result = ws.recv()

        # Send a request that requires authorization.
        request = {"op": "subscribe", "args": ["order", "position", "margin"]}
        ws.send(json.dumps(request))

        cst: typing.DefaultDict[str, float] = collections.defaultdict(
            lambda: 1, {"XBTUSD": 10 ** -8}
        )

        def __process_order_update(msg):
            for data in msg["data"]:
                if data["orderID"] not in self._order_table:
                    self._order_table[data["orderID"]] = {}

                for key, value in data.items():
                    self._order_table[data["orderID"]][key] = value

            if "action" in msg and (
                msg["action"] == "insert" or msg["action"] == "update"
            ):
                for data in msg["data"]:
                    if "ordStatus" not in data:
                        continue
                    order_data = self._order_table[data["orderID"]]
                    fee_payed = 0

                    if data["ordStatus"] == "Filled":
                        pair_name = order_data["symbol"]
                        corresponding_trades = self._client.Execution.Execution_getTradeHistory(
                            symbol=pair_name,
                            filter=json.dumps({"orderID": data["orderID"]}),
                        ).result()[
                            0
                        ]
                        fee_payed = sum(
                            [
                                trade["execComm"] * cst[pair_name]
                                for trade in corresponding_trades
                            ]
                        )
                    volume_side = 1 if order_data["side"] == "Buy" else -1

                    dic = {
                        "orderID": order_data["orderID"],
                        "client_orderID": order_data["clOrdID"],
                        "symbol": order_data["symbol"],
                        "status": order_data["ordStatus"].upper(),
                        "price": order_data["price"],
                        "average_price": order_data["avgPx"]
                        if "avgPx" in order_data and order_data["avgPx"] is not None
                        else None,
                        "fee": fee_payed,
                        "fee_asset": "XBT",
                        "volume_realized": order_data["cumQty"] * volume_side
                        if "cumQty" in order_data and order_data["cumQty"] is not None
                        else 0,
                        "volume": order_data["orderQty"] * volume_side,
                        "time": datetime.strptime(
                            order_data["timestamp"][:-1] + "000",
                            "%Y-%m-%dT%H:%M:%S.%f",
                        ),
                        "message": order_data,
                    }

                    if dic["status"] == "PARTIALLYFILLED":
                        dic["status"] = "PARTIALLY_FILLED"

                    self._register_order_data(dic)
                    on_update(AbstractExchangeHandler.OrderUpdate(**dic))

        _position_table: typing.Dict[str, typing.Dict[str, typing.Any]] = {}

        def __process_position_update(msg):
            for data in msg["data"]:
                symbol = data["symbol"]
                if symbol not in _position_table:
                    _position_table[symbol] = {}

                for key, value in data.items():
                    _position_table[symbol][key] = value

                position = _position_table[symbol]

                on_update(
                    AbstractExchangeHandler.PositionUpdate(
                        symbol=symbol,
                        size=position["currentQty"],
                        value=round(
                            position["currentQty"] / position["avgCostPrice"], 8
                        )
                        if position["avgCostPrice"] != None
                        else None,
                        entry_price=position["avgCostPrice"],
                        liquidation_price=position["liquidationPrice"],
                    )
                )

        _margin_data: typing.Dict[str, typing.Any] = {}

        def __process_margin_update(msg):
            data = msg["data"][0]

            for key, value in data.items():
                _margin_data[key] = value

            on_update(
                AbstractExchangeHandler.BalanceUpdate(
                    balance=_margin_data["marginBalance"] * cst["XBTUSD"], symbol="XBT"
                )
            )

        def __process_msg(msg):
            try:
                msg = json.loads(msg)
            except:
                return

            if "table" not in msg:
                return

            # Process order update table
            if msg["table"] == "order" and "data" in msg:
                __process_order_update(msg)

            # Process position update table
            if msg["table"] == "position" and "data" in msg:
                __process_position_update(msg)

            # Process margin update table
            if msg["table"] == "margin" and "data" in msg:
                __process_margin_update(msg)

        def __ping():
            ws.send("ping")
            timer = threading.Timer(10, __ping)
            timer.start()

        timer = threading.Timer(10, __ping)
        timer.start()

        while True:
            try:
                result = ws.recv()
                __process_msg(result)
            except Exception as e:
                traceback.print_tb(e.__traceback__)
                self.logger.error(
                    f"An error happened in user update socket {e.__class__.__name__} {e}: {result}, restarting..."
                )
                timer.cancel()
                self.start_user_update_socket(on_update)
                break

    async def load_historical_data(
        self, symbol: str, candle_type: str, amount: int
    ) -> pd.DataFrame:
        """load_historical_data loads historical klines from exchange

        Args:
            symbol (str): pair name
            candle_type (str): exchange specific type of candles ("1m" for example)
            amount (int): number of klines to load

        Returns:
            pd.DataFrame: dataframe with columns: Date, Open, High, Low, Close, Volume
        """

        parse_interval = lambda interval: (
            1
            if interval == "1m"
            else 5
            if interval == "5m"
            else 60
            if interval == "1h"
            else 24 * 60
            if interval == "1d"
            else 0
        )

        l_time = datetime.now()
        client = bitmex.bitmex(test=False)
        data: typing.List[typing.Any] = []
        max_amount_per_request = 1000

        k = 0
        while len(data) < amount:
            r_time = l_time - timedelta(
                minutes=max_amount_per_request * parse_interval(candle_type)
            )
            tmp = client.Trade.Trade_getBucketed(
                binSize=candle_type,
                symbol=symbol,
                startTime=r_time,
                count=max_amount_per_request,
            ).result()[0]

            data = tmp + data
            l_time = r_time

            k += 1

            if k % 3 == 0:
                time.sleep(5)

        df = pd.DataFrame(
            data[len(data) - amount :],
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )

        df = df.rename(
            columns={
                "timestamp": "Date",
                "open": "Open",
                "high": "High",
                "low": "Low",
                "close": "Close",
                "volume": "Volume",
            }
        )

        df["Date"] = df["Date"].map(lambda x: x.strftime("%Y-%m-%d %H:%M"))

        return df

    def get_symbols_data(self) -> typing.Dict[str, AbstractExchangeHandler.SymbolData]:
        raise NotImplementedError

    def _round_price(
        self, symbol: str, price: typing.Optional[float]
    ) -> typing.Optional[float]:
        if price is None:
            return None

        # TODO
        return int(price * 2) / 2

    async def create_order(
        self,
        symbol: str,
        side: str,
        price: typing.Optional[float],
        volume: float,
        client_ordID: typing.Optional[str] = None,
    ) -> AbstractExchangeHandler.NewOrderData:
        if client_ordID is None:
            if price is not None:
                result = self._client.Order.Order_new(
                    symbol=symbol,
                    side=side,
                    orderQty=volume,
                    price=self._round_price(symbol, price),
                    ordType="Limit",
                    execInst="ParticipateDoNotInitiate",
                ).result()[0]
            else:
                result = self._client.Order.Order_new(
                    symbol=symbol, side=side, orderQty=volume, ordType="Market",
                ).result()[0]
        else:
            self._user_update_pending(
                client_ordID, self._round_price(symbol, price), volume, symbol, side
            )
            if price is not None:
                result = self._client.Order.Order_new(
                    clOrdID=client_ordID,
                    symbol=symbol,
                    side=side,
                    orderQty=volume,
                    price=self._round_price(symbol, price),
                    ordType="Limit",
                    execInst="ParticipateDoNotInitiate",
                ).result()[0]
            else:
                result = self._client.Order.Order_new(
                    clOrdID=client_ordID,
                    symbol=symbol,
                    orderQty=volume,
                    side=side,
                    ordType="Market",
                ).result()[0]

        return AbstractExchangeHandler.NewOrderData(
            orderID=result["orderID"], client_orderID=result["clOrdID"]
        )

    async def create_orders(
        self,
        symbol: str,
        data: typing.List[typing.Tuple[str, float, float, typing.Optional[str]]],
    ) -> typing.List[AbstractExchangeHandler.NewOrderData]:
        orders: typing.List[typing.Dict[str, typing.Union[str, float]]] = [
            dict(
                symbol=symbol,
                side=order_data[0],
                orderQty=order_data[2],
                price=typing.cast(float, self._round_price(symbol, order_data[1])),
                ordType="Limit",
                execInst="ParticipateDoNotInitiate",
            )
            if len(order_data) == 3 or order_data[3] is None
            else dict(
                clOrdID=order_data[3],
                symbol=symbol,
                side=order_data[0],
                orderQty=order_data[2],
                price=typing.cast(float, self._round_price(symbol, order_data[1])),
                ordType="Limit",
                execInst="ParticipateDoNotInitiate",
            )
            for order_data in data
        ]
        for order in orders:
            self._user_update_pending(
                str(order["clOrdID"]),
                float(order["price"]),
                float(order["orderQty"]),
                str(order["symbol"]),
                str(order["side"]),
            )
        results = self._client.Order.Order_newBulk(orders=json.dumps(orders))
        results = results.result()[0]

        return [
            AbstractExchangeHandler.NewOrderData(
                orderID=result["orderID"], client_orderID=result["clOrdID"]
            )
            for result in results
        ]

    async def cancel_order(
        self,
        order_id: typing.Optional[str] = None,
        client_orderID: typing.Optional[str] = None,
    ) -> None:
        if order_id is not None:
            self._user_update_pending_cancel(order_id=order_id)
            self._client.Order.Order_cancel(orderID=order_id).result()
        elif client_orderID is not None:
            self._user_update_pending_cancel(client_orderID=client_orderID)
            self._client.Order.Order_cancel(clOrdID=client_orderID).result()
        else:
            raise ValueError(
                "Either order_id of client_orderID should be sent, but both are None"
            )

    async def cancel_orders(self, orders: typing.List[str]) -> None:
        for order_id in orders:
            self._user_update_pending_cancel(order_id=order_id)

        self._client.Order.Order_cancel(orderID=json.dumps(orders)).result()
