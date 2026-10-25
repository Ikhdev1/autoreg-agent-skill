# SMS and phone verification

The pack has no bundled SMS API client: numbers are a paid, provider-specific
resource and a wrong integration wastes money. Use a `hook` (or a tiny local
service) with one of the providers below. The flow stays the same:

```yaml
steps:
  - action: hook
    name: buy_number          # returns {"phone": "+1..."}
  - fill: {target: "input[type=tel]", value: "{{phone}}"}
  - action: hook
    name: wait_sms            # returns {"sms_code": "123456"}
  - fill: {target: "input[name=code]", value: "{{sms_code}}"}
```

`hooks.py`:

```python
import os, time, requests

BASE = "https://hero-sms.com/stubs/handler_api.php"
KEY = os.environ["HERO_SMS_KEY"]
SERVICE = os.environ.get("SMS_SERVICE", "ot")   # target's service code
COUNTRY = os.environ.get("SMS_COUNTRY", "0")

def buy_number(ctx):
    r = requests.get(BASE, params={"api_key": KEY, "action": "getNumber",
                                   "service": SERVICE, "country": COUNTRY},
                     timeout=30)
    # ACCESS_NUMBER:<id>:<phone>
    state, order_id, phone = r.text.strip().split(":")
    ctx.vars["sms_order"] = order_id
    return {"phone": "+" + phone.lstrip("+")}

def wait_sms(ctx):
    order_id = ctx.vars["sms_order"]
    deadline = time.time() + 300
    while time.time() < deadline:
        r = requests.get(BASE, params={"api_key": KEY, "action": "getStatus",
                                       "id": order_id}, timeout=30)
        text = r.text.strip()
        if text.startswith("STATUS_OK:"):
            return {"sms_code": text.split(":", 1)[1]}
        if text == "STATUS_CANCEL":
            raise RuntimeError("SMS order cancelled")
        time.sleep(5)
    raise TimeoutError("SMS code timeout")
```

On failure, call `setStatus&status=8&id=<order>` to cancel/refund (provider
dependent). Do this on every failure branch.

## Paid / API

| Service | API shape | Notes |
|---|---|---|
| HeroSMS | SMS-Activate-compatible `handler_api.php` | Successor to SMS-Activate; multi-country; the operator's default |
| 5SIM | REST `/v1/user/buy/activation/{country}/{operator}/{product}` + `/check/{id}` | Service/country/operator selection |
| SMSPool | REST | Per-code + monthly rental; free tier |
| PVACodes | REST | Multi-country; used by the Jio/Gemini link farm |
| PVAPins | REST | Receive + inbox + rental |
| NumberOTP | REST | $0.10 start, 150+ countries, no card |
| SMS-MAN | REST | Receive + rental + free numbers |
| Tiger SMS | REST | Has API |
| OnlineSIM | REST | One-off + extendable rental |
| GlobalSMS | REST | US-focused |
| PingMe | App/web | Second number |
| SMSHub | REST | Cloudflare-protected; check current API |
| mytempsms | API v5 (`america.receivesms.top`) | Key is MD5; cheap |
| Textverified | API | US non-VoIP; strict targets |
| SMSPinVerify | API | Non-VoIP, long-lived US numbers |
| FoxSIMs | API | US SIM, one-off/long-term |
| Buru | API | Pay per code, US numbers |

## Rental

| Service | Notes |
|---|---|
| Groovl | Real SIM, long-term; web inbox + email/URL forwarding |
| SMSSir | UK numbers; SMS + calls + OTP |
| Virtty | Duration-based rental; API callback |
| SMS7 / USA SIM | US physical SIM, temp + long-term |
| Freeje | One-off + extendable |
| Quackr | Free temp + paid private |
| ASMS / AnonymSMS | Inbox + rental + API + MCP |

## Free public receivers (low-risk only)

The October 2026 "live" list:

