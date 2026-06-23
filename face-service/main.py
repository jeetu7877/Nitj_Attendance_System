from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import numpy as np
import base64, cv2, json, os
from pathlib import Path
from deepface import DeepFace

app = FastAPI()

ENCODINGS_DIR = Path("face_data")
ENCODINGS_DIR.mkdir(exist_ok=True)

def decode_image(base64_str: str) -> np.ndarray:
    if "," in base64_str:
        base64_str = base64_str.split(",")[1]
    img_bytes = base64.b64decode(base64_str)
    arr = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return img

def get_embedding(image_b64: str):
    img = decode_image(image_b64)
    tmp_path = "/tmp/face_tmp.jpg"
    cv2.imwrite(tmp_path, img)
    try:
        result = DeepFace.represent(
            img_path=tmp_path,
            model_name="Facenet",
            enforce_detection=True
        )
        return result[0]["embedding"]
    except Exception as e:
        raise HTTPException(400, f"No face detected: {str(e)}")

# ── ENROLL ──────────────────────────────────────────
class EnrollRequest(BaseModel):
    student_id: str
    classroom_id: str
    image: str

@app.post("/enroll")
def enroll(req: EnrollRequest):
    embedding = get_embedding(req.image)
    key = f"{req.classroom_id}_{req.student_id}"
    path = ENCODINGS_DIR / f"{key}.json"
    path.write_text(json.dumps(embedding))
    return {"success": True, "message": "Face enrolled successfully!"}

# ── RECOGNIZE ───────────────────────────────────────
class RecognizeRequest(BaseModel):
    classroom_id: str
    image: str
    tolerance: float = 0.6

@app.post("/recognize")
def recognize(req: RecognizeRequest):
    unknown_embedding = get_embedding(req.image)
    unknown_arr = np.array(unknown_embedding)

    results = []
    pattern = f"{req.classroom_id}_"

    for file in ENCODINGS_DIR.glob("*.json"):
        if not file.stem.startswith(pattern):
            continue
        student_id = file.stem.replace(pattern, "", 1)
        known_arr = np.array(json.loads(file.read_text()))

        # Cosine similarity
        dot = np.dot(unknown_arr, known_arr)
        norm = np.linalg.norm(unknown_arr) * np.linalg.norm(known_arr)
        similarity = dot / norm if norm != 0 else 0

        if similarity >= req.tolerance:
            results.append({
                "student_id": student_id,
                "confidence": round(float(similarity), 3),
                "status": "recognized"
            })

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
    return {"status": "ok", "service": "face-recognition (deepface)"}
