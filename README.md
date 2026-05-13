# Gemini Live Audio Chatbot

A real-time voice-interactive chatbot for kiosk/robot deployment, powered by Google's Gemini Live API. Built with FastAPI and WebSockets for low-latency bidirectional audio streaming.

## Features

- **Real-time Voice Interaction** - Bidirectional audio streaming with Gemini 2.5 Flash
- **25 Voice Options** - Choose from male/female voices with different styles
- **Admin Control Panel** - Manage all connected kiosks from a single dashboard
- **Live System Instructions** - Update the bot's persona in real-time
- **Structured Logging** - JSON-formatted logs with session correlation
- **Metrics Collection** - Track connections, latency, and audio throughput

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Set your API key
echo "GOOGLE_API_KEY=your_api_key_here" > .env

# Run the server
uvicorn main:app --host 0.0.0.0 --port 8000
venv/bin/uvicorn main:app --reload --port 8000 --host 172.17.0.1
```

## Configuration

Environment variables (`.env`):

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_API_KEY` | *required* | Google API key for Gemini |
| `GEMINI_MODEL` | `gemini-2.5-flash-native-audio-preview-12-2025` | Model identifier |
| `SAMPLE_RATE` | `24000` | Audio sample rate (Hz) |
| `SILENCE_DURATION_MS` | `50` | VAD silence duration |
| `GENERATION_TEMPERATURE` | `0.3` | Response creativity |

## API Endpoints

### Health & Metrics
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/metrics` | GET | System metrics (JSON) |

### Admin Control
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/admin/status` | GET | System status |
| `/admin/kill` | POST | Interrupt all sessions |
| `/admin/speak` | POST | Make all bots speak text |
| `/admin/mute` | POST | Toggle global mute |
| `/admin/voices` | GET | List available voices |
| `/admin/voice` | POST | Set active voice |
| `/admin/instructions` | GET/POST | Read/update system instructions |

### WebSocket
| Endpoint | Description |
|----------|-------------|
| `/ws/audio` | Bidirectional audio stream |

## Available Voices

| Voice | Gender | Style |
|-------|--------|-------|
| Zephyr | Female | Bright |
| Puck | Male | Upbeat |
| Charon | Male | Informative |
| Kore | Female | Firm |
| Fenrir | Male | Excitable |
| Leda | Female | Youthful |
| Orus | Male | Firm |
| Aoede | Female | Breezy |
| ... | ... | ... |

*25 voices total. View all via `/admin/voices` endpoint.*

## Architecture

```
┌─────────────────┐         ┌──────────────────┐
│   Kiosk/Robot   │◄───────►│   FastAPI Server │
│   (WebSocket)   │  audio  │                  │
└─────────────────┘         └────────┬─────────┘
                                     │
                            ┌────────▼─────────┐
                            │  Gemini Live API │
                            └──────────────────┘
```

## Kiosk Deployment Notes

- **Latency**: Audio delay buffer is set to 150ms for responsive interactions
- **Auto-reconnect**: Client automatically reconnects on WebSocket disconnect
- **Fullscreen**: Index page has fullscreen button that auto-hides after 10s
- **PWA Support**: Manifest included for installable web app

## File Structure

```
├── main.py           # FastAPI server
├── index.html        # Kiosk client UI
├── admin.html        # Admin control panel
├── instructions.txt  # System instructions
├── static/
│   ├── app.js        # Client JavaScript
│   ├── admin.js      # Admin panel JavaScript
│   ├── style.css     # Client styles
│   ├── admin.css     # Admin panel styles
│   └── pcm-processor.js  # Audio worklet
└── requirements.txt
```

## License

Internal use only - Smart Methods Company
