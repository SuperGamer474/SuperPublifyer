# SuperPublifyer

**SuperPublifyer** lets you expose a local dev server via a hosted public gateway (server already running on Render). This repo contains the **client GUI** that connects your local site to the public endpoint.

---

## Quick TL;DR
- Server is already hosted: `https://superpublifyer.onrender.com`
- Alternative URL: `http://public.servehttp.com`   
- Download and run the client only.

---

## Repo layout
```
client/
  └─ SuperPublifyer.py    # GUI client

server.py                 # For server
static/                   # For server
  └─ 404.html             # For server
requirements.txt          # For server
```

---

## Quick start (client only)
1. Clone or download this repo.  
2. Run the client:
```
python client/SuperPublifyer.py
```
3. In the GUI enter:
- **Project name** (letters, numbers, `_` or `-`, 3–50 chars)  
- **Local server** `host:port` (e.g. `localhost:8000`)  
4. Click **Connect**. Accessible URLs will appear — copy or open them.

If you used `localhost:PORT`, the client will also show `127.0.0.1:PORT` labelled **(Local only)**.

---

## Notes & troubleshooting
- If the client cannot connect even after 5 attempts, please manually visit the [superpublifyer](https://superpublifyer.onrender.com) website and wait until it has started.
- WebSocket/connect issues: confirm outbound connections to `superpublifyer.onrender.com` are allowed by your firewall.  
- Timeout: server waits ~30s for the client to respond — increase locally if needed.

---

## Security
Public URLs are public. Don’t expose sensitive services (databases, admin panels) without adding auth/TLS. This is for demos/dev only unless you harden it.

---
