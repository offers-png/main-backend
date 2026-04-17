/**
 * Sal AI Voice Server v1.0
 * Real-time conversational AI for outbound sales calls
 * Stack: Twilio Media Streams + ElevenLabs + Claude + Deepgram
 *
 * Required env vars on Render:
 *   ELEVENLABS_API_KEY
 *   ANTHROPIC_API_KEY  
 *   SUPABASE_SERVICE_KEY
 *   DEEPGRAM_API_KEY (optional — enables live transcription)
 *   TELEGRAM_BOT_TOKEN
 *   TELEGRAM_CHAT_ID
 */

const express = require('express');
const WebSocket = require('ws');
const http = require('http');

const app = express();
const server = http.createServer(app);
const wss = new WebSocket.Server({ server });

// All secrets from environment — never hardcoded
const ELEVENLABS_KEY = process.env.ELEVENLABS_API_KEY;
const CLAUDE_KEY = process.env.ANTHROPIC_API_KEY;
const ELEVENLABS_VOICE_ID = process.env.ELEVENLABS_VOICE_ID || 'pNInz6obpgDQGcFmaJgB'; // Adam
const SUPABASE_URL = process.env.SUPABASE_URL || 'https://wzcuzyouymauokijaqjk.supabase.co';
const SUPABASE_KEY = process.env.SUPABASE_SERVICE_KEY;
const TG_BOT = process.env.TELEGRAM_BOT_TOKEN;
const SALEH_CHAT_ID = process.env.TELEGRAM_CHAT_ID;

app.use(express.json());
app.use(express.urlencoded({ extended: true }));

app.get('/', (req, res) => res.json({ status: 'Sal AI Voice Server running', version: '1.0.0' }));
app.get('/health', (req, res) => res.json({ ok: true, ts: Date.now() }));

/**
 * Twilio webhook — called when lead picks up
 * Returns TwiML that connects call audio to our WebSocket
 */
app.post('/voice/answer', (req, res) => {
  const callSid = req.body.CallSid || '';
  const bizName = req.query.biz || 'your business';
  const host = process.env.RENDER_EXTERNAL_HOSTNAME || req.headers['x-forwarded-host'] || req.headers.host;

  console.log('[voice/answer] callSid:', callSid, 'biz:', bizName, 'host:', host);

  res.set('Content-Type', 'text/xml');
  res.send(`<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://${host}/voice/stream?biz=${encodeURIComponent(bizName)}&amp;callSid=${callSid}" />
  </Connect>
</Response>`);
});

/**
 * WebSocket handler — real-time voice conversation
 * Audio pipeline: Twilio mulaw -> Deepgram STT -> Claude -> ElevenLabs TTS -> Twilio
 */
