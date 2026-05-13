"""
Gemini Live Audio Backend Server.

A FastAPI-based WebSocket server that proxies audio between clients and
Google's Gemini Live API for real-time voice interactions.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
import uuid
import logging
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, AsyncGenerator

import pandas as pd
from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

if TYPE_CHECKING:
    from collections.abc import MutableMapping


# =============================================================================
# CONFIGURATION
# =============================================================================

class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    google_api_key: str = Field(..., description="Google API key for Gemini access")
    gemini_model: str = Field(
        default="gemini-2.5-flash-native-audio-preview-12-2025",
        description="Gemini model identifier"
    )
    sample_rate: int = Field(default=24000, ge=8000, le=48000)
    silence_duration_ms: int = Field(default=50, ge=50, le=2000)
    generation_temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    instruction_file: Path = Field(default=Path("instructions.txt"))
    excel_file: Path = Field(default=Path("employees.xlsx"))
    static_dir: Path = Field(default=Path("static"))

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


settings = Settings()


# =============================================================================
# EXCEL DATA LOADER
# =============================================================================

def load_excel_data(path: Path) -> str:
    """Load employee data from Excel file and convert to structured text."""
    if not path.exists():
        logger.warning(f"Excel file not found: {path}")
        return ""
    try:
        df = pd.read_excel(path)
        df = df.fillna("")
        lines = ["### بيانات الموظفين"]
        lines.append(f"يوجد {len(df)} موظف في قاعدة البيانات.\n")
        lines.append("عند سؤالك عن موظف، ابحث في البيانات التالية وأجب بدقة:\n")
        for _, row in df.iterrows():
            parts = [f"{col}: {str(row[col]).strip()}" for col in df.columns if str(row[col]).strip()]
            lines.append("- " + " | ".join(parts))
        result = "\n".join(lines)
        logger.info(f"Loaded {len(df)} employees from Excel: {path}")
        return result
    except Exception as e:
        logger.error(f"Failed to load Excel file: {e}")
        return ""


# =============================================================================
# VOICE OPTIONS
# =============================================================================

@dataclass(frozen=True)
class VoiceOption:
    """Represents a Gemini voice option."""
    name: str
    gender: str
    style: str


AVAILABLE_VOICES: tuple[VoiceOption, ...] = (
    VoiceOption("Zephyr", "Female", "Bright"),
    VoiceOption("Puck", "Male", "Upbeat"),
    VoiceOption("Charon", "Male", "Informative"),
    VoiceOption("Kore", "Female", "Firm"),
    VoiceOption("Fenrir", "Male", "Excitable"),
    VoiceOption("Leda", "Female", "Youthful"),
    VoiceOption("Orus", "Male", "Firm"),
    VoiceOption("Aoede", "Female", "Breezy"),
    VoiceOption("Callirrhoe", "Female", "Easy-going"),
    VoiceOption("Autonoe", "Female", "Bright"),
    VoiceOption("Enceladus", "Male", "Breathy"),
    VoiceOption("Iapetus", "Male", "Clear"),
    VoiceOption("Umbriel", "Male", "Easy-going"),
    VoiceOption("Algieba", "Male", "Smooth"),
    VoiceOption("Despina", "Female", "Smooth"),
    VoiceOption("Erinome", "Female", "Clear"),
    VoiceOption("Algenib", "Male", "Gravelly"),
    VoiceOption("Achernar", "Female", "Soft"),
    VoiceOption("Achird", "Male", "Friendly"),
    VoiceOption("Gacrux", "Male", "Mature"),
    VoiceOption("Laomedeia", "Female", "Upbeat"),
    VoiceOption("Sadachbia", "Female", "Lively"),
    VoiceOption("Sadaltager", "Male", "Knowledgeable"),
    VoiceOption("Schedar", "Female", "Even"),
    VoiceOption("Zubenelgenubi", "Male", "Casual"),
)

VOICE_NAME_SET: frozenset[str] = frozenset(v.name for v in AVAILABLE_VOICES)


# =============================================================================
# STRUCTURED LOGGING
# =============================================================================

class JSONFormatter(logging.Formatter):
    """JSON log formatter for structured logging."""
    
    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        
        # Add session_id if present
        if hasattr(record, "session_id"):
            log_data["session_id"] = record.session_id
        
        # Add extra fields if present
        if hasattr(record, "extra"):
            log_data.update(record.extra)
        
        # Add exception info
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        return json.dumps(log_data)


def setup_logging() -> logging.Logger:
    """Configure structured JSON logging."""
    logger = logging.getLogger("GeminiLiveBackend")
    logger.setLevel(logging.INFO)
    
    # Remove existing handlers
    logger.handlers.clear()
    
    # Console handler with JSON formatting
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logger.addHandler(handler)
    
    return logger


logger = setup_logging()


# =============================================================================
# METRICS COLLECTION
# =============================================================================

@dataclass
class Metrics:
    """In-memory metrics container."""
    
    start_time: float = field(default_factory=time.time)
    total_sessions: int = 0
    total_audio_bytes_sent: int = 0
    total_audio_bytes_received: int = 0
    total_gemini_requests: int = 0
    gemini_latencies_ms: deque = field(default_factory=lambda: deque(maxlen=100))
    
    def record_gemini_latency(self, latency_ms: float) -> None:
        """Record a Gemini API response latency."""
        self.gemini_latencies_ms.append(latency_ms)
    
    def get_latency_stats(self) -> dict:
        """Calculate latency statistics."""
        if not self.gemini_latencies_ms:
            return {"avg_ms": 0, "p95_ms": 0, "count": 0}
        
        latencies = sorted(self.gemini_latencies_ms)
        avg = sum(latencies) / len(latencies)
        p95_idx = int(len(latencies) * 0.95)
        p95 = latencies[min(p95_idx, len(latencies) - 1)]
        
        return {
            "avg_ms": round(avg, 2),
            "p95_ms": round(p95, 2),
            "count": len(latencies)
        }
    
    def to_dict(self, active_connections: int) -> dict:
        """Export all metrics as a dictionary."""
        uptime_seconds = time.time() - self.start_time
        return {
            "uptime_seconds": round(uptime_seconds, 1),
            "active_connections": active_connections,
            "total_sessions": self.total_sessions,
            "audio": {
                "bytes_sent": self.total_audio_bytes_sent,
                "bytes_received": self.total_audio_bytes_received,
            },
            "gemini": {
                "total_requests": self.total_gemini_requests,
                "latency": self.get_latency_stats(),
            }
        }


metrics = Metrics()


# =============================================================================
# TENANT STATE MANAGEMENT
# =============================================================================

class TenantState:
    """State container for a single tenant (folder)."""
    
    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        self._system_instructions: str = ""
        self._is_muted: bool = False
        self._current_voice: str = "Zephyr"  # Default voice
        self._connections: MutableMapping[WebSocket, asyncio.Queue[dict]] = {}
    
    @property
    def system_instructions(self) -> str:
        return self._system_instructions
    
    @system_instructions.setter
    def system_instructions(self, value: str) -> None:
        self._system_instructions = value
        logger.info("System instructions updated", extra={"extra": {"tenant": self.tenant_id, "length": len(value)}})
    
    @property
    def is_muted(self) -> bool:
        return self._is_muted

    @is_muted.setter
    def is_muted(self, value: bool) -> None:
        self._is_muted = value
        logger.info("Mute state changed", extra={"extra": {"tenant": self.tenant_id, "muted": value}})
    
    @property
    def current_voice(self) -> str:
        return self._current_voice
    
    @current_voice.setter
    def current_voice(self, value: str) -> None:
        if value not in VOICE_NAME_SET:
            raise ValueError(f"Invalid voice: {value}")
        self._current_voice = value
        logger.info("Voice changed", extra={"extra": {"tenant": self.tenant_id, "voice": value}})
    
    @property
    def connection_count(self) -> int:
        return len(self._connections)
    
    def register(self, websocket: WebSocket) -> asyncio.Queue[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue()
        self._connections[websocket] = queue
        metrics.total_sessions += 1
        logger.info("Connection registered", extra={"extra": {"tenant": self.tenant_id, "active": self.connection_count}})
        return queue
    
    def unregister(self, websocket: WebSocket) -> None:
        if websocket in self._connections:
            del self._connections[websocket]
            logger.info("Connection unregistered", extra={"extra": {"tenant": self.tenant_id, "active": self.connection_count}})
    
    async def broadcast(self, command: dict) -> None:
        for queue in self._connections.values():
            await queue.put(command)
    
    def load_instructions_from_file(self, path: Path, excel_path: Path | None = None) -> None:
        """Load system instructions from file, optionally appending Excel data."""
        if not path.exists():
            raise FileNotFoundError(f"Instruction file not found: {path}")
        
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            raise ValueError(f"Instruction file is empty: {path}")
        
        # Append Excel employee data if available
        excel_data = load_excel_data(excel_path or settings.excel_file)
        if excel_data:
            content = content + "\n\n" + excel_data
        
        self._system_instructions = content
        logger.info("Loaded instructions from file", extra={"extra": {"tenant": self.tenant_id, "path": str(path), "length": len(content)}})


class TenantManager:
    """Manages all tenant states."""
    
    DEFAULT_TENANT = "main"
    
    def __init__(self) -> None:
        self._tenants: dict[str, TenantState] = {}
        self._genai_client: genai.Client | None = None
    
    @property
    def genai_client(self) -> genai.Client:
        """Lazy-initialized singleton Gemini client (shared across all tenants)."""
        if self._genai_client is None:
            self._genai_client = genai.Client(api_key=settings.google_api_key)
            logger.info("Initialized Gemini client")
        return self._genai_client
    
    def get_tenant(self, tenant_id: str) -> TenantState:
        """Get or create a tenant state."""
        if tenant_id not in self._tenants:
            self._tenants[tenant_id] = TenantState(tenant_id)
            self._load_tenant_instructions(tenant_id)
        return self._tenants[tenant_id]
    
    def _load_tenant_instructions(self, tenant_id: str) -> None:
        """Load instructions for a tenant from file."""
        tenant = self._tenants[tenant_id]
        
        if tenant_id == self.DEFAULT_TENANT:
            # Main tenant uses root instructions.txt
            instruction_path = settings.instruction_file
        else:
            # Other tenants use {folder}/instructions.txt
            instruction_path = Path(tenant_id) / "instructions.txt"
        
        if instruction_path.exists():
            tenant.load_instructions_from_file(instruction_path)
        else:
            # Fall back to main instructions
            if settings.instruction_file.exists():
                tenant.load_instructions_from_file(settings.instruction_file)
                logger.info(f"Tenant {tenant_id} using main instructions (no {instruction_path})")
    
    def get_all_connection_count(self) -> int:
        """Get total connections across all tenants."""
        return sum(t.connection_count for t in self._tenants.values())
    
    def is_valid_tenant(self, tenant_id: str) -> bool:
        """Check if a tenant folder exists."""
        if tenant_id == self.DEFAULT_TENANT:
            return True
        folder = Path(tenant_id)
        return folder.is_dir() and not tenant_id.startswith(".") and tenant_id not in ("venv", "__pycache__", "static")


tenant_manager = TenantManager()


# Backward compatibility: app_state points to main tenant
@property
def _get_app_state_instructions() -> str:
    return tenant_manager.get_tenant(TenantManager.DEFAULT_TENANT).system_instructions


class ApplicationState:
    """Backward-compatible application state (delegates to main tenant)."""
    
    @property
    def system_instructions(self) -> str:
        return tenant_manager.get_tenant(TenantManager.DEFAULT_TENANT).system_instructions
    
    @system_instructions.setter
    def system_instructions(self, value: str) -> None:
        tenant_manager.get_tenant(TenantManager.DEFAULT_TENANT).system_instructions = value
    
    @property
    def is_muted(self) -> bool:
        return tenant_manager.get_tenant(TenantManager.DEFAULT_TENANT).is_muted

    @is_muted.setter
    def is_muted(self, value: bool) -> None:
        tenant_manager.get_tenant(TenantManager.DEFAULT_TENANT).is_muted = value
    
    @property
    def current_voice(self) -> str:
        return tenant_manager.get_tenant(TenantManager.DEFAULT_TENANT).current_voice
    
    @current_voice.setter
    def current_voice(self, value: str) -> None:
        tenant_manager.get_tenant(TenantManager.DEFAULT_TENANT).current_voice = value
    
    @property
    def genai_client(self) -> genai.Client:
        return tenant_manager.genai_client
    
    def load_instructions_from_file(self, path: Path) -> None:
        tenant_manager.get_tenant(TenantManager.DEFAULT_TENANT).load_instructions_from_file(path)


app_state = ApplicationState()


# =============================================================================
# CONNECTION MANAGEMENT (Backward compatible - delegates to main tenant)
# =============================================================================

class ConnectionManager:
    """Backward-compatible connection manager (delegates to main tenant)."""
    
    @property
    def connection_count(self) -> int:
        return tenant_manager.get_tenant(TenantManager.DEFAULT_TENANT).connection_count
    
    def register(self, websocket: WebSocket) -> asyncio.Queue[dict]:
        return tenant_manager.get_tenant(TenantManager.DEFAULT_TENANT).register(websocket)
    
    def unregister(self, websocket: WebSocket) -> None:
        tenant_manager.get_tenant(TenantManager.DEFAULT_TENANT).unregister(websocket)
    
    async def broadcast(self, command: dict) -> None:
        await tenant_manager.get_tenant(TenantManager.DEFAULT_TENANT).broadcast(command)


connection_manager = ConnectionManager()


# =============================================================================
# GEMINI AUDIO SESSION
# =============================================================================

class GeminiAudioSession:
    """Encapsulates a single user's audio session with Gemini Live API."""
    
    def __init__(self, websocket: WebSocket, command_queue: asyncio.Queue[dict], session_id: str, tenant: TenantState) -> None:
        self.websocket = websocket
        self.command_queue = command_queue
        self.session_id = session_id
        self.tenant = tenant
        self._client = tenant_manager.genai_client
        self._restart_event = asyncio.Event()
        self._client_disconnected = False
        self._tasks: list[asyncio.Task] = []
        self._session_start = time.time()
    
    def _log(self, level: int, msg: str, **extra) -> None:
        """Log with session correlation ID."""
        log_extra = {"session_id": self.session_id[:8]}
        if extra:
            log_extra["extra"] = extra
        logger.log(level, msg, extra=log_extra)
    
    async def run(self) -> None:
        """Main session loop with automatic reconnection on Gemini errors only."""
        while not self._client_disconnected:
            try:
                self._restart_event.clear()
                await self._run_session()
            except WebSocketDisconnect:
                self._log(logging.INFO, "Client disconnected normally")
                self._client_disconnected = True
                break
            except ExceptionGroup as eg:
                if self._is_client_disconnect(eg):
                    self._log(logging.INFO, "Client disconnected")
                    self._client_disconnected = True
                    break
                
                for exc in eg.exceptions:
                    if not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                        self._log(logging.ERROR, f"Task error: {type(exc).__name__}: {exc}")
                
                if not self._client_disconnected:
                    self._log(logging.INFO, "Reconnecting to Gemini in 1 second...")
                    await asyncio.sleep(1)
            except Exception as e:
                if self._client_disconnected:
                    break
                self._log(logging.ERROR, f"Session error: {type(e).__name__}: {e}")
                self._log(logging.INFO, "Reconnecting to Gemini in 1 second...")
                await asyncio.sleep(1)
    
    def _is_client_disconnect(self, eg: ExceptionGroup) -> bool:
        """Check if any exception in the group indicates client disconnect."""
        for exc in eg.exceptions:
            if isinstance(exc, WebSocketDisconnect):
                return True
            if isinstance(exc, RuntimeError) and "disconnect" in str(exc).lower():
                return True
        return False
    
    async def _run_session(self) -> None:
        """Execute a single Gemini session."""
        if self._client_disconnected:
            return
            
        config = self._build_config()
        self._tasks.clear()
        
        connect_start = time.time()
        async with self._client.aio.live.connect(
            model=settings.gemini_model, 
            config=config
        ) as session:
            connect_latency = (time.time() - connect_start) * 1000
            metrics.record_gemini_latency(connect_latency)
            metrics.total_gemini_requests += 1
            self._log(logging.INFO, "Connected to Gemini", latency_ms=round(connect_latency, 1))
            
            # Send current mute state to client on connect
            if self.tenant.is_muted:
                try:
                    await self.websocket.send_json({"type": "mute", "muted": True})
                except Exception:
                    pass
            
            async with asyncio.TaskGroup() as tg:
                self._tasks.append(tg.create_task(self._receive_audio(session)))
                self._tasks.append(tg.create_task(self._send_audio(session)))
                self._tasks.append(tg.create_task(self._handle_commands(session)))
    
    def _build_config(self) -> types.LiveConnectConfig:
        """Build the Gemini Live connection configuration."""
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self.tenant.current_voice
                    )
                )
            ),
            generation_config=types.GenerationConfig(
                temperature=settings.generation_temperature,
                thinking_config=types.ThinkingConfig(include_thoughts=False)
            ),
            system_instruction=types.Content(
                parts=[types.Part(text=self.tenant.system_instructions)]
            ),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    silence_duration_ms=settings.silence_duration_ms
                )
            )
        )
    
    async def _receive_audio(self, session) -> None:
        """Receive and forward audio from Gemini to the client."""
        try:
            while not self._restart_event.is_set() and not self._client_disconnected:
                turn = session.receive()
                async for response in turn:
                    if self._client_disconnected:
                        return
                    await self._process_response(response)
        except asyncio.CancelledError:
            pass
    
    async def _process_response(self, response) -> None:
        """Process a single response from Gemini."""
        if self._client_disconnected:
            return
            
        if not (response.server_content and response.server_content.model_turn):
            return
            
        if self.tenant.is_muted:
            return
        
        for part in response.server_content.model_turn.parts:
            if part.inline_data:
                try:
                    data = part.inline_data.data
                    metrics.total_audio_bytes_sent += len(data)
                    await self.websocket.send_bytes(data)
                except Exception:
                    self._client_disconnected = True
                    return
    
    async def _send_audio(self, session) -> None:
        """Forward audio from the client to Gemini."""
        try:
            while not self._restart_event.is_set() and not self._client_disconnected:
                data = await self.websocket.receive_bytes()
                metrics.total_audio_bytes_received += len(data)
                await session.send_realtime_input(
                    media=types.Blob(
                        data=data, 
                        mime_type=f"audio/pcm;rate={settings.sample_rate}"
                    )
                )
        except WebSocketDisconnect:
            self._client_disconnected = True
            raise
        except asyncio.CancelledError:
            pass
    
    async def _handle_commands(self, session) -> None:
        """Process admin commands from the command queue."""
        try:
            while not self._restart_event.is_set() and not self._client_disconnected:
                command = await self._wait_for_command()
                if command is None:
                    return
                await self._execute_command(command, session)
        except asyncio.CancelledError:
            pass
    
    async def _wait_for_command(self) -> dict | None:
        """Wait for either a command or restart signal."""
        cmd_task = asyncio.create_task(self.command_queue.get())
        restart_task = asyncio.create_task(self._restart_event.wait())
        tasks = [cmd_task, restart_task]
        
        try:
            done, _ = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED
            )
            
            if restart_task in done:
                return None
            
            return cmd_task.result()
        finally:
            # Always clean up tasks, even if we're cancelled by TaskGroup
            for task in tasks:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
    
    async def _execute_command(self, command: dict, session) -> None:
        """Execute a single admin command."""
        cmd_type = command.get("type")
        
        if cmd_type == "kill":
            await self._handle_kill_command()
        elif cmd_type == "speak":
            await self._handle_speak_command(command.get("text", ""), session)
        elif cmd_type == "mute":
            await self._handle_mute_command(command.get("muted", False))
        elif cmd_type == "restart":
            await self._handle_restart_command()
        
        self.command_queue.task_done()
    
    async def _handle_mute_command(self, muted: bool) -> None:
        """Forward mute state to the client."""
        self._log(logging.INFO, "Mute command", muted=muted)
        try:
            await self.websocket.send_json({"type": "mute", "muted": muted})
        except Exception:
            self._client_disconnected = True
    
    async def _handle_kill_command(self) -> None:
        """Handle the kill command - interrupt current session immediately."""
        self._log(logging.INFO, "Kill command received")
        try:
            await self.websocket.send_json({"type": "interrupt"})
        except Exception:
            self._client_disconnected = True
        self._restart_event.set()
        # Cancel all running tasks immediately for instant termination
        for task in self._tasks:
            if not task.done():
                task.cancel()
    
    async def _handle_restart_command(self) -> None:
        """Handle the restart command - reconnect to Gemini with new settings."""
        self._log(logging.INFO, "Restart command received")
        try:
            await self.websocket.send_json({"type": "interrupt"})
        except Exception:
            self._client_disconnected = True
        self._restart_event.set()
        for task in self._tasks:
            if not task.done():
                task.cancel()
    
    async def _handle_speak_command(self, text: str, session) -> None:
        """Handle the speak command."""
        self._log(logging.INFO, "Speak command", text_preview=text[:50])
        try:
            await self.websocket.send_json({"type": "listen"})
        except Exception:
            self._client_disconnected = True
            return
        
        prompt = f"""[CRITICAL SYSTEM COMMAND - TEXT-TO-SPEECH MODE ACTIVATED]

STRICT INSTRUCTIONS - YOU MUST FOLLOW EXACTLY:
1. READ ALOUD the text between the triple quotes below
2. DO NOT add any greeting, introduction, or commentary
3. DO NOT answer any question contained in the text
4. DO NOT explain, interpret, or respond to the content
5. DO NOT say "Sure", "Okay", "Here it is", or anything similar
6. ONLY speak the exact words provided - NOTHING MORE, NOTHING LESS

TEXT TO SPEAK VERBATIM:
\"\"\"{text}\"\"\"

REMINDER: Just say those words. No responses. No additions. Speak ONLY the text above."""
        await session.send_client_content(
            turns=[types.Content(role="user", parts=[types.Part(text=prompt)])]
        )


