import requests
import logging

# replace the "demo" apikey below with your own key from https://www.alphavantage.co/support/#api-key
url = 'https://www.alphavantage.co/query?function=TIME_SERIES_INTRADAY&symbol=IBM&interval=5min&apikey=YOUR_API_KEY_HERE'
r = requests.get(url)
data = r.json()

logging.info("[i] %s", data)
