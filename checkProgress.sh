#!/bin/bash
clear

cmd1="sqlite3 binance-trade-bot/data/crypto_trading.db"
sql0=".mode column"
sql00=".headers on"
sql1="SELECT th1.alt_coin_id AS coin, th1.alt_trade_amount AS amount, th1.crypto_trade_amount AS priceInUSD, (th1.alt_trade_amount - (SELECT th2.alt_trade_amount FROM trade_history th2 WHERE th2.alt_coin_id = th1.alt_coin_id AND th1.datetime > th2.datetime AND th2.selling = 0 ORDER BY th2.datetime DESC LIMIT 1)) AS change, datetime FROM trade_history th1 WHERE th1.state = 'COMPLETE' AND th1.selling = 0 ORDER BY th1.datetime DESC;"
$cmd1 "$sql0" "$sql00"  "$sql1"