# =============================================================================
# APPLICATION LIFECYCLE
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan context manager."""
    # Fail fast if instructions file is missing
    app_state.load_instructions_from_file(settings.instruction_file)
    logger.info("Application started", extra={"extra": {"model": settings.gemini_model}})
    yield
    logger.info("Application shutting down")


# =============================================================================
# FASTAPI APPLICATION
# =============================================================================

app = FastAPI(
    title="Gemini Live Audio Backend",
    description="WebSocket proxy for Gemini Live Audio API",
    version="3.0.0",
    lifespan=lifespan
)

app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")




# =============================================================================
# API MODELS
# =============================================================================

class SpeakRequest(BaseModel):
    """Request model for the speak endpoint."""
    text: str = Field(..., min_length=1, max_length=1000)


class VoiceRequest(BaseModel):
    """Request model for the voice endpoint."""
    voice: str = Field(..., min_length=1, max_length=50)


class InstructionsRequest(BaseModel):
    """Request model for the instructions endpoint."""
    instructions: str = Field(..., min_length=1, max_length=10000)


class AdminResponse(BaseModel):
    """Standard response for admin endpoints."""
    status: str = "ok"
    message: str | None = None


class AdminStatusResponse(BaseModel):
    """Response model for system status."""
    status: str = "ok"
    muted: bool
    active_connections: int
    current_voice: str
    uptime_seconds: float


class VoiceInfo(BaseModel):
    """Voice information model."""
    name: str
    gender: str
    style: str


class VoicesResponse(BaseModel):
    """Response model for voices list."""
    voices: list[VoiceInfo]
    current: str


class InstructionsResponse(BaseModel):
    """Response model for instructions endpoint."""
    status: str = "ok"
    instructions: str


class HealthResponse(BaseModel):
    """Response model for health check."""
    status: str = "ok"


# =============================================================================
# HEALTH & METRICS ENDPOINTS
# =============================================================================

@app.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Simple health check endpoint for deployment monitoring."""
    return HealthResponse()


