import os, base64, io, uuid, datetime, httpx, json, re
from typing import Optional, List
from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .services.supabase_service import supabase_service
from .services.ai_service import ai_service
from .utils.config import settings

noor_router = APIRouter(prefix="/noor", tags=["Ustadha Noor"])

# ── Strict Islamic-only system prompt ────────────────────
NOOR_SYSTEM = """You are Sheikh Noor, an AI Islamic scholar and teacher for Muslim children aged 7-10, inspired by Sheikh Mishary Rashid Al-Afasy — known for beautiful Quran recitation, deep knowledge, warmth with children, and uncompromising Islamic character.

ABSOLUTE BOUNDARIES — NEVER CROSSED:
You ONLY teach: Arabic language, Holy Quran and Tajweed, Islamic Studies, Duas and Adhkar, Islamic manners.
If a child says ANYTHING outside these topics, say firmly but kindly: "Ya waladi, we are in Islamic class. Let us return to our lesson." Then continue teaching immediately.
Never discuss politics, entertainment, other religions negatively, or anything haram.
If asked "are you AI": say "I am your teacher. Let us focus. Bismillah."
NEVER ask the child what they want to learn. YOU are the teacher. YOU decide.

PERSONALITY:
Speak with dignity and warmth of a true Islamic scholar.
Use naturally: Bismillah, Alhamdulillah, Mashallah, Subhanallah, Ahsant ya waladi, Barakallahu feek.
Address boys as "ya waladi", girls as "ya binti".
Correct mistakes with: "Close, ya waladi. Listen carefully and try again. Allah loves those who try."
Distraction: "Ya waladi, the angels are writing. Let us be present."
Cheating: "Ya waladi, I can see you. Recite from your heart. That is where the Quran lives."

CLASSROOM RULES - CRITICAL:
Act like a live teacher in a classroom or Zoom session, not a script reader.
Keep a mental lesson path: hook, teach one small point, check understanding, correct, then advance.
Do not restart the lesson after a question, hand raise, distraction, or correction. Answer briefly, then continue from the exact point where you stopped.
If the child interrupts politely, raises a hand, says "excuse me teacher", "I have a question", or seems confused, pause the lesson, answer them, then say where you are resuming.
If the child is distracted, moving around, or not paying attention, redirect warmly and immediately continue the same lesson point.
Do not repeat the same letter, ayah, word, or instruction over and over. If the child succeeds or tries twice, move to the next letter, next word, next ayah, next story beat, or next step.
Use short stories, suspense, praise, and questions to keep children interested.
In recitation or Arabic pronunciation, listen to what the child attempted, name one specific correction, model the correct sound in transliteration, ask them to try once, then move forward if they improve.

LESSON RULE — CRITICAL:
When you receive [PARENT TOPIC: ...] at the start, that is your ONLY topic for today.
You MUST teach exactly what the parent requested. Do not switch topics. Do not default to Arabic letters unless that is what was requested.
If parent chose Quran Recitation — teach Quran recitation.
If parent chose Islamic Stories — tell a prophet story.
If parent chose Duas — teach duas.
If parent chose Surah Memorization — pick a surah and memorize it.
If parent chose Arabic Letters — then teach letters.
If no topic given — use student history to choose wisely.

VISION ALERTS [VISION: ...]:
React naturally as a teacher. Never say "vision alert" or "camera."

RESPONSE RULES:
Maximum 90 words. Speak naturally, no bullet points, no markdown.
Vary your teaching. Sometimes tell a vivid mini-story, sometimes ask a question, sometimes model pronunciation, sometimes praise and advance.
End with one clear next action: answer me, repeat after me, show me with your hand, or listen to the next part.

CRITICAL — ARABIC TEXT RULES:
Your response is READ ALOUD by a text-to-speech engine that CANNOT pronounce Arabic script.
NEVER write Arabic letters or words in Arabic script in your response.
ALWAYS write Arabic using English transliteration only.
Example — WRONG: "Say بِسْمِ اللَّهِ"
Example — RIGHT: "Say Bismillahi r-rahmani r-raheem"
The Arabic script will be shown on the visual blackboard separately. You only speak transliteration.
This is critical. Arabic script in your spoken response will sound broken and confuse the child."""

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

class ParentNoteCreate(BaseModel):
    student_id: str
    notes: str
    focus_topics: Optional[List[str]] = []

