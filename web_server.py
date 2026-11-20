"""
Flask server for the HealthCare Hospital website chat.

Endpoints:
  GET  /              -> serves the website
  POST /chat          -> takes {message, session_id}, returns reply
  POST /speak         -> takes {text}, returns human-like voice (MP3)
  POST /transcribe    -> takes audio file, returns {text} (Groq Whisper)
  GET  /health        -> server health check
  GET  /api/doctors    -> live doctor list for the website
  GET  /api/departments -> live department list for the website

Run:
    python web_server.py
    python web_server.py --https     (phone mic/voice needs HTTPS)

Then open on the PC: http://127.0.0.1:5000
Or on the phone (same Wi-Fi): http://<your-PC-IP>:5000
The server prints the phone link on startup.
"""

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS

# Jervis-style agent first (understands free-form messages via
# tools); the rule-based flow stays as the fallback if the agent
# raises. web_agent.py itself is untouched.
try:
    from agent import process_message as _agent_process_message
except Exception as _agent_import_error:
    print(f"Agent unavailable ({_agent_import_error}); "
          f"using rule-based flow.")
    _agent_process_message = None
from web_agent import process_message as _rule_process_message


def process_message(message, session):
    if _agent_process_message is not None:
        try:
            return _agent_process_message(message, session)
        except Exception:
            import traceback
            print("Agent error, falling back to rule-based flow:")
            print(traceback.format_exc())
    return _rule_process_message(message, session)

app = Flask(__name__, template_folder="templates", static_folder="static")
CORS(app)


# In-memory session store: {session_id: {state, patient_name, ...}}
# For production use Redis or a DB, but this is fine for testing.
SESSIONS = {}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok", "sessions": len(SESSIONS)})


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json() or {}
    message = (data.get("message") or "").strip()
    session_id = data.get("session_id") or "default"

    if not message:
        return jsonify({"error": "empty message"}), 400

    session = SESSIONS.setdefault(session_id, {})
    try:
        reply = process_message(message, session)
    except Exception as e:
        import traceback
        print("Chat error:", traceback.format_exc())
        reply = "Sorry, something went wrong. Please try again."

    return jsonify({"reply": reply, "state": session.get("state", "idle")})


@app.route("/speak", methods=["POST"])
def speak():
    """Text -> human-like voice (MP3), free Microsoft Edge TTS.

    Body: {"text": "...", "voice": "optional voice name"}.
    The voice is picked from the text itself (Bangla script ->
    bn-BD-NabanitaNeural, anything else -> en-US-AriaNeural).
    On failure (edge-tts not installed / no network) returns 503
    JSON — the website then falls back to the browser's built-in
    voice, so voice never breaks the chat.
    """
    data = request.get_json() or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "empty text"}), 400
    try:
        from tts import synthesize
        audio = synthesize(text, voice=data.get("voice") or None)
    except ImportError:
        return jsonify(
            {"error": "edge-tts is not installed — run: pip install edge-tts"}
        ), 503
    except Exception as e:
        print("TTS error:", e)
        return jsonify({"error": "voice generation failed"}), 503
    if not audio:
        return jsonify({"error": "nothing to speak"}), 400
    from flask import Response
    return Response(audio, mimetype="audio/mpeg")


