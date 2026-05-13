/**
 * Robot Voice Interface - Main Application
 * 
 * Real-time voice interaction client for Gemini Live API.
 * Handles audio capture, WebSocket communication, and visualization.
 * 
 * @version 3.5.0 - Multi-tenant support
 * @author Smart Methods
 */

'use strict';

// Get tenant ID from injected window variable (set by server for tenant pages)
const TENANT_ID = window.TENANT_ID || null;

// =============================================================================
// CONFIGURATION
// =============================================================================

const CONFIG = Object.freeze({
    // Audio Settings
    SAMPLE_RATE: 24000,
    BUFFER_SIZE: 128,
    PCM_MAX_VALUE: 32768,

    // Playback Settings
    AUDIO_DELAY_BUFFER_MS: 150,

    // Visualizer Settings
    FFT_SIZE: 2048,
    SMOOTHING_CONSTANT: 0.85,

    // Audio Analysis
    VOICE_THRESHOLD: 500,
    DB_FLOOR: -60,
    RMS_BOOST_FACTOR: 1.5,
    MIC_SENSITIVITY_REDUCTION: 0.5,
    AMPLITUDE_CURVE_POWER: 0.8,
    MIN_RMS_VALUE: 0.0001,

    // Byte audio constants (for AnalyserNode output which is 0-255)
    BYTE_AUDIO_CENTER: 128,

    // Animation
    AMPLITUDE_DECAY_RATE: 0.95,
    MIN_IDLE_AMPLITUDE: 0.08,
    AMPLITUDE_MULTIPLIER: 2.0,
    MUTE_AMPLITUDE: 0.15,

    // Interpolation
    ROBOT_LERP_FACTOR: 0.1,
    MIC_LERP_FACTOR: 0.15,

    // WebSocket Settings
    RECONNECT_DELAY_MS: 1000,

    // UI Settings
    FULLSCREEN_HIDE_DELAY_MS: 10000,

    // Wave Drawing
    WAVE_LINE_WIDTH: 2,
    WAVE_STEP_SIZE: 5,
    WAVE_HEIGHT_MULTIPLIER: 50,

    // Wave Configuration
    WAVES: Object.freeze([
        { color: '#00f2ff', opacity: 1.0, speed: 0.5, offset: 0 },
        { color: '#0077ff', opacity: 0.8, speed: 0.8, offset: 10 },
        { color: '#bd00ff', opacity: 0.6, speed: 1.2, offset: 20 }
    ])
});

// =============================================================================
// APPLICATION STATE
// =============================================================================

const audioState = {
    context: null,
    stream: null,
    inputSource: null,
    processor: null,
    outputAnalyser: null,
    nextStartTime: 0,
    scheduledSources: []
};

const visualizerState = {
    isRobotSpeaking: false,
    currentAmplitude: 0
};

/**
 * WebSocket connection state.
 */
const connectionState = {
    websocket: null,
    ignoreIncomingAudio: false,
    isMuted: false  // Tracks global mute from admin panel
};

// =============================================================================
// DOM ELEMENTS
// =============================================================================

const elements = Object.freeze({
    visualizer: document.getElementById('visualizer'),
    fullscreenBtn: document.getElementById('fullscreenBtn')
});

const canvasCtx = elements.visualizer?.getContext('2d');

// =============================================================================
// UTILITY FUNCTIONS
// =============================================================================

/**
 * Linear interpolation between two values.
 * @param {number} start - Starting value
 * @param {number} end - Target value  
 * @param {number} factor - Interpolation factor (0-1)
 * @returns {number} Interpolated value
 */
const lerp = (start, end, factor) => start + (end - start) * factor;

/**
 * Calculate RMS (Root Mean Square) from samples.
 * @param {Uint8Array|Int16Array} samples - Audio samples
 * @param {number} normalize - Normalization factor
 * @param {number} offset - Value to subtract from samples
 * @returns {number} RMS value
 */
function calculateRMS(samples, normalize = 1, offset = 0) {
    let sum = 0;
    for (let i = 0; i < samples.length; i++) {
        const amplitude = (samples[i] - offset) / normalize;
        sum += amplitude * amplitude;
    }
    return Math.sqrt(sum / samples.length);
}

/**
 * Calculate average amplitude from PCM data.
 * @param {Int16Array} pcmData - PCM audio samples
 * @returns {number} Average absolute amplitude
 */
