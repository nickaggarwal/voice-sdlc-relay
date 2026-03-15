# Voice SDLC Relay Server

FastAPI message router for the Voice SDLC system. Routes messages between the mobile app and agent connector via REST, SSE, and WebSocket.

## Deploy on Render

1. Create a new **Web Service** on [render.com](https://render.com)
2. Connect your GitHub repo and set the **Root Directory** to `packages/relay`
3. Render auto-detects Python. Set:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Add `RELAY_SECRET` in the **Environment** tab
5. Deploy. Your relay will be available at `https://your-relay.onrender.com`

Alternatively, use the `render.yaml` Blueprint for one-click setup.

## Run Locally

```bash
pip install -r requirements.txt
RELAY_SECRET=your-secret uvicorn main:app --host 0.0.0.0 --port 8080
```
