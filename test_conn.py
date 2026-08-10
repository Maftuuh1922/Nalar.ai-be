import urllib.request
try:
    response = urllib.request.urlopen('http://127.0.0.1:8085/api/health', timeout=2)
    print("SUCCESS:", response.read().decode())
except Exception as e:
    print("FAILED:", e)