function calculateAverageAmplitude(pcmData) {
    let sum = 0;
    for (let i = 0; i < pcmData.length; i++) {
        sum += Math.abs(pcmData[i]);
    }
    return sum / pcmData.length;
}

// =============================================================================
// INITIALIZATION
// =============================================================================

// Use DOMContentLoaded for faster startup (fires before images finish loading)
document.addEventListener('DOMContentLoaded', () => {
    initializeApplication();
    initializeFullscreenButton();
});

async function initializeApplication() {
    console.log('Starting application...');

    // Initialize audio and connect WebSocket in parallel for fastest startup
    initializeAudioContext();
    connectWebSocket();

    // Request microphone access immediately (shows prompt ASAP)
    startAudioCapture();

    // STT disabled - causes audio mode switching bug on mobile
    // initializeSpeechRecognition();
}

/**
 * Initialize audio context and analyser.
 * Called after WebSocket connects to avoid blocking page load.
 */
async function initializeAudioContext() {
    if (audioState.context) return; // Already initialized

    try {
        console.log('Initializing audio context...');

        audioState.context = new AudioContext({
            sampleRate: CONFIG.SAMPLE_RATE
        });

        // Listen for AudioContext state changes (e.g., headphones unplugged)
        audioState.context.onstatechange = handleAudioContextStateChange;

        audioState.outputAnalyser = audioState.context.createAnalyser();
        audioState.outputAnalyser.fftSize = CONFIG.FFT_SIZE;
        audioState.outputAnalyser.smoothingTimeConstant = CONFIG.SMOOTHING_CONSTANT;
        audioState.outputAnalyser.connect(audioState.context.destination);

        console.log('Audio context initialized successfully');
    } catch (error) {
        console.error('Audio context initialization error:', error);
    }
}

/**
 * Handle AudioContext state changes for resilience.
 * Auto-recovers if the context is suspended (e.g., audio device change).
 */
function handleAudioContextStateChange() {
    const state = audioState.context?.state;
    console.log('AudioContext state changed:', state);

    if (state === 'suspended') {
        console.log('AudioContext suspended, attempting to resume...');
        audioState.context.resume().then(() => {
            console.log('AudioContext resumed successfully');
        }).catch((error) => {
            console.error('Failed to resume AudioContext:', error);
        });
    }
}

function initializeFullscreenButton() {
    const btn = elements.fullscreenBtn;
    if (!btn) return;

    setTimeout(() => {
        btn.style.opacity = '0';
        btn.style.pointerEvents = 'none';
    }, CONFIG.FULLSCREEN_HIDE_DELAY_MS);

    btn.addEventListener('click', async () => {
        try {
            if (!document.fullscreenElement) {
                await document.documentElement.requestFullscreen();
            } else {
                await document.exitFullscreen();
            }
        } catch (error) {
            console.error('Fullscreen error:', error);
        }
    });
}

// =============================================================================
// WEBSOCKET COMMUNICATION
// =============================================================================

function connectWebSocket() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsPath = TENANT_ID ? `/${TENANT_ID}/ws/audio` : '/ws/audio';
    const wsUrl = `${protocol}//${location.host}${wsPath}`;

    console.log('Connecting to WebSocket:', wsUrl);
    connectionState.websocket = new WebSocket(wsUrl);

    // Receive binary data directly as ArrayBuffer (no Blob conversion needed)
    connectionState.websocket.binaryType = 'arraybuffer';

    connectionState.websocket.onopen = handleWebSocketOpen;
    connectionState.websocket.onmessage = handleWebSocketMessage;
    connectionState.websocket.onclose = handleWebSocketClose;
    connectionState.websocket.onerror = (error) => {
        console.error('WebSocket error:', error);
        connectionState.websocket?.close();
    };
}

function handleWebSocketOpen() {
    console.log('WebSocket connected');
    drawOutputVisualizer();
}

function handleWebSocketMessage(event) {
    try {
        // With binaryType='arraybuffer', binary data comes as ArrayBuffer directly
        if (event.data instanceof ArrayBuffer) {
            // if (connectionState.ignoreIncomingAudio) return; // Removed for lower latency/barge-in support
            playAudioChunk(event.data);
        } else {
            const message = JSON.parse(event.data);
            if (message.type === 'interrupt') {
                console.log('Received interrupt signal');
                stopAudio();
            } else if (message.type === 'listen') {
                console.log('Received listen signal');
                connectionState.ignoreIncomingAudio = false;
            } else if (message.type === 'mute') {
                connectionState.isMuted = message.muted;
                console.log('Mute state changed:', message.muted);
                if (message.muted) {
                    // Freeze visualizer when muted
                    visualizerState.currentAmplitude = 0;
                }
            }
        }
    } catch (error) {
        console.error('Error processing message:', error);
    }
}

