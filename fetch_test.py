import urllib.request
try:
    urllib.request.urlopen('http://localhost:20128/v1/chat/completions')
except Exception as e:
    if hasattr(e, 'read'):
        print(e.read().decode())
    else:
        print(e)