@app.get("/metrics")
async def get_metrics() -> JSONResponse:
    """Get current system metrics."""
    return JSONResponse(content=metrics.to_dict(connection_manager.connection_count))


# =============================================================================
# WEBSOCKET ENDPOINTS
# =============================================================================

@app.websocket("/ws/audio")
async def audio_websocket(websocket: WebSocket) -> None:
    """WebSocket endpoint for bidirectional audio streaming (main tenant)."""
    await websocket.accept()
    session_id = str(uuid.uuid4())
    tenant = tenant_manager.get_tenant(TenantManager.DEFAULT_TENANT)
    logger.info("New WebSocket connection", extra={"session_id": session_id[:8], "extra": {"tenant": "main"}})
    
    command_queue = tenant.register(websocket)
    session = GeminiAudioSession(websocket, command_queue, session_id, tenant)
    
    try:
        await session.run()
    finally:
        tenant.unregister(websocket)
        logger.info("Session ended", extra={"session_id": session_id[:8]})


@app.websocket("/{tenant_id}/ws/audio")
async def tenant_audio_websocket(websocket: WebSocket, tenant_id: str) -> None:
    """WebSocket endpoint for bidirectional audio streaming (tenant-specific)."""
    # Validate tenant
    if not tenant_manager.is_valid_tenant(tenant_id):
        await websocket.close(code=4004, reason="Invalid tenant")
        return
    
    await websocket.accept()
    session_id = str(uuid.uuid4())
    tenant = tenant_manager.get_tenant(tenant_id)
    logger.info("New WebSocket connection", extra={"session_id": session_id[:8], "extra": {"tenant": tenant_id}})
    
    command_queue = tenant.register(websocket)
    session = GeminiAudioSession(websocket, command_queue, session_id, tenant)
    
    try:
        await session.run()
    finally:
        tenant.unregister(websocket)
        logger.info("Session ended", extra={"session_id": session_id[:8]})


