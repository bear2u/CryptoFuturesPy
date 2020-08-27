"""
    This module contains an implementation for Binance Futures (BinanceFuturesExchangeHandler)
"""


import pandas as pd
import typing
import json
import logging
import pandas as pd

from . import futurespy as fp
from . import AbstractExchangeHandler


class BinanceFuturesExchangeHandler(AbstractExchangeHandler):
    def __init__(self, public_key, private_key):
        super().__init__(public_key, private_key)
        self._client = fp.Client(
            testnet=False, api_key=self._public_key, sec_key=self._private_key
        )

        self._orderId_dict = {}
        self._clOrderId_dict = {}

        self.logger = logging.Logger(__name__)
        self._order_table: typing.Dict[str, typing.Dict[str, typing.Any]] = {}
        
        self.exchange_information = fp.MarketData().exchange_info()

    def start_kline_socket(
        self,
        on_update: typing.Callable[[AbstractExchangeHandler.KlineCallback], None],
        candle_type: str,
        pair_name: str,
    ) -> None:
        def _on_update(message):
            candle = message["k"]
            if candle["x"]:
                on_update(
                    self.KlineCallback(
                        time=pd.to_datetime(candle["t"], unit="ms"),
                        open=float(candle["o"]),
                        high=float(candle["h"]),
                        low=float(candle["l"]),
                        close=float(candle["c"]),
                        volume=float(candle["v"]),
                    )
                )

        ws = fp.WebsocketMarket(
            symbol=pair_name,
            on_message=lambda _, message: _on_update(message),
            interval=candle_type,
        )
        ws.candle_socket()

    def start_price_socket(
        self,
        on_update: typing.Callable[[AbstractExchangeHandler.PriceCallback], None],
        pair_name: str,
    ) -> None:
        def _on_update(message):
            on_update(self.PriceCallback(float(message["p"])))

        ws = fp.WebsocketMarket(
            symbol=pair_name, on_message=lambda _, message: _on_update(message),
        )
        ws.mark_price_socket()

    def start_user_update_socket(
        self, on_update: typing.Callable[[AbstractExchangeHandler.UserUpdate], None]
    ) -> None:
        super().start_user_update_socket(on_update)

        def _on_update_recieved(message: typing.Dict[str, typing.Any]) -> None:
            print(message)
            # if message["e"] == "ACCOUNT_UPDATE":
            #     print(message)
            # elif message["e"] == "ORDER_TRADE_UPDATE":
            #     print(
            #         {
            #             "id": message["o"]["c"],
            #             "status": message["o"]["X"],
            #             "price": float(message["o"]["ap"]),
            #             "fee": float(message["o"]["n"]) if "n" in message["o"] else 0,
            #             "fee_asset": message["o"]["N"] if "N" in message["o"] else None,
            #             "volume": float(message["o"]["z"]),
            #             "time": pd.to_datetime(message["o"]["T"], unit="ms"),
            #             "message": message
            #         }
            #     )

        self._client.user_update_socket(
            on_message=lambda ws, message: _on_update_recieved(json.loads(message)),
            on_close=lambda x: self.start_user_update_socket(on_update),
        )

    def _round_price(
        self, symbol: str, price: typing.Optional[float]
    ) -> typing.Optional[float]:
        
        for d in self.exchange_information['symbols']:
            if d['symbol'] == symbol:
                price_precision = d['symbol']['pricePrecision']
                break
        
        return None if price is None else round(price, price_precision)
    
    def _round_volume(
        self, symbol: str, volume: typing.Optional[float]
    ) -> typing.Optional[float]:
        
        for d in self.exchange_information['symbols']:
            if d['symbol'] == symbol:
                quantity_precision = d['symbol']['quantityPrecision']
                break
            
        return None if volume is None else round(volume, quantity_precision)

    def _user_update_pending(
        self,
        client_orderID: str,
        price: typing.Optional[float],
        volume: float,
        symbol: str,
        side: str,
    ) -> None:
        ...  # TODO

    def _user_update_pending_cancel(
        self,
        order_id: typing.Optional[str] = None,
        client_orderID: typing.Optional[str] = None,
    ) -> None:
        ...  # TODO

    def get_pairs_list(self) -> typing.List[str]:
        """get_pairs_list Returns all available pairs on exchange

        Returns:
            typing.List[str]: The list of symbol strings
        """

        return [pair["symbol"] for pair in self.exchange_information["symbols"]]

    async def load_historical_data(
        self, symbol: str, candle_type: str, amount: int
    ) -> pd.DataFrame:
        """load_historical_data Loads historical klines from exchange

        Args:
            symbol (str): Pair name
            candle_type (str): Exchange specific type of candles ("1m" for example)
            amount (int): Number of klines to load

        Returns:
            pd.DataFrame: Dataframe with columns: Date, Open, High, Low, Close, Volume
        """
        marketDataLoader = fp.MarketData(
            symbol=symbol, interval=candle_type, testnet=False
        )
        data = marketDataLoader.load_historical_candles(count=amount).iloc[:-1]
        data = data[["Date", "Open", "High", "Low", "Close", "Volume"]]

        return data

    async def create_order(
        self,
        symbol: str,
        side: str,
        price: typing.Optional[float],
        volume: float,
        client_ordID: typing.Optional[str] = None,
    ) -> AbstractExchangeHandler.NewOrderData:
        """create_order Place one limit or market order

        Args:
            symbol (str): Pair name, for which to place an order
            side (str): "Buy" or "Sell"
            price (typing.Optional[float]): If the price is set, the price for limit order. Else - market order.
            volume (float): The volume of the order
            client_ordID (typing.Optional[str], optional): Client order_id. 
                Could be generated using generate_client_order_id(). Defaults to None.

        Returns:
            AbstractExchangeHandler.NewOrderData: Data of the resulting order.
        """
        print(symbol, side, price, volume, client_ordID)

        if client_ordID is None:
            if price is not None:
                result = self._client.new_order(
                    symbol=symbol,
                    side=side.upper(),
                    orderType="LIMIT",
                    quantity=self._round_volume(symbol, volume),
                    price=self._round_price(symbol, price),
                    timeInForce="GTX",  # POST ONLY
                )
            else:
                result = self._client.new_order(
                    symbol=symbol,
                    side=side.upper(),
                    quantity=self._round_volume(symbol, volume),
                    orderType="MARKET",
                )
        else:
            self._user_update_pending(
                client_ordID,
                self._round_price(symbol, price),
                self._round_volume(symbol, volume),
                symbol,
                side.upper(),
            )
            if price is not None:
                result = self._client.new_order(
                    newClientOrderId=client_ordID,
                    symbol=symbol,
                    side=side.upper(),
                    orderType="LIMIT",
                    quantity=self._round_volume(symbol, volume),
                    price=self._round_price(symbol, price),
                    timeInForce="GTX",  # POST ONLY
                )
            else:
                result = self._client.new_order(
                    newClientOrderId=client_ordID,
                    symbol=symbol,
                    quantity=self._round_volume(symbol, volume),
                    side=side.upper(),
                    orderType="MARKET",
                )

        return AbstractExchangeHandler.NewOrderData(
            orderID=result["orderId"], client_orderID=result["clientOrderId"]
        )

    async def create_orders(
        self,
        symbol: str,
        data: typing.List[typing.Tuple[str, float, float, typing.Optional[str]]],
    ) -> typing.List[AbstractExchangeHandler.NewOrderData]:
        """create_orders Create a lot of orders from one request (if the exchange supports it)

        If the exchange does not support it, should create a parallel http requests, but it should be warned in docstring.

        Args:
            symbol (str): Pair name, for which to place orders
            data (typing.List[typing.Tuple[str, float, float, typing.Optional[str]]]): The list of tuple params like in
                create_order() - (side, price, volume, client_ordID), except price should not be None.

        Returns:
            typing.List[AbstractExchangeHandler.NewOrderData]: List of results
        """
        orders: typing.List[typing.Dict[str, typing.Union[str, float]]] = [
            {
                "symbol": symbol,
                "side": order_data[0].upper(),
                "type": "LIMIT",
                "quantity": self._round_volume(symbol, order_data[2]),
                "price": typing.cast(float, self._round_price(symbol, order_data[1])),
                # "timeInForce" : "GTX" # POST ONLY
            }
            if len(order_data) == 3 or order_data[3] is None
            else {
                "clOrdID": order_data[3],
                "symbol": symbol,
                "side": order_data[0].upper(),
                "type": "LIMIT",
                "quantity": self._round_volume(symbol, order_data[2]),
                "price": typing.cast(float, self._round_price(symbol, order_data[1])),
                # "timeInForce" : "GTX" # POST ONLY
            }
            for order_data in data
        ]
        for order in orders:
            self._user_update_pending(
                client_orderID=str(order["clOrdID"]),
                price=float(order["price"]),
                volume=float(order["quantity"]),
                symbol=str(order["symbol"]),
                side=str(order["side"]),
            )

        results = []
        orders_list = self._split_list(lst=orders, size=5)
        for tmp_orders_list in orders_list:
            results.append(self._client.place_multiple_orders(tmp_orders_list))

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
        """cancel_order Cancel one order via order_id or client_orderID

        Either order_id or client_orderID should be sent.
        If both are sent, will use order_id.

        Args:
            order_id (typing.Optional[str], optional): Server's order id. Defaults to None.
            client_orderID (typing.Optional[str], optional): Client's order id. Defaults to None.
        """

        if order_id is not None and order_id in self._orderId_dict.keys():
            self._client.cancel_order(
                symbol=self._orderId_dict[order_id], orderId=order_id
            )
        elif (
            client_orderID is not None and client_orderID in self._clOrderId_dict.keys()
        ):
            self._client.cancel_order(
                symbol=self._clOrderId_dict[client_orderID], clientID=client_orderID
            )
        else:
            raise ValueError(
                "Either order_id of client_orderID should be sent, but both are None"
            )

    @staticmethod
    def _split_list(lst, size):
        return [lst[i : i + size] for i in range(0, len(lst), size)]

    @staticmethod
    def swap_dict(orders):
        symbols_dict = {}
        for key, value in orders.items():
            if value in symbols_dict:
                symbols_dict[value].append(key)
            else:
                symbols_dict[value] = [key]

    async def cancel_orders(self, orders: typing.List[str]) -> None:
        """cancel_orders Cancels a lot of orders in one requets

        If the exchange does not support it, should create a parallel http requests, but it should be warned in docstring.

        Args:
            orders (typing.List[str]): The list of server's order_ids.
        """

        for order_id in orders:
            self._user_update_pending_cancel(order_id=order_id)

        symbols_dict = self.swap_dict(self._orderId_dict)

        to_cancel_dict = {}
        for key in symbols_dict.keys():
            for order_id in orders:
                if order_id in symbols_dict[key]:
                    if key in to_cancel_dict:
                        to_cancel_dict[key].append(order_id)
                    else:
                        to_cancel_dict[key] = [order_id]

        results = []
        for symbol in to_cancel_dict.keys():
            tmp_list = self._split_list(to_cancel_dict[symbol], 10)
            for lst in tmp_list:
                result = self._client.cancel_multiple_orders(
                    symbol=symbol, orderIdList=lst
                )
                results.append(result)

        return results
