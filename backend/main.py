import os
import uuid
import json
import httpx
import jwt
import hmac
import hashlib
import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
import time
from datetime import datetime, timedelta
from typing import List, Optional
from urllib.parse import quote
from fastapi import FastAPI, Depends, HTTPException, Request, Response, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordBearer
from fastapi.responses import StreamingResponse
import mimetypes
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Boolean, ForeignKey, Table, update
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# --- Configuration & Environment ---
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "https://api.myteaspoon.tech/api/v2/auth/callback")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://myteaspoon.tech")
JWT_SECRET = os.getenv("JWT_SECRET")
APP_SECRET = os.getenv("SECRET_KEY").encode()

# --- MinIO Configuration ---
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "http://minio:9000")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY")
MINIO_PUBLIC_URL = os.getenv("MINIO_PUBLIC_URL", "https://api.myteaspoon.tech")
BUCKET_NAME = "teaspoon-files"

s3_client = boto3.client(
    's3',
    endpoint_url=MINIO_ENDPOINT,
    aws_access_key_id=MINIO_ACCESS_KEY,
    aws_secret_access_key=MINIO_SECRET_KEY,
    config=Config(signature_version='s3v4')
)

# Ensure bucket exists on startup
try:
    s3_client.head_bucket(Bucket=BUCKET_NAME)
except ClientError:
    try:
        s3_client.create_bucket(Bucket=BUCKET_NAME)
    except Exception as e:
        print(f"Warning: Could not create MinIO bucket on startup. ({e})")

# --- Database Setup ---
DB_FILE_NAME = os.getenv("DB_FILE", "teaspoon_v1.db")
# Tell SQLAlchemy to use the dynamic filename
SQLALCHEMY_DATABASE_URL = f"sqlite:///./data/{DB_FILE_NAME}"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

user_courses = Table('user_courses', Base.metadata,
                     Column('user_id', Integer, ForeignKey('users.id')),
                     Column('course_code', String, ForeignKey('courses.code'))
                     )


class DBUserAssignment(Base):
    __tablename__ = "user_assignments"
    user_id = Column(Integer, ForeignKey('users.id', ondelete="CASCADE"), primary_key=True)
    assignment_id = Column(Integer, ForeignKey('assignments.id', ondelete="CASCADE"), primary_key=True)
    is_completed = Column(Boolean, default=False)
    grade = Column(Integer, nullable=True)


class DBUser(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    google_id = Column(String, unique=True, index=True)
    email = Column(String, unique=True, index=True)
    name = Column(String)
    picture = Column(String)
    role = Column(String, default="student")
    followed_courses = relationship("DBCourse", secondary=user_courses)


class DBCourse(Base):
    __tablename__ = "courses"
    code = Column(String, primary_key=True, index=True)
    name = Column(String)
    hw_weight = Column(Integer, default=0)
    hw_drop = Column(Integer, default=0)
    ww_weight = Column(Integer, default=0)
    ww_drop = Column(Integer, default=0)
    exam_weight = Column(Integer, default=0)
    hw_magen = Column(Boolean, default=False)
    ww_magen = Column(Boolean, default=False)
    exam_magen = Column(Boolean, default=False)


class DBAssignment(Base):
    __tablename__ = "assignments"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String)
    courseCode = Column(String, ForeignKey("courses.code"))
    type = Column(String)
    deadline = Column(String)
    isOptional = Column(Boolean, default=False)
    attachments = relationship("DBAttachment", back_populates="assignment", cascade="all, delete-orphan")


