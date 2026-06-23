/**
 * NITJ Classroom Platform v10.2 — Node.js / Express
 * ===================================================
 * JavaScript port of the FastAPI backend (main.py).
 *
 * Install:
 *   npm install express cors better-sqlite3 bcryptjs jsonwebtoken \
 *               multer nodemailer dotenv uuid
 *
 * Run:
 *   node index.js
 *   # or with auto-reload:
 *   npx nodemon index.js
 *
 * NOTE: Face recognition endpoints (/recognize, /join_classroom with face,
 * /admin_reset_face) are stubbed — face_recognition has no direct JS port.
 * Use a Python microservice or a cloud Vision API (AWS Rekognition, Azure Face)
 * and call it via HTTP from the stub handlers marked with TODO.
 */

"use strict";

require("dotenv").config();
const express = require("express");
const cors = require("cors");
const bcrypt = require("bcryptjs");
const jwt = require("jsonwebtoken");
const multer = require("multer");
const nodemailer = require("nodemailer");
const path = require("path");
const fs = require("fs");
const crypto = require("crypto");
const { v4: uuidv4 } = require("uuid");
const Database = require("better-sqlite3");

// ══════════════════════════════════════════════════════════════
// CONFIG
// ══════════════════════════════════════════════════════════════
const DB_PATH = process.env.DB_PATH || "nitj.db";
const UPLOAD_DIR = process.env.UPLOAD_DIR || "uploads";
const SECRET_KEY = process.env.SECRET_KEY || "nitj-change-this-secret-2025";
const TOKEN_EXP = "7d";
const PORT = parseInt(process.env.PORT || "8000");

const SMTP_HOST = process.env.SMTP_HOST || "smtp.gmail.com";
const SMTP_PORT = parseInt(process.env.SMTP_PORT || "587");
const SMTP_USER = process.env.SMTP_USER || "";
const SMTP_PASS = process.env.SMTP_PASS || "";
const FROM_NAME = "NITJ Classroom";
const FROM_ADDR = SMTP_USER || "noreply@nitj.ac.in";

const OTP_EXPIRY_MIN = 10;
const OTP_MAX_ATTEMPTS = 5;
const RESEND_COOLDOWN = 60; // seconds
const TOLERANCE = 0.48;

const BLOCKED_DOMAINS = new Set([
  "tempmail.com",
  "temp-mail.org",
  "guerrillamail.com",
  "mailinator.com",
  "throwaway.email",
  "yopmail.com",
  "sharklasers.com",
  "maildrop.cc",
  "10minutemail.com",
  "10minutemail.net",
  "20minutemail.com",
  "dispostable.com",
  "fakeinbox.com",
  "trashmail.com",
  "trashmail.io",
  "spam4.me",
  "spamgourmet.com",
  "tempr.email",
  "discard.email",
]);

fs.mkdirSync(UPLOAD_DIR, { recursive: true });

// ══════════════════════════════════════════════════════════════
// DATABASE
// ══════════════════════════════════════════════════════════════
const db = new Database(DB_PATH);
db.pragma("journal_mode = WAL");
db.pragma("foreign_keys = ON");

function initDb() {
  db.exec(`
    CREATE TABLE IF NOT EXISTS users (
      id              TEXT PRIMARY KEY,
      name            TEXT NOT NULL,
      email           TEXT NOT NULL UNIQUE,
      hashed_password TEXT NOT NULL,
      department      TEXT DEFAULT '',
      email_verified  INTEGER DEFAULT 1,
      created_at      TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS password_resets (
      id         TEXT PRIMARY KEY,
      user_id    TEXT NOT NULL,
      otp_hash   TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      used       INTEGER DEFAULT 0,
      attempts   INTEGER DEFAULT 0,
      created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS classrooms (
      id           TEXT PRIMARY KEY,
      creator_id   TEXT NOT NULL,
      name         TEXT NOT NULL,
      subject      TEXT NOT NULL,
      branch       TEXT NOT NULL,
      year         INTEGER NOT NULL,
      section      TEXT NOT NULL,
      code         TEXT NOT NULL UNIQUE,
      description  TEXT DEFAULT '',
      banner_color TEXT DEFAULT '#1565C0',
      created_at   TEXT DEFAULT (datetime('now')),
      FOREIGN KEY (creator_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS classroom_members (
      id              TEXT PRIMARY KEY,
      classroom_id    TEXT NOT NULL,
      user_id         TEXT NOT NULL,
      roll_number     TEXT DEFAULT '',
      branch          TEXT DEFAULT '',
      year            INTEGER DEFAULT 1,
      section         TEXT DEFAULT '',
      face_enrolled   INTEGER DEFAULT 0,
      face_locked     INTEGER DEFAULT 0,
      face_updated_at TEXT DEFAULT '',
      is_admin        INTEGER DEFAULT 0,
      joined_at       TEXT DEFAULT (datetime('now')),
      FOREIGN KEY (classroom_id) REFERENCES classrooms(id),
      FOREIGN KEY (user_id) REFERENCES users(id),
      UNIQUE(classroom_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS face_audit_logs (
      id           TEXT PRIMARY KEY,
      classroom_id TEXT NOT NULL,
      student_id   TEXT NOT NULL,
      action       TEXT NOT NULL,
      performed_by TEXT NOT NULL,
      performed_at TEXT DEFAULT (datetime('now')),
      notes        TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS attendance (
      id           TEXT PRIMARY KEY,
      classroom_id TEXT NOT NULL,
      student_id   TEXT NOT NULL,
      date         TEXT NOT NULL,
      time         TEXT NOT NULL,
      status       TEXT DEFAULT 'present',
      confidence   REAL DEFAULT 0,
      UNIQUE(classroom_id, student_id, date)
    );
    CREATE TABLE IF NOT EXISTS posts (
      id           TEXT PRIMARY KEY,
      classroom_id TEXT NOT NULL,
      user_id      TEXT NOT NULL,
      type         TEXT DEFAULT 'announcement',
      title        TEXT NOT NULL,
      content      TEXT DEFAULT '',
      file_url     TEXT DEFAULT '',
      file_name    TEXT DEFAULT '',
      created_at   TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS comments (
      id         TEXT PRIMARY KEY,
      post_id    TEXT NOT NULL,
      user_id    TEXT NOT NULL,
      comment    TEXT NOT NULL,
      created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS assignments (
      id           TEXT PRIMARY KEY,
      classroom_id TEXT NOT NULL,
      creator_id   TEXT NOT NULL,
      title        TEXT NOT NULL,
      description  TEXT DEFAULT '',
      file_url     TEXT DEFAULT '',
      file_name    TEXT DEFAULT '',
      due_date     TEXT NOT NULL,
      created_at   TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS assignment_submissions (
      id            TEXT PRIMARY KEY,
      assignment_id TEXT NOT NULL,
      student_id    TEXT NOT NULL,
      file_url      TEXT DEFAULT '',
      file_name     TEXT DEFAULT '',
      submitted_at  TEXT DEFAULT (datetime('now')),
      status        TEXT DEFAULT 'submitted',
      UNIQUE(assignment_id, student_id)
    );
    CREATE TABLE IF NOT EXISTS notifications (
      id         TEXT PRIMARY KEY,
      user_id    TEXT NOT NULL,
      title      TEXT NOT NULL,
      message    TEXT NOT NULL,
      type       TEXT DEFAULT 'info',
      read       INTEGER DEFAULT 0,
      created_at TEXT DEFAULT (datetime('now'))
    );
  `);
  console.log("✅ DB ready:", DB_PATH);
}

