from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import face_recognition
import numpy as np
import base64, cv2, json, os
from pathlib import Path

app = FastAPI()

# Face encodings को यहाँ store करेंगे (file-based simple storage)
ENCODINGS_DIR = Path("face_data")
ENCODINGS_DIR.mkdir(exist_ok=True)

def decode_image(base64_str: str) -> np.ndarray:
    """Base64 image → numpy array"""
    if "," in base64_str:
        base64_str = base64_str.split(",")[1]
    img_bytes = base64.b64decode(base64_str)
    arr = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

def get_encoding(image_b64: str):
    """Image से face encoding निकालो"""
    img = decode_image(image_b64)
    encs = face_recognition.face_encodings(img)
    if not encs:
        return None
    return encs[0]

# ── ENROLL (face save करो) ──────────────────────────
class EnrollRequest(BaseModel):
    student_id: str
    classroom_id: str
    image: str  # base64

@app.post("/enroll")
def enroll(req: EnrollRequest):
    enc = get_encoding(req.image)
    if enc is None:
        raise HTTPException(400, "No face detected in image.")
    
    key = f"{req.classroom_id}_{req.student_id}"
    path = ENCODINGS_DIR / f"{key}.json"
    path.write_text(json.dumps(enc.tolist()))
    
    return {"success": True, "message": "Face enrolled successfully!"}

# ── RECOGNIZE (attendance) ──────────────────────────
class RecognizeRequest(BaseModel):
    classroom_id: str
    image: str  # base64
    tolerance: float = 0.48

@app.post("/recognize")
def recognize(req: RecognizeRequest):
    # Camera image से encoding निकालो
    unknown_img = decode_image(req.image)
    unknown_encs = face_recognition.face_encodings(unknown_img)
    
    if not unknown_encs:
        return {"results": [], "message": "No face detected in image."}

    # उस classroom के सभी enrolled faces load करो
    results = []
    pattern = f"{req.classroom_id}_"
    
    for file in ENCODINGS_DIR.glob("*.json"):
        if not file.stem.startswith(pattern):
            continue
        
        student_id = file.stem.replace(pattern, "")
        known_enc = np.array(json.loads(file.read_text()))
        
        for unknown_enc in unknown_encs:
            dist = face_recognition.face_distance([known_enc], unknown_enc)[0]
            if dist <= req.tolerance:
                results.append({
                    "student_id": student_id,
                    "confidence": round(float(1 - dist), 3),
                    "status": "recognized"
                })
                break  # एक face एक बार match हो

    return {"results": results}

# ── CLEAR FACE (delete encoding) ───────────────────
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
    return {"status": "ok", "service": "face-recognition"}