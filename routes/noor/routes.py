# ============================================================
#  USTADHA NOOR — Islamic AI Teacher Backend
#  Add this file to your project, then include the router in
#  your main.py:  app.include_router(noor_router)
# ============================================================

import os, base64, io, uuid, datetime, httpx
from typing import Optional, List
from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from supabase import create_client, Client

# ── Config ────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
OPENAI_KEY    = os.environ.get("OPENAI_API_KEY", "")  # for Whisper

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
noor_router = APIRouter(prefix="/noor", tags=["Ustadha Noor"])

# ── Strict Islamic-only system prompt ────────────────────
NOOR_SYSTEM = """You are Sheikh Noor Al-Afasy, an AI Islamic scholar and teacher for Muslim children aged 7-10. You are inspired by the great Sheikh Mishary Rashid Al-Afasy — known for his beautiful Quran recitation, deep knowledge, warmth with children, and uncompromising Islamic character.

═══════════════════════════════════════════
ABSOLUTE BOUNDARIES — NEVER CROSSED
═══════════════════════════════════════════
1. You ONLY teach these subjects — nothing else exists:
   • Arabic language (letters, words, grammar, writing)
   • Holy Quran (recitation, tajweed, memorization, tafseer simplified)
   • Islamic Studies (aqeedah, fiqh for kids, seerah, prophets)
   • Duas and Adhkar
   • Islamic manners and character (akhlaq)

2. If a child says ANYTHING outside these boundaries (games, TV shows, food, school subjects, jokes, songs, anything worldly):
   → Say firmly but kindly: "Yaa waladi/ya binti, we are in Islamic class now. Let us return to our lesson." Then immediately continue teaching.

3. NEVER discuss:
   • Politics of any kind
   • Other religions in a negative way
   • Violence or scary stories
   • Anything haram, inappropriate, or doubtful
   • Technology, games, entertainment
   • Anything a Muslim scholar would consider inappropriate for children

4. If asked "are you AI" or "are you a robot": Say "I am your teacher. Let us focus on our lesson. Bismillah."

5. NEVER ask the child what they want to learn. YOU decide the lesson. YOU are the teacher.

═══════════════════════════════════════════
PERSONALITY — SHEIKH AL-AFASY INSPIRED
═══════════════════════════════════════════
- Speak with the dignity and warmth of a true Islamic scholar
- Use Islamic phrases naturally: "Bismillah", "Alhamdulillah", "Mashallah", "Subhanallah", "Ahsant ya waladi", "Barakallahu feek"
- Address boys as "ya waladi" (my son) and girls as "ya binti" (my daughter)
- Never use slang. Speak clearly, simply, with wisdom.
- When a child answers correctly: celebrate warmly — "Mashallah! Ahsant! You answered like a true student of knowledge!"
- When a child makes a mistake: never shame — "Close, ya waladi. Listen carefully and try again. Allah loves those who try."
- When a child is distracted: "Ya waladi, the angels are writing everything. Let us be present in our lesson."
- Occasional gentle Islamic reminders woven into teaching: "The Prophet ﷺ said: seek knowledge..."
- Recite short Quran verses with proper transliteration when relevant

═══════════════════════════════════════════
CLASSROOM RULES YOU ENFORCE
═══════════════════════════════════════════
- You lead every second of class. No child input on what to learn.
- After teaching a chunk, say: "Now raise your hand when you are ready to answer, ya waladi."
- If vision shows distraction: address it as a scholar would — calm, firm, redirecting
- If vision shows cheating during recitation: "Ya waladi, I can see you. Recite from your heart. That is where the Quran lives."
- You track what was covered and build on it each response.

═══════════════════════════════════════════
VISION ALERTS [VISION: ...]
═══════════════════════════════════════════
React naturally as a teacher — never mention "vision alert" or "camera."
- Distracted → bring back with wisdom and warmth
- Cheating → catch with dignity, no embarrassment
- Not present → "Where did you go, ya waladi? Come back to your lesson."

═══════════════════════════════════════════
LESSON STRUCTURE
═══════════════════════════════════════════
Each lesson follows this flow — YOU decide the pace:
1. Open with Bismillah and a short dua for knowledge
2. Brief review of last topic (if history provided)
3. Teach today's topic in small clear chunks
4. Ask child to repeat after you / answer a question
5. Praise or correct with patience
6. Move to next chunk
7. End session with dua and encouragement

TOPICS IN ORDER OF PRIORITY:
Arabic: Alef to Ya — letter name, shape, sound, example word
Quran: Al-Fatiha → Al-Ikhlas → Al-Falaq → An-Nas → Al-Kawthar → Al-Asr → Al-Fil → Shorter surahs
Islamic Studies: Tawheed first → 5 Pillars → 6 Pillars of Iman → Prophets (Adam, Nuh, Ibrahim, Musa, Isa, Muhammad ﷺ) → Islamic manners
Duas: Bismillah → before eating → after eating → sleeping → waking → entering home → leaving home → entering masjid

PRONUNCIATION & TAJWEED:
- Teach proper makhraj (articulation points) for each letter
- Point out specific tajweed rules: madd, ghunna, qalqala, ikhfaa
- Never accept sloppy recitation — always gently correct

═══════════════════════════════════════════
RESPONSE RULES
═══════════════════════════════════════════
- Maximum 60 words per response — you speak aloud on a phone
- Always end with either: a question, "repeat after me", or "raise your hand when ready"
- Never use bullet points or markdown in responses — speak naturally
- Use transliteration for all Arabic so child can follow along
- Speak as if the child is sitting right in front of you"""

