"""Kalshi signed-request client. Credentials are read from env at runtime ONLY
(KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY). They are never printed, logged, or written to disk.
Signing per docs.kalshi.com/getting_started/api_keys: base64(sign(ts_ms + METHOD + path_without_query)),
RSA-PSS/SHA-256 (MGF1 SHA-256, salt = digest length) for RSA keys; Ed25519 for Ed25519 keys."""
import base64, json, os, re, time, urllib.request, urllib.error
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HOST = "https://api.elections.kalshi.com"
PREFIX = "/trade-api/v2"


class AuthError(Exception):
    pass


def _pem_from_env(raw):
    """Rebuild a valid PEM if newlines were collapsed to spaces (or arrived as literal \\n)."""
    raw = raw.replace("\\n", "\n").strip()
    m = re.search(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", raw, re.S)
    if not m:
        raise AuthError("KALSHI_PRIVATE_KEY is not a PEM block")
    label, body = m.group(1), re.sub(r"\s+", "", m.group(2))
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return ("-----BEGIN %s-----\n%s\n-----END %s-----\n" % (label, "\n".join(lines), label)).encode()


class KalshiAuthClient:
    def __init__(self):
        kid, pk = os.environ.get("KALSHI_API_KEY_ID"), os.environ.get("KALSHI_PRIVATE_KEY")
        if not kid or not pk:
            raise AuthError("KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY not set in this process environment")
        self._kid = kid.strip()
        try:
            self._key = serialization.load_pem_private_key(_pem_from_env(pk), password=None)
        except Exception as e:  # do not include key material in the message
            raise AuthError("could not parse private key (%s)" % type(e).__name__) from None
        self.key_type = "ed25519" if isinstance(self._key, Ed25519PrivateKey) else "rsa"

    def _sign(self, text):
        msg = text.encode()
        if self.key_type == "ed25519":
            sig = self._key.sign(msg)
        else:
            sig = self._key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                  salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        return base64.b64encode(sig).decode()

    def build(self, method, path, body=None):
        """Return (url, headers, data) without sending. path is relative to /trade-api/v2."""
        full = PREFIX + path
        ts = str(int(time.time() * 1000))
        headers = {"KALSHI-ACCESS-KEY": self._kid, "KALSHI-ACCESS-TIMESTAMP": ts,
                   "KALSHI-ACCESS-SIGNATURE": self._sign(ts + method.upper() + full.split("?")[0]),
                   "Content-Type": "application/json", "User-Agent": "btc15m-kalshi-bot/1.0"}
        data = json.dumps(body).encode() if body is not None else None
        return HOST + full, headers, data

    def request(self, method, path, body=None, timeout=10, retries=2):
        """Signed request with a hard socket timeout. GET/DELETE are retried on network errors
        (re-signed each attempt). POST is NOT retried here: order placement stays single-shot and
        relies on the per-window client_order_id for idempotency."""
        m = method.upper(); attempts = (retries + 1) if m in ("GET", "DELETE") else 1
        last = None
        for i in range(attempts):
            url, headers, data = self.build(method, path, body)
            req = urllib.request.Request(url, data=data, headers=headers, method=m)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    txt = r.read().decode()
                    return r.status, (json.loads(txt) if txt else {})
            except urllib.error.HTTPError as e:
                txt = e.read().decode(errors="replace")[:500]
                try: j = json.loads(txt)
                except Exception: j = {"raw": txt}
                if e.code >= 500 and i + 1 < attempts:
                    last = e; time.sleep(0.5 * (i + 1)); continue
                return e.code, j
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last = e
                if i + 1 < attempts: time.sleep(0.5 * (i + 1)); continue
                raise
        raise RuntimeError(f"{m} {path.split('?')[0]} failed: {type(last).__name__}")

    @staticmethod
    def redacted(headers):
        return {k: ("<redacted>" if k.startswith("KALSHI-ACCESS") else v) for k, v in headers.items()}
