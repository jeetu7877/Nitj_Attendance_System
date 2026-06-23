from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import numpy as np
import base64, json, io
from pathlib import Path
from PIL import Image

app = FastAPI()

ENCODINGS_DIR = Path("face_data")
ENCODINGS_DIR.mkdir(exist_ok=True)

# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

def decode_image(base64_str: str) -> np.ndarray:
    if "," in base64_str:
        base64_str = base64_str.split(",")[1]
    img_bytes = base64.b64decode(base64_str)
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    img = img.resize((128, 128))
    return np.array(img, dtype=np.float32) / 255.0

def get_embedding(image_b64: str) -> list:
    """Image को flat normalized array बनाओ"""
    arr = decode_image(image_b64)
    return arr.flatten().tolist()

def cosine_similarity(a, b) -> float:
    a, b = np.array(a), np.array(b)
    dot = np.dot(a, b)
    norm = np.linalg.norm(a) * np.linalg.norm(b)
    return float(dot / norm) if norm != 0 else 0.0

# ══════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════

# ── ENROLL ──────────────────────────────────────────
class EnrollRequest(BaseModel):
    student_id: str
    classroom_id: str
    image: str

@app.post("/enroll")
def enroll(req: EnrollRequest):
    try:
        embedding = get_embedding(req.image)
        key = f"{req.classroom_id}_{req.student_id}"
        path = ENCODINGS_DIR / f"{key}.json"
        path.write_text(json.dumps(embedding))
        return {"success": True, "message": "Face enrolled successfully!"}
    except Exception as e:
        raise HTTPException(400, f"Enrollment failed: {str(e)}")

# ── RECOGNIZE ───────────────────────────────────────
class RecognizeRequest(BaseModel):
    classroom_id: str
    image: str
    tolerance: float = 0.98

@app.post("/recognize")
def recognize(req: RecognizeRequest):
    try:
        unknown_embedding = get_embedding(req.image)
    except Exception as e:
        return {"results": [], "message": str(e)}

    results = []
    pattern = f"{req.classroom_id}_"

    for file in ENCODINGS_DIR.glob("*.json"):
        if not file.stem.startswith(pattern):
            continue

        student_id = file.stem.replace(pattern, "", 1)
        known_embedding = json.loads(file.read_text())

        sim = cosine_similarity(unknown_embedding, known_embedding)

        if sim >= req.tolerance:
            results.append({
                "student_id": student_id,
                "confidence": round(sim, 4),
                "status": "recognized"
            })

    results.sort(key=lambda x: x["confidence"], reverse=True)
    return {"results": results}

# ── CLEAR ───────────────────────────────────────────
class ClearRequest(BaseModel):
    student_id: str
    classroom_id: str

@app.post("/clear")
def clear(req: ClearRequest):
    key = f"{req.classroom_id}_{req.student_id}"
    path = ENCODINGS_DIR / f"{key}.json"
    if path.exists():
        path.unlink()
    return {"success": True}

# ── HEALTH ──────────────────────────────────────────
@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "face-recognition (pixel-based)",
        "enrolled_faces": len(list(ENCODINGS_DIR.glob("*.json")))
    }
