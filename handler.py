
import datetime
import logging
import json
import requests
import pandas as pd
import datetime
import awswrangler as wr
import uuid

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

def get_option_name_and_settlement(coin):
    """
    :param coin: crypto-currency coin name ('BTC', 'ETH')
    :return: 2 lists:
                        1.  list of traded options for the selected coin;
                        2.  list of settlement period for the selected coin.
    """

    r = requests.get("https://test.deribit.com/api/v2/public/get_instruments?currency=" + coin + "&kind=option")
    result = json.loads(r.text)

    # get option name
    name = pd.json_normalize(result['result'])['instrument_name']
    name = list(name)

    # get option settlement period
    settlement_period = pd.json_normalize(result['result'])['settlement_period']
    settlement_period = list(settlement_period)

    return name, settlement_period


def get_option_data(coin):
    """
    :param coin: crypto-currency coin name ('BTC', 'ETH')
    :return: pandas data frame with all option data for a given coin
    """

    # get option name and settlement
    coin_name, settlement_period  = get_option_name_and_settlement(coin)

    # initialize data frame
    coin_df = []

    # loop to download data for each instrument
    for i in range(len(coin_name)):
        # download option data -- requests and convert json to pandas
        print ("getting " + coin_name[i])
        r = requests.get('https://test.deribit.com/api/v2/public/get_order_book?instrument_name=' + coin_name[i])
        result = json.loads(r.text)
        df = pd.json_normalize(result['result'])

        # add settlement period
        df['settlement_period'] = settlement_period[i]

        # append data to data frame
        coin_df.append(df)

    # finalize data frame
    coin_df = pd.concat(coin_df)

    # remove useless columns from coin_df
    columns = ['state', 'estimated_delivery_price']
    coin_df.drop(columns, inplace=True, axis=1)

    return coin_df


def run(event, context):
    current_time = datetime.datetime.now().time()
    name = context.function_name

    print('Date and time: ' + datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S") + ' , format: dd/mm/yyyy hh:mm:ss')

    btc_data = get_option_data('BTC')
    eth_data = get_option_data('ETH')

    btc_data['id'] = [uuid.uuid1() for _ in range(len(btc_data.index))]
    eth_data['id'] = [uuid.uuid1() for _ in range(len(eth_data.index))]

    btc_data = btc_data.astype(str)
    eth_data = eth_data.astype(str)

    wr.dynamodb.put_df(df=btc_data, table_name="deribit_btc")
    wr.dynamodb.put_df(df=eth_data, table_name="deribit_eth")

    logger.info("Your cron function " + name + " ran at " + str(current_time))
