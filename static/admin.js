/**
 * Admin Panel JavaScript
 * @version 3.2.0 - Multi-tenant support
 */

'use strict';

// Get tenant ID from injected window variable (set by server for tenant pages)
const TENANT_ID = window.TENANT_ID || null;
const API_PREFIX = TENANT_ID ? `/${TENANT_ID}` : '';

const CONFIG = Object.freeze({
    STATUS_REFRESH_INTERVAL_MS: 3000,
    TOAST_DURATION_MS: 3000,
});

const elements = Object.freeze({
    killBtn: document.getElementById('killBtn'),
    speakBtn: document.getElementById('speakBtn'),
    muteBtn: document.getElementById('muteBtn'),
    speakText: document.getElementById('speakText'),
    toast: document.getElementById('toast'),
    voiceSelect: document.getElementById('voiceSelect'),
    instructionsText: document.getElementById('instructionsText'),
    instructionsBtn: document.getElementById('instructionsBtn'),
    connectionCount: document.getElementById('connectionCount'),
    uptime: document.getElementById('uptime'),
    currentVoice: document.getElementById('currentVoice'),
});

function formatUptime(seconds) {
    if (seconds < 60) return `${Math.floor(seconds)}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    const hours = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    return `${hours}h ${mins}m`;
}

function showToast(message, isError = false) {
    elements.toast.textContent = message;
    elements.toast.className = 'toast show ' + (isError ? 'error' : 'success');
    setTimeout(() => { elements.toast.className = 'toast'; }, CONFIG.TOAST_DURATION_MS);
}

function setLoading(loading) {
    elements.killBtn.disabled = loading;
    elements.speakBtn.disabled = loading;
    elements.muteBtn.disabled = loading;
    elements.instructionsBtn.disabled = loading;
    elements.voiceSelect.disabled = loading;
}

function updateMuteButton(isMuted) {
    const btnText = elements.muteBtn.querySelector('.btn-text');
    if (isMuted) {
        btnText.textContent = 'تشغيل الصوت';
        elements.muteBtn.classList.add('muted');
    } else {
        btnText.textContent = 'كتم الصوت';
        elements.muteBtn.classList.remove('muted');
    }
}

function updateDashboard(status) {
    elements.connectionCount.textContent = status.active_connections;
    elements.uptime.textContent = formatUptime(status.uptime_seconds);
    elements.currentVoice.textContent = status.current_voice;
}

async function adminRequest(url, body = null, method = 'POST') {
    setLoading(true);
    try {
        const options = { method, headers: { 'Content-Type': 'application/json' } };
        if (body) options.body = JSON.stringify(body);
        const response = await fetch(url, options);
        if (!response.ok) { showToast(`خطأ: ${response.status}`, true); return false; }
        return await response.json();
    } catch (error) {
        showToast('خطأ في الاتصال', true);
        return false;
    } finally {
        setLoading(false);
    }
}

async function killAudio() {
    const res = await adminRequest(`${API_PREFIX}/admin/kill`);
    if (res) showToast('تم إيقاف جميع الجلسات');
}

async function sendText() {
    const text = elements.speakText.value.trim();
    if (!text) { showToast('الرجاء إدخال نص', true); return; }
    const res = await adminRequest(`${API_PREFIX}/admin/speak`, { text });
    if (res) { elements.speakText.value = ''; showToast('تم إرسال النص'); }
}

async function toggleMute() {
    const res = await adminRequest(`${API_PREFIX}/admin/mute`);
    if (res) { updateMuteButton(res.muted); updateDashboard(res); }
}

async function changeVoice() {
    const voice = elements.voiceSelect.value;
    if (!voice) return;
    const res = await adminRequest(`${API_PREFIX}/admin/voice`, { voice });
    if (res) { showToast(`تم تغيير الصوت إلى ${voice}`); elements.currentVoice.textContent = voice; }
}

async function saveInstructions() {
    const instructions = elements.instructionsText.value.trim();
    if (!instructions) { showToast('الرجاء إدخال التعليمات', true); return; }
    const res = await adminRequest(`${API_PREFIX}/admin/instructions`, { instructions });
    if (res) showToast('تم حفظ التعليمات');
}

async function checkStatus() {
    try {
        const response = await fetch(`${API_PREFIX}/admin/status`);
        if (response.ok) {
            const data = await response.json();
            updateMuteButton(data.muted);
            updateDashboard(data);
        }
    } catch (error) { /* silent */ }
}

async function loadVoices() {
    try {
        const response = await fetch(`${API_PREFIX}/admin/voices`);
        if (response.ok) {
            const data = await response.json();
            elements.voiceSelect.innerHTML = data.voices.map(v =>
                `<option value="${v.name}" ${v.name === data.current ? 'selected' : ''}>${v.name} (${v.gender})</option>`
            ).join('');
        }
    } catch (error) { /* silent */ }
}

async function loadInstructions() {
    try {
        const response = await fetch(`${API_PREFIX}/admin/instructions`);
        if (response.ok) {
            const data = await response.json();
            elements.instructionsText.value = data.instructions;
        }
    } catch (error) { /* silent */ }
}

elements.killBtn.addEventListener('click', killAudio);
elements.speakBtn.addEventListener('click', sendText);
elements.muteBtn.addEventListener('click', toggleMute);
elements.voiceSelect.addEventListener('change', changeVoice);
elements.instructionsBtn.addEventListener('click', saveInstructions);

checkStatus();
loadVoices();
loadInstructions();
setInterval(checkStatus, CONFIG.STATUS_REFRESH_INTERVAL_MS);
