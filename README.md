# Voice SDLC Relay Server

FastAPI message router for the Voice SDLC system. Routes messages between the mobile app and agent connector via REST, SSE, and WebSocket.

## Deploy on Replit

1. Import this repo into Replit
2. Add `RELAY_SECRET` in Secrets tab
3. Click Run

## Run Locally

```bash
pip install -r requirements.txt
RELAY_SECRET=your-secret uvicorn main:app --host 0.0.0.0 --port 8080
```