# ── Models ────────────────────────────────────────────────
class ChatMessage(BaseModel):
    role: str   # "user" | "assistant"
    content: str

class ChatRequest(BaseModel):
    student_id: Optional[str] = None
    lesson_id: Optional[str] = None
    message: str
    image_b64: Optional[str] = None   # JPEG base64 from camera
    mode: str = "TEACHING"            # TEACHING | RECITATION | HOMEWORK | VISION
    history: List[ChatMessage] = []

class VisionRequest(BaseModel):
    student_id: Optional[str] = None
    lesson_id: Optional[str] = None
    image_b64: str
    mode: str = "TEACHING"  # TEACHING | RECITATION

class TranscribeRequest(BaseModel):
    student_id: Optional[str] = None
    lesson_id: Optional[str] = None
    audio_b64: str          # base64 encoded audio (webm or mp4)
    surah: Optional[str] = None

class StudentCreate(BaseModel):
    name: str
    age: Optional[int] = None
    level: str = "beginner"

class ProgressUpdate(BaseModel):
    student_id: str
    surah_memorized: Optional[str] = None
    surah_in_progress: Optional[str] = None
    dua_learned: Optional[str] = None
    arabic_level: Optional[int] = None
    lesson_minutes: Optional[int] = None

class StartLesson(BaseModel):
    student_id: str

class EndLesson(BaseModel):
    lesson_id: str
    student_id: str
    topics_covered: List[str] = []
    summary: Optional[str] = None

# ── Helper: call Claude ───────────────────────────────────
async def call_claude(messages: list, max_tokens: int = 300) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": max_tokens,
                "system": NOOR_SYSTEM,
                "messages": messages,
            }
        )
        if resp.status_code != 200:
            raise HTTPException(500, f"Claude error: {resp.text}")
        return resp.json()["content"][0]["text"]

# ── Helper: build image content block ────────────────────
def image_block(b64: str) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}
    }

# ── Helper: log attention event ──────────────────────────
def log_attention(student_id: str, lesson_id: str, event_type: str, description: str):
    try:
        supabase.table("noor_attention_log").insert({
            "student_id": student_id,
            "lesson_id": lesson_id,
            "event_type": event_type,
            "description": description,
        }).execute()
    except Exception:
        pass  # Non-critical, don't crash

# ═══════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════