# =============================================================================
# ADMIN ENDPOINTS
# =============================================================================

@app.post("/admin/kill", response_model=AdminResponse)
async def admin_kill() -> AdminResponse:
    """Interrupt all active sessions."""
    await connection_manager.broadcast({"type": "kill"})
    return AdminResponse(message=f"Kill sent to {connection_manager.connection_count} clients")


@app.post("/admin/speak", response_model=AdminResponse)
async def admin_speak(request: SpeakRequest) -> AdminResponse:
    """Make all connected robots speak the specified text."""
    await connection_manager.broadcast({"type": "speak", "text": request.text})
    return AdminResponse(message=f"Speak command sent to {connection_manager.connection_count} clients")


@app.post("/admin/mute", response_model=AdminStatusResponse)
async def admin_toggle_mute() -> AdminStatusResponse:
    """Toggle the global mute state."""
    app_state.is_muted = not app_state.is_muted
    # Broadcast mute state to all connected clients
    await connection_manager.broadcast({"type": "mute", "muted": app_state.is_muted})
    return AdminStatusResponse(
        muted=app_state.is_muted,
        active_connections=connection_manager.connection_count,
        current_voice=app_state.current_voice,
        uptime_seconds=round(time.time() - metrics.start_time, 1)
    )


@app.get("/admin/status", response_model=AdminStatusResponse)
async def admin_status() -> AdminStatusResponse:
    """Get current system status."""
    return AdminStatusResponse(
        muted=app_state.is_muted,
        active_connections=connection_manager.connection_count,
        current_voice=app_state.current_voice,
        uptime_seconds=round(time.time() - metrics.start_time, 1)
    )


