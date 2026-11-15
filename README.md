# _UHH by Plunger Studios

_UHH is a chat website powered by a Python AI backend. Chat history and personalization stay in each user's browser's IndexedDB/local storage. Existing chats (including old image messages) are retained; new image generation is disabled.

## What users need to know

- The email screen creates a **local profile only**. It does not verify a Google/Gmail account, create a password, or sync chats across devices.
- Messages go from the website to the _UHH backend on the same hosted service, which tries Gemini, Groq, then OpenRouter. The backend does not intentionally save chat contents; the AI providers and hosting platform process requests under their own current policies.
- API keys belong in the backend host's private environment settings. Never include a key in the Windows app, GitHub, a `.env` file committed to Git, or chat.
- The default OpenRouter model is `openrouter/free`, which uses OpenRouter's free-model router. Free-model availability and quotas can change. Groq billing depends on the selected model/account; leave `GROQ_MODEL` blank until you confirm a model is available at no charge. Gemini free-tier limits also vary. **Fallback cannot guarantee zero cost** if a provider account can bill usage.
- The backend is public and the local email profile is not authentication. Basic per-IP rate limiting is included, but it cannot prevent all abuse or protect provider quotas. Set provider spending limits/disable billing where possible; don't publish the endpoint until you understand this risk.
- Render's free services may sleep when idle, so the first reply can be slow. Free plans and quotas are not guaranteed and may change.

## Publish the website on Render

1. Put this project in a GitHub repository. Do not add `.env`, provider keys, or `backend_url.txt`.
2. In Render, create a Blueprint from the repository and select the included `render.yaml`. It creates one web service for both the website and AI API.
3. In the service's Environment settings, add secret variables:
   - `GEMINI_API_KEY` — your Gemini key
   - `GROQ_API_KEY` — your Groq key (optional)
   - `OPENROUTER_API_KEY` — your OpenRouter key (optional)
   - `GROQ_MODEL` — a model ID your Groq account can use; verify pricing before enabling
   - Optional: `GEMINI_MODEL` (defaults to `gemini-3.6-flash`) and `OPENROUTER_MODEL` (defaults to the free-only `openrouter/free` router)
4. Deploy and wait for Render to show the service as live. The website URL looks like `https://your-service-name.onrender.com`; open that URL to use _UHH.

The website and API share that URL, so website users don't need a separate backend URL or API key. The backend tries configured providers in Gemini → Groq → OpenRouter order when one is rate-limited or unavailable. It never switches to a paid OpenRouter model by default. When all configured providers are unavailable, the app shows the requested outage notice and polls provider status without generating test prompts.

## Run _UHH locally on Windows

1. Install Python 3.
2. Copy `backend_url.example.txt` to `backend_url.txt` and replace its contents with your Render URL. Alternatively, copy `.env.example` to `.env` and set `UHH_BACKEND_URL`.
3. Double-click `run.bat`. It starts the local app and opens `http://localhost:8000`.

For local development, the same backend URL can be set in `.env`. Do not put provider keys there; the local app does not need them.

## Build a runnable Windows app

1. Complete the Render deployment and create `backend_url.txt` with the website URL.
2. Install Python 3 and PyInstaller: `python -m pip install pyinstaller`
3. Double-click `build_windows.bat`.
4. Share `dist\_UHH.exe` (and the copied `backend_url.txt` next to it) with users. The executable contains no provider API keys. Users' chat histories stay in their browser on their own device.

The executable launches the local app in the default browser. It uses `http://localhost:8000`, the same browser origin as the current local app, so existing local chat data remains available and is not deleted or migrated. Keep the executable's console window open while using _UHH.

## Local profile and chat data

- Email and terms acceptance are saved locally; the email is not sent to the backend and is not a verified sign-in.
- Chats, personalization, and any historical generated images are saved in the current browser's local IndexedDB/local storage. Clearing browser data removes them.
- No chat history is uploaded or synced as part of this app; the browser sends only the current conversation context needed for a reply.

## Terms and privacy

The app presents the Terms of Use & Privacy Notice supplied for _uhh at first launch. That notice is an app-provided summary, not legal advice. Review provider and hosting terms and update the notice before public release.