`goinsms.xyz` (US/UK/HK/CN), `temp-number.com` (8500+, 60+ countries, 7d),
`quackr.io` (22 countries, 1000+, 24h), `anonymsms.com` (US, 24/7),
`receive-sms-free.cc`, `sms-online.co`, `sms24.me` (53 countries, some video
unlock), `2nd-no.com` (3 numbers/account/3 days, JSON-RPC),
`temporary-phone-number.com`, `receivesms.org`, `jiemahao.com`,
`freephonenum.com`, `smstome.com`, `smss.net`, `temp-number.org`,
`temporarynumber.com`, `7sim.net`, `foundtt.com`, `online-sim.org`,
`24sms7.com`, `receive-smss.com`.

Also: `sms-receive.net`, `hs3x.com`, `receivefreesms.net`,
`receivesmsonline.in`, `receiveasms.com`, `us-phone-number.com`, `anon-sms.com`,
`sms.sellaite.com`, `sms-verification-number.com`, `getfreesmsnumber.com`.

Rules:

- The number is public. Another user may consume the code.
- Use only for low-value targets; never for Telegram/OpenAI (both ban these
  pools).
- Keep the browser's exit IP stable while receiving the code.
- Read the newest message; a stale code from the previous run is a common bug.

## Telegram bots

`@ReceiveSMSRobot` (10+ countries), `@KupitNomerBot`, `@receive_robot`,
`@timeofsms_bot`, `@SMSPVA_COM_BOT`, plus the free-mail/OTP bots listed in the
mail catalog. Bots are manual fallbacks, not batch infrastructure.

## 2nd-no JSON-RPC

`2nd-no.com` is a free Polish receiver with a JSON-RPC API:

```text
POST https://2no.pl/
  {"id": 310}                                  -> random free number
  {"id": 301, "query": {number_id, name, ...}} -> reserve (Turnstile response_key)
  {"id": 311, "query": {offset, limit}}        -> my numbers
  {"id": 415, "query": {phone: "+48...", number_id: 123}} -> messages
header x-auth-token = sessionStorage["2NR-TOKEN"]
```

Direct HTTP is Cloudflare-403; call it with `browser_fetch` inside the logged-in
page. Sitekey: `0x4AAAAAAAh6YYTPTzEcN3Ep`. Reuse an active number before
reserving a new one (3-number cap); mark a number used only after a real OTP.

## Provider API examples

### HeroSMS / SMS-Activate protocol

```text
GET /stubs/handler_api.php?api_key=...&action=getNumber&service=<code>&country=<id>
    -> ACCESS_NUMBER:<order_id>:<phone>
GET ...&action=getStatus&id=<order_id>
    -> STATUS_WAIT_CODE | STATUS_OK:<code> | STATUS_CANCEL
GET ...&action=setStatus&status=8&id=<order_id>   # cancel/refund
GET ...&action=setStatus&status=6&id=<order_id>   # complete
```

### 5SIM

```text
GET https://5sim.net/v1/user/buy/activation/{country}/{operator}/{product}
Authorization: Bearer <token>
    -> {"id": 123, "phone": "+..."}
GET https://5sim.net/v1/user/check/{id}
    -> {"status": "RECEIVED", "sms": [{"code": "123456"}]}
GET https://5sim.net/v1/user/cancel/{id}
```

### NumberOTP / PVACodes / PVAPins

REST APIs with a bearer/API key; read the provider's current OpenAPI page. The
generic pattern is always: buy → poll → code → complete/cancel. Put it in a hook
and keep the provider key in `.env`, never in the adapter.

## Selection rules

1. Recon first: if there is no phone field, do not buy a number.
2. Country must match the target's expected market; a US number on a Polish
   target is a flag.
3. If the target rejects VoIP, use Textverified/SMSPinVerify/FoxSIMs.
4. One number per account; never reuse a number across accounts.
5. Cancel/refund on every failure branch.
6. Keep the same exit IP for the number purchase, browser, and code entry.
7. Free numbers are a fallback, not a pipeline. Budget for a paid API.