@app.get("/admin/voices", response_model=VoicesResponse)
async def admin_get_voices() -> VoicesResponse:
    """Get all available voices."""
    return VoicesResponse(
        voices=[VoiceInfo(name=v.name, gender=v.gender, style=v.style) for v in AVAILABLE_VOICES],
        current=app_state.current_voice
    )


@app.post("/admin/voice", response_model=AdminResponse)
async def admin_set_voice(request: VoiceRequest) -> AdminResponse:
    """Set the active voice for all sessions."""
    if request.voice not in VOICE_NAME_SET:
        return AdminResponse(status="error", message=f"Invalid voice: {request.voice}")
    
    app_state.current_voice = request.voice
    # Restart all sessions to use the new voice
    await connection_manager.broadcast({"type": "restart"})
    return AdminResponse(message=f"Voice changed to {request.voice}, restarting {connection_manager.connection_count} sessions")


@app.get("/admin/instructions", response_model=InstructionsResponse)
async def admin_get_instructions() -> InstructionsResponse:
    """Get current system instructions."""
    return InstructionsResponse(instructions=app_state.system_instructions)


@app.post("/admin/instructions", response_model=AdminResponse)
async def admin_set_instructions(request: InstructionsRequest) -> AdminResponse:
    """Update system instructions for all sessions."""
    app_state.system_instructions = request.instructions
    # Save to file for persistence
    settings.instruction_file.write_text(request.instructions, encoding="utf-8")
    # Restart all sessions to use the new instructions
    await connection_manager.broadcast({"type": "restart"})
    return AdminResponse(message=f"Instructions updated, restarting {connection_manager.connection_count} sessions")