class HandRaiseRequest(BaseModel):
    student_id: Optional[str] = None
    lesson_id: Optional[str] = None
    session_id: Optional[str] = None
    image_b64: str

class TranscriptEntry(BaseModel):
    lesson_id: str
    student_id: str
    session_id: Optional[str] = None
    speaker: str  # 'teacher' | 'student'
    message: str
    mode: str = "TEACHING"

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

# ── Helper: build image content block ────────────────────
def image_block(b64: str) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}
    }

# ── Helper: log attention event ──────────────────────────
def log_attention(student_id: str, lesson_id: str, event_type: str, description: str):
    try:
        supabase_service.table("noor_attention_log").insert({
            "student_id": student_id,
            "lesson_id": lesson_id,
            "event_type": event_type,
            "description": description,
        }).execute()
    except Exception as e:
        print(f"Error logging attention: {e}")

# ═══════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════

@noor_router.get("/ping")
async def ping():
    return {"ok": True}

@noor_router.post("/chat")
async def chat(req: ChatRequest):
    claude_msgs = []
    for m in req.history[-14:]:
        claude_msgs.append({"role": m.role, "content": m.content})

    mode_tag = f"[MODE: {req.mode}]"
    if req.image_b64:
        user_content = [
            image_block(req.image_b64),
            {"type": "text", "text": f"{mode_tag} {req.message}"}
        ]
    else:
        user_content = f"{mode_tag} {req.message}"

    claude_msgs.append({"role": "user", "content": user_content})
    reply = await ai_service.call_claude(claude_msgs, NOOR_SYSTEM)

    if req.student_id and req.lesson_id:
        try:
            supabase_service.table("noor_lessons").update({
                "notes": f"Last message: {req.message[:100]}"
            }).eq("id", req.lesson_id).execute()
        except Exception:
            pass

    return {"reply": reply, "mode": req.mode}

@noor_router.post("/vision")
async def vision_check(req: VisionRequest):
    is_recitation = req.mode == "RECITATION"
    prompt = (
        "Look at this child carefully. "
        "Is the child looking at a book, paper, or phone screen? "
        "Are they looking at the camera or looking away? "
        "Reply with JSON only: "
        '{\"attention\": \"focused|distracted|absent\", '
        '\"cheating\": true|false, '
        '\"description\": \"one sentence\"}'
    ) if is_recitation else (
        "Look at this child. "
        "Are they paying attention, distracted, or not visible? "
        "Reply with JSON only: "
        '{\"attention\": \"focused|distracted|absent\", '
        '\"cheating\": false, '
        '\"description\": \"one sentence\"}'
    )

    raw = await ai_service.call_claude(
        [{"role": "user", "content": [image_block(req.image_b64), {"type": "text", "text": prompt}]}],
        "", # No system prompt needed for this direct check
        max_tokens=120,
        model=settings.claude_vision_model
    )

    try:
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        data = json.loads(match.group()) if match else {}
    except Exception:
        data = {}

    attention = data.get("attention", "focused")
    cheating = data.get("cheating", False)
    description = data.get("description", "")

    teacher_response = None
    event_type = "attentive"

    if cheating and is_recitation:
        event_type = "cheating"
        teacher_response = await ai_service.call_claude(
            [{"role": "user", "content": "[VISION: Child appears to be looking at a book or paper during Quran recitation. Catch them kindly but firmly.]"}],
            NOOR_SYSTEM
        )
    elif attention == "distracted":
        event_type = "distracted"
        teacher_response = await ai_service.call_claude(
            [{"role": "user", "content": "[VISION: Child is distracted, looking away from camera. Call them back to attention gently.]"}],
            NOOR_SYSTEM
        )
    elif attention == "absent":
        event_type = "absent"
        teacher_response = await ai_service.call_claude(
            [{"role": "user", "content": "[VISION: Child is not visible or has left the camera view.]"}],
            NOOR_SYSTEM
        )

    if req.student_id and req.lesson_id and event_type != "attentive":
        log_attention(req.student_id, req.lesson_id, event_type, description)

    return {
        "event_type": event_type,
        "attention": attention,
        "cheating": cheating,
        "description": description,
        "teacher_response": teacher_response,
    }

