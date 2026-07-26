import json
import urllib.request
from urllib.error import HTTPError, URLError

try:
    req = urllib.request.Request("http://127.0.0.1:8000/api/settings/model")
    # Need to bypass auth if possible, or just see the error
    with urllib.request.urlopen(req) as response:
        body = response.read().decode('utf-8')
        print("BODY LENGTH:", len(body))
        print("BODY:", body)
except HTTPError as e:
    body = e.read().decode('utf-8')
    print("HTTP ERROR:", e.code)
    print("BODY:", body)
except URLError as e:
    print("URL ERROR:", e.reason)