# =============================================================================
# TENANT-SPECIFIC ADMIN ENDPOINTS
# =============================================================================

def _get_tenant_or_404(tenant_id: str) -> TenantState:
    """Get tenant or raise 404."""
    from fastapi import HTTPException
    if not tenant_manager.is_valid_tenant(tenant_id):
        raise HTTPException(status_code=404, detail=f"Tenant not found: {tenant_id}")
    return tenant_manager.get_tenant(tenant_id)


@app.post("/{tenant_id}/admin/kill", response_model=AdminResponse)
async def tenant_admin_kill(tenant_id: str) -> AdminResponse:
    """Interrupt all active sessions for a specific tenant."""
    tenant = _get_tenant_or_404(tenant_id)
    await tenant.broadcast({"type": "kill"})
    return AdminResponse(message=f"Kill sent to {tenant.connection_count} clients")


@app.post("/{tenant_id}/admin/speak", response_model=AdminResponse)
async def tenant_admin_speak(tenant_id: str, request: SpeakRequest) -> AdminResponse:
    """Make all connected robots speak the specified text for a specific tenant."""
    tenant = _get_tenant_or_404(tenant_id)
    await tenant.broadcast({"type": "speak", "text": request.text})
    return AdminResponse(message=f"Speak command sent to {tenant.connection_count} clients")