class DBAttachment(Base):
    __tablename__ = "attachments"
    id = Column(Integer, primary_key=True, index=True)
    assignment_id = Column(Integer, ForeignKey("assignments.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    filename = Column(String)
    object_name = Column(String)
    category = Column(String, default="assignment")

    assignment = relationship("DBAssignment", back_populates="attachments")


class DBAttachmentLike(Base):
    __tablename__ = "attachment_likes"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    attachment_id = Column(Integer, ForeignKey("attachments.id"))


class DBUserStat(Base):
    __tablename__ = "user_stats"
    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    lifetime_likes = Column(Integer, default=0)


class DBAuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    action = Column(String)  # "CREATE", "UPDATE", "DELETE"
    entity_type = Column(String)  # "ASSIGNMENT", "COURSE"
    entity_id = Column(String)
    old_data = Column(String, nullable=True)  # JSON snapshot
    new_data = Column(String, nullable=True)  # JSON snapshot
    status = Column(String, default="PENDING")  # PENDING until approved/denied
    created_at = Column(String, default=lambda: datetime.utcnow().isoformat())

    user = relationship("DBUser")


# Create all tables
Base.metadata.create_all(bind=engine)


# --- Pydantic Schemas ---
class AssignmentCreate(BaseModel):
    title: str
    courseCode: str
    type: str
    deadline: str
    isOptional: bool = False


class CourseUpdate(BaseModel):
    name: str
    hw_weight: int = 0
    hw_drop: int = 0
    ww_weight: int = 0
    ww_drop: int = 0
    exam_weight: int = 0
    hw_magen: bool = False
    ww_magen: bool = False
    exam_magen: bool = False


class AttachmentUpdate(BaseModel):
    filename: str


class GradeUpdate(BaseModel):
    grade: Optional[int]


class CourseCodeUpdate(BaseModel):
    new_code: str


# --- App Setup ---
app = FastAPI(title="Teaspoon API")

# Robust CORS handling with regex catch-all
allowed_origins = [
    "https://myteaspoon.tech",
    "http://localhost:5173",
    "http://localhost:3000",
]
if FRONTEND_URL and FRONTEND_URL not in allowed_origins:
    allowed_origins.append(FRONTEND_URL.strip().rstrip("/"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=r"https?://([a-zA-Z0-9-]+\.)*myteaspoon\.tech",  # Catches any subdomain mismatch
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Anti-Cache Middleware to prevent browsers from holding onto ghost 200 OK errors
@app.middleware("http")
async def prevent_browser_caching(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- Authentication Dependencies ---
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)


def get_optional_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    if not token:
        return None
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except:
        return None


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except:
        raise HTTPException(status_code=401, detail="Invalid token")


def get_admin_user(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    user = db.query(DBUser).filter(DBUser.id == current_user["id"]).first()
    if not user or user.role not in ["admin", "owner"]:
        raise HTTPException(status_code=403, detail="Admin access strictly required")
    return user


def get_write_user(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    user = db.query(DBUser).filter(DBUser.id == current_user["id"]).first()
    if user and user.role == "restricted":
        raise HTTPException(status_code=403, detail="Your account has been restricted from making edits.")
    return current_user  # Returns the normal payload so your routes don't break


# -- Secure MinIO Link Generation --
def generate_secure_download_query(attachment_id: int, expires_in_seconds: int = 3600) -> str:
    """Generates a cryptographically signed query string with an expiration timestamp."""
    expires_at = int(time.time()) + expires_in_seconds
    message = f"{attachment_id}:{expires_at}".encode()
    signature = hmac.new(APP_SECRET, message, hashlib.sha256).hexdigest()
    return f"?expires={expires_at}&sig={signature}"


# --- Auth Routes ---
@app.get("/api/v2/auth/login")
def login_via_google():
    google_auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?client_id={GOOGLE_CLIENT_ID}&response_type=code&redirect_uri={GOOGLE_REDIRECT_URI}&scope=openid%20email%20profile&access_type=offline"
    return RedirectResponse(url=google_auth_url)


@app.get("/api/v2/auth/callback")
async def google_auth_callback(code: str, db: Session = Depends(get_db)):
    async with httpx.AsyncClient() as client:
        token_res = await client.post("https://oauth2.googleapis.com/token", data={
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": GOOGLE_REDIRECT_URI
        })
        token_data = token_res.json()
        if "access_token" not in token_data:
            error_msg = token_data.get("error_description", token_data.get("error", "Unknown error"))
            raise HTTPException(status_code=400, detail=f"Google Error: {error_msg}")

        user_res = await client.get("https://www.googleapis.com/oauth2/v2/userinfo",
                                    headers={"Authorization": f"Bearer {token_data['access_token']}"})
        user_info = user_res.json()

    user = db.query(DBUser).filter(DBUser.google_id == user_info["id"]).first()
    if not user:
        user = DBUser(google_id=user_info["id"], email=user_info["email"], name=user_info["name"],
                      picture=user_info.get("picture", ""))
        db.add(user)
        db.commit()
        db.refresh(user)

    jwt_token = jwt.encode({"sub": user.google_id, "id": user.id, "exp": datetime.utcnow() + timedelta(days=30)},
                           JWT_SECRET, algorithm="HS256")
    return RedirectResponse(url=f"{FRONTEND_URL}/?token={jwt_token}")


@app.get("/api/v2/users/me")
def get_me(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    user = db.query(DBUser).filter(DBUser.id == current_user["id"]).first()

    # 1. Calculate valid likes from this current active semester
    semester_likes = db.query(DBAttachmentLike).join(
        DBAttachment, DBAttachmentLike.attachment_id == DBAttachment.id
    ).filter(DBAttachment.user_id == user.id).count()

    # 2. Grab their preserved "Vault" score from previous semesters
    stats = db.query(DBUserStat).filter(DBUserStat.user_id == user.id).first()
    lifetime = stats.lifetime_likes if stats else 0

    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "picture": user.picture,
        "role": user.role,
        "totalLikesReceived": semester_likes + lifetime  # Combine them!
    }


# --- Course Routes ---
@app.get("/api/v2/courses")
def get_all_courses(db: Session = Depends(get_db)):
    courses = db.query(DBCourse).all()
    return {
        c.code: {
            "name": c.name,
            "hw_weight": c.hw_weight,
            "hw_drop": c.hw_drop,
            "ww_weight": c.ww_weight,
            "ww_drop": c.ww_drop,
            "exam_weight": c.exam_weight,
            "hw_magen": c.hw_magen,
            "ww_magen": c.ww_magen,
            "exam_magen": c.exam_magen
        } for c in courses
    }


@app.put("/api/v2/courses/{course_code}")
def update_course(course_code: str, course_data: dict, current_user: dict = Depends(get_write_user),
                  db: Session = Depends(get_db)):
    user = db.query(DBUser).filter(DBUser.id == current_user["id"]).first()
    course = db.query(DBCourse).filter(DBCourse.code == course_code).first()
    is_trusted = user.role in ["admin", "owner"]

    # Filter frontend keys to match EXACTLY what exists in the database
    valid_data = {}
    dummy = course if course else DBCourse()
    for key, value in course_data.items():
        if hasattr(dummy, key) and not key.startswith("_"):
            valid_data[key] = value

    if not course:
        new_course = DBCourse(code=course_code, **valid_data)
        db.add(new_course)
        if not is_trusted:
            audit_log = DBAuditLog(
                user_id=user.id,
                action="CREATE",
                entity_type="COURSE",
                entity_id=course_code,
                new_data=json.dumps(valid_data),
                status="PENDING"
            )
            db.add(audit_log)
    else:
        # Snapshot existing course before updating safely
        old_data = {key: getattr(course, key) for key in valid_data.keys()}

        for key, value in valid_data.items():
            setattr(course, key, value)

        if not is_trusted:
            audit_log = DBAuditLog(
                user_id=user.id,
                action="UPDATE",
                entity_type="COURSE",
                entity_id=course_code,
                old_data=json.dumps(old_data),
                new_data=json.dumps(valid_data),
                status="PENDING"
            )
            db.add(audit_log)

    db.commit()
    return {"success": True}


@app.put("/api/v2/admin/courses/{old_code}/code")
def update_course_code(old_code: str, payload: CourseCodeUpdate, admin: DBUser = Depends(get_admin_user),
                       db: Session = Depends(get_db)):
    new_code = payload.new_code
    if new_code == old_code:
        return {"success": True}

    course = db.query(DBCourse).filter(DBCourse.code == old_code).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")

    if db.query(DBCourse).filter(DBCourse.code == new_code).first():
        raise HTTPException(status_code=400, detail="Target course code already exists")

    # Clone the course FIRST so the new Foreign Key target exists
    course_dict = {c.name: getattr(course, c.name) for c in course.__table__.columns if c.name != "code"}
    course_dict["code"] = new_code

    new_course = DBCourse(**course_dict)
    db.add(new_course)
    db.flush()  # Pushes the new course to the DB instantly, keeping the transaction open

    # Repoint all assignments to the new course code
    db.query(DBAssignment).filter(DBAssignment.courseCode == old_code).update({"courseCode": new_code})

    # Safely repoint the many-to-many association table
    db.execute(
        update(user_courses)
        .where(user_courses.c.course_code == old_code)
        .values(course_code=new_code)
    )

    # Safely destroy the old course now that nothing depends on it
    db.delete(course)

    # Commit the entire transaction atomically
    db.commit()

    return {"success": True}


@app.get("/api/v2/users/me/courses")
def get_my_courses(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    user = db.query(DBUser).filter(DBUser.id == current_user["id"]).first()
    return [c.code for c in user.followed_courses]


@app.post("/api/v2/users/me/courses")
def update_my_courses(course_codes: List[str], current_user: dict = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    user = db.query(DBUser).filter(DBUser.id == current_user["id"]).first()
    user.followed_courses = db.query(DBCourse).filter(DBCourse.code.in_(course_codes)).all()
    db.commit()
    return {"success": True}


# --- Assignment Routes ---
@app.get("/api/v2/assignments")
def get_assignments(optional_user: dict = Depends(get_optional_user), db: Session = Depends(get_db)):
    assignments = db.query(DBAssignment).all()
    user_data = {}

    if optional_user:
        entries = db.query(DBUserAssignment).filter(DBUserAssignment.user_id == optional_user["id"]).all()
        user_data = {e.assignment_id: {"completed": e.is_completed, "grade": e.grade} for e in entries}

    results = []
    for a in assignments:
        attachments = []
        for att in a.attachments:
            try:
                # Generate a mathematically un-guessable 1-hour presigned URL
                secure_query = generate_secure_download_query(att.id, expires_in_seconds=3600)
                url = f"{MINIO_PUBLIC_URL}/api/v2/attachments/{att.id}/download{secure_query}"

                # Dynamically calculate likes and user status
                likes_count = db.query(DBAttachmentLike).filter(DBAttachmentLike.attachment_id == att.id).count()

                is_liked = False
                if optional_user:
                    is_liked = db.query(DBAttachmentLike).filter(
                        DBAttachmentLike.attachment_id == att.id,
                        DBAttachmentLike.user_id == optional_user["id"]
                    ).first() is not None

                attachments.append({
                    "id": att.id,
                    "filename": att.filename,
                    "url": url,
                    "uploader_id": att.user_id,
                    "category": att.category,
                    "likes": likes_count,
                    "isLikedByMe": is_liked
                })
            except Exception as e:
                print(f"Error generating url for {att.filename}: {e}")

        results.append({
            "id": a.id,
            "title": a.title,
            "courseCode": a.courseCode,
            "type": a.type,
            "deadline": a.deadline,
            "isOptional": a.isOptional,
            "isCompleted": user_data.get(a.id, {}).get("completed", False),
            "grade": user_data.get(a.id, {}).get("grade", None),
            "attachments": attachments
        })
    return results


@app.post("/api/v2/assignments")
def create_assignment(assignment: AssignmentCreate, current_user: dict = Depends(get_write_user),
                      db: Session = Depends(get_db)):
    user = db.query(DBUser).filter(DBUser.id == current_user["id"]).first()

    new_assignment = DBAssignment(**assignment.dict())
    db.add(new_assignment)
    db.commit()
    db.refresh(new_assignment)

    # Send for admin approval if not owner or admin
    if user and user.role not in ["admin", "owner"]:
        audit_log = DBAuditLog(
            user_id=user.id,
            action="CREATE",
            entity_type="ASSIGNMENT",
            entity_id=f"{new_assignment.id}:{new_assignment.courseCode} - {new_assignment.title}",
            new_data=json.dumps(assignment.dict()),
            status="PENDING"
        )
        db.add(audit_log)
        db.commit()

    return new_assignment


@app.put("/api/v2/assignments/{assignment_id}")
def update_assignment(assignment_id: int, assignment: AssignmentCreate,
                      current_user: dict = Depends(get_write_user),
                      db: Session = Depends(get_db)):
    assignment = db.query(DBAssignment).filter(DBAssignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")

    old_data = {
        "title": assignment.title,
        "courseCode": assignment.courseCode,
        "type": assignment.type,
        "deadline": assignment.deadline,
        "isOptional": assignment.isOptional
    }

    # Always apply changes optimistically
    for key, value in assignment.dict().items():
        setattr(assignment, key, value)

    # Send for admin approval if not owner or admin
    user = db.query(DBUser).filter(DBUser.id == current_user["id"]).first()
    if user and user.role != "admin":
        audit_log = DBAuditLog(
            user_id=user.id,
            action="UPDATE",
            entity_type="ASSIGNMENT",
            entity_id=f"{assignment.id}:{assignment.courseCode} - {assignment.title}",
            old_data=json.dumps(old_data),
            new_data=json.dumps(assignment.dict()),
            status="PENDING"  # Stored for Admin Approval
        )
        db.add(audit_log)

    db.commit()
    db.refresh(assignment)
    return assignment


@app.post("/api/v2/attachments/{attachment_id}/like")
def toggle_like(attachment_id: int, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    existing_like = db.query(DBAttachmentLike).filter(
        DBAttachmentLike.user_id == current_user["id"],
        DBAttachmentLike.attachment_id == attachment_id
    ).first()

    if existing_like:
        db.delete(existing_like)
    else:
        new_like = DBAttachmentLike(user_id=current_user["id"], attachment_id=attachment_id)
        db.add(new_like)

    db.commit()
    return {"success": True}


@app.delete("/api/v2/assignments/{assignment_id}")
def delete_assignment(assignment_id: int, current_user: dict = Depends(get_write_user),
                      db: Session = Depends(get_db)):
    user = db.query(DBUser).filter(DBUser.id == current_user["id"]).first()
    assignment = db.query(DBAssignment).filter(DBAssignment.id == assignment_id).first()

    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")

    is_trusted = user and user.role in ["admin", "owner"]

    if not is_trusted:
        old_data = {
            "title": assignment.title,
            "courseCode": assignment.courseCode,
            "type": assignment.type,
            "deadline": assignment.deadline,
            "isOptional": assignment.isOptional,
            "attachments": [
                {
                    "id": att.id,
                    "user_id": att.user_id,
                    "filename": att.filename,
                    "object_name": att.object_name,
                    "category": att.category
                } for att in assignment.attachments
            ]
        }

        audit_log = DBAuditLog(
            user_id=user.id,
            action="DELETE",
            entity_type="ASSIGNMENT",
            entity_id=f"{assignment.id}:{assignment.courseCode} - {assignment.title}",
            old_data=json.dumps(old_data),
            status="PENDING"
        )
        db.add(audit_log)

    else:
        # Admin/Owner: delete the files from MinIO instantly
        for att in assignment.attachments:
            try:
                s3_client.delete_object(Bucket=BUCKET_NAME, Key=att.object_name)
            except Exception:
                pass

    db.delete(assignment)
    db.commit()
    return {"success": True}


@app.post("/api/v2/assignments/{assignment_id}/toggle")
def toggle_assignment_completion(assignment_id: int, current_user: dict = Depends(get_current_user),
                                 db: Session = Depends(get_db)):
    entry = db.query(DBUserAssignment).filter_by(user_id=current_user["id"], assignment_id=assignment_id).first()
    if not entry:
        entry = DBUserAssignment(user_id=current_user["id"], assignment_id=assignment_id, is_completed=True)
        db.add(entry)
    else:
        entry.is_completed = not entry.is_completed
    db.commit()
    return {"success": True}


@app.post("/api/v2/assignments/{assignment_id}/grade")
def update_assignment_grade(assignment_id: int, grade_data: GradeUpdate, current_user: dict = Depends(get_current_user),
                            db: Session = Depends(get_db)):
    entry = db.query(DBUserAssignment).filter_by(user_id=current_user["id"], assignment_id=assignment_id).first()
    if not entry:
        entry = DBUserAssignment(user_id=current_user["id"], assignment_id=assignment_id, grade=grade_data.grade)
        db.add(entry)
    else:
        entry.grade = grade_data.grade
    db.commit()
    return {"success": True}


# --- Calendar Routes ---
@app.get("/api/v2/calendar/feed")
def get_calendar_feed(token: Optional[str] = None, courses: Optional[str] = None, db: Session = Depends(get_db)):
    target_courses = []

    if token:
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            user_id = payload.get("id")
            user = db.query(DBUser).filter(DBUser.id == user_id).first()
            if user:
                target_courses = [c.code for c in user.followed_courses]
        except Exception:
            pass

    if not target_courses and courses:
        target_courses = [c.strip() for c in courses.split(",") if c.strip()]

    if not target_courses:
        assignments = []
    else:
        assignments = db.query(DBAssignment).filter(DBAssignment.courseCode.in_(target_courses)).all()

    # Build a lookup so each event shows ITS OWN course name (not a shared one)
    course_codes = {a.courseCode for a in assignments if a.courseCode}
    course_map = {
        c.code: c.name
        for c in db.query(DBCourse).filter(DBCourse.code.in_(course_codes)).all()
    } if course_codes else {}

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Teaspoon//IL",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Teaspoon Assignments",
        "X-WR-TIMEZONE:UTC"
    ]

    now_str = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    for a in assignments:
        if not a.deadline:
            continue
        try:
            # Parse the deadline into a real datetime so we can shift it
            raw = a.deadline
            if "." in raw:
                raw = raw.split(".")[0]
            raw = raw.rstrip("Z")
            # Try the most common ISO formats
            try:
                end_dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                end_dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")

            start_dt = end_dt - timedelta(minutes=30)
            dt_end_str = end_dt.strftime("%Y%m%dT%H%M%SZ")
            dt_start_str = start_dt.strftime("%Y%m%dT%H%M%SZ")
        except Exception:
            continue

        title = (a.title or "Assignment").replace("\r", "").replace("\n", " ")
        # Resolve THIS assignment's course name individually
        course_name = course_map.get(a.courseCode, a.courseCode or "")
        course_label = f"{course_name}" if course_name and course_name != a.courseCode else (a.courseCode or "")
        desc = f"סוג: {a.type} | קורס: {course_label}".replace("\r", "").replace("\n", " ")

        lines.extend([
            "BEGIN:VEVENT",
            f"UID:assignment-{a.id}@teaspoon",
            f"DTSTAMP:{now_str}",
            f"DTSTART:{dt_start_str}",
            f"DTEND:{dt_end_str}",
            f"SUMMARY:{course_label} - {title}",
            f"DESCRIPTION:{desc}",
            "END:VEVENT"
        ])

    lines.append("END:VCALENDAR")
    ics_content = "\r\n".join(lines)

    return Response(content=ics_content, media_type="text/calendar",
                    headers={"Content-Disposition": 'attachment; filename="teaspoon.ics"'})


# --- File Attachments ---
@app.post("/api/v2/assignments/{assignment_id}/attachments")
async def upload_attachment(assignment_id: int, file: UploadFile = File(...), category: str = Form("assignment"),
                            current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    assignment = db.query(DBAssignment).filter(DBAssignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")

    ext = os.path.splitext(file.filename)[1]
    object_name = f"{uuid.uuid4()}{ext}"

    try:
        s3_client.upload_fileobj(file.file, BUCKET_NAME, object_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"MinIO Upload Error: {str(e)}")

    new_attachment = DBAttachment(assignment_id=assignment_id, user_id=current_user["id"], filename=file.filename,
                                  object_name=object_name, category=category)
    db.add(new_attachment)
    db.commit()
    db.refresh(new_attachment)
    return {"id": new_attachment.id, "filename": new_attachment.filename, "category": new_attachment.category}


@app.put("/api/v2/attachments/{attachment_id}")
def update_attachment(attachment_id: int, data: AttachmentUpdate, current_user: dict = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    attachment = db.query(DBAttachment).filter(DBAttachment.id == attachment_id).first()
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")

    user = db.query(DBUser).filter(DBUser.id == current_user["id"]).first()
    if attachment.user_id != current_user["id"] and user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized to edit this file")

    attachment.filename = data.filename
    db.commit()
    return {"success": True}


@app.delete("/api/v2/attachments/{attachment_id}")
def delete_attachment(attachment_id: int, current_user: dict = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    attachment = db.query(DBAttachment).filter(DBAttachment.id == attachment_id).first()
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")

    user = db.query(DBUser).filter(DBUser.id == current_user["id"]).first()
    if attachment.user_id != current_user["id"] and user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized to delete this file")

    try:
        s3_client.delete_object(Bucket=BUCKET_NAME, Key=attachment.object_name)
    except Exception:
        pass

    db.delete(attachment)
    db.commit()
    return {"success": True}


@app.get("/api/v2/attachments/{attachment_id}/download")
def download_attachment(attachment_id: int, expires: int, sig: str, db: Session = Depends(get_db)):
    # Check if the link has expired
    if int(time.time()) > expires:
        raise HTTPException(status_code=403, detail="Download link has expired. Please refresh the page.")

    # Cryptographically verify that nobody tampered with the ID or timestamp
    expected_message = f"{attachment_id}:{expires}".encode()
    expected_sig = hmac.new(APP_SECRET, expected_message, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(sig, expected_sig):
        raise HTTPException(status_code=403, detail="Invalid or tampered download signature.")

    att = db.query(DBAttachment).filter(DBAttachment.id == attachment_id).first()
    if not att:
        raise HTTPException(status_code=404, detail="Attachment not found")

    try:
        s3_response = s3_client.get_object(Bucket=BUCKET_NAME, Key=att.object_name)

        def file_stream():
            for chunk in s3_response['Body'].iter_chunks():
                yield chunk

        content_type, _ = mimetypes.guess_type(att.filename)
        encoded_filename = quote(att.filename)

        return StreamingResponse(
            file_stream(),
            media_type=content_type or "application/octet-stream",
            headers={"Content-Disposition": f"inline; filename*=utf-8''{encoded_filename}"}
        )
    except Exception as e:
        print(f"MinIO Download Error: {e}")
        raise HTTPException(status_code=500, detail="Error retrieving file from storage")


# --- Admin Dashboard Routes ---
class RoleUpdate(BaseModel):
    role: str


@app.get("/api/v2/admin/users")
def get_all_users(admin: DBUser = Depends(get_admin_user), db: Session = Depends(get_db)):
    users = db.query(DBUser).all()
    return [{"id": u.id, "name": u.name, "email": u.email, "role": u.role, "picture": u.picture} for u in users]


@app.put("/api/v2/admin/users/{target_user_id}/role")
def update_user_role(target_user_id: int, role_data: RoleUpdate, admin: DBUser = Depends(get_admin_user),
                     db: Session = Depends(get_db)):
    if role_data.role not in ["admin", "user", "restricted"]:
        raise HTTPException(status_code=400, detail="Invalid role definition")

    user = db.query(DBUser).filter(DBUser.id == target_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.role == "owner":
        raise HTTPException(status_code=403, detail="Cannot modify the role of an owner.")

    user.role = role_data.role
    db.commit()
    return {"success": True, "new_role": user.role}


@app.get("/api/v2/admin/logs")
def get_audit_logs(limit: int = 50, admin: DBUser = Depends(get_admin_user), db: Session = Depends(get_db)):
    # Only fetch PENDING logs!
    logs = db.query(DBAuditLog).filter(DBAuditLog.status == "PENDING").order_by(DBAuditLog.id.desc()).limit(limit).all()

    result = []
    for log in logs:
        user = db.query(DBUser).filter(DBUser.id == log.user_id).first()
        result.append({
            "id": log.id,
            "user_name": user.name if user else "Unknown User",
            "user_email": user.email if user else "unknown@email.com",
            "action": log.action,
            "entity_type": log.entity_type,
            "entity_id": log.entity_id,
            "old_data": log.old_data,
            "new_data": log.new_data,
            "status": log.status,
            "created_at": log.created_at
        })
    return result


@app.post("/api/v2/admin/logs/{log_id}/approve")
def approve_change(log_id: int, admin: DBUser = Depends(get_admin_user), db: Session = Depends(get_db)):
    log = db.query(DBAuditLog).filter(DBAuditLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")

    # Wipe files from MinIO if the change is a DELETE
    if log.action == "DELETE" and log.entity_type == "ASSIGNMENT":
        old_data = json.loads(log.old_data)
        for att in old_data.get("attachments", []):
            try:
                s3_client.delete_object(Bucket=BUCKET_NAME, Key=att["object_name"])
            except Exception as e:
                print(f"MinIO Delete Error: {e}")

    # The change is already live, so we simply delete the pending ticket
    db.delete(log)
    db.commit()
    return {"success": True, "message": "Change approved and log cleared."}


@app.post("/api/v2/admin/logs/{log_id}/revert")
def revert_change(log_id: int, admin: DBUser = Depends(get_admin_user), db: Session = Depends(get_db)):
    log = db.query(DBAuditLog).filter(DBAuditLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")

    if log.entity_type == "ASSIGNMENT":
        real_id = int(log.entity_id.split(":")[0]) if ":" in log.entity_id else int(log.entity_id)
        if log.action == "UPDATE":
            old_data = json.loads(log.old_data)
            assignment = db.query(DBAssignment).filter(DBAssignment.id == real_id).first()
            if assignment:
                for key, value in old_data.items():
                    setattr(assignment, key, value)
        elif log.action == "CREATE":
            # Reverting a creation means deleting it
            assignment = db.query(DBAssignment).filter(DBAssignment.id == real_id).first()
            if assignment:
                db.delete(assignment)
        elif log.action == "DELETE":
            # Reverting a deletion means recreating it with the EXACT old ID and connecting to the attachments
            old_data = json.loads(log.old_data)
            attachments_data = old_data.pop("attachments", [])
            restored_assignment = DBAssignment(id=real_id, **old_data)
            db.add(restored_assignment)
            for att in attachments_data:
                restored_att = DBAttachment(
                    id=att["id"],
                    assignment_id=restored_assignment.id,
                    user_id=att["user_id"],
                    filename=att["filename"],
                    object_name=att["object_name"],
                    category=att["category"]
                )
                db.add(restored_att)

    elif log.entity_type == "COURSE":
        course = db.query(DBCourse).filter(DBCourse.code == log.entity_id).first()
        if log.action == "CREATE":
            if course: db.delete(course)
        elif log.action == "UPDATE":
            old_data = json.loads(log.old_data)
            if course:
                for key, value in old_data.items():
                    setattr(course, key, value)

    db.delete(log)
    db.commit()
    return {"success": True, "message": "Change reverted."}


@app.post("/api/v2/admin/logs/{log_id}/reject_and_block")
def reject_and_block(log_id: int, admin: DBUser = Depends(get_admin_user), db: Session = Depends(get_db)):
    log = db.query(DBAuditLog).filter(DBAuditLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")

    # 1. Block the spammer (unless they are somehow an admin/owner)
    spammer = db.query(DBUser).filter(DBUser.id == log.user_id).first()
    if spammer and spammer.role not in ["admin", "owner"]:
        spammer.role = "restricted"

    # 2. Extract the real ID using the new format
    real_id_str = log.entity_id.split(":")[0] if ":" in log.entity_id else log.entity_id

    # 3. Revert the change (identical to revert logic)
    if log.entity_type == "ASSIGNMENT":
        real_id = int(real_id_str)
        if log.action == "UPDATE":
            old_data = json.loads(log.old_data)
            assignment = db.query(DBAssignment).filter(DBAssignment.id == real_id).first()
            if assignment:
                for key, value in old_data.items():
                    setattr(assignment, key, value)
        elif log.action == "CREATE":
            assignment = db.query(DBAssignment).filter(DBAssignment.id == real_id).first()
            if assignment:
                db.delete(assignment)
        elif log.action == "DELETE":
            old_data = json.loads(log.old_data)
            attachments_data = old_data.pop("attachments", [])
            restored_assignment = DBAssignment(id=real_id, **old_data)
            db.add(restored_assignment)
            for att in attachments_data:
                db.add(DBAttachment(id=att["id"], assignment_id=restored_assignment.id, user_id=att["user_id"],
                                    filename=att["filename"], object_name=att["object_name"], category=att["category"]))

    elif log.entity_type == "COURSE":
        course = db.query(DBCourse).filter(DBCourse.code == real_id_str).first()
        if log.action == "CREATE" and course:
            db.delete(course)
        elif log.action == "UPDATE" and course:
            for key, value in json.loads(log.old_data).items():
                setattr(course, key, value)

    db.delete(log)
    db.commit()
    return {"success": True, "message": "Change reverted and user restricted."}