function handleWebSocketClose() {
    console.log('WebSocket disconnected. Reconnecting...');
    stopAudio();
    setTimeout(connectWebSocket, CONFIG.RECONNECT_DELAY_MS);
}

// =============================================================================
// AUDIO CAPTURE
// =============================================================================

async function startAudioCapture() {
    try {
        audioState.stream = await navigator.mediaDevices.getUserMedia({
            audio: {
                echoCancellation: false,
                noiseSuppression: false,
                autoGainControl: false
            }
        });

        if (audioState.context.state === 'suspended') {
            await audioState.context.resume();
        }

        audioState.inputSource = audioState.context.createMediaStreamSource(audioState.stream);
        await setupAudioWorklet();
    } catch (error) {
        console.error('Audio capture error:', error);
    }
}

async function setupAudioWorklet() {
    // Use external worklet file for better maintainability
    await audioState.context.audioWorklet.addModule('/static/pcm-processor.js');

    const workletNode = new AudioWorkletNode(audioState.context, 'pcm-processor');
    workletNode.port.onmessage = handleAudioWorkletMessage;

    audioState.inputSource.connect(workletNode);
    workletNode.connect(audioState.context.destination);
    audioState.processor = workletNode;
}

function handleAudioWorkletMessage(event) {
    const { websocket } = connectionState;
    if (!websocket || websocket.readyState !== WebSocket.OPEN) return;

    // Prevent loopback - don't send when robot is speaking
    if (audioState.scheduledSources.length > 0) return;

    const pcmData = event.data;

    // Check for voice activity to reset ignore flag
    if (calculateAverageAmplitude(pcmData) > CONFIG.VOICE_THRESHOLD) {
        connectionState.ignoreIncomingAudio = false;
    }

    websocket.send(pcmData);

    // Visualize microphone input when robot is silent
    if (!visualizerState.isRobotSpeaking) {
        updateMicVisualization(pcmData);
    }
}

// =============================================================================
// AUDIO PLAYBACK
// =============================================================================

function playAudioChunk(arrayBuffer) {
    const int16Data = new Int16Array(arrayBuffer);
    const float32Data = new Float32Array(int16Data.length);

    for (let i = 0; i < int16Data.length; i++) {
        float32Data[i] = int16Data[i] / CONFIG.PCM_MAX_VALUE;
    }

    const audioBuffer = audioState.context.createBuffer(
        1,
        float32Data.length,
        CONFIG.SAMPLE_RATE
    );
    audioBuffer.getChannelData(0).set(float32Data);

    const source = audioState.context.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(audioState.outputAnalyser);

    const currentTime = audioState.context.currentTime;
    if (audioState.nextStartTime < currentTime) {
        audioState.nextStartTime = currentTime;
    }

    source.start(audioState.nextStartTime);
    audioState.nextStartTime += audioBuffer.duration;
    visualizerState.isRobotSpeaking = true;

    audioState.scheduledSources.push(source);
    source.onended = () => handleSourceEnded(source);
}

function handleSourceEnded(source) {
    // Disconnect the source node to allow garbage collection
    try {
        source.disconnect();
    } catch (error) {
        // Already disconnected - this is expected for some edge cases
        console.debug('Source already disconnected:', error.message);
    }

    setTimeout(() => {
        audioState.scheduledSources = audioState.scheduledSources.filter(s => s !== source);

        if (audioState.scheduledSources.length === 0) {
            visualizerState.isRobotSpeaking = false;
            audioState.nextStartTime = audioState.context.currentTime;
        }
    }, CONFIG.AUDIO_DELAY_BUFFER_MS);
}

function stopAudio() {
    // Prevent ghost callbacks by nullifying onended before stopping
    for (const source of audioState.scheduledSources) {
        source.onended = null;
        try {
            source.stop();
            source.disconnect();
        } catch (error) {
            // Expected for already-stopped sources
            console.debug('Error stopping source:', error.message);
        }
    }
    audioState.scheduledSources = [];

    audioState.nextStartTime = audioState.context?.currentTime ?? 0;
    visualizerState.isRobotSpeaking = false;
    visualizerState.currentAmplitude = 0;
    connectionState.ignoreIncomingAudio = true;
}