# ── POST /noor/chat ───────────────────────────────────────
@noor_router.post("/chat")
async def chat(req: ChatRequest):
    """Main chat endpoint. Handles text + optional camera image."""

    # Build message history for Claude
    claude_msgs = []
    for m in req.history[-14:]:  # last 7 turns
        claude_msgs.append({"role": m.role, "content": m.content})

    # Build current user message
    mode_tag = f"[MODE: {req.mode}]"
    if req.image_b64:
        user_content = [
            image_block(req.image_b64),
            {"type": "text", "text": f"{mode_tag} {req.message}"}
        ]
    else:
        user_content = f"{mode_tag} {req.message}"

    claude_msgs.append({"role": "user", "content": user_content})

    reply = await call_claude(claude_msgs)

    # Log to lesson if we have IDs
    if req.student_id and req.lesson_id:
        try:
            supabase.table("noor_lessons").update({
                "notes": f"Last message: {req.message[:100]}"
            }).eq("id", req.lesson_id).execute()
        except Exception:
            pass

    return {"reply": reply, "mode": req.mode}


# ── POST /noor/vision ─────────────────────────────────────
@noor_router.post("/vision")
async def vision_check(req: VisionRequest):
    """
    Silent background camera check every ~9 seconds.
    Returns: event_type + optional teacher_response.
    """
    is_recitation = req.mode == "RECITATION"

    if is_recitation:
        prompt = ("Look at this child carefully. "
                  "Is the child looking at a book, paper, or phone screen? "
                  "Are they looking at the camera or looking away? "
                  "Reply with JSON only: "
                  '{\"attention\": \"focused|distracted|absent\", '
                  '\"cheating\": true|false, '
                  '\"description\": \"one sentence\"}')
    else:
        prompt = ("Look at this child. "
                  "Are they paying attention, distracted, or not visible? "
                  "Reply with JSON only: "
                  '{\"attention\": \"focused|distracted|absent\", '
                  '\"cheating\": false, '
                  '\"description\": \"one sentence\"}')

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 120,
                "messages": [{
                    "role": "user",
                    "content": [image_block(req.image_b64), {"type": "text", "text": prompt}]
                }]
            }
        )

    raw = resp.json()["content"][0]["text"].strip()

    # Parse JSON safely
    import json, re
    try:
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        data = json.loads(match.group()) if match else {}
    except Exception:
        data = {}

    attention   = data.get("attention", "focused")
    cheating    = data.get("cheating", False)
    description = data.get("description", "")

    teacher_response = None
    event_type = "attentive"

    if cheating and is_recitation:
        event_type = "cheating"
        teacher_response = await call_claude([{
            "role": "user",
            "content": f"[VISION: Child appears to be looking at a book or paper during Quran recitation. Catch them kindly but firmly.]"
        }])
    elif attention == "distracted":
        event_type = "distracted"
        teacher_response = await call_claude([{
            "role": "user",
            "content": "[VISION: Child is distracted, looking away from camera. Call them back to attention gently.]"
        }])
    elif attention == "absent":
        event_type = "absent"
        teacher_response = await call_claude([{
            "role": "user",
            "content": "[VISION: Child is not visible or has left the camera view.]"
        }])

    # Log event
    if req.student_id and req.lesson_id and event_type != "attentive":
        log_attention(req.student_id, req.lesson_id, event_type, description)

    return {
        "event_type": event_type,
        "attention": attention,
        "cheating": cheating,
        "description": description,
        "teacher_response": teacher_response,
    }


# ── POST /noor/transcribe ─────────────────────────────────
@noor_router.post("/transcribe")
async def transcribe_recitation(req: TranscribeRequest):
    """
    Transcribe audio via Whisper, then score Quran pronunciation.
    Expects base64 audio (webm/mp4).
    """
    if not OPENAI_KEY:
        raise HTTPException(400, "OPENAI_API_KEY not set — Whisper unavailable")

    # Decode audio
    audio_bytes = base64.b64decode(req.audio_b64)

    # Send to Whisper
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_KEY}"},
            files={"file": ("audio.webm", io.BytesIO(audio_bytes), "audio/webm")},
            data={"model": "whisper-1", "language": "ar"}
        )
        if resp.status_code != 200:
            raise HTTPException(500, f"Whisper error: {resp.text}")

    transcript = resp.json().get("text", "")

    # Score pronunciation with Claude
    surah_context = f"The child is reciting {req.surah}. " if req.surah else ""
    score_prompt = (
        f"{surah_context}The child recited: \"{transcript}\"\n"
        "Evaluate the Quran recitation. Point out any pronunciation or tajweed mistakes "
        "with the correct version. Be kind and specific. Max 3 corrections at a time."
    )

    feedback = await call_claude([{"role": "user", "content": score_prompt}])

    return {
        "transcript": transcript,
        "feedback": feedback,
        "surah": req.surah,
    }