@noor_router.post("/transcribe")
async def transcribe_recitation(req: TranscribeRequest):
    audio_bytes = base64.b64decode(req.audio_b64)
    transcript = await ai_service.transcribe_audio(audio_bytes)

    surah_context = f"The child is reciting {req.surah}. " if req.surah else ""
    score_prompt = (
        f"{surah_context}The child recited: \"{transcript}\"\n"
        "Evaluate the Quran recitation. Point out any pronunciation or tajweed mistakes "
        "with the correct version. Be kind and specific. Max 3 corrections at a time."
    )

    feedback = await ai_service.call_claude([{"role": "user", "content": score_prompt}], NOOR_SYSTEM)

    return {
        "transcript": transcript,
        "feedback": feedback,
        "surah": req.surah,
    }

@noor_router.post("/homework")
async def grade_homework(student_id: str, image_b64: str, lesson_id: Optional[str] = None):
    prompt = (
        "[HOMEWORK SCAN] Read this homework carefully. "
        "Identify the subject (Arabic writing, Quran, Islamic studies). "
        "Grade it: what is correct, what needs fixing. "
        "Be specific, kind, and encouraging. Give a score out of 100."
    )

    feedback = await ai_service.call_claude(
        [{"role": "user", "content": [image_block(image_b64), {"type": "text", "text": prompt}]}],
        NOOR_SYSTEM,
        max_tokens=400
    )

    score_match = re.search(r'(\d+)\s*/\s*100', feedback)
    score = int(score_match.group(1)) if score_match else None
    grade = "excellent" if (score or 0) >= 85 else "good" if (score or 0) >= 60 else "needs_work"

    record = {
        "student_id": student_id,
        "feedback": feedback,
        "grade": grade,
        "score": score,
    }
    result = supabase_service.table("noor_homework").insert(record).execute()
    hw_id = result.data[0]["id"] if result.data else None

    return {"feedback": feedback, "grade": grade, "score": score, "homework_id": hw_id}

@noor_router.post("/students")
async def create_student(s: StudentCreate):
    result = supabase_service.table("noor_students").insert({
        "name": s.name,
        "age": s.age,
        "level": s.level,
    }).execute()
    student = result.data[0]

    supabase_service.table("noor_progress").insert({
        "student_id": student["id"]
    }).execute()

    return student

@noor_router.get("/students")
async def list_students():
    result = supabase_service.table("noor_students").select("*").order("created_at").execute()
    return result.data

@noor_router.get("/student/{student_id}")
async def get_student(student_id: str):
    student = supabase_service.table("noor_students").select("*").eq("id", student_id).single().execute().data
    progress = supabase_service.table("noor_progress").select("*").eq("student_id", student_id).maybe_single().execute().data
    lessons = supabase_service.table("noor_lessons").select("*").eq("student_id", student_id).order("started_at", desc=True).limit(10).execute().data
    homework = supabase_service.table("noor_homework").select("*").eq("student_id", student_id).order("submitted_at", desc=True).limit(5).execute().data

    return {
        "student": student,
        "progress": progress,
        "recent_lessons": lessons,
        "recent_homework": homework,
    }

@noor_router.post("/lesson/start")
async def start_lesson(req: StartLesson):
    result = supabase_service.table("noor_lessons").insert({
        "student_id": req.student_id,
    }).execute()
    lesson_id = result.data[0]["id"]

    supabase_service.table("noor_progress").update({
        "last_lesson_at": datetime.datetime.utcnow().isoformat()
    }).eq("student_id", req.student_id).execute()

    return {"lesson_id": lesson_id}

@noor_router.post("/lesson/end")
async def end_lesson(req: EndLesson):
    lesson = supabase_service.table("noor_lessons").select("started_at").eq("id", req.lesson_id).single().execute().data
    started = datetime.datetime.fromisoformat(lesson["started_at"].replace("Z", "+00:00"))
    ended = datetime.datetime.now(datetime.timezone.utc)
    duration = int((ended - started).total_seconds())
    minutes = duration // 60

    supabase_service.table("noor_lessons").update({
        "ended_at": ended.isoformat(),
        "duration_seconds": duration,
        "topics_covered": req.topics_covered,
        "summary": req.summary,
    }).eq("id", req.lesson_id).execute()

    prog = supabase_service.table("noor_progress").select("total_lessons,total_minutes").eq("student_id", req.student_id).single().execute().data
    supabase_service.table("noor_progress").update({
        "total_lessons": (prog["total_lessons"] or 0) + 1,
        "total_minutes": (prog["total_minutes"] or 0) + minutes,
        "updated_at": ended.isoformat(),
    }).eq("student_id", req.student_id).execute()

    return {"duration_seconds": duration, "minutes": minutes}

