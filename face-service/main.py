from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import numpy as np
import base64, cv2, json
from pathlib import Path
import mediapipe as mp

app = FastAPI()

ENCODINGS_DIR = Path("face_data")
ENCODINGS_DIR.mkdir(exist_ok=True)

# MediaPipe Face Mesh
mp_face_mesh = mp.solutions.face_mesh
face_mesh = mp_face_mesh.FaceMesh(
    static_image_mode=True,
    max_num_faces=10,
    refine_landmarks=True,
    min_detection_confidence=0.5
)

def decode_image(base64_str: str) -> np.ndarray:
    if "," in base64_str:
        base64_str = base64_str.split(",")[1]
    img_bytes = base64.b64decode(base64_str)
    arr = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return img

def get_landmarks(image_b64: str):
    """Image से face landmarks निकालो — यही हमारा embedding है"""
    img = decode_image(image_b64)
    img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    h, w = img_rgb.shape[:2]
    
    results = face_mesh.process(img_rgb)
    if not results.multi_face_landmarks:
        raise HTTPException(400, "No face detected in image.")
    
    # पहले face के landmarks को flat array बनाओ
    lm = results.multi_face_landmarks[0].landmark
    embedding = []
    for point in lm:
        embedding.extend([point.x, point.y, point.z])
    
    return embedding

def get_all_landmarks(image_b64: str):
    """सभी faces के landmarks निकालो (recognize के लिए)"""
    img = decode_image(image_b64)
    img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    
    results = face_mesh.process(img_rgb)
    if not results.multi_face_landmarks:
        return []
    
    all_embeddings = []
    for face_landmarks in results.multi_face_landmarks:
        embedding = []
        for point in face_landmarks.landmark:
            embedding.extend([point.x, point.y, point.z])
        all_embeddings.append(embedding)
    
    return all_embeddings

def cosine_similarity(a, b):
    a, b = np.array(a), np.array(b)
    dot = np.dot(a, b)
    norm = np.linalg.norm(a) * np.linalg.norm(b)
    return float(dot / norm) if norm != 0 else 0.0

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
    tolerance: float = 0.993  # MediaPipe के लिए high similarity चाहिए

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
        
        # हर detected face से compare करो
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