# ── POST /noor/homework ───────────────────────────────────
@noor_router.post("/homework")
async def grade_homework(student_id: str, image_b64: str, lesson_id: Optional[str] = None):
    """Grade homework from camera image."""

    prompt = (
        "[HOMEWORK SCAN] Read this homework carefully. "
        "Identify the subject (Arabic writing, Quran, Islamic studies). "
        "Grade it: what is correct, what needs fixing. "
        "Be specific, kind, and encouraging. Give a score out of 100."
    )

    feedback = await call_claude([{
        "role": "user",
        "content": [image_block(image_b64), {"type": "text", "text": prompt}]
    }], max_tokens=400)

    # Determine grade
    import re
    score_match = re.search(r'(\d+)\s*/\s*100', feedback)
    score = int(score_match.group(1)) if score_match else None
    grade = "excellent" if (score or 0) >= 85 else "good" if (score or 0) >= 60 else "needs_work"

    # Save to DB
    record = {
        "student_id": student_id,
        "feedback": feedback,
        "grade": grade,
        "score": score,
    }
    result = supabase.table("noor_homework").insert(record).execute()
    hw_id = result.data[0]["id"] if result.data else None

    return {"feedback": feedback, "grade": grade, "score": score, "homework_id": hw_id}


# ── POST /noor/students ───────────────────────────────────
@noor_router.post("/students")
async def create_student(s: StudentCreate):
    """Create a new student profile."""
    result = supabase.table("noor_students").insert({
        "name": s.name,
        "age": s.age,
        "level": s.level,
    }).execute()
    student = result.data[0]

    # Create blank progress record
    supabase.table("noor_progress").insert({
        "student_id": student["id"]
    }).execute()

    return student


# ── GET /noor/students ────────────────────────────────────
@noor_router.get("/students")
async def list_students():
    result = supabase.table("noor_students").select("*").order("created_at").execute()
    return result.data


# ── GET /noor/student/{id} ────────────────────────────────
@noor_router.get("/student/{student_id}")
async def get_student(student_id: str):
    """Load student profile + progress + recent lessons."""
    student = supabase.table("noor_students").select("*").eq("id", student_id).single().execute().data
    progress = supabase.table("noor_progress").select("*").eq("student_id", student_id).maybe_single().execute().data
    lessons = supabase.table("noor_lessons").select("*").eq("student_id", student_id).order("started_at", desc=True).limit(10).execute().data
    homework = supabase.table("noor_homework").select("*").eq("student_id", student_id).order("submitted_at", desc=True).limit(5).execute().data

    return {
        "student": student,
        "progress": progress,
        "recent_lessons": lessons,
        "recent_homework": homework,
    }


# ── POST /noor/lesson/start ───────────────────────────────
@noor_router.post("/lesson/start")
async def start_lesson(req: StartLesson):
    """Start a new lesson session. Returns lesson_id."""
    result = supabase.table("noor_lessons").insert({
        "student_id": req.student_id,
    }).execute()
    lesson_id = result.data[0]["id"]

    # Update progress last_lesson_at
    supabase.table("noor_progress").update({
        "last_lesson_at": datetime.datetime.utcnow().isoformat()
    }).eq("student_id", req.student_id).execute()

    return {"lesson_id": lesson_id}


# ── POST /noor/lesson/end ─────────────────────────────────
@noor_router.post("/lesson/end")
async def end_lesson(req: EndLesson):
    """End lesson, save duration + summary."""
    lesson = supabase.table("noor_lessons").select("started_at").eq("id", req.lesson_id).single().execute().data
    started = datetime.datetime.fromisoformat(lesson["started_at"].replace("Z", "+00:00"))
    ended = datetime.datetime.now(datetime.timezone.utc)
    duration = int((ended - started).total_seconds())
    minutes = duration // 60

    supabase.table("noor_lessons").update({
        "ended_at": ended.isoformat(),
        "duration_seconds": duration,
        "topics_covered": req.topics_covered,
        "summary": req.summary,
    }).eq("id", req.lesson_id).execute()

    # Update total stats
    prog = supabase.table("noor_progress").select("total_lessons,total_minutes").eq("student_id", req.student_id).single().execute().data
    supabase.table("noor_progress").update({
        "total_lessons": (prog["total_lessons"] or 0) + 1,
        "total_minutes": (prog["total_minutes"] or 0) + minutes,
        "updated_at": ended.isoformat(),
    }).eq("student_id", req.student_id).execute()

    return {"duration_seconds": duration, "minutes": minutes}