@app.post("/{tenant_id}/admin/mute", response_model=AdminStatusResponse)
async def tenant_admin_toggle_mute(tenant_id: str) -> AdminStatusResponse:
    """Toggle the mute state for a specific tenant."""
    tenant = _get_tenant_or_404(tenant_id)
    tenant.is_muted = not tenant.is_muted
    await tenant.broadcast({"type": "mute", "muted": tenant.is_muted})
    return AdminStatusResponse(
        muted=tenant.is_muted,
        active_connections=tenant.connection_count,
        current_voice=tenant.current_voice,
        uptime_seconds=round(time.time() - metrics.start_time, 1)
    )


@app.get("/{tenant_id}/admin/status", response_model=AdminStatusResponse)
async def tenant_admin_status(tenant_id: str) -> AdminStatusResponse:
    """Get current system status for a specific tenant."""
    tenant = _get_tenant_or_404(tenant_id)
    return AdminStatusResponse(
        muted=tenant.is_muted,
        active_connections=tenant.connection_count,
        current_voice=tenant.current_voice,
        uptime_seconds=round(time.time() - metrics.start_time, 1)
    )


@app.get("/{tenant_id}/admin/voices", response_model=VoicesResponse)
async def tenant_admin_get_voices(tenant_id: str) -> VoicesResponse:
    """Get all available voices for a specific tenant."""
    tenant = _get_tenant_or_404(tenant_id)
    return VoicesResponse(
        voices=[VoiceInfo(name=v.name, gender=v.gender, style=v.style) for v in AVAILABLE_VOICES],
        current=tenant.current_voice
    )


@app.post("/{tenant_id}/admin/voice", response_model=AdminResponse)
async def tenant_admin_set_voice(tenant_id: str, request: VoiceRequest) -> AdminResponse:
    """Set the active voice for a specific tenant."""
    tenant = _get_tenant_or_404(tenant_id)
    if request.voice not in VOICE_NAME_SET:
        return AdminResponse(status="error", message=f"Invalid voice: {request.voice}")
    
    tenant.current_voice = request.voice
    await tenant.broadcast({"type": "restart"})
    return AdminResponse(message=f"Voice changed to {request.voice}, restarting {tenant.connection_count} sessions")


@app.get("/{tenant_id}/admin/instructions", response_model=InstructionsResponse)
async def tenant_admin_get_instructions(tenant_id: str) -> InstructionsResponse:
    """Get current system instructions for a specific tenant."""
    tenant = _get_tenant_or_404(tenant_id)
    return InstructionsResponse(instructions=tenant.system_instructions)


@app.post("/{tenant_id}/admin/instructions", response_model=AdminResponse)
async def tenant_admin_set_instructions(tenant_id: str, request: InstructionsRequest) -> AdminResponse:
    """Update system instructions for a specific tenant."""
    tenant = _get_tenant_or_404(tenant_id)
    tenant.system_instructions = request.instructions
    # Save to tenant's instruction file for persistence
    tenant_instruction_file = Path(tenant_id) / "instructions.txt"
    tenant_instruction_file.write_text(request.instructions, encoding="utf-8")
    await tenant.broadcast({"type": "restart"})
    return AdminResponse(message=f"Instructions updated, restarting {tenant.connection_count} sessions")


# =============================================================================
# IMAGE DISPLAY STATE
# =============================================================================

class ImageState:
    """Tracks the current image to display on index page."""
    def __init__(self):
        self.url: str | None = None
        self.expires_at: float = 0

    def set(self, url: str, duration: float = 15.0):
        self.url = url
        self.expires_at = time.time() + duration

    def get(self) -> str | None:
        if self.url and time.time() < self.expires_at:
            return self.url
        self.url = None
        return None


image_state = ImageState()


class ImageRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2000)


@app.post("/admin/show-image", response_model=AdminResponse)
async def admin_show_image(request: ImageRequest) -> AdminResponse:
    """Send an image URL to be displayed on the index page for 15 seconds."""
    image_state.set(request.url, duration=15.0)
    return AdminResponse(message="تم إرسال الصورة بنجاح")


@app.get("/admin/current-image")
async def admin_current_image() -> JSONResponse:
    """Return the current active image URL (polled by index page)."""
    url = image_state.get()
    return JSONResponse(content={"url": url})


@app.post("/admin/upload-excel", response_model=AdminResponse)
async def admin_upload_excel(file: UploadFile = File(...)) -> AdminResponse:
    """Upload a new Excel file and reload employee data into the bot instructions."""
    if not file.filename.endswith((".xlsx", ".xls")):
        return AdminResponse(status="error", message="يجب أن يكون الملف بصيغة .xlsx أو .xls")
    
    try:
        contents = await file.read()
        # Save the uploaded file
        settings.excel_file.write_bytes(contents)
        
        # Reload instructions with new Excel data for main tenant
        tenant = tenant_manager.get_tenant(TenantManager.DEFAULT_TENANT)
        tenant.load_instructions_from_file(settings.instruction_file, settings.excel_file)
        
        # Restart all sessions to pick up new data
        await connection_manager.broadcast({"type": "restart"})
        
        # Count rows for feedback
        df = pd.read_excel(io.BytesIO(contents))
        return AdminResponse(message=f"تم رفع الملف بنجاح وتحميل {len(df)} موظف. جاري إعادة تشغيل الجلسات.")
    except Exception as e:
        logger.error(f"Excel upload failed: {e}")
        return AdminResponse(status="error", message=f"فشل رفع الملف: {str(e)}")


