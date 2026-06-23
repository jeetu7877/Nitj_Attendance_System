from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import numpy as np
import base64, cv2, json
from pathlib import Path
import mediapipe as mp
from mediapipe.tasks import python
from mediapipe.tasks.python import vision
from mediapipe.tasks.python.vision import FaceLandmarker, FaceLandmarkerOptions
from mediapipe.tasks.python.core.base_options import BaseOptions
import urllib.request

app = FastAPI()

ENCODINGS_DIR = Path("face_data")
ENCODINGS_DIR.mkdir(exist_ok=True)

# Model download करो
MODEL_PATH = "face_landmarker.task"
if not Path(MODEL_PATH).exists():
    print("Downloading face landmarker model...")
    urllib.request.urlretrieve(
        "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
        MODEL_PATH
    )
    print("Model downloaded!")

options = FaceLandmarkerOptions(
    base_options=BaseOptions(model_asset_path=MODEL_PATH),
    num_faces=10,
    min_face_detection_confidence=0.5
)
detector = FaceLandmarker.create_from_options(options)

# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

def decode_image(base64_str: str) -> np.ndarray:
    if "," in base64_str:
        base64_str = base64_str.split(",")[1]
    img_bytes = base64.b64decode(base64_str)
    arr = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return img

def get_landmarks(image_b64: str):
    """Single face — enroll के लिए"""
    img = decode_image(image_b64)
    img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(
        image_format=mp.ImageFormat.SRGB,
        data=img_rgb
    )
    result = detector.detect(mp_image)
    if not result.face_landmarks:
        raise HTTPException(400, "No face detected in image.")
    lm = result.face_landmarks[0]
    embedding = []
    for point in lm:
        embedding.extend([point.x, point.y, point.z])
    return embedding

def get_all_landmarks(image_b64: str):
    """Multiple faces — recognize के लिए"""
    img = decode_image(image_b64)
    img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(
        image_format=mp.ImageFormat.SRGB,
        data=img_rgb
    )
    result = detector.detect(mp_image)
    if not result.face_landmarks:
        return []
    all_embeddings = []
    for face in result.face_landmarks:
        embedding = []
        for point in face:
            embedding.extend([point.x, point.y, point.z])
        all_embeddings.append(embedding)
    return all_embeddings

def cosine_similarity(a, b):
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
    embedding = get_landmarks(req.image)
    key = f"{req.classroom_id}_{req.student_id}"
    path = ENCODINGS_DIR / f"{key}.json"
    path.write_text(json.dumps(embedding))
    return {"success": True, "message": "Face enrolled successfully!"}

# ── RECOGNIZE ───────────────────────────────────────
class RecognizeRequest(BaseModel):
    classroom_id: str
    image: str
    tolerance: float = 0.993

@app.post("/recognize")
def recognize(req: RecognizeRequest):
    unknown_embeddings = get_all_landmarks(req.image)

    if not unknown_embeddings:
        return {"results": [], "message": "No face detected."}

    results = []
    pattern = f"{req.classroom_id}_"
    matched_students = set()

    for file in ENCODINGS_DIR.glob("*.json"):
        if not file.stem.startswith(pattern):
            continue

        student_id = file.stem.replace(pattern, "", 1)
        if student_id in matched_students:
            continue

        known_embedding = json.loads(file.read_text())

        best_similarity = 0
        for unknown_emb in unknown_embeddings:
            sim = cosine_similarity(unknown_emb, known_embedding)
            if sim > best_similarity:
                best_similarity = sim

        if best_similarity >= req.tolerance:
            results.append({
                "student_id": student_id,
                "confidence": round(best_similarity, 4),
                "status": "recognized"
            })
            matched_students.add(student_id)

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
        "service": "face-recognition (mediapipe)",
        "enrolled_faces": len(list(ENCODINGS_DIR.glob("*.json")))
    }