# ── POST /noor/progress ───────────────────────────────────
@noor_router.post("/progress")
async def update_progress(req: ProgressUpdate):
    """Update student progress after lesson."""
    prog = supabase.table("noor_progress").select("*").eq("student_id", req.student_id).single().execute().data

    updates = {"updated_at": datetime.datetime.utcnow().isoformat()}

    if req.surah_memorized:
        memorized = set(prog.get("surahs_memorized") or [])
        memorized.add(req.surah_memorized)
        in_progress = set(prog.get("surahs_in_progress") or [])
        in_progress.discard(req.surah_memorized)
        updates["surahs_memorized"] = list(memorized)
        updates["surahs_in_progress"] = list(in_progress)

    if req.surah_in_progress:
        in_progress = set(prog.get("surahs_in_progress") or [])
        in_progress.add(req.surah_in_progress)
        updates["surahs_in_progress"] = list(in_progress)

    if req.dua_learned:
        duas = set(prog.get("duas_learned") or [])
        duas.add(req.dua_learned)
        updates["duas_learned"] = list(duas)

    if req.arabic_level is not None:
        updates["arabic_level"] = max(0, min(100, req.arabic_level))

    if req.lesson_minutes:
        updates["total_minutes"] = (prog.get("total_minutes") or 0) + req.lesson_minutes

    supabase.table("noor_progress").update(updates).eq("student_id", req.student_id).execute()
    return {"updated": True}


# ── GET /noor/progress/{student_id} ──────────────────────
@noor_router.get("/progress/{student_id}")
async def get_progress(student_id: str):
    result = supabase.table("noor_progress").select("*").eq("student_id", student_id).single().execute()
    return result.data


# ── GET /noor/homework/{student_id} ──────────────────────
@noor_router.get("/homework/{student_id}")
async def get_homework(student_id: str):
    result = supabase.table("noor_homework").select("*").eq("student_id", student_id).order("submitted_at", desc=True).execute()
    return result.data


# ── GET /noor/attention/{lesson_id} ──────────────────────
@noor_router.get("/attention/{lesson_id}")
async def get_attention_log(lesson_id: str):
    result = supabase.table("noor_attention_log").select("*").eq("lesson_id", lesson_id).order("logged_at").execute()
    return result.data


# ═══════════════════════════════════════════════════════════
#  PARENT NOTES
# ═══════════════════════════════════════════════════════════

class ParentNoteCreate(BaseModel):
    student_id: str
    notes: str
    focus_topics: Optional[List[str]] = []

@noor_router.post("/parent-notes")
async def save_parent_notes(req: ParentNoteCreate):
    result = supabase.table("noor_parent_notes").insert({
        "student_id": req.student_id,
        "notes": req.notes,
        "focus_topics": req.focus_topics,
    }).execute()
    return result.data[0]

@noor_router.get("/parent-notes/{student_id}")
async def get_parent_notes(student_id: str):
    # Get most recent unused note, or latest
    result = supabase.table("noor_parent_notes")\
        .select("*")\
        .eq("student_id", student_id)\
        .order("created_at", desc=True)\
        .limit(1)\
        .execute()
    return result.data[0] if result.data else None

@noor_router.patch("/parent-notes/{note_id}/used")
async def mark_note_used(note_id: str):
    supabase.table("noor_parent_notes").update({"used": True}).eq("id", note_id).execute()
    return {"marked": True}


# ═══════════════════════════════════════════════════════════
#  HAND RAISE DETECTION
# ═══════════════════════════════════════════════════════════

class HandRaiseRequest(BaseModel):
    student_id: Optional[str] = None
    lesson_id: Optional[str] = None
    session_id: Optional[str] = None
    image_b64: str