// =============================================================================
// VISUALIZATION
// =============================================================================

function drawOutputVisualizer() {
    if (visualizerState.isRobotSpeaking && audioState.outputAnalyser) {
        const dataArray = new Uint8Array(audioState.outputAnalyser.frequencyBinCount);
        audioState.outputAnalyser.getByteTimeDomainData(dataArray);

        const rms = calculateRMS(dataArray, CONFIG.BYTE_AUDIO_CENTER, CONFIG.BYTE_AUDIO_CENTER);
        const boostedRMS = rms * CONFIG.RMS_BOOST_FACTOR;

        visualizerState.currentAmplitude = lerp(
            visualizerState.currentAmplitude,
            boostedRMS,
            CONFIG.ROBOT_LERP_FACTOR
        );
    } else if (visualizerState.currentAmplitude > CONFIG.MIN_IDLE_AMPLITUDE) {
        // Gentle decay when silent - this is the ONLY thing that reduces amplitude
        visualizerState.currentAmplitude *= CONFIG.AMPLITUDE_DECAY_RATE;
    }

    renderWaves(visualizerState.currentAmplitude);
    requestAnimationFrame(drawOutputVisualizer);
}

function updateMicVisualization(pcmData) {
    // Don't update visualizer when muted from admin panel
    if (connectionState.isMuted) return;

    // Calculate RMS using CONFIG values
    let sum = 0;
    for (let i = 0; i < pcmData.length; i++) {
        const amplitude = pcmData[i] / CONFIG.PCM_MAX_VALUE;
        sum += amplitude * amplitude;
    }
    const rms = Math.sqrt(sum / pcmData.length);

    // Convert to decibels using CONFIG
    const db = 20 * Math.log10(Math.max(rms, CONFIG.MIN_RMS_VALUE));

    // Map dB range to [0, 1] using CONFIG.DB_FLOOR
    const dbRange = -CONFIG.DB_FLOOR;
    let targetAmplitude = (db - CONFIG.DB_FLOOR) / dbRange;
    targetAmplitude = Math.max(0, Math.min(1, targetAmplitude));

    // Apply curve for better visual response
    targetAmplitude = Math.pow(targetAmplitude, CONFIG.AMPLITUDE_CURVE_POWER);
    targetAmplitude *= CONFIG.MIC_SENSITIVITY_REDUCTION;

    // Only lerp UP to louder sounds - decay handles the fade out
    if (targetAmplitude > visualizerState.currentAmplitude) {
        visualizerState.currentAmplitude = lerp(
            visualizerState.currentAmplitude,
            targetAmplitude,
            CONFIG.MIC_LERP_FACTOR
        );
    }
}

function renderWaves(amplitude) {
    if (!canvasCtx || !elements.visualizer) return;

    const { width, height } = elements.visualizer;
    canvasCtx.clearRect(0, 0, width, height);

    // When muted, use subtle idle animation instead of responding to mic
    const effectiveAmplitude = connectionState.isMuted
        ? CONFIG.MUTE_AMPLITUDE  // Gentle wave when muted
        : Math.max(CONFIG.MIN_IDLE_AMPLITUDE, amplitude * CONFIG.AMPLITUDE_MULTIPLIER);
    const time = performance.now() * 0.001;

    for (const wave of CONFIG.WAVES) {
        drawSineWave(width, height, time * wave.speed + wave.offset, effectiveAmplitude, wave.color, wave.opacity);
    }
}

function drawSineWave(width, height, phase, amplitude, color, opacity) {
    canvasCtx.beginPath();
    canvasCtx.strokeStyle = color;
    canvasCtx.lineWidth = CONFIG.WAVE_LINE_WIDTH;
    canvasCtx.globalAlpha = opacity;

    const midY = height / 2;

    for (let x = 0; x <= width; x += CONFIG.WAVE_STEP_SIZE) {
        const normX = x / width;
        const taper = Math.sin(normX * Math.PI);
        const y = midY + Math.sin(normX * Math.PI * 2 + phase) * (amplitude * CONFIG.WAVE_HEIGHT_MULTIPLIER * taper);

        if (x === 0) {
            canvasCtx.moveTo(x, y);
        } else {
            canvasCtx.lineTo(x, y);
        }
    }

    canvasCtx.stroke();
    canvasCtx.globalAlpha = 1.0;
}