import json
import urllib.error
import urllib.request

URL = "https://www.americangirl.com/collections/american-girl-last-chance/products.json?limit=250"

try:
    with urllib.request.urlopen(URL, timeout=30) as resp:
        data = json.load(resp)
        print("Status:", resp.status)
        print("Products:", len(data["products"]))
except urllib.error.HTTPError as e:
    print("Status:", e.code, e.reason)
