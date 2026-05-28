"""
NITJ Classroom — Backend LITE v10.2 (Render/Railway Deploy)
============================================================
Ye file Render/Railway pe deploy hoti hai.
Face recognition NAHI hai — iske liye alag face-service hai.
 
RAM: ~200MB max (Render free tier compatible)
 
Install:
    pip install -r requirements.txt
 
Run:
    uvicorn main:app --host 0.0.0.0 --port $PORT
"""
 
import os, io, base64, pickle, sqlite3, logging, uuid, secrets, hashlib, re
from datetime import date, datetime, timedelta
from typing import Optional
import numpy as np
from PIL import Image
 
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
 
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from passlib.context import CryptContext
from jose import JWTError, jwt
 
try:
    import aiosmtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    SMTP_OK = True
except ImportError:
    SMTP_OK = False
 
try:
    import dns.resolver
    DNS_OK = True
except ImportError:
    DNS_OK = False
 
# Face recognition NOT imported here — handled by face-service
FR_OK = False
 
# ══════════════════════════════════════════════════════════════
# CONFIG  (set these in Render Environment Variables)
# ══════════════════════════════════════════════════════════════
DB_PATH    = "nitj.db"
PKL_PATH   = "faces.pkl"
UPLOAD_DIR = "uploads"
 
SECRET_KEY = os.getenv("SECRET_KEY", "nitj-change-this-2025")
ALGORITHM  = "HS256"
TOKEN_EXP  = 60 * 24 * 7   # 7 days
 
SMTP_HOST  = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT  = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER  = os.getenv("SMTP_USER", "")
SMTP_PASS  = os.getenv("SMTP_PASS", "")
FROM_NAME  = "NITJ Classroom"
FROM_ADDR  = SMTP_USER or "noreply@nitj.ac.in"
 
# Face Service URL (your second deployment — Railway/Fly.io)
FACE_SERVICE_URL = os.getenv("FACE_SERVICE_URL", "")
 
OTP_EXPIRY_MIN   = 10
OTP_MAX_ATTEMPTS = 5
RESEND_COOLDOWN  = 60
TOLERANCE        = 0.48
 
BLOCKED = {
    "tempmail.com","temp-mail.org","guerrillamail.com","mailinator.com",
    "throwaway.email","yopmail.com","10minutemail.com","trashmail.com",
    "maildrop.cc","fakeinbox.com","dispostable.com","spam4.me",
}
 
os.makedirs(UPLOAD_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)
 
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer  = HTTPBearer(auto_error=False)
 
app = FastAPI(title="NITJ Classroom API Lite", version="10.2.0")
 
@app.exception_handler(RequestValidationError)
async def val_err(req, exc):
    msgs = [e["msg"] for e in exc.errors()]
    return JSONResponse(422, {"detail": "; ".join(msgs)})
 
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
 
try:
    app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
except Exception:
    pass
 
# ══════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════
def get_db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c
 
