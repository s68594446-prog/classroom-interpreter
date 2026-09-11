`launch.json` is a local dev-server definition used by the IDE preview. It
starts the Vite dev server on 127.0.0.1:5173, which proxies /api, /health and
/ws to the backend on 127.0.0.1:8000.

Open the dev UI at **http://127.0.0.1:5173**, not http://localhost:5173.
The server binds IPv4 loopback only (V1 requires loopback), while `localhost`
may resolve to IPv6 `::1` and the proxy then fails to connect.
