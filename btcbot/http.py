import json, time, urllib.request, urllib.error

UA = {"User-Agent": "btc15m-paper-bot/1.0", "Accept": "application/json"}

def get_json(url, timeout=10, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # network / http error
            last = e
            time.sleep(0.5 * (i + 1))
    raise RuntimeError(f"GET {url} failed: {last}")