initDb();

const FACE_SERVICE_URL = process.env.FACE_SERVICE_URL || "";

async function callFaceService(endpoint, body) {
  if (!FACE_SERVICE_URL) throw new Error("Face service not configured.");
  const r = await fetch(`${FACE_SERVICE_URL}${endpoint}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}

// ══════════════════════════════════════════════════════════════
// HELPERS
// ══════════════════════════════════════════════════════════════

/** SHA-256 hash of an OTP string */
function hashOtp(otp) {
  return crypto.createHash("sha256").update(otp, "utf8").digest("hex");
}

/** Generate a 6-digit OTP */
function genOtp() {
  return String(Math.floor(100000 + Math.random() * 900000));
}

/** Validate email format and domain */
function validateEmail(email) {
  email = email.toLowerCase().trim();
  if (!/^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$/.test(email)) {
    return { ok: false, err: "Invalid email format." };
  }
  const domain = email.split("@")[1].toLowerCase();
  if (BLOCKED_DOMAINS.has(domain)) {
    return {
      ok: false,
      err: "Disposable/temporary email domains are not allowed.",
    };
  }
  return { ok: true, err: "" };
}

/** Build HTML email for OTP */
function buildEmailHtml(name, otp) {
  const subject = "NITJ Classroom — Password Reset Code";
  const html = `<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"/><title>${subject}</title></head>
<body style="margin:0;padding:0;background:#EDF2FB;font-family:'Segoe UI',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#EDF2FB;padding:32px 16px;">
  <tr><td align="center">
  <table width="100%" cellpadding="0" cellspacing="0" style="max-width:540px;">
  <tr><td style="background:linear-gradient(135deg,#0a1f38 0%,#1565C0 100%);border-radius:16px 16px 0 0;padding:32px;text-align:center;">
    <div style="font-size:22px;font-weight:800;color:#fff;">NITJ Classroom</div>
  </td></tr>
  <tr><td style="background:#ffffff;padding:36px 32px;">
    <p style="font-size:26px;margin:0 0 8px;">🔐</p>
    <h2 style="font-size:22px;font-weight:800;color:#0a1f38;margin:0 0 12px;">Reset Your Password</h2>
    <p style="font-size:14.5px;color:#4a6080;line-height:1.7;margin:0 0 28px;">
      Hi <strong style="color:#0a1f38;">${name}</strong>,<br/>
      Use the code below to reset your NITJ Classroom password. Expires in <strong>10 minutes</strong>.
    </p>
    <div style="background:#EEF4FB;border:2.5px dashed #BBDEFB;border-radius:14px;padding:28px 20px;text-align:center;margin-bottom:28px;">
      <div style="font-size:48px;font-weight:900;color:#1565C0;letter-spacing:14px;font-family:'Courier New',Courier,monospace;">${otp}</div>
    </div>
    <p style="font-size:13px;color:#7a90ab;">If you didn't request this, ignore this email.</p>
  </td></tr>
  </table>
  </td></tr>
</table>
</body>
</html>`;
  return { subject, html };
}

/** Send email via SMTP */
async function sendEmail(to, name, subject, html) {
  if (!SMTP_USER || !SMTP_PASS) {
    console.warn("⚠️  SMTP not configured.");
    return false;
  }
  try {
    const transporter = nodemailer.createTransport({
      host: SMTP_HOST,
      port: SMTP_PORT,
      secure: false,
      auth: { user: SMTP_USER, pass: SMTP_PASS },
    });
    await transporter.sendMail({
      from: `"${FROM_NAME}" <${FROM_ADDR}>`,
      to: `"${name}" <${to}>`,
      subject,
      html,
    });
    console.log("✅ Email sent →", to);
    return true;
  } catch (e) {
    console.error("❌ Email failed →", to, ":", e.message);
    return false;
  }
}

/** Create and store a password-reset OTP */
function issueResetOtp(userId) {
  db.prepare(
    "UPDATE password_resets SET used=1 WHERE user_id=? AND used=0",
  ).run(userId);
  const otp = genOtp();
  const otpHash = hashOtp(otp);
  const exp = new Date(Date.now() + OTP_EXPIRY_MIN * 60 * 1000).toISOString();
  db.prepare(
    "INSERT INTO password_resets (id,user_id,otp_hash,expires_at) VALUES (?,?,?,?)",
  ).run(uuidv4(), userId, otpHash, exp);
  return otp;
}

/** Push an in-app notification */
function pushNotif(userId, title, message, type = "info") {
  db.prepare(
    "INSERT INTO notifications (id,user_id,title,message,type) VALUES (?,?,?,?,?)",
  ).run(uuidv4(), userId, title, message, type);
}

/** Return today's date as YYYY-MM-DD */
function today() {
  return new Date().toISOString().slice(0, 10);
}

/** Return current time as HH:MM:SS */
function nowTime() {
  return new Date().toTimeString().slice(0, 8);
}

// ══════════════════════════════════════════════════════════════
// JWT HELPERS
// ══════════════════════════════════════════════════════════════

function makeToken(uid) {
  return jwt.sign({ sub: uid }, SECRET_KEY, { expiresIn: TOKEN_EXP });
}

/** Express middleware — extracts user from Bearer token */
function authMiddleware(req, res, next) {
  const auth = req.headers.authorization;
  if (!auth || !auth.startsWith("Bearer ")) {
    return res.status(401).json({ detail: "Not authenticated" });
  }
  try {
    const payload = jwt.verify(auth.slice(7), SECRET_KEY);
    const uid = payload.sub;
    if (!uid) return res.status(401).json({ detail: "Invalid token" });
    const user = db.prepare("SELECT * FROM users WHERE id=?").get(uid);
    if (!user) return res.status(401).json({ detail: "User not found" });
    delete user.hashed_password;
    req.user = user;
    next();
  } catch {
    return res
      .status(401)
      .json({ detail: "Session expired. Please login again." });
  }
}

/** Throw 403 unless the user is an admin of the classroom */
function requireAdmin(classroomId, userId) {
  const r = db
    .prepare(
      "SELECT is_admin FROM classroom_members WHERE classroom_id=? AND user_id=?",
    )
    .get(classroomId, userId);
  if (!r || !r.is_admin) {
    const err = new Error("Admin access required.");
    err.status = 403;
    throw err;
  }
}

/** Central error handler */
function errHandler(err, req, res, _next) {
  const status = err.status || 500;
  res.status(status).json({ detail: err.message || "Internal server error" });
}

// ══════════════════════════════════════════════════════════════
// MULTER (file uploads)
// ══════════════════════════════════════════════════════════════
const storage = multer.diskStorage({
  destination: (_req, _file, cb) => cb(null, UPLOAD_DIR),
  filename: (_req, file, cb) => {
    const ext = path.extname(file.originalname);
    cb(null, uuidv4().replace(/-/g, "") + ext);
  },
});
const upload = multer({ storage });

// ══════════════════════════════════════════════════════════════
// EXPRESS APP
// ══════════════════════════════════════════════════════════════
const app = express();
app.use(cors({ origin: "*", credentials: true }));
app.use(express.json({ limit: "20mb" }));
app.use("/uploads", express.static(UPLOAD_DIR));

// ── AUTH — REGISTER ──────────────────────────────────────────
app.post("/register", (req, res, next) => {
  try {
    let { name, email, password, department = "" } = req.body;
    email = (email || "").toLowerCase().trim();

    const { ok, err } = validateEmail(email);
    if (!ok) return res.status(400).json({ detail: err });
    if (!name?.trim())
      return res.status(400).json({ detail: "Name is required." });
    if ((password || "").length < 6)
      return res
        .status(400)
        .json({ detail: "Password must be at least 6 characters." });

    const existing = db
      .prepare("SELECT id FROM users WHERE email=?")
      .get(email);
    if (existing)
      return res
        .status(400)
        .json({ detail: "Email already registered. Please login." });

    const uid = uuidv4();
    const hash = bcrypt.hashSync(password, 10);
    db.prepare(
      "INSERT INTO users (id,name,email,hashed_password,department,email_verified) VALUES (?,?,?,?,?,1)",
    ).run(uid, name.trim(), email, hash, department);

    console.log("✅ New user registered:", name, "<" + email + ">");
    res.json({
      success: true,
      token: makeToken(uid),
      message: `Account created! Welcome to NITJ Classroom, ${name.trim()} 🎉`,
    });
  } catch (e) {
    next(e);
  }
});

// Alias: old clients may call /send_otp for registration
app.post("/send_otp", (req, res, next) => {
  req.url = "/register";
  app.handle(req, res, next);
});

// ── AUTH — LOGIN ─────────────────────────────────────────────
app.post("/login", (req, res, next) => {
  try {
    const { email, password } = req.body;
    const row = db
      .prepare("SELECT * FROM users WHERE email=?")
      .get((email || "").toLowerCase().trim());

    if (!row)
      return res.status(401).json({ detail: "No account with this email." });
    if (!bcrypt.compareSync(password || "", row.hashed_password))
      return res.status(401).json({ detail: "Incorrect password." });

    res.json({
      success: true,
      token: makeToken(row.id),
      message: `Welcome back, ${row.name}!`,
    });
  } catch (e) {
    next(e);
  }
});

// ── AUTH — ME ────────────────────────────────────────────────
app.get("/me", authMiddleware, (req, res) => res.json(req.user));

// ── AUTH — FORGOT PASSWORD ───────────────────────────────────
app.post("/forgot_password", async (req, res, next) => {
  try {
    const email = (req.body.email || "").toLowerCase().trim();
    const user = db.prepare("SELECT * FROM users WHERE email=?").get(email);
    if (!user)
      return res.status(404).json({ detail: "No account with this email." });

    const last = db
      .prepare(
        "SELECT created_at FROM password_resets WHERE user_id=? AND used=0 ORDER BY created_at DESC LIMIT 1",
      )
      .get(user.id);
    if (last) {
      const elapsed =
        (Date.now() - new Date(last.created_at + "Z").getTime()) / 1000;
      if (elapsed < RESEND_COOLDOWN)
        return res.status(429).json({
          detail: `Please wait ${Math.ceil(RESEND_COOLDOWN - elapsed)}s before requesting again.`,
        });
    }

    const otp = issueResetOtp(user.id);
    const { subject, html } = buildEmailHtml(user.name, otp);
    const sent = await sendEmail(email, user.name, subject, html);

    const resp = {
      success: true,
      message: `Password reset code sent to ${email}.`,
    };
    if (!sent && !SMTP_USER) {
      resp.smtp_status = "not_configured";
      console.log(`🔑 DEV-ONLY reset OTP for <${email}>: ${otp}`);
    }
    res.json(resp);
  } catch (e) {
    next(e);
  }
});

// ── AUTH — RESET PASSWORD ────────────────────────────────────
app.post("/reset_password", (req, res, next) => {
  try {
    const { email, otp, new_password } = req.body;
    if ((new_password || "").length < 6)
      return res
        .status(400)
        .json({ detail: "Password must be at least 6 characters." });

    const row = db
      .prepare(
        `
      SELECT pr.*, u.id AS uid FROM password_resets pr
      JOIN users u ON pr.user_id = u.id
      WHERE u.email=? AND pr.used=0
      ORDER BY pr.created_at DESC LIMIT 1
    `,
      )
      .get((email || "").toLowerCase().trim());

    if (!row)
      return res
        .status(400)
        .json({ detail: "No pending reset. Please request a new OTP." });
    if (row.attempts >= OTP_MAX_ATTEMPTS)
      return res
        .status(429)
        .json({ detail: "Too many attempts. Please request a new OTP." });
    if (new Date(row.expires_at + "Z") < new Date())
      return res
        .status(400)
        .json({ detail: "OTP has expired. Please request a new one." });
    if (hashOtp((otp || "").trim()) !== row.otp_hash) {
      db.prepare(
        "UPDATE password_resets SET attempts=attempts+1 WHERE id=?",
      ).run(row.id);
      return res.status(400).json({ detail: "Invalid OTP." });
    }

    db.prepare("UPDATE users SET hashed_password=? WHERE id=?").run(
      bcrypt.hashSync(new_password, 10),
      row.uid,
    );
    db.prepare("UPDATE password_resets SET used=1 WHERE id=?").run(row.id);

    res.json({
      success: true,
      message: "Password reset successfully! Please login.",
    });
  } catch (e) {
    next(e);
  }
});

// Alias for legacy clients
app.post("/verify_otp", (req, res, next) => {
  req.url = "/reset_password";
  app.handle(req, res, next);
});

// ── CLASSROOMS — CREATE ──────────────────────────────────────
app.post("/create_classroom", authMiddleware, (req, res, next) => {
  try {
    const {
      name,
      subject,
      branch,
      year,
      section,
      description = "",
      banner_color = "#1565C0",
    } = req.body;
    let code;
    do {
      code = crypto.randomBytes(3).toString("hex").toUpperCase();
    } while (db.prepare("SELECT id FROM classrooms WHERE code=?").get(code));

    const cid = uuidv4();
    db.prepare(
      "INSERT INTO classrooms (id,creator_id,name,subject,branch,year,section,code,description,banner_color) VALUES (?,?,?,?,?,?,?,?,?,?)",
    ).run(
      cid,
      req.user.id,
      name,
      subject,
      branch,
      year,
      section,
      code,
      description,
      banner_color,
    );
    db.prepare(
      "INSERT INTO classroom_members (id,classroom_id,user_id,roll_number,is_admin,face_enrolled,face_locked) VALUES (?,?,?,?,?,?,?)",
    ).run(uuidv4(), cid, req.user.id, "ADMIN", 1, 0, 0);

    console.log(
      "✅ Classroom created:",
      name,
      "[" + code + "] by",
      req.user.name,
    );
    res.json({
      success: true,
      classroom_id: cid,
      code,
      message: `Classroom '${name}' created! Code: ${code}`,
    });
  } catch (e) {
    next(e);
  }
});

// ── CLASSROOMS — LIST ────────────────────────────────────────
app.get("/classrooms", authMiddleware, (req, res, next) => {
  try {
    const rows = db
      .prepare(
        `
      SELECT cl.*, u.name creator_name, cm.is_admin,
             (SELECT COUNT(*) FROM classroom_members WHERE classroom_id=cl.id) AS member_count,
             (SELECT COUNT(*) FROM assignments WHERE classroom_id=cl.id AND due_date >= date('now')) AS upcoming_assignments
      FROM classrooms cl
      JOIN users u ON cl.creator_id = u.id
      JOIN classroom_members cm ON cm.classroom_id = cl.id AND cm.user_id = ?
      ORDER BY cm.joined_at DESC
    `,
      )
      .all(req.user.id);
    res.json(rows);
  } catch (e) {
    next(e);
  }
});

// ── CLASSROOMS — GET ONE ─────────────────────────────────────
app.get("/classroom/:cid", authMiddleware, (req, res, next) => {
  try {
    const { cid } = req.params;
    const row = db
      .prepare(
        "SELECT cl.*, u.name creator_name FROM classrooms cl JOIN users u ON cl.creator_id=u.id WHERE cl.id=?",
      )
      .get(cid);
    if (!row) return res.status(404).json({ detail: "Classroom not found." });

    const mem = db
      .prepare(
        "SELECT is_admin,face_enrolled,face_locked,face_updated_at FROM classroom_members WHERE classroom_id=? AND user_id=?",
      )
      .get(cid, req.user.id);
    if (!mem)
      return res
        .status(403)
        .json({ detail: "You are not a member of this classroom." });

    res.json({
      ...row,
      is_admin: Boolean(mem.is_admin),
      face_enrolled: Boolean(mem.face_enrolled),
      face_locked: Boolean(mem.face_locked),
      face_updated_at: mem.face_updated_at,
      member_count: db
        .prepare(
          "SELECT COUNT(*) AS c FROM classroom_members WHERE classroom_id=?",
        )
        .get(cid).c,
      upcoming_assignments: db
        .prepare(
          "SELECT COUNT(*) AS c FROM assignments WHERE classroom_id=? AND due_date>=date('now')",
        )
        .get(cid).c,
    });
  } catch (e) {
    next(e);
  }
});

// ── CLASSROOMS — DELETE ──────────────────────────────────────
app.delete("/classroom/:cid", authMiddleware, (req, res, next) => {
  try {
    const { cid } = req.params;
    requireAdmin(cid, req.user.id);
    for (const t of [
      "classroom_members",
      "attendance",
      "posts",
      "comments",
      "assignments",
      "assignment_submissions",
      "face_audit_logs",
    ]) {
      try {
        db.prepare(`DELETE FROM ${t} WHERE classroom_id=?`).run(cid);
      } catch {}
    }
    db.prepare("DELETE FROM classrooms WHERE id=?").run(cid);
    res.json({ success: true, message: "Classroom deleted." });
  } catch (e) {
    next(e);
  }
});

// ── CLASSROOMS — MEMBERS ─────────────────────────────────────
app.get("/classroom/:cid/members", authMiddleware, (req, res, next) => {
  try {
    const rows = db
      .prepare(
        `
      SELECT u.id, u.name, u.email,
             cm.roll_number, cm.branch, cm.year, cm.section,
             cm.face_enrolled, cm.face_locked, cm.is_admin,
             cm.joined_at, cm.face_updated_at
      FROM classroom_members cm
      JOIN users u ON cm.user_id = u.id
      WHERE cm.classroom_id = ?
      ORDER BY cm.is_admin DESC, u.name
    `,
      )
      .all(req.params.cid);
    res.json(rows);
  } catch (e) {
    next(e);
  }
});

// ── CLASSROOMS — REMOVE MEMBER ───────────────────────────────
app.delete("/classroom/:cid/remove/:uid", authMiddleware, (req, res, next) => {
  try {
    const { cid, uid } = req.params;
    requireAdmin(cid, req.user.id);
    if (uid === req.user.id)
      return res.status(400).json({ detail: "You cannot remove yourself." });
    db.prepare(
      "DELETE FROM classroom_members WHERE classroom_id=? AND user_id=?",
    ).run(cid, uid);
    res.json({ success: true, message: "Member removed." });
  } catch (e) {
    next(e);
  }
});
// ── JOIN CLASSROOM ───────────────────────────────────────────
app.post("/join_classroom", authMiddleware, async (req, res, next) => {
  try {
    const {
      code,
      roll_number = "",
      branch = "",
      year = 1,
      section = "",
      image = "",
    } = req.body;
    const cls = db
      .prepare("SELECT * FROM classrooms WHERE code=?")
      .get((code || "").trim().toUpperCase());
    if (!cls)
      return res.status(404).json({ detail: "Invalid classroom code." });

    const alreadyMember = db
      .prepare(
        "SELECT id FROM classroom_members WHERE classroom_id=? AND user_id=?",
      )
      .get(cls.id, req.user.id);
    if (alreadyMember)
      return res
        .status(400)
        .json({ detail: "You are already a member of this classroom." });

    const mid = uuidv4();
    db.prepare(
      "INSERT INTO classroom_members (id,classroom_id,user_id,roll_number,branch,year,section,is_admin) VALUES (?,?,?,?,?,?,?,0)",
    ).run(mid, cls.id, req.user.id, roll_number, branch, year, section);

    let faceMsg = "No face image provided. Contact your admin to enroll.";
    let faceEnrolled = 0;

    if (image) {
      try {
        const result = await callFaceService("/enroll", {
          student_id: req.user.id,
          classroom_id: cls.id,
          image,
        });
        if (result.success) {
          db.prepare(
            "UPDATE classroom_members SET face_enrolled=1 WHERE classroom_id=? AND user_id=?",
          ).run(cls.id, req.user.id);
          faceEnrolled = 1;
          faceMsg = "Face enrolled successfully!";
        } else {
          faceMsg = result.message || "Face enrollment failed.";
        }
      } catch (e) {
        faceMsg = "Face service error: " + e.message;
      }
    }

    res.json({
      success: true,
      classroom_id: cls.id,
      face_enrolled: faceEnrolled,
      message: `Joined '${cls.name}'! ${faceMsg}`,
    });
  } catch (e) {
    next(e);
  }
});

// ── ADMIN FACE RESET ─────────────────────────────────────────
// ── ADMIN FACE RESET ─────────────────────────────────────────
app.post("/admin_reset_face", authMiddleware, async (req, res, next) => {
  try {
    const { classroom_id, student_id, image = "", notes = "" } = req.body;
    requireAdmin(classroom_id, req.user.id);
    const row = db
      .prepare(
        "SELECT cm.*, u.name AS student_name FROM classroom_members cm JOIN users u ON cm.user_id=u.id WHERE cm.classroom_id=? AND cm.user_id=?",
      )
      .get(classroom_id, student_id);
    if (!row)
      return res
        .status(404)
        .json({ detail: "Student not found in this classroom." });

    if (image) {
      try {
        await callFaceService("/enroll", {
          student_id,
          classroom_id,
          image,
        });
      } catch (e) {
        return res
          .status(500)
          .json({ detail: "Face service error: " + e.message });
      }
    }

    const now = new Date().toISOString();
    db.prepare(
      "UPDATE classroom_members SET face_enrolled=1,face_locked=1,face_updated_at=? WHERE classroom_id=? AND user_id=?",
    ).run(now, classroom_id, student_id);
    db.prepare(
      "INSERT INTO face_audit_logs (id,classroom_id,student_id,action,performed_by,notes) VALUES (?,?,?,?,?,?)",
    ).run(
      uuidv4(),
      classroom_id,
      student_id,
      "ADMIN_RESET",
      req.user.id,
      notes || "Admin reset",
    );
    pushNotif(
      student_id,
      "Face Reset",
      "Your face data was reset by the admin.",
      "warn",
    );

    res.json({ success: true, message: "Face reset successfully!" });
  } catch (e) {
    next(e);
  }
});

// ── ADMIN CLEAR FACE ─────────────────────────────────────────
app.post(
  "/admin_clear_face",
  authMiddleware,
  upload.none(),
  (req, res, next) => {
    try {
      const { classroom_id, student_id, notes = "" } = req.body;
      requireAdmin(classroom_id, req.user.id);
      const now = new Date().toISOString();
      db.prepare(
        "UPDATE classroom_members SET face_enrolled=0,face_locked=0,face_updated_at=? WHERE classroom_id=? AND user_id=?",
      ).run(now, classroom_id, student_id);
      db.prepare(
        "INSERT INTO face_audit_logs (id,classroom_id,student_id,action,performed_by,notes) VALUES (?,?,?,?,?,?)",
      ).run(
        uuidv4(),
        classroom_id,
        student_id,
        "ADMIN_CLEARED",
        req.user.id,
        notes || "Cleared by admin",
      );
      pushNotif(
        student_id,
        "Face Cleared",
        "Your face data was cleared by the admin.",
        "info",
      );
      res.json({ success: true, message: "Face data cleared." });
    } catch (e) {
      next(e);
    }
  },
);

// ── FACE AUDIT LOG ───────────────────────────────────────────
app.get("/admin/face_audit/:classroomId", authMiddleware, (req, res, next) => {
  try {
    requireAdmin(req.params.classroomId, req.user.id);
    const rows = db
      .prepare(
        `
      SELECT f.*, u1.name AS student_name, u2.name AS performed_by_name
      FROM face_audit_logs f
      LEFT JOIN users u1 ON f.student_id   = u1.id
      LEFT JOIN users u2 ON f.performed_by = u2.id
      WHERE f.classroom_id = ?
      ORDER BY f.performed_at DESC
    `,
      )
      .all(req.params.classroomId);
    res.json(rows);
  } catch (e) {
    next(e);
  }
});

// ── FACE RECOGNITION ATTENDANCE ──────────────────────────────
app.post("/recognize", authMiddleware, async (req, res, next) => {
  try {
    const { classroom_id, image } = req.body;
    requireAdmin(classroom_id, req.user.id);

    const faceResult = await callFaceService("/recognize", {
      classroom_id,
      image,
    });

    const todayStr = today();
    const nowStr = nowTime();

    for (const r of faceResult.results || []) {
      const exists = db
        .prepare(
          "SELECT id FROM attendance WHERE classroom_id=? AND student_id=? AND date=?",
        )
        .get(classroom_id, r.student_id, todayStr);

      if (!exists) {
        db.prepare(
          "INSERT INTO attendance (id,classroom_id,student_id,date,time,status,confidence) VALUES (?,?,?,?,?,?,?)",
        ).run(
          uuidv4(),
          classroom_id,
          r.student_id,
          todayStr,
          nowStr,
          "present",
          r.confidence,
        );
      }
    }

    res.json({ results: faceResult.results || [] });
  } catch (e) {
    next(e);
  }
});
// ── ATTENDANCE — GET ─────────────────────────────────────────
app.get("/attendance/:classroomId", authMiddleware, (req, res, next) => {
  try {
    const { classroomId } = req.params;
    const { date_filter } = req.query;
    const mem = db
      .prepare(
        "SELECT is_admin FROM classroom_members WHERE classroom_id=? AND user_id=?",
      )
      .get(classroomId, req.user.id);
    if (!mem)
      return res
        .status(403)
        .json({ detail: "You are not a member of this classroom." });

    let q, params;
    if (mem.is_admin) {
      q =
        "SELECT a.*, u.name AS student_name FROM attendance a JOIN users u ON a.student_id=u.id WHERE a.classroom_id=?";
      params = [classroomId];
    } else {
      q =
        "SELECT a.*, u.name AS student_name FROM attendance a JOIN users u ON a.student_id=u.id WHERE a.classroom_id=? AND a.student_id=?";
      params = [classroomId, req.user.id];
    }
    if (date_filter) {
      q += " AND a.date=?";
      params.push(date_filter);
    }
    q += " ORDER BY a.date DESC, a.time DESC";
    res.json(db.prepare(q).all(...params));
  } catch (e) {
    next(e);
  }
});

// ── ATTENDANCE — MY ──────────────────────────────────────────
app.get("/my_attendance", authMiddleware, (req, res, next) => {
  try {
    const rows = db
      .prepare(
        `
      SELECT a.*, cl.name AS classroom_name, cl.subject
      FROM attendance a
      JOIN classrooms cl ON a.classroom_id = cl.id
      WHERE a.student_id = ?
      ORDER BY a.date DESC
    `,
      )
      .all(req.user.id);
    res.json(rows);
  } catch (e) {
    next(e);
  }
});

// ── ATTENDANCE — MARK ABSENT ─────────────────────────────────
app.post("/mark_absent/:classroomId", authMiddleware, (req, res, next) => {
  try {
    const { classroomId } = req.params;
    requireAdmin(classroomId, req.user.id);
    const todayStr = today();
    const nowStr = nowTime();
    const members = db
      .prepare(
        "SELECT user_id FROM classroom_members WHERE classroom_id=? AND is_admin=0",
      )
      .all(classroomId);

    let cnt = 0;
    for (const m of members) {
      const already = db
        .prepare(
          "SELECT id FROM attendance WHERE classroom_id=? AND student_id=? AND date=?",
        )
        .get(classroomId, m.user_id, todayStr);
      if (!already) {
        db.prepare(
          "INSERT INTO attendance (id,classroom_id,student_id,date,time,status,confidence) VALUES (?,?,?,?,?,?,?)",
        ).run(uuidv4(), classroomId, m.user_id, todayStr, nowStr, "absent", 0);
        cnt++;
      }
    }
    res.json({
      success: true,
      message: `${cnt} student${cnt !== 1 ? "s" : ""} marked absent.`,
      date: todayStr,
    });
  } catch (e) {
    next(e);
  }
});

// ── ASSIGNMENTS — MY ─────────────────────────────────────────
app.get("/my_assignments", authMiddleware, (req, res, next) => {
  try {
    const todayStr = today();
    const classIds = db
      .prepare("SELECT classroom_id FROM classroom_members WHERE user_id=?")
      .all(req.user.id)
      .map((r) => r.classroom_id);

    const result = [];
    for (const cid of classIds) {
      const cr = db
        .prepare("SELECT name, subject FROM classrooms WHERE id=?")
        .get(cid);
      if (!cr) continue;
      const mem = db
        .prepare(
          "SELECT is_admin FROM classroom_members WHERE classroom_id=? AND user_id=?",
        )
        .get(cid, req.user.id);
      const ia = Boolean(mem?.is_admin);

      for (const r of db
        .prepare(
          "SELECT * FROM assignments WHERE classroom_id=? ORDER BY due_date",
        )
        .all(cid)) {
        const a = {
          ...r,
          classroom_name: cr.name,
          subject: cr.subject,
          is_overdue: r.due_date < todayStr,
          is_admin: ia,
        };
        if (!ia) {
          const sub = db
            .prepare(
              "SELECT * FROM assignment_submissions WHERE assignment_id=? AND student_id=?",
            )
            .get(r.id, req.user.id);
          a.my_submission = sub || null;
        } else {
          a.submission_count = db
            .prepare(
              "SELECT COUNT(*) AS c FROM assignment_submissions WHERE assignment_id=?",
            )
            .get(r.id).c;
        }
        result.push(a);
      }
    }
    result.sort((a, b) => a.due_date.localeCompare(b.due_date));
    res.json(result);
  } catch (e) {
    next(e);
  }
});

// ── ASSIGNMENTS — CREATE ─────────────────────────────────────
app.post(
  "/create_assignment",
  authMiddleware,
  upload.single("file"),
  (req, res, next) => {
    try {
      const { classroom_id, title, description = "", due_date } = req.body;
      requireAdmin(classroom_id, req.user.id);

      let fu = "",
        fn = "";
      if (req.file) {
        fu = `/uploads/${req.file.filename}`;
        fn = req.file.originalname;
      }

      const aid = uuidv4();
      db.prepare(
        "INSERT INTO assignments (id,classroom_id,creator_id,title,description,file_url,file_name,due_date) VALUES (?,?,?,?,?,?,?,?)",
      ).run(
        aid,
        classroom_id,
        req.user.id,
        title,
        description,
        fu,
        fn,
        due_date,
      );

      const mems = db
        .prepare(
          "SELECT user_id FROM classroom_members WHERE classroom_id=? AND is_admin=0",
        )
        .all(classroom_id);
      const cls = db
        .prepare("SELECT name FROM classrooms WHERE id=?")
        .get(classroom_id);
      for (const m of mems)
        pushNotif(
          m.user_id,
          "New Assignment",
          `'${title}' in ${cls.name}. Due: ${due_date}`,
          "info",
        );

      res.json({
        success: true,
        assignment_id: aid,
        message: "Assignment created!",
      });
    } catch (e) {
      next(e);
    }
  },
);

// ── ASSIGNMENTS — LIST ───────────────────────────────────────
app.get("/assignments/:classroomId", authMiddleware, (req, res, next) => {
  try {
    const { classroomId } = req.params;
    const todayStr = today();
    const mem = db
      .prepare(
        "SELECT is_admin FROM classroom_members WHERE classroom_id=? AND user_id=?",
      )
      .get(classroomId, req.user.id);
    if (!mem)
      return res
        .status(403)
        .json({ detail: "You are not a member of this classroom." });

    const ia = Boolean(mem.is_admin);
    const rows = db
      .prepare(
        "SELECT * FROM assignments WHERE classroom_id=? ORDER BY due_date",
      )
      .all(classroomId);
    const result = [];
    for (const r of rows) {
      const a = { ...r, is_overdue: r.due_date < todayStr, is_admin: ia };
      if (ia) {
        a.submission_count = db
          .prepare(
            "SELECT COUNT(*) AS c FROM assignment_submissions WHERE assignment_id=?",
          )
          .get(r.id).c;
      } else {
        const sub = db
          .prepare(
            "SELECT * FROM assignment_submissions WHERE assignment_id=? AND student_id=?",
          )
          .get(r.id, req.user.id);
        a.my_submission = sub || null;
      }
      result.push(a);
    }
    res.json(result);
  } catch (e) {
    next(e);
  }
});

// ── ASSIGNMENTS — UPDATE DUE DATE ────────────────────────────
app.put("/assignment/:aid/due_date", authMiddleware, (req, res, next) => {
  try {
    const a = db
      .prepare("SELECT classroom_id FROM assignments WHERE id=?")
      .get(req.params.aid);
    if (!a) return res.status(404).json({ detail: "Assignment not found." });
    requireAdmin(a.classroom_id, req.user.id);
    db.prepare("UPDATE assignments SET due_date=? WHERE id=?").run(
      req.body.due_date,
      req.params.aid,
    );
    res.json({ success: true, message: "Due date updated." });
  } catch (e) {
    next(e);
  }
});

// ── ASSIGNMENTS — DELETE ─────────────────────────────────────
app.delete("/assignment/:aid", authMiddleware, (req, res, next) => {
  try {
    const a = db
      .prepare("SELECT classroom_id FROM assignments WHERE id=?")
      .get(req.params.aid);
    if (!a) return res.status(404).json({ detail: "Assignment not found." });
    requireAdmin(a.classroom_id, req.user.id);
    db.prepare("DELETE FROM assignment_submissions WHERE assignment_id=?").run(
      req.params.aid,
    );
    db.prepare("DELETE FROM assignments WHERE id=?").run(req.params.aid);
    res.json({ success: true });
  } catch (e) {
    next(e);
  }
});

// ── ASSIGNMENTS — SUBMIT ─────────────────────────────────────
app.post(
  "/submit_assignment",
  authMiddleware,
  upload.single("file"),
  (req, res, next) => {
    try {
      const { assignment_id } = req.body;
      const asgn = db
        .prepare("SELECT * FROM assignments WHERE id=?")
        .get(assignment_id);
      if (!asgn)
        return res.status(404).json({ detail: "Assignment not found." });

      const status = asgn.due_date >= today() ? "submitted" : "late";
      let fu = "",
        fn = "";
      if (req.file) {
        fu = `/uploads/${req.file.filename}`;
        fn = req.file.originalname;
      }

      const now = new Date().toISOString();
      try {
        db.prepare(
          "INSERT INTO assignment_submissions (id,assignment_id,student_id,file_url,file_name,submitted_at,status) VALUES (?,?,?,?,?,?,?)",
        ).run(uuidv4(), assignment_id, req.user.id, fu, fn, now, status);
      } catch {
        db.prepare(
          "UPDATE assignment_submissions SET file_url=?,file_name=?,submitted_at=?,status=? WHERE assignment_id=? AND student_id=?",
        ).run(fu, fn, now, status, assignment_id, req.user.id);
      }
      res.json({
        success: true,
        status,
        message:
          status === "submitted" ? "Submitted on time!" : "Submitted (late).",
      });
    } catch (e) {
      next(e);
    }
  },
);

// ── SUBMISSIONS — LIST ───────────────────────────────────────
app.get("/submissions/:assignmentId", authMiddleware, (req, res, next) => {
  try {
    const a = db
      .prepare("SELECT classroom_id FROM assignments WHERE id=?")
      .get(req.params.assignmentId);
    if (!a) return res.status(404).json({ detail: "Assignment not found." });
    requireAdmin(a.classroom_id, req.user.id);
    const rows = db
      .prepare(
        "SELECT s.*, u.name AS student_name FROM assignment_submissions s JOIN users u ON s.student_id=u.id WHERE s.assignment_id=? ORDER BY s.submitted_at",
      )
      .all(req.params.assignmentId);
    res.json(rows);
  } catch (e) {
    next(e);
  }
});

// ── POSTS — CREATE ───────────────────────────────────────────
app.post("/post", authMiddleware, (req, res, next) => {
  try {
    const {
      classroom_id,
      type = "announcement",
      title,
      content = "",
    } = req.body;
    requireAdmin(classroom_id, req.user.id);
    const pid = uuidv4();
    db.prepare(
      "INSERT INTO posts (id,classroom_id,user_id,type,title,content) VALUES (?,?,?,?,?,?)",
    ).run(pid, classroom_id, req.user.id, type, title, content);
    res.json({ success: true, post_id: pid });
  } catch (e) {
    next(e);
  }
});

// ── POSTS — UPLOAD MATERIAL ──────────────────────────────────
app.post(
  "/upload_material",
  authMiddleware,
  upload.single("file"),
  (req, res, next) => {
    try {
      const { classroom_id, title, content = "" } = req.body;
      requireAdmin(classroom_id, req.user.id);
      let fu = "",
        fn = "";
      if (req.file) {
        fu = `/uploads/${req.file.filename}`;
        fn = req.file.originalname;
      }
      const pid = uuidv4();
      db.prepare(
        "INSERT INTO posts (id,classroom_id,user_id,type,title,content,file_url,file_name) VALUES (?,?,?,?,?,?,?,?)",
      ).run(pid, classroom_id, req.user.id, "material", title, content, fu, fn);
      res.json({
        success: true,
        post_id: pid,
        file_url: fu,
        message: "Uploaded!",
      });
    } catch (e) {
      next(e);
    }
  },
);

// ── POSTS — LIST ─────────────────────────────────────────────
app.get("/posts/:classroomId", authMiddleware, (req, res, next) => {
  try {
    const rows = db
      .prepare(
        `
      SELECT p.*, u.name AS user_name,
             (SELECT COUNT(*) FROM comments WHERE post_id=p.id) AS comment_count
      FROM posts p JOIN users u ON p.user_id = u.id
      WHERE p.classroom_id = ? ORDER BY p.created_at DESC
    `,
      )
      .all(req.params.classroomId);
    res.json(rows);
  } catch (e) {
    next(e);
  }
});

// ── POSTS — DELETE ───────────────────────────────────────────
app.delete("/post/:pid", authMiddleware, (req, res, next) => {
  try {
    const post = db
      .prepare("SELECT classroom_id FROM posts WHERE id=?")
      .get(req.params.pid);
    if (!post) return res.status(404).json({ detail: "Post not found." });
    requireAdmin(post.classroom_id, req.user.id);
    db.prepare("DELETE FROM comments WHERE post_id=?").run(req.params.pid);
    db.prepare("DELETE FROM posts WHERE id=?").run(req.params.pid);
    res.json({ success: true });
  } catch (e) {
    next(e);
  }
});

// ── COMMENTS — ADD ───────────────────────────────────────────
app.post("/comment", authMiddleware, (req, res, next) => {
  try {
    const { post_id, comment } = req.body;
    db.prepare(
      "INSERT INTO comments (id,post_id,user_id,comment) VALUES (?,?,?,?)",
    ).run(uuidv4(), post_id, req.user.id, comment);
    res.json({ success: true });
  } catch (e) {
    next(e);
  }
});

// ── COMMENTS — LIST ──────────────────────────────────────────
app.get("/comments/:postId", authMiddleware, (req, res, next) => {
  try {
    const rows = db
      .prepare(
        "SELECT cm.*, u.name AS user_name FROM comments cm JOIN users u ON cm.user_id=u.id WHERE cm.post_id=? ORDER BY cm.created_at",
      )
      .all(req.params.postId);
    res.json(rows);
  } catch (e) {
    next(e);
  }
});

// ── NOTIFICATIONS — LIST ─────────────────────────────────────
app.get("/notifications", authMiddleware, (req, res, next) => {
  try {
    const rows = db
      .prepare(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 50",
      )
      .all(req.user.id);
    res.json(rows);
  } catch (e) {
    next(e);
  }
});

// ── NOTIFICATIONS — MARK ALL READ ────────────────────────────
app.post("/notifications/read_all", authMiddleware, (req, res, next) => {
  try {
    db.prepare("UPDATE notifications SET read=1 WHERE user_id=?").run(
      req.user.id,
    );
    res.json({ success: true });
  } catch (e) {
    next(e);
  }
});

// ── HEALTH CHECK ─────────────────────────────────────────────
app.get("/health", (_req, res) => {
  res.json({
    status: "ok",
    version: "10.2.0",
    registration: "direct (no OTP)",
    face_recognition: "NOT INSTALLED — use external microservice",
    smtp_configured: Boolean(SMTP_USER && SMTP_PASS),
    smtp_host: SMTP_USER ? SMTP_HOST : "not configured",
    timestamp: new Date().toISOString(),
  });
});

// ── ERROR HANDLER ────────────────────────────────────────────
app.use(errHandler);

// ── START ────────────────────────────────────────────────────
app.listen(PORT, "0.0.0.0", () => {
  console.log(
    `🚀 NITJ Classroom API v10.2.0 running on http://0.0.0.0:${PORT}`,
  );
});