# =============================================================================
# STATIC FILE ENDPOINTS
# =============================================================================

@app.get("/")
async def serve_index() -> FileResponse:
    """Serve the main application page."""
    return FileResponse("index.html")


@app.get("/admin")
async def serve_admin() -> FileResponse:
    """Serve the admin control panel."""
    return FileResponse("admin.html")


@app.get("/manifest.json")
async def serve_manifest() -> FileResponse:
    """Serve the PWA manifest file."""
    return FileResponse("manifest.json")


@app.get("/favicon.ico")
async def serve_favicon() -> FileResponse:
    """Serve the favicon using the logo image."""
    return FileResponse("static/sm.png", media_type="image/png")


# =============================================================================
# TENANT ADMIN PANEL (Dynamic - serves admin.html with tenant context)
# =============================================================================

from fastapi.responses import HTMLResponse

@app.get("/{tenant_id}/admin")
async def serve_tenant_admin(tenant_id: str) -> HTMLResponse:
    """Serve the admin control panel for a specific tenant."""
    from fastapi import HTTPException
    
    # Validate tenant
    if not tenant_manager.is_valid_tenant(tenant_id):
        raise HTTPException(status_code=404, detail="Tenant not found")
    
    # Read the base admin.html and inject tenant context
    admin_path = Path("admin.html")
    if not admin_path.exists():
        raise HTTPException(status_code=404, detail="Admin template not found")
    
    html_content = admin_path.read_text(encoding="utf-8")
    
    # Inject tenant ID into the page via a script tag
    tenant_script = f'''<script>window.TENANT_ID = "{tenant_id}";</script>'''
    
    # Insert before the closing </head> tag
    html_content = html_content.replace("</head>", f"{tenant_script}\n</head>")
    
    # Update the title to include tenant name
    html_content = html_content.replace(
        "<title>Robot Admin</title>",
        f"<title>{tenant_id.upper()} Robot Admin</title>"
    )
    
    return HTMLResponse(content=html_content)


# =============================================================================
# DYNAMIC STATIC FILE SERVING (CATCH-ALL - MUST BE LAST)
# =============================================================================

@app.get("/{file_path:path}")
async def serve_dynamic_file(file_path: str):
    """
    Catch-all route to serve files securely from subdirectories in the project root.
    
    IMPORTANT: This route MUST be defined last to avoid intercepting specific routes.
    
    Security Rules:
    1. NO serving files from the project root (e.g. main.py, .env).
    2. NO serving files from hidden directories (e.g. .git/).
    3. NO serving files from system directories (e.g. venv/, __pycache__/).
    4. ONLY serve files if they are inside a valid subdirectory (e.g. ksu/index.html).
    """
    from fastapi import HTTPException
    
    # Resolve the path relative to the project root
    project_root = Path(".").resolve()
    requested_path = (project_root / file_path).resolve()
    
    # Security Check 1: Ensure path is within project root
    if not str(requested_path).startswith(str(project_root)):
        raise HTTPException(status_code=404, detail="File not found")
    
    # Get the relative path parts
    try:
        relative_path = requested_path.relative_to(project_root)
    except ValueError:
        raise HTTPException(status_code=404, detail="File not found")
        
    parts = relative_path.parts
    
    # Security Check 2: Block root-level FILES
    # Allow root-level DIRECTORIES (to serve their index.html)
    # e.g. "ksu" -> parts=("ksu",) -> OK (if directory)
    # e.g. "main.py" -> parts=("main.py") -> BLOCKED (if file)
    if len(parts) < 2 and requested_path.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    # Security Check 3: Block hidden and system directories
    top_level_dir = parts[0]
    if (
        top_level_dir.startswith(".") or 
        top_level_dir in ("venv", "__pycache__", "static", "__pycache__")
    ):
        raise HTTPException(status_code=404, detail="Not found")

    # If it's a directory, look for index.html and inject tenant context
    if requested_path.is_dir():
        index_path = requested_path / "index.html"
        if index_path.exists() and index_path.is_file():
            # Check if this is a tenant folder (first-level directory)
            if len(parts) == 1 and tenant_manager.is_valid_tenant(parts[0]):
                # Inject tenant ID into the HTML
                tenant_id = parts[0]
                html_content = index_path.read_text(encoding="utf-8")
                tenant_script = f'<script>window.TENANT_ID = "{tenant_id}";</script>'
                html_content = html_content.replace("</head>", f"{tenant_script}\n</head>")
                return HTMLResponse(content=html_content)
            return FileResponse(index_path)
        raise HTTPException(status_code=404, detail="File not found")

    # If it's a file, serve it
    if requested_path.exists() and requested_path.is_file():
        return FileResponse(requested_path)
    
    # Try adding .html extension (e.g., /ksu/admin -> /ksu/admin.html)
    html_path = requested_path.with_suffix(".html")
    if html_path.exists() and html_path.is_file():
        # Re-check security: ensure .html file is not at root level
        html_relative = html_path.relative_to(project_root)
        if len(html_relative.parts) >= 2:
            return FileResponse(html_path)
        
    raise HTTPException(status_code=404, detail="File not found")