@noor_router.post("/hand-raise")
async def detect_hand_raise(req: HandRaiseRequest):
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 10,
                "messages": [{
                    "role": "user",
                    "content": [
                        image_block(req.image_b64),
                        {"type": "text", "text": "Is any hand or arm raised or lifted upward in this image? Be generous — even a partial raise counts. Answer only: yes or no"}
                    ]
                }]
            }
        )
    text = resp.json()["content"][0]["text"].strip().lower()
    raised = "yes" in text

    if raised and req.student_id and req.lesson_id:
        try:
            supabase.table("noor_hand_raises").insert({
                "lesson_id": req.lesson_id,
                "student_id": req.student_id,
                "session_id": req.session_id,
            }).execute()
            # Increment hand_raises on session
            if req.session_id:
                sess = supabase.table("noor_sessions").select("hand_raises").eq("id", req.session_id).single().execute().data
                supabase.table("noor_sessions").update({
                    "hand_raises": (sess.get("hand_raises") or 0) + 1
                }).eq("id", req.session_id).execute()
        except Exception:
            pass

    return {"raised": raised}


# ═══════════════════════════════════════════════════════════
#  TRANSCRIPTS
# ═══════════════════════════════════════════════════════════

class TranscriptEntry(BaseModel):
    lesson_id: str
    student_id: str
    session_id: Optional[str] = None
    speaker: str  # 'teacher' | 'student'
    message: str
    mode: str = "TEACHING"

@noor_router.post("/transcript/add")
async def add_transcript_entry(req: TranscriptEntry):
    result = supabase.table("noor_transcripts").insert({
        "lesson_id": req.lesson_id,
        "student_id": req.student_id,
        "session_id": req.session_id,
        "speaker": req.speaker,
        "message": req.message,
        "mode": req.mode,
    }).execute()
    return result.data[0]

@noor_router.get("/transcript/{lesson_id}")
async def get_transcript(lesson_id: str):
    result = supabase.table("noor_transcripts")\
        .select("*")\
        .eq("lesson_id", lesson_id)\
        .order("timestamp")\
        .execute()
    return result.data


# ═══════════════════════════════════════════════════════════
#  SESSIONS
# ═══════════════════════════════════════════════════════════

class SessionStart(BaseModel):
    lesson_id: str
    student_id: str

class SessionEnd(BaseModel):
    session_id: str
    lesson_id: str
    student_id: str
    duration_seconds: Optional[int] = None
    recording_url: Optional[str] = None
    attention_score: Optional[int] = None
    cheating_attempts: Optional[int] = 0

@noor_router.post("/session/start")
async def start_session(req: SessionStart):
    result = supabase.table("noor_sessions").insert({
        "lesson_id": req.lesson_id,
        "student_id": req.student_id,
    }).execute()
    return result.data[0]

@noor_router.post("/session/end")
async def end_session(req: SessionEnd):
    ended = datetime.datetime.now(datetime.timezone.utc)
    updates = {
        "ended_at": ended.isoformat(),
        "cheating_attempts": req.cheating_attempts,
    }
    if req.duration_seconds: updates["duration_seconds"] = req.duration_seconds
    if req.recording_url: updates["recording_url"] = req.recording_url
    if req.attention_score is not None: updates["attention_score"] = req.attention_score

    # Auto-generate transcript summary
    try:
        transcript = supabase.table("noor_transcripts")\
            .select("speaker,message")\
            .eq("lesson_id", req.lesson_id)\
            .order("timestamp")\
            .execute().data
        if transcript:
            convo = "\n".join([f"{t['speaker'].upper()}: {t['message']}" for t in transcript[-20:]])
            summary = await call_claude([{
                "role": "user",
                "content": f"Summarize this Islamic lesson in 2-3 sentences for a parent report. What was covered, how did the student do?\n\n{convo}"
            }], max_tokens=150)
            updates["transcript_summary"] = summary
    except Exception:
        pass

    supabase.table("noor_sessions").update(updates).eq("id", req.session_id).execute()
    return {"ended": True}

@noor_router.get("/sessions/{student_id}")
async def get_sessions(student_id: str):
    result = supabase.table("noor_sessions")\
        .select("*")\
        .eq("student_id", student_id)\
        .order("started_at", desc=True)\
        .limit(20)\
        .execute()
    return result.data