def init_db():
    c = get_db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE, hashed_password TEXT NOT NULL,
        department TEXT DEFAULT '', email_verified INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS password_resets (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
        otp_hash TEXT NOT NULL, expires_at TEXT NOT NULL,
        used INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS classrooms (
        id TEXT PRIMARY KEY, creator_id TEXT NOT NULL,
        name TEXT NOT NULL, subject TEXT NOT NULL,
        branch TEXT NOT NULL, year INTEGER NOT NULL,
        section TEXT NOT NULL, code TEXT NOT NULL UNIQUE,
        description TEXT DEFAULT '', banner_color TEXT DEFAULT '#1565C0',
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (creator_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS classroom_members (
        id TEXT PRIMARY KEY, classroom_id TEXT NOT NULL,
        user_id TEXT NOT NULL, roll_number TEXT DEFAULT '',
        branch TEXT DEFAULT '', year INTEGER DEFAULT 1,
        section TEXT DEFAULT '', face_enrolled INTEGER DEFAULT 0,
        face_locked INTEGER DEFAULT 0, face_updated_at TEXT DEFAULT '',
        is_admin INTEGER DEFAULT 0,
        joined_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (classroom_id) REFERENCES classrooms(id),
        FOREIGN KEY (user_id) REFERENCES users(id),
        UNIQUE(classroom_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS face_audit_logs (
        id TEXT PRIMARY KEY, classroom_id TEXT NOT NULL,
        student_id TEXT NOT NULL, action TEXT NOT NULL,
        performed_by TEXT NOT NULL,
        performed_at TEXT DEFAULT (datetime('now')),
        notes TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS attendance (
        id TEXT PRIMARY KEY, classroom_id TEXT NOT NULL,
        student_id TEXT NOT NULL, date TEXT NOT NULL,
        time TEXT NOT NULL, status TEXT DEFAULT 'present',
        confidence REAL DEFAULT 0,
        UNIQUE(classroom_id, student_id, date)
    );
    CREATE TABLE IF NOT EXISTS posts (
        id TEXT PRIMARY KEY, classroom_id TEXT NOT NULL,
        user_id TEXT NOT NULL, type TEXT DEFAULT 'announcement',
        title TEXT NOT NULL, content TEXT DEFAULT '',
        file_url TEXT DEFAULT '', file_name TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS comments (
        id TEXT PRIMARY KEY, post_id TEXT NOT NULL,
        user_id TEXT NOT NULL, comment TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS assignments (
        id TEXT PRIMARY KEY, classroom_id TEXT NOT NULL,
        creator_id TEXT NOT NULL, title TEXT NOT NULL,
        description TEXT DEFAULT '', file_url TEXT DEFAULT '',
        file_name TEXT DEFAULT '', due_date TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS assignment_submissions (
        id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL,
        student_id TEXT NOT NULL, file_url TEXT DEFAULT '',
        file_name TEXT DEFAULT '',
        submitted_at TEXT DEFAULT (datetime('now')),
        status TEXT DEFAULT 'submitted',
        UNIQUE(assignment_id, student_id)
    );
    CREATE TABLE IF NOT EXISTS notifications (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
        title TEXT NOT NULL, message TEXT NOT NULL,
        type TEXT DEFAULT 'info', read INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    );
    """)
    c.commit(); c.close()
    log.info("✅ DB ready")
 
init_db()
 
# ══════════════════════════════════════════════════════════════
# EMAIL / OTP HELPERS
# ══════════════════════════════════════════════════════════════
def hash_otp(otp: str) -> str:
    return hashlib.sha256(otp.encode()).hexdigest()
 
def gen_otp() -> str:
    return str(secrets.randbelow(900000) + 100000)
 
def validate_email_addr(email: str) -> tuple:
    email = email.lower().strip()
    if not re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", email):
        return False, "Invalid email format."
    domain = email.split("@")[1].lower()
    if domain in BLOCKED:
        return False, "Disposable email domains not allowed."
    return True, ""
 
def build_reset_email(name: str, otp: str) -> tuple:
    subject = "NITJ Classroom — Password Reset Code"
    html = f"""<!DOCTYPE html><html><body style="background:#EDF2FB;font-family:Arial,sans-serif;margin:0;padding:32px 16px;">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table style="max-width:520px;width:100%;">
<tr><td style="background:linear-gradient(135deg,#0a1f38,#1565C0);border-radius:16px 16px 0 0;padding:28px;text-align:center;">
  <div style="font-size:22px;font-weight:800;color:#fff;">NITJ Classroom</div>
  <div style="font-size:11px;color:rgba(255,255,255,.55);">Dr. B. R. Ambedkar NIT Jalandhar</div>
</td></tr>
<tr><td style="background:#fff;padding:32px;">
  <h2 style="color:#0a1f38;margin:0 0 12px;">🔐 Reset Your Password</h2>
  <p style="color:#4a6080;font-size:14px;line-height:1.7;margin:0 0 24px;">
    Hi <strong>{name}</strong>, use the code below to reset your password.<br/>
    This code expires in <strong>10 minutes</strong>.
  </p>
  <div style="background:#EEF4FB;border:2px dashed #BBDEFB;border-radius:12px;padding:24px;text-align:center;margin-bottom:24px;">
    <div style="font-size:11px;color:#7a90ab;font-weight:700;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px;">Your Reset Code</div>
    <div style="font-size:44px;font-weight:900;color:#1565C0;letter-spacing:12px;font-family:'Courier New',monospace;">{otp}</div>
    <div style="font-size:12px;color:#7a90ab;margin-top:10px;">⏱ Valid for 10 minutes only</div>
  </div>
  <div style="background:#FEF3C7;border:1px solid #FCD34D;border-radius:8px;padding:12px 16px;font-size:13px;color:#92400e;">
    🔒 Never share this code. NITJ Classroom will never call or message you for this.
  </div>
  <p style="font-size:12px;color:#7a90ab;margin-top:18px;">If you didn't request this, ignore this email.</p>
</td></tr>
<tr><td style="background:#F5F8FF;border-radius:0 0 16px 16px;padding:16px;text-align:center;border-top:1px solid #DDE8F7;">
  <p style="font-size:11px;color:#7a90ab;margin:0;">© 2025 NIT Jalandhar · NITJ Classroom Platform · Automated email</p>
</td></tr>
</table></td></tr></table>
</body></html>"""
    return subject, html
 
async def send_email(to: str, name: str, subject: str, html: str) -> bool:
    if not SMTP_OK or not SMTP_USER or not SMTP_PASS:
        log.warning("SMTP not configured — set SMTP_USER + SMTP_PASS env vars")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{FROM_NAME} <{FROM_ADDR}>"
        msg["To"]      = f"{name} <{to}>"
        msg.attach(MIMEText(html, "html", "utf-8"))
        await aiosmtplib.send(
            msg, hostname=SMTP_HOST, port=SMTP_PORT,
            start_tls=True, username=SMTP_USER, password=SMTP_PASS, timeout=20)
        log.info("✅ Email sent → %s", to)
        return True
    except Exception as e:
        log.error("❌ Email failed → %s : %s", to, e)
        return False
 
async def _issue_reset_otp(user_id: str, conn) -> str:
    conn.execute("UPDATE password_resets SET used=1 WHERE user_id=? AND used=0", (user_id,))
    otp  = gen_otp()
    exp  = (datetime.utcnow() + timedelta(minutes=OTP_EXPIRY_MIN)).isoformat()
    conn.execute(
        "INSERT INTO password_resets (id,user_id,otp_hash,expires_at) VALUES (?,?,?,?)",
        (str(uuid.uuid4()), user_id, hash_otp(otp), exp))
    conn.commit()
    return otp
 
# ══════════════════════════════════════════════════════════════
# FACE STORE  (shared with face-service via same disk or env)
# ══════════════════════════════════════════════════════════════
def load_faces() -> dict:
    if os.path.exists(PKL_PATH):
        try:
            with open(PKL_PATH, "rb") as f:
                return pickle.load(f)
        except Exception as e:
            log.warning("Face store corrupted: %s", e)
    return {}
 
def save_faces(store: dict):
    with open(PKL_PATH, "wb") as f:
        pickle.dump(store, f)
 
def push_notif(user_id, title, message, ntype="info"):
    c = get_db()
    c.execute(
        "INSERT INTO notifications (id,user_id,title,message,type) VALUES (?,?,?,?,?)",
        (str(uuid.uuid4()), user_id, title, message, ntype))
    c.commit(); c.close()
 
# ══════════════════════════════════════════════════════════════
# JWT
# ══════════════════════════════════════════════════════════════
def make_token(uid: str) -> str:
    return jwt.encode(
        {"sub": uid, "exp": datetime.utcnow() + timedelta(minutes=TOKEN_EXP)},
        SECRET_KEY, algorithm=ALGORITHM)
 
def get_uid(creds: HTTPAuthorizationCredentials = Depends(bearer)):
    if not creds: raise HTTPException(401, "Not authenticated")
    try:
        p = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        uid = p.get("sub")
        if not uid: raise HTTPException(401, "Invalid token")
        return uid
    except JWTError:
        raise HTTPException(401, "Session expired. Please login again.")
 
def get_user(uid=Depends(get_uid)):
    c = get_db()
    row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    c.close()
    if not row: raise HTTPException(401, "User not found")
    u = dict(row); u.pop("hashed_password", None)
    return u
 
def need_admin(classroom_id: str, user_id: str):
    c = get_db()
    r = c.execute(
        "SELECT is_admin FROM classroom_members WHERE classroom_id=? AND user_id=?",
        (classroom_id, user_id)).fetchone()
    c.close()
    if not r or not r["is_admin"]:
        raise HTTPException(403, "Admin access required.")
 
# ══════════════════════════════════════════════════════════════
# SCHEMAS
# ══════════════════════════════════════════════════════════════
class RegisterReq(BaseModel):
    name: str; email: str; password: str; department: str = ""
 
class LoginReq(BaseModel):
    email: str; password: str
 
class ForgotReq(BaseModel):
    email: str
 
class ResetReq(BaseModel):
    email: str; otp: str; new_password: str
 
class CreateClsReq(BaseModel):
    name: str; subject: str; branch: str; year: int
    section: str; description: str = ""; banner_color: str = "#1565C0"
 
class JoinClsReq(BaseModel):
    code: str; roll_number: str = ""; branch: str = ""
    year: int = 1; section: str = ""; image: str = ""
 
class PostReq(BaseModel):
    classroom_id: str; type: str = "announcement"
    title: str; content: str = ""
 
class CommentReq(BaseModel):
    post_id: str; comment: str
 
class RecognizeReq(BaseModel):
    classroom_id: str; image: str
 
class UpdateDueReq(BaseModel):
    assignment_id: str; due_date: str
 
class FaceResetReq(BaseModel):
    classroom_id: str; student_id: str; image: str; notes: str = ""
 
# ══════════════════════════════════════════════════════════════
# AUTH ROUTES
# ══════════════════════════════════════════════════════════════
@app.post("/register")
async def register(req: RegisterReq):
    email = req.email.lower().strip()
    ok, err = validate_email_addr(email)
    if not ok: raise HTTPException(400, err)
    if not req.name.strip(): raise HTTPException(400, "Name required.")
    if len(req.password) < 6: raise HTTPException(400, "Password min 6 chars.")
    c = get_db()
    if c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
        c.close(); raise HTTPException(400, "Email already registered.")
    uid = str(uuid.uuid4())
    c.execute(
        "INSERT INTO users (id,name,email,hashed_password,department,email_verified) VALUES (?,?,?,?,?,1)",
        (uid, req.name.strip(), email, pwd_ctx.hash(req.password), req.department))
    c.commit(); c.close()
    return {"success": True, "token": make_token(uid),
            "message": f"Welcome to NITJ Classroom, {req.name.strip()} 🎉"}
 
# Alias for frontend compatibility
@app.post("/send_otp")
async def send_otp_alias(req: RegisterReq):
    return await register(req)
 
@app.post("/login")
async def login(req: LoginReq):
    c   = get_db()
    row = c.execute("SELECT * FROM users WHERE email=?", (req.email.lower().strip(),)).fetchone()
    c.close()
    if not row: raise HTTPException(401, "No account with this email.")
    if not pwd_ctx.verify(req.password, row["hashed_password"]):
        raise HTTPException(401, "Incorrect password.")
    return {"success": True, "token": make_token(row["id"]),
            "message": f"Welcome back, {row['name']}!"}
 
@app.get("/me")
async def me(user=Depends(get_user)):
    return user
 
@app.post("/forgot_password")
async def forgot_password(req: ForgotReq):
    email = req.email.lower().strip()
    c     = get_db()
    user  = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user: c.close(); raise HTTPException(404, "No account with this email.")
    last = c.execute(
        "SELECT created_at FROM password_resets WHERE user_id=? AND used=0 "
        "ORDER BY created_at DESC LIMIT 1", (user["id"],)).fetchone()
    if last:
        elapsed = (datetime.utcnow()-datetime.fromisoformat(last["created_at"])).total_seconds()
        if elapsed < RESEND_COOLDOWN:
            c.close(); raise HTTPException(429, f"Wait {int(RESEND_COOLDOWN-elapsed)}s.")
    otp = await _issue_reset_otp(user["id"], c); c.close()
    subject, html = build_reset_email(user["name"], otp)
    sent = await send_email(email, user["name"], subject, html)
    resp = {"success": True, "message": f"Reset code sent to {email}."}
    if not sent: resp["smtp_warning"] = "SMTP not configured. Check server logs."
    return resp
 
@app.post("/reset_password")
async def reset_password(req: ResetReq):
    if len(req.new_password) < 6: raise HTTPException(400, "Password min 6 chars.")
    email = req.email.lower().strip(); c = get_db()
    row = c.execute(
        "SELECT pr.*,u.id uid FROM password_resets pr JOIN users u ON pr.user_id=u.id "
        "WHERE u.email=? AND pr.used=0 ORDER BY pr.created_at DESC LIMIT 1", (email,)).fetchone()
    if not row: c.close(); raise HTTPException(400, "No pending reset.")
    if row["attempts"] >= OTP_MAX_ATTEMPTS: c.close(); raise HTTPException(429, "Too many attempts.")
    if datetime.fromisoformat(row["expires_at"]) < datetime.utcnow():
        c.close(); raise HTTPException(400, "OTP expired.")
    if hash_otp(req.otp.strip()) != row["otp_hash"]:
        c.execute("UPDATE password_resets SET attempts=attempts+1 WHERE id=?", (row["id"],))
        c.commit(); c.close(); raise HTTPException(400, "Invalid OTP.")
    c.execute("UPDATE users SET hashed_password=? WHERE id=?",
              (pwd_ctx.hash(req.new_password), row["uid"]))
    c.execute("UPDATE password_resets SET used=1 WHERE id=?", (row["id"],))
    c.commit(); c.close()
    return {"success": True, "message": "Password reset! Please login."}
 
@app.post("/verify_otp")
async def verify_otp_alias(req: ResetReq):
    return await reset_password(req)
 
# ══════════════════════════════════════════════════════════════
# CLASSROOMS
# ══════════════════════════════════════════════════════════════
@app.post("/create_classroom")
async def create_classroom(req: CreateClsReq, user=Depends(get_user)):
    code = secrets.token_hex(3).upper(); c = get_db()
    while c.execute("SELECT id FROM classrooms WHERE code=?", (code,)).fetchone():
        code = secrets.token_hex(3).upper()
    cid = str(uuid.uuid4())
    c.execute(
        "INSERT INTO classrooms (id,creator_id,name,subject,branch,year,section,code,description,banner_color) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (cid, user["id"], req.name, req.subject, req.branch, req.year, req.section, code, req.description, req.banner_color))
    c.execute(
        "INSERT INTO classroom_members (id,classroom_id,user_id,roll_number,is_admin,face_enrolled,face_locked) VALUES (?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), cid, user["id"], "ADMIN", 1, 0, 0))
    c.commit(); c.close()
    return {"success": True, "classroom_id": cid, "code": code,
            "message": f"Classroom '{req.name}' created! Code: {code}"}
 
@app.get("/classrooms")
async def get_classrooms(user=Depends(get_user)):
    c = get_db()
    rows = c.execute("""
        SELECT cl.*, u.name creator_name, cm.is_admin,
               (SELECT COUNT(*) FROM classroom_members WHERE classroom_id=cl.id) AS member_count,
               (SELECT COUNT(*) FROM assignments WHERE classroom_id=cl.id AND due_date>=date('now')) AS upcoming_assignments
        FROM classrooms cl JOIN users u ON cl.creator_id=u.id
        JOIN classroom_members cm ON cm.classroom_id=cl.id AND cm.user_id=?
        ORDER BY cm.joined_at DESC""", (user["id"],)).fetchall()
    c.close(); return [dict(r) for r in rows]
 
@app.get("/classroom/{cid}")
async def get_classroom(cid: str, user=Depends(get_user)):
    c = get_db()
    row = c.execute(
        "SELECT cl.*,u.name creator_name FROM classrooms cl JOIN users u ON cl.creator_id=u.id WHERE cl.id=?",
        (cid,)).fetchone()
    if not row: c.close(); raise HTTPException(404, "Classroom not found.")
    mem = c.execute(
        "SELECT is_admin,face_enrolled,face_locked,face_updated_at FROM classroom_members WHERE classroom_id=? AND user_id=?",
        (cid, user["id"])).fetchone()
    if not mem: c.close(); raise HTTPException(403, "Not a member.")
    d = dict(row)
    d["is_admin"]        = bool(mem["is_admin"])
    d["face_enrolled"]   = bool(mem["face_enrolled"])
    d["face_locked"]     = bool(mem["face_locked"])
    d["face_updated_at"] = mem["face_updated_at"]
    d["member_count"]    = c.execute("SELECT COUNT(*) FROM classroom_members WHERE classroom_id=?", (cid,)).fetchone()[0]
    d["upcoming_assignments"] = c.execute(
        "SELECT COUNT(*) FROM assignments WHERE classroom_id=? AND due_date>=date('now')", (cid,)).fetchone()[0]
    c.close(); return d
 
@app.delete("/classroom/{cid}")
async def delete_classroom(cid: str, user=Depends(get_user)):
    need_admin(cid, user["id"]); c = get_db()
    for t in ["classroom_members","attendance","posts","comments","assignments","assignment_submissions","face_audit_logs"]:
        try: c.execute(f"DELETE FROM {t} WHERE classroom_id=?", (cid,))
        except: pass
    c.execute("DELETE FROM classrooms WHERE id=?", (cid,)); c.commit(); c.close()
    s = load_faces()
    for k in [k for k in s if s[k].get("classroom_id") == cid]: del s[k]
    save_faces(s)
    return {"success": True}
 
@app.get("/classroom/{cid}/members")
async def get_members(cid: str, user=Depends(get_user)):
    c = get_db()
    rows = c.execute("""
        SELECT u.id, u.name, u.email, cm.roll_number, cm.branch, cm.year, cm.section,
               cm.face_enrolled, cm.face_locked, cm.is_admin, cm.joined_at, cm.face_updated_at
        FROM classroom_members cm JOIN users u ON cm.user_id=u.id
        WHERE cm.classroom_id=? ORDER BY cm.is_admin DESC, u.name""", (cid,)).fetchall()
    c.close(); return [dict(r) for r in rows]
 
@app.delete("/classroom/{cid}/remove/{uid}")
async def remove_member(cid: str, uid: str, user=Depends(get_user)):
    need_admin(cid, user["id"])
    if uid == user["id"]: raise HTTPException(400, "Cannot remove yourself.")
    c = get_db()
    c.execute("DELETE FROM classroom_members WHERE classroom_id=? AND user_id=?", (cid, uid))
    c.commit(); c.close()
    s = load_faces(); s.pop(f"cls_{cid}_stu_{uid}", None); save_faces(s)
    return {"success": True, "message": "Member removed."}
 
# ══════════════════════════════════════════════════════════════
# JOIN + FACE ENROLLMENT
# ══════════════════════════════════════════════════════════════
@app.post("/join_classroom")
async def join_classroom(req: JoinClsReq, user=Depends(get_user)):
    import httpx
    c   = get_db()
    cls = c.execute("SELECT * FROM classrooms WHERE code=?", (req.code.strip().upper(),)).fetchone()
    if not cls: c.close(); raise HTTPException(404, "Invalid classroom code.")
    cid = cls["id"]
    if c.execute("SELECT id FROM classroom_members WHERE classroom_id=? AND user_id=?",
                 (cid, user["id"])).fetchone():
        c.close(); raise HTTPException(400, "Already a member.")
 
    mid = str(uuid.uuid4())
    c.execute(
        "INSERT INTO classroom_members (id,classroom_id,user_id,roll_number,branch,year,section,is_admin) VALUES (?,?,?,?,?,?,?,0)",
        (mid, cid, user["id"], req.roll_number, req.branch, req.year, req.section))
    c.commit()
 
    face_enrolled = 0; face_msg = ""
    if req.image and FACE_SERVICE_URL:
        # Delegate face encoding to face-service
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{FACE_SERVICE_URL}/encode_face",
                    json={"classroom_id": cid, "student_id": user["id"],
                          "roll_number": req.roll_number, "name": user["name"], "image": req.image},
                    headers={"X-Internal-Key": os.getenv("INTERNAL_KEY", "nitj-internal")})
            if resp.status_code == 200:
                face_enrolled = 1; face_msg = "✅ Face enrolled!"
                now = datetime.now().isoformat()
                c.execute("UPDATE classroom_members SET face_enrolled=1,face_locked=1,face_updated_at=? WHERE id=?", (now, mid))
                c.commit()
                c.execute("INSERT INTO face_audit_logs (id,classroom_id,student_id,action,performed_by,notes) VALUES (?,?,?,?,?,?)",
                          (str(uuid.uuid4()), cid, user["id"], "ENROLLED", user["id"], "Join enrollment"))
                c.commit()
            else:
                face_msg = f"⚠️ Face service: {resp.json().get('detail', 'error')}"
        except Exception as e:
            face_msg = f"⚠️ Face service unavailable: {str(e)[:60]}"
    elif req.image:
        face_msg = "⚠️ Face service not configured (FACE_SERVICE_URL env var missing)"
    else:
        face_msg = "No face captured. Contact admin to enroll."
 
    c.close()
    return {"success": True, "classroom_id": cid, "face_enrolled": face_enrolled,
            "message": f"Joined '{cls['name']}'! {face_msg}"}
 
# ══════════════════════════════════════════════════════════════
# RECOGNIZE — delegates to face-service
# ══════════════════════════════════════════════════════════════
@app.post("/recognize")
async def recognize(req: RecognizeReq, user=Depends(get_user)):
    import httpx
    need_admin(req.classroom_id, user["id"])
    if not FACE_SERVICE_URL:
        return {"results": [{"status": "error",
                "message": "Face service not configured. Set FACE_SERVICE_URL env var."}]}
    c = get_db()
    today = date.today().isoformat()
    now_t = datetime.now().strftime("%H:%M:%S")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{FACE_SERVICE_URL}/recognize",
                json={"classroom_id": req.classroom_id, "image": req.image},
                headers={"X-Internal-Key": os.getenv("INTERNAL_KEY", "nitj-internal")})
        if resp.status_code != 200:
            return {"results": [{"status": "error", "message": "Face service error."}]}
        data = resp.json()
        results = []
        for r in data.get("results", []):
            if r["status"] == "present":
                try:
                    c.execute(
                        "INSERT INTO attendance (id,classroom_id,student_id,date,time,status,confidence) VALUES (?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), req.classroom_id, r["student_id"], today, now_t, "present", r.get("confidence", 0)))
                    c.commit()
                    results.append({"status":"present","name":r["name"],"roll":r.get("roll",""),
                                    "confidence":r.get("confidence",0),"date":today,"time":now_t})
                except sqlite3.IntegrityError:
                    results.append({"status":"duplicate","name":r["name"],"roll":r.get("roll",""),
                                    "confidence":r.get("confidence",0),"message":f"{r['name']} already marked."})
            else:
                results.append(r)
        c.close()
        return {"results": results}
    except Exception as e:
        c.close()
        return {"results": [{"status": "error", "message": f"Face service error: {str(e)[:80]}"}]}
 
# ══════════════════════════════════════════════════════════════
# ADMIN FACE MANAGEMENT — proxies to face-service
# ══════════════════════════════════════════════════════════════
@app.post("/admin_reset_face")
async def admin_reset_face(req: FaceResetReq, user=Depends(get_user)):
    import httpx
    need_admin(req.classroom_id, user["id"])
    if not FACE_SERVICE_URL:
        raise HTTPException(503, "Face service not configured.")
    c = get_db()
    row = c.execute(
        "SELECT cm.*, u.name student_name FROM classroom_members cm JOIN users u ON cm.user_id=u.id "
        "WHERE cm.classroom_id=? AND cm.user_id=?", (req.classroom_id, req.student_id)).fetchone()
    if not row: c.close(); raise HTTPException(404, "Student not found.")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{FACE_SERVICE_URL}/encode_face",
                json={"classroom_id": req.classroom_id, "student_id": req.student_id,
                      "roll_number": row["roll_number"], "name": row["student_name"], "image": req.image},
                headers={"X-Internal-Key": os.getenv("INTERNAL_KEY", "nitj-internal")})
        if resp.status_code != 200:
            c.close(); raise HTTPException(400, resp.json().get("detail", "Face error"))
        now = datetime.now().isoformat()
        c.execute("UPDATE classroom_members SET face_enrolled=1,face_locked=1,face_updated_at=? WHERE classroom_id=? AND user_id=?",
                  (now, req.classroom_id, req.student_id))
        c.execute("INSERT INTO face_audit_logs (id,classroom_id,student_id,action,performed_by,notes) VALUES (?,?,?,?,?,?)",
                  (str(uuid.uuid4()), req.classroom_id, req.student_id, "ADMIN_RESET", user["id"], req.notes or "Admin reset"))
        c.commit(); c.close()
        push_notif(req.student_id, "Face Reset", "Your face was reset by admin.", "warn")
        return {"success": True, "message": "Face reset!"}
    except HTTPException: raise
    except Exception as e:
        c.close(); raise HTTPException(503, f"Face service error: {str(e)[:80]}")
 
@app.post("/admin_clear_face")
async def admin_clear_face(
    classroom_id: str = Form(...), student_id: str = Form(...),
    notes: str = Form(""), user=Depends(get_user)):
    import httpx
    need_admin(classroom_id, user["id"]); c = get_db()
    c.execute("UPDATE classroom_members SET face_enrolled=0,face_locked=0,face_updated_at=? WHERE classroom_id=? AND user_id=?",
              (datetime.now().isoformat(), classroom_id, student_id))
    c.execute("INSERT INTO face_audit_logs (id,classroom_id,student_id,action,performed_by,notes) VALUES (?,?,?,?,?,?)",
              (str(uuid.uuid4()), classroom_id, student_id, "ADMIN_CLEARED", user["id"], notes or "Cleared"))
    c.commit()
    # Also clear on face service
    if FACE_SERVICE_URL:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.delete(
                    f"{FACE_SERVICE_URL}/face/{classroom_id}/{student_id}",
                    headers={"X-Internal-Key": os.getenv("INTERNAL_KEY", "nitj-internal")})
        except: pass
    push_notif(student_id, "Face Cleared", "Your face data was cleared by admin.", "info")
    c.close()
    return {"success": True, "message": "Face data cleared."}
 
@app.get("/admin/face_audit/{classroom_id}")
async def face_audit(classroom_id: str, user=Depends(get_uid)):
    need_admin(classroom_id, user); c = get_db()
    rows = c.execute("""
        SELECT f.*, u1.name student_name, u2.name performed_by_name
        FROM face_audit_logs f
        LEFT JOIN users u1 ON f.student_id=u1.id
        LEFT JOIN users u2 ON f.performed_by=u2.id
        WHERE f.classroom_id=? ORDER BY f.performed_at DESC""", (classroom_id,)).fetchall()
    c.close(); return [dict(r) for r in rows]
 
# ══════════════════════════════════════════════════════════════
# ATTENDANCE
# ══════════════════════════════════════════════════════════════
@app.get("/attendance/{classroom_id}")
async def get_attendance(classroom_id: str, date_filter: Optional[str] = None, user=Depends(get_user)):
    c = get_db()
    mem = c.execute("SELECT is_admin FROM classroom_members WHERE classroom_id=? AND user_id=?",
                    (classroom_id, user["id"])).fetchone()
    if not mem: c.close(); raise HTTPException(403, "Not a member.")
    if mem["is_admin"]:
        q = "SELECT a.*,u.name student_name FROM attendance a JOIN users u ON a.student_id=u.id WHERE a.classroom_id=?"
        p = [classroom_id]
    else:
        q = "SELECT a.*,u.name student_name FROM attendance a JOIN users u ON a.student_id=u.id WHERE a.classroom_id=? AND a.student_id=?"
        p = [classroom_id, user["id"]]
    if date_filter: q += " AND a.date=?"; p.append(date_filter)
    q += " ORDER BY a.date DESC,a.time DESC"
    rows = c.execute(q, p).fetchall(); c.close()
    return [dict(r) for r in rows]
 
@app.get("/my_attendance")
async def my_attendance(user=Depends(get_user)):
    c = get_db()
    rows = c.execute(
        "SELECT a.*,cl.name classroom_name,cl.subject FROM attendance a "
        "JOIN classrooms cl ON a.classroom_id=cl.id WHERE a.student_id=? ORDER BY a.date DESC",
        (user["id"],)).fetchall()
    c.close(); return [dict(r) for r in rows]
 
@app.post("/mark_absent/{classroom_id}")
async def mark_absent(classroom_id: str, user=Depends(get_user)):
    need_admin(classroom_id, user["id"]); c = get_db()
    today = date.today().isoformat(); now_t = datetime.now().strftime("%H:%M:%S")
    mems = c.execute("SELECT user_id FROM classroom_members WHERE classroom_id=? AND is_admin=0", (classroom_id,)).fetchall()
    cnt = 0
    for m in mems:
        sid = m["user_id"]
        if not c.execute("SELECT id FROM attendance WHERE classroom_id=? AND student_id=? AND date=?",
                         (classroom_id, sid, today)).fetchone():
            c.execute("INSERT INTO attendance (id,classroom_id,student_id,date,time,status,confidence) VALUES (?,?,?,?,?,?,?)",
                      (str(uuid.uuid4()), classroom_id, sid, today, now_t, "absent", 0))
            cnt += 1
    c.commit(); c.close()
    return {"success": True, "message": f"{cnt} students marked absent.", "date": today}
 
# ══════════════════════════════════════════════════════════════
# ASSIGNMENTS
# ══════════════════════════════════════════════════════════════
@app.get("/my_assignments")
async def my_assignments(user=Depends(get_user)):
    c = get_db(); today = date.today().isoformat()
    cids = [r["classroom_id"] for r in c.execute(
        "SELECT classroom_id FROM classroom_members WHERE user_id=?", (user["id"],)).fetchall()]
    result = []
    for cid in cids:
        cr = c.execute("SELECT name,subject FROM classrooms WHERE id=?", (cid,)).fetchone()
        if not cr: continue
        mem = c.execute("SELECT is_admin FROM classroom_members WHERE classroom_id=? AND user_id=?", (cid, user["id"])).fetchone()
        ia = bool(mem and mem["is_admin"])
        for r in c.execute("SELECT * FROM assignments WHERE classroom_id=? ORDER BY due_date", (cid,)).fetchall():
            a = dict(r); a["classroom_name"]=cr["name"]; a["subject"]=cr["subject"]
            a["is_overdue"]=a["due_date"]<today; a["is_admin"]=ia
            if not ia:
                sub = c.execute("SELECT * FROM assignment_submissions WHERE assignment_id=? AND student_id=?", (a["id"],user["id"])).fetchone()
                a["my_submission"] = dict(sub) if sub else None
            else:
                a["submission_count"] = c.execute("SELECT COUNT(*) FROM assignment_submissions WHERE assignment_id=?", (a["id"],)).fetchone()[0]
            result.append(a)
    c.close(); result.sort(key=lambda x:x["due_date"]); return result
 
@app.post("/create_assignment")
async def create_assignment(
    classroom_id: str=Form(...), title: str=Form(...),
    description: str=Form(""), due_date: str=Form(...),
    file: UploadFile=File(None), user=Depends(get_user)):
    need_admin(classroom_id, user["id"]); fu=fn=""
    if file and file.filename:
        ext=os.path.splitext(file.filename)[1]; f2=f"{uuid.uuid4().hex}{ext}"
        with open(os.path.join(UPLOAD_DIR,f2),"wb") as fw: fw.write(await file.read())
        fu=f"/uploads/{f2}"; fn=file.filename
    aid=str(uuid.uuid4()); c=get_db()
    c.execute("INSERT INTO assignments (id,classroom_id,creator_id,title,description,file_url,file_name,due_date) VALUES (?,?,?,?,?,?,?,?)",
              (aid,classroom_id,user["id"],title,description,fu,fn,due_date)); c.commit()
    mems=c.execute("SELECT user_id FROM classroom_members WHERE classroom_id=? AND is_admin=0",(classroom_id,)).fetchall()
    cls=c.execute("SELECT name FROM classrooms WHERE id=?",(classroom_id,)).fetchone()
    for m in mems: push_notif(m["user_id"],"New Assignment",f"'{title}' in {cls['name']}. Due:{due_date}","info")
    c.close(); return {"success":True,"assignment_id":aid,"message":"Assignment created!"}
 
@app.get("/assignments/{classroom_id}")
async def get_assignments(classroom_id: str, user=Depends(get_user)):
    c=get_db(); today=date.today().isoformat()
    mem=c.execute("SELECT is_admin FROM classroom_members WHERE classroom_id=? AND user_id=?",
                  (classroom_id,user["id"])).fetchone()
    if not mem: c.close(); raise HTTPException(403,"Not a member.")
    ia=bool(mem["is_admin"])
    rows=c.execute("SELECT * FROM assignments WHERE classroom_id=? ORDER BY due_date",(classroom_id,)).fetchall()
    result=[]
    for r in rows:
        a=dict(r); a["is_overdue"]=a["due_date"]<today; a["is_admin"]=ia
        if ia: a["submission_count"]=c.execute("SELECT COUNT(*) FROM assignment_submissions WHERE assignment_id=?",(a["id"],)).fetchone()[0]
        else:
            sub=c.execute("SELECT * FROM assignment_submissions WHERE assignment_id=? AND student_id=?",(a["id"],user["id"])).fetchone()
            a["my_submission"]=dict(sub) if sub else None
        result.append(a)
    c.close(); return result
 
@app.put("/assignment/{aid}/due_date")
async def update_due(aid: str, req: UpdateDueReq, user=Depends(get_user)):
    c=get_db(); a=c.execute("SELECT classroom_id FROM assignments WHERE id=?",(aid,)).fetchone()
    if not a: c.close(); raise HTTPException(404,"Not found.")
    need_admin(a["classroom_id"],user["id"])
    c.execute("UPDATE assignments SET due_date=? WHERE id=?",(req.due_date,aid))
    c.commit(); c.close(); return {"success":True}
 
@app.delete("/assignment/{aid}")
async def del_assignment(aid: str, user=Depends(get_user)):
    c=get_db(); a=c.execute("SELECT classroom_id FROM assignments WHERE id=?",(aid,)).fetchone()
    if not a: c.close(); raise HTTPException(404,"Not found.")
    need_admin(a["classroom_id"],user["id"])
    c.execute("DELETE FROM assignment_submissions WHERE assignment_id=?",(aid,))
    c.execute("DELETE FROM assignments WHERE id=?",(aid,))
    c.commit(); c.close(); return {"success":True}
 
@app.post("/submit_assignment")
async def submit_assignment(
    assignment_id: str=Form(...), file: UploadFile=File(None), user=Depends(get_user)):
    c=get_db(); asgn=c.execute("SELECT * FROM assignments WHERE id=?",(assignment_id,)).fetchone()
    if not asgn: c.close(); raise HTTPException(404,"Not found.")
    today=date.today().isoformat(); sv="submitted" if asgn["due_date"]>=today else "late"
    fu=fn=""
    if file and file.filename:
        ext=os.path.splitext(file.filename)[1]; f2=f"{uuid.uuid4().hex}{ext}"
        with open(os.path.join(UPLOAD_DIR,f2),"wb") as fw: fw.write(await file.read())
        fu=f"/uploads/{f2}"; fn=file.filename
    now=datetime.now().isoformat()
    try: c.execute("INSERT INTO assignment_submissions (id,assignment_id,student_id,file_url,file_name,submitted_at,status) VALUES (?,?,?,?,?,?,?)",
                   (str(uuid.uuid4()),assignment_id,user["id"],fu,fn,now,sv))
    except sqlite3.IntegrityError:
        c.execute("UPDATE assignment_submissions SET file_url=?,file_name=?,submitted_at=?,status=? WHERE assignment_id=? AND student_id=?",
                  (fu,fn,now,sv,assignment_id,user["id"]))
    c.commit(); c.close()
    return {"success":True,"status":sv,"message":"Submitted on time!" if sv=="submitted" else "Submitted late."}
 
@app.get("/submissions/{assignment_id}")
async def get_submissions(assignment_id: str, user=Depends(get_user)):
    c=get_db(); a=c.execute("SELECT classroom_id FROM assignments WHERE id=?",(assignment_id,)).fetchone()
    if not a: c.close(); raise HTTPException(404,"Not found.")
    need_admin(a["classroom_id"],user["id"])
    rows=c.execute("SELECT s.*,u.name student_name FROM assignment_submissions s JOIN users u ON s.student_id=u.id WHERE s.assignment_id=? ORDER BY s.submitted_at",(assignment_id,)).fetchall()
    c.close(); return [dict(r) for r in rows]
 
# ══════════════════════════════════════════════════════════════
# POSTS & COMMENTS
# ══════════════════════════════════════════════════════════════
@app.post("/post")
async def create_post(req: PostReq, user=Depends(get_user)):
    need_admin(req.classroom_id,user["id"]); pid=str(uuid.uuid4()); c=get_db()
    c.execute("INSERT INTO posts (id,classroom_id,user_id,type,title,content) VALUES (?,?,?,?,?,?)",
              (pid,req.classroom_id,user["id"],req.type,req.title,req.content))
    c.commit(); c.close(); return {"success":True,"post_id":pid}
 
@app.post("/upload_material")
async def upload_material(
    classroom_id: str=Form(...), title: str=Form(...),
    content: str=Form(""), file: UploadFile=File(None), user=Depends(get_user)):
    need_admin(classroom_id,user["id"]); fu=fn=""
    if file and file.filename:
        ext=os.path.splitext(file.filename)[1]; f2=f"{uuid.uuid4().hex}{ext}"
        with open(os.path.join(UPLOAD_DIR,f2),"wb") as fw: fw.write(await file.read())
        fu=f"/uploads/{f2}"; fn=file.filename
    pid=str(uuid.uuid4()); c=get_db()
    c.execute("INSERT INTO posts (id,classroom_id,user_id,type,title,content,file_url,file_name) VALUES (?,?,?,?,?,?,?,?)",
              (pid,classroom_id,user["id"],"material",title,content,fu,fn))
    c.commit(); c.close(); return {"success":True,"post_id":pid,"file_url":fu}
 
@app.get("/posts/{classroom_id}")
async def get_posts(classroom_id: str, user=Depends(get_user)):
    c=get_db()
    rows=c.execute("""SELECT p.*,u.name user_name,(SELECT COUNT(*) FROM comments WHERE post_id=p.id) comment_count
        FROM posts p JOIN users u ON p.user_id=u.id WHERE p.classroom_id=? ORDER BY p.created_at DESC""",(classroom_id,)).fetchall()
    c.close(); return [dict(r) for r in rows]
 
@app.delete("/post/{pid}")
async def del_post(pid: str, user=Depends(get_user)):
    c=get_db(); post=c.execute("SELECT classroom_id FROM posts WHERE id=?",(pid,)).fetchone()
    if not post: c.close(); raise HTTPException(404,"Post not found.")
    need_admin(post["classroom_id"],user["id"])
    c.execute("DELETE FROM comments WHERE post_id=?",(pid,))
    c.execute("DELETE FROM posts WHERE id=?",(pid,))
    c.commit(); c.close(); return {"success":True}
 
@app.post("/comment")
async def add_comment(req: CommentReq, user=Depends(get_user)):
    c=get_db()
    c.execute("INSERT INTO comments (id,post_id,user_id,comment) VALUES (?,?,?,?)",
              (str(uuid.uuid4()),req.post_id,user["id"],req.comment))
    c.commit(); c.close(); return {"success":True}
 
@app.get("/comments/{post_id}")
async def get_comments(post_id: str, user=Depends(get_user)):
    c=get_db()
    rows=c.execute("SELECT cm.*,u.name user_name FROM comments cm JOIN users u ON cm.user_id=u.id WHERE cm.post_id=? ORDER BY cm.created_at",(post_id,)).fetchall()
    c.close(); return [dict(r) for r in rows]
 
# ══════════════════════════════════════════════════════════════
# NOTIFICATIONS
# ══════════════════════════════════════════════════════════════
@app.get("/notifications")
async def get_notifications(user=Depends(get_user)):
    c=get_db()
    rows=c.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 50",(user["id"],)).fetchall()
    c.close(); return [dict(r) for r in rows]
 
@app.post("/notifications/read_all")
async def read_all(user=Depends(get_user)):
    c=get_db(); c.execute("UPDATE notifications SET read=1 WHERE user_id=?",(user["id"],)); c.commit(); c.close()
    return {"success":True}
 
# ══════════════════════════════════════════════════════════════
# HEALTH
# ══════════════════════════════════════════════════════════════
@app.get("/health")
async def health():
    return {
        "status": "ok", "version": "10.2.0-lite",
        "mode": "lite (no face_recognition)",
        "face_service": FACE_SERVICE_URL or "not configured",
        "smtp": bool(SMTP_USER and SMTP_PASS),
        "timestamp": datetime.now().isoformat()
    }