@app.route("/transcribe", methods=["POST"])
def transcribe():
    """Voice -> text with Groq Whisper (free with the existing key).

    The website records the mic (MediaRecorder) and POSTs the audio
    as multipart form field "audio", plus an optional "language"
    field ("bn" or "en") from the EN / বাংলা toggle. The language
    is passed to Whisper as a hint: without it Whisper auto-detects,
    and on short/noisy clips it hallucinates other languages
    (Russian, Portuguese) instead of hearing Bangla/English.
    Same model + SDK call as the Telegram bot's voice.py
    (whisper-large-v3), so no new account, key or payment is needed.

    Returns {"text": "..."} (empty string when nothing was heard).
    On failure (no GROQ_API_KEY / Groq error) returns 503 JSON —
    the website then falls back to the browser's built-in listener,
    so voice never breaks the chat.
    """
    if "audio" not in request.files:
        return jsonify({"error": "no audio file"}), 400
    audio = request.files["audio"]
    if not audio or not audio.filename:
        return jsonify({"error": "empty audio file"}), 400

    import os
    import tempfile
    from dotenv import load_dotenv
    load_dotenv()
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key:
        return jsonify({"error": "GROQ_API_KEY is missing"}), 503

    # Groq needs a filename with an audio extension. The browser
    # sends webm (Chrome/Android) or mp4 (Safari); keep what it sent.
    filename = audio.filename or "voice.webm"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".webm", ".ogg", ".mp3", ".mp4", ".m4a", ".wav"):
        ext = ".webm"
    tmp_path = None
    try:
        from groq import Groq
        client = Groq(api_key=groq_key)
        with tempfile.NamedTemporaryFile(
            delete=False, suffix=ext
        ) as tmp:
            audio.save(tmp.name)
            tmp_path = tmp.name
        # Language hint from the page (toggle: "bn" or "en"). Without
        # it Whisper auto-detects — and on short/noisy clips it
        # hallucinates other languages (Russian "Продолжение следует",
        # Portuguese "É isso aí"). The hint + prompt pin it to the
        # language the patient is actually speaking.
        lang = (request.form.get("language") or "").strip().lower()
        if lang not in ("bn", "en"):
            lang = None
        prompts = {
            "bn": "ডাক্তার অ্যাপয়েন্টমেন্ট হাসপাতাল সময়সূচি আয়েশা সুফান রানা মিনা",
            "en": "doctor appointment hospital schedule Ayesha Sufan Rana Mina",
        }
        kwargs = {"model": "whisper-large-v3", "response_format": "text"}
        if lang:
            kwargs["language"] = lang
            kwargs["prompt"] = prompts[lang]
        with open(tmp_path, "rb") as f:
            result = client.audio.transcriptions.create(
                file=(os.path.basename(tmp_path), f),
                **kwargs,
            )
        text = result if isinstance(result, str) else (
            getattr(result, "text", "") or ""
        )
        return jsonify({"text": (text or "").strip()})
    except Exception as e:
        print("Transcribe error:", e)
        return jsonify({"error": "voice recognition failed"}), 503
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass


@app.route("/api/doctors")
def api_doctors():
    """Website-এর জন্য সব doctor. -> [{name, department, specialization}]

    Reads the database (Telegram/code changes show up here);
    falls back to the built-in list when the DB is not seeded yet.
    """
    from clinic_data import CLINIC_DOCTORS, get_all_doctors
    try:
        doctors = get_all_doctors()
    except Exception:
        doctors = []
    if not doctors:
        doctors = CLINIC_DOCTORS
    return jsonify([
        {
            "name": d["name"],
            "department": d["department"],
            "specialization": d.get("specialization", "") or d["department"],
        }
        for d in doctors
    ])


@app.route("/api/departments")
def api_departments():
    """Website-এর জন্য সব department. -> [{name}]"""
    try:
        from clinic_data import get_departments
        depts = get_departments()
    except Exception:
        depts = []
    if not depts:
        # Fresh deploy without a migrated DB: fall back to the
        # hardcoded doctor list so the website never gets an empty list.
        from clinic_data import CLINIC_DOCTORS
        depts = sorted({d["department"] for d in CLINIC_DOCTORS})
    return jsonify([{"name": d} for d in depts])


def _lan_ip():
    """This PC's Wi-Fi/LAN address, so the phone can reach it."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    import argparse
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--https", action="store_true",
        help="Serve over HTTPS (self-signed cert). Needed for the "
             "phone microphone/voice — browsers block the mic on "
             "plain http://192.168.x.x links.",
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("PORT", "5000")),
    )
    args = parser.parse_args()

    scheme = "https" if args.https else "http"
    print("Starting HealthCare Hospital web server...")
    print(f"Open on this PC: {scheme}://127.0.0.1:{args.port}")
    print(f"Open on your phone (same Wi-Fi): "
          f"{scheme}://{_lan_ip()}:{args.port}")
    if args.https:
        print("HTTPS: your browser will warn about the certificate — "
              "tap Advanced / Proceed to continue.")

    # Auto-sync: file <-> database, no restarts needed.
    # - Startup: any DB-only doctors are adopted into the file,
    #   then the file is synced into the DB.
    # - Watcher: every clinic_data.py edit reaches the DB within
    #   a few seconds (Telegram changes write back to the file).
    from sync import converge_on_startup, start_file_watcher
    converge_on_startup()
    start_file_watcher()

    # Start the per-doctor cutoff report scheduler in a
    # background thread. Fires an email report for each doctor
    # 30 minutes before their chamber starts.
    from scheduler import start_scheduler
    start_scheduler()

    if args.https:
        # adhoc cert needs pyopenssl (in requirements.txt)
        app.run(host="0.0.0.0", port=args.port, debug=False,
                ssl_context="adhoc")
    else:
        app.run(host="0.0.0.0", port=args.port, debug=False)