wss.on('connection', async (ws, req) => {
  const url = new URL(req.url, 'http://localhost');
  const bizName = decodeURIComponent(url.searchParams.get('biz') || 'your business');
  const callSid = url.searchParams.get('callSid') || '';

  console.log('[ws] connected | biz:', bizName, '| callSid:', callSid);

  let streamSid = null;
  let conversationHistory = [];
  let audioBuffer = Buffer.alloc(0);
  let isSpeaking = false;
  let silenceTimer = null;
  let hasGreeted = false;

  const systemPrompt = `You are Sal, calling from Sal AI (dealdily.com) to speak with the owner of ${bizName}.
Keep ALL responses under 3 sentences. This is a phone call — be natural and brief.
Sal AI helps local businesses with:
- Automated customer follow-up sequences
- Google review response management  
- Customer reactivation campaigns
Goal: get them to say yes to a free 15-minute demo.
Tone: friendly, confident, not pushy. Natural speech — "Yeah", "Absolutely", "That makes sense".
If busy: "No problem at all, when would be a better time to chat?"
If not interested: "Totally understand, have a great day!"
If interested: get their name and best callback time.`;

  async function textToSpeech(text) {
    if (!ELEVENLABS_KEY) { console.warn('No ELEVENLABS_API_KEY set'); return null; }
    try {
      const r = await fetch(`https://api.elevenlabs.io/v1/text-to-speech/${ELEVENLABS_VOICE_ID}/stream`, {
        method: 'POST',
        headers: { 'xi-api-key': ELEVENLABS_KEY, 'Content-Type': 'application/json', 'Accept': 'audio/mpeg' },
        body: JSON.stringify({ text, model_id: 'eleven_turbo_v2', voice_settings: { stability: 0.5, similarity_boost: 0.8, style: 0.0, use_speaker_boost: true } })
      });
      if (!r.ok) { console.error('ElevenLabs error:', r.status); return null; }
      return Buffer.from(await r.arrayBuffer());
    } catch (e) { console.error('TTS error:', e.message); return null; }
  }

  async function getClaudeResponse(userText) {
    if (!CLAUDE_KEY) return "I'm having trouble connecting. Could you call us back at dealdily.com?";
    try {
      conversationHistory.push({ role: 'user', content: userText });
      const r = await fetch('https://api.anthropic.com/v1/messages', {
        method: 'POST',
        headers: { 'x-api-key': CLAUDE_KEY, 'anthropic-version': '2023-06-01', 'content-type': 'application/json' },
        body: JSON.stringify({ model: 'claude-haiku-4-5-20251001', max_tokens: 100, system: systemPrompt, messages: conversationHistory })
      });
      const d = await r.json();
      const reply = d.content?.[0]?.text || "Could you say that again?";
      conversationHistory.push({ role: 'assistant', content: reply });
      return reply;
    } catch (e) { return "Sorry about that — could you repeat that?"; }
  }

  function sendAudio(audio) {
    if (ws.readyState !== WebSocket.OPEN || !streamSid) return;
    ws.send(JSON.stringify({ event: 'media', streamSid, media: { payload: audio.toString('base64') } }));
  }

  async function greet() {
    if (hasGreeted) return;
    hasGreeted = true;
    const msg = `Hi there, this is Sal calling from Sal AI. Is this the owner or manager of ${bizName}?`;
    console.log('[greet]', msg);
    const audio = await textToSpeech(msg);
    if (audio) { conversationHistory.push({ role: 'assistant', content: msg }); sendAudio(audio); }
  }

  async function alertSaleh() {
    if (!TG_BOT || !SALEH_CHAT_ID) return;
    const summary = conversationHistory.slice(-4).map(m => (m.role === 'user' ? 'Caller' : 'Sal') + ': ' + m.content).join('\n');
    await fetch(`https://api.telegram.org/bot${TG_BOT}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: SALEH_CHAT_ID, parse_mode: 'Markdown', text: `🔥 *HOT LEAD — AI Voice Call*\n\nBusiness: ${bizName}\nCall SID: ${callSid}\n\nCall them back NOW!\n\n${summary}` })
    }).catch(() => {});
  }

  ws.on('message', async (data) => {
    try {
      const msg = JSON.parse(data.toString());
      if (msg.event === 'start') {
        streamSid = msg.start.streamSid;
        console.log('[stream] started:', streamSid);
        setTimeout(greet, 800);
      } else if (msg.event === 'media' && msg.media?.payload) {
        audioBuffer = Buffer.concat([audioBuffer, Buffer.from(msg.media.payload, 'base64')]);
        clearTimeout(silenceTimer);
        silenceTimer = setTimeout(async () => {
          if (audioBuffer.length < 400 || isSpeaking) { audioBuffer = Buffer.alloc(0); return; }
          const captured = audioBuffer; audioBuffer = Buffer.alloc(0);
          isSpeaking = true;
          try {
            const text = await transcribe(captured);
            if (!text?.trim() || text.trim().length < 3) return;
            console.log('[caller]:', text);
            const lower = text.toLowerCase();
            if (lower.match(/yes|interested|sure|tell me more|demo|how does/)) alertSaleh();
            const reply = await getClaudeResponse(text);
            console.log('[sal]:', reply);
            const audio = await textToSpeech(reply);
            if (audio) sendAudio(audio);
          } finally { isSpeaking = false; }
        }, 700);
      } else if (msg.event === 'stop') {
        console.log('[stream] stopped');
        saveConversation();
      }
    } catch (e) { console.error('[ws message error]', e.message); }
  });

  ws.on('close', () => { console.log('[ws] closed'); clearTimeout(silenceTimer); });

  async function transcribe(audioData) {
    const DG_KEY = process.env.DEEPGRAM_API_KEY;
    if (!DG_KEY) return '';
    try {
      const r = await fetch('https://api.deepgram.com/v1/listen?model=nova-2&smart_format=true', {
        method: 'POST',
        headers: { 'Authorization': `Token ${DG_KEY}`, 'Content-Type': 'audio/mulaw;rate=8000' },
        body: audioData
      });
      const d = await r.json();
      return d.results?.channels?.[0]?.alternatives?.[0]?.transcript || '';
    } catch (e) { return ''; }
  }

  async function saveConversation() {
    if (!SUPABASE_KEY) return;
    const summary = conversationHistory.map(m => (m.role === 'user' ? 'Caller' : 'Sal') + ': ' + m.content).join('\n');
    await fetch(`${SUPABASE_URL}/rest/v1/saleh2_memory`, {
      method: 'POST',
      headers: { 'apikey': SUPABASE_KEY, 'Authorization': `Bearer ${SUPABASE_KEY}`, 'Content-Type': 'application/json', 'Prefer': 'return=minimal' },
      body: JSON.stringify({ category: 'call', subject: `Voice call: ${bizName}`, content: summary, source: 'voice_ai', importance: 8 })
    }).catch(() => {});
  }
});

const PORT = process.env.PORT || 3001;
server.listen(PORT, () => {
  console.log(`[startup] Sal AI Voice Server on port ${PORT}`);
  if (!ELEVENLABS_KEY) console.warn('[startup] WARNING: ELEVENLABS_API_KEY not set');
  if (!CLAUDE_KEY) console.warn('[startup] WARNING: ANTHROPIC_API_KEY not set');
});