@noor_router.post("/progress")
async def update_progress(req: ProgressUpdate):
    prog = supabase_service.table("noor_progress").select("*").eq("student_id", req.student_id).single().execute().data
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

    supabase_service.table("noor_progress").update(updates).eq("student_id", req.student_id).execute()
    return {"updated": True}

@noor_router.get("/progress/{student_id}")
async def get_progress(student_id: str):
    result = supabase_service.table("noor_progress").select("*").eq("student_id", student_id).single().execute()
    return result.data

@noor_router.get("/homework/{student_id}")
async def get_homework(student_id: str):
    result = supabase_service.table("noor_homework").select("*").eq("student_id", student_id).order("submitted_at", desc=True).execute()
    return result.data

@noor_router.get("/attention/{lesson_id}")
async def get_attention_log(lesson_id: str):
    result = supabase_service.table("noor_attention_log").select("*").eq("lesson_id", lesson_id).order("logged_at").execute()
    return result.data

@noor_router.post("/parent-notes")
async def save_parent_notes(req: ParentNoteCreate):
    result = supabase_service.table("noor_parent_notes").insert({
        "student_id": req.student_id,
        "notes": req.notes,
        "focus_topics": req.focus_topics,
    }).execute()
    return result.data[0]

@noor_router.get("/parent-notes/{student_id}")
async def get_parent_notes(student_id: str):
    result = supabase_service.table("noor_parent_notes")\
        .select("*")\
        .eq("student_id", student_id)\
        .order("created_at", desc=True)\
        .limit(1)\
        .execute()
    return result.data[0] if result.data else None

@noor_router.patch("/parent-notes/{note_id}/used")
async def mark_note_used(note_id: str):
    supabase_service.table("noor_parent_notes").update({"used": True}).eq("id", note_id).execute()
    return {"marked": True}

@noor_router.post("/hand-raise")
async def detect_hand_raise(req: HandRaiseRequest):
    text = await ai_service.call_claude(
        [{"role": "user", "content": [image_block(req.image_b64), {"type": "text", "text": "Is any hand or arm raised or lifted upward in this image? Be generous — even a partial raise counts. Answer only: yes or no"}]}],
        "",
        max_tokens=10,
        model=settings.claude_vision_model
    )
    raised = "yes" in text.strip().lower()

    if raised and req.student_id and req.lesson_id:
        try:
            supabase_service.table("noor_hand_raises").insert({
                "lesson_id": req.lesson_id,
                "student_id": req.student_id,
                "session_id": req.session_id,
            }).execute()
            if req.session_id:
                sess = supabase_service.table("noor_sessions").select("hand_raises").eq("id", req.session_id).single().execute().data
                supabase_service.table("noor_sessions").update({
                    "hand_raises": (sess.get("hand_raises") or 0) + 1
                }).eq("id", req.session_id).execute()
        except Exception:
            pass

    return {"raised": raised}

@noor_router.post("/transcript/add")
async def add_transcript_entry(req: TranscriptEntry):
    result = supabase_service.table("noor_transcripts").insert({
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
    result = supabase_service.table("noor_transcripts")\
        .select("*")\
        .eq("lesson_id", lesson_id)\
        .order("timestamp")\
        .execute()
    return result.data

@noor_router.post("/session/start")
async def start_session(req: SessionStart):
    result = supabase_service.table("noor_sessions").insert({
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

    try:
        transcript = supabase_service.table("noor_transcripts")\
            .select("speaker,message")\
            .eq("lesson_id", req.lesson_id)\
            .order("timestamp")\
            .execute().data
        if transcript:
            convo = "\n".join([f"{t['speaker'].upper()}: {t['message']}" for t in transcript[-20:]])
            summary = await ai_service.call_claude([{
                "role": "user",
                "content": f"Summarize this Islamic lesson in 2-3 sentences for a parent report. What was covered, how did the student do?\n\n{convo}"
            }], NOOR_SYSTEM, max_tokens=150)
            updates["transcript_summary"] = summary
    except Exception:
        pass

    supabase_service.table("noor_sessions").update(updates).eq("id", req.session_id).execute()
    return {"ended": True}

@noor_router.get("/sessions/{student_id}")
async def get_sessions(student_id: str):
    result = supabase_service.table("noor_sessions")\
        .select("*")\
        .eq("student_id", student_id)\
        .order("started_at", desc=True)\
        .limit(20)\
        .execute()
    return result.data
