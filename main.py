"""
Voicebot Assurance - Orchestrateur FastAPI
============================================

Ce service fait le lien entre :
  1. La voix de l'utilisateur (fichier audio reçu)         -> Whisper (Speech-to-Text)
  2. Le texte transcrit                                     -> Rasa (NLU + gestion du dialogue, via son API REST)
  3. La réponse texte de Rasa                               -> gTTS (Text-to-Speech)
  4. L'audio généré                                         -> renvoyé au client

Important : ce fichier NE remplace PAS le serveur Rasa. Il suppose que Rasa
tourne déjà séparément (voir README) et lui parle en HTTP.
"""

import logging
import os
import shutil
import tempfile
import uuid
from urllib.parse import quote

import requests
import whisper
from fastapi import FastAPI, Form, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from gtts import gTTS
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("voicebot")

RASA_REST_URL = os.getenv("RASA_REST_URL", "http://localhost:5005/webhooks/rest/webhook")
WHISPER_MODEL_NAME = os.getenv("WHISPER_MODEL", "tiny")

AUDIO_OUTPUT_DIR = os.getenv("AUDIO_OUTPUT_DIR", "audio_responses")
os.makedirs(AUDIO_OUTPUT_DIR, exist_ok=True)

DEFAULT_FALLBACK_REPLY = "Désolé, je n'ai pas compris votre demande. Pouvez-vous reformuler ?"

# 1. On initialise FastAPI une seule fois
app = FastAPI(
    title="Voicebot Assurance",
    description="Assistant vocal de déclaration de sinistres (Rasa + Whisper + gTTS)",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Chargement du modèle Whisper au démarrage
# ---------------------------------------------------------------------------

whisper_model = None


@app.on_event("startup")
def load_whisper_model() -> None:
    global whisper_model
    logger.info("Chargement du modèle Whisper '%s'...", WHISPER_MODEL_NAME)
    whisper_model = whisper.load_model(WHISPER_MODEL_NAME)
    logger.info("Modèle Whisper chargé.")


# ---------------------------------------------------------------------------
# Fonctions utilitaires
# ---------------------------------------------------------------------------


def transcribe_audio(file_path: str) -> str:
    """Transcrit un fichier audio en texte grâce à Whisper."""
    if whisper_model is None:
        raise HTTPException(status_code=503, detail="Le modèle Whisper n'est pas encore prêt.")
    result = whisper_model.transcribe(file_path, language="fr")
    return result.get("text", "").strip()


def ask_rasa(sender_id: str, message: str) -> list[str]:
    """Envoie le message utilisateur à Rasa et récupère la/les réponse(s) texte."""
    payload = {"sender": sender_id, "message": message}
    try:
        response = requests.post(RASA_REST_URL, json=payload, timeout=15)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Impossible de joindre Rasa sur %s : %s", RASA_REST_URL, exc)
        raise HTTPException(
            status_code=502,
            detail="Le serveur Rasa est injoignable. Vérifiez qu'il est bien lancé (rasa run --enable-api).",
        ) from exc

    data = response.json()
    return [item["text"] for item in data if "text" in item]


def synthesize_speech(text: str) -> str:
    """Génère un fichier audio MP3 à partir d'un texte, via gTTS."""
    output_path = os.path.join(AUDIO_OUTPUT_DIR, f"{uuid.uuid4()}.mp3")
    tts = gTTS(text=text, lang="fr")
    tts.save(output_path)
    return output_path


# ---------------------------------------------------------------------------
# Schémas de requête
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    sender_id: str = "test_user"
    message: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
async def serve_ui():
    """Sert l'interface Web HTML simple pour discuter avec le bot."""
    return FileResponse("index.html")


@app.get("/health")
def health_check():
    """Vérifie que l'API et Rasa répondent bien."""
    rasa_ok = True
    try:
        requests.post(RASA_REST_URL, json={"sender": "healthcheck", "message": "ping"}, timeout=3)
    except requests.RequestException:
        rasa_ok = False
    return {
        "api": "ok",
        "whisper_model_loaded": whisper_model is not None,
        "rasa_reachable": rasa_ok,
    }


@app.post("/chat")
def chat_text(payload: ChatRequest):
    """Endpoint texte, sans audio."""
    replies = ask_rasa(payload.sender_id, payload.message)
    if not replies:
        replies = [DEFAULT_FALLBACK_REPLY]
    return {"sender_id": payload.sender_id, "user_message": payload.message, "bot_replies": replies}


@app.post("/voice")
async def voice_endpoint(
    sender_id: str = Form(default="test_user"),
    audio: UploadFile = File(...),
):
    """Pipeline complet : audio entrant -> texte (Whisper) -> Rasa -> texte -> audio (gTTS)."""
    suffix = os.path.splitext(audio.filename or "")[1] or ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(audio.file, tmp)
        tmp_path = tmp.name

    try:
        user_text = transcribe_audio(tmp_path)
        logger.info("Utilisateur (%s) : %s", sender_id, user_text or "<vide>")

        if not user_text:
            bot_text = "Je n'ai rien entendu, pouvez-vous répéter ?"
        else:
            replies = ask_rasa(sender_id, user_text)
            bot_text = " ".join(replies) if replies else DEFAULT_FALLBACK_REPLY

        audio_path = synthesize_speech(bot_text)
    finally:
        os.remove(tmp_path)

    return FileResponse(
        audio_path,
        media_type="audio/mpeg",
        filename=os.path.basename(audio_path),
        headers={
            "X-Transcription": quote(user_text),
            "X-Bot-Reply": quote(bot_text),